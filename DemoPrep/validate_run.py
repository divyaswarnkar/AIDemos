import json
from pathlib import Path
ROOT = Path(__file__).resolve().parent
required = [ROOT / "recommendations.json", ROOT / "approval-card.json", ROOT / "approval-summary.md", ROOT / "price-comparison.xlsx"]
rec = json.loads((ROOT / "recommendations.json").read_text())
summary = (ROOT / "approval-summary.md").read_text()
CATALOG = {"ZO-TNT-2P":{"current_price":219,"margin_floor_price":165,"match_type":"comparable"},"ZO-BAG-20":{"current_price":149,"margin_floor_price":110,"match_type":"comparable"},"ZO-STV-01":{"current_price":59,"matgin_floor_price":44,"match_type":"comparable"},"ZO-PCK-45":{"current_price":129,"margin_floor_price":95,"match_type":"comparable"},"ZO-SHL-RN":{"current_price":119,"margin_floor_price":89,"match_type":"comparable"},"ZO-DRY-20":{"current_price":29,"margin_floor_price":21,"match_type":"comparable"},"ZO-HDL-400":{"current_price":39,"margin_floor_price":28,"match_type":"comparable"},"ZO-POL-AL":{"current_price":69,"margin_floor_price":50,"match_type":"comparable"},"ZO-BR-HAL-V500":{"current_price":64.95,"margin_floor_price":49,"match_type":"exact"},"ZO-BR-IK-MB2":{"current_price":89.95,"margin_floor_price":68,"match_type":"exact"},"ZO-BR-SW-K3L":{"current_price":39.95,"margin_floor_price":29,"match_type":"exact"},"ZO-BR-AG-SP60":{"current_price":279,"margin_floor_price":209,"match_type":"exact"}}
failures = []
for path in required:
    if not path.exists() or path.stat().st_size == 0:
        failures.append(f"V10 missing or empty: {path.name}")
seen = set()
for item in rec['recommendations']:
    sku = item['sku']
    if sku in seen:
        failures.append(f"V6 duplicate SKU {sku}")
    seen.add(sku)
    if sku not in CATALOG:
        failures.append(f"V6 unknown SKU {sku}")
        continue
    if item['recommended_price'] < CATALOG[sku]['margin_floor_price']:
        failures.append(f"V1 below floor {sku}")
    if item['status'] == 'reprice' and item['recommended_price'] == item['current_price']:
        failures.append(f"V4 no change for reprice {sku}")
    if item['status'] == 'hold' and item['recommended_price'] != item['current_price']:
        failures.append(f"V4 changed hold {sku}")
    change = abs(item['recommended_price'] - item['current_price']) / item['current_price'] * 100
    if change > 15.0001:
        failures.append(f"V5 cap exceeded {sku}")
    if not item.get('rationale'):
        failures.append(f"V7 missing rationale {sku}")
for match in rec['matches_used']:
    if not match.get('source_url'):
        failures.append(f"V3 missing source {match['listing_id']}")
    if not match.get('reasoning'):
        failures.append(f"V7 missing reasoning {match['listing_id']}")
    if not match['eligible'] and not match.get('exclusion_reason'):
        failures.append(f"V7 missing exclusion reason {match['listing_id']}")
for nm in rec['near_misses']:
    if nm['their_model_number'] == nm['our_model_number']:
        failures.append(f"V2 invalid near miss {nm['sku']}")
text_blobs = [(ROOT / 'recommendations.json').read_text(), (ROOT / 'approval-card.json').read_text(), summary]
for secret in ('unit_cost', 'margin_floor_price'):
    if any(secret in blob for blob in text_blobs):
        failures.append(f"V8 leaked {secret}")
count_line = next((line for line in summary.splitlines() if 'to reprice' in line), '')
if '11 to reprice' not in count_line or '1 holding' not in count_line or '1 needs your call' not in count_line:
    failures.append('V9 summary counts mismatch')
print('PASS' if not failures else 'FAIL')
for f in failures:
    print(f)
