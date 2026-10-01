import json
from pathlib import Path


root = Path(__file__).parent
for name in ['recommendations.json', 'approval-card.json', 'approval-summary.md', 'price-comparison.xlsx']:
    path = root / name
    assert path.exists() and path.stat().st_size > 0, f'V10 failed: {name}'

rec = json.loads((root / 'recommendations.json').read_text())
summary = (root / 'approval-summary.md').read_text()
combined = (root / 'recommendations.json').read_text() + (root / 'approval-card.json').read_text() + summary
assert 'unit_cost' not in combined and 'margin_floor_price' not in combined, 'V8 failed'

seen = set()
for item in rec['recommendations']:
    assert item['sku'] not in seen, f"V6 failed: duplicate {item['sku']}"
    seen.add(item['sku'])
    if item['status'] == 'reprice':
        assert item['recommended_price'] is not None and item['recommended_price'] != item['current_price'], f"V4 failed: {item['sku']}"
    elif item['status'] == 'hold':
        assert item['recommended_price'] == item['current_price'], f"V4 failed: {item['sku']}"
    elif item['status'] == 'review':
        assert item['recommended_price'] is None, f"V4 failed: {item['sku']}"
    assert item['rationale'], f"V7 failed rationale: {item['sku']}"
    if item['recommended_price'] is not None:
        change = abs(item['recommended_price'] - item['current_price']) / item['current_price'] * 100
        assert change <= 15 + 1e-9, f"V5 failed: {item['sku']}"

for match in rec['matches_used']:
    assert match['reasoning'], f"V7 failed reasoning: {match['listing_id']}"
    assert match['listing_id'], 'V3 failed'

for miss in rec['near_misses']:
    assert miss['their_model_number'] != miss['our_model_number'], f"V2 failed: {miss['sku']}"

count_line = summary.splitlines()[1]
assert str(len([x for x in rec['recommendations'] if x['status'] == 'reprice'])) in count_line, 'V9 failed reprice count'
assert str(len([x for x in rec['recommendations'] if x['status'] == 'hold']))  in count_line, 'V9 failed hold count'
assert str(len([x for x in rec['recommendations'] if x['status'] == 'review'])) in count_line, 'V9 failed review count'

print('Validation passed')
