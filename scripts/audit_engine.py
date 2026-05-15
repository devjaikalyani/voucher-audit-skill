"""
Run a policy-based audit over a Spine HR voucher + ZIP of supporting proofs.

Inputs:
  - voucher PDF path (Spine HR Expense Voucher)
  - proofs ZIP path
  - employee master CSV (code → designation)
  - policy rules JSON
  - optional: history directory (past audit decision logs)

Output:
  A structured JSON "audit findings" object that the PDF generator turns into
  the final report. The structure intentionally mirrors the sections of the
  sample audit report so the generator stays simple.

Usage:
  python audit_engine.py <voucher.pdf> <proofs.zip> [--out audit.json]
"""
import sys, os, json, csv, re, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_voucher import extract_voucher

# OCR backend
# -----------
# This skill is Cowork-orchestrated: the calling agent (Claude in Cowork mode)
# does OCR by viewing each proof image via its native vision (Read tool) and
# supplies the resulting proofs JSON to run_audit.py via --proofs-json.
#
# There is intentionally NO automatic OCR backend here:
#   - Anthropic Claude-Vision API: removed (the May 2026 batch hit silent
#     auth failures that produced empty proofs).
#   - Tesseract: removed (low accuracy on small phone screenshots).
#
# Workflow:
#   1. python scripts/cowork_stage.py <proofs.zip> --out <staging_dir>
#      → extracts unique images and writes ocr_cache.template.json
#   2. Agent reads each image via Read tool, fills ocr_cache.json
#   3. python scripts/cowork_stage.py --build-proofs <ocr_cache.json> \
#          <proofs.zip> <proofs.json>
#   4. python scripts/run_audit.py <voucher.pdf> <proofs.zip> \
#          --proofs-json <proofs.json>
#
# If audit_engine.run_audit() is invoked without a pre-built proofs JSON,
# index_proofs raises a hard error pointing at this workflow.
_OCR_BACKEND = 'cowork-vision'


def index_proofs(zip_path, *args, **kwargs):
    raise RuntimeError(
        "Voucher Audit Skill is Cowork-vision-only — proofs OCR must be "
        "supplied by the agent. Run scripts/cowork_stage.py to extract "
        "images, fill ocr_cache.json by reading each image, then pass "
        "the resulting proofs JSON to run_audit.py via --proofs-json. "
        f"(zip={zip_path!r})"
    )

# Unolo GPS-distance loader -- used to verify two-wheeler / petrol claims
try:
    from unolo_loader import load_unolo, km_for, days_active
    _UNOLO = load_unolo()
except Exception as _e:
    print(f"[audit_engine] Unolo data unavailable ({_e}); km-based "
          f"calculations will fall back to claimed values.", file=sys.stderr)
    _UNOLO = {}
    def km_for(*a, **kw): return 0.0
    def days_active(*a, **kw): return 0

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.path.join(SKILL_ROOT, 'assets', 'policy_rules.json')
MASTER_PATH = os.path.join(SKILL_ROOT, 'assets', 'employee_master.csv')
HISTORY_DIR = os.path.join(SKILL_ROOT, 'history', 'decisions')


# -------- Loaders --------

def load_policy(path=POLICY_PATH):
    with open(path) as f:
        return json.load(f)


def load_employee_master(path=MASTER_PATH):
    out = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            code = (row.get('Code') or '').strip()
            if code:
                out[code] = row
    return out


def load_history(path=HISTORY_DIR):
    items = []
    if not os.path.isdir(path):
        return items
    for fname in sorted(os.listdir(path)):
        if fname.endswith('.json'):
            try:
                with open(os.path.join(path, fname)) as f:
                    items.append(json.load(f))
            except Exception:
                pass
    return items


# -------- Identity validators --------

def _norm(s):
    if not s:
        return ''
    return re.sub(r'\s+', ' ', s.strip().upper())


def _name_match(master_name, voucher_name):
    """Strict yes/no, with light tolerance for whitespace & case."""
    return _norm(master_name) == _norm(voucher_name) if master_name and voucher_name else False


def _name_match_note(master_name, voucher_name):
    """Human-readable explanation of the name comparison."""
    if not master_name and not voucher_name:
        return 'Both names missing'
    if not master_name:
        return f'Master CSV has no name for this code; voucher says "{voucher_name}"'
    if not voucher_name:
        return f'Voucher has no employee name; master says "{master_name}"'
    if _norm(master_name) == _norm(voucher_name):
        return 'OK - master and voucher names match exactly'
    # token overlap (helps surface order/abbrev differences without auto-passing)
    a = set(_norm(master_name).split())
    b = set(_norm(voucher_name).split())
    overlap = a & b
    return (f'MISMATCH - voucher "{voucher_name}" vs master "{master_name}". '
            f'Common tokens: {sorted(overlap)}. Verify employee identity before approving.')


def _voucher_fingerprint(voucher):
    """Stable id used for dedup across runs: <code>__voucher<no>__<isodate>."""
    return (f"{voucher.get('employee_code') or 'XXXX'}"
            f"__voucher{voucher.get('voucher_no') or 'X'}"
            f"__{voucher.get('voucher_date') or 'nodate'}")


# -------- Designation classification --------

def map_designation(designation_text, policy):
    """Map a free-text designation from the master CSV to a policy bucket."""
    if not designation_text:
        return ('SR_EXECUTIVE', 'Default - designation missing in master')
    raw = designation_text.upper().strip()
    for bucket_key, bucket in policy['designations'].items():
        for alias in bucket['aliases']:
            if alias.upper() == raw or alias.upper() in raw:
                return (bucket_key, f'Mapped "{designation_text}" -> {bucket_key} via alias "{alias}"')
    # default fallback
    return ('SR_EXECUTIVE', f'Could not match "{designation_text}" - defaulted to SR_EXECUTIVE; verify with HR')


# -------- City grade --------

def city_grade(city_name, policy):
    if not city_name:
        return 'C', 'No city derivable - defaulting to Grade C (most restrictive)'
    name = city_name.upper().strip()
    # Exact match first, then substring (handles "New Delhi" vs "Delhi" etc.)
    for grade, info in policy['city_grades'].items():
        cities = info['cities']
        if cities == '*':
            continue
        for c in cities:
            if c == name:
                return grade, f'{city_name} -> Grade {grade} ({info["label"]})'
    for grade, info in policy['city_grades'].items():
        cities = info['cities']
        if cities == '*':
            continue
        for c in cities:
            if c in name or name in c:
                return grade, f'{city_name} -> Grade {grade} ({info["label"]})'
    return 'C', f'{city_name} not in Grade A or B list -> Grade C'


def derive_city_from_voucher(voucher):
    """Determine destination city from cost_center (primary) or narration keyword search."""
    import re as _re

    # Words that indicate the cost_center is an office/entity, not a destination city
    _SKIP_WORDS = {
        'HEAD', 'OFFICE', 'HO', 'CORPORATE', 'HEADQUARTERS', 'HQ',
        'ADMIN', 'ACCOUNTS', 'HR', 'FINANCE', 'OPERATIONS', 'UNIT',
        'BRANCH', 'DEPOT', 'PLANT', 'FACTORY', 'WORKSHOP', 'YARD',
        'PAN', 'INDIA', 'MULTIPLE', 'OTHERS', 'OTHER', 'NCR',
    }
    # Exact-match state names (not cities)
    _STATE_NAMES = {
        'MAHARASHTRA', 'RAJASTHAN', 'GUJARAT', 'KARNATAKA', 'ANDHRA PRADESH',
        'TELANGANA', 'MADHYA PRADESH', 'CHHATTISGARH', 'JHARKHAND',
        'UTTAR PRADESH', 'UTTARAKHAND', 'ODISHA', 'ORISSA', 'WEST BENGAL',
        'TAMIL NADU', 'KERALA', 'BIHAR', 'PUNJAB', 'HARYANA', 'ASSAM',
        'MEGHALAYA', 'TRIPURA', 'MANIPUR', 'NAGALAND', 'MIZORAM',
        'ARUNACHAL PRADESH', 'SIKKIM', 'GOA', 'HIMACHAL PRADESH',
        'JAMMU AND KASHMIR', 'LADAKH',
    }

    # 1. Use cost_center directly — contains exact city name most of the time
    cc = (voucher.get('cost_center') or '').strip()
    if cc:
        cc_upper = cc.upper()
        # Skip pure state names
        if cc_upper not in _STATE_NAMES:
            # Skip if any token is an office-type word (e.g. "Nagpur_Head Office")
            tokens = set(_re.split(r'[\s_\-/]+', cc_upper))
            if not tokens.intersection(_SKIP_WORDS):
                return cc  # e.g. "Dhule", "Nagpur", "Palghar_MH", "Jalgaon"

    KNOWN_CITIES = [
        # Grade A
        'DELHI', 'NEW DELHI', 'MUMBAI', 'KOLKATA', 'KOLKATTA', 'CHENNAI',
        'AHMEDABAD', 'BANGALORE', 'BENGALURU', 'HYDERABAD', 'JAIPUR', 'PUNE',
        # Grade B — state capitals + Tier II
        'AGRA', 'AMRITSAR', 'BARODA', 'VADODARA', 'FARIDABAD', 'GHAZIABAD',
        'GAZIABAD', 'INDORE', 'JABALPUR', 'JAMSHEDPUR', 'KANPUR', 'KOCHI',
        'MYSURU', 'MYSORE', 'NAGPUR', 'SURAT', 'VISAKHAPATNAM', 'VISHAKAPATNAM',
        'LUCKNOW', 'PATNA', 'BHOPAL', 'RAIPUR', 'RANCHI', 'DEHRADUN',
        'GANDHINAGAR', 'BHUBANESWAR', 'THIRUVANANTHAPURAM', 'TRIVANDRUM',
        'SHIMLA', 'SRINAGAR', 'ITANAGAR', 'GUWAHATI', 'KOHIMA', 'AIZAWL',
        'SHILLONG', 'AGARTALA', 'IMPHAL', 'GANGTOK', 'PANAJI', 'CHANDIGARH',
        'NASHIK', 'AURANGABAD', 'SOLAPUR', 'KOLHAPUR', 'AMRAVATI', 'NANDED',
        'JALGAON', 'AKOLA', 'LATUR',
        # Other common cities where Rite Water operates
        'VARANASI', 'PALGHAR', 'DHULE', 'AHMEDNAGAR', 'SATARA', 'SANGLI',
        'OSMANABAD', 'BULDHANA', 'WARDHA', 'YAVATMAL', 'GONDIA', 'BHANDARA',
        'CHANDRAPUR', 'GADCHIROLI', 'RAIGAD', 'RATNAGIRI', 'SINDHUDURG',
        'THANE', 'VASAI', 'VIRAR', 'MEERUT', 'MATHURA', 'ALIGARH',
        'BAREILLY', 'MORADABAD', 'GORAKHPUR', 'ALLAHABAD', 'PRAYAGRAJ',
        'JODHPUR', 'UDAIPUR', 'KOTA', 'AJMER', 'BIKANER',
        'GWALIOR', 'UJJAIN', 'SAGAR', 'REWA', 'SATNA', 'BILASPUR',
        'DURG', 'BHILAI', 'KORBA', 'BOKARO', 'DHANBAD', 'HAZARIBAGH',
        'MUZAFFARPUR', 'BHAGALPUR', 'GAYA', 'PURI', 'CUTTACK', 'ROURKELA',
        'BERHAMPUR', 'SAMBALPUR', 'COIMBATORE', 'MADURAI', 'TRICHY',
        'TIRUCHIRAPPALLI', 'SALEM', 'TIRUNELVELI', 'VELLORE', 'ERODE',
        'WARANGAL', 'KARIMNAGAR', 'NIZAMABAD', 'KHAMMAM', 'GUNTUR',
        'NELLORE', 'KURNOOL', 'TIRUPATI', 'RAJAHMUNDRY', 'VIJAYAWADA',
        'HUBLI', 'DHARWAD', 'MANGALORE', 'BELGAUM', 'BELLARY',
        'KOZHIKODE', 'KOLLAM', 'THRISSUR', 'PALAKKAD', 'ALAPPUZHA',
    ]

    def _search(txt):
        t = txt.upper()
        for c in KNOWN_CITIES:
            if c in t:
                return c.title()
        return None

    # 2. Search narration first (narration = destination intent)
    narration = voucher.get('narration') or ''
    if narration:
        found = _search(narration)
        if found:
            return found

    # 3. Fall back to cost_center text (may contain city even if skipped above)
    if cc:
        found = _search(cc)
        if found:
            return found

    return None


_DESIG_LABEL = {
    'DIRECTOR_CFO': 'Director / CFO',
    'VP_GM':        'VP / General Manager',
    'SR_MANAGER':   'Senior Manager',
    'MANAGER':      'Manager',
    'ASST_MANAGER': 'Asst. Manager',
    'SR_EXECUTIVE': 'Senior Executive',
    'TECHNICIAN':   'Technician',
}

def _desig_label(bucket):
    return _DESIG_LABEL.get(bucket, bucket.replace('_', ' ').title())

_GRADE_LABEL = {
    'A': 'Grade A (Metro / Major City)',
    'B': 'Grade B (Large City)',
    'C': 'Grade C (Other City / Town)',
}

# Proof date-match tolerance (days) per expense head.
# Tighter for receipts that should match the travel day; looser for hotel
# bills that are often settled after checkout.
_DATE_TOLERANCE = {
    'Hotel':           30,
    '1AC Train':        7, '2AC Train': 7, '3AC Train': 7,
    'Sleeper Train':    7, 'Train':     7,
    'Bus':              7, 'Flight':    7,
    'Cab':              3, 'Auto':      3,
    'Petrol':          14, 'Toll':     14, 'Parking': 14,
    'Food Allowance':  14,
    'Site Expense':    30, 'Vehicle Maintenance': 30,
}

# Maps designation bucket → key in site_deputation_30_plus_days.lodging_caps.
# DIRECTOR_CFO and VP_GM are not subject to deputation caps (they travel at actual).
_DEPUTATION_MAP = {
    'SR_MANAGER':   'PROJECT_MANAGER',
    'MANAGER':      'PROJECT_MANAGER',
    'ASST_MANAGER': 'ASST_MANAGER',
    'SR_EXECUTIVE': 'ENGINEER',
    'TECHNICIAN':   'TECHNICIAN',
}

# Designation buckets whose conveyance entitlement is four-wheeler (car rate).
_CAR_RATE_BUCKETS = {'VP_GM', 'SR_MANAGER'}


# -------- UPI + reference bill pairing --------

_UPI_KINDS  = {'upi_screenshot', 'payment_screenshot',
               'bank_transfer_screenshot', 'fastag_screenshot'}
_BILL_KINDS = {
    'hotel_bill_printed', 'hotel_bill_handwritten',
    'fuel_receipt', 'workshop_bill_printed', 'workshop_bill_handwritten',
    'food_receipt', 'purchase_invoice', 'receipt_photo',
    'train_ticket', 'bus_ticket', 'flight_ticket', 'cab_receipt', 'auto_receipt',
}


def _upi_bill_pairing(matched_proofs, all_proofs):
    """
    Return (is_paired, note) when matched proofs contain both a UPI/payment
    screenshot AND a reference bill with consistent amounts.

    A UPI screenshot proves *payment happened*; the reference bill proves *what
    was paid for*.  Together they satisfy the two-document proof standard for
    hotel, FASTag, hospitality, and similar expenses.
    """
    upi_proofs  = [p for p in matched_proofs if (p.get('kind') or '') in _UPI_KINDS]
    bill_proofs = [p for p in matched_proofs if (p.get('kind') or '') in _BILL_KINDS]

    if not upi_proofs or not bill_proofs:
        # Also check unmatched proofs in the full proof list (e.g. UPI with no date)
        all_upi  = [p for p in all_proofs if (p.get('kind') or '') in _UPI_KINDS]
        all_bill = [p for p in all_proofs  if (p.get('kind') or '') in _BILL_KINDS]
        if matched_proofs and all_upi and all_bill:
            upi_proofs  = all_upi[:1]
            bill_proofs = all_bill[:1]
        else:
            return False, ''

    # Check amount alignment between any UPI and any bill
    for u in upi_proofs:
        u_amt = u.get('amount') or u.get('matched_amount')
        for b in bill_proofs:
            b_amt = b.get('amount') or b.get('matched_amount')
            if u_amt and b_amt and abs(u_amt - b_amt) <= max(10, u_amt * 0.05):
                return True, (
                    f'UPI payment verified against reference bill: '
                    f'{u.get("file", "")} (Rs.{u_amt:,.0f}) matches '
                    f'{b.get("file", "")} (Rs.{b_amt:,.0f}).'
                )

    # Both types present even if amounts differ slightly — still strong evidence
    return True, (
        f'UPI/payment screenshot ({upi_proofs[0].get("file", "")}) and '
        f'reference bill ({bill_proofs[0].get("file", "")}) both present.'
    )


# -------- Group bill employee-share split --------

def _count_shared_employees(proof, master_names):
    """
    Count how many Rite Water employees are named on this proof document.

    Returns (count, [matched_name, ...]).
    master_names: frozenset of normalised uppercase employee names from the CSV.
    """
    persons = [_norm(p) for p in (proof.get('persons_named') or []) if p]
    if not persons:
        return 1, []

    matched = []
    for person in persons:
        pt = set(person.split())
        for master in master_names:
            mt = set(master.split())
            # At least 2 tokens overlap → treat as the same person
            if len(pt & mt) >= 2:
                matched.append(person.title())
                break

    matched = list(dict.fromkeys(matched))   # preserve order, deduplicate
    return max(1, len(matched)), matched


# -------- Per line-item evaluation --------

def evaluate_line(item, ctx):
    """Return a finding dict for one line item."""
    head = item['expense_head']
    claimed = item.get('claimed_inr') or 0
    reviewer_approved = item.get('approved_by_reviewer_inr') or 0
    designation = ctx['designation_bucket']
    desig_data = ctx['policy']['designations'][designation]
    grade = ctx['city_grade']
    proofs = ctx['proofs']

    finding = {
        'date': item['date'],
        'expense_head': head,
        'expense_head_raw': item.get('expense_head_raw', ''),
        'remarks': item.get('remarks', ''),
        'claimed_inr': claimed,
        'approved_by_reviewer_inr': reviewer_approved,
        'rejected_by_reviewer_inr': item.get('rejected_inr') or 0,
        'policy_eligible_inr': None,
        'policy_status': 'CONDITIONAL',
        'severity': 'LOW',
        'rule_applied': '',
        'proof_required': '',
        'proof_status': '',
        'matched_proofs': [],
        'audit_note': '',
        'recommended_action': '',
    }

    # Match proofs by date proximity and amount.  We compare against EVERY
    # candidate amount in the proof, since OCR often produces noise like
    # "214,970" (rupee symbol read as "2") alongside the real "14,970".
    date_tolerance = _DATE_TOLERANCE.get(head, 14)   # Gap 7: per-head tolerance
    matched = []
    for p in proofs:
        score = 0
        all_amounts = list(p.get('amounts') or [])
        if p.get('amount') is not None and p['amount'] not in all_amounts:
            all_amounts.append(p['amount'])
        # Date proximity: exact match scores 2; within tolerance scores 1
        proof_dates = p.get('dates') or []
        if item['date'] in proof_dates:
            score += 2
        elif claimed and proof_dates:
            try:
                from datetime import date
                idate = date.fromisoformat(item['date'])
                for pd in proof_dates:
                    pdate = date.fromisoformat(pd)
                    if abs((idate - pdate).days) <= date_tolerance:
                        score += 1
                        break
            except Exception:
                pass
        # Amount match against ANY candidate amount.
        # Tolerance: 5% or Rs.10 minimum — cash receipts and OCR rounding
        # often produce small deviations from the exact line-item amount.
        matched_amount = None
        if claimed:
            for a in all_amounts:
                if a and abs(a - claimed) <= max(10, claimed * 0.05):
                    score += 3
                    matched_amount = a
                    break
        if score > 0:
            matched.append({'file': p['file_name'], 'score': score,
                            'vendor': p.get('vendor'),
                            'amount': matched_amount or p.get('amount'),
                            'matched_amount': matched_amount,
                            'dates': p.get('dates'), 'fingerprint': p.get('fingerprint'),
                            'duplicate_of': p.get('duplicate_of')})
    matched.sort(key=lambda x: -x['score'])
    finding['matched_proofs'] = matched

    # Score 3 = amount match via UPI/payment screenshot — treat as strong proof.
    # Score 4+ = amount + date match (ideal). Both are acceptable.
    has_strong_proof = any(m['score'] >= 3 for m in matched)
    has_some_proof   = any(m['score'] >= 2 for m in matched)
    duplicate_used   = any(m.get('duplicate_of') for m in matched)

    # ---- UPI + reference bill pairing ----
    is_upi_paired, upi_pair_note = _upi_bill_pairing(matched, proofs)

    # ---- Group bill split ----
    # If any matched proof carries multiple Rite Water employee names, divide
    # the claimed amount equally so each employee is audited for their share.
    # We rebind `claimed` to the per-person share for all cap comparisons below;
    # `full_claimed` preserves the original for display purposes.
    full_claimed  = claimed
    split_divisor = 1
    split_names   = []
    if ctx.get('master_names'):
        for mp in matched:
            proof_rec = next((p for p in proofs if p['file_name'] == mp.get('file')), None)
            if proof_rec:
                n, names = _count_shared_employees(proof_rec, ctx['master_names'])
                if n > 1:
                    split_divisor = n
                    split_names   = names
                    break
    if split_divisor > 1:
        claimed = round(full_claimed / split_divisor, 2)

    # Hotel
    if head == 'Hotel':
        cap = desig_data['hotel'].get(grade)

        # Gap 4: nights inference — prefer OCR-extracted value, then try date-pair on hotel proofs
        nights = 1
        for p in proofs:
            n = p.get('nights')
            if n:
                try:
                    nights = max(nights, int(n))
                except ValueError:
                    pass
        if nights == 1:
            for p in proofs:
                if 'hotel' in (p.get('kind') or '').lower():
                    pdates = sorted(p.get('dates') or [])
                    if len(pdates) >= 2:
                        try:
                            d1 = datetime.date.fromisoformat(pdates[0])
                            d2 = datetime.date.fromisoformat(pdates[-1])
                            diff = abs((d2 - d1).days)
                            if 1 <= diff <= 30:
                                nights = max(nights, diff)
                        except Exception:
                            pass

        # Gap 5: site deputation — 30+ day trip → apply monthly lodging cap
        period_days = 0
        p_from_str = ctx.get('period_from')
        p_to_str   = ctx.get('period_to')
        if p_from_str and p_to_str:
            try:
                period_days = (datetime.date.fromisoformat(p_to_str) -
                               datetime.date.fromisoformat(p_from_str)).days + 1
            except Exception:
                period_days = 0

        dep_bracket  = _DEPUTATION_MAP.get(designation)
        use_deputation = dep_bracket and period_days > 30

        if cap == 'actual':
            finding['policy_eligible_inr'] = claimed
            finding['rule_applied'] = f'{_desig_label(designation)} grade: hotel expenses reimbursed at actual cost.'
        elif use_deputation:
            dep_caps    = ctx['policy'].get('site_deputation_30_plus_days', {}).get('lodging_caps', {})
            monthly_cap = dep_caps.get(dep_bracket, {}).get(grade, cap * 30)
            months      = period_days / 30
            eligible    = round(monthly_cap * months)
            finding['policy_eligible_inr'] = min(claimed, eligible)
            finding['rule_applied'] = (
                f'Site deputation rule (trip = {period_days} days > 30). '
                f'{_desig_label(designation)} monthly lodging cap: Rs.{monthly_cap:,} '
                f'({_GRADE_LABEL.get(grade, f"Grade {grade}")} city). '
                f'Eligible for {months:.1f} months: Rs.{eligible:,}.'
            )
            if claimed > eligible:
                finding['policy_status'] = 'FLAG'
                finding['severity'] = 'HIGH'
                finding['audit_note'] = (
                    f'Site deputation cap applied ({period_days} days). '
                    f'Hotel claimed Rs.{claimed:,.0f} exceeds monthly cap of '
                    f'Rs.{monthly_cap:,} × {months:.1f} months = Rs.{eligible:,}. '
                    f'Excess: Rs.{claimed - eligible:,.0f}.'
                )
                finding['recommended_action'] = (
                    f'Reduce hotel to Rs.{eligible:,} per deputation policy, '
                    f'or provide company guest-house exemption documentation.'
                )
        else:
            eligible = cap * nights
            finding['policy_eligible_inr'] = min(claimed, eligible)
            finding['rule_applied'] = (
                f'{_GRADE_LABEL.get(grade, f"Grade {grade}")} city: {_desig_label(designation)} grade hotel cap '
                f'is Rs.{cap:,} per night. Policy limit for {nights} night(s): Rs.{eligible:,}.'
            )
            if claimed > eligible:
                finding['policy_status'] = 'FLAG'
                finding['severity'] = 'HIGH'
                finding['audit_note'] = (
                    f'Hotel amount claimed (Rs.{claimed:,.0f}) exceeds the '
                    f'{_GRADE_LABEL.get(grade, f"Grade {grade}")} city cap of Rs.{eligible:,} '
                    f'for {_desig_label(designation)} grade. Excess amount: Rs.{claimed - eligible:,.0f}.'
                )
                finding['recommended_action'] = (
                    f'Reduce the hotel claim by Rs.{claimed - eligible:,.0f}, '
                    f'or obtain written approval confirming a higher entitlement.'
                )
        finding['proof_required'] = 'Hotel tax invoice + payment receipt'
        finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_some_proof else 'MISSING')

    # Food allowance
    elif head == 'Food Allowance':
        # Gap 6: checkout-day rule — on the last night of a hotel stay the employee
        # travels home, so no overnight stay → use the without-stay rate for that day.
        with_stay = ctx.get('has_hotel_stay', False)
        if with_stay and item.get('date') in ctx.get('hotel_checkout_dates', set()):
            with_stay = False
        rate = desig_data['food_with_stay'] if with_stay else desig_data['food_without_stay_over_12hr']
        trip_days_ctx    = ctx.get('food_trip_days', 1)
        food_trip_elig   = ctx.get('food_trip_elig', 0)
        food_total_clm   = ctx.get('food_total_claimed', claimed)

        if rate == 'actual':
            finding['policy_eligible_inr'] = claimed
            finding['rule_applied'] = f'{_desig_label(designation)} grade: food at actual cost permitted.'
        elif food_trip_elig == float('inf') or (food_trip_elig > 0 and food_total_clm <= food_trip_elig):
            # Total food claimed for the trip is within the trip-level entitlement → approve full amount
            finding['policy_eligible_inr'] = claimed
            stay_note = 'with overnight stay' if with_stay else 'without overnight stay'
            finding['rule_applied'] = (
                f'{_desig_label(designation)} grade: Rs.{rate:,}/day ({stay_note}) x '
                f'{trip_days_ctx} trip days = Rs.{food_trip_elig:,.0f} total entitlement. '
                f'Total food claimed (Rs.{food_total_clm:,.0f}) is within the trip limit.'
            )
            finding['policy_status'] = 'APPROVE'
            finding['severity'] = 'LOW'
            finding['audit_note'] = (
                f'Trip food entitlement: Rs.{rate:,}/day x {trip_days_ctx} days = '
                f'Rs.{food_trip_elig:,.0f}. Total food claimed: Rs.{food_total_clm:,.0f}. '
                f'Within policy limit — approved.'
            )
            finding['recommended_action'] = 'Approve as claimed.'
        else:
            finding['policy_eligible_inr'] = min(claimed, rate)
            stay_note = 'with overnight stay' if with_stay else 'without overnight stay (>12 hr trip)'
            finding['rule_applied'] = (f'{_desig_label(designation)} grade: daily food allowance capped at '
                                       f'Rs.{rate} ({stay_note}).')
            if claimed > rate:
                finding['policy_status'] = 'FLAG'
                finding['severity'] = 'HIGH' if claimed > rate * 1.3 else 'MEDIUM'
                finding['audit_note'] = (
                    f'Food allowance claimed (Rs.{claimed:,.0f}) exceeds the {_desig_label(designation)} '
                    f'grade daily rate of Rs.{rate:,}. Excess amount: Rs.{claimed - rate:,.0f}. '
                    f'A recovery or reduction is recommended.'
                )
                finding['recommended_action'] = f'Reduce food allowance to Rs.{rate:,} per policy.'
        finding['proof_required'] = 'Per-diem (no bills required) - eligible only with overnight stay proof'
        finding['proof_status'] = 'PER-DIEM (no proof needed)'

    # Train
    elif head in ('1AC Train', '2AC Train', '3AC Train', 'Sleeper Train', 'Train'):
        allowed_class = desig_data['travel_class']
        finding['rule_applied'] = (
            f'{_desig_label(designation)} grade is entitled to: {allowed_class}.'
        )
        finding['proof_required'] = 'PNR / e-ticket + fare receipt'
        finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_some_proof else 'MISSING')
        # Class allowed?
        ok_classes = []
        if 'actual' in allowed_class.lower():
            ok_classes = ['1AC Train', '2AC Train', '3AC Train', 'Sleeper Train', 'Train']
        elif '1ac' in allowed_class.lower() or '2ac' in allowed_class.lower():
            ok_classes = ['1AC Train', '2AC Train', '3AC Train', 'Sleeper Train']
        elif '3ac' in allowed_class.lower():
            ok_classes = ['3AC Train', 'Sleeper Train']
        else:
            ok_classes = ['Sleeper Train']
        if head not in ok_classes and head != 'Train':
            finding['policy_status'] = 'FLAG'
            finding['severity'] = 'MEDIUM'
            finding['audit_note'] = (
                f'{head} was claimed, but {_desig_label(designation)} grade is only entitled to '
                f'{allowed_class}. This class of travel requires a higher grade or written approval.'
            )
            finding['recommended_action'] = (
                'Claim the equivalent fare for the entitled class, '
                'or obtain written approval for the upgrade.'
            )
            finding['policy_eligible_inr'] = 0
        else:
            finding['policy_eligible_inr'] = claimed
            if not has_strong_proof:
                finding['policy_status'] = 'CONDITIONAL'
                finding['severity'] = 'MEDIUM'
                finding['audit_note'] = 'Please provide the PNR or e-ticket. An internal debit voucher alone is not sufficient as proof of travel.'

    # Bus
    elif head == 'Bus':
        finding['rule_applied'] = (
            f'{_desig_label(designation)} grade travel entitlement: {desig_data["travel_class"]}.'
        )
        finding['policy_eligible_inr'] = claimed
        finding['proof_required'] = 'Bus ticket'
        finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_some_proof else 'MISSING')

    # Flight
    elif head == 'Flight':
        if 'flight' in desig_data['travel_class'].lower():
            finding['policy_eligible_inr'] = min(claimed, 5000)
            finding['rule_applied'] = (
                'VP / General Manager grade and above: flight travel permitted up to Rs.5,000. '
                'For routes over 500 km or fares above Rs.5,000, prior management approval is required.'
            )
            if claimed > 5000:
                finding['policy_status'] = 'CONDITIONAL'
                finding['severity'] = 'MEDIUM'
                finding['audit_note'] = (
                    f'Flight fare of Rs.{claimed:,.0f} exceeds the Rs.5,000 limit. '
                    'Prior management approval is required before this amount can be reimbursed.'
                )
        else:
            finding['policy_status'] = 'FLAG'
            finding['severity'] = 'HIGH'
            finding['audit_note'] = (
                f'{_desig_label(designation)} grade is not entitled to flight travel under the current policy.'
            )
            finding['policy_eligible_inr'] = 0
            finding['recommended_action'] = 'Reject this claim, or escalate to VP / GM level for exceptional approval.'
        finding['proof_required'] = 'E-ticket / boarding pass'

    # Cab/Auto/Other transport
    elif head in ('Cab', 'Auto'):
        finding['rule_applied'] = (f'{_desig_label(designation)} grade: local conveyance by '
                                   f'{desig_data["conveyance"]} permitted.')
        finding['policy_eligible_inr'] = claimed
        finding['proof_required'] = 'Cab/auto receipt with route, ride ID, amount, date'
        if duplicate_used:
            finding['policy_status'] = 'REJECT'
            finding['severity'] = 'MEDIUM'
            finding['audit_note'] = 'Duplicate receipt detected (already submitted in another line).'
            finding['policy_eligible_inr'] = 0
            finding['recommended_action'] = 'Reject duplicate'
        else:
            finding['policy_status'] = 'APPROVE' if has_strong_proof else 'CONDITIONAL'
            finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_some_proof else 'MISSING')

    # Petrol / Fuel
    elif head == 'Petrol':
        bike = ctx['policy']['petrol_charges']['bike_per_km']
        car  = ctx['policy']['petrol_charges']['car_per_km']

        # Gap 2: select rate from designation's conveyance entitlement.
        # Buckets explicitly entitled to a 4-wheeler pay the car rate (Rs.9/km).
        # All others (including ambiguous "2 wheeler / 4 wheeler") default to bike (Rs.3/km).
        if designation in _CAR_RATE_BUCKETS:
            rate_per_km  = car
            vehicle_type = 'four-wheeler'
        else:
            rate_per_km  = bike
            vehicle_type = 'two-wheeler'

        finding['proof_required'] = 'Odometer reading screenshots (start + end) or petrol slip with km log'

        def _odo_km(proof_list):
            km = 0
            for p in proof_list:
                s, e = p.get('odometer_start'), p.get('odometer_end')
                if s is not None and e is not None and isinstance(s, (int, float)) \
                        and isinstance(e, (int, float)) and e > s:
                    km += int(e) - int(s)
            return km

        per_item_km = _odo_km(matched)

        all_petrol_claims    = [li for li in ctx.get('all_line_items', []) if li.get('expense_head') == 'Petrol']
        total_petrol_claimed = sum(li.get('claimed_inr') or 0 for li in all_petrol_claims)
        n_petrol             = len(all_petrol_claims) or 1
        share                = claimed / total_petrol_claimed if total_petrol_claimed else (1 / n_petrol)

        if per_item_km > 0:
            # Odometer photos matched directly to this line item
            eligible = per_item_km * rate_per_km
            finding['policy_eligible_inr'] = min(claimed, eligible)
            finding['rule_applied'] = (
                f'{vehicle_type.title()} reimbursement at Rs.{rate_per_km}/km. '
                f'Distance from odometer readings: {per_item_km} km. '
                f'Policy-eligible: Rs.{eligible:,.0f}.'
            )
            finding['proof_status'] = 'OK'
            if claimed > eligible:
                finding['policy_status'] = 'FLAG'
                finding['severity'] = 'MEDIUM'
                finding['audit_note'] = (
                    f'Claimed Rs.{claimed:,.0f} exceeds odometer-verified eligible amount of '
                    f'Rs.{eligible:,.0f} ({per_item_km} km × Rs.{rate_per_km}/km). '
                    f'Excess: Rs.{claimed - eligible:,.0f}.'
                )
                finding['recommended_action'] = f'Reduce to Rs.{eligible:,.0f} per odometer readings.'
        else:
            # No per-item odometer — try voucher-level odometer aggregate first
            all_odo_km    = _odo_km(proofs)
            attributed_km = round(all_odo_km * share)

            if all_odo_km > 0:
                line_eligible = min(claimed, attributed_km * rate_per_km)
                finding['policy_eligible_inr'] = line_eligible
                finding['rule_applied'] = (
                    f'{vehicle_type.title()} reimbursement at Rs.{rate_per_km}/km. '
                    f'Voucher-level odometer total: {all_odo_km} km '
                    f'(eligible total: Rs.{all_odo_km * rate_per_km:,}). '
                    f'This line attributed {attributed_km} km proportionally.'
                )
                finding['proof_status'] = 'OK'
                if claimed > line_eligible:
                    finding['policy_status'] = 'FLAG'
                    finding['severity'] = 'MEDIUM'
                    finding['audit_note'] = (
                        f'Claimed Rs.{claimed:,.0f} exceeds odometer-attributed eligible amount of '
                        f'Rs.{line_eligible:,.0f} ({attributed_km} km × Rs.{rate_per_km}/km). '
                        f'Total odometer: {all_odo_km} km; total petrol claimed: Rs.{total_petrol_claimed:,.0f}; '
                        f'total eligible: Rs.{all_odo_km * rate_per_km:,}.'
                    )
                    finding['recommended_action'] = (
                        f'Total petrol claim should not exceed Rs.{all_odo_km * rate_per_km:,} '
                        f'per odometer readings ({all_odo_km} km × Rs.{rate_per_km}/km).'
                    )
            else:
                # Gap 1: no odometer photos — fall back to Unolo GPS data for the claim period
                unolo_km  = 0.0
                emp_code  = ctx.get('employee_code', '')
                p_from    = ctx.get('period_from')
                p_to      = ctx.get('period_to')
                if emp_code and p_from and p_to:
                    try:
                        unolo_km = km_for(_UNOLO, emp_code, p_from, p_to)
                    except Exception:
                        unolo_km = 0.0

                if unolo_km > 0:
                    attributed_km = round(unolo_km * share)
                    line_eligible = min(claimed, attributed_km * rate_per_km)
                    finding['policy_eligible_inr'] = line_eligible
                    finding['rule_applied'] = (
                        f'{vehicle_type.title()} reimbursement at Rs.{rate_per_km}/km '
                        f'(Unolo GPS used — no odometer photos submitted). '
                        f'GPS distance for claim period: {unolo_km:.0f} km. '
                        f'This line attributed {attributed_km} km proportionally.'
                    )
                    finding['proof_status'] = 'OK'
                    if claimed > line_eligible:
                        finding['policy_status'] = 'FLAG'
                        finding['severity'] = 'MEDIUM'
                        finding['audit_note'] = (
                            f'Claimed Rs.{claimed:,.0f} exceeds Unolo GPS-attributed eligible amount of '
                            f'Rs.{line_eligible:,.0f} ({attributed_km} km × Rs.{rate_per_km}/km). '
                            f'Excess: Rs.{claimed - line_eligible:,.0f}.'
                        )
                        finding['recommended_action'] = (
                            f'Reduce to Rs.{line_eligible:,.0f} per Unolo GPS data, '
                            f'or submit odometer photos covering the full trip.'
                        )
                else:
                    # No odometer and no Unolo GPS — approve with a MEDIUM advisory flag
                    finding['policy_eligible_inr'] = claimed
                    finding['policy_status']       = 'CONDITIONAL'
                    finding['severity']            = 'MEDIUM'
                    finding['rule_applied'] = (
                        f'Fuel reimbursement: Rs.{bike}/km two-wheeler, Rs.{car}/km four-wheeler. '
                        'Amount conditionally approved — no GPS or odometer data available.'
                    )
                    finding['proof_status'] = 'MISSING'
                    finding['audit_note'] = (
                        'No odometer proof or Unolo GPS data found for this claim period. '
                        'Admin should request odometer readings (start + end km) before approving.'
                    )
                    finding['recommended_action'] = (
                        'Hold until employee provides odometer screenshots or trip log.'
                    )

    # Toll / FASTag
    elif head in ('Toll', 'Parking'):
        finding['rule_applied'] = 'Actual toll/parking charges with receipt'
        finding['policy_eligible_inr'] = claimed
        finding['proof_required'] = 'FASTag/toll receipt'
        finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_some_proof else 'MISSING')

    # Cruise / Hospitality - special handling
    elif head == 'Cruise':
        finding['rule_applied'] = ctx['policy']['category_rules']['hospitality_entertainment']['description']
        finding['policy_status'] = 'CONDITIONAL'
        finding['severity'] = 'HIGH'
        finding['audit_note'] = ('Hospitality/entertainment expense not covered under standard travel policy. '
                                 'Requires VP/GM pre-approval. Cap Rs.5,000/month under Other Expense.')
        finding['policy_eligible_inr'] = min(claimed, 5000) if desig_data['level'] >= 90 else 0
        finding['proof_required'] = 'Receipt + senior management approval letter'
        finding['proof_status'] = 'OK' if has_strong_proof else 'WEAK'
        finding['recommended_action'] = 'Verify VP/GM pre-approval letter is on file before payment'

    # Site Expense / Vehicle Maintenance
    # Reimbursed at actual cost. Any proof uploaded for the period is sufficient
    # to approve — amount matching is not required because site expenses are
    # often paid in cash with bundled receipts that show a different total.
    elif head in ('Site Expense', 'Vehicle Maintenance'):
        finding['rule_applied'] = ('Site/vehicle expense for company-owned asset. Requires legible bill + '
                                   'payment proof + asset reference (vehicle no., site code).')
        finding['policy_eligible_inr'] = claimed
        finding['proof_required'] = 'Workshop/vendor tax invoice + payment receipt + vehicle/asset reference'
        # Any proof in the ZIP counts as sufficient for Site Expense.
        # Amount matching is not required — site costs are often paid in cash
        # with bundled receipts that don't map 1-to-1 to individual line items.
        has_any_proof = len(proofs) > 0
        finding['proof_status'] = 'OK' if has_any_proof else 'MISSING'
        if has_any_proof:
            finding['policy_status'] = 'APPROVE'
            finding['severity'] = 'LOW'
        else:
            finding['policy_status'] = 'CONDITIONAL'
            finding['severity'] = 'MEDIUM'
            finding['audit_note'] = 'No proof attached. Please submit bill or payment receipt.'
            finding['recommended_action'] = 'Attach workshop/vendor bill or payment screenshot'
        # Manager approval threshold
        if claimed > ctx['policy']['manager_approval_threshold_inr']:
            threshold = ctx['policy']['manager_approval_threshold_inr']
            finding['audit_note'] = (finding['audit_note'] + ' ').strip() + (
                f' The claim amount of Rs.{claimed:,.0f} exceeds the Rs.{threshold:,} threshold '
                'that requires senior management approval.'
            )

    # Mobile / internet recharge
    elif head == 'Mobile Recharge':
        _MOBILE_CAP = 500
        finding['rule_applied'] = (
            f'Work mobile/internet recharge — eligible for work-purpose SIM only, '
            f'max Rs.{_MOBILE_CAP}/month. Personal numbers not reimbursable.'
        )
        finding['policy_eligible_inr'] = min(claimed, _MOBILE_CAP)
        finding['proof_required'] = 'Telecom receipt / recharge confirmation with number'
        finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_some_proof else 'MISSING')
        if claimed > _MOBILE_CAP:
            finding['policy_status'] = 'FLAG'
            finding['severity'] = 'MEDIUM'
            finding['audit_note'] = (
                f'Mobile recharge capped at Rs.{_MOBILE_CAP}/month. '
                f'Claimed Rs.{claimed:,.0f} exceeds limit by Rs.{claimed - _MOBILE_CAP:,.0f}.'
            )
            finding['recommended_action'] = f'Reduce to Rs.{_MOBILE_CAP}/month per policy.'
        if not has_strong_proof:
            finding['policy_status'] = 'CONDITIONAL'
            finding['audit_note'] = (finding.get('audit_note', '') + ' ').strip() + (
                ' Provide telecom receipt confirming work number and recharge amount.'
            )

    # Other / fallback
    else:
        finding['rule_applied'] = 'Other expense - actual, monthly cap Rs.5,000, receipt mandatory'
        finding['policy_eligible_inr'] = min(claimed, 5000)
        finding['proof_required'] = 'Original receipt'
        # Accept any proof present (same rationale as Site Expense — cash receipts
        # are often bundled and may not match individual line-item amounts exactly)
        has_any_proof = len(proofs) > 0
        finding['proof_status'] = 'OK' if has_strong_proof else ('WEAK' if has_any_proof else 'MISSING')
        if not has_any_proof:
            finding['policy_status'] = 'CONDITIONAL'
            finding['severity'] = 'MEDIUM'
            finding['audit_note'] = 'No proof attached. Please submit original receipt.'
            finding['recommended_action'] = 'Attach original bill or payment receipt'
        if claimed > 5000:
            finding['policy_status'] = 'FLAG'
            finding['severity'] = 'MEDIUM'
            finding['audit_note'] = (finding['audit_note'] + ' ').strip() + \
                ' Other-expense category capped at Rs.5,000/month.'

    # Default status fall-through
    if finding['policy_status'] == 'CONDITIONAL' and finding['policy_eligible_inr'] is None:
        finding['policy_eligible_inr'] = claimed

    # Policy-eligible must never exceed what the Project Head approved for this line.
    # Reviewer's rejection is authoritative — we can flag it but cannot pay more.
    finding['policy_eligible_inr'] = min(finding['policy_eligible_inr'] or 0, reviewer_approved)

    # Project Head approved vs policy-eligible delta
    eligible = finding['policy_eligible_inr'] or 0
    if reviewer_approved > eligible + 1:
        finding['audit_note'] = (finding['audit_note'] + ' ').strip() + (
            f' The amount approved by the Project Head (Rs.{reviewer_approved:,.0f}) exceeds '
            f'the policy-eligible amount of Rs.{eligible:,.0f}. A recovery or reduction is recommended.'
        )
        if finding['severity'] == 'LOW':
            finding['severity'] = 'MEDIUM'

    # ---- Post-process: UPI + reference bill pairing ----
    # When both a payment screenshot and a reference bill are present, the
    # two-document standard is satisfied: upgrade proof status and lift any
    # CONDITIONAL that was purely due to missing/weak proof (not cap breach).
    if is_upi_paired:
        if finding.get('proof_status') in ('WEAK', 'MISSING', ''):
            finding['proof_status'] = 'OK'
        if finding['policy_status'] == 'CONDITIONAL' and finding['severity'] in ('LOW', 'MEDIUM'):
            finding['policy_status'] = 'APPROVE'
            finding['severity']      = 'LOW'
        prefix = f'[UPI + Bill Verified] {upi_pair_note}' if upi_pair_note else '[UPI + Bill Verified]'
        finding['audit_note'] = (prefix + ' ' + (finding.get('audit_note') or '')).strip()

    # ---- Post-process: group bill split ----
    if split_divisor > 1:
        split_note = (
            f'[Group Bill ÷{split_divisor}] '
            f'{split_divisor} Rite Water employees on this bill '
            f'({", ".join(split_names)}). '
            f'Full bill Rs.{full_claimed:,.0f} ÷ {split_divisor} = '
            f'this employee\'s share Rs.{claimed:,.0f}.'
        )
        finding['audit_note'] = (split_note + ' ' + (finding.get('audit_note') or '')).strip()
        finding['split_divisor']   = split_divisor
        finding['split_names']     = split_names
        finding['full_claimed_inr'] = full_claimed

    return finding


# -------- Voucher-level checks --------

def voucher_level_checks(voucher, findings, policy, history):
    breaches = []
    # Period check
    pf, pt = voucher.get('period_from'), voucher.get('period_to')
    if pf and pt:
        d1 = datetime.date.fromisoformat(pf)
        d2 = datetime.date.fromisoformat(pt)
        days = (d2 - d1).days + 1
        if days > policy['max_claim_period_days']:
            breaches.append({
                'severity': 'MEDIUM',
                'clause': (f'This voucher covers {days} days, which exceeds the '
                           f'{policy["max_claim_period_days"]}-day maximum claim period.'),
                'entries': 'Whole voucher',
                'amount_at_risk': voucher.get('gross_claimed') or 0,
            })
    # Manager approval threshold
    gross = voucher.get('gross_approved_by_reviewer') or voucher.get('gross_claimed') or 0
    if gross > policy['manager_approval_threshold_inr']:
        breaches.append({
            'severity': 'LOW',
            'clause': (f'Total voucher amount of Rs.{gross:,.0f} exceeds the '
                       f'Rs.{policy["manager_approval_threshold_inr"]:,} limit -- '
                       f'senior management approval must be on file before payment.'),
            'entries': 'Whole voucher',
            'amount_at_risk': gross,
        })
    # Duplicate proofs
    dup_files = [m for f in findings for m in f['matched_proofs'] if m.get('duplicate_of')]
    if dup_files:
        breaches.append({
            'severity': 'MEDIUM',
            'clause': 'Duplicate proof file(s) detected: ' + ', '.join({m['file'] for m in dup_files}),
            'entries': 'Affected lines flagged individually',
            'amount_at_risk': sum((m.get('amount') or 0) for m in dup_files),
        })
    # Gap 3: Other Expense / Office Expense aggregate monthly cap (Rs.5,000)
    _OTHER_CAP = 5000
    other_lines = [f for f in findings if f['expense_head'] in ('Other Expense', 'Office Expense')]
    total_other = sum(f.get('claimed_inr') or 0 for f in other_lines)
    if len(other_lines) > 1 and total_other > _OTHER_CAP:
        breaches.append({
            'severity': 'MEDIUM',
            'clause': (
                f'Total "Other/Office Expense" across {len(other_lines)} lines is '
                f'Rs.{total_other:,.0f}, which exceeds the monthly cap of Rs.{_OTHER_CAP:,}. '
                f'Each line was within its individual cap but the aggregate is over-limit.'
            ),
            'entries': ', '.join(f"{f['expense_head']} {f['date']}" for f in other_lines),
            'amount_at_risk': total_other - _OTHER_CAP,
        })

    # High-severity findings
    high_findings = [f for f in findings if f['severity'] == 'HIGH']
    for f in high_findings:
        breaches.append({
            'severity': 'HIGH',
            'clause': f"{f['expense_head']} on {f['date']}: {f['audit_note']}".strip(),
            'entries': f"{f['expense_head']} {f['date']}",
            'amount_at_risk': max(0, (f['claimed_inr'] or 0) - (f['policy_eligible_inr'] or 0)),
        })
    return breaches


def _hotel_checkout_dates(proofs):
    """Return a set of ISO date strings that are the end-date of a hotel stay.

    When a hotel bill proof contains two or more dates (e.g. check-in and
    check-out), the latest date is the checkout date.  Food lines on that date
    should use the without-stay rate (Gap 6).
    """
    dates = set()
    for p in proofs:
        if 'hotel' in (p.get('kind') or '').lower():
            pdates = sorted(p.get('dates') or [])
            if len(pdates) >= 2:
                dates.add(pdates[-1])
    return dates


def history_patterns(employee_code, expense_heads, history):
    """Surface short notes from past audit decisions for the same employee/heads."""
    notes = []
    for h in history:
        if h.get('employee_code') == employee_code:
            for past in (h.get('decisions') or []):
                if past.get('expense_head') in expense_heads:
                    notes.append(
                        f"Past voucher #{h.get('voucher_no')}: {past.get('expense_head')} "
                        f"claimed Rs.{past.get('claimed')}, admin finalized Rs.{past.get('admin_final')} "
                        f"({past.get('decision_pattern_note', '')})")
    return notes[:8]


# -------- Main orchestration --------

def run_audit(voucher_pdf, proofs_zip, proofs_data=None, voucher_data=None, emp_code_hint=None):
    if voucher_data is not None:
        voucher = voucher_data
        if emp_code_hint and not voucher.get('employee_code'):
            voucher['employee_code'] = emp_code_hint
    elif voucher_pdf is not None:
        # extract_document uses Claude Vision; fall back to plain Spine HR
        # extraction if the anthropic SDK isn't available.
        try:
            from extract_document import is_spine_hr_voucher, extract_from_document, \
                                         build_voucher_stub, build_proof_entry
            _has_vision = True
        except ImportError as _e:
            print(f"  [audit_engine] extract_document unavailable ({_e}); "
                  "assuming Spine HR voucher.", file=sys.stderr)
            _has_vision = False
        if not _has_vision or is_spine_hr_voucher(voucher_pdf):
            voucher = extract_voucher(voucher_pdf)
        else:
            print(f"  [auto-detect] '{os.path.basename(voucher_pdf)}' is not a Spine HR voucher — "
                  "using Claude vision extraction.", file=sys.stderr)
            extracted = extract_from_document(voucher_pdf)
            voucher   = build_voucher_stub(extracted, voucher_pdf, emp_code=emp_code_hint)
            if proofs_data is None:
                proofs_data = {'proofs': [build_proof_entry(voucher_pdf, extracted)]}
    else:
        raise ValueError("Either voucher_pdf or voucher_data must be provided.")

    if proofs_data is not None:
        proof_data = proofs_data
    else:
        proof_data = index_proofs(proofs_zip) if proofs_zip and os.path.exists(proofs_zip) else {'proofs': []}
    proofs = proof_data['proofs']

    policy = load_policy()
    master = load_employee_master()
    history = load_history()

    code = voucher.get('employee_code')
    emp = master.get(code, {})
    designation_text = emp.get('Designation') or ''
    bucket, desig_note = map_designation(designation_text, policy)

    city = derive_city_from_voucher(voucher)
    grade, city_note = city_grade(city, policy)

    has_hotel_stay = any(li['expense_head'] == 'Hotel' for li in voucher['line_items'])

    # Trip-level food allowance entitlement: rate/day × trip_days
    _food_desig    = policy.get('designations', {}).get(bucket, {})
    _food_rate     = _food_desig.get('food_without_stay_over_12hr', 0)
    _pf, _pt       = voucher.get('period_from'), voucher.get('period_to')
    _trip_days     = 1
    if _pf and _pt:
        try:
            _trip_days = max(1, (datetime.date.fromisoformat(_pt) - datetime.date.fromisoformat(_pf)).days + 1)
        except Exception:
            pass
    if isinstance(_food_rate, str) and _food_rate.lower() == 'actual':
        _food_trip_elig = float('inf')
    else:
        _food_trip_elig = (_food_rate or 0) * _trip_days
    # Deduplicate by date: take the max claimed per food date (avoids duplicate rows inflating total)
    _food_by_date = {}
    for _li in voucher['line_items']:
        if _li.get('expense_head') == 'Food Allowance':
            _dt = _li.get('date', '')
            _food_by_date[_dt] = max(_food_by_date.get(_dt, 0), _li.get('claimed_inr') or 0)
    _food_total_claimed = sum(_food_by_date.values())

    ctx = {
        'policy':               policy,
        'designation_bucket':   bucket,
        'city_grade':           grade,
        'proofs':               proofs,
        'has_hotel_stay':       has_hotel_stay,
        'all_line_items':       voucher['line_items'],
        # Gap 1 & 5: employee + period needed for Unolo GPS and deputation check
        'employee_code':        code,
        'period_from':          voucher.get('period_from'),
        'period_to':            voucher.get('period_to'),
        # Gap 6: checkout dates for food allowance rate selection
        'hotel_checkout_dates': _hotel_checkout_dates(proofs),
        # Trip-level food entitlement (rate/day × trip_days)
        'food_trip_days':        _trip_days,
        'food_trip_elig':        _food_trip_elig,
        'food_total_claimed':    _food_total_claimed,
        # Group bill split: normalised set of all employee names for name matching
        'master_names': frozenset(
            _norm(row.get('Name') or '') for row in master.values() if row.get('Name')
        ),
    }

    findings = [evaluate_line(li, ctx) for li in voucher['line_items']]
    breaches = voucher_level_checks(voucher, findings, policy, history)
    expense_heads = sorted({f['expense_head'] for f in findings})
    past_notes = history_patterns(code, expense_heads, history)

    total_claimed = sum((f['claimed_inr'] or 0) for f in findings)
    total_reviewer = sum((f['approved_by_reviewer_inr'] or 0) for f in findings)
    total_eligible = sum((f['policy_eligible_inr'] or 0) for f in findings)

    return {
        'audit_metadata': {
            'audit_reference': f'AUD-POL-{voucher.get("voucher_no") or "X"}-{datetime.date.today().year}',
            'report_date': datetime.date.today().isoformat(),
            'policy_version': policy['policy_name'] + ' (effective ' + policy['effective_date'] + ')',
        },
        'voucher': voucher,
        'employee': {
            'code': code,
            'name_master': emp.get('Name'),
            'name_voucher': voucher.get('employee_name'),
            'designation_master': designation_text,
            'designation_bucket': bucket,
            'designation_note': desig_note,
            'department': emp.get('Department'),
            'branch': emp.get('Branch'),
            'name_match': _name_match(emp.get('Name'), voucher.get('employee_name')),
            'name_match_note': _name_match_note(emp.get('Name'), voucher.get('employee_name')),
            'voucher_fingerprint': _voucher_fingerprint(voucher),
        },
        'city_grade': {
            'derived_city': city,
            'grade': grade,
            'note': city_note,
        },
        'proofs': proofs,
        'findings': findings,
        'breaches': breaches,
        'history_notes': past_notes,
        'totals': {
            'gross_claimed': voucher.get('gross_claimed') or total_claimed,
            'reviewer_approved': voucher.get('gross_approved_by_reviewer') or total_reviewer,
            'reviewer_rejected': voucher.get('gross_rejected') or 0,
            'policy_eligible': total_eligible,
            'recommended_hold': max(0, (voucher.get('gross_approved_by_reviewer') or total_reviewer) - total_eligible),
        },
        'recommendations': build_recommendations(findings, breaches, ctx, voucher),
        'conclusion': build_conclusion(findings, breaches, ctx, voucher, total_eligible),
        'claude_verdict': None,   # filled in below
    }

    print("  Running Claude AI reasoning pass (Sonnet)...", file=sys.stderr)
    audit_dict['claude_verdict'] = claude_final_verdict(audit_dict)
    if audit_dict['claude_verdict']:
        print("  Claude verdict: "
              + audit_dict['claude_verdict'].get('overall_recommendation', '?'),
              file=sys.stderr)
    else:
        print("  Claude verdict unavailable (will be omitted from PDF).", file=sys.stderr)

    return audit_dict


def build_recommendations(findings, breaches, ctx, voucher):
    recs = []
    if any(b['severity'] == 'HIGH' for b in breaches):
        recs.append({'priority': 'IMMEDIATE',
                     'text': 'Hold disbursement on flagged HIGH-severity items until breach is resolved.'})
    weak_proofs = [f for f in findings if 'WEAK' in (f.get('proof_status') or '') or 'MISSING' in (f.get('proof_status') or '')]
    if weak_proofs:
        names = ', '.join({f['expense_head'] for f in weak_proofs})
        recs.append({'priority': 'IMMEDIATE',
                     'text': f'Strengthen proof documents for: {names}. Request original/legible scans where missing.'})
    if any(f['expense_head'] == 'Hotel' for f in findings):
        recs.append({'priority': 'SHORT-TERM',
                     'text': 'Confirm employee designation from HR records. Hotel & food caps depend on grade.'})
    if any(b['severity'] == 'LOW' and 'manager-approval' in b['clause'].lower() for b in breaches):
        recs.append({'priority': 'SHORT-TERM',
                     'text': 'Confirm senior management approval is documented for above-threshold voucher.'})
    if any(m.get('duplicate_of') for f in findings for m in f['matched_proofs']):
        recs.append({'priority': 'CONFIRMED',
                     'text': 'Duplicate proof submissions detected and rejected. Flag employee for review.'})
    recs.append({'priority': 'LONG-TERM',
                 'text': 'Maintain admin decision log so the audit engine can learn patterns over future vouchers.'})
    return recs


def build_conclusion(findings, breaches, ctx, voucher, total_eligible):
    h = sum(1 for b in breaches if b['severity'] == 'HIGH')
    m = sum(1 for b in breaches if b['severity'] == 'MEDIUM')
    l = sum(1 for b in breaches if b['severity'] == 'LOW')
    gross = voucher.get('gross_claimed') or 0
    reviewer = voucher.get('gross_approved_by_reviewer') or 0
    hold = max(0, reviewer - total_eligible)
    return (f'Voucher {voucher.get("voucher_no")} submitted by {voucher.get("employee_name")} '
            f'for "{voucher.get("narration") or "travel expenses"}" has been reviewed against the '
            f'Rite Water Solutions Tour & Travel Policy. '
            f'The audit identified {len(breaches)} concern(s): {h} High severity, {m} Medium, {l} Low. '
            f'Gross amount claimed: Rs.{gross:,.0f}. '
            f'Amount approved by Project Head: Rs.{reviewer:,.0f}. '
            f'Policy-eligible amount (per this audit): Rs.{total_eligible:,.0f}. '
            + (f'Recommended hold pending resolution: Rs.{hold:,.0f}.'
               if hold > 0 else 'No hold recommended -- all amounts are within policy limits.'))


# -------- Claude AI reasoning layer --------

_VERDICT_TOOL = {
    "name": "give_audit_verdict",
    "description": "Provide a final independent audit verdict on the expense voucher.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overall_recommendation": {
                "type": "string",
                "enum": ["APPROVE", "APPROVE_WITH_DEDUCTIONS", "HOLD_FOR_CLARIFICATION", "REJECT"],
                "description": (
                    "APPROVE: all items within policy, proofs sufficient. "
                    "APPROVE_WITH_DEDUCTIONS: some items exceed policy or lack proof — pay reduced amount. "
                    "HOLD_FOR_CLARIFICATION: key documents missing, employee must respond before payment. "
                    "REJECT: clear policy violations or fraudulent indicators."
                )
            },
            "confidence": {
                "type": "string",
                "enum": ["HIGH", "MEDIUM", "LOW"],
                "description": "Confidence level given available evidence and proof quality."
            },
            "recommended_payable_inr": {
                "type": ["number", "null"],
                "description": "Recommended final payable amount in INR after deductions. Null if REJECT."
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "2-4 sentences of plain-English reasoning. Cite specific items, amounts, "
                    "and policy rules. Be direct and actionable."
                )
            },
            "key_concerns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific concerns to flag (max 5). Empty list if no concerns."
            },
            "items_to_approve": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expense heads that can be approved at claimed amount."
            },
            "items_to_reduce": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "expense_head": {"type": "string"},
                        "claimed_inr":      {"type": "number"},
                        "recommended_inr":  {"type": "number"},
                        "reason":           {"type": "string"}
                    },
                    "required": ["expense_head", "claimed_inr", "recommended_inr", "reason"]
                },
                "description": "Items to approve at a reduced amount with reason."
            },
            "items_to_reject": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expense heads to reject, with brief reason appended."
            },
            "admin_action_required": {
                "type": "string",
                "description": (
                    "Specific action the admin must take. E.g. 'Request hotel tax invoice from employee' "
                    "or 'Approve — no action needed'. Be concrete."
                )
            }
        },
        "required": [
            "overall_recommendation", "confidence", "reasoning",
            "key_concerns", "admin_action_required"
        ]
    }
}

_VERDICT_SYSTEM = (
    "You are an independent internal auditor for Rite Water Solutions reviewing an employee "
    "expense voucher. You will receive:\n"
    "1. The complete Tour & Travel Policy (JSON above)\n"
    "2. A structured audit summary with rule-engine findings\n\n"
    "Your task: give a final, independent audit verdict using the give_audit_verdict tool.\n\n"
    "Guidelines:\n"
    "- Cross-check claimed amounts against policy caps for the employee's designation and city grade\n"
    "- Assess whether submitted proofs are sufficient for each expense type\n"
    "- Flag any patterns that suggest padding, duplication, or policy circumvention\n"
    "- Approve what is legitimate; reduce what exceeds caps; hold/reject what lacks proof or violates policy\n"
    "- Be fair but firm. Give a clear, actionable recommendation.\n"
    "- Recommended payable should equal sum of items_to_approve + sum of items_to_reduce.recommended_inr"
)


def _fmt_audit_for_verdict(audit):
    """Produce a concise text summary of the audit dict for the Claude verdict prompt."""
    v    = audit['voucher']
    emp  = audit['employee']
    cg   = audit['city_grade']
    tots = audit['totals']

    lines = [
        "=== VOUCHER SUMMARY ===",
        f"Voucher No: {v.get('voucher_no')}  |  Period: {v.get('period_from')} to {v.get('period_to')}",
        f"Employee: {v.get('employee_name')} ({emp.get('code')})  |  Designation: {emp.get('designation_master')} -> {emp.get('designation_bucket')}",
        f"City: {cg.get('derived_city') or '(unknown)'}  |  Grade: {cg.get('grade')}  |  {cg.get('note')}",
        f"Narration: {v.get('narration') or '(none)'}",
        f"Claimed: Rs.{tots.get('gross_claimed', 0):,.0f}  |  Reviewer Approved: Rs.{tots.get('reviewer_approved', 0):,.0f}  |  Rule-engine Eligible: Rs.{tots.get('policy_eligible', 0):,.0f}",
        "",
        "=== LINE ITEMS & FINDINGS ===",
    ]
    for f in audit['findings']:
        lines.append(
            f"[{f['policy_status']} / {f['severity']}] {f['expense_head']} on {f['date']}: "
            f"Claimed Rs.{f.get('claimed_inr', 0):,.0f}, Eligible Rs.{f.get('policy_eligible_inr') or 0:,.0f}"
        )
        if f.get('rule_applied'):
            lines.append(f"  Rule: {f['rule_applied']}")
        lines.append(f"  Proof: {f.get('proof_status') or '(not checked)'}  |  Matched: {len(f.get('matched_proofs') or [])}")
        if f.get('audit_note'):
            lines.append(f"  Note: {f['audit_note']}")

    if audit.get('breaches'):
        lines.append("")
        lines.append("=== POLICY BREACHES ===")
        for b in audit['breaches']:
            lines.append(f"[{b['severity']}] {b['clause']}  (Amount at risk: Rs.{b.get('amount_at_risk', 0):,.0f})")

    proofs = audit.get('proofs') or []
    if proofs:
        lines.append("")
        lines.append(f"=== PROOFS SUBMITTED ({len(proofs)} file(s)) ===")
        for p in proofs:
            parts = [p['kind']]
            if p.get('vendor'):    parts.append(f"vendor={p['vendor']}")
            if p.get('amount'):    parts.append(f"Rs.{p['amount']:,.0f}")
            if p.get('dates'):     parts.append(f"date={p['dates'][0]}")
            if p.get('duplicate_of'): parts.append(f"DUPLICATE OF {p['duplicate_of']}")
            lines.append(f"  {p['file_name']}: " + "  |  ".join(parts))

    return "\n".join(lines)


def claude_final_verdict(audit):
    """
    Call Claude Sonnet to produce an independent audit verdict.
    Returns a verdict dict, or None if the call fails / anthropic unavailable.

    The full policy_rules.json is passed as a cached system message so repeated
    audits in the same session pay for the policy tokens only once.
    """
    try:
        import anthropic as _anthropic
    except ImportError:
        return None

    try:
        # Load and cache policy JSON as the system prompt prefix
        with open(POLICY_PATH) as f:
            policy_text = f.read()
    except Exception:
        policy_text = "{}"  # degrade gracefully if file missing

    client = _anthropic.Anthropic()
    audit_summary = _fmt_audit_for_verdict(audit)

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=[
                # Policy JSON cached — re-used across back-to-back audits in the same session
                {"type": "text", "text": policy_text,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _VERDICT_SYSTEM},
            ],
            tools=[_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "give_audit_verdict"},
            messages=[{"role": "user", "content": audit_summary}]
        )
        for block in (msg.content or []):
            if hasattr(block, 'type') and block.type == 'tool_use' \
                    and block.name == 'give_audit_verdict':
                return block.input or {}
        return None
    except Exception as e:
        print(f"  [claude_final_verdict] error: {e}", file=sys.stderr)
        return None


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('voucher_pdf')
    ap.add_argument('proofs_zip', nargs='?', default=None)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    result = run_audit(args.voucher_pdf, args.proofs_zip)
    out = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(out)
        print(f'Audit findings written to {args.out}', file=sys.stderr)
    else:
        sys.stdout.buffer.write((out + '\n').encode('utf-8'))
