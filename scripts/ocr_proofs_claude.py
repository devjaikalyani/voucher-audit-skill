"""
OCR proof images using Claude vision when Tesseract is unavailable.
Produces the same proof-index JSON as extract_proofs.py.

v2 upgrades:
  - Tool use: structured output via extract_receipt_data tool — no silent JSON failures
  - Batch API: all images submitted in one batch call (~50% cheaper); polls until done
  - Sync fallback: used for <=2 images or when --sync flag is passed

Usage:
    python ocr_proofs_claude.py <proofs.zip> [--out proofs.json] [--sync]
"""
import sys, os, json, re, zipfile, tempfile, hashlib, base64, argparse, time

try:
    import anthropic
except ImportError as _e:
    if __name__ == '__main__':
        print("ERROR: anthropic SDK not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)
    raise


def _load_shared_env():
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shared_env = os.path.normpath(os.path.join(skill_root, '..', '.env.shared'))
    if os.path.exists(shared_env):
        with open(shared_env) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip()

_load_shared_env()
CLIENT = anthropic.Anthropic()
MODEL  = "claude-haiku-4-5-20251001"

# Minimal system prompt — field definitions live in the tool schema now
SYSTEM_TEXT = (
    "You are a receipt OCR engine for Rite Water Solutions expense auditing. "
    "Extract all visible information from the image using the extract_receipt_data tool. "
    "IMPORTANT: on fuel/petrol receipts, amount_primary is the small rupee amount paid "
    "(typically under Rs.2,000 for a single fill), NOT the odometer reading (5-6 digit number). "
    "On UPI/PhonePe/GPay screenshots, amount_primary is the rupee amount transferred, "
    "not the reference or transaction number."
)

# Tool definition — guarantees a parseable structured response every time.
# Claude fills tool arguments instead of generating a JSON string, so
# json.JSONDecodeError can never silently drop a proof record.
RECEIPT_TOOL = {
    "name": "extract_receipt_data",
    "description": "Extract structured fields from a receipt, bill, or payment screenshot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "amount_primary": {
                "type": ["number", "null"],
                "description": "Total amount paid/payable as a plain number. Null if unclear."
            },
            "amounts": {
                "type": "array",
                "items": {"type": "number"},
                "description": (
                    "All rupee amounts visible in the document. "
                    "Exclude odometer readings, km values, phone numbers, and reference numbers."
                )
            },
            "dates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "All dates found, in YYYY-MM-DD format."
            },
            "vendor": {
                "type": ["string", "null"],
                "description": "Merchant or payee name."
            },
            "ride_id": {
                "type": ["string", "null"],
                "description": "Ride or booking ID (Ola, Uber, Rapido, etc.)."
            },
            "invoice_no": {
                "type": ["string", "null"],
                "description": "Invoice or bill number."
            },
            "txn_id": {
                "type": ["string", "null"],
                "description": "UPI transaction ID or UTR/NEFT reference number."
            },
            "pnr": {
                "type": ["string", "null"],
                "description": "10-digit railway PNR number."
            },
            "gstin": {
                "type": ["string", "null"],
                "description": "GST Identification Number (15-character alphanumeric)."
            },
            "vehicle_no": {
                "type": ["string", "null"],
                "description": "Vehicle registration number (e.g. MH12AB1234)."
            },
            "nights": {
                "type": ["integer", "null"],
                "description": "Number of hotel nights if this is a hotel bill."
            },
            "odometer_start": {
                "type": ["integer", "null"],
                "description": (
                    "Lower/starting odometer reading in km (5-6 digit number like 35812). "
                    "Only for vehicle/odometer photos — null for all other document types."
                )
            },
            "odometer_end": {
                "type": ["integer", "null"],
                "description": (
                    "Higher/ending odometer reading in km. "
                    "Distance travelled = odometer_end - odometer_start."
                )
            },
            "persons_named": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Full names of individuals explicitly printed on this document "
                    "(e.g. guest name on hotel folio, member names on a group restaurant bill, "
                    "passenger name on a ticket). "
                    "Leave empty for fuel receipts, UPI screenshots, and any document "
                    "where no person's name appears."
                )
            },
            "kind": {
                "type": "string",
                "enum": [
                    "upi_screenshot",
                    "fastag_screenshot",
                    "bank_transfer_screenshot",
                    "payment_screenshot",
                    "hotel_bill_printed",
                    "hotel_bill_handwritten",
                    "train_ticket",
                    "bus_ticket",
                    "flight_ticket",
                    "cab_receipt",
                    "auto_receipt",
                    "fuel_receipt",
                    "workshop_bill_printed",
                    "workshop_bill_handwritten",
                    "food_receipt",
                    "purchase_invoice",
                    "receipt_photo",
                    "other"
                ],
                "description": (
                    "Document type. Use 'handwritten' variants when the bill is clearly "
                    "hand-written (not typed/printed)."
                )
            }
        },
        "required": ["kind", "amounts", "dates"]
    }
}

_MONTHS = {m: i for i, m in enumerate(
    ['', 'jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}

def _parse_date(s):
    s = s.strip()
    m = re.match(r'(\d{1,2})[-\s/]([A-Za-z]{3,9})[-\s/](\d{2,4})', s)
    if m:
        d = int(m.group(1)); mo = _MONTHS.get(m.group(2)[:3].lower()); y = int(m.group(3))
        if mo:
            if y < 100: y += 2000
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r'(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


_MAX_DIM   = 1000       # resize longest side to this before sending
_RAW_LIMIT = 3_500_000  # hard ceiling: base64 of 3.5 MB < API 5 MB cap


def _resize_for_ocr(raw):
    """Resize image so longest side <= _MAX_DIM; return (jpeg_bytes, 'image/jpeg')."""
    import io
    try:
        from PIL import Image
    except ImportError:
        if len(raw) > _RAW_LIMIT:
            raise RuntimeError("Pillow not installed — run: pip install Pillow")
        return raw, None

    img = Image.open(io.BytesIO(raw)).convert('RGB')
    if max(img.size) > _MAX_DIM:
        ratio = _MAX_DIM / max(img.size)
        img = img.resize((max(1, int(img.width * ratio)),
                          max(1, int(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    data = buf.getvalue()
    if len(data) > _RAW_LIMIT:
        for q in (70, 55, 40):
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=q)
            data = buf.getvalue()
            if len(data) <= _RAW_LIMIT:
                break
    return data, 'image/jpeg'


def _make_user_content(raw, orig_mt, fname):
    """Build the user message content list for a single image. Returns None on error."""
    try:
        data, mt = _resize_for_ocr(raw)
        if mt is None:
            mt = orig_mt
    except Exception as exc:
        print(f"  WARNING: cannot resize {fname} ({exc}) — skipping.", file=sys.stderr)
        return None
    b64 = base64.standard_b64encode(data).decode()
    return [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
        {"type": "text", "text": "Extract all fields from this receipt or payment screenshot."}
    ]


def _extract_tool_result(message):
    """Pull the extract_receipt_data tool input dict from a Message object."""
    for block in (message.content or []):
        if hasattr(block, 'type') and block.type == 'tool_use' \
                and block.name == 'extract_receipt_data':
            return block.input or {}
    return {}


# ---------- Sync path (small batches / fallback) ----------

def _ocr_sync(raw, orig_mt, fname):
    """Single-image OCR via tool use. Returns OCR dict (never raises)."""
    content = _make_user_content(raw, orig_mt, fname)
    if content is None:
        return {}
    for attempt in range(3):
        try:
            msg = CLIENT.messages.create(
                model=MODEL,
                max_tokens=512,
                system=[{"type": "text", "text": SYSTEM_TEXT,
                          "cache_control": {"type": "ephemeral"}}],
                tools=[RECEIPT_TOOL],
                tool_choice={"type": "tool", "name": "extract_receipt_data"},
                messages=[{"role": "user", "content": content}]
            )
            return _extract_tool_result(msg)
        except anthropic.RateLimitError:
            time.sleep(30 * (attempt + 1))
        except Exception as e:
            print(f"  OCR error ({fname}): {e}", file=sys.stderr)
            return {}
    return {}


# ---------- Batch path (default for 3+ images) ----------

def _ocr_batch(unique_images):
    """
    Submit all unique images as one Batch API job; poll until complete.

    unique_images: list of (content_hash, fname, raw_bytes, orig_mt)
    Returns: dict mapping content_hash -> OCR result dict,
             or None if the Batch API is unavailable (caller falls back to sync).
    """
    # Build one batch request per unique image
    requests = []
    custom_id_to_hash = {}

    for i, (content_hash, fname, raw, orig_mt) in enumerate(unique_images):
        content = _make_user_content(raw, orig_mt, fname)
        if content is None:
            continue
        cid = f"img_{i}"
        custom_id_to_hash[cid] = content_hash
        requests.append({
            "custom_id": cid,
            "params": {
                "model": MODEL,
                "max_tokens": 512,
                "system": [{"type": "text", "text": SYSTEM_TEXT,
                             "cache_control": {"type": "ephemeral"}}],
                "tools": [RECEIPT_TOOL],
                "tool_choice": {"type": "tool", "name": "extract_receipt_data"},
                "messages": [{"role": "user", "content": content}]
            }
        })

    if not requests:
        return {}

    print(f"  Submitting batch of {len(requests)} image(s) to Batch API...", file=sys.stderr)
    try:
        batch = CLIENT.messages.batches.create(requests=requests)
    except Exception as e:
        print(f"  Batch API unavailable ({e}); will fall back to sync.", file=sys.stderr)
        return None  # signals caller to use sync

    print(f"  Batch ID: {batch.id} — polling every 15 s...", file=sys.stderr)
    MAX_WAIT_S = 1200   # give up after 20 minutes
    elapsed    = 0
    poll_s     = 15

    while elapsed < MAX_WAIT_S:
        time.sleep(poll_s)
        elapsed += poll_s
        try:
            batch = CLIENT.messages.batches.retrieve(batch.id)
        except Exception as e:
            print(f"  Poll error: {e}", file=sys.stderr)
            continue
        counts = batch.request_counts
        done   = counts.succeeded + counts.errored + counts.canceled + counts.expired
        total  = done + counts.processing
        print(f"  [{elapsed}s] {done}/{total} complete...", file=sys.stderr)
        if batch.processing_status == "ended":
            break
    else:
        print(f"  WARNING: batch timed out after {MAX_WAIT_S}s — partial results.", file=sys.stderr)

    results = {}
    try:
        for result in CLIENT.messages.batches.results(batch.id):
            content_hash = custom_id_to_hash.get(result.custom_id)
            if content_hash is None:
                continue
            if result.result.type == "succeeded":
                results[content_hash] = _extract_tool_result(result.result.message)
            else:
                print(f"  Batch error for {result.custom_id}: {result.result.type}", file=sys.stderr)
                results[content_hash] = {}
    except Exception as e:
        print(f"  Error retrieving batch results: {e}", file=sys.stderr)

    return results


# ---------- Proof record builder ----------

def fingerprint(record, fname=''):
    keys = [record.get('ride_id'), record.get('invoice_no'), record.get('txn_id')]
    key  = next((k for k in keys if k), None)
    if key:
        return "id:" + key
    vendor = record.get('vendor')
    amount = record.get('amount')
    dates  = record.get('dates') or []
    if not vendor and not amount and not dates:
        return "unique:" + hashlib.md5(fname.encode()).hexdigest()[:10]
    parts = [str(vendor or 'x'), str(amount or 0), str(dates[0] if dates else 'x')]
    return "fp:" + hashlib.md5('|'.join(parts).encode()).hexdigest()[:10]


def _build_proof_record(fname, ocr):
    """Convert a raw OCR tool-result dict into a full proof record."""
    amounts = [a for a in (ocr.get('amounts') or []) if isinstance(a, (int, float))]
    primary = ocr.get('amount_primary')
    if isinstance(primary, (int, float)) and primary not in amounts:
        amounts.insert(0, primary)

    dates = []
    for d in (ocr.get('dates') or []):
        parsed = _parse_date(str(d)) if d else None
        if parsed and parsed not in dates:
            dates.append(parsed)

    odo_start = ocr.get('odometer_start')
    odo_end   = ocr.get('odometer_end')
    if isinstance(odo_start, float): odo_start = int(odo_start)
    if isinstance(odo_end,   float): odo_end   = int(odo_end)

    preview_parts = [f"vendor={ocr.get('vendor')}", f"amounts={amounts}", f"dates={dates}"]
    if odo_start is not None or odo_end is not None:
        dist = (odo_end - odo_start) if (odo_start and odo_end and odo_end > odo_start) else None
        preview_parts.append(
            f"odometer={odo_start}->{odo_end}" + (f" ({dist} km)" if dist else ""))

    rec = {
        'file_name':      fname,
        'kind':           ocr.get('kind', 'other'),
        'ride_id':        ocr.get('ride_id'),
        'invoice_no':     ocr.get('invoice_no'),
        'txn_id':         ocr.get('txn_id'),
        'pnr':            ocr.get('pnr'),
        'gstin':          ocr.get('gstin'),
        'vehicle_no':     ocr.get('vehicle_no'),
        'check_in':       None,
        'check_out':      None,
        'nights':         ocr.get('nights'),
        'odometer_start': odo_start,
        'odometer_end':   odo_end,
        'amounts':        amounts,
        'amount':         max(amounts) if amounts else None,
        'dates':          dates,
        'vendor':         ocr.get('vendor'),
        'persons_named':  [p for p in (ocr.get('persons_named') or []) if p and str(p).strip()],
        'text_preview':   '[Claude OCR] ' + ' '.join(preview_parts),
    }
    rec['fingerprint'] = fingerprint(rec, fname)
    rec['duplicate_of'] = None
    return rec


# ---------- Main entry point ----------

def index_proofs_claude(zip_path, sync=False):
    """
    Index all image proofs in a ZIP using Claude Vision OCR.

    sync=False (default): submit all images as a single Batch API job
                          (~50% cheaper; adds 1-5 min polling wait).
    sync=True:            per-image sequential calls (faster for <=2 images
                          or when called with --sync flag).
    """
    IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'tiff'}

    # Phase 1: collect all image files from the ZIP
    files = []   # list of (fname, raw_bytes, orig_mt)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            for root, _, fnames in os.walk(tmp):
                for fn in sorted(fnames):
                    if fn.startswith('.') or fn.startswith('__'):
                        continue
                    ext = fn.lower().rsplit('.', 1)[-1]
                    if ext not in IMAGE_EXTS:
                        continue
                    orig_mt = {
                        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                        'png': 'image/png',  'webp': 'image/webp',
                        'tiff': 'image/tiff',
                    }.get(ext, 'image/jpeg')
                    raw = open(os.path.join(root, fn), 'rb').read()
                    files.append((fn, raw, orig_mt))

    # Phase 2: deduplicate by content hash before sending to API
    seen_hashes   = {}            # content_hash -> first fname
    unique_images = []            # (content_hash, fname, raw, orig_mt)
    hash_for_file = {}            # fname -> content_hash (for all files, incl. dupes)

    for fn, raw, orig_mt in files:
        h = hashlib.md5(raw).hexdigest()
        hash_for_file[fn] = h
        if h not in seen_hashes:
            seen_hashes[h] = fn
            unique_images.append((h, fn, raw, orig_mt))

    n_total  = len(files)
    n_unique = len(unique_images)
    n_dupes  = n_total - n_unique
    print(f"  {n_total} image(s) found, {n_unique} unique"
          + (f", {n_dupes} duplicate(s) skipped." if n_dupes else "."), file=sys.stderr)
    print(f"  OCR-ing {n_unique} image(s) via Claude {MODEL}...", file=sys.stderr)

    # Phase 3: run OCR — batch (default) or sync fallback
    ocr_cache = {}   # content_hash -> OCR result dict
    use_sync  = sync or n_unique <= 2

    if not use_sync:
        batch_results = _ocr_batch(unique_images)
        if batch_results is None:
            use_sync = True   # batch unavailable; fall through to sync
        else:
            ocr_cache = batch_results
            for h, fn, _, _ in unique_images:
                ocr_cache.setdefault(h, {})   # ensure every hash has an entry

    if use_sync:
        for i, (h, fn, raw, orig_mt) in enumerate(unique_images, 1):
            print(f"  [{i}/{n_unique}] {fn}", file=sys.stderr)
            ocr_cache[h] = _ocr_sync(raw, orig_mt, fn)

    if n_dupes:
        print(f"  Deduplication: {n_dupes} image(s) reused cached OCR "
              f"({n_unique} API call(s) made).", file=sys.stderr)

    # Phase 4: build proof records for every file (dupes reuse cached OCR)
    proofs = []
    for fn, raw, orig_mt in files:
        h   = hash_for_file[fn]
        ocr = ocr_cache.get(h, {})
        proofs.append(_build_proof_record(fn, ocr))

    # Phase 5: mark cross-file duplicate fingerprints
    seen_fps = {}
    for p in proofs:
        fp = p['fingerprint']
        if fp in seen_fps:
            p['duplicate_of'] = seen_fps[fp]
        else:
            seen_fps[fp] = p['file_name']

    return {'proofs': proofs}


# ---------- Legacy single-image entry point (used by extract_document.py) ----------

def ocr_image(path):
    ext     = path.lower().rsplit('.', 1)[-1]
    orig_mt = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
               'png': 'image/png',  'webp': 'image/webp',
               'tiff': 'image/tiff'}.get(ext, 'image/jpeg')
    raw     = open(path, 'rb').read()
    return _ocr_sync(raw, orig_mt, os.path.basename(path))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('proofs_zip')
    ap.add_argument('--out',  default=None)
    ap.add_argument('--sync', action='store_true',
                    help='Use sequential per-image calls instead of Batch API')
    args = ap.parse_args()

    data = index_proofs_claude(args.proofs_zip, sync=args.sync)
    out  = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(out)
        print(f"Proofs JSON written: {args.out}", file=sys.stderr)
    else:
        print(out)
