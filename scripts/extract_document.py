"""
Claude-vision extraction for non-Spine-HR expense documents.

Provides:
  is_spine_hr_voucher(pdf_path)  -> bool
  extract_from_document(path)    -> dict  (raw Claude response)
  build_voucher_stub(extracted, path, emp_code=None) -> voucher dict
  build_proof_entry(path, extracted)                 -> proof index entry
"""
import os, re, base64, hashlib, datetime, json, sys

try:
    import anthropic
except ImportError as _e:
    # Re-raise so callers can catch it and fall back. Only exit when run
    # directly as a script.
    if __name__ == '__main__':
        print("ERROR: anthropic SDK not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)
    raise


def _load_shared_env():
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shared_env = os.path.normpath(os.path.join(skill_root, '..', '.env.shared'))
    if os.path.exists(shared_env):
        with open(shared_env) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip()

_load_shared_env()
_CLIENT = anthropic.Anthropic()
_MODEL  = "claude-haiku-4-5-20251001"

_EXTRACT_SYSTEM = """\
You are an expense document parser for an Indian company (Rite Water Solutions).
Given any expense document — bill, invoice, receipt, workshop bill, hotel bill,
travel ticket — extract all details and return ONLY a JSON object (no markdown
fences, no explanation) with these exact keys:

  voucher_no:    invoice/bill/receipt number (string or null)
  date:          primary document date in YYYY-MM-DD (string or null)
  employee_name: name of the employee/claimant if visible (string or null)
  employee_code: employee ID/code (e.g. RWSIPL123) if visible (string or null)
  vendor:        merchant or service provider name (string or null)
  narration:     one-line description of what this expense is for (string)
  cost_center:   department, project site, or cost centre if visible (string or null)
  currency:      "INR" unless explicitly stated otherwise
  total_amount:  final total or amount payable as a plain number (null if unclear)
  gstin:         GST number on the document (string or null)
  payment_mode:  Cash / UPI / Card / Cheque / Bank Transfer (string or null)
  document_type: one of: invoice, receipt, hotel_bill, workshop_bill, food_bill,
                 travel_ticket, other
  is_handwritten: true if the document is handwritten, false if printed/typed
  line_items: array of expense lines found in the document, each containing:
    {
      date:         YYYY-MM-DD (use document date if no per-line date available)
      description:  what this line item is
      expense_type: one of: Hotel, Food Allowance, Bus, Train, Flight, Cab, Auto,
                    Fuel, Toll, Parking, Site Expense, Vehicle Maintenance, Other Expense
      amount:       numeric amount (plain number, no symbols)
      remarks:      any extra detail (string, may be empty)
    }

If a single-item document has no table, create exactly one line_item with the
full amount. All amounts must be plain numbers — no currency symbols, no commas.
"""


# ---------------------------------------------------------------------------
# Spine HR detection
# ---------------------------------------------------------------------------

def is_spine_hr_voucher(pdf_path):
    """Return True if the PDF is a Spine HR expense voucher form."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = ' '.join(
                (page.extract_text() or '') for page in pdf.pages[:2]
            ).upper()
        markers = [
            'VOUCHER NO', 'EMPLOYEE CODE', 'GROSS PAYABLE',
            'NET PAYABLE', 'NARRATION', 'FOR THE PERIOD',
        ]
        return sum(1 for m in markers if m in text) >= 4
    except Exception:
        return False


# ---------------------------------------------------------------------------
# PDF → images helper
# ---------------------------------------------------------------------------

def _pdf_to_images(pdf_path):
    """Render PDF pages as JPEG base64 strings. Returns [(b64, media_type)]."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        import io
        out = []
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            buf = io.BytesIO(pix.tobytes('jpeg'))
            out.append((base64.standard_b64encode(buf.getvalue()).decode(), 'image/jpeg'))
        return out
    except ImportError:
        pass
    try:
        from pdf2image import convert_from_path
        import io
        pages = convert_from_path(pdf_path, dpi=150)
        out = []
        for p in pages:
            buf = io.BytesIO()
            p.save(buf, format='JPEG', quality=85)
            out.append((base64.standard_b64encode(buf.getvalue()).decode(), 'image/jpeg'))
        return out
    except Exception:
        pass
    return []


def _compress_to_limit(raw, limit=4_500_000):
    """Return (compressed_bytes, 'image/jpeg') under limit, or raise RuntimeError."""
    import io
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow not installed — run: pip install Pillow")
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    ratio = (limit / len(raw)) ** 0.5
    if ratio < 1.0:
        img = img.resize((max(1, int(img.width * ratio)),
                          max(1, int(img.height * ratio))), Image.LANCZOS)
    for quality in (85, 75, 60, 45):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        if buf.tell() <= limit:
            return buf.getvalue(), 'image/jpeg'
    raise RuntimeError(f"Could not compress image below {limit} bytes")


def _image_b64(path):
    """Read image file and return (b64, media_type), resizing if needed."""
    ext = path.lower().rsplit('.', 1)[-1]
    mt  = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
           'png': 'image/png',  'webp': 'image/webp',
           'tiff': 'image/tiff'}.get(ext, 'image/jpeg')
    raw = open(path, 'rb').read()
    if len(raw) > 4_500_000:
        try:
            raw, mt = _compress_to_limit(raw)
        except Exception as exc:
            print(f"  WARNING: cannot compress image ({exc})", file=sys.stderr)
    return base64.standard_b64encode(raw).decode(), mt


# ---------------------------------------------------------------------------
# Claude vision extraction
# ---------------------------------------------------------------------------

def extract_from_document(file_path):
    """Send document to Claude and return parsed expense dict (may be {})."""
    ext  = file_path.lower().rsplit('.', 1)[-1]
    content = []

    if ext == 'pdf':
        images = _pdf_to_images(file_path)
        if images:
            for b64, mt in images[:6]:  # cap at 6 pages
                content.append({"type": "image",
                                 "source": {"type": "base64", "media_type": mt, "data": b64}})
        else:
            # Fallback: send raw PDF as a document block
            raw = open(file_path, 'rb').read()
            b64 = base64.standard_b64encode(raw).decode()
            content.append({"type": "document",
                             "source": {"type": "base64",
                                        "media_type": "application/pdf",
                                        "data": b64}})
    else:
        b64, mt = _image_b64(file_path)
        content.append({"type": "image",
                         "source": {"type": "base64", "media_type": mt, "data": b64}})

    content.append({"type": "text",
                     "text": "Extract all expense details from this document."})

    resp = _CLIENT.messages.create(
        model=_MODEL,
        max_tokens=512,
        system=[{
            "type": "text",
            "text": _EXTRACT_SYSTEM,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  WARNING: Claude returned non-JSON. Preview: {raw[:200]}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Voucher stub builder
# ---------------------------------------------------------------------------

_EXPENSE_TYPE_MAP = {
    'hotel':               'Hotel',
    'food allowance':      'Food Allowance',
    'bus':                 'Bus',
    'train':               'Train',
    'flight':              'Flight',
    'cab':                 'Cab',
    'auto':                'Auto',
    'fuel':                'Petrol',
    'toll':                'Toll',
    'parking':             'Parking',
    'site expense':        'Site Expense',
    'vehicle maintenance': 'Vehicle Maintenance',
    'other expense':       'Other Expense',
}


def _classify_head(expense_type):
    return _EXPENSE_TYPE_MAP.get((expense_type or '').lower().strip(),
                                  expense_type or 'Other Expense')


def build_voucher_stub(extracted, file_path, emp_code=None):
    """Return a voucher dict compatible with audit_engine.run_audit()."""
    fname     = os.path.basename(file_path)
    today     = datetime.date.today().isoformat()
    short_h   = hashlib.md5(fname.encode()).hexdigest()[:6].upper()
    voucher_no = extracted.get('voucher_no') or f'DOC-{short_h}'
    date       = extracted.get('date') or today
    total      = extracted.get('total_amount')
    narration  = extracted.get('narration') or fname

    raw_items = extracted.get('line_items') or []
    if not raw_items and total:
        raw_items = [{
            'date':         date,
            'description':  narration,
            'expense_type': 'Other Expense',
            'amount':       total,
            'remarks':      '',
        }]

    line_items = []
    for li in raw_items:
        amt      = float(li.get('amount') or 0)
        head_raw = li.get('expense_type') or li.get('description') or 'Other Expense'
        head     = _classify_head(head_raw)
        line_items.append({
            'date':                    li.get('date') or date,
            'expense_head_raw':        head_raw,
            'expense_head':            head,
            'remarks':                 li.get('remarks') or li.get('description') or '',
            'claimed_inr':             amt,
            'approved_by_reviewer_inr': amt,
            'rejected_inr':            0.0,
        })

    gross = float(total) if total else sum(li['claimed_inr'] for li in line_items)
    return {
        'voucher_no':                 voucher_no,
        'voucher_date':               date,
        'employee_name':              emp_code and extracted.get('employee_name') or extracted.get('employee_name'),
        'employee_code':              emp_code or extracted.get('employee_code'),
        'cost_center':                extracted.get('cost_center'),
        'period_from':                date,
        'period_to':                  date,
        'narration':                  narration,
        'currency':                   extracted.get('currency') or 'INR',
        'gross_claimed':              gross,
        'gross_approved_by_reviewer': gross,
        'gross_rejected':             0.0,
        'net_payable':                gross,
        'line_items':                 line_items,
        'source_file':                fname,
    }


# ---------------------------------------------------------------------------
# Proof index entry builder
# ---------------------------------------------------------------------------

def build_proof_entry(file_path, extracted):
    """Return a proof index dict (same schema as ocr_proofs_claude.py output)."""
    fname     = os.path.basename(file_path)
    total     = extracted.get('total_amount')
    amounts   = [float(total)] if isinstance(total, (int, float)) else []
    date_str  = extracted.get('date')
    vendor    = extracted.get('vendor')
    inv_no    = extracted.get('voucher_no')

    doc_type    = extracted.get('document_type') or 'other'
    handwritten = extracted.get('is_handwritten', False)
    if doc_type == 'hotel_bill':
        kind = 'hotel_bill_handwritten' if handwritten else 'hotel_bill_printed'
    elif doc_type == 'workshop_bill':
        kind = 'workshop_bill_handwritten' if handwritten else 'workshop_bill_printed'
    elif doc_type in ('invoice', 'receipt', 'food_bill'):
        kind = 'receipt_photo' if handwritten else 'purchase_invoice'
    elif doc_type == 'travel_ticket':
        kind = 'train_ticket'
    else:
        kind = 'other'

    if inv_no:
        fingerprint = 'id:' + inv_no
    elif vendor or total:
        fp_key = f"{vendor}|{total}|{date_str}"
        fingerprint = 'fp:' + hashlib.md5(fp_key.encode()).hexdigest()[:10]
    else:
        fingerprint = 'unique:' + hashlib.md5(fname.encode()).hexdigest()[:10]

    return {
        'file_name':    fname,
        'kind':         kind,
        'ride_id':      None,
        'invoice_no':   inv_no,
        'txn_id':       None,
        'pnr':          None,
        'gstin':        extracted.get('gstin'),
        'vehicle_no':   None,
        'check_in':     None,
        'check_out':    None,
        'nights':       None,
        'amounts':      amounts,
        'amount':       total,
        'dates':        [date_str] if date_str else [],
        'vendor':       vendor,
        'text_preview': (f"[Claude OCR] vendor={vendor} "
                         f"amounts={amounts} dates={[date_str] if date_str else []}"),
        'fingerprint':  fingerprint,
        'duplicate_of': None,
    }
