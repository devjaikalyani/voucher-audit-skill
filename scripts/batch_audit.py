"""
Batch audit runner for claims downloaded by spine_hr_browser.py.

Expected folder structure (created by the browser script):
  in_process_claims/
    {EMP_CODE}_{VOUCHER_NO}/
        proofs_{VOUCHER_NO}.zip     <- employee-uploaded attachments (required)
        voucher_{VOUCHER_NO}.pdf    <- Spine HR form PDF (optional but preferred)
    {EMP_CODE}_{VOUCHER_NO}/
        ...

For each folder this script:
  1. Parses EMP_CODE and VOUCHER_NO from the folder name
  2. Looks up employee name in assets/employee_master.csv by code
  3. Skips if proofs ZIP is missing
  4. Skips if an audit report for this voucher already exists in output/
  5. Runs:  run_audit.py <voucher.pdf> <proofs.zip> --emp-code CODE --out-dir output/
     OR:    run_audit.py <best_doc_from_zip> <proofs.zip> --emp-code CODE
     depending on whether a voucher PDF was downloaded

Also handles the legacy _browser_downloads/ layout for ZIPs not yet moved into
per-claim folders.

Usage:
    python scripts/batch_audit.py [--claims-dir PATH] [--out-dir PATH]
"""
import os, sys, re, csv, zipfile, tempfile, subprocess, argparse
from pathlib import Path

SKILL_ROOT   = Path(__file__).parent.parent
CLAIMS_DIR   = SKILL_ROOT / 'in_process_claims'
LEGACY_DIR   = CLAIMS_DIR / '_browser_downloads'
OUT_DIR      = SKILL_ROOT / 'output'
MASTER_CSV   = SKILL_ROOT / 'assets' / 'employee_master.csv'
RUN_AUDIT    = Path(__file__).parent / 'run_audit.py'

IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'tiff'}


# ---------------------------------------------------------------------------
# Employee master lookup
# ---------------------------------------------------------------------------

def _load_master():
    """Return dict {code_upper: name} from employee_master.csv."""
    out = {}
    try:
        with open(MASTER_CSV, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                code = (row.get('Code') or '').strip().upper()
                name = (row.get('Name') or '').strip()
                if code:
                    out[code] = name
    except Exception as e:
        print(f'[batch_audit] WARNING: could not load employee master: {e}', file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Folder / file helpers
# ---------------------------------------------------------------------------

def _already_audited(voucher_no, out_dir):
    for f in Path(out_dir).iterdir():
        if f'Voucher{voucher_no}' in f.name or f'voucher_{voucher_no}.' in f.name:
            return True
    return False


def _best_doc_from_zip(zip_path):
    """Extract the largest PDF (or largest image) from a ZIP to a temp file.
    Returns (temp_path, ext) or (None, None) if nothing usable found.
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        entries  = zf.infolist()
        pdfs     = [e for e in entries
                    if not os.path.basename(e.filename).startswith(('__', '.'))
                    and e.filename.lower().endswith('.pdf')]
        images   = [e for e in entries
                    if not os.path.basename(e.filename).startswith(('__', '.'))
                    and e.filename.lower().rsplit('.', 1)[-1] in IMAGE_EXTS]
        if pdfs:
            best = sorted(pdfs, key=lambda e: e.file_size, reverse=True)[0]
        elif images:
            best = sorted(images, key=lambda e: e.file_size, reverse=True)[0]
        else:
            return None, None

        ext = os.path.splitext(best.filename)[1] or '.bin'
        raw = zf.read(best.filename)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False,
                                     prefix='batch_doc_') as tf:
        tf.write(raw)
        return tf.name, ext


# ---------------------------------------------------------------------------
# Per-claim audit runner
# ---------------------------------------------------------------------------

def _run_one(emp_code, voucher_no, voucher_pdf, proofs_zip, out_dir, master):
    """Run run_audit.py for one claim. Returns True on success."""
    emp_name = master.get(emp_code.upper(), '(unknown)')
    label    = f'#{voucher_no} | {emp_code} | {emp_name}'
    temp_doc = None

    if voucher_pdf and os.path.exists(voucher_pdf):
        doc_arg  = voucher_pdf
        doc_label = os.path.basename(voucher_pdf)
    else:
        # No Spine HR voucher PDF — extract best document from proofs ZIP
        print(f'    no voucher PDF — extracting best document from proofs ZIP')
        temp_doc, _ = _best_doc_from_zip(proofs_zip)
        if not temp_doc:
            print(f'    SKIP {label}: no usable document found in ZIP')
            return False
        doc_arg   = temp_doc
        doc_label = f'(from ZIP) {os.path.basename(proofs_zip)}'

    print(f'  doc      : {doc_label}')
    print(f'  proofs   : {os.path.basename(proofs_zip)}')
    print(f'  employee : {emp_code} -> {emp_name}')

    try:
        result = subprocess.run(
            [sys.executable, str(RUN_AUDIT),
             doc_arg, proofs_zip,
             '--emp-code', emp_code,
             '--out-dir',  str(out_dir)],
            text=True
        )
        return result.returncode == 0
    finally:
        if temp_doc:
            try:
                os.unlink(temp_doc)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Claim discovery
# ---------------------------------------------------------------------------

def _discover_claims(claims_dir, legacy_dir):
    """Yield (emp_code, voucher_no, voucher_pdf_or_None, proofs_zip) tuples.

    Scans:
      1. claims_dir/{EMP_CODE}_{VOUCHER_NO}/ subfolders (primary, new structure)
      2. legacy_dir/*.zip files  (old _browser_downloads layout)
    """
    seen_vouchers = set()

    # --- Primary: per-claim folders ---
    if claims_dir.is_dir():
        for folder in sorted(claims_dir.iterdir()):
            if not folder.is_dir() or folder.name.startswith('_'):
                continue
            m = re.match(r'^([A-Z]+\d+)_(\d+)$', folder.name, re.IGNORECASE)
            if not m:
                continue
            emp_code   = m.group(1).upper()
            voucher_no = m.group(2)

            # Find proofs ZIP:  proofs_{N}.zip  (required)
            proofs_zip = folder / f'proofs_{voucher_no}.zip'
            if not proofs_zip.exists():
                # Also accept any *.zip in the folder
                zips = list(folder.glob('*.zip'))
                proofs_zip = zips[0] if zips else None
            if not proofs_zip or not proofs_zip.exists():
                print(f'  SKIP {folder.name}: no proofs ZIP found')
                continue

            # Find voucher PDF:  voucher_{N}.pdf  (optional)
            voucher_pdf = folder / f'voucher_{voucher_no}.pdf'
            if not voucher_pdf.exists():
                pdfs = [p for p in folder.glob('*.pdf')
                        if 'proofs' not in p.name.lower()]
                voucher_pdf = pdfs[0] if pdfs else None

            seen_vouchers.add(voucher_no)
            yield emp_code, voucher_no, (str(voucher_pdf) if voucher_pdf else None), str(proofs_zip)

    # --- Legacy: _browser_downloads/*.zip ---
    if legacy_dir.is_dir():
        for zip_path in sorted(legacy_dir.glob('*.zip')):
            m = re.match(r'^([A-Z]+\d+)_(\d+)_\d+\.zip$', zip_path.name, re.IGNORECASE)
            if not m:
                continue
            emp_code   = m.group(1).upper()
            voucher_no = m.group(2)
            if voucher_no in seen_vouchers:
                continue  # already handled by per-claim folder
            yield emp_code, voucher_no, None, str(zip_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--claims-dir', default=str(CLAIMS_DIR))
    ap.add_argument('--out-dir',    default=str(OUT_DIR))
    args = ap.parse_args()

    claims_dir = Path(args.claims_dir)
    legacy_dir = claims_dir / '_browser_downloads'
    out_dir    = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    master = _load_master()
    print(f'Employee master loaded: {len(master)} entries')

    claims = list(_discover_claims(claims_dir, legacy_dir))
    if not claims:
        print(f'No claims found in {claims_dir}')
        return

    print(f'Found {len(claims)} claim(s) to process\n')

    success, skipped, failed = [], [], []

    for i, (emp_code, voucher_no, voucher_pdf, proofs_zip) in enumerate(claims, 1):
        emp_name = master.get(emp_code.upper(), '(unknown)')
        print(f'[{i}/{len(claims)}] Voucher #{voucher_no} | {emp_code} | {emp_name}')

        if _already_audited(voucher_no, out_dir):
            print(f'  skip: audit report already exists for #{voucher_no}')
            skipped.append(f'{emp_code}_#{voucher_no}')
            continue

        ok = _run_one(emp_code, voucher_no, voucher_pdf, proofs_zip, out_dir, master)
        if ok:
            success.append(f'{emp_code}_#{voucher_no}_{emp_name}')
        else:
            failed.append(f'{emp_code}_#{voucher_no}')
        print()

    print('=' * 60)
    print(f'Batch complete: {len(success)} succeeded  |  '
          f'{len(skipped)} skipped  |  {len(failed)} failed')
    if success:
        print('\nSucceeded:')
        for s in success:
            print(f'  {s}')
    if failed:
        print('\nFailed:')
        for f in failed:
            print(f'  {f}')
    if skipped:
        print('\nSkipped (already audited):')
        for s in skipped:
            print(f'  {s}')


if __name__ == '__main__':
    main()
