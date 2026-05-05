"""
Load Unolo GPS-distance reports for cross-checking petrol / two-wheeler claims.

Each month the admin drops a 'Summary Details Report Unolo <Month>.xlsx' into
assets/unolo_data/ as a CSV (use convert_unolo.py or the bootstrap routine in
this module). The loader exposes:

    load_unolo()             -> dict[employee_code -> [{date, km, ...}]]
    km_for(code, d_from, d_to) -> total km the employee logged in that window
    days_active(code, d_from, d_to) -> number of days with km > 0

Naming convention: assets/unolo_data/<lower>_<year>.csv  (e.g. april_2026.csv)
Internal IDs in Unolo come prefixed with 'T-' (e.g. T-RWSIPL469); we strip the
prefix so codes match the employee master CSV.
"""
import os, csv, glob, datetime
from collections import defaultdict

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNOLO_DIR  = os.path.join(SKILL_ROOT, 'assets', 'unolo_data')


def _to_float(x):
    try:
        return float(str(x).strip().replace(',', ''))
    except (ValueError, AttributeError, TypeError):
        return 0.0


def load_unolo(directory=UNOLO_DIR):
    """Return dict {employee_code: [{'date': iso, 'km': float, 'odo': float,
    'designation': str, 'team': str, 'employee_label': str}, ...]}."""
    by_code = defaultdict(list)
    if not os.path.isdir(directory):
        return by_code
    for path in sorted(glob.glob(os.path.join(directory, '*.csv'))):
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                code = (row.get('employee_code') or '').strip().upper()
                if not code:
                    continue
                by_code[code].append({
                    'date': row.get('date') or '',
                    'km': _to_float(row.get('total_distance_km')),
                    'odo': _to_float(row.get('odometer_distance_km')),
                    'designation': row.get('designation') or '',
                    'team': row.get('team') or '',
                    'employee_label': row.get('employee_label') or '',
                    'source_file': os.path.basename(path),
                })
    return by_code


def km_for(unolo, code, date_from, date_to):
    """Total km logged for employee between date_from and date_to (inclusive).
    Dates may be ISO strings or datetime.date objects. Returns float."""
    code = (code or '').strip().upper()
    if code not in unolo:
        return 0.0
    if isinstance(date_from, str):
        date_from = datetime.date.fromisoformat(date_from)
    if isinstance(date_to, str):
        date_to = datetime.date.fromisoformat(date_to)
    total = 0.0
    for entry in unolo[code]:
        try:
            d = datetime.date.fromisoformat(entry['date'])
        except ValueError:
            continue
        if date_from <= d <= date_to:
            total += entry['km']
    return total


def days_active(unolo, code, date_from, date_to):
    """How many days the employee logged > 0 km in the period."""
    code = (code or '').strip().upper()
    if code not in unolo:
        return 0
    if isinstance(date_from, str):
        date_from = datetime.date.fromisoformat(date_from)
    if isinstance(date_to, str):
        date_to = datetime.date.fromisoformat(date_to)
    n = 0
    for entry in unolo[code]:
        try:
            d = datetime.date.fromisoformat(entry['date'])
        except ValueError:
            continue
        if date_from <= d <= date_to and entry['km'] > 0:
            n += 1
    return n


def daily_breakdown(unolo, code, date_from, date_to):
    """Per-day entries in window (used for line-by-line audit trace)."""
    code = (code or '').strip().upper()
    out = []
    if code not in unolo:
        return out
    if isinstance(date_from, str):
        date_from = datetime.date.fromisoformat(date_from)
    if isinstance(date_to, str):
        date_to = datetime.date.fromisoformat(date_to)
    for entry in unolo[code]:
        try:
            d = datetime.date.fromisoformat(entry['date'])
        except ValueError:
            continue
        if date_from <= d <= date_to:
            out.append(entry)
    return out


if __name__ == '__main__':
    import sys, json
    unolo = load_unolo()
    print(f'Loaded {len(unolo)} employees from {UNOLO_DIR}', file=sys.stderr)
    if len(sys.argv) >= 2:
        code = sys.argv[1].upper()
        d_from = sys.argv[2] if len(sys.argv) > 2 else '2026-04-01'
        d_to   = sys.argv[3] if len(sys.argv) > 3 else '2026-04-30'
        print(json.dumps({
            'code': code,
            'window': [d_from, d_to],
            'total_km': km_for(unolo, code, d_from, d_to),
            'days_active': days_active(unolo, code, d_from, d_to),
            'daily': daily_breakdown(unolo, code, d_from, d_to)[:31],
        }, indent=2, default=str))
    else:
        # show top-10 most active codes
        ranked = sorted(((c, sum(e['km'] for e in es)) for c, es in unolo.items()),
                        key=lambda x: -x[1])[:10]
        for code, total in ranked:
            print(f'{code}: {total:,.1f} km')
