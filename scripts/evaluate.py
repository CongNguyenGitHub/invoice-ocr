"""
Invoice OCR — Batch Evaluation Script

Evaluates the OCR pipeline against labeled ground truth data.
Downloads receipt images from public URLs and compares extracted
results with the labels in label_minitet_festive_24_v3_public.json.

Usage:
    python scripts/evaluate.py --sample 50
    python scripts/evaluate.py --input label_minitet_festive_24_v3_public.json --output report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.observability.logging import setup_logging

setup_logging("INFO")
logger = logging.getLogger(__name__)


@dataclass
class FieldAccuracy:
    total: int = 0
    correct: int = 0
    empty_pred: int = 0
    empty_gt: int = 0

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return self.correct / self.total

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": round(self.accuracy * 100, 1),
            "empty_pred": self.empty_pred,
            "empty_gt": self.empty_gt,
        }


@dataclass
class EvalReport:
    total_samples: int = 0
    successful: int = 0
    failed: int = 0
    field_accuracy: dict[str, FieldAccuracy] = field(default_factory=dict)
    per_store: dict[str, dict] = field(default_factory=dict)
    avg_time: float = 0.0
    errors: list[str] = field(default_factory=list)
    mismatches: dict[str, list[dict]] = field(default_factory=dict)


def _normalize_money_for_compare(value: str) -> str:
    """Normalize money for comparison: strip everything except digits."""
    if not value:
        return ""
    import re
    v = re.sub(r'[^\d]', '', str(value))
    return v.lstrip('0') or '0'


def _normalize_label_record(record: dict) -> dict:
    """Normalize a ground truth label record to unified format."""
    # Unify product fields
    products = []
    for p in record.get("products", []):
        products.append({
            "product_id": p.get("product_id", p.get("product_code", "")),
            "product_name": p.get("product_name", ""),
            "product_unit_price": p.get("product_unit_price", ""),
            "product_quantity": p.get("product_quantity", p.get("product_amount", "")),
            "product_total_money": p.get("product_total_money", ""),
        })

    return {
        "name": record.get("name", ""),
        "type": record.get("type", ""),
        "date": record.get("date", ""),
        "time": record.get("time", ""),
        "pos_id": record.get("pos_id", ""),
        "receipt_number": record.get("receipt_number", ""),
        "cashier": record.get("cashier", ""),
        "total_money": record.get("total_money", ""),
        "barcode": record.get("barcode", ""),
        "products": products,
    }


def compare_field(pred: str, gt: str, field_name: str) -> bool | None:
    """Compare a single field between prediction and ground truth."""
    if not gt:
        return None  # Nothing to compare against in ground truth
    if not pred:
        return False

    pred = str(pred).strip().lower()
    gt = str(gt).strip().lower()

    if field_name in ("total_money", "product_unit_price", "product_total_money"):
        return _normalize_money_for_compare(pred) == _normalize_money_for_compare(gt)
        
    if field_name == "barcode":
        return pred.replace('*', '') == gt.replace('*', '')
        
    if field_name == "product_quantity":
        import re
        from src.pipeline.postprocessor import _normalize_quantity
        return _normalize_quantity(pred) == _normalize_quantity(gt)
        
    if field_name == "product_name":
        import difflib
        return difflib.SequenceMatcher(None, pred, gt).ratio() > 0.95

    return pred == gt


def evaluate_single(pred: dict, gt: dict) -> tuple[dict[str, list[bool]], list[dict]]:
    """Compare predicted vs ground truth for a single record."""
    results = {}
    mismatches = []
    
    # Base fields
    base_fields = ["name", "type", "date", "time", "pos_id", "receipt_number", "cashier", "total_money", "barcode"]
    for field_name in base_fields:
        gt_val = gt.get(field_name, "")
        pred_val = pred.get(field_name, "")
        val = compare_field(pred_val, gt_val, field_name)
        if val is not None:
            results.setdefault(field_name, []).append(val)
            if not val:
                mismatches.append({
                    "field": field_name,
                    "expected": str(gt_val).strip(),
                    "predicted": str(pred_val).strip()
                })
        
    # Product fields
    gt_products = gt.get("products", [])
    raw_pred_products = pred.get("products", [])
    
    # Greedy alignment of products to prevent order swaps from destroying accuracy
    aligned_pred = []
    used_pred_indices = set()
    
    for gt_idx, gt_p in enumerate(gt_products):
        best_match_idx = -1
        best_match_score = -1
        
        gt_price = _normalize_money_for_compare(gt_p.get("product_unit_price", ""))
        gt_total = _normalize_money_for_compare(gt_p.get("product_total_money", ""))
        gt_name = str(gt_p.get("product_name", "")).strip().lower()
        
        for pred_idx, pred_p in enumerate(raw_pred_products):
            if pred_idx in used_pred_indices: continue
            
            pr_price = _normalize_money_for_compare(pred_p.get("product_unit_price", ""))
            pr_total = _normalize_money_for_compare(pred_p.get("product_total_money", ""))
            pr_name = str(pred_p.get("product_name", "")).strip().lower()
            
            score = 0
            if gt_price and pr_price == gt_price: score += 2
            if gt_total and pr_total == gt_total: score += 2
            if gt_name and pr_name == gt_name: score += 3
            
            if score > best_match_score:
                best_match_score = score
                best_match_idx = pred_idx
                
        if best_match_idx != -1 and best_match_score > 0:
            aligned_pred.append(raw_pred_products[best_match_idx])
            used_pred_indices.add(best_match_idx)
        else:
            aligned_pred.append({})
            
    # Append leftover unmapped preds
    for pred_idx, pred_p in enumerate(raw_pred_products):
        if pred_idx not in used_pred_indices:
            aligned_pred.append(pred_p)
            
    pred_products = aligned_pred
    product_fields = ["product_id", "product_name", "product_unit_price", "product_quantity", "product_total_money"]
    
    for i in range(max(len(gt_products), len(pred_products))):
        gt_p = gt_products[i] if i < len(gt_products) else {}
        pred_p = pred_products[i] if i < len(pred_products) else {}
        
        for field_name in product_fields:
            gt_val = gt_p.get(field_name, "")
            pred_val = pred_p.get(field_name, "")
            val = compare_field(pred_val, gt_val, field_name)
            if val is not None:
                results.setdefault(field_name, []).append(val)
                if not val:
                    mismatches.append({
                        "field": f"product[{i}].{field_name}",
                        "expected": str(gt_val).strip(),
                        "predicted": str(pred_val).strip()
                    })
            
    return results, mismatches


def download_image(url: str, cache_dir: Path) -> bytes | None:
    """Download an image, caching locally."""
    # Create a filename from the URL
    filename = url.split("/")[-1]
    cache_path = cache_dir / filename

    if cache_path.exists():
        return cache_path.read_bytes()

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.content
            cache_path.write_bytes(data)
            return data
    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Evaluate Invoice OCR pipeline")
    parser.add_argument("--input", default="label_minitet_festive_24_v3_public.json", help="Labels file")
    parser.add_argument("--output", default="evaluation_report.json", help="Output report file")
    parser.add_argument("--sample", type=int, default=0, help="Evaluate only N random samples (0=all)")
    parser.add_argument("--cache-dir", default="data/eval_images", help="Image cache directory")
    parser.add_argument("--store-type", default=None, help="Filter to specific store type")
    args = parser.parse_args()

    # Load labels
    logger.info(f"Loading labels from {args.input}")
    with open(args.input, 'r', encoding='utf-8') as f:
        labels = json.load(f)

    # Filter by store type
    if args.store_type:
        labels = [r for r in labels if r.get('type') == args.store_type]
        logger.info(f"Filtered to {len(labels)} records of type '{args.store_type}'")

    # Sample
    if args.sample > 0 and args.sample < len(labels):
        import random
        random.seed(42)
        labels = random.sample(labels, args.sample)
        logger.info(f"Sampled {args.sample} records")

    # Prepare
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = EvalReport()
    report.total_samples = len(labels)

    from src.pipeline.runner import run_pipeline

    times = []

    for i, record in enumerate(labels):
        gt = _normalize_label_record(record)
        img_url = record.get("file", "")
        store_type = record.get("type", "unknown")

        logger.info(f"[{i+1}/{len(labels)}] Processing {store_type}: {img_url[-40:]}")

        # Download image
        img_data = download_image(img_url, cache_dir)
        if img_data is None:
            report.failed += 1
            report.errors.append(f"Download failed: {img_url}")
            continue

        # Run pipeline
        try:
            t0 = time.perf_counter()
            result = run_pipeline(img_data)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            pred = result.invoice.model_dump()
        except Exception as e:
            report.failed += 1
            report.errors.append(f"Pipeline error for {img_url}: {e}")
            continue

        report.successful += 1

        # Compare fields
        field_results, mismatches = evaluate_single(pred, gt)
        if mismatches:
            report.mismatches[img_url] = mismatches
            
        for field_name, is_correct_list in field_results.items():
            if field_name not in report.field_accuracy:
                report.field_accuracy[field_name] = FieldAccuracy()
            fa = report.field_accuracy[field_name]
            for is_correct in is_correct_list:
                fa.total += 1
                if is_correct:
                    fa.correct += 1

        # Per-store tracking
        if store_type not in report.per_store:
            report.per_store[store_type] = {"total": 0, "correct_total_money": 0}
        report.per_store[store_type]["total"] += 1
        
        # Check if total money exists, and check if all is correct for this receipt
        money_list = field_results.get("total_money", [])
        if money_list and all(money_list):
            report.per_store[store_type]["correct_total_money"] += 1

    # Compute averages
    if times:
        report.avg_time = sum(times) / len(times)

    # Compute average accuracy across fields
    total_acc = sum(fa.accuracy for fa in report.field_accuracy.values())
    avg_acc = total_acc / len(report.field_accuracy) if report.field_accuracy else 0.0

    # Output report
    output = {
        "total_samples": report.total_samples,
        "successful": report.successful,
        "failed": report.failed,
        "avg_processing_time_seconds": round(report.avg_time, 2),
        "overall_average_accuracy": round(avg_acc * 100, 1),
        "field_accuracy": {k: v.to_dict() for k, v in report.field_accuracy.items()},
        "per_store_type": report.per_store,
        "errors": report.errors[:20],  # Limit errors in report
        "mismatches": report.mismatches,
    }

    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Print summary
    print(f"\n{'='*50}")
    print(f"EVALUATION REPORT")
    print(f"{'='*50}")
    print(f"Total: {report.total_samples} | Success: {report.successful} | Failed: {report.failed}")
    print(f"Avg time: {report.avg_time:.2f}s")
    print(f"\nOverall Average Accuracy: {avg_acc*100:.1f}%\n")
    print(f"\nField Accuracy:")
    for name, fa in report.field_accuracy.items():
        print(f"  {name:20s}: {fa.accuracy*100:5.1f}% ({fa.correct}/{fa.total})")
    print(f"\nReport saved to: {args.output}")


if __name__ == "__main__":
    main()
