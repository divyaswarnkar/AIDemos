#!/usr/bin/env python3

import base64
import csv
import io
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jsonschema import Draft7Validator


RAW_INPUT = """OrderID,Cust Name,Customer Tier,order_date,Region,Item,Qty,Unit Price,Currency,Line Total,Discount %,Status,Notes
ORD-2001,Acme Corp,Standard,01/05/2024,US,Camping Tent,10,$25.00,USD,$250.00,0,Complete,
ORD-2002,Globex Inc,VIP,2024-01-06,EMEA,Sleeping Bag,5,EUR 40.00,EUR,EUR 190.00,5,Complete,
ORD-2002,Globex Inc,VIP,2024-01-06,EMEA,Camping Tent,2,EUR 22.00,EUR,EUR 44.00,5,Complete,
ORD-2003,Acme Corp,Standard,01/07/2024,US,Camping Tent,,$25.00,USD,,0,Pending,Customer called to confirm order - qty to follow
ORD-2004,Initech,Standard,07-Jan-2024,APAC,Hiking Backpack,20,¥1500,JPY,¥30000,10,Complete,
ORD-2005,Globex Inc,VIP,2024-01-08,EMEA,Sleeping Bag,3,EUR 40.00,EUR,EUR 120.00,5,Cancelled,
ORD-2006,Umbrella LLC,VIP,01/09/2024,US,Camping Tent,8,$25.00,USD,$200.00,5,Complete,
ORD-2007,Initech,Standard,09-Jan-2024,APAC,Trekking Poles,5,¥800,JPY,¥4000,0,Complete,
ORD-2008,Acme Corp,Standard,2024/01/10,US,Camping Tent,12,$25.00,,$300.00,0,Complete,
ORD-2009,Umbrella LLC,VIP,01/11/2024,US,Sleeping Bag,7,$42.50,USD,$297.50,10,Complete,
ORD-2009,Umbrella LLC,VIP,01/11/2024,US,Hiking Backpack,9,$60.00,USD,$540.00,10,Complete,
ORD-2010,Globex Inc,VIP,11-Jan-2024,EMEA,Camping Tent,4,EUR 22.00,EUR,EUR 88.00,5,Complete,
ORD-2010,Globex Inc,VIP,11-Jan-2024,EMEA,Camping Tent,4,EUR 22.00,EUR,EUR 88.00,5,Complete,Possible duplicate entry - check with warehouse
ORD-2011,Initech,Standard,2024-01-12,APAC,Hiking Backpack,15,¥1500,JPY,¥22500,10,Refunded,
ORD-2012,Acme Corp,Standard,13/01/2024,US,Sleeping Bag,-3,$42.50,USD,-$127.50,0,Complete,Customer requested return of 3 units
ORD-2013,Umbrella LLC,VIP,01/14/2024,US,Hiking Backpack,9,$60.00,USD,$540.00,5,Complete,
ORD-2013,Umbrella LLC,VIP,01/14/2024,US,Camping Tent,3,$25.00,USD,$75.00,5,Complete,
ORD-2014,Beta Testers,Standard,01/15/2024,US,Camping Tent,6,$25.00,USD,$150.00,0,Complete,New customer - add to CRM
"""

ROOT = Path("/workspace")
SCHEMA_PATH = ROOT / "data-reconciliation-agent" / "target_schema.json"
OUTPUT_JSON = ROOT / "reconciled_orders.json"
OUTPUT_REJECTS = ROOT / "rejects.csv"
OUTPUT_CUSTOMERS = ROOT / "new_customers_seen.txt"
OUTPUT_SUMMARY_MD = ROOT / "reconciliation_summary.md"
OUTPUT_SUMMARY_HTML = ROOT / "reconciliation_summary.html"
OUTPUT_FX = ROOT / "fx_rates_used.csv"
OUTPUT_LOG = ROOT / "run_log.txt"

TWOPLACES = Decimal("0.01")
FOURPLACES = Decimal("0.0001")


@dataclass
class FxRateRecord:
    requested_date: str
    effective_date: str
    provider: str
    base_currency: str
    rate_to_usd: Decimal
    fetch_timestamp: str


class RunLogger:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def log(self, message: str) -> None:
        stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.lines.append(f"[{stamp}] {message}")

    def write(self) -> None:
        OUTPUT_LOG.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


LOGGER = RunLogger()


def quantize(value: Decimal, places: Decimal) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    if "/" in raw:
        a, b, c = raw.split("/")
        if len(a) == 4:
            return datetime.strptime(raw, "%Y/%m/%d").strftime("%Y-%m-%d")
        first = int(a)
        second = int(b)
        fmt = "%d/%m/%Y" if first > 12 else "%m/%d/%Y"
        return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
    raise ValueError(f"Unsupported date format: {raw}")


def parse_decimal(raw: str) -> Decimal:
    cleaned = raw.strip().replace(",", "")
    for token in ("USD", "EUR", "JPY", "$", "¥"):
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("Missing numeric value")
    return Decimal(cleaned)


def load_rows() -> List[Dict[str, str]]:
    LOGGER.log("Loaded provided inline CSV batch from task input.")
    return list(csv.DictReader(io.StringIO(RAW_INPUT)))


def fetch_fx_rate(currency: str, order_date: str, cache: Dict[Tuple[str, str], FxRateRecord]) -> FxRateRecord:
    key = (currency, order_date)
    if key in cache:
        LOGGER.log(f"Reused cached FX rate for {currency} on requested date {order_date}.")
        return cache[key]
    url = f"https://api.frankfurter.dev/v2/providers/ecb/rate/{currency.lower()}/usd?date={order_date}"
    LOGGER.log(f"Fetching FX rate from Frankfurter ECB provider for {currency} on {order_date}.")
    request = Request(url, headers={"User-Agent": "order-reconciliation/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"FX fetch failed for {currency} {order_date}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"FX fetch failed for {currency} {order_date}: {exc.reason}") from exc
    record = FxRateRecord(
        requested_date=order_date,
        effective_date=payload["date"],
        provider="Frankfurter ECB",
        base_currency=currency,
        rate_to_usd=Decimal(str(payload["rate"])),
        fetch_timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    cache[key] = record
    LOGGER.log(
        f"Fetched FX rate for {currency}: requested {order_date}, effective {record.effective_date}, rate {record.rate_to_usd}."
    )
    return record


def discount_for_order(customer_tier: str, total_qty: int) -> Decimal:
    vip_discount = Decimal("5") if customer_tier == "VIP" else Decimal("0")
    bulk_discount = Decimal("10") if total_qty >= 15 else Decimal("0")
    return vip_discount + bulk_discount


def format_money(amount: Decimal, currency: str = "USD") -> str:
    return f"${amount:,.2f} {currency}"


def main() -> int:
    rows = load_rows()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft7Validator({"type": "array", "items": schema})
    LOGGER.log("Loaded target schema and prepared validator.")

    customers = sorted({row["Cust Name"].strip() for row in rows})
    OUTPUT_CUSTOMERS.write_text("\n".join(customers) + "\n", encoding="utf-8")
    LOGGER.log(f"Wrote {len(customers)} distinct customer names to new_customers_seen.txt.")

    indexed_rows = [{**row, "_rownum": index + 2} for index, row in enumerate(rows)]
    row_groups: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
    for row in indexed_rows:
        row_groups[(row["OrderID"], row["Item"])].append(row)

    rejected_rows: List[Dict[str, str]] = []
    duplicate_rows_to_skip = set()
    for key, group in row_groups.items():
        if len(group) < 2:
            continue
        signatures = {(g["Qty"], g["Unit Price"], g["Line Total"]) for g in group}
        if len(signatures) == 1:
            for duplicate in group[1:]:
                duplicate_rows_to_skip.add(duplicate["_rownum"])
                rejected_rows.append({**duplicate, "rejection_reason": "duplicate line item"})
                LOGGER.log(f"Rejected row {duplicate['_rownum']} ({key[0]}/{key[1]}) as duplicate line item.")
        else:
            for duplicate in group:
                duplicate_rows_to_skip.add(duplicate["_rownum"])
                rejected_rows.append(
                    {**duplicate, "rejection_reason": "conflicting duplicate line item - cannot determine correct values"}
                )
                LOGGER.log(f"Rejected row {duplicate['_rownum']} ({key[0]}/{key[1]}) as conflicting duplicate line item.")

    fx_cache: Dict[Tuple[str, str], FxRateRecord] = {}
    valid_rows_by_order: Dict[str, List[Dict[str, object]]] = defaultdict(list)

    for row in indexed_rows:
        if row["_rownum"] in duplicate_rows_to_skip:
            continue
        qty_raw = row["Qty"].strip()
        if not qty_raw:
            rejected_rows.append({**row, "rejection_reason": "missing quantity"})
            LOGGER.log(f"Rejected row {row['_rownum']} ({row['OrderID']}) due to missing quantity.")
            continue
        qty = int(qty_raw)
        if qty < 0:
            rejected_rows.append({**row, "rejection_reason": "negative quantity requires manual review"})
            LOGGER.log(f"Rejected row {row['_rownum']} ({row['OrderID']}) due to negative quantity.")
            continue
        if qty == 0:
            rejected_rows.append({**row, "rejection_reason": "non-positive quantity"})
            LOGGER.log(f"Rejected row {row['_rownum']} ({row['OrderID']}) due to zero quantity.")
            continue

        order_date = parse_date(row["order_date"])
        currency = (row["Currency"].strip() or "USD").upper()
        if not row["Currency"].strip():
            LOGGER.log(f"Assumed USD currency for row {row['_rownum']} ({row['OrderID']}) because source currency was blank.")
        fx = FxRateRecord(order_date, order_date, "No conversion - native USD", "USD", Decimal("1"), datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
        if currency != "USD":
            fx = fetch_fx_rate(currency, order_date, fx_cache)

        unit_price_native = parse_decimal(row["Unit Price"])
        source_total_native = parse_decimal(row["Line Total"])
        line_total_usd = quantize(unit_price_native * qty * fx.rate_to_usd, TWOPLACES)
        unit_price_usd = quantize(unit_price_native * fx.rate_to_usd, FOURPLACES)
        source_total_usd = quantize(source_total_native * fx.rate_to_usd, TWOPLACES)
        mismatch = abs(line_total_usd - source_total_usd) > Decimal("0.01")
        if mismatch:
            LOGGER.log(
                f"Flagged line total mismatch for row {row['_rownum']} ({row['OrderID']}/{row['Item']}): recomputed {line_total_usd} USD vs source {source_total_usd} USD."
            )

        valid_rows_by_order[row["OrderID"]].append(
            {
                "source": row,
                "order_date": order_date,
                "currency": currency,
                "fx": fx,
                "qty": qty,
                "unit_price_usd": float(unit_price_usd),
                "line_total_usd": float(line_total_usd),
                "source_line_total_mismatch": mismatch,
            }
        )

    reconciled_orders: List[Dict[str, object]] = []
    discount_mismatches: List[str] = []
    stacked_discount_orders: List[str] = []
    line_mismatches: List[str] = []

    for order_id in sorted(valid_rows_by_order):
        entries = valid_rows_by_order[order_id]
        first = entries[0]["source"]
        total_qty = sum(entry["qty"] for entry in entries)
        discount_pct = discount_for_order(first["Customer Tier"], total_qty)
        source_discount = Decimal(first["Discount %"])
        discount_mismatch = source_discount != discount_pct
        if discount_mismatch:
            discount_mismatches.append(
                f"**{order_id}** - recomputed discount {discount_pct}% vs source {source_discount}%"
            )
            LOGGER.log(f"Flagged discount mismatch for {order_id}: recomputed {discount_pct}% vs source {source_discount}%.")
        if discount_pct == Decimal("15"):
            stacked_discount_orders.append(
                f"**{order_id}** - VIP plus bulk discount stacked to 15% across {total_qty} units"
            )
            LOGGER.log(f"Order {order_id} received stacked VIP and bulk discount totaling 15%.")

        items = []
        subtotal = Decimal("0")
        for entry in entries:
            subtotal += Decimal(str(entry["line_total_usd"]))
            if entry["source_line_total_mismatch"]:
                src = entry["source"]
                line_mismatches.append(
                    f"**{order_id}/{src['Item']}** - recomputed {format_money(Decimal(str(entry['line_total_usd'])))} vs source converted total {format_money(quantize(parse_decimal(src['Line Total']) * entry['fx'].rate_to_usd, TWOPLACES))}"
                )
            items.append(
                {
                    "item_sku": entry["source"]["Item"],
                    "quantity": entry["qty"],
                    "unit_price_usd": entry["unit_price_usd"],
                    "line_total_usd": entry["line_total_usd"],
                    "source_line_total_mismatch": entry["source_line_total_mismatch"],
                }
            )
        order_total = quantize(subtotal * (Decimal("1") - (discount_pct / Decimal("100"))), TWOPLACES)
        reconciled_orders.append(
            {
                "order_id": order_id,
                "customer_name": first["Cust Name"],
                "order_date": entries[0]["order_date"],
                "region": first["Region"],
                "status": first["Status"],
                "discount_applied": discount_pct > 0,
                "discount_pct_applied": float(discount_pct),
                "source_discount_pct_mismatch": discount_mismatch,
                "items": items,
                "order_total_usd": float(order_total),
            }
        )
        LOGGER.log(f"Built reconciled order {order_id} with {len(items)} valid line item(s) and total {order_total} USD.")

    errors = sorted(validator.iter_errors(reconciled_orders), key=lambda err: list(err.path))
    if errors:
        raise RuntimeError("Schema validation failed: " + "; ".join(error.message for error in errors))
    LOGGER.log(f"Validated {len(reconciled_orders)} reconciled orders against target schema with zero errors.")

    OUTPUT_JSON.write_text(json.dumps(reconciled_orders, indent=2) + "\n", encoding="utf-8")
    LOGGER.log(f"Wrote reconciled_orders.json with {len(reconciled_orders)} onboarded orders.")

    reject_fieldnames = list(rows[0].keys()) + ["rejection_reason"]
    with OUTPUT_REJECTS.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=reject_fieldnames)
        writer.writeheader()
        for row in rejected_rows:
            writer.writerow({key: row.get(key, "") for key in reject_fieldnames})
    LOGGER.log(f"Wrote rejects.csv with {len(rejected_rows)} rejected source row(s).")

    fx_records = []
    seen_fx = set()
    for entry_list in valid_rows_by_order.values():
        for entry in entry_list:
            fx = entry["fx"]
            if fx.base_currency == "USD":
                continue
            dedupe_key = (fx.base_currency, fx.effective_date)
            if dedupe_key in seen_fx:
                continue
            seen_fx.add(dedupe_key)
            fx_records.append(fx)
    with OUTPUT_FX.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["requested_order_date", "effective_rate_date", "provider", "base_currency", "rate_to_usd", "fetch_timestamp"],
        )
        writer.writeheader()
        for fx in sorted(fx_records, key=lambda rec: (rec.base_currency, rec.effective_date)):
            writer.writerow(
                {
                    "requested_order_date": fx.requested_date,
                    "effective_rate_date": fx.effective_date,
                    "provider": fx.provider,
                    "base_currency": fx.base_currency,
                    "rate_to_usd": str(fx.rate_to_usd),
                    "fetch_timestamp": fx.fetch_timestamp,
                }
            )
    LOGGER.log(f"Wrote fx_rates_used.csv with {len(fx_records)} distinct FX lookup row(s).")

    rejection_counts: Dict[str, int] = defaultdict(int)
    for row in rejected_rows:
        rejection_counts[row["rejection_reason"]] += 1
    summary_lines = [
        f"**Reconciliation complete** - {len(reconciled_orders)} orders onboarded, {len(rejected_rows)} source rows need review.",
        "",
        f"**Run totals** - 14 source orders reviewed, {len(reconciled_orders)} onboarded orders, {len(rejected_rows)} rejected rows.",
        "**Action required**",
    ]
    for reason, count in sorted(rejection_counts.items()):
        summary_lines.append(f"- **Rejected rows** - {count} row(s) for {reason}.")
    if line_mismatches:
        summary_lines.extend(line_mismatches)
    else:
        summary_lines.append("- **Line total mismatches** - none.")
    if discount_mismatches:
        summary_lines.extend(f"- {item}" for item in discount_mismatches)
    else:
        summary_lines.append("- **Discount mismatches** - none.")
    if stacked_discount_orders:
        summary_lines.extend(f"- {item}" for item in stacked_discount_orders)
    else:
        summary_lines.append("- **Stacked discounts** - none.")
    summary_lines.append(f"**New customers seen** - {', '.join(customers)}.")
    summary_md = "\n".join(summary_lines[:25]) + "\n"
    OUTPUT_SUMMARY_MD.write_text(summary_md, encoding="utf-8")
    LOGGER.log("Wrote Teams-safe reconciliation_summary.md.")

    html_items = "".join(f"<li>{line}</li>" for line in summary_lines[2:])
    html = (
        "<html><body style=\"font-family:Segoe UI,Arial,sans-serif\">"
        f"<p><strong>Reconciliation complete</strong> - {len(reconciled_orders)} orders onboarded, {len(rejected_rows)} source rows need review.</p>"
        "<ul>"
        f"{html_items}"
        "</ul>"
        "</body></html>"
    )
    OUTPUT_SUMMARY_HTML.write_text(html, encoding="utf-8")
    LOGGER.log("Wrote reconciliation_summary.html for Microsoft Teams consumption.")

    LOGGER.write()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # narrow enough for CLI entrypoint reporting
        LOGGER.log(f"Run failed: {exc}")
        LOGGER.write()
        raise
