import json
from pathlib import Path

ROOT = Path("/workspace")
catalogue = {"products":[{"sku":"ZO-TNT-2P","current_price":219,"margin_floor_price":165,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-BAG-20","current_price":149,"margin_floor_price":110,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-STV-01","current_price":59,"margin_floor_price":44,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-PCK-45","current_price":129,"margin_floor_price":95,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-SHL-RN","current_price":119,"margin_floor_price":89,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-DRY-20","current_price":29,"margin_floor_price":21,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-HDL-400","current_price":39,"margin_floor_price":28,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-POL-AL","current_price":69,"margin_floor_price":50,"brand":"Zava Outdoors","model_number":None},{"sku":"ZO-BR-HAL-V500","current_price":64.95,"margin_floor_price":49,"brand":"Halcyon Optics","model_number":"HAL-V500"},{"sku":"ZO-BR-IK-MB2","current_price":89.95,"margin_floor_price":68,"brand":"Ironkettle","model_number":"IK-MB2"},{"sku":"ZO-BR-SW-K3L","current_price":39.95,"margin_floor_price":29,"brand":"Stonewick","model_number":"SW-K3L"},{"sku":"ZO-BR-AG-SP60","current_price":279,"margin_floor_price":209,"brand":"Alpenglow","model_number":"AG-SP60-26"}]}
rules = {"max_change_per_run_pct": 15}

files = [ROOT / "recommendations.json", ROOT / "approval-summary.md", ROOT / "approval-card.json", ROOT / "price-comparison.xlsx"]
errors = []
warnings = []
for f in files:
    if not f.exists() or f.stat().st_size == 0:
        errors.append(f"V10 {f.name} missing or empty")

data = json.loads((ROOT / "recommendations.json").read_text())
summary = (ROOT / "approval-summary.md").read_text()
card = (ROOT / "approval-card.json").read_text()
sku_map = {p['sku']: p for p in catalogue['products']}

seen = set()
for rec in data['recommendations']:
    sku = rec['sku']
    if sku not in sku_map:
        errors.append(f"V6 unknown sku {sku}")
    if sku in seen:
        errors.append(f"V6 duplicate sku {sku}")
    seen.add(sku)
    floor = sku_map[sku]['margin_floor_price']
    current = sku_map[sku]['current_price']
    rp = rec['recommended_price']
    if rec['status'] == 'reprice':
        if rp is None or rp == rec['current_price']:
            errors.append(f"V4 reprice status mismatch {sku}")
    if rec['status'] == 'hold':
        if rp != rec['current_price']:
            errors.append(f"V4 hold status mismatch {sku}")
    if rec['status'] == 'review' and rp is not None:
        errors.append(f"V4 review status mismatch {sku}")
    if rp is not None and rp < floor:
        errors.append(f"V1 below floor {sku}")
    if rp is not None:
        move = abs((rp - current) / current) * 100
        if move > rules['max_change_per_run_pct'] + 1e-6:
            errors.append(f"V5 cap exceeded {sku}")
    if rec['match_type'] == 'comparable' and not rec['rationale']:
        errors.append(f"V7 missing comparable rationale {sku}")
    if not rec['rationale']:
        errors.append(f"V7 missing recommendation rationale {sku}")

for match in data['matches_used']:
    if not match['reasoning']:
        errors.append(f"V7 missing match reasoning {match['sku']} {match['listing_id']}")
for miss in data['near_misses']:
    if miss['their_model_number'] == miss['our_model_number']:
        errors.append(f"V2 near miss identical {miss['sku']}")
for match in data['matches_used']:
    if not match['listing_id'] or not match['competitor']:
        errors.append(f"V3 source trace missing {match['sku']}")
for ex in [m for m in data['matches_used'] if not m['eligible']]:
    if not ex['exclusion_reason']:
        errors.append(f"V7 exclusion reason missing {ex['sku']} {ex['listing_id']}")

for forbidden in ['unit_cost', 'margin_floor_price', ' 165', ' 209', ' 118', ' 182']:
    if forbidden in json.dumps(data) or forbidden in summary or forbidden in card:
        errors.append(f"V8 confidential figure leaked: {forbidden}")

reprices = sum(1 for r in data['recommendations'] if r['status'] == 'reprice')
holds = sum(1 for r in data['recommendations'] if r['status'] == 'hold')
reviews = sum(1 for r in data['recommendations'] if r['status'] == 'review')
count_line = f"{reprices} to reprice · {holds} holding · {reviews} needs your call"
if count_line not in summary:
    errors.append('V9 summary counts do not match recommendations')

eligible_by_sku = {}
for m in data['matches_used']:
    if m['eligible']:
        eligible_by_sku.setdefault(m['sku'], 0)
        eligible_by_sku[m['sku']] += 1
for rec in data['recommendations']:
    if rec['sku'] not in eligible_by_sku:
        warnings.append(f"W1 no eligible match {rec['sku']}")
    if rec['status'] == 'reprice' and eligible_by_sku.get(rec['sku']) == 1:
        warnings.append(f"W2 one competitor listing {rec['sku']}")
    if rec['status'] == 'reprice' and abs((rec['recommended_price'] - rec['current_price']) / rec['current_price']) * 100 > 10:
        warnings.append(f"W4 more than 10 percent move {rec['sku']}")
for warning in data['warnings']:
    if warning['code'] == 'W5':
        warnings.append(f"W5 clearance influence {warning['sku']}")
if data['missing_snapshots']:
    warnings.append('W6 missing or stale competitor snapshot')

print('VALIDATION PASSED' if not errors else 'VALIDATION FAILED')
for e in errors:
    print(f"ERROR: {e}")
for w in warnings:
    print(f"WARNING: {w}")
raise SystemExit(1 if errors else 0)
