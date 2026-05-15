---
name: voucher-audit
description: Audit Rite Water Solutions employee expense vouchers (Spine HR PDFs) against the Tour & Travel Policy and a ZIP of supporting proof documents. Use this skill whenever the user uploads or refers to a Spine HR voucher and proofs, mentions reviewing/auditing employee claims, mentions Voucher No. / RWSIPL employee codes / "claim review" / "in-process claims" / "policy check on a claim", asks for a Policy-Based Expense Audit Report PDF, wants to fetch/download claims from Spine HR, or wants to run a batch audit on in-process claims. Trigger this skill even if the user does not explicitly say "audit" - any input pairing of a Rite Water voucher PDF + proofs (zip / loose images) is in scope. The output is a multi-section audit PDF matching the format used by Rite Water's Internal Audit Division, plus an updated processed_vouchers state file so the same claim is never audited twice.
---

# Voucher Audit Skill

## !! MANDATORY — READ BEFORE DOING ANYTHING !!

The entire audit pipeline is **already implemented** inside this skill's `scripts/` folder.
You MUST call those scripts. Do **NOT** write your own audit, OCR, policy, or PDF code.

**OCR is Cowork-vision-only.** The skill no longer ships an automatic OCR
backend (Anthropic API path was removed because it produced silent empty
proofs on auth failure; Tesseract path was removed for poor accuracy on
phone screenshots). Before calling `run_audit.py`, you (the Cowork agent)
MUST do the OCR yourself by reading each proof image with the Read tool.

`run_audit.py` will refuse to run without `--proofs-json` and will print
the exact 4-step recovery commands.

There are **two modes**:
- **Mode A — Manual upload**: voucher PDF + proofs ZIP → stage → vision-OCR → `run_audit.py`
- **Mode B — SpineHR Fetch**: download claims from SpineHR → for each: stage → vision-OCR → `run_audit.py`

---

## Step 1 — Find the skill root

```bash
SKILL_ROOT=$(python3 -c "import os; p='/sessions'; dirs=[d for d in os.listdir(p) if os.path.isdir(os.path.join(p,d))]; print(os.path.join(p,dirs[0],'mnt','.claude','skills','voucher-audit'))")
echo "Skill root: $SKILL_ROOT"
```

## Step 2 — Install Python dependencies

```bash
pip3 install -q pdfplumber reportlab Pillow selenium webdriver-manager 2>&1 | tail -3
```

## Step 3 — Determine output directory

```bash
OUT_DIR=$(python3 -c "
import os
preferred = '/mnt/d/Company Projects/Voucher_Audit_Skill/output'
fallback = '/sessions/' + os.listdir('/sessions')[0] + '/mnt/outputs'
if os.path.isdir('/mnt/d/Company Projects/Voucher_Audit_Skill'):
    os.makedirs(preferred, exist_ok=True)
    print(preferred)
else:
    os.makedirs(fallback, exist_ok=True)
    print(fallback)
")
echo "Output directory: $OUT_DIR"
```

---

## CORE LOOP — How to audit ONE voucher (Cowork-vision-only)

Every audit, regardless of mode, follows this same sequence.

### 1. Stage proof images for vision OCR

```bash
STAGE_DIR="$OUT_DIR/_staging/<VOUCHER_NO>"
python3 "$SKILL_ROOT/scripts/cowork_stage.py" \
  --voucher "<voucher.pdf>" \
  --proofs  "<proofs.zip>" \
  --out     "$STAGE_DIR"
```

Output:
- `$STAGE_DIR/proofs/<hash>_<filename>` — unique image files (deduped by content hash)
- `$STAGE_DIR/proof_templates/<hash>.json` — one OCR template per unique image
- `$STAGE_DIR/proofs.json` — pre-seeded with empty templates; YOU fill this in
- `$STAGE_DIR/voucher_doc.pdf` + `$STAGE_DIR/voucher.json` — the voucher itself
- `$STAGE_DIR/manifest.json` — index of everything that needs filling

### 2. Vision-OCR every image with the Read tool

For every unique image in `$STAGE_DIR/proofs/`, use the Read tool:

```
Read: D:\Company Projects\Voucher_Audit_Skill\output\_staging\<VOUCHER_NO>\proofs\<hash>_<filename>
```

For each image, extract these fields and update the corresponding entry in `$STAGE_DIR/proofs.json`:

- `kind`: one of `upi_screenshot`, `fastag_screenshot`, `bank_transfer_screenshot`,
  `payment_screenshot`, `hotel_bill_printed`, `hotel_bill_handwritten`,
  `train_ticket`, `bus_ticket`, `flight_ticket`, `cab_receipt`, `auto_receipt`,
  `fuel_receipt`, `workshop_bill_printed`, `workshop_bill_handwritten`,
  `food_receipt`, `purchase_invoice`, `receipt_photo`, `other`
- `vendor`: merchant / payee name (string or null)
- `amount`: primary rupee amount (number or null) — NOT odometer/phone numbers
- `amounts`: list of all rupee amounts visible
- `dates`: list of dates in `YYYY-MM-DD` format
- `txn_id`, `ride_id`, `invoice_no`, `pnr`, `gstin`, `vehicle_no` as visible
- `odometer_start`, `odometer_end` for Unolo attendance screenshots
- `nights` for hotel bills
- `persons_named` for group bills

Fill ONLY the unique images. Duplicates inherit their twin's OCR automatically.

If the **voucher itself** is a non-Spine-HR document (manifest says
`is_spine_hr: false`), also Read `voucher_doc.<ext>` and fill `voucher.json`.

### 3. Finalize the staging directory

```bash
python3 "$SKILL_ROOT/scripts/cowork_stage.py" --finalize "$STAGE_DIR"
```

This recomputes per-proof fingerprints + cross-file `duplicate_of` links
from the values you filled in.

### 4. Run the audit

```bash
python3 "$SKILL_ROOT/scripts/run_audit.py" \
  "<voucher.pdf>" "<proofs.zip>" \
  --proofs-json "$STAGE_DIR/proofs.json" \
  --out-dir     "$OUT_DIR"
```

If the voucher document was non-Spine-HR, also pass `--voucher-json "$STAGE_DIR/voucher.json"`.

Output files in `$OUT_DIR`:
- `audit_voucher_<N>.json`
- `RiteAuditReport_Voucher<N>_<Name>_<Code>.pdf`

### 5. Show result summary

```bash
python3 -c "
import json, glob
files = sorted(glob.glob('$OUT_DIR/audit_voucher_*.json'))
d = json.load(open(files[-1]))
v = d['voucher']; t = d['totals']
print(f'Voucher {v[\"voucher_no\"]} | {v[\"employee_name\"]} ({v[\"employee_code\"]})')
print(f'Claimed:        INR {t[\"gross_claimed\"]:,.0f}')
print(f'PH Approved:    INR {t[\"reviewer_approved\"]:,.0f}')
print(f'Policy Eligible:INR {t[\"policy_eligible\"]:,.0f}')
print(f'Recommended Hold:INR {t[\"recommended_hold\"]:,.0f}')
"
```

---

## MODE A — Audit an uploaded voucher PDF + proofs ZIP (single claim)

Use when the user uploads or provides paths to one voucher PDF and one
proofs ZIP. The voucher PDF name typically contains "TravelExp",
"Voucher", or "ExpVouch"; the proofs ZIP name contains the employee code
(e.g. "RWSIPL562") or "proofs".

Steps: locate files → run the **Core Loop** above for that single pair.

---

## MODE A2 — Bulk upload (multiple claims at once)

When the user uploads multiple voucher PDFs and proofs ZIPs together,
auto-pair them by voucher number first, then run the Core Loop per pair.

### A2-1 — Locate all uploaded files

```bash
find /sessions -name "*.pdf" 2>/dev/null | grep -Ev "RiteAuditReport|PolicyBased" | sort
find /sessions -name "*.zip" 2>/dev/null | sort
```

### A2-2 — Auto-pair ZIPs and PDFs by voucher number

The proofs ZIP filename encodes the voucher number: `RWSIPL562_3134_20260421122635.zip` → `3134`.
The voucher PDF needs a text scan to extract its voucher number.

```bash
python3 - << 'PAIR_EOF'
import os, re, glob, json
zips = sorted(glob.glob('/sessions/**/*.zip', recursive=True))
pdfs = sorted(f for f in glob.glob('/sessions/**/*.pdf', recursive=True)
              if not re.search(r'RiteAuditReport|PolicyBased', f))

zip_map = {}
for z in zips:
    m = re.match(r'^[A-Z0-9]+_(\d+)_\d+\.zip$', os.path.basename(z))
    if m: zip_map[m.group(1)] = z

import pdfplumber
pdf_map = {}
for p in pdfs:
    try:
        with pdfplumber.open(p) as doc:
            text = '\n'.join(page.extract_text() or '' for page in doc.pages[:2])
        m = re.search(r'Voucher\s+No\.?\s*:?\s*(\d+)', text, re.IGNORECASE)
        if m: pdf_map[m.group(1)] = p
    except Exception: pass

pairs = [{'voucher_no': v, 'pdf': pdf_map[v], 'zip': zip_map[v]}
         for v in zip_map if v in pdf_map]

print(f'Matched pairs : {len(pairs)}')
for p in pairs:
    print(f"  Voucher {p['voucher_no']:>6} | PDF: {os.path.basename(p['pdf'])} | ZIP: {os.path.basename(p['zip'])}")

with open('/tmp/voucher_pairs.json', 'w') as f:
    json.dump(pairs, f, indent=2)
PAIR_EOF
```

### A2-3 — For EACH matched pair, run the Core Loop

For each pair in `/tmp/voucher_pairs.json`:
1. Stage (Core Loop step 1) into a per-voucher staging dir
2. Vision-OCR every image in that staging dir (Core Loop step 2)
3. Finalize (Core Loop step 3)
4. Run audit with `--proofs-json` (Core Loop step 4)

Do not try to OCR images for multiple vouchers in a single pass — keep
each voucher's staging directory and proofs.json separate so the audit
engine can attribute proofs to findings cleanly.

### A2-4 — Show batch results

```bash
python3 -c "
import json, glob
files = sorted(glob.glob('$OUT_DIR/audit_voucher_*.json'))
print(f'Total audit reports: {len(files)}')
for f in files:
    d = json.load(open(f))
    v = d['voucher']; t = d['totals']
    print(f'  Voucher {v[\"voucher_no\"]:>5} | {v[\"employee_name\"]:<30} | Claimed {t[\"gross_claimed\"]:>8,.0f} | Eligible {t[\"policy_eligible\"]:>8,.0f} | Hold {t[\"recommended_hold\"]:>8,.0f}')
"
```

---

## MODE B — Fetch in-process claims from SpineHR and audit them

Use when the user asks to fetch, download, or pull claims from SpineHR's
"In Process" section. Default to fetching **3 claims** unless the user
specifies a different number.

### B1 — Install Chrome (needed for browser automation)

```bash
which chromium-browser chromium google-chrome 2>/dev/null || (
  apt-get update -qq 2>/dev/null &&
  apt-get install -y -qq chromium chromium-driver 2>/dev/null ||
  apt-get install -y -qq chromium-browser 2>/dev/null
)
```

### B2 — Load SpineHR credentials from Windows drive

```bash
python3 -c "
import os, shutil
src = '/mnt/d/Company Projects/.env.shared'
dst = os.path.dirname('$SKILL_ROOT') + '/.env.shared'
if os.path.exists(src):
    shutil.copy(src, dst)
    print('Credentials loaded from Windows drive')
elif os.path.exists(dst):
    print('Credentials already present')
else:
    print('WARNING: .env.shared not found - set SPINEHR_URL, SPINEHR_USERNAME, SPINEHR_PASSWORD manually')
"
```

The `.env.shared` file should contain `SPINEHR_URL`, `SPINEHR_USERNAME`,
`SPINEHR_PASSWORD` — these are the only env vars the skill reads.
**Do not** add `ANTHROPIC_API_KEY` — it is not used by the audit pipeline anymore.

### B3 — Fetch in-process claims from SpineHR

```bash
CLAIMS_DIR="$SKILL_ROOT/in_process_claims"
mkdir -p "$CLAIMS_DIR"

python3 "$SKILL_ROOT/scripts/spine_hr_browser.py" \
  --headless \
  --max 3 2>&1
```

This logs into SpineHR, navigates to **Claims → Approve Voucher →
IN PROCESS**, and downloads the proofs ZIP + voucher PDF for each claim
into `in_process_claims/{EMP_CODE}_{VOUCHER_NO}/`.

### B4 — For each downloaded claim, run the Core Loop

For each subfolder in `$CLAIMS_DIR`:
1. Stage (Core Loop step 1) into `$OUT_DIR/_staging/<voucher_no>`
2. Vision-OCR every image (Core Loop step 2)
3. Finalize (Core Loop step 3)
4. Run audit with `--proofs-json` (Core Loop step 4)

The skill's `state_store.py` ensures already-audited vouchers are skipped
on the next run — this is checked inside `run_audit.py`.

### B5 — Show batch results

Same as A2-4.

---

## What NOT to do

- Do NOT write your own voucher parser
- Do NOT write your own OCR code
- Do NOT write your own policy evaluation
- Do NOT write your own PDF generator
- Do NOT call `ocr_proofs_claude.py` directly — it is dead code retained
  only for legacy compatibility
- Do NOT skip the staging step and try to feed `run_audit.py` a ZIP
  without `--proofs-json` — it will refuse and tell you why
- Do NOT batch-OCR images across multiple vouchers — keep each voucher's
  staging directory + proofs.json fully separate

The scripts in `$SKILL_ROOT/scripts/` handle audit logic correctly —
your job is solely to do the OCR step via the Read tool and feed the
results back via the staging mechanism.
