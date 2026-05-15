"""
One-shot wrapper that runs the full voucher audit pipeline.

Accepts 1 or 2 input files in any order:
  - A ZIP file is always treated as the proofs archive.
  - A PDF/image is treated as the expense document (Spine HR voucher or plain bill).

The engine auto-detects whether the document is a Spine HR voucher or a regular
expense document and adjusts extraction accordingly.

Usage:
  python run_audit.py <file1> [file2] [options]

Examples:
  python run_audit.py voucher.pdf proofs.zip          # voucher + proofs
  python run_audit.py proofs.zip voucher.pdf          # same, any order
  python run_audit.py hotel_bill.jpg --emp-code RWSIPL562   # single document
  python run_audit.py workshop_invoice.pdf            # emp code on document

Options:
  --emp-code CODE    Employee code for master lookup (e.g. RWSIPL123).
                     Required when the document does not show the employee code.
  --proofs-json FILE Pre-computed proofs index JSON (from ocr_proofs_claude.py)
                     — skips re-running OCR on the proofs ZIP.
  --out-dir DIR      Output directory (default: <skill>/output/)

Output files land in --out-dir:
  audit_voucher_<N>.json
  RiteAuditReport_Voucher<N>_<Name>_<Code>.pdf
"""
import sys, os, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datetime, hashlib  # used by _minimal_voucher_stub fallback
from audit_engine import run_audit
from generate_audit_pdf import render_pdf, output_filename

_ZIP_EXT  = {'.zip'}
_DOC_EXTS = {'.pdf', '.jpg', '.jpeg', '.png', '.webp', '.tiff', '.gif'}

# Used as a fallback when extract_document.build_voucher_stub can't be imported
# (e.g. the anthropic SDK isn't installed). Mirrors build_voucher_stub minus
# the _classify_head expense_type normalisation — agent-shaped JSON is expected
# to already use canonical expense_type names.
_EXPENSE_TYPE_FALLBACK = {
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


def _minimal_voucher_stub(raw, file_path, emp_code=None):
    fname     = os.path.basename(file_path)
    today     = datetime.date.today().isoformat()
    short_h   = hashlib.md5(fname.encode()).hexdigest()[:6].upper()
    voucher_no = raw.get('voucher_no') or f'DOC-{short_h}'
    date       = raw.get('date') or today
    total      = raw.get('total_amount')
    narration  = raw.get('narration') or fname

    raw_items = raw.get('line_items') or []
    if not raw_items and total:
        raw_items = [{'date': date, 'description': narration,
                      'expense_type': 'Other Expense', 'amount': total, 'remarks': ''}]
    line_items = []
    for li in raw_items:
        amt = float(li.get('amount') or 0)
        head_raw = li.get('expense_type') or li.get('description') or 'Other Expense'
        head = _EXPENSE_TYPE_FALLBACK.get((head_raw or '').lower().strip(),
                                           head_raw or 'Other Expense')
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
        'employee_name':              raw.get('employee_name'),
        'employee_code':              emp_code or raw.get('employee_code'),
        'cost_center':                raw.get('cost_center'),
        'period_from':                date,
        'period_to':                  date,
        'narration':                  narration,
        'currency':                   raw.get('currency') or 'INR',
        'gross_claimed':              gross,
        'gross_approved_by_reviewer': gross,
        'gross_rejected':             0.0,
        'net_payable':                gross,
        'line_items':                 line_items,
        'source_file':                fname,
    }


def _classify_inputs(files):
    """Split a list of file paths into (doc_path, zip_path) in any order."""
    doc_path = None
    zip_path = None
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in _ZIP_EXT:
            if zip_path:
                raise ValueError(f"More than one ZIP provided: {zip_path}, {f}")
            zip_path = f
        elif ext in _DOC_EXTS:
            if doc_path:
                raise ValueError(f"More than one document provided: {doc_path}, {f}")
            doc_path = f
        else:
            raise ValueError(
                f"Unrecognised file type '{f}'. "
                f"Supported: {sorted(_DOC_EXTS)} or {sorted(_ZIP_EXT)}")
    if not doc_path:
        raise ValueError("No document file provided (PDF or image).")
    return doc_path, zip_path


def main():
    ap = argparse.ArgumentParser(
        description='Audit an expense voucher or document. Files can be supplied in any order.')
    ap.add_argument('files', nargs='+',
                    help='1 or 2 input files: expense document (PDF/image) and/or '
                         'proofs archive (ZIP), in any order')
    ap.add_argument('--emp-code', default=None,
                    help='Employee code for master lookup — needed when the document '
                         'does not show the employee code (e.g. RWSIPL123)')
    ap.add_argument('--out-dir', default=None,
                    help='Output directory (default: <skill>/output/)')
    ap.add_argument('--proofs-json', default=None,
                    help='Pre-computed proofs index JSON (skips re-running OCR on ZIP)')
    ap.add_argument('--voucher-json', default=None,
                    help='Pre-extracted voucher dict JSON (skips Claude Vision document '
                         'extraction — used by cowork_stage.py for cowork-native runs)')
    args = ap.parse_args()

    if len(args.files) > 2:
        ap.error('Too many files — provide at most one document and one proofs ZIP.')

    # Validate all files exist first
    for f in args.files:
        if not os.path.exists(f):
            ap.error(f'File not found: {f}')

    doc_path, zip_path = _classify_inputs(args.files)
    print(f'  Document : {os.path.basename(doc_path)}')
    if zip_path:
        print(f'  Proofs   : {os.path.basename(zip_path)}')
    else:
        print(f'  Proofs   : (none — document will be used as its own proof)')

    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir    = args.out_dir or os.path.join(skill_root, 'output')
    os.makedirs(out_dir, exist_ok=True)

    proofs_data = None
    if args.proofs_json:
        with open(args.proofs_json, encoding='utf-8') as f:
            proofs_data = json.load(f)
        # Strip cowork _schema annotations from individual proof entries so they
        # don't pollute the audit JSON.
        for p in proofs_data.get('proofs', []):
            p.pop('_schema', None)
        print(f'[0/3] Loaded pre-computed proofs: {len(proofs_data.get("proofs", []))} files')

    voucher_data = None
    if args.voucher_json:
        with open(args.voucher_json, encoding='utf-8') as f:
            raw_voucher = json.load(f)
        raw_voucher.pop('_schema', None)
        # Reuse extract_document.build_voucher_stub to convert agent-shaped JSON
        # into the audit_engine's expected voucher dict.
        try:
            from extract_document import build_voucher_stub
            voucher_data = build_voucher_stub(raw_voucher, doc_path,
                                               emp_code=args.emp_code)
        except ImportError:
            # extract_document failed to import (no anthropic SDK). The dict the
            # agent provided already has the canonical shape modulo this builder
            # — fall back to using it directly with minimal massaging.
            voucher_data = _minimal_voucher_stub(raw_voucher, doc_path,
                                                  emp_code=args.emp_code)
        print(f'[0/3] Loaded pre-extracted voucher: '
              f'#{voucher_data.get("voucher_no")} '
              f'({len(voucher_data.get("line_items") or [])} line items)')

    print('[1/3] Extracting document and proofs...')
    try:
        audit = run_audit(doc_path, zip_path,
                          proofs_data=proofs_data,
                          voucher_data=voucher_data,
                          emp_code_hint=args.emp_code)
    except RuntimeError as e:
        msg = str(e)
        if 'Cowork-vision-only' in msg:
            print('\nERROR: This skill is Cowork-vision-only — it does not OCR '
                  'proof images itself.', file=sys.stderr)
            print('\nNext steps:', file=sys.stderr)
            stage_dir = os.path.join(out_dir, '_staging',
                                     os.path.splitext(os.path.basename(zip_path))[0])
            print(f'  1. python scripts/cowork_stage.py --proofs "{zip_path}" '
                  f'--out "{stage_dir}"', file=sys.stderr)
            print(f'  2. (Cowork agent) Read each image under {stage_dir}/proofs/, '
                  f'fill {stage_dir}/proofs.json', file=sys.stderr)
            print(f'  3. python scripts/cowork_stage.py --finalize "{stage_dir}"',
                  file=sys.stderr)
            print(f'  4. python scripts/run_audit.py "{doc_path}" "{zip_path}" '
                  f'--proofs-json "{stage_dir}/proofs.json" --out-dir "{out_dir}"',
                  file=sys.stderr)
            sys.exit(2)
        raise

    voucher_no = audit['voucher'].get('voucher_no') or 'X'
    json_path  = os.path.join(out_dir, f'audit_voucher_{voucher_no}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(audit, f, indent=2, ensure_ascii=False, default=str)
    print(f'[2/3] Findings JSON written: {json_path}')

    pdf_path = os.path.join(out_dir, output_filename(audit))
    render_pdf(audit, pdf_path)
    print(f'[3/3] Audit PDF written: {pdf_path}')

    t = audit['totals']
    print('\nSummary:')
    print(f'  Employee   : {audit["voucher"].get("employee_name")} ({audit["voucher"].get("employee_code")})')
    print(f'  Reference  : #{voucher_no}, dated {audit["voucher"].get("voucher_date")}')
    print(f'  Designation: {audit["employee"].get("designation_master")} '
          f'-> {audit["employee"]["designation_bucket"]}')
    print(f'  City Grade : {audit["city_grade"]["grade"]} '
          f'({audit["city_grade"]["derived_city"] or "not derivable"})')
    print(f'  Claimed    : Rs.{t["gross_claimed"]:,.0f}')
    print(f'  Approved   : Rs.{t["reviewer_approved"]:,.0f}')
    print(f'  Eligible   : Rs.{t["policy_eligible"]:,.0f}')
    print(f'  Hold       : Rs.{t["recommended_hold"]:,.0f}')
    print(f'  Concerns   : {len(audit["breaches"])} ('
          f'{sum(1 for b in audit["breaches"] if b["severity"] == "HIGH")} High, '
          f'{sum(1 for b in audit["breaches"] if b["severity"] == "MEDIUM")} Medium, '
          f'{sum(1 for b in audit["breaches"] if b["severity"] == "LOW")} Low)')

    # ── Auto-log to Google Sheets ─────────────────────────────────────────────
    _auto_log_to_sheets(json_path)


def _auto_log_to_sheets(json_path):
    """Silently append this audit to Google Sheets if credentials are configured."""
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Locate credentials
    creds_path = (os.environ.get('GOOGLE_CREDS_FILE')
                  or os.path.join(skill_root, 'google_creds.json'))
    if not os.path.exists(creds_path):
        return  # no creds configured — skip silently

    # Locate sheet ID
    sheet_id = os.environ.get('GOOGLE_SHEET_ID')
    if not sheet_id:
        cfg_path = os.path.join(skill_root, 'sheets_config.json')
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding='utf-8') as f:
                    sheet_id = json.load(f).get('sheet_id', '')
            except Exception:
                pass
    if not sheet_id:
        return  # no sheet ID configured — skip silently

    try:
        log_script = os.path.join(skill_root, 'scripts', 'log_to_sheets.py')
        import subprocess
        result = subprocess.run(
            [sys.executable, log_script, json_path,
             '--sheet-id', sheet_id, '--creds', creds_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print(f'  Sheets     : logged to Google Sheet ✓')
        else:
            print(f'  Sheets     : could not log ({result.stderr.strip()[:80]})')
    except Exception as e:
        print(f'  Sheets     : skipped ({e})')


if __name__ == '__main__':
    main()
