"""
Open a ZIP of supporting proof documents (PDFs and images), extract text from
each, and return a structured index that the audit engine can match against
voucher line items.

For each proof we capture:
- file name and type
- extracted text (PDF text layer or OCR for images)
- common identifiers (ride ID, invoice no., transaction ID, PNR, GSTIN, vehicle no.)
- amounts, dates, vendor hints
- check-in/out and nights (for hotel bills)
- a fingerprint used to detect duplicate submissions

Categories of proofs we expect (per Rite Water expense workflow):
  - Two Wheeler / Bike (petrol slips, bike service)
  - Food / Meals (restaurant bills - usually only relevant for designation>=Director,
    since per-diem covers others)
  - Bus / Train Travel (tickets, PNRs)
  - Hotel / Accommodation (tax invoices, payment proof)
  - FASTag / Toll (toll receipts)
  - Site Expenses (workshop bills, repairs, materials)
  - Site Deputation (>30 days lodging/fooding)

Duplicate detection compares ride/invoice/txn IDs first, then a hash of
(vendor, amount, first-date) when no ID is present.

Usage:
    python extract_proofs.py path/to/proofs.zip > proofs.json
"""
import sys, json, re, os, zipfile, tempfile, hashlib

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed.", file=sys.stderr)
    sys.exit(1)

try:
    from PIL import Image
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


PATTERNS = {
    'ride_id': r'(?:Ride\s*ID|Ride\s*Id|Booking\s*ID)\s*[:#]?\s*([A-Z0-9]{8,})',
    'invoice_no': r'(?:Invoice\s*(?:No|Number)|Bill\s*No|Inv\s*No)\.?\s*[:#]?\s*([A-Z0-9\-/]{5,})',
    'txn_id': r'(?:Txn\s*ID|Transaction\s*ID|UPI\s*Ref|UTR)\s*[:#]?\s*([A-Z0-9]{8,})',
    'pnr': r'\bPNR\s*[:#]?\s*(\d{10})\b',
    'gstin': r'(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d])',
    'amount': r'(?:Rs\.?|INR|Amount|Total|Paid|Grand\s*Total|\u20B9)\s*[:#]?\s*([\d,]+(?:\.\d+)?)',
    'check_in': r'(?:Check[-\s]*in|Arrival)\s*[:#]?\s*([0-9A-Za-z\-/\s,:]+)',
    'check_out': r'(?:Check[-\s]*out|Departure)\s*[:#]?\s*([0-9A-Za-z\-/\s,:]+)',
    'nights': r'(\d+)\s*night',
    'vehicle_no': r'\b([A-Z]{2}[\s-]?\d{1,2}[\s-]?[A-Z]{1,3}[\s-]?\d{3,4})\b',
}

VENDOR_HINTS = {
    'rapido': ['rapido', 'rd1773', 'rd1774'],
    'ola': ['ola cabs', 'ola.com'],
    'uber': ['uber'],
    'razorpay': ['razorpay'],
    'phonepe': ['phonepe'],
    'gpay': ['google pay', 'g pay', 'gpay'],
    'bhim_upi': ['bhim', '@upi', '@okicici', '@oksbi', '@okhdfc', '@okaxis', '@paytm'],
    'irctc': ['irctc'],
    'workshop_garage': ['workshop', 'garage', 'service center', 'auto care', 'motors'],
    'hotel': ['hotel', 'guest house', 'lodge', 'resort', 'paying guest'],
    'petrol_pump': ['petrol pump', 'fuel station', 'hp ', 'iocl', 'bharat petroleum', 'bpcl', 'shell'],
    'fastag': ['fastag', 'fast tag', 'paytm fastag', 'icici fastag'],
}


def detect_vendor(text):
    if not text:
        return None
    t = text.lower()
    for v, hints in VENDOR_HINTS.items():
        if any(h in t for h in hints):
            return v
    return None


def extract_amounts(text):
    if not text:
        return []
    out = []
    for m in re.finditer(PATTERNS['amount'], text, re.IGNORECASE):
        try:
            out.append(float(m.group(1).replace(',', '')))
        except ValueError:
            pass
    return out


def _fallback_amounts(text):
    """When prefix-based regex returns nothing, scan for comma-formatted
    amounts that look like receipt totals (>= 100, no more than 2 decimals)."""
    if not text:
        return []
    out = []
    for m in re.finditer(r'(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{3,7}(?:\.\d{1,2})?)(?![\d.])', text):
        try:
            v = float(m.group(1).replace(',', ''))
            if 100 <= v <= 1_000_000:
                out.append(v)
        except ValueError:
            pass
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for v in out:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def extract_first(pat, text, default=None):
    if not text:
        return default
    m = re.search(pat, text, re.IGNORECASE)
    return m.group(1).strip() if m else default


_MONTHS = {m: i for i, m in enumerate(
    ['', 'jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}


def _parse_date(s):
    s = s.strip()
    m = re.match(r'(\d{1,2})[-\s/]([A-Za-z]{3,9})[-\s/](\d{2,4})', s)
    if m:
        d = int(m.group(1)); mo = _MONTHS.get(m.group(2)[:3].lower()); y = int(m.group(3))
        if mo:
            if y < 100:
                y += 2000
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r'(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def extract_dates(text):
    if not text:
        return []
    dates = set()
    for pat in [r'\d{1,2}[-/]\d{1,2}[-/]\d{2,4}',
                r'\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}']:
        for m in re.finditer(pat, text):
            iso = _parse_date(m.group(0))
            if iso:
                dates.add(iso)
    return sorted(dates)


def fingerprint(record):
    keys = [record.get('ride_id'), record.get('invoice_no'), record.get('txn_id')]
    key = next((k for k in keys if k), None)
    if key:
        return "id:" + key
    parts = [str(record.get('vendor') or 'x'),
             str(record.get('amount') or 0),
             str((record.get('dates') or ['x'])[0])]
    h = hashlib.md5('|'.join(parts).encode()).hexdigest()[:10]
    return "fp:" + h


def extract_text_from_pdf(path):
    out = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                out.append(page.extract_text() or '')
    except Exception as e:
        return "[pdfplumber error: " + str(e) + "]"
    return '\n'.join(out)


def extract_text_from_image(path):
    if not HAS_OCR:
        return "[OCR unavailable: install pytesseract+Pillow to read " + os.path.basename(path) + "]"
    try:
        img = Image.open(path)
        if max(img.size) > 2400:
            ratio = 2400.0 / max(img.size)
            img = img.resize((int(img.size[0]*ratio), int(img.size[1]*ratio)))
        return pytesseract.image_to_string(img)
    except Exception as e:
        return "[OCR error: " + str(e) + "]"


def index_proofs(zip_path):
    proofs = []
    with zipfile.ZipFile(zip_path, 'r') as zf:
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            for root, _, files in os.walk(tmp):
                for fname in files:
                    if fname.startswith('.') or fname.startswith('__'):
                        continue
                    full = os.path.join(root, fname)
                    ext = fname.lower().rsplit('.', 1)[-1]
                    if ext == 'pdf':
                        text = extract_text_from_pdf(full)
                        kind = 'pdf'
                    elif ext in ('jpg', 'jpeg', 'png', 'webp', 'tiff'):
                        text = extract_text_from_image(full)
                        kind = 'image'
                    else:
                        text = ''
                        kind = ext
                    amounts = extract_amounts(text) or _fallback_amounts(text)
                    rec = {
                        'file_name': fname,
                        'kind': kind,
                        'ride_id': extract_first(PATTERNS['ride_id'], text),
                        'invoice_no': extract_first(PATTERNS['invoice_no'], text),
                        'txn_id': extract_first(PATTERNS['txn_id'], text),
                        'pnr': extract_first(PATTERNS['pnr'], text),
                        'gstin': extract_first(PATTERNS['gstin'], text),
                        'vehicle_no': extract_first(PATTERNS['vehicle_no'], text),
                        'check_in': extract_first(PATTERNS['check_in'], text),
                        'check_out': extract_first(PATTERNS['check_out'], text),
                        'nights': extract_first(PATTERNS['nights'], text),
                        'amounts': amounts,
                        'amount': max(amounts) if amounts else None,
                        'dates': extract_dates(text),
                        'vendor': detect_vendor(text),
                        'text_preview': (text[:1500] if text else ''),
                    }
                    rec['fingerprint'] = fingerprint(rec)
                    proofs.append(rec)
    seen = {}
    for p in proofs:
        fp = p['fingerprint']
        if fp in seen:
            p['duplicate_of'] = seen[fp]
        else:
            seen[fp] = p['file_name']
            p['duplicate_of'] = None
    return {'proofs': proofs}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    data = index_proofs(sys.argv[1])
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
