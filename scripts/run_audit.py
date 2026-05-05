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
from audit_engine import run_audit
from generate_audit_pdf import render_pdf, output_filename

_ZIP_EXT  = {'.zip'}
_DOC_EXTS = {'.pdf', '.jpg', '.jpeg', '.png', '.webp', '.tiff', '.gif'}


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
        print(f'[0/3] Loaded pre-computed proofs: {len(proofs_data.get("proofs", []))} files')

    print('[1/3] Extracting document and proofs...')
    audit = run_audit(doc_path, zip_path,
                      proofs_data=proofs_data,
                      emp_code_hint=args.emp_code)

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


if __name__ == '__main__':
    main()
