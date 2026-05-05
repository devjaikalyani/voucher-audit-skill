"""
Daily orchestrator.

Wakes up (typically via Windows Task Scheduler at 12:00 PM), pulls fresh
in-process claims from SpineHR, runs the audit pipeline on each one we
haven't seen before, writes audit PDFs into output/, and updates
history/processed_vouchers.json so we never run the same voucher twice.

Single-pass design: it processes everything available *that day* and exits.
The next day's run picks up the next batch of in-process claims.

Usage:
    python daily_run.py [--show-browser] [--limit 100] [--skip-fetch]

  --show-browser  : run Chrome visibly (handy on first install)
  --skip-fetch    : skip the SpineHR fetch and just audit any pre-staged
                    folders inside in_process_claims/
  --limit N       : cap how many claims this run audits (default 100)
"""
import os, sys, json, time, argparse, datetime, traceback
from pathlib import Path

SKILL_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(SKILL_ROOT / 'scripts'))

from audit_engine import run_audit
from generate_audit_pdf import render_pdf, output_filename
import state_store


IN_PROCESS_DIR = SKILL_ROOT / 'in_process_claims'
OUTPUT_DIR     = SKILL_ROOT / 'output'
LOG_DIR        = SKILL_ROOT / 'history' / 'daily_runs'


def log(message, log_file=None):
    line = f'[{datetime.datetime.now().isoformat(timespec="seconds")}] {message}'
    print(line, flush=True)
    if log_file:
        log_file.write(line + '\n'); log_file.flush()


def discover_claim_folders():
    """Each subfolder in in_process_claims/ should be a single claim:
        EMPLOYEENAME_VOUCHERNO/
            voucher.pdf            (or *.pdf)
            proofs/                (folder of receipt files OR *.zip)
    """
    if not IN_PROCESS_DIR.exists():
        return []
    out = []
    for d in sorted(IN_PROCESS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith('_'):
            continue
        # find voucher pdf
        pdfs = list(d.glob('*.pdf'))
        if not pdfs:
            continue
        voucher_pdf = pdfs[0]
        # find proofs zip OR proofs folder
        zips = list(d.glob('*.zip'))
        proofs_zip = zips[0] if zips else None
        proofs_dir = d / 'proofs'
        if not proofs_zip and proofs_dir.exists():
            # zip the folder on the fly
            import zipfile
            tmp_zip = d / 'proofs_auto.zip'
            if not tmp_zip.exists():
                with zipfile.ZipFile(tmp_zip, 'w') as zf:
                    for p in proofs_dir.rglob('*'):
                        if p.is_file():
                            zf.write(p, p.relative_to(proofs_dir))
            proofs_zip = tmp_zip
        out.append({
            'folder': d,
            'voucher_pdf': voucher_pdf,
            'proofs_zip': proofs_zip,
        })
    return out


def audit_one(claim, log_file):
    """Run the audit pipeline for one staged claim. Returns the state-store
    entry that was added, or None if skipped/failed."""
    voucher_pdf = str(claim['voucher_pdf'])
    proofs_zip = str(claim['proofs_zip']) if claim['proofs_zip'] else None
    log(f'  auditing {claim["folder"].name}', log_file)
    try:
        result = run_audit(voucher_pdf, proofs_zip)
    except Exception as e:
        log(f'    AUDIT FAILED: {e}', log_file)
        traceback.print_exc()
        return None

    voucher = result.get('voucher', {})
    voucher_no = voucher.get('voucher_no')
    employee_code = voucher.get('employee_code')
    voucher_date = voucher.get('voucher_date')
    employee_name = (voucher.get('employee_name') or
                     result.get('employee', {}).get('name_master', ''))

    if state_store.already_processed(voucher_no, employee_code, voucher_date):
        log(f'    SKIP: voucher #{voucher_no} for {employee_name} already in state', log_file)
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_out = OUTPUT_DIR / f'audit_voucher_{voucher_no}.json'
    with open(json_out, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

    pdf_out = OUTPUT_DIR / output_filename(result)
    render_pdf(result, str(pdf_out))
    log(f'    OK -> {pdf_out.name}', log_file)

    return state_store.mark_processed(
        voucher_no=voucher_no,
        employee_code=employee_code,
        employee_name=employee_name,
        voucher_date=voucher_date,
        voucher_pdf_path=voucher_pdf,
        proofs_path=proofs_zip,
        audit_pdf_path=str(pdf_out),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--show-browser', action='store_true')
    ap.add_argument('--skip-fetch', action='store_true')
    ap.add_argument('--limit', type=int, default=100)
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f'run_{datetime.date.today().isoformat()}.log'
    log_file = open(log_path, 'a', encoding='utf-8')
    log(f'=== Daily run started ({"with" if not args.skip_fetch else "without"} SpineHR fetch) ===',
        log_file)

    if not args.skip_fetch:
        try:
            from spine_hr_browser import fetch_in_process_claims
            log('Fetching new in-process claims from SpineHR...', log_file)
            new_claims = fetch_in_process_claims(headless=not args.show_browser,
                                                  max_claims=args.limit)
            log(f'  downloaded {len(new_claims)} new claim folder(s)', log_file)
        except Exception as e:
            log(f'SpineHR fetch failed: {e} -- continuing with already-staged folders',
                log_file)

    claims = discover_claim_folders()
    log(f'Discovered {len(claims)} claim folder(s) under {IN_PROCESS_DIR}', log_file)

    audited = 0
    for claim in claims[:args.limit]:
        if audit_one(claim, log_file):
            audited += 1

    log(f'=== Daily run complete: {audited} new audit(s) generated ===', log_file)
    log_file.close()


if __name__ == '__main__':
    main()
