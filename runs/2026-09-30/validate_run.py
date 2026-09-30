import json
import re
from pathlib import Path


ROOT = Path("/workspace")
payload = json.loads((ROOT / "recommendations.json").read_text())
summary = (ROOT / "approval-summary.md").read_text()

floors = {
    "ZO-TNT-2P": 165,
    "ZO-BAG-20": 110,
    "ZO-STV-01": 44,
    "ZO-PCK-45": 95,
    "ZO-SHL-RN": 89,
    "ZO-DRY-20": 21,
    "ZO-HDL-400": 28,
    "ZO-POL-AL": 50,
    "ZO-BR-HAL-V500": 49,
    "ZO-BR-IK-MB2": 68,
    "ZO-BR-SW-K3L": 29,
    "ZO-BR-AG-SP60": 209,
}
current = {
    "ZO-TNT-2P": 219,
    "ZO-BAG-20": 149,
    "ZO-STV-01": 59,
    "ZO-PCK-45": 129,
    "ZO-SHL-RN": 119,
    "ZO-DRY-20": 29,
    "ZO-HDL-400": 39,
    "ZO-POL-AL": 69,
    "ZO-BR-HAL-V500": 64.95,
    "ZO-BR-IK-MB2": 89.95,
    "ZO-BR-SW-K3L": 39.95,
    "ZO-BR-AG-SP60": 279,
}
errors = []
seen = set()

for item in payload["recommendations"]:
    sku = item["sku"]
    if sku not in floors:
        errors.append(f"V6 unknown sku {sku}")
    if sku in seen:
        errors.append(f"V6 duplicate sku {sku}")
    seen.add(sku)
    if item["status"] == "reprice" and item["recommended_price"] == item["current_price"]:
        errors.append(f"V4 {sku} reprice equals current")
    if item["status"] == "hold" and item["recommended_price"] != item["current_price"]:
        errors.append(f"V4 {sku} hold differs from current")
    if item["status"] == "review" and item["recommended_price"] is not None:
        errors.append(f"V4 {sku} review has price")
    if item["recommended_price"] is not None and item["recommended_price"] < floors[sku]:
        errors.append(f"V1 {sku} below floor")
    if item["recommended_price"] is not None and abs((item["recommended_price"] - current[sku]) / current[sku] * 100) > 15.0001:
        errors.append(f"V5 {sku} exceeds cap")
    if not item.get("rationale"):
        errors.append(f"V7 {sku} missing rationale")

for match in payload["matches_used"]:
    if not match.get("reasoning"):
        errors.append(f"V7 {match['sku']} missing reasoning")
    if not match.get("listing_id"):
        errors.append(f"V3 {match['sku']} missing trace")

for near in payload["near_misses"]:
    if near["their_model_number"] == near["our_model_number"]:
        errors.append(f"V2 {near['sku']} near miss identical")

secret_pattern = re.compile(r"unit_cost|margin_floor_price")
for filename in ("recommendations.json", "approval-card.json", "approval-summary.md"):
    if secret_pattern.search((ROOT / filename).read_text()):
        errors.append(f"V8 secret field in {filename}")

for filename in ("recommendations.json", "approval-card.json", "approval-summary.md", "price-comparison.xlsx"):
    file_path = ROOT / filename
    if not file_path.exists() or file_path.stat().st_size == 0:
        errors.append(f"V10 missing {filename}")

count_line = f"{len([r for r in payload['recommendations'] if r['status'] == 'reprice'])} to reprice · {len([r for r in payload['recommendations'] if r['status'] == 'hold'])} holding · {len([r for r in payload['recommendations'] if r['status'] == 'review'])} needs your call"
if count_line not in summary:
    errors.append("V9 summary counts mismatch")

print(json.dumps({"errors": errors, "warnings": payload["warnings"]}, indent=2))
raise SystemExit(1 if errors else 0)
