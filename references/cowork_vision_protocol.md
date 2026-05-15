# Cowork Vision Protocol

How the voucher-audit skill runs **without an Anthropic API key** when invoked
from inside Cowork. Instead of calling Claude Vision over HTTP, the skill
*stages* every image that needs OCR and lets the cowork agent (which is itself
Claude, with native multimodal `Read`) do the OCR in chat. The agent writes
back JSON files in the schemas the audit engine already understands, then
re-invokes the pipeline with `--voucher-json` and `--proofs-json` flags.

The Anthropic-API path is unchanged — local Windows runs (`run_now.bat`,
`daily_run.py`) keep using `anthropic.Anthropic()` exactly as before.

## Two new entry points

1. **`scripts/cowork_stage.py`** — extracts the voucher document and unzips
   every proof image into a staging directory, emitting a `manifest.json` and
   empty response templates the agent fills in.
2. **`scripts/run_audit.py --voucher-json FILE --proofs-json FILE`** — the
   existing one-shot wrapper, now able to consume pre-extracted JSON instead
   of calling the API. (`--proofs-json` already existed; `--voucher-json` is
   new.)

## Staging directory layout

After running `cowork_stage.py --voucher <doc> --proofs <zip> --out <dir>`:

```
<staging>/
  manifest.json           # written by cowork_stage.py, read by the agent
  voucher_doc.<ext>       # the voucher document, copied verbatim (image or PDF)
  voucher_template.json   # empty template the agent fills in (omit if Spine HR PDF)
  voucher.json            # agent writes this after OCR'ing voucher_doc
  proofs/                 # all proof images extracted from the zip
    <hash>_<orig_name>    # one file per unique proof image
  proof_templates/
    <hash>.json           # one empty template per proof image
  proofs.json             # agent writes this after OCR'ing every proof
```

`manifest.json` summarises what's pending:

```json
{
  "session_id": "stage_20260506_103412",
  "voucher": {
    "is_spine_hr": false,
    "doc_path": "voucher_doc.jpg",
    "template_path": "voucher_template.json",
    "response_path": "voucher.json"
  },
  "proofs": [
    {"file_name": "ExpVouch_RWSIPL706_...jpg",
     "image_path": "proofs/abc123_ExpVouch_RWSIPL706_...jpg",
     "template_path": "proof_templates/abc123.json"}
  ],
  "proofs_response_path": "proofs.json",
  "emp_code_hint": "RWSIPL706"
}
```

If the voucher document is a Spine HR PDF (detected via
`extract_document.is_spine_hr_voucher`), the `voucher.is_spine_hr` flag is
true, no `voucher_template.json` is written, and the agent skips the voucher
step entirely — `extract_voucher.py` will parse the structured PDF on its own.

## What the agent fills in

### voucher.json

Agent reads `voucher_doc.<ext>` natively, then writes a JSON object matching
the schema in `extract_document._EXTRACT_SYSTEM`:

```json
{
  "voucher_no": "INV-2226",
  "date": "2026-04-15",
  "employee_name": "Mahesh Jadhav",
  "employee_code": "RWSIPL706",
  "vendor": "HP Petrol Pump, Pune",
  "narration": "Petrol fill-up on 15-Apr-2026",
  "cost_center": null,
  "currency": "INR",
  "total_amount": 1080,
  "gstin": null,
  "payment_mode": "UPI",
  "document_type": "fuel_receipt",
  "is_handwritten": false,
  "line_items": [
    {
      "date": "2026-04-15",
      "description": "Petrol",
      "expense_type": "Fuel",
      "amount": 1080,
      "remarks": ""
    }
  ]
}
```

Schema rules (must match `_EXTRACT_SYSTEM`):
- All amounts are plain numbers — no rupee symbol, no commas.
- `expense_type` is one of: Hotel, Food Allowance, Bus, Train, Flight, Cab,
  Auto, Fuel, Toll, Parking, Site Expense, Vehicle Maintenance, Other Expense.
- `document_type` is one of: invoice, receipt, hotel_bill, workshop_bill,
  food_bill, travel_ticket, other.
- A single-amount document gets exactly one line_item.

`cowork_stage.py` writes a pre-filled template with all keys present and
nulled, plus the schema rules in a `_schema` field — agent just fills in
what it can read.

### proofs.json

Agent reads every image in `proofs/`, writes a single JSON to `proofs.json`
of shape:

```json
{
  "proofs": [
    {
      "file_name": "ExpVouch_RWSIPL706_...jpg",
      "kind": "fuel_receipt",
      "ride_id": null,
      "invoice_no": null,
      "txn_id": "T2604154...",
      "pnr": null,
      "gstin": null,
      "vehicle_no": null,
      "check_in": null,
      "check_out": null,
      "nights": null,
      "odometer_start": null,
      "odometer_end": null,
      "amounts": [1080],
      "amount": 1080,
      "dates": ["2026-04-15"],
      "vendor": "HP Petrol Pump",
      "persons_named": [],
      "text_preview": "[cowork-vision] vendor=HP Petrol Pump amounts=[1080] dates=['2026-04-15']",
      "fingerprint": "fp:abc123",
      "duplicate_of": null
    }
  ]
}
```

This matches `_build_proof_record()` in `ocr_proofs_claude.py`. The agent
fills the OCR fields (`kind`, `amounts`, `dates`, `vendor`, IDs, etc.); the
fingerprinting + duplicate detection helper functions are exposed on the
template for the agent to copy if it wants exact parity, otherwise
`cowork_stage.py --finalize <staging>` will recompute them post-hoc.

`kind` enum: upi_screenshot, fastag_screenshot, bank_transfer_screenshot,
payment_screenshot, hotel_bill_printed, hotel_bill_handwritten, train_ticket,
bus_ticket, flight_ticket, cab_receipt, auto_receipt, fuel_receipt,
workshop_bill_printed, workshop_bill_handwritten, food_receipt,
purchase_invoice, receipt_photo, other.

## Resume — running the audit

Once `voucher.json` and `proofs.json` are populated:

```
python scripts/run_audit.py <voucher_doc> <proofs_zip> \
    --emp-code <code> \
    --voucher-json <staging>/voucher.json \
    --proofs-json  <staging>/proofs.json \
    --out-dir <skill>/output
```

`run_audit.py` passes the JSONs into `audit_engine.run_audit(voucher_data=...,
proofs_data=...)`, which already exists, and runs the policy engine + PDF
render normally. No API call is made.

## Verdict step

`audit_engine.claude_final_verdict()` calls Sonnet for an independent text
verdict. In cowork mode it returns `None` (existing graceful-fail behaviour),
which the PDF generator already handles by omitting that section. If a
cowork-native verdict is wanted later, mirror the same staging pattern:
write `verdict_input.json` + `verdict_template.json`, agent fills the
template, `run_audit.py --verdict-json` consumes it.

## Why not refactor extract_document.py / ocr_proofs_claude.py directly?

Both modules are tightly coupled to the Anthropic SDK shape (system prompts,
tool-use schemas, batch polling). Routing them through a shim would either
duplicate that logic in cowork form or add a fragile "fake API client". The
staging-dir approach keeps the existing API path 100% untouched and gives
the cowork agent the same input — just an image — that the API call would
have received. If the API path changes, the cowork path doesn't break.
