"""
log_to_sheets.py — Append a completed audit result to the Rite Water Voucher Audit Google Sheet.

Usage:
    python scripts/log_to_sheets.py <audit_json_path> [options]

Options:
    --sheet-id  ID  Google Sheet ID (from URL). If omitted, reads GOOGLE_SHEET_ID env var.
    --creds     FILE  Path to service-account JSON key file.
                      If omitted, reads GOOGLE_CREDS_FILE env var or looks for
                      'google_creds.json' next to this script.
    --dry-run       Print the row that would be appended without writing.

Sheet format (auto-created if tab is missing):
    Col A: Voucher No
    Col B: Employee Name
    Col C: Employee Code
    Col D: Date of Claim
    Col E: Date Processed (audit run date)
    Col F: Amount Claimed (INR)
    Col G: Amount Approved by PH (INR)
    Col H: Rite Approved Amount (INR)
    Col I: Recommended Hold (INR)
    Col J: Concerns (count)
    Col K: Audit PDF filename

Setup (one-time):
    1. Create a Google Cloud project and enable the Google Sheets API.
    2. Create a Service Account, download its JSON key, save as:
       D:\\Company Projects\\Voucher_Audit_Skill\\google_creds.json
    3. Share your Google Sheet with the service account email (Editor access).
    4. Set GOOGLE_SHEET_ID env var or pass --sheet-id.

Or use OAuth (simpler for personal use):
    gcloud auth application-default login
    Then the script will use application-default credentials automatically.
"""
import argparse, json, os, sys, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SKILL_ROOT  = SCRIPT_DIR.parent
TAB_NAME    = "Audit Log"
HEADERS     = [
    "Voucher No", "Employee Name", "Employee Code",
    "Date of Claim", "Date Processed",
    "Amount Claimed (INR)", "Approved by PH (INR)",
    "Rite Approved Amount (INR)", "Recommended Hold (INR)",
    "No. of Concerns", "Audit PDF Filename",
]


def _find_creds(creds_arg):
    """Return path to credentials file, or None to use application-default."""
    if creds_arg:
        p = Path(creds_arg)
        if not p.exists():
            print(f"[log_to_sheets] ERROR: creds file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return str(p)
    env = os.environ.get("GOOGLE_CREDS_FILE")
    if env and Path(env).exists():
        return env
    default = SKILL_ROOT / "google_creds.json"
    if default.exists():
        return str(default)
    return None  # fall back to application-default credentials


def _get_client(creds_path):
    try:
        import gspread
        from google.oauth2.service_account import Credentials as SACredentials
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:
        print("[log_to_sheets] Install gspread + google-auth: pip install gspread --break-system-packages", file=sys.stderr)
        sys.exit(1)

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    ]

    if creds_path:
        creds = SACredentials.from_service_account_file(creds_path, scopes=SCOPES)
        return gspread.authorize(creds)
    else:
        # Try application-default credentials (works after `gcloud auth application-default login`)
        try:
            import google.auth
            creds, _ = google.auth.default(scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception as e:
            print(f"[log_to_sheets] No credentials found. Either:\n"
                  f"  a) Save a service account JSON to {SKILL_ROOT}/google_creds.json\n"
                  f"  b) Run: gcloud auth application-default login\n"
                  f"  Error: {e}", file=sys.stderr)
            sys.exit(1)


def _ensure_tab(sheet, tab_name):
    """Return worksheet, creating it with headers if it doesn't exist."""
    import gspread
    try:
        ws = sheet.worksheet(tab_name)
        # Check if headers are present
        row1 = ws.row_values(1)
        if not row1:
            ws.append_row(HEADERS, value_input_option="USER_ENTERED")
        return ws
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS) + 2)
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")
        return ws


def build_row(audit: dict) -> list:
    v = audit.get("voucher", {})
    t = audit.get("totals", {})
    findings = audit.get("findings", [])

    voucher_no     = v.get("voucher_no", "")
    emp_name       = v.get("employee_name", "")
    emp_code       = v.get("employee_code", "")
    claim_date     = v.get("voucher_date") or v.get("date") or ""
    processed_date = datetime.date.today().isoformat()

    claimed   = t.get("gross_claimed", 0) or 0
    ph_approv = t.get("reviewer_approved", 0) or 0
    eligible  = t.get("policy_eligible", 0) or 0
    hold      = t.get("recommended_hold", 0) or 0
    concerns  = len([f for f in findings if f.get("severity") in ("HIGH", "MEDIUM")])

    pdf_name = audit.get("pdf_path") or audit.get("pdf_filename") or ""
    if pdf_name:
        pdf_name = Path(pdf_name).name

    return [
        str(voucher_no),
        emp_name,
        emp_code,
        str(claim_date),
        processed_date,
        round(float(claimed), 2),
        round(float(ph_approv), 2),
        round(float(eligible), 2),
        round(float(hold), 2),
        concerns,
        pdf_name,
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audit_json", help="Path to the audit_voucher_XXXX.json output file")
    ap.add_argument("--sheet-id", default=None, help="Google Sheet ID")
    ap.add_argument("--creds",    default=None, help="Path to service-account JSON key")
    ap.add_argument("--dry-run",  action="store_true", help="Print row without writing")
    args = ap.parse_args()

    # Load audit JSON
    audit_path = Path(args.audit_json)
    if not audit_path.exists():
        print(f"[log_to_sheets] ERROR: file not found: {audit_path}", file=sys.stderr)
        sys.exit(1)
    with open(audit_path, encoding="utf-8") as f:
        audit = json.load(f)

    row = build_row(audit)

    if args.dry_run:
        print("DRY RUN — row that would be appended:")
        for h, v in zip(HEADERS, row):
            print(f"  {h:<30}: {v}")
        return

    # Get sheet ID
    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        print("[log_to_sheets] ERROR: provide --sheet-id or set GOOGLE_SHEET_ID env var", file=sys.stderr)
        sys.exit(1)

    creds_path = _find_creds(args.creds)
    gc = _get_client(creds_path)

    try:
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        print(f"[log_to_sheets] Cannot open sheet {sheet_id}: {e}", file=sys.stderr)
        sys.exit(1)

    ws = _ensure_tab(sh, TAB_NAME)
    ws.append_row(row, value_input_option="USER_ENTERED")

    v = audit.get("voucher", {})
    print(f"[log_to_sheets] Logged voucher #{v.get('voucher_no')} ({v.get('employee_name')}) to sheet.")


if __name__ == "__main__":
    main()
