"""
cowork_stage.py — Stage a voucher + proofs ZIP for cowork-native vision OCR.

Use this when running the audit from inside Cowork without an
`ANTHROPIC_API_KEY`. It extracts every proof image and the voucher document
into a staging directory, writes empty JSON templates the cowork agent fills
in, and emits a `manifest.json` describing what's pending.

Once the agent has populated `voucher.json` and `proofs.json`, run:

    python scripts/run_audit.py <voucher_doc> <proofs_zip> \\
        --emp-code <CODE> \\
        --voucher-json <staging>/voucher.json \\
        --proofs-json  <staging>/proofs.json

…and the audit completes without making any API calls.

See `references/cowork_vision_protocol.md` for the full contract.

Usage:
    python scripts/cowork_stage.py --voucher <doc.pdf|.jpg> --proofs <proofs.zip> \\
                                    --out <staging_dir> [--emp-code <CODE>]
    python scripts/cowork_stage.py --proofs <proofs.zip>   --out <staging_dir>     # zip-only mode
    python scripts/cowork_stage.py --finalize <staging_dir>                        # re-fingerprint proofs.json
"""
import os, sys, json, argparse, hashlib, zipfile, shutil, datetime, re
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'tiff', 'gif'}
DOC_EXTS   = IMAGE_EXTS | {'pdf'}

# Schema constants kept in lock-step with extract_document.py / ocr_proofs_claude.py.
EXPENSE_TYPES = [
    'Hotel', 'Food Allowance', 'Bus', 'Train', 'Flight', 'Cab', 'Auto',
    'Fuel', 'Toll', 'Parking', 'Site Expense', 'Vehicle Maintenance', 'Other Expense',
]
DOC_TYPES = [
    'invoice', 'receipt', 'hotel_bill', 'workshop_bill', 'food_bill',
    'travel_ticket', 'other',
]
PROOF_KINDS = [
    'upi_screenshot', 'fastag_screenshot', 'bank_transfer_screenshot',
    'payment_screenshot', 'hotel_bill_printed', 'hotel_bill_handwritten',
    'train_ticket', 'bus_ticket', 'flight_ticket', 'cab_receipt', 'auto_receipt',
    'fuel_receipt', 'workshop_bill_printed', 'workshop_bill_handwritten',
    'food_receipt', 'purchase_invoice', 'receipt_photo', 'other',
]


# ---------------------------------------------------------------------------
# Spine HR detection (mirrors extract_document.is_spine_hr_voucher) — but kept
# inline here so cowork_stage doesn't need anthropic SDK to import.
# ---------------------------------------------------------------------------

def _is_spine_hr_pdf(pdf_path):
    if not pdf_path or not pdf_path.lower().endswith('.pdf'):
        return False
    try:
        import pdfplumber
    except ImportError:
        try:
            import fitz
            doc = fitz.open(pdf_path)
            text = ' '.join((p.get_text() or '') for p in doc[:2]).upper()
        except Exception:
            return False
    else:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = ' '.join((p.extract_text() or '') for p in pdf.pages[:2]).upper()
        except Exception:
            return False
    markers = ['VOUCHER NO', 'EMPLOYEE CODE', 'GROSS PAYABLE',
               'NET PAYABLE', 'NARRATION', 'FOR THE PERIOD']
    return sum(1 for m in markers if m in text) >= 4


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

VOUCHER_TEMPLATE = {
    "_schema": {
        "_comment": (
            "Read voucher_doc.<ext> natively, then fill in this template and "
            "save as voucher.json. All amounts are plain numbers (no Rs symbol, "
            "no commas). Allowed expense_type values are listed in the "
            "_allowed_expense_types field. Allowed document_type values are in "
            "_allowed_document_types. A single-amount doc gets exactly one "
            "line_item."
        ),
        "_allowed_expense_types": EXPENSE_TYPES,
        "_allowed_document_types": DOC_TYPES,
    },
    "voucher_no":    None,
    "date":          None,
    "employee_name": None,
    "employee_code": None,
    "vendor":        None,
    "narration":     "",
    "cost_center":   None,
    "currency":      "INR",
    "total_amount":  None,
    "gstin":         None,
    "payment_mode":  None,
    "document_type": "other",
    "is_handwritten": False,
    "line_items":    [
        {"date": None, "description": "", "expense_type": "Other Expense",
         "amount": None, "remarks": ""}
    ],
}


def _proof_template(file_name):
    return {
        "_schema": {
            "_comment": (
                "Read the image at image_path and fill in the OCR fields. "
                "amounts is a list of all rupee amounts visible (NOT odometer "
                "readings, NOT phone numbers). amount = the primary one (max). "
                "On UPI/payment screenshots, amount is the rupees transferred. "
                "On fuel receipts, amount is the small rupee amount paid (under "
                "Rs.2000 typically), NEVER the 5-6 digit odometer reading. "
                "dates in YYYY-MM-DD. Empty arrays/null for fields not visible. "
                "fingerprint and duplicate_of can be left null — cowork_stage "
                "--finalize will compute them from the other fields."
            ),
            "_allowed_kinds": PROOF_KINDS,
        },
        "file_name":      file_name,
        "kind":           "other",
        "ride_id":        None,
        "invoice_no":     None,
        "txn_id":         None,
        "pnr":            None,
        "gstin":          None,
        "vehicle_no":     None,
        "check_in":       None,
        "check_out":      None,
        "nights":         None,
        "odometer_start": None,
        "odometer_end":   None,
        "amounts":        [],
        "amount":         None,
        "dates":          [],
        "vendor":         None,
        "persons_named":  [],
        "text_preview":   "",
        "fingerprint":    None,
        "duplicate_of":   None,
    }


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def stage(voucher_path, proofs_zip, out_dir, emp_code=None):
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'proofs').mkdir(exist_ok=True)
    (out / 'proof_templates').mkdir(exist_ok=True)

    session_id = 'stage_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    # ---- Voucher staging ----
    voucher_entry = None
    if voucher_path:
        vsrc = Path(voucher_path).resolve()
        if not vsrc.exists():
            print(f'ERROR: voucher document not found: {vsrc}', file=sys.stderr)
            sys.exit(2)
        ext = vsrc.suffix.lstrip('.').lower() or 'bin'
        if ext not in DOC_EXTS:
            print(f'ERROR: voucher must be PDF or image, got .{ext}', file=sys.stderr)
            sys.exit(2)
        vdest = out / f'voucher_doc.{ext}'
        shutil.copy(vsrc, vdest)
        is_spine = _is_spine_hr_pdf(str(vsrc))
        voucher_entry = {
            'is_spine_hr':   is_spine,
            'doc_path':      vdest.name,
            'template_path': None if is_spine else 'voucher_template.json',
            'response_path': None if is_spine else 'voucher.json',
        }
        if not is_spine:
            with open(out / 'voucher_template.json', 'w', encoding='utf-8') as f:
                json.dump(VOUCHER_TEMPLATE, f, indent=2)
            # Pre-create voucher.json as a copy of the template (without _schema)
            seed = {k: v for k, v in VOUCHER_TEMPLATE.items() if not k.startswith('_')}
            with open(out / 'voucher.json', 'w', encoding='utf-8') as f:
                json.dump(seed, f, indent=2)

    # ---- Proofs staging ----
    proof_entries = []
    if proofs_zip:
        zsrc = Path(proofs_zip).resolve()
        if not zsrc.exists():
            print(f'ERROR: proofs ZIP not found: {zsrc}', file=sys.stderr)
            sys.exit(2)

        seen_hashes = set()
        with zipfile.ZipFile(zsrc, 'r') as zf:
            for info in zf.infolist():
                fname_only = os.path.basename(info.filename)
                if not fname_only or fname_only.startswith(('.', '__')):
                    continue
                ext = fname_only.lower().rsplit('.', 1)[-1] if '.' in fname_only else ''
                if ext not in IMAGE_EXTS:
                    continue
                raw = zf.read(info.filename)
                h = hashlib.md5(raw).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', fname_only)
                staged_name = f'{h}_{safe_name}'
                with open(out / 'proofs' / staged_name, 'wb') as f:
                    f.write(raw)
                tmpl = _proof_template(fname_only)
                with open(out / 'proof_templates' / f'{h}.json', 'w', encoding='utf-8') as f:
                    json.dump(tmpl, f, indent=2)
                proof_entries.append({
                    'file_name':     fname_only,
                    'image_path':    f'proofs/{staged_name}',
                    'template_path': f'proof_templates/{h}.json',
                    'hash':          h,
                })

    # Pre-create proofs.json as a list of empty templates (without _schema)
    if proof_entries:
        seed_proofs = []
        for entry in proof_entries:
            tmpl_path = out / entry['template_path']
            with open(tmpl_path, encoding='utf-8') as f:
                tmpl = json.load(f)
            seed_proofs.append({k: v for k, v in tmpl.items() if not k.startswith('_')})
        with open(out / 'proofs.json', 'w', encoding='utf-8') as f:
            json.dump({'proofs': seed_proofs}, f, indent=2)

    manifest = {
        'session_id':           session_id,
        'created_at':           datetime.datetime.now().isoformat(timespec='seconds'),
        'voucher':              voucher_entry,
        'proofs':               proof_entries,
        'proofs_response_path': 'proofs.json',
        'emp_code_hint':        emp_code,
        'protocol_doc':         '../references/cowork_vision_protocol.md',
    }
    with open(out / 'manifest.json', 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    # Friendly summary
    print(f'Staged session: {session_id}')
    print(f'Staging dir   : {out}')
    if voucher_entry:
        if voucher_entry['is_spine_hr']:
            print(f'Voucher       : Spine HR PDF — no agent OCR needed')
        else:
            print(f'Voucher       : NOT Spine HR — agent must fill {voucher_entry["response_path"]}')
    else:
        print(f'Voucher       : (none provided)')
    print(f'Proofs        : {len(proof_entries)} unique image(s) — agent fills proofs.json')
    if proof_entries:
        print(f'                first: {proof_entries[0]["image_path"]}')
    print()
    print('NEXT STEPS for the cowork agent:')
    print('  1. Read manifest.json')
    if voucher_entry and not voucher_entry['is_spine_hr']:
        print(f'  2. Read {voucher_entry["doc_path"]} natively, edit voucher.json')
    print('  3. Read each image in proofs/, edit proofs.json (one entry per image)')
    print('  4. (Optional) python scripts/cowork_stage.py --finalize ' + str(out))
    print('  5. python scripts/run_audit.py <doc> <zip> '
          + (f'--emp-code {emp_code} ' if emp_code else '')
          + '--voucher-json voucher.json --proofs-json proofs.json --out-dir output/')

    return manifest


# ---------------------------------------------------------------------------
# Finalize — recompute fingerprints + duplicate_of from agent-filled proofs.json
# ---------------------------------------------------------------------------

def _fingerprint(rec, fname=''):
    keys = [rec.get('ride_id'), rec.get('invoice_no'), rec.get('txn_id')]
    key = next((k for k in keys if k), None)
    if key:
        return 'id:' + key
    vendor = rec.get('vendor')
    amount = rec.get('amount')
    dates  = rec.get('dates') or []
    if not vendor and not amount and not dates:
        return 'unique:' + hashlib.md5(fname.encode()).hexdigest()[:10]
    parts = [str(vendor or 'x'), str(amount or 0), str(dates[0] if dates else 'x')]
    return 'fp:' + hashlib.md5('|'.join(parts).encode()).hexdigest()[:10]


def finalize(staging_dir):
    out = Path(staging_dir).resolve()
    pj  = out / 'proofs.json'
    if not pj.exists():
        print(f'ERROR: {pj} not found', file=sys.stderr)
        sys.exit(2)
    with open(pj, encoding='utf-8') as f:
        data = json.load(f)

    seen = {}
    for p in data.get('proofs', []):
        # Recompute amount from amounts if missing
        if p.get('amount') is None and p.get('amounts'):
            p['amount'] = max(a for a in p['amounts'] if isinstance(a, (int, float)))
        # Recompute fingerprint
        p['fingerprint'] = _fingerprint(p, p.get('file_name', ''))
        # Build text_preview if empty
        if not p.get('text_preview'):
            p['text_preview'] = (
                f"[cowork-vision] vendor={p.get('vendor')} "
                f"amounts={p.get('amounts')} dates={p.get('dates')}"
            )
        # Cross-file dup detection
        fp = p['fingerprint']
        if fp in seen:
            p['duplicate_of'] = seen[fp]
        else:
            seen[fp] = p['file_name']
            p['duplicate_of'] = None

    with open(pj, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    print(f'Finalized: {pj} ({len(data.get("proofs", []))} proof(s))')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--voucher',  default=None, help='Voucher document (PDF or image)')
    ap.add_argument('--proofs',   default=None, help='Proofs ZIP archive')
    ap.add_argument('--out',      default=None, help='Staging directory')
    ap.add_argument('--emp-code', default=None, help='Employee code hint (e.g. RWSIPL706)')
    ap.add_argument('--finalize', default=None, metavar='STAGING_DIR',
                    help='Re-fingerprint and dedupe proofs.json after agent fills it')
    args = ap.parse_args()

    if args.finalize:
        finalize(args.finalize)
        return

    if not args.out:
        ap.error('--out is required (path to staging directory)')
    if not args.voucher and not args.proofs:
        ap.error('At least one of --voucher or --proofs must be supplied')

    stage(args.voucher, args.proofs, args.out, emp_code=args.emp_code)


if __name__ == '__main__':
    main()
