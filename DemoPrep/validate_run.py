import json
from pathlib import Path

root = Path(__file__).resolve().parent
rec = json.loads((root / "recommendations.json").read_text())
summary = (root / "approval-summary.md").read_text()
card = (root / "approval-card.json").read_text()
required = [root / "recommendations.json", root / "approval-card.json", root / "approval-summary.md", root / "price-comparison.xlsx"]
errors = []
for path in required:
    if not path.exists() or path.stat().st_size == 0:
        errors.append(f"V10 {path.name} missing or empty")
recs = rec.get("recommendations", [])
seen = set()
reprices = holds = reviews = 0
for item in recs:
    sku = item["sku"]
    if sku in seen:
        errors.append(f"V6 duplicate sku {sku}")
    seen.add(sku)
    if item["status"] == "reprice":
        reprices += 1
        if item["recommended_price"] == item["current_price"]:
            errors.append(f"V4 reprice without price change {sku}")
    elif item["status"] == "hold":
        holds += 1
        if item["recommended_price"] != item["current_price"]:
            errors.append(f"V4 hold with changed price {sku}")
    elif item["status"] == "review":
        reviews += 1
        if item["recommended_price"] is not None:
            errors.append(f"V4 review has price {sku}")
    if not item.get("rationale"):
        errors.append(f"V7 missing rationale {sku}")
for match in rec.get("matches_used", []):
    if not match.get("reasoning"):
        errors.append(f"V7 missing reasoning {match['sku']}")
    if not match.get("source_url"):
        errors.append(f"V3 missing source URL {match['sku']}")
for miss in rec.get("near_misses", []):
    if miss["their_model_number"] == miss["our_model_number"]:
        errors.append(f"V2 invalid near miss {miss['sku']}")
for blob in [json.dumps(rec), summary, card]:
    if "unit_cost" in blob or "margin_floor_price" in blob:
        errors.append("V8 confidential fields leaked")
count_line = f"{reprices} to reprice · {holds} holding · {reviews} needs your call"
if count_line not in summary:
    errors.append("V9 summary counts do not match")
if errors:
    print("FAIL")
    print("\n".join(errors))
    raise SystemExit(1)
print("PASS")
