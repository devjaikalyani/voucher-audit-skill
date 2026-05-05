# Voucher Audit Skill

Audits Rite Water Solutions Spine HR expense vouchers against the Tour &
Travel Policy and a ZIP of supporting proofs, cross-checking petrol /
two-wheeler claims against Unolo GPS data. Designed for admin/finance staff
who issue the final verdict.

## Folder layout

```
Voucher_Audit_Skill/
|-- SKILL.md
|-- README.md
|-- requirements.txt
|-- assets/
|   |-- policy_rules.json         encoded Tour & Travel Policy
|   |-- employee_master.csv       545 employees -> code/name/designation
|   `-- unolo_data/
|       `-- april_2026.csv        per-employee per-day km log
|-- scripts/
|   |-- run_audit.py              one-shot manual audit
|   |-- daily_run.py              12:00 PM scheduled orchestrator
|   |-- spine_hr_browser.py       Selenium SpineHR fetcher
|   |-- audit_engine.py           policy + identity + Unolo reasoning
|   |-- extract_voucher.py        Spine HR voucher parser
|   |-- extract_proofs.py         tesseract fallback OCR
|   |-- ocr_proofs_claude.py      Claude Vision OCR (recommended)
|   |-- extract_document.py       vision parser for non-Spine PDFs
|   |-- generate_audit_pdf.py     9-section audit PDF
|   |-- unolo_loader.py           GPS distance lookup
|   |-- state_store.py            processed_vouchers.json wrapper
|   |-- log_decision.py           record admin's final verdict
|   |-- install_scheduled_task.ps1   Windows Task installer
|   `-- run_now.bat               manual trigger
|-- references/                   policy / format / learning-loop docs
|-- history/
|   |-- processed_vouchers.json   dedup state
|   |-- decisions/                admin's past verdicts
|   `-- daily_runs/               per-day execution logs
|-- in_process_claims/            downloaded vouchers awaiting audit
|-- output/                       generated audit JSON + PDFs
`-- examples/                     sample input/output
```

## Quick install

```powershell
cd "D:\Company Projects\Voucher_Audit_Skill"
pip install anthropic selenium webdriver-manager PyMuPDF Pillow pdfplumber reportlab
```

## Configure SpineHR credentials

Add to `D:\Company Projects\.env.shared`:
```
ANTHROPIC_API_KEY=sk-ant-...
SPINEHR_URL=https://<your-spinehr-host>/...
SPINEHR_USERNAME=<admin user>
SPINEHR_PASSWORD=<admin pass>
```

## Tune the SpineHR DOM selectors

```powershell
python scripts\spine_hr_browser.py --inspect --show
```
Look at the printed HTML and update the `SELECTORS` dict at the top of
`scripts\spine_hr_browser.py` with the right CSS for the login fields,
claims menu, In-Process tab, claim row, and download button.

## Schedule the daily run

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
```
This registers a Windows task that fires every day at 12:00 PM, only when
the PC is on. The script downloads new in-process claims, audits each one
exactly once, and exits.

## Manual run anytime

```powershell
scripts\run_now.bat                       # run today's pass now
python scripts\run_audit.py <voucher> <zip>   # one-shot single-claim audit
```

## Learning loop

After admin decides on a voucher, log the verdict so future audits learn:

```powershell
python scripts\log_decision.py output\audit_voucher_3583.json ^
    --final-amount 12000 ^
    --note "Reduced hotel to Grade-C cap; cruise pre-approved"
```

## What changed in this build

- Unolo GPS data now drives Two-Wheeler / Petrol calculations
  (km x Rs.3 bike, Rs.9 car). Excess vs Unolo is FLAG / HIGH severity.
- Voucher / employee identity validated three ways: code, name (with
  token-overlap fuzzy match), and a unique fingerprint
  `<code>__voucher<no>__<date>` so a claim is never audited twice.
- `daily_run.py` is a single-pass 12:00 PM orchestrator that drives the
  whole pipeline (SpineHR fetch -> stage -> audit -> PDF -> state).
- `spine_hr_browser.py` is a Selenium-driven fetcher with placeholder DOM
  selectors that you tune once with `--inspect` to match SpineHR's layout.
- `install_scheduled_task.ps1` registers the Windows scheduled task in one
  command from an elevated PowerShell.
