---
name: voucher-audit
description: Audit employee expense vouchers (Spine HR PDFs) against your company's Travel & Reimbursement Policy and a ZIP of supporting proof documents, cross-checking petrol / two-wheeler claims against Unolo GPS data. Use this skill whenever the user uploads or refers to a Spine HR voucher and proofs, mentions reviewing or auditing employee claims, mentions a Voucher No. / employee code / "claim review" / "in-process claims" / "policy check on a claim", asks for a Policy-Based Expense Audit Report PDF, or wants to schedule a daily auto-fetch of pending claims from Spine HR. Trigger this skill even if the user does not explicitly say "audit" — any input pairing of a voucher PDF and proofs (zip / loose images) is in scope. The output is a multi-section audit PDF, plus an updated processed_vouchers state file so the same claim is never audited twice.
---

# Voucher Audit Skill

This skill produces a policy-based second opinion on an employee expense voucher,
designed to be used by admin or finance staff who issue the final reimbursement verdict.

A voucher comes from Spine HR; proofs come as a ZIP of receipts (PDFs and phone
photos / payment screenshots); GPS distance comes from the monthly Unolo Excel export.

The skill:

1. Parses the voucher header (Voucher No., employee code, narration, dates) and each
   line item (Expense Head, Date, Remarks, Claimed, Approved, Rejected).
2. Validates employee identity: looks up the employee code in the bundled employee
   master CSV and confirms the name on the voucher matches. Mismatches are surfaced
   in the report.
3. Indexes every proof document using Claude Vision OCR (tool-use structured extraction)
   or Tesseract as fallback — extracting ride IDs, invoice numbers, PNRs, txn IDs,
   GSTINs, vehicle numbers, amounts, and dates.
4. Loads Unolo GPS data for the claim period from
   `assets/unolo_data/<month>_<year>.csv` and uses it to verify two-wheeler / petrol
   claims (km × per-km rate from policy). When Unolo data is unavailable the audit
   falls back to claimed amounts but flags MEDIUM severity for manual verification.
5. Applies the Travel & Reimbursement Policy (encoded in `assets/policy_rules.json`):
   designation buckets, city grades, hotel/food caps, train class entitlements,
   hospitality rules, period limits, approval thresholds, and duplicate detection.
6. Runs a Claude Sonnet AI reasoning layer that cross-checks UPI screenshots against
   reference bills, splits group bills by number of employees named on the receipt,
   and produces an overall verdict (APPROVE / APPROVE_WITH_DEDUCTIONS / HOLD /
   REJECT) with confidence and per-line reasoning.
7. Records observations in a structured JSON, then renders a 9-section audit PDF
   using ReportLab. Filename format:
   `PolicyBased_AuditReport_Voucher<N>_<EmployeeName>_<Code>.pdf`
8. Logs the voucher fingerprint to `history/processed_vouchers.json` so the same
   `(employee_code, voucher_no, voucher_date)` is never audited twice.

---

## Three ways to run the skill

### A. One-shot manual audit
```bash
cd /path/to/Voucher_Audit_Skill
python scripts/run_audit.py path/to/voucher.pdf path/to/proofs.zip
```

### B. Daily scheduled auto-fetch (recommended for production)

1. Add credentials to a `.env.shared` file one level above this repo:
   ```
   SPINEHR_URL=https://<your-spine-hr-host>/...
   SPINEHR_USERNAME=<admin-user>
   SPINEHR_PASSWORD=<admin-password>
   ANTHROPIC_API_KEY=<your-anthropic-api-key>
   ```
2. Install dependencies:
   ```bash
   pip install anthropic selenium webdriver-manager PyMuPDF Pillow reportlab pdfplumber
   ```
3. Register the Windows scheduled task (elevated PowerShell):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
   ```
   This registers `VoucherAudit_DailyRun` to fire at 12:00 PM daily.

4. (One-time) Tune SpineHR selectors by capturing the live DOM:
   ```bash
   python scripts/spine_hr_browser.py --inspect
   ```
   Open the generated `history/spinehr_dump_<date>.html`, find the correct CSS /
   XPath for your SpineHR version, and update the `SELECTORS` dict in
   `scripts/spine_hr_browser.py`.

### C. Trigger on demand
```bash
python scripts/daily_run.py --show-browser
```
Or on Windows: `scripts\run_now.bat`

---

## How the daily run works

`scripts/daily_run.py` performs one full pass:

1. Logs into Spine HR and opens the **In-Process Claims** tab.
2. For each claim **not** already in `history/processed_vouchers.json`:
   - Downloads the voucher PDF + attached proof ZIP into
     `in_process_claims/<EMP_CODE>_<VOUCHER_NO>/`.
3. For each newly downloaded claim:
   - Runs the full audit pipeline (parse → OCR → policy check → Unolo cross-check
     → AI verdict → PDF render).
   - Writes the audit PDF and JSON to `output/`.
   - Appends a fingerprint to the processed list so it won't run again.
4. Exits. The task scheduler calls the script once per day; it processes every
   pending claim that day before terminating.

---

## Policy & employee data setup

### `assets/policy_rules.json`
Encodes your company's travel policy: city grades (A/B/C), designation buckets with
hotel/food/travel-class limits, petrol rates (bike Rs/km, car Rs/km), proof
requirements, and audit severity levels.

Copy `assets/policy_rules.example.json` to `assets/policy_rules.json` and fill in
your company's actual limits before running.

### `assets/employee_master.csv`
Maps employee codes to names and designations.
Required columns: `Sr, Code, Name, Branch, Department, Designation`

Export from your HR system and drop here. Refresh monthly.

### `assets/unolo_data/<month>_<year>.csv`
Monthly GPS distance report from Unolo, one row per employee per day.
Required columns:
`employee_label, employee_code, date, designation, team, total_distance_km, odometer_distance_km`

Naming convention: `april_2026.csv`, `may_2026.csv`, etc.
The `T-` prefix in Unolo's internal employee IDs is stripped automatically.

---

## File reference

| File | Purpose |
|---|---|
| `assets/policy_rules.json` | Travel policy rules (your config) |
| `assets/policy_rules.example.json` | Template — copy and fill in |
| `assets/employee_master.csv` | Employee code → name → designation |
| `assets/unolo_data/<month>_<year>.csv` | GPS km data per employee per day |
| `scripts/run_audit.py` | One-shot audit wrapper |
| `scripts/daily_run.py` | Daily orchestrator |
| `scripts/spine_hr_browser.py` | Selenium Spine HR fetcher |
| `scripts/audit_engine.py` | Policy reasoning + Unolo cross-check |
| `scripts/extract_voucher.py` | Spine HR voucher PDF parser |
| `scripts/ocr_proofs_claude.py` | Claude Vision OCR (tool use + Batch API) |
| `scripts/extract_proofs.py` | Tesseract fallback OCR |
| `scripts/extract_document.py` | Vision parser for non-Spine PDFs |
| `scripts/generate_audit_pdf.py` | 9-section ReportLab PDF renderer |
| `scripts/unolo_loader.py` | GPS distance lookup helpers |
| `scripts/state_store.py` | `processed_vouchers.json` wrapper |
| `scripts/log_decision.py` | Records admin's final verdict for learning loop |
| `scripts/install_scheduled_task.ps1` | Windows Task Scheduler installer |
| `scripts/run_now.bat` | Manual trigger (Windows) |
| `references/policy_full.md` | Human-readable policy reference |
| `references/report_format.md` | Audit PDF section structure |
| `references/learning_loop.md` | Admin decision feedback loop |
| `examples/sample_audit_input.json` | Fictional sample — input schema reference |
| `examples/sample_audit_report.pdf` | Fictional sample — example output PDF |
| `history/processed_vouchers.json` | Dedup state (gitignored) |
| `in_process_claims/` | Downloaded voucher folders (gitignored) |
| `output/` | Generated audit JSON + PDFs (gitignored) |

---

## Identity validation

Every audit validates three identifiers in sequence:

1. **Employee Code** — looked up in `employee_master.csv`. Unknown codes are flagged
   HIGH severity.
2. **Employee Name** — voucher name vs. master name, compared after whitespace and
   case normalization. Token overlap is recorded so admin can judge edge cases.
3. **Voucher Fingerprint** — `<code>__voucher<no>__<voucher_date>` keyed in
   `processed_vouchers.json`. The same fingerprint is never audited twice.

---

## Adapting to a different HR or GPS system

- **Different HR system:** Replace `scripts/spine_hr_browser.py` with a fetcher for
  your system. The output it must produce is a folder per claim containing a voucher
  PDF and a proofs ZIP — the rest of the pipeline is system-agnostic.
- **Different GPS system:** Replace `scripts/unolo_loader.py`. It must expose
  `load_unolo()`, `km_for(data, code, date_from, date_to)`, and
  `days_active(data, code, date_from, date_to)`. The audit engine imports these
  three functions only.
- **Different policy:** Edit `assets/policy_rules.json`. Designation buckets,
  city grades, per-km rates, and caps are all data-driven — no code changes needed
  for most policy updates.
