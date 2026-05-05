"""
Extract structured data from a Rite Water Spine HR Expense Voucher PDF.

The Spine HR voucher PDF has a consistent layout. Header fields appear as
"Voucher No. <N>", "Voucher date <D>", "Employee Name <N> Employee Code <C>",
"For the period <D1> to <D2> Cost Center <CC>", "Narration <text>".
Line items live in a table with columns:
  Expense Head | Expense Date | Remarks | (currency) | Claimed | Approved | Rejected
Totals follow as "Gross Payable" and "Net Payable/Recoverable" rows.

We use pdfplumber's table detection + regex over the page text. Every field
has a deterministic rule so the same PDF always produces the same output.

Usage:
    python extract_voucher.py path/to/voucher.pdf > voucher.json
"""
import sys, json, re, os

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install pdfplumber --break-system-packages",
          file=sys.stderr)
    sys.exit(1)

MONTHS = {m: i for i, m in enumerate(
    ['', 'jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}


def parse_date(text):
    """Parse '17-Apr-26' / '17 Apr 2026' / '17/04/2026' to ISO YYYY-MM-DD."""
    if not text:
        return None
    text = str(text).strip()
    m = re.match(r'(\d{1,2})[-\s/]([A-Za-z]{3,9})[-\s/](\d{2,4})', text)
    if m:
        d = int(m.group(1))
        mo = MONTHS.get(m.group(2)[:3].lower())
        y = int(m.group(3))
        if mo:
            if y < 100:
                y += 2000
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r'(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})', text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def to_float(text):
    """Return float ONLY if text is a clean numeric value.
    Refuses to mine digits from prose like 'MH 40 CM 3268' so amount detection
    in line-item rows isn't contaminated.
    """
    if text is None:
        return None
    s = str(text).strip().replace(',', '')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


HEAD_PATTERNS = [
    (r'\bhotel\b|\blodging\b|\baccommodation\b|\bguest\s*house\b', 'Hotel'),
    (r'\bfood\s*allowance\b|\bper\s*diem\b|\bda\b|\bbreakfast\b|\blunch\b|\bdinner\b|\bmeal\b', 'Food Allowance'),
    (r'\b1\s*ac\b', '1AC Train'),
    (r'\b2\s*ac\b', '2AC Train'),
    (r'\b3\s*ac\b', '3AC Train'),
    (r'\bsleeper\b.*\btrain\b|\btrain\b.*\bsleeper\b', 'Sleeper Train'),
    (r'\btrain\b|\brailway\b|\birctc\b', 'Train'),
    (r'\bbus\b|\bmsrtc\b|\bksrtc\b|\bgsrtc\b|\bstate\s*transport\b', 'Bus'),
    (r'\bflight\b|\bair\b.*\btravel\b|\bairline\b|\bindigo\b|\bair\s*india\b|\bspice\s*jet\b|\bgo\s*air\b|\bvistara\b', 'Flight'),
    (r'\bcab\b|\btaxi\b|\brapido\b|\bola\b|\buber\b|\bzoom\s*car\b|\bmeru\b', 'Cab'),
    (r'\bauto\b(?!.*manager)', 'Auto'),
    (r'\bcruise\b', 'Cruise'),
    (r'\b2\s*wheeler\b|\btwo\s*wheeler\b|\bbike\b|\bmotorcycle\b|\bscooter\b', 'Petrol'),
    (r'\bpetrol\b|\bfuel\b|\bdiesel\b', 'Petrol'),
    (r'\bfas\s*tag\b|\bfastag\b|\bfast\s*tag\b', 'Toll'),
    (r'\btoll\b', 'Toll'),
    (r'\bparking\b', 'Parking'),
    (r'\bsite\s*expense', 'Site Expense'),
    (r'\bvehicle\s*service\b|\bservicing\b|\bmaintenance\b|\brepair\b|\btyres?\b|\bbattery\b', 'Vehicle Maintenance'),
    (r'\bmobile\s*recharge\b|\bsim\s*recharge\b|\bphone\s*recharge\b|\bdata\s*recharge\b|\bmobile\s*data\b|\bprepaid\s*recharge\b', 'Mobile Recharge'),
    (r'\bstationery\b|\bprinting\b|\bphotocopy\b|\bcourier\b|\bpostage\b', 'Office Expense'),
    (r'\bother\s*expense\b', 'Other Expense'),
    (r'\boffice\s*expense\b', 'Office Expense'),
]


def classify_head(raw_head, raw_remarks=''):
    text = f"{raw_head or ''} {raw_remarks or ''}".lower()
    for pat, label in HEAD_PATTERNS:
        if re.search(pat, text):
            return label
    return (raw_head or 'Unclassified').strip()


def extract_voucher(pdf_path):
    out = {
        'voucher_no': None,
        'voucher_date': None,
        'employee_name': None,
        'employee_code': None,
        'cost_center': None,
        'period_from': None,
        'period_to': None,
        'narration': None,
        'currency': 'INR',
        'gross_claimed': None,
        'gross_approved_by_reviewer': None,
        'gross_rejected': None,
        'net_payable': None,
        'line_items': [],
        'source_file': os.path.basename(pdf_path),
    }

    text_chunks = []
    table_rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text_chunks.append(page.extract_text() or '')
            for tbl in (page.extract_tables() or []):
                table_rows.extend(tbl)
    text = '\n'.join(text_chunks)

    # ---- Header fields ----
    m = re.search(r'Voucher\s*No\.?\s*(\d+)', text, re.I)
    if m:
        out['voucher_no'] = m.group(1).strip()

    m = re.search(r'Voucher\s*date\s*([0-9A-Za-z\-/\s]+?)(?:\n|Employee)', text, re.I)
    if m:
        out['voucher_date'] = parse_date(m.group(1))

    m = re.search(r'Employee\s*Name\s+(.+?)\s{2,}Employee\s*Code\s+([A-Z0-9_]+)', text, re.I)
    if m:
        out['employee_name'] = m.group(1).strip()
        out['employee_code'] = m.group(2).strip()
    else:
        m1 = re.search(r'Employee\s*Code\s+([A-Z0-9_]+)', text, re.I)
        if m1:
            out['employee_code'] = m1.group(1).strip()
        m2 = re.search(r'Employee\s*Name\s+([A-Z][A-Za-z .]+?)(?:\s*Employee|\n)', text, re.I)
        if m2:
            out['employee_name'] = m2.group(1).strip()

    DATE_RE = r'(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})'
    m = re.search(r'For\s*the\s*period\s+' + DATE_RE + r'\s+to\s+' + DATE_RE, text, re.I)
    if m:
        out['period_from'] = parse_date(m.group(1))
        out['period_to'] = parse_date(m.group(2))

    m = re.search(r'Cost\s*Center\s+([A-Za-z][A-Za-z0-9_\s]+?)(?:\n|Narration)', text, re.I)
    if m:
        out['cost_center'] = m.group(1).strip()

    m = re.search(r'Narration\s+(.+?)(?:\n|$)', text, re.I)
    if m:
        out['narration'] = m.group(1).strip()

    # ---- Line items from table rows ----
    HEADER_TOKENS = {'Expense Head', 'Expense\nDate', 'Claimed\nAmount',
                     'Approved\nAmount', 'Rejected\nAmount', 'Remarks'}
    TOTAL_LABELS = ('gross payable', 'net payable', 'net payable/recoverable',
                    'total', 'subtotal')

    for row in table_rows:
        if not row:
            continue
        cells = [(c or '').strip() if c is not None else '' for c in row]
        if not any(cells):
            continue
        if any(c in HEADER_TOKENS for c in cells):
            continue
        joined_lower = ' '.join(cells).lower()
        if any(lbl in joined_lower for lbl in TOTAL_LABELS):
            amounts = [a for a in (to_float(c) for c in cells) if a is not None]
            if 'gross' in joined_lower and amounts:
                out['gross_claimed'] = amounts[0]
                if len(amounts) >= 2:
                    out['gross_approved_by_reviewer'] = amounts[1]
                if len(amounts) >= 3:
                    out['gross_rejected'] = amounts[2]
            if 'net' in joined_lower and amounts:
                out['net_payable'] = amounts[0]
            continue
        date_iso = None
        for c in cells:
            d = parse_date(c)
            if d:
                date_iso = d
                break
        amounts = [a for a in (to_float(c) for c in cells) if a is not None]
        head = None
        for c in cells:
            if c in ('INR', 'Rs', 'Rs.', '₹'):
                continue
            if parse_date(c):
                continue
            if to_float(c) is not None:
                continue
            head = c
            break
        if not date_iso or not amounts:
            continue
        remarks = ''
        for c in cells:
            if c == head or parse_date(c) or to_float(c) is not None or c in ('INR', 'Rs', 'Rs.'):
                continue
            if len(c) > len(remarks):
                remarks = c
        out['line_items'].append({
            'date': date_iso,
            'expense_head_raw': head or '',
            'expense_head': classify_head(head, remarks),
            'remarks': remarks,
            'claimed_inr': amounts[0] if len(amounts) >= 1 else None,
            'approved_by_reviewer_inr': amounts[1] if len(amounts) >= 2 else (amounts[0] if amounts else None),
            'rejected_inr': amounts[2] if len(amounts) >= 3 else 0.0,
        })

    if out['gross_claimed'] is None:
        m = re.search(r'Gross\s*Payable\s*INR\s*([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)', text, re.I)
        if m:
            out['gross_claimed'] = to_float(m.group(1).replace(',', ''))
            out['gross_approved_by_reviewer'] = to_float(m.group(2).replace(',', ''))
            out['gross_rejected'] = to_float(m.group(3).replace(',', ''))
    if out['net_payable'] is None:
        m = re.search(r'Net\s*Payable[/\w]*\s*INR\s*([\d,.]+)', text, re.I)
        if m:
            out['net_payable'] = to_float(m.group(1).replace(',', ''))

    return out


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    data = extract_voucher(sys.argv[1])
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
