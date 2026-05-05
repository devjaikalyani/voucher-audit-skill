---
name: voucher-audit
description: Audit Rite Water Solutions employee expense vouchers (Spine HR PDFs) against the Tour & Travel Policy and a ZIP of supporting proof documents, cross-checking petrol / two-wheeler claims against Unolo GPS data. Use this skill whenever the user uploads or refers to a Spine HR voucher and proofs, mentions reviewing/auditing employee claims, mentions Voucher No. / RWSIPL employee codes / "claim review" / "in-process claims" / "policy check on a claim", asks for a Policy-Based Expense Audit Report PDF, or wants to schedule a daily auto-fetch of pending claims from Spine HR. Trigger this skill even if the user does not explicitly say "audit" - any input pairing of a Rite Water voucher PDF + proofs (zip / loose images) is in scope. The output is a multi-section audit PDF matching the format used by Rite Water's Internal Audit Division, plus an updated processed_vouchers state file so the same claim is never audited twice.
---

# Voucher Audit Skill

This skill produces a stronger second opinion on an expense voucher than the
project head's review, and is designed to be used by admin/finance staff who
issue the final verdict.

A voucher comes from Spine HR; proofs come as a ZIP of receipts (PDFs and
phone photos / payment screenshots); GPS distance comes from the monthly
Unolo Excel export. The skill:

1. Parses the voucher header (Voucher No., employee code, narration, dates)
   and each line item (Expense Head, Date, Remarks, Claimed, Approved,
   Rejected).
2. Validates the employee identity: looks up the voucher's employee code in
   the bundled employee master CSV and confirms the voucher's employee name
   matches. Mismatches are surfaced in the report.
3. Indexes every proof document (Claude Vision OCR if available, otherwise
   Tesseract) - extracting ride IDs, invoice numbers, PNRs, txn IDs, GSTINs,
   vehicle numbers, amounts, dates.
4. Loads Unolo GPS data for the claim period from
   `assets/unolo_data/<month>_<year>.csv` and uses it to verify Two-Wheeler
   / Petrol claims (km x Rs.3 bike, Rs.9 car). When Unolo data isn't
   available the audit falls back to claimed amounts but flags MEDIUM
   severity so admin manually verifies.
5. Applies the Tour & Travel Policy (encoded in
   `assets/policy_rules.json`): designation buckets, city grades, hotel/food
   caps, train class entitlements, hospitality rules, period & threshold
   limits, duplicate detection.
6. Records observations in a structured JSON, then renders a 9-section audit
   PDF using ReportLab. Filename format
   `PolicyBased_AuditReport_Voucher<N>_<EmployeeName>_<Code>.pdf`.
7. Logs the voucher fingerprint to `history/processed_vouchers.json` so the
   same `(employee_code, voucher_no, voucher_date)` is never audited twice.

## Three ways to run the skill

### A. One-shot manual audit
```
cd D:\Company Projects\Voucher_Audit_Skill
python scripts\run_audit.py <voucher.pdf> <proofs.zip>
```

### B. Daily scheduled auto-fetch (recommended for production)
1. Add SpineHR creds to `D:\Company Projects\.env.shared`:
   ```
   SPINEHR_URL=https://<your-spine-hr-host>/...
   SPINEHR_USERNAME=<admin-user>
   SPINEHR_PASSWORD=<admin-password>
   ANTHROPIC_API_KEY=<existing key>
   ```
2. Install Selenium/Chrome deps once:
   ```
   pip install anthropic selenium webdriver-manager PyMuPDF Pillow
   ```
3. Register the Windows scheduled task (run from elevated PowerShell):
   ```
   powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
   ```
   This registers `VoucherAudit_DailyRun` to fire at 12:00 PM daily, only when
   the PC is on, and only run once per day.
4. (One-time selector tuning) Run with `--inspect` to dump the SpineHR DOM
   so the right CSS selectors can be plugged into
   `scripts/spine_hr_browser.py` SELECTORS dict:
   ```
   python scripts\spine_hr_browser.py --inspect --show
   ```

### C. Trigger today's pass on demand
```
scripts\run_now.bat
```
or `python scripts\daily_run.py --show-browser` to see Chrome.

## How the daily run behaves

`scripts/daily_run.py` performs one full pass:

1. Logs into SpineHR, opens **In-Process Claims**.
2. For each claim NOT already in `history/processed_vouchers.json`:
   - downloads the voucher PDF + attached proofs into
     `in_process_claims/<EmployeeName>_<VoucherNo>/`.
3. For each newly-staged claim folder:
   - runs the audit pipeline (voucher parse + proofs OCR + policy + Unolo
     cross-check + PDF render).
   - writes the audit PDF to `output/`.
   - appends a fingerprint to the processed list so it won't run again.
4. Writes a per-day log to `history/daily_runs/run_YYYY-MM-DD.log` and
   exits.

The single-pass design means the task scheduler only needs to fire the
script once at 12:00 PM and the script handles every available claim that
day before terminating.

## Files in this skill

- `assets/policy_rules.json` - encoded Tour & Travel Policy
- `assets/employee_master.csv` - 545 employees, code -> name -> designation
- `assets/unolo_data/<month>_<year>.csv` - GPS distance per employee per day
- `scripts/run_audit.py` - one-shot wrapper
- `scripts/daily_run.py` - 12:00 PM orchestrator
- `scripts/spine_hr_browser.py` - Selenium SpineHR fetcher
- `scripts/audit_engine.py` - policy reasoning, identity validation, Unolo check
- `scripts/extract_voucher.py` - Spine HR PDF parser
- `scripts/extract_proofs.py` - tesseract fallback OCR
- `scripts/ocr_proofs_claude.py` - Claude Vision OCR (recommended)
- `scripts/extract_document.py` - vision parser for non-Spine PDFs
- `scripts/generate_audit_pdf.py` - 9-section audit PDF renderer
- `scripts/unolo_loader.py` - GPS distance lookup
- `scripts/state_store.py` - processed_vouchers.json wrapper
- `scripts/log_decision.py` - records admin's final verdict for learning
- `scripts/install_scheduled_task.ps1` - Windows Task Scheduler installer
- `scripts/run_now.bat` - manual trigger
- `references/policy_full.md` - readable Tour & Travel Policy
- `references/report_format.md` - audit PDF section structure
- `references/learning_loop.md` - admin decision feedback loop
- `history/processed_vouchers.json` - dedup state
- `history/decisions/` - past admin verdicts
- `history/daily_runs/` - daily run logs
- `in_process_claims/` - downloaded voucher folders awaiting audit
- `output/` - generated audit JSON + PDFs
- `examples/` - sample input/output

## Identity matching guarantee

Every audit run validates THREE identifiers in sequence:

1. **Employee Code** (e.g. `RWSIPL469`) - the voucher's code is looked up in
   the master CSV. Missing or unknown codes are flagged HIGH severity.
2. **Employee Name** - voucher name vs master name compared after whitespace
   and case normalization. Mismatch (or partial match) is recorded with the
   token overlap so admin can decide.
3. **Voucher Fingerprint** - `<code>__voucher<no>__<voucher_date>` is the
   key used by `processed_vouchers.json`. The same fingerprint is never
   audited twice.

## Updating monthly Unolo data

Drop the new month's Unolo report into `assets/unolo_data/` named
`<month>_<year>.csv` (lowercase, e.g. `may_2026.csv`). Use any consistent
CSV columns; the loader expects:
`employee_label, employee_code, date, designation, team,
total_distance_km, odometer_distance_km`. The `T-` prefix in Unolo's
internal IDs is stripped automatically to match the master CSV.

If the file isn't present, the audit engine still runs - petrol/two-wheeler
items are flagged CONDITIONAL/MEDIUM with the note "manually verify km
claimed".
