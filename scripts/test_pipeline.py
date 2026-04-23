import logging
import traceback

from src.pipeline.runner import run_pipeline

logging.basicConfig(level=logging.INFO)

try:
    with open('temp_api_test/1705716201fXfon_blob', 'rb') as f:
        image_data = f.read()
    result = run_pipeline(image_data)
    print('=== RESULT ===')
    print(result.invoice.model_dump_json(indent=2))
    print('\n=== METADATA ===')
    print(f'Hash: {result.image_hash}')
    print(f'YOLO conf: {result.yolo_confidence:.3f}')
    print(f'Fallback: {result.yolo_used_fallback}')
    print(f'Timings: {result.timings}')
except Exception:
    print(traceback.format_exc())
