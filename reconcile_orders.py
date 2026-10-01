import csv
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from jsonschema import Draft7Validator


ROOT = Path('/workspace')
SCHEMA_PATH = ROOT / 'data-reconciliation-agent' / 'target_schema.json'
TWO_PLACES = Decimal('0.01')
FOUR_PLACES = Decimal('0.0001')


def round_2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def round_4(value: Decimal) -> Decimal:
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def parse_date(value: str) -> str:
    value = value.strip()
    for fmt in ('%Y-%m-%d', '%d-%b-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    if '/' in value:
        first, second, year = value.split('/')
        first_i = int(first)
        if first_i > 12:
            return datetime.strptime(value, '%d/%m/%Y').date().isoformat()
        return datetime.strptime(value, '%m/%d/%Y').date().isoformat()
    raise ValueError(f'Unsupported date format: {value}')


def parse_money(value: str | None) -> Decimal | None:
    value = (value or '').strip()
    if not value:
        return None
    negative = value.startswith('-')
    cleaned = (
        value.replace('EUR', '')
        .replace('USD', '')
        .replace('JPY', '')
        .replace('$', '')
        .replace('¥', '')
        .replace(',', '')
        .strip()
    )
    amount = Decimal(cleaned)
    return -amount if negative and amount > 0 else amount


def fetch_rate(base: str, order_date: str, cache: dict, audit_rows: dict, fetch_timestamp: str) -> tuple[Decimal, str]:
    if base == 'USD':
        return Decimal('1'), order_date
    key = (base, order_date)
    if key in cache:
        return cache[key]
    url = f'https://api.frankfurter.dev/v2/providers/ecb/rate/{base.lower()}/usd?date={order_date}'
    request = urllib.request.Request(url, headers={'User-Agent': 'order-reconciliation/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    rate = Decimal(str(payload['rate']))
    effective_date = payload['date']
    cache[key] = (rate, effective_date)
    audit_rows[(base, effective_date)] = {
        'requested_order_date': order_date,
        'effective_rate_date': effective_date,
        'provider': 'Frankfurter ECB',
        'base_currency': base,
        'quote_currency': 'USD',
        'rate': str(rate),
        'fetch_timestamp': fetch_timestamp,
    }
    return cache[key]


def format_money(amount: float) -> str:
    return f'${amount:,.2f} USD'


def reconcile(input_csv: Path) -> dict:
    fetch_timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    outputs = {
        'json': ROOT / 'reconciled_orders.json',
        'rejects': ROOT / 'rejects.csv',
        'customers': ROOT / 'new_customers_seen.txt',
        'summary': ROOT / 'reconciliation_summary.md',
        'html': ROOT / 'reconciliation_summary.html',
        'fx': ROOT / 'fx_rates_used.csv',
    }

    with input_csv.open(newline='', encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))

    customers = sorted({row['Cust Name'] for row in rows})
    rate_cache = {}
    fx_audit = {}
    rejects = []
    valid_candidates = []
    rejection_counts = defaultdict(int)
    logs = []

    for idx, row in enumerate(rows, start=2):
        logs.append(f'Loaded row {idx} for order {row["OrderID"]}.')
        qty_raw = (row['Qty'] or '').strip()
        if qty_raw == '':
            reason = 'missing quantity'
            rejects.append({**row, 'rejection_reason': reason})
            rejection_counts[reason] += 1
            logs.append(f'Rejected row {idx}: {reason}.')
            continue

        qty = int(qty_raw)
        if qty <= 0:
            reason = 'negative quantity requires manual review' if qty < 0 else 'non-positive quantity'
            rejects.append({**row, 'rejection_reason': reason})
            rejection_counts[reason] += 1
            logs.append(f'Rejected row {idx}: {reason}.')
            continue

        order_date = parse_date(row['order_date'])
        currency = (row['Currency'] or '').strip() or 'USD'
        unit_native = parse_money(row['Unit Price'])
        source_line_native = parse_money(row['Line Total'])
        rate, effective_date = fetch_rate(currency, order_date, rate_cache, fx_audit, fetch_timestamp)
        line_total_usd = round_2(Decimal(qty) * unit_native * rate)
        unit_price_usd = round_4(unit_native * rate)
        source_line_usd = None if source_line_native is None else round_2(source_line_native * rate)
        mismatch = source_line_usd is not None and abs(source_line_usd - line_total_usd) > Decimal('0.01')
        valid_candidates.append(
            {
                'row': row,
                'qty': qty,
                'order_date': order_date,
                'unit_price_usd': unit_price_usd,
                'line_total_usd': line_total_usd,
                'source_line_total_mismatch': mismatch,
            }
        )
        logs.append(f'Prepared valid candidate row {idx} with FX {currency}->{rate} effective {effective_date}.')

    by_order_item = defaultdict(list)
    for candidate in valid_candidates:
        by_order_item[(candidate['row']['OrderID'], candidate['row']['item'])].append(candidate)

    accepted = []
    for candidates in by_order_item.values():
        if len(candidates) == 1:
            accepted.append(candidates[0])
            continue
        first = candidates[0]
        identical = all(
            candidate['qty'] == first['qty']
            and parse_money(candidate['row']['Unit Price']) == parse_money(first['row']['Unit Price'])
            and parse_money(candidate['row']['Line Total']) == parse_money(first['row']['Line Total'])
            for candidate in candidates[1:]
        )
        if identical:
            accepted.append(first)
            for duplicate in candidates[1:]:
                reason = 'duplicate line item'
                rejects.append({**duplicate['row'], 'rejection_reason': reason})
                rejection_counts[reason] += 1
                logs.append(
                    f'Rejected duplicate row for order {duplicate["row"]["OrderID"]} item {duplicate["row"]["Item"]}.'
                )
        else:
            for duplicate in candidates:
                reason = 'conflicting duplicate line item - cannot determine correct values'
                rejects.append({**duplicate['row'], 'rejection_reason': reason})
                rejection_counts[reason] += 1
                logs.append(
                    f'Rejected conflicting duplicate row for order {duplicate["row"]["OrderID"]} item {duplicate["row"]["Item"]}.'
                )

    grouped_orders = defaultdict(list)
    for candidate in accepted:
        grouped_orders[candidate['row']['OrderID']].append(candidate)

    reconciled = []
    discount_mismatches = []
    line_mismatches = []
    stacked_discounts = []

    for order_id in sorted(grouped_orders):
        items = grouped_orders[order_id]
        sample = items[0]['row']
        total_qty = sum(item['qty'] for item in items)
        discount_pct = Decimal('5') if sample['Customer Tier'] == 'VIP' else Decimal('0')
        if total_qty >= 15:
            discount_pct += Decimal('10')
        source_discount = Decimal(sample['Discount %'])
        source_discount_mismatch = source_discount != discount_pct
        subtotal = sum(item['line_total_usd'] for item in items)
        order_total = round_2(subtotal * (Decimal('1') - (discount_pct / Decimal('100')))
        if discount_pct == Decimal('15'):
            stacked_discounts.append(order_id)
        if source_discount_mismatch:
            discount_mismatches.append((order_id, float(discount_pct), float(source_discount)))

        payload_items = []
        for item in items:
            if item['source_line_total_mismatch']:
                line_mismatches.append((order_id, item['row']['Item'], float(item['line_total_usd']), item['row']['Line Total']))
            payload_items.append(
                {
                    'item_sku': item['row']['Item'],
                    'quantity': item['qty'],
                    'unit_price_usd': float(item['unit_price_usd']),
                    'line_total_usd': float(item['line_total_usd']),
                    'source_line_total_mismatch': item['source_line_total_mismatch'],
                }
            )

        reconciled.append(
            {
                'order_id': order_id,
                'customer_name': sample['Cust Name'],
                'order_date': items[0]['order_date'],
                'region': sample['Region'],
                'status': sample['Status'],
                'discount_applied': discount_pct > 0,
                'discount_pct_applied': float(discount_pct),
                'source_discount_pct_mismatch': source_discount_mismatch,
                'items': payload_items,
                'order_total_usd': float(order_total),
            }
        )
        logs.append(f'Built reconciled order {order_id} with {len(payload_items)} item(s).')

    with SCHEMA_PATH.open(encoding='utf-8') as handle:
        schema = json.load(handle)
    validator = Draft7Validator({'type': 'array', 'items': schema})
    errors = sorted(validator.iter_errors(reconciled), key=lambda error: list(error.path))
    if errors:
        raise ValueError('\n'.join(error.message for error in errors))
    logs.append('Schema validation passed for reconciled_orders.json.')

    outputs['json'].write_text(json.dumps(reconciled, indent=2) + '\n', encoding='utf-8')

    reject_fields = list(rows[0].keys()) + ['rejection_reason']
    with outputs['rejects'].open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=reject_fields)
        writer.writeheader()
        writer.writerows(rejects)

    outputs['customers'].write_text('\n'.join(customers) + '\n', encoding='utf-8')

    with outputs['fx'].open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                'requested_order_date',
                'effective_rate_date',
                'provider',
                'base_currency',
                'quote_currency',
                'rate',
                'fetch_timestamp',
            ],
        )
        writer.writeheader()
        for key in sorted(fx_audit):
            writer.writerow(fx_audit[key])

    summary_lines = [
        f'**Reconciliation complete** — {len(reconciled)} orders onboarded, {len(rejects)} rows need review.',
        '',
        '**Action required**',
    ]
    if rejection_counts:
        for reason, count in sorted(rejection_counts.items()):
            summary_lines.append(f'- **Rejected rows** — {count} row(s) for {reason}.')
    else:
        summary_lines.append('- **Rejected rows** — 0 rows.')
    for order_id, item, recomputed, source in line_mismatches:
        summary_lines.append(
            f'- **{order_id} / {item}** — line total recomputed to {format_money(recomputed)}; source recorded {source}.'
        )
    for order_id, recomputed, source in discount_mismatches:
        summary_lines.append(f'- **{order_id}** — discount recomputed to {recomputed:.0f}%; source recorded {source:.0f}%.')
    if stacked_discounts:
        summary_lines.append(
            f'- **Stacked discounts** — {", ".join(stacked_discounts)} applied VIP + bulk for 15% total discount.'
        )
    summary_lines.extend(
        [
            '',
            '**Counts**',
            f'- **Orders processed** — {len({row["OrderID"] for row in rows})} order IDs across {len(rows)} source rows.',
            f'- **Orders onboarded** — {len(reconciled)} orders.',
            f'- **Orders fully rejected** — {len({entry["OrderID"] for entry in rejects} - {order["order_id"] for order in reconciled})} orders.',
            '',
            '**New customers seen**',
            f'- **Customers** — {", ".join(customers)}',
        ]
    )
    outputs['summary'].write_text('\n'.join(summary_lines[:25]) + '\n', encoding='utf-8')

    html = [
        '<!DOCTYPE html>',
        '<html>',
        '<head><meta charset="utf-8"><title>Reconciliation Summary</title>',
        '<style>body{font-family:Arial,sans-serif;color:#242424}.container{max-width:900px;margin:20px auto;padding:20px;border:1px solid #ddd;border-radius:8px}h1{font-size:22px}strong{color:#0f6cbd}</style></head>',
        '<body><div class="container">',
        '<h1>Order Reconciliation Summary</h1>',
        f'<p><strong>Outcome:</strong> {len(reconciled)} orders onboarded, {len(rejects)} rows rejected for review.</p>',
        '<p><strong>Processing log</strong></p><ul>',
    ]
    html.extend(f'<li>{entry}</li>' for entry in logs)
    html.append('</ul><p><strong>Rejection reasons</strong></p><ul>')
    html.extend(f'<li><strong>{reason}</strong>: {count} row(s)</li>' for reason, count in sorted(rejection_counts.items()))
    html.append('</ul><p><strong>Discount mismatches</strong></p><ul>')
    if discount_mismatches:
        html.extend(
            f'<li><strong>{order_id}</strong>: recomputed {recomputed:.0f}% vs source {source:.0f}%</li>'
            for order_id, recomputed, source in discount_mismatches
        )
    else:
        html.append('<li>None</li>')
    html.append('</ul><p><strong>Line total mismatches</strong></p><ul>')
    if line_mismatches:
        html.extend(
            f'<li><strong>{order_id | /item}</strong>: recomputed {format_money(recomputed)} vs source {source}</li>'
            for order_id, item, recomputed, source in line_mismatches
        )
    else:
        html.append('<li>None</li>')
    html.append('</ul>')
    html.append(f'<p><strong>Stacked discounts:</strong> {", ".join(stacked_discounts) if stacked_discounts else "None"}</p>')
    html.append(f'<p><strong>New customers:</strong> {", ".join(customers)}</p>')
    html.append('</div></body></html>')
    outputs['html'].write_text(''.join(html), encoding='utf-8')

    return {
        'orders_onboarded': len(reconciled),
        'rows_rejected': len(rejects),
        'discount_mismatches': discount_mismatches,
        'line_mismatches': line_mismatches,
        'stacked_discounts': stacked_discounts,
        'customers': customers,
        'logs': logs,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: python3 reconcile_orders.py <input_csv>')
        return 2
    result = reconcile(Path(sys.argv[1]))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
