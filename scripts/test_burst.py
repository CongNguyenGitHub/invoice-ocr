import argparse
import asyncio
import json
import random
import time
from pathlib import Path

import httpx

API_URL = "http://127.0.0.1:8000/api/v1/invoices"

async def process_single_image(client: httpx.AsyncClient, item: dict, index: int) -> dict:
    url = item.get("file")
    if not url:
        return {"status": "error", "error": "No URL"}

    start_time = time.time()

    # 1. Download image from the GotIt CDN/URL
    try:
        resp = await client.get(url, timeout=15.0)
        resp.raise_for_status()
        image_bytes = resp.content
    except Exception as e:
        return {
            "index": index,
            "status": "download_failed",
            "url": url,
            "error": str(e),
            "time": time.time() - start_time
        }

    # 2. Submit job to local API
    try:
        store_type = item.get("type", "")
        # The API expects the exact Enum value. If standard JSON has 'aeon', it maps correctly.
        files = {
            "image": ("receipt.jpg", image_bytes, "image/jpeg")
        }
        data = {}
        if store_type and store_type != "unknown":
            data["store_type"] = store_type

        resp = await client.post(API_URL, files=files, data=data, timeout=30.0)
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP Error: {e.response.status_code} - {e.response.text}"
        return {
            "index": index,
            "status": "submit_failed",
            "url": url,
            "error": error_msg,
            "time": time.time() - start_time
        }
    except Exception as e:
        return {
            "index": index,
            "status": "submit_failed",
            "url": url,
            "error": repr(e),
            "time": time.time() - start_time
        }

    # 3. Poll for completion
    while True:
        try:
            poll_resp = await client.get(f"{API_URL}/{job_id}", timeout=10.0)
            poll_resp.raise_for_status()
            job_data = poll_resp.json()
            status = job_data["status"]

            if status in ("completed", "failed"):
                end_time = time.time()
                return {
                    "index": index,
                    "job_id": job_id,
                    "status": status,
                    "error": job_data.get("error"),
                    "time": end_time - start_time,
                    "url": url
                }
        except Exception as e:
            return {
                "index": index,
                "job_id": job_id,
                "status": "poll_failed",
                "error": str(e),
                "time": time.time() - start_time
            }

        await asyncio.sleep(2.0)

async def main():
    parser = argparse.ArgumentParser(description="Burst test invoice OCR API")
    parser.add_argument("-n", "--num", type=int, default=10, help="Number of images to test")
    parser.add_argument("-f", "--file", type=str, default="label_minitet_festive_24_v3_public.json", help="Path to JSON label file")
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File {args.file} not found.")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Filter items with valid URLs
    valid_items = [item for item in data if item.get("file")]

    if not valid_items:
        print("No valid URLs found in the JSON file.")
        return

    num_samples = min(args.num, len(valid_items))
    samples = random.sample(valid_items, num_samples)

    print(f"\n🚀 Selecting {num_samples} images for burst testing...")
    print("Starting concurrent burst (downloading and submitting)...\n")

    start_time = time.time()

    # Use limits to avoid completely exhausting local sockets if N is very large
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [process_single_image(client, item, i) for i, item in enumerate(samples)]
        results = await asyncio.gather(*tasks)

    total_time = time.time() - start_time

    # Calculate metrics
    successes = [r for r in results if r["status"] == "completed"]
    failures = [r for r in results if r["status"] not in ("completed", "download_failed")]
    downloads_failed = [r for r in results if r["status"] == "download_failed"]

    success_times = [r["time"] for r in successes]

    print("=" * 50)
    print("📊 BURST TEST RESULTS")
    print("=" * 50)
    print(f"Total Requests       : {num_samples}")
    print(f"Total Wall Time      : {total_time:.2f}s")
    print(f"Successful OCR Jobs  : {len(successes)}")
    print(f"Failed OCR Jobs      : {len(failures)}")
    if downloads_failed:
        print(f"Image D/L Failed     : {len(downloads_failed)} (External image links dead/timed out, not counted via API)")

    if success_times:
        print("\n⏱️ Performance (Successful jobs lifecycle):")
        print(f"  Average Time : {sum(success_times)/len(success_times):.2f}s")
        print(f"  Min Time     : {min(success_times):.2f}s")
        print(f"  Max Time     : {max(success_times):.2f}s")
        # Approximate RPS (assuming they are processing fully in parallel)
        print(f"  System T-Put : {len(successes) / total_time:.2f} jobs/second")

    if failures:
        print("\n❌ Failure Details (First 10):")
        for f in failures[:10]:
            job_id = f.get('job_id', 'N/A')
            print(f"  - Job {job_id} (Status: {f['status']}): {f.get('error')}")

if __name__ == "__main__":
    asyncio.run(main())
