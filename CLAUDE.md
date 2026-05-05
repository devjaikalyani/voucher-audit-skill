# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Install dependencies
```bash
pip install pdfplumber reportlab pytesseract Pillow anthropic
# Tesseract OCR binary (optional — used only if Claude vision OCR is not available):
# Windows: https://github.com/UB-Mannheim/tesseract/wiki
# Ubuntu: sudo apt install tesseract-ocr
```

### API key
Set `ANTHROPIC_API_KEY` in your environment, or place it in a `.env.shared` file one level
above this repo (i.e. the parent folder of `Voucher_Audit_Skill/`).
`ocr_proofs_claude.py` and `spine_hr_browser.py` load it automatically — no manual export needed.

SpineHR credentials (`SPINEHR_URL`, `SPINEHR_USERNAME`, `SPINEHR_PASSWORD`) follow the same
`.env.shared` convention.

### Run an audit (normal path)
```bash
cd "D:\Company Projects\Voucher_Audit_Skill"
python scripts/run_audit.py path/to/voucher.pdf path/to/proofs.zip
# Output lands in output/ by default; override with --out-dir
```

### Run individual pipeline stages (for debugging)
```bash
python scripts/extract_voucher.py voucher.pdf               # -> JSON to stdout
python scripts/extract_proofs.py  proofs.zip                # -> JSON to stdout
python scripts/audit_engine.py    voucher.pdf proofs.zip --out audit.json
python scripts/generate_audit_pdf.py audit.json --out-dir output/
```

### Log an admin decision (learning loop)
```bash
python scripts/log_decision.py output/audit_voucher_3583.json \
    --final-amount 12000 \
    --note "Reduced hotel to Grade-C VP/GM cap" \
    --per-line "Hotel=6000" --per-line "Food Allowance=750"
```

## Architecture

The skill is a sequential, file-based pipeline with no web server or framework:

```
voucher.pdf + proofs.zip
        │
        ▼
extract_voucher.py   — pdfplumber + regex → structured voucher dict
extract_proofs.py    — ZIP → per-file OCR/text extraction → proof index with
                       fingerprints for duplicate detection
        │
        ▼
audit_engine.py      — loads policy_rules.json + employee_master.csv +
                       history/decisions/*.json, runs evaluate_line() for each
                       line item, runs voucher_level_checks(), returns audit dict
        │
        ▼
generate_audit_pdf.py — ReportLab Platypus → 9-section A4 PDF
        │
        ▼
output/audit_voucher_<N>.json
output/PolicyBased_AuditReport_Voucher<N>_<Name>_<Code>.pdf
```

`run_audit.py` is the one-shot wrapper that calls `audit_engine.run_audit()` then `generate_audit_pdf.render_pdf()`.

### Key data contracts

**Voucher dict** (from `extract_voucher.py`): `voucher_no`, `employee_name`, `employee_code`, `period_from/to` (ISO dates), `narration`, `cost_center`, `gross_claimed`, `gross_approved_by_reviewer`, `gross_rejected`, `net_payable`, `line_items[]`.

Each `line_item`: `date` (ISO), `expense_head` (canonical label), `expense_head_raw`, `remarks`, `claimed_inr`, `approved_by_reviewer_inr`, `rejected_inr`.

**Proof record** (from `extract_proofs.py`): `file_name`, `kind`, `ride_id`, `invoice_no`, `txn_id`, `pnr`, `gstin`, `amounts[]`, `amount` (max), `dates[]`, `vendor`, `fingerprint`, `duplicate_of`.

**Audit findings dict** (from `audit_engine.py`): top-level keys `audit_metadata`, `voucher`, `employee`, `city_grade`, `proofs`, `findings[]`, `breaches[]`, `history_notes[]`, `totals`, `recommendations[]`, `conclusion`. This dict is both written to JSON and passed directly to `render_pdf()`.

### Policy and designation logic

- `assets/policy_rules.json` is the source of truth. It encodes city grades (A/B/C), seven designation buckets (`DIRECTOR_CFO` → `TECHNICIAN`) with hotel/food/travel-class caps, petrol rates, hospitality rules, and global flags.
- `assets/employee_master.csv` maps employee codes to designation text. Columns: `Sr,Code,Name,Branch,Department,Designation`.
- Designation text is matched to a bucket via alias substrings in `audit_engine.map_designation()`; unmatched defaults to `SR_EXECUTIVE`.
- City is derived from the voucher narration/cost center via a hardcoded city list; unknown cities default to Grade C (most restrictive).

### Expense head canonicalization

`extract_voucher.classify_head()` maps raw expense head text + remarks to a fixed set of canonical labels (`Hotel`, `Food Allowance`, `1AC Train`, `2AC Train`, `3AC Train`, `Sleeper Train`, `Train`, `Bus`, `Flight`, `Cab`, `Auto`, `Cruise`, `Petrol`, `Toll`, `Parking`, `Site Expense`, `Vehicle Maintenance`, `Other Expense`, `Office Expense`). The audit engine's `evaluate_line()` switch is keyed on these labels — adding a new category requires updating both `HEAD_PATTERNS` in `extract_voucher.py` and the corresponding branch in `evaluate_line()`.

### Learning loop

After admin finalizes a voucher, `log_decision.py` writes `history/decisions/<voucher_no>.json`. On the next audit run, `audit_engine.history_patterns()` scans all decision files and surfaces past approved amounts for the same employee + expense head in the `history_notes` field of the audit dict, which then appears in the PDF's Conclusion section.

### Line-item proof matching

`evaluate_line()` scores each proof against the line item by date proximity (±30 days = +1, exact = +2) and amount proximity (±2% = +3). A proof needs score ≥ 4 for `has_strong_proof`; score ≥ 2 for `has_some_proof`. Proof status is `OK / WEAK / MISSING` depending on these thresholds and drives `APPROVE / CONDITIONAL / REJECT` statuses.

## Updating policy or employee data

- **Policy change**: Edit `assets/policy_rules.json`. Update `references/policy_full.md` in sync (JSON is the engine source of truth; markdown is human reference only).
- **Employee master refresh**: Replace `assets/employee_master.csv` with a new Spine HR export keeping the same column names (`Sr,Code,Name,Branch,Department,Designation`).
- **New expense category**: Add a regex to `HEAD_PATTERNS` in `extract_voucher.py`, then add an `elif head == '<NewLabel>':` block in `audit_engine.evaluate_line()`.
