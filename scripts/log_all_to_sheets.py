"""
log_all_to_sheets.py — Push all audit_voucher_*.json files to Google Sheets.
Self-contained: no dependency on log_to_sheets.py.
"""
import argparse, json, os, sys, datetime
from pathlib import Path

SHEET_ID   = "1aBIdbOrtYRaIOhskeOk3Rb59MLce8ixjnHr9UDkZl_k"
TAB_NAME   = "Audit Log"
HEADERS    = [
    "Voucher No", "Employee Name", "Employee Code",
    "Date of Claim", "Date Processed",
    "Amount Claimed (INR)", "Approved by PH (INR)",
    "Rite Approved Amount (INR)", "Recommended Hold (INR)",
    "No. of Concerns", "Audit PDF Filename",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_client(creds_path):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("ERROR: Missing packages. Run: pip install gspread google-auth")
        sys.exit(1)
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


def ensure_tab(spreadsheet, tab_name):
    import gspread
    try:
        ws = spreadsheet.worksheet(tab_name)
        if not ws.row_values(1):
            ws.append_row(HEADERS, value_input_option="USER_ENTERED")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS)+2)
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")
    return ws


def build_row(audit):
    v = audit.get("voucher", {})
    t = audit.get("totals", {})
    findings = audit.get("findings", [])
    pdf = audit.get("pdf_path") or ""
    if pdf:
        pdf = Path(pdf).name
    return [
        str(v.get("voucher_no", "")),
        v.get("employee_name", ""),
        v.get("employee_code", ""),
        str(v.get("voucher_date") or v.get("date", "")),
        datetime.date.today().isoformat(),
        round(float(t.get("gross_claimed", 0) or 0), 2),
        round(float(t.get("reviewer_approved", 0) or 0), 2),
        round(float(t.get("policy_eligible", 0) or 0), 2),
        round(float(t.get("recommended_hold", 0) or 0), 2),
        len([f for f in findings if f.get("severity") in ("HIGH", "MEDIUM")]),
        pdf,
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", default=SHEET_ID)
    ap.add_argument("--creds",    default=None)
    ap.add_argument("--out-dir",  default=None)
    ap.add_argument("--dry-run",  action="store_true")
    args = ap.parse_args()

    # Locate output dir
    script_dir = Path(__file__).parent
    skill_root = script_dir.parent
    out_dir = Path(args.out_dir) if args.out_dir else skill_root / "output"
    jsons = sorted(out_dir.glob("audit_voucher_*.json"))
    if not jsons:
        print(f"No audit_voucher_*.json files found in {out_dir}")
        sys.exit(0)
    print(f"Found {len(jsons)} audit file(s) in {out_dir}")

    if args.dry_run:
        for j in jsons:
            audit = json.loads(j.read_text(encoding="utf-8"))
            row = build_row(audit)
            v = audit.get("voucher", {})
            print(f"  Would log: #{row[0]} {row[1]:<25} Claimed {row[5]:>9,.0f}  Eligible {row[7]:>9,.0f}  Hold {row[8]:>6,.0f}")
        return

    # Locate creds
    creds_path = args.creds
    if not creds_path:
        creds_path = str(skill_root / "google_creds.json")
    if not Path(creds_path).exists():
        print(f"ERROR: Credentials file not found: {creds_path}")
        sys.exit(1)

    sheet_id = args.sheet_id
    print(f"Connecting to Google Sheet: {sheet_id}")
    gc = get_client(creds_path)
    try:
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        print(f"ERROR: Cannot open sheet — {e}")
        print("Make sure you have shared the sheet with:")
        creds_data = json.loads(Path(creds_path).read_text())
        print(f"  {creds_data.get('client_email','<service account email>')}")
        sys.exit(1)

    ws = ensure_tab(sh, TAB_NAME)

    # Read existing voucher numbers to skip duplicates
    existing = set()
    try:
        all_rows = ws.get_all_values()
        for r in all_rows[1:]:
            if r and r[0].strip():
                existing.add(r[0].strip())
    except Exception:
        pass

    logged = skipped = errors = 0
    for j in jsons:
        try:
            audit = json.loads(j.read_text(encoding="utf-8"))
            v = audit.get("voucher", {})
            vno = str(v.get("voucher_no", "")).strip()
            if vno in existing:
                print(f"  SKIP  #{vno} ({v.get('employee_name','')}) — already in sheet")
                skipped += 1
                continue
            row = build_row(audit)
            ws.append_row(row, value_input_option="USER_ENTERED")
            existing.add(vno)
            print(f"  ADDED #{vno} — {v.get('employee_name','')} | Eligible Rs.{row[7]:,.0f} | Hold Rs.{row[8]:,.0f}")
            logged += 1
        except Exception as e:
            print(f"  ERROR {j.name}: {e}", file=sys.stderr)
            errors += 1

    print()
    print(f"Done — Added: {logged}, Skipped (already logged): {skipped}, Errors: {errors}")


if __name__ == "__main__":
    main()
