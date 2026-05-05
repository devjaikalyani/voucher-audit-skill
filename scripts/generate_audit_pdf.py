"""
Render a structured audit findings JSON into a Rite Audit Report PDF.

Usage:
    python generate_audit_pdf.py audit.json [--out report.pdf]
"""
import sys, os, json, argparse, datetime

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, KeepTogether, PageBreak, Flowable)
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
except ImportError:
    print('ERROR: reportlab not installed. Run: pip install reportlab --break-system-packages',
          file=sys.stderr)
    sys.exit(1)


# ---------- Orphan guard ----------

class _OrphanGuard(Flowable):
    """Conditional page break: if less than `threshold` (default 25%) of the
    usable page height remains when this flowable is laid out, a page break is
    inserted automatically.  Otherwise it takes zero space and is invisible."""

    def __init__(self, full_height, threshold=0.25):
        super().__init__()
        self._full_height = full_height
        self._threshold   = threshold
        self.width  = 0
        self.height = 0

    def wrap(self, avail_w, avail_h):
        if avail_h < self._full_height * self._threshold:
            # Claim more height than is available → framework can't fit this
            # flowable → split() returns [] → page break is inserted.
            return (0, avail_h + 1)
        return (0, 0)

    def split(self, avail_w, avail_h):
        return []   # unsplittable → forces a page break

    def draw(self):
        pass


# ---------- Colors ----------
NAVY           = colors.HexColor('#1F3A5F')
HEADER_BLUE    = colors.HexColor('#2C5282')
TABLE_HEADER_BG= colors.HexColor('#E2E8F0')
ALERT_BG       = colors.HexColor('#FFF5F5')
ALERT_BORDER   = colors.HexColor('#E53E3E')
GRID           = colors.HexColor('#CBD5E0')
ROW_ALT        = colors.HexColor('#F7FAFC')

STATUS_COLORS = {
    'APPROVE':     colors.HexColor('#2F855A'),
    'CONDITIONAL': colors.HexColor('#B7791F'),
    'FLAG':        colors.HexColor('#C05621'),
    'REJECT':      colors.HexColor('#C53030'),
}
SEVERITY_COLORS = {
    'HIGH':   colors.HexColor('#C53030'),
    'MEDIUM': colors.HexColor('#B7791F'),
    'LOW':    colors.HexColor('#3182CE'),
}

# ---------- Plain-English label maps ----------

_STATUS_LABEL = {
    'APPROVE':     'Approved',
    'CONDITIONAL': 'Conditional -- Documents Needed',
    'FLAG':        'Policy Breach',
    'REJECT':      'Rejected',
}

_SEVERITY_LABEL = {
    'HIGH':   'High Risk',
    'MEDIUM': 'Medium Risk',
    'LOW':    'Low Risk',
}

_PRIORITY_LABEL = {
    'IMMEDIATE':  'Action Required Now',
    'SHORT-TERM': 'Action Needed Soon',
    'CONFIRMED':  'Confirmed Issue',
    'LONG-TERM':  'Future Improvement',
}

_PROOF_STATUS_LABEL = {
    'OK':      'Sufficient ✓',
    'WEAK':    'Insufficient — Follow Up Required',
    'MISSING': 'Not Provided',
}

_KIND_LABEL = {
    # Payment proofs
    'upi_screenshot':          'UPI Payment Screenshot',
    'payment_screenshot':      'Payment Screenshot',
    'bank_transfer_screenshot':'Bank Transfer Screenshot',
    # Toll / transit
    'fastag_screenshot':       'FASTag / Toll Screenshot',
    # Hotel
    'hotel_bill_printed':      'Hotel Bill (Printed)',
    'hotel_bill_handwritten':  'Hotel Bill (Handwritten)',
    'hotel_bill':              'Hotel Bill',
    # Transport tickets
    'train_ticket':            'Train Ticket',
    'bus_ticket':              'Bus Ticket',
    'flight_ticket':           'Flight Ticket / Boarding Pass',
    'cab_receipt':             'Cab / Taxi Receipt',
    'auto_receipt':            'Auto-Rickshaw Receipt',
    'fuel_receipt':            'Fuel / Petrol Receipt',
    # Vendor bills
    'workshop_bill_printed':   'Workshop Bill (Printed)',
    'workshop_bill_handwritten': 'Workshop Bill (Handwritten)',
    'workshop_bill':           'Workshop Bill',
    'food_receipt':            'Food / Restaurant Bill',
    'purchase_invoice':        'Purchase Invoice',
    'receipt_photo':           'Receipt Photo',
    'bill':                    'Bill / Invoice',
    # Generic
    'ticket':                  'Travel Ticket',
    'pdf':                     'PDF Document',
    'image':                   'Image',
    'other':                   'Other',
}

_DESIG_LABEL = {
    'DIRECTOR_CFO': 'Director / CFO',
    'VP_GM':        'VP / General Manager',
    'SR_MANAGER':   'Senior Manager',
    'MANAGER':      'Manager',
    'ASST_MANAGER': 'Asst. Manager',
    'SR_EXECUTIVE': 'Senior Executive',
    'TECHNICIAN':   'Technician',
}

_CITY_GRADE_LABEL = {
    'A': 'Grade A — Metro / Major City',
    'B': 'Grade B — Large City',
    'C': 'Grade C — Other City / Town',
}


def _proof_status_label(raw):
    if not raw:
        return '-'
    if raw.upper().startswith('PER-DIEM') or raw.upper().startswith('PER DIEM'):
        return 'Per-Diem Expense — No Bill Required'
    return _PROOF_STATUS_LABEL.get(raw.upper(), raw)


def styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle('Title1', parent=ss['Heading1'], fontSize=18,
                          textColor=colors.white, alignment=TA_CENTER, leading=22))
    ss.add(ParagraphStyle('Subtitle', parent=ss['Normal'], fontSize=9,
                          textColor=colors.white, alignment=TA_CENTER))
    ss.add(ParagraphStyle('Section', parent=ss['Heading2'], fontSize=12,
                          textColor=NAVY, spaceBefore=14, spaceAfter=6))
    ss.add(ParagraphStyle('Body', parent=ss['Normal'], fontSize=9, leading=12))
    ss.add(ParagraphStyle('Small', parent=ss['Normal'], fontSize=8, leading=10))
    ss.add(ParagraphStyle('AlertBody', parent=ss['Normal'], fontSize=9, leading=12,
                          textColor=colors.HexColor('#742A2A')))
    return ss


def fmt_inr(x):
    if x is None:
        return '-'
    try:
        return f'{float(x):,.0f}'
    except (TypeError, ValueError):
        return str(x)


def _pdf_str(s):
    """Strip characters that Helvetica/Latin-1 cannot render to avoid black-box glyphs."""
    if not s:
        return s or ''
    return s.encode('latin-1', errors='replace').decode('latin-1')


def header_block(audit, ss):
    voucher = audit['voucher']
    emp     = audit['employee']
    meta    = audit['audit_metadata']

    title_data = [
        [Paragraph('<b>RITE AUDIT REPORT</b>', ss['Title1'])],
        [Paragraph(meta['policy_version'], ss['Subtitle'])],
    ]
    title_tbl = Table(title_data, colWidths=[180 * mm])
    title_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), NAVY),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 12),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 12),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))

    def _lbl(text):
        return Paragraph(f'<b>{text}</b>', ss['Small'])
    def _val(text):
        return Paragraph(str(text) if text else '-', ss['Small'])

    desig_label = _DESIG_LABEL.get(emp.get('designation_bucket'), emp.get('designation_bucket') or 'N/A')
    info = [
        [_lbl('Report Date:'),     _val(meta['report_date']),
         _lbl('Audit Reference:'), _val(meta['audit_reference'])],
        [_lbl('Voucher No.:'),     _val(voucher.get('voucher_no') or '-'),
         _lbl('Voucher Date:'),    _val(voucher.get('voucher_date') or '-')],
        [_lbl('Employee Name:'),   _val(voucher.get('employee_name') or '-'),
         _lbl('Employee Code:'),   _val(voucher.get('employee_code') or '-')],
        [_lbl('Cost Center:'),     _val(voucher.get('cost_center') or '-'),
         _lbl('Trip / Purpose:'),  _val(voucher.get('narration') or '-')],
        [_lbl('Claim Period:'),
         _val(f"{voucher.get('period_from') or '-'}  to  {voucher.get('period_to') or '-'}"),
         _lbl('Designation:'),
         _val(f"{emp.get('designation_master') or 'N/A'}  ({desig_label})")],
        [_lbl('Total Amount Claimed:'),
         _val(f"INR {fmt_inr(audit['totals']['gross_claimed'])}"),
         _lbl('Project Head Approved:'),
         _val(f"INR {fmt_inr(audit['totals']['reviewer_approved'])}")],
        [_lbl('Policy-Eligible Amount:'),
         _val(f"INR {fmt_inr(audit['totals']['policy_eligible'])}"),
         _lbl('Recommended Hold:'),
         _val(f"INR {fmt_inr(audit['totals']['recommended_hold'])}")],
        [_lbl('Documents Submitted:'),
         _val(f"{len(audit['proofs'])} file(s) reviewed"),
         _lbl('Total Extracted\nAmount (Proofs):'),
         _val('INR ' + fmt_inr(sum(p.get('amount') or 0 for p in audit['proofs'] if p.get('amount'))))],
    ]
    info_tbl = Table(info, colWidths=[42 * mm, 48 * mm, 42 * mm, 48 * mm])
    info_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (0, -1), TABLE_HEADER_BG),
        ('BACKGROUND',    (2, 0), (2, -1), TABLE_HEADER_BG),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return [title_tbl, Spacer(1, 4 * mm), info_tbl, Spacer(1, 4 * mm)]


def alerts_block(audit, ss):
    alerts = []
    for f in audit['findings']:
        if f['severity'] == 'HIGH' and f.get('audit_note'):
            alerts.append(f"<b>{f['expense_head']} ({f['date']}):</b> {f['audit_note']}")
    for b in audit['breaches']:
        if b['severity'] == 'HIGH' and 'duplicate' in b['clause'].lower():
            alerts.append(f"<b>Duplicate document detected:</b> {b['clause']}")
    if not alerts:
        return []
    body = '<br/>'.join(f'({i+1}) {a}' for i, a in enumerate(alerts))
    body = '<b>⚠ IMPORTANT — Items Requiring Immediate Attention:</b><br/>' + body
    para = Paragraph(body, ss['AlertBody'])
    tbl = Table([[para]], colWidths=[180 * mm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), ALERT_BG),
        ('BOX',           (0, 0), (-1, -1), 1.5, ALERT_BORDER),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    return [tbl, Spacer(1, 4 * mm)]


def section_title(num, text, ss):
    return Paragraph(f'<b>{num}. {text.upper()}</b>', ss['Section'])


def policy_rules_table(audit, ss):
    def _pval(text):
        return Paragraph(str(text), ss['Small'])

    grade       = audit['city_grade']['grade']
    grade_label = _CITY_GRADE_LABEL.get(grade, f'Grade {grade}')
    desig_label = _DESIG_LABEL.get(audit['employee']['designation_bucket'],
                                   audit['employee']['designation_bucket'])

    rows = [['Policy Rule', 'Details', 'Applies To']]
    rows.append([
        'Maximum Claim Period',
        _pval('Expenses must be claimed within 90 days of the travel date.'),
        'All claims',
    ])
    rows.append([
        'Senior Approval Required',
        _pval('Any voucher totalling more than Rs.10,000 must have a senior management sign-off on file.'),
        'Entire voucher',
    ])
    rows.append([
        'City Category Identified',
        _pval(f"{audit['city_grade']['derived_city'] or '(city could not be determined)'} — "
              f"{grade_label}. City category determines hotel and food allowance limits."),
        'Hotel & Food',
    ])
    rows.append([
        'Employee Grade Identified',
        _pval(f"Designation on record: {audit['employee'].get('designation_master') or '(unknown)'}. "
              f"Classified under: {desig_label}. This grade sets hotel, food, and travel-class limits."),
        'Hotel, Food & Travel',
    ])
    rows.append([
        'Duplicate Document Check',
        _pval('All submitted files are checked against each other. Identical receipts submitted '
              'for more than one expense line will be flagged.'),
        'All expense lines',
    ])
    rows.append([
        'Hospitality & Entertainment',
        _pval('Expenses such as client dinners, boat cruises, or entertainment events are NOT covered '
              'under the standard travel policy. VP / General Manager pre-approval is required.'),
        'Cruise, client meals',
    ])
    rows.append([
        'Handwritten Receipts',
        _pval('Handwritten receipts are not accepted as valid proof of expenditure. '
              'Original printed bills or digital receipts are required.'),
        'All claims',
    ])

    tbl = Table(rows, colWidths=[45 * mm, 95 * mm, 40 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    return [tbl, Spacer(1, 3 * mm)]


import re as _re

def _filename_hints(fname):
    """Extract date, payment app, and capture method from a proof filename."""
    hints = []
    u = fname.upper()

    # Date: Screenshot_YYYYMMDD-HHMMSS or _YYYYMMDD_ patterns
    for pat in (r'(\d{4})(\d{2})(\d{2})[-_T]', r'(\d{4})-(\d{2})-(\d{2})'):
        m = _re.search(pat, fname)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2020 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
                hints.append(f"Date: {y:04d}-{mo:02d}-{d:02d}")
                break

    # Payment app
    for app, label in [('PHONEPE', 'PhonePe'), ('GOOGLEPAY', 'Google Pay'),
                       ('GPAY', 'Google Pay'), ('PAYTM', 'Paytm'),
                       ('BHIM', 'BHIM UPI'), ('AMAZONPAY', 'Amazon Pay')]:
        if app in u:
            hints.append(f"App: {label}")
            break

    # Capture method
    if 'SCREENSHOT' in u:
        hints.append('Screenshot')
    elif 'IMG' in u or 'WA0' in u:
        hints.append('Photo')

    return hints


def proof_review_table(audit, ss):
    rows = [['#', 'File Name', 'Document Type', 'Details Extracted', 'Verification Result']]
    for i, p in enumerate(audit['proofs'], start=1):
        details = []
        if p.get('vendor'):
            details.append(f"Merchant: {_pdf_str(p['vendor'])}")
        if p.get('amount'):
            details.append(f"Amount: Rs.{fmt_inr(p['amount'])}")
        if p.get('dates'):
            details.append(f"Date: {p['dates'][0]}")
        if p.get('ride_id'):
            details.append(f"Ride ID: {_pdf_str(p['ride_id'])}")
        if p.get('invoice_no'):
            details.append(f"Invoice No: {_pdf_str(p['invoice_no'])}")
        if p.get('txn_id'):
            details.append(f"Transaction ID: {_pdf_str(p['txn_id'])}")
        if p.get('pnr'):
            details.append(f"PNR: {_pdf_str(p['pnr'])}")
        if p.get('check_in') and p.get('check_out'):
            details.append(f"Hotel Stay: {p.get('nights') or '?'} night(s)")

        # Odometer readings — shown instead of/in addition to amount for fuel proofs
        odo_s = p.get('odometer_start')
        odo_e = p.get('odometer_end')
        if odo_s is not None or odo_e is not None:
            if odo_s is not None and odo_e is not None and odo_e > odo_s:
                dist = int(odo_e) - int(odo_s)
                details.append(f"Odometer: {int(odo_s):,} -> {int(odo_e):,} km  |  Distance: {dist} km  |  Eligible @ Rs.3/km: Rs.{dist*3:,}")
            elif odo_e is not None:
                details.append(f"Odometer reading: {int(odo_e):,} km")
            elif odo_s is not None:
                details.append(f"Odometer reading: {int(odo_s):,} km")

        if details:
            details_text = '\n'.join(details)
        else:
            # Fallback: parse what we can from the filename itself
            fn_hints = _filename_hints(p.get('file_name', ''))
            if fn_hints:
                details_text = '  |  '.join(fn_hints) + '  -- document contents could not be read'
            else:
                details_text = 'Could not read document contents -- please review manually'

        if p.get('duplicate_of'):
            status = '✗ Duplicate — Reject'
            color  = STATUS_COLORS['REJECT']
        elif details:
            status = '✓ Verified'
            color  = STATUS_COLORS['APPROVE']
        else:
            status = '⚠ Incomplete — Follow Up'
            color  = STATUS_COLORS['CONDITIONAL']

        kind_label = _KIND_LABEL.get((p.get('kind') or '').lower(),
                                     (p.get('kind') or 'Unknown').title())
        rows.append([
            str(i),
            Paragraph(p['file_name'][:65], ss['Small']),
            Paragraph(kind_label, ss['Small']),
            Paragraph(details_text, ss['Small']),
            Paragraph(f"<font color='{color.hexval()}'><b>{status}</b></font>", ss['Small']),
        ])
    if len(rows) == 1:
        rows.append(['-', Paragraph('(No supporting documents found)', ss['Small']), '-', '-', '-'])

    tbl = Table(rows, colWidths=[8 * mm, 52 * mm, 26 * mm, 66 * mm, 28 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 3),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 3),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    return [tbl, Spacer(1, 3 * mm)]


def line_item_block(audit, ss):
    elems = []
    for i, f in enumerate(audit['findings'], start=1):
        status        = f['policy_status']
        sev           = f['severity']
        no_issues     = not (f.get('audit_note') or '').strip()

        # When the audit found nothing wrong, normalise the display regardless of
        # what proof_status or policy_status the engine emitted internally.
        if no_issues:
            status        = 'APPROVE'
            sev           = 'LOW'

        status_color  = STATUS_COLORS.get(status, NAVY)
        sev_color     = SEVERITY_COLORS.get(sev, colors.black)
        status_label  = 'No Issues' if no_issues else _STATUS_LABEL.get(status, status)
        sev_label     = 'No Issues' if no_issues else _SEVERITY_LABEL.get(sev, sev)

        title = (
            f"<b>#{i}  {f['expense_head']}  --  {f['date']}"
            f"  &nbsp;|&nbsp;  Claimed: INR {fmt_inr(f['claimed_inr'])}"
            f"  &nbsp;|&nbsp;  Project Head Approved: INR {fmt_inr(f['approved_by_reviewer_inr'])}"
            f"  &nbsp;|&nbsp;  Policy Eligible: INR {fmt_inr(f['policy_eligible_inr'])}"
            f"  &nbsp;|&nbsp;  <font color='{status_color.hexval()}'>{status_label}</font>"
            f"  &nbsp;|&nbsp;  <font color='{sev_color.hexval()}'>{sev_label}</font></b>"
        )

        proof_stat_display = 'Verified -- No Action Required' if no_issues else _proof_status_label(f.get('proof_status') or '')
        matched_files = ', '.join(m['file'] for m in f['matched_proofs']) or '(None matched — document may be missing or unclear)'

        _style = TableStyle([
            ('SPAN',          (0, 0), (-1, 0)),
            ('FONTSIZE',      (0, 0), (-1, -1), 8),
            ('GRID',          (0, 0), (-1, -1), 0.3, GRID),
            ('BACKGROUND',    (0, 0), (-1, 0), TABLE_HEADER_BG),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ])
        _cols = [36 * mm, 120 * mm, 24 * mm]

        # Anchor: title + rule + documents-required kept together so the heading
        # is never stranded at the bottom of a page without at least one content row.
        anchor_rows = [
            [Paragraph(title, ss['Body']), '', ''],
            ['Policy Rule Applied:',
             Paragraph(f['rule_applied'] or '-', ss['Small']), ''],
            ['Documents Required:',
             Paragraph(f['proof_required'] or '-', ss['Small']),
             Paragraph(f"Document Status:<br/>{proof_stat_display}", ss['Small'])],
        ]
        anchor_tbl = Table(anchor_rows, colWidths=_cols)
        anchor_tbl.setStyle(_style)

        # Body rows: allowed to split across pages freely so long content never
        # causes half-empty pages.
        body_rows = [
            ['Documents Found:',
             Paragraph(matched_files, ss['Small']), ''],
            ['Audit Finding:',
             Paragraph(f['audit_note'] or 'No issues identified.', ss['Small']), ''],
        ]
        if f.get('recommended_action'):
            body_rows.append(['Action Required:',
                              Paragraph(f['recommended_action'], ss['Small']), ''])
        body_tbl = Table(body_rows, colWidths=_cols)
        body_tbl.setStyle(_style)

        elems.append(KeepTogether([anchor_tbl]))
        elems.extend([body_tbl, Spacer(1, 5 * mm)])
    return elems


def hotel_analysis_block(audit, ss):
    hotel_findings = [f for f in audit['findings'] if f['expense_head'] == 'Hotel']
    if not hotel_findings:
        return []
    f     = hotel_findings[0]
    grade = audit['city_grade']['grade']
    desig_caps = {
        'TECHNICIAN': 700, 'SR_EXECUTIVE': 750, 'ASST_MANAGER': 750,
        'MANAGER': 750, 'SR_MANAGER': 1000, 'VP_GM': 1500, 'DIRECTOR_CFO': 'Actual',
    }
    if grade == 'A':
        desig_caps = {'TECHNICIAN': 1000, 'SR_EXECUTIVE': 1100, 'ASST_MANAGER': 1200,
                      'MANAGER': 1500, 'SR_MANAGER': 2000, 'VP_GM': 3000,
                      'DIRECTOR_CFO': 'Actual'}
    elif grade == 'B':
        desig_caps = {'TECHNICIAN': 900, 'SR_EXECUTIVE': 1000, 'ASST_MANAGER': 1000,
                      'MANAGER': 1200, 'SR_MANAGER': 1500, 'VP_GM': 2000,
                      'DIRECTOR_CFO': 'Actual'}
    nights  = 1
    for p in audit['proofs']:
        if p.get('nights'):
            try:
                nights = max(nights, int(p['nights']))
            except (ValueError, TypeError):
                pass
    claimed = f['claimed_inr'] or 0
    grade_label = _CITY_GRADE_LABEL.get(grade, f'Grade {grade}')

    def _hh(t): return Paragraph(f'<b>{t}</b>', ss['Small'])
    rows = [[_hh('Employee Grade'),
             _hh(f'Nightly Cap ({grade_label}) INR'),
             _hh(f'Policy Limit for {nights} Night(s) INR'),
             _hh('Amount Claimed INR'),
             _hh('Amount Over Limit INR')]]
    for d, cap in desig_caps.items():
        if cap == 'Actual':
            elig  = claimed
            excess = 0
        else:
            elig  = cap * nights
            excess = max(0, claimed - elig)
        rows.append([_DESIG_LABEL.get(d, d), str(cap) if cap != 'Actual' else 'Actual cost',
                     fmt_inr(elig), fmt_inr(claimed),
                     fmt_inr(excess) if excess else '--'])

    tbl = Table(rows, colWidths=[38 * mm, 40 * mm, 38 * mm, 32 * mm, 32 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    emp_desig_label = _DESIG_LABEL.get(audit['employee']['designation_bucket'],
                                        audit['employee']['designation_bucket'])
    note = Paragraph(
        f"<b>How to read this table:</b> The hotel claim of Rs.{fmt_inr(claimed)} for "
        f"{nights} night(s) in a {grade_label} location is shown against the nightly limit for "
        f"each employee grade. The employee on this voucher is classified as "
        f"<b>{emp_desig_label}</b> — refer to that row to determine the eligible amount. "
        f"Any amount in the 'Amount Over Limit' column must be recovered or approved by a higher authority.",
        ss['Small'])
    return [tbl, Spacer(1, 2 * mm), note, Spacer(1, 3 * mm)]


def food_analysis_block(audit, ss):
    food_findings = [f for f in audit['findings'] if f['expense_head'] == 'Food Allowance']
    if not food_findings:
        return []
    days           = len(food_findings)
    claimed_per_day = food_findings[0]['claimed_inr'] or 0

    rows = [['Employee Grade', 'Daily Allowance — With Overnight Stay (INR)',
             'Total Claimed (INR)', 'Policy Eligible (INR)', 'Difference']]
    for d, rate in [('TECHNICIAN', 400), ('SR_EXECUTIVE', 400),
                    ('ASST_MANAGER', 500), ('MANAGER', 500),
                    ('SR_MANAGER', 600), ('VP_GM', 750), ('DIRECTOR_CFO', 'Actual')]:
        claimed_total = claimed_per_day * days
        if rate == 'Actual':
            elig = claimed_total
            diff = 0
        else:
            elig = rate * days
            diff = claimed_total - elig
        rows.append([
            _DESIG_LABEL.get(d, d),
            str(rate) if rate != 'Actual' else 'Actual cost',
            fmt_inr(claimed_total),
            fmt_inr(elig),
            (f'Rs.{fmt_inr(diff)} over limit') if diff > 0 else
            ('Within limit' if diff == 0 else f'Rs.{fmt_inr(-diff)} within limit'),
        ])
    tbl = Table(rows, colWidths=[35 * mm, 45 * mm, 30 * mm, 30 * mm, 33 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',  (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',(0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',  (0, 0), (-1, -1), 8),
        ('GRID',      (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',    (0, 0), (-1, -1), 'TOP'),
    ]))
    return [tbl, Spacer(1, 3 * mm)]


def breach_summary_block(audit, ss):
    if not audit['breaches']:
        return [Paragraph('No policy concerns were identified for this voucher.', ss['Body']),
                Spacer(1, 3 * mm)]
    rows = [['Ref.', 'Policy Concern', 'Risk Level', 'Affected Expense Lines', 'Amount at Risk (INR)']]
    for i, b in enumerate(audit['breaches'], start=1):
        sev_c     = SEVERITY_COLORS.get(b['severity'], colors.black)
        sev_label = _SEVERITY_LABEL.get(b['severity'], b['severity'])
        rows.append([
            f'B-{i:02d}',
            Paragraph(b['clause'][:500], ss['Small']),
            Paragraph(f"<font color='{sev_c.hexval()}'><b>{sev_label}</b></font>", ss['Small']),
            Paragraph(str(b['entries']), ss['Small']),
            Paragraph(fmt_inr(b.get('amount_at_risk') or 0), ss['Small']),
        ])
    tbl = Table(rows, colWidths=[12 * mm, 80 * mm, 22 * mm, 36 * mm, 30 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    return [tbl, Spacer(1, 3 * mm)]


def eligible_amount_block(audit, ss):
    def _hdr(text):
        return Paragraph(f'<b>{text}</b>', ss['Small'])

    rows = [[_hdr('#'), _hdr('Expense Type'), _hdr('Date'),
             _hdr('Claimed\n(INR)'), _hdr('Project Head\nApproved (INR)'),
             _hdr('Policy\nEligible (INR)'), _hdr('Audit Status'),
             _hdr('Supporting\nDocument')]]

    for i, f in enumerate(audit['findings'], start=1):
        status_c     = STATUS_COLORS.get(f['policy_status'], NAVY)
        status_label = _STATUS_LABEL.get(f['policy_status'], f['policy_status'])
        # Best-matched proof for this line item
        best = (f.get('matched_proofs') or [{}])[0]
        doc_name = best.get('file', '')
        if doc_name:
            # Truncate long filenames but keep extension readable
            if len(doc_name) > 28:
                base, _, ext = doc_name.rpartition('.')
                doc_name = base[:22] + '…' + (('.' + ext) if ext else '')
            doc_cell = Paragraph(doc_name, ss['Small'])
        else:
            doc_cell = Paragraph('<font color="#999999">None</font>', ss['Small'])
        rows.append([
            str(i),
            Paragraph(f['expense_head'], ss['Small']),
            f['date'],
            fmt_inr(f['claimed_inr']),
            fmt_inr(f['approved_by_reviewer_inr']),
            fmt_inr(f['policy_eligible_inr']),
            Paragraph(f"<font color='{status_c.hexval()}'><b>{status_label}</b></font>", ss['Small']),
            doc_cell,
        ])
    rows.append([
        '', Paragraph('<b>TOTAL</b>', ss['Small']), '',
        Paragraph(f"<b>{fmt_inr(audit['totals']['gross_claimed'])}</b>", ss['Small']),
        Paragraph(f"<b>{fmt_inr(audit['totals']['reviewer_approved'])}</b>", ss['Small']),
        Paragraph(f"<b>{fmt_inr(audit['totals']['policy_eligible'])}</b>", ss['Small']),
        '', '',
    ])
    tbl = Table(rows, colWidths=[8 * mm, 30 * mm, 20 * mm, 18 * mm, 26 * mm, 22 * mm, 24 * mm, 32 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('FONTNAME',      (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, -1), (-1, -1), TABLE_HEADER_BG),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return [tbl, Spacer(1, 3 * mm)]


def recommendations_block(audit, ss):
    if not audit['recommendations']:
        return []
    rows = [['Ref.', 'Priority', 'Recommendation']]
    for i, r in enumerate(audit['recommendations'], start=1):
        priority_label = _PRIORITY_LABEL.get(r['priority'], r['priority'])
        pri_color = (STATUS_COLORS['REJECT']      if r['priority'] == 'IMMEDIATE'  else
                     STATUS_COLORS['FLAG']         if r['priority'] == 'SHORT-TERM' else
                     STATUS_COLORS['CONDITIONAL']  if r['priority'] == 'CONFIRMED'  else
                     STATUS_COLORS['APPROVE'])
        rows.append([
            f'R-{i:02d}',
            Paragraph(f"<font color='{pri_color.hexval()}'><b>{priority_label}</b></font>", ss['Small']),
            Paragraph(r['text'], ss['Small']),
        ])
    tbl = Table(rows, colWidths=[12 * mm, 38 * mm, 130 * mm])
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    return [tbl, Spacer(1, 3 * mm)]


def claude_verdict_block(audit, ss):
    """Section 8 — AI Audit Verdict (Claude Sonnet independent analysis)."""
    v = audit.get('claude_verdict')
    if not v:
        return []

    elems = []

    # --- Overall recommendation banner ---
    rec     = v.get('overall_recommendation', 'UNKNOWN')
    conf    = v.get('confidence', '')
    payable = v.get('recommended_payable_inr')

    rec_color = {
        'APPROVE':                  STATUS_COLORS['APPROVE'],
        'APPROVE_WITH_DEDUCTIONS':  STATUS_COLORS['CONDITIONAL'],
        'HOLD_FOR_CLARIFICATION':   STATUS_COLORS['FLAG'],
        'REJECT':                   STATUS_COLORS['REJECT'],
    }.get(rec, NAVY)

    rec_label = {
        'APPROVE':                 'APPROVE',
        'APPROVE_WITH_DEDUCTIONS': 'APPROVE WITH DEDUCTIONS',
        'HOLD_FOR_CLARIFICATION':  'HOLD FOR CLARIFICATION',
        'REJECT':                  'REJECT',
    }.get(rec, rec)

    banner_data = [[
        Paragraph(
            f"<font color='{rec_color.hexval()}'><b>AI VERDICT: {rec_label}</b></font>"
            + (f"  <font color='#666666'>Confidence: {conf}</font>" if conf else ""),
            ss['Body']),
        Paragraph(
            f"<b>Recommended Payable: INR {fmt_inr(payable)}</b>" if payable is not None
            else "<b>Recommended Payable: —</b>",
            ss['Body']),
    ]]
    banner = Table(banner_data, colWidths=[120 * mm, 60 * mm])
    banner.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), TABLE_HEADER_BG),
        ('BOX',           (0, 0), (-1, -1), 1.0, rec_color),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elems += [banner, Spacer(1, 3 * mm)]

    # --- Reasoning paragraph ---
    reasoning = _pdf_str(v.get('reasoning') or '')
    if reasoning:
        elems.append(Paragraph(f"<b>Reasoning:</b> {reasoning}", ss['Body']))
        elems.append(Spacer(1, 2 * mm))

    # --- Admin action ---
    action = _pdf_str(v.get('admin_action_required') or '')
    if action:
        action_para = Paragraph(f"<b>Admin Action Required:</b> {action}", ss['Body'])
        action_tbl  = Table([[action_para]], colWidths=[180 * mm])
        action_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor('#FFFAF0')),
            ('BOX',           (0, 0), (-1, -1), 0.8, colors.HexColor('#D69E2E')),
            ('LEFTPADDING',   (0, 0), (-1, -1), 8),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
            ('TOPPADDING',    (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        elems += [action_tbl, Spacer(1, 3 * mm)]

    # --- Key concerns ---
    concerns = [_pdf_str(c) for c in (v.get('key_concerns') or []) if c]
    if concerns:
        elems.append(Paragraph('<b>Key Concerns:</b>', ss['Body']))
        for c in concerns:
            elems.append(Paragraph(f'• {c}', ss['Small']))
        elems.append(Spacer(1, 2 * mm))

    # --- Line-item breakdown table ---
    approve_heads = v.get('items_to_approve') or []
    reduce_items  = v.get('items_to_reduce')  or []
    reject_heads  = v.get('items_to_reject')  or []

    if approve_heads or reduce_items or reject_heads:
        rows = [['Expense Head', 'AI Decision', 'Claimed', 'Recommended', 'Reason']]
        for head in approve_heads:
            rows.append([
                _pdf_str(str(head)),
                Paragraph("<font color='#2F855A'><b>Approve</b></font>", ss['Small']),
                '—', '= Claimed', '—',
            ])
        for item in reduce_items:
            head  = _pdf_str(str(item.get('expense_head', '')))
            camt  = item.get('claimed_inr')
            ramt  = item.get('recommended_inr')
            rsn   = _pdf_str(str(item.get('reason', '')))
            rows.append([
                head,
                Paragraph("<font color='#B7791F'><b>Reduce</b></font>", ss['Small']),
                f"Rs.{fmt_inr(camt)}" if camt is not None else '—',
                f"Rs.{fmt_inr(ramt)}" if ramt is not None else '—',
                Paragraph(rsn, ss['Small']),
            ])
        for head in reject_heads:
            rows.append([
                _pdf_str(str(head)),
                Paragraph("<font color='#C53030'><b>Reject</b></font>", ss['Small']),
                '—', 'Rs.0', '—',
            ])
        tbl = Table(rows, colWidths=[38 * mm, 26 * mm, 22 * mm, 26 * mm, 68 * mm])
        tbl.setStyle(TableStyle([
            ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BLUE),
            ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
            ('FONTSIZE',      (0, 0), (-1, -1), 8),
            ('GRID',          (0, 0), (-1, -1), 0.4, GRID),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ]))
        elems += [tbl, Spacer(1, 3 * mm)]

    elems.append(Paragraph(
        '<i>This AI verdict is generated by Claude Sonnet and is advisory only. '
        'The finance/admin team retains final authority over all payment decisions.</i>',
        ss['Small']))
    elems.append(Spacer(1, 2 * mm))
    return elems


def conclusion_block(audit, ss):
    p     = Paragraph(audit['conclusion'], ss['Body'])
    notes = []
    if audit['history_notes']:
        notes.append(Paragraph('<b>Past Decisions for This Employee:</b>', ss['Body']))
        for n in audit['history_notes']:
            notes.append(Paragraph('— ' + n, ss['Small']))
    sign_rows = [
        ['Prepared By',        'Reviewed By',     'Approved By'],
        ['Rite Audit Engine',  'Admin',            'Admin'],
        [audit['audit_metadata']['report_date'], '________________', '________________'],
    ]
    sign = Table(sign_rows, colWidths=[60 * mm, 60 * mm, 60 * mm])
    sign.setStyle(TableStyle([
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 0), (-1, 0), TABLE_HEADER_BG),
        ('FONTSIZE',   (0, 0), (-1, -1), 9),
        ('GRID',       (0, 0), (-1, -1), 0.4, GRID),
        ('ALIGN',      (0, 0), (-1, -1), 'CENTER'),
    ]))
    sign_block = KeepTogether([
        Spacer(1, 6 * mm), sign, Spacer(1, 4 * mm),
        Paragraph(
            '<i>CONFIDENTIAL — For Internal Audit Use Only. '
            'Policy Reference: Rite Water Solutions Travel & Reimbursement Policy, effective 01-Nov-2023.</i>',
            ss['Small']),
    ])
    return [p, Spacer(1, 3 * mm)] + notes + [sign_block]


def _make_page_footer(audit_ref, voucher_no):
    """Return an onPage callback that draws a footer with page number on every page."""
    def _footer(canvas, doc):
        canvas.saveState()
        w, h = A4
        footer_y = 8 * mm
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(colors.HexColor('#666666'))
        # Left: audit reference
        canvas.drawString(15 * mm, footer_y, f'Ref: {audit_ref}  |  Voucher #{voucher_no}')
        # Right: page number
        canvas.drawRightString(w - 15 * mm, footer_y,
                               f'Page {doc.page}')
        # Thin rule above footer
        canvas.setStrokeColor(colors.HexColor('#cccccc'))
        canvas.setLineWidth(0.4)
        canvas.line(15 * mm, footer_y + 4 * mm, w - 15 * mm, footer_y + 4 * mm)
        canvas.restoreState()
    return _footer


def render_pdf(audit, out_path):
    ss  = styles()
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=18 * mm,   # extra bottom room for footer
        title=f"Rite Audit Report — Voucher {audit['voucher'].get('voucher_no')}",
    )
    footer = _make_page_footer(
        audit['audit_metadata']['audit_reference'],
        audit['voucher'].get('voucher_no') or '—',
    )
    usable_h = A4[1] - 12 * mm - 18 * mm   # page height minus top + bottom margins

    def _section(num, label, content_elems):
        """Emit an orphan-guarded section heading: if < 25 % of the page height
        remains at layout time, the heading is automatically pushed to the next
        page.  Otherwise it sits inline and content flows naturally below it."""
        guard = _OrphanGuard(usable_h, threshold=0.25)
        return [guard, section_title(num, label, ss)] + (content_elems or [])

    story = []
    story += header_block(audit, ss)
    story += alerts_block(audit, ss)
    story += _section('1', 'Policy Rules Applicable to This Claim', policy_rules_table(audit, ss))
    story += _section('2', 'Supporting Documents Submitted',        proof_review_table(audit, ss))
    story += _section('3', 'Expense Line-by-Line Review',           line_item_block(audit, ss))
    hotel_block = hotel_analysis_block(audit, ss)
    if hotel_block:
        story += _section('4', 'Hotel Expense — Detailed Breakdown', hotel_block)
    story += _section('5', 'Policy Concerns Summary',               breach_summary_block(audit, ss))
    story += _section('6', 'Final Amount Summary',                  eligible_amount_block(audit, ss))
    story += _section('7', 'Recommendations to Finance / HR',       recommendations_block(audit, ss))
    if audit.get('claude_verdict'):
        story += _section('8', 'AI Audit Verdict',                  claude_verdict_block(audit, ss))
        story += _section('9', 'Audit Conclusion',                  conclusion_block(audit, ss))
    else:
        story += _section('8', 'Audit Conclusion',                  conclusion_block(audit, ss))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def output_filename(audit):
    voucher = audit['voucher']
    emp     = audit['employee']
    name    = (emp.get('name_master') or voucher.get('employee_name') or 'Unknown').replace(' ', '')
    code    = voucher.get('employee_code') or 'XXXX'
    no      = voucher.get('voucher_no') or 'X'
    return f'RiteAuditReport_Voucher{no}_{name}_{code}.pdf'


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('audit_json')
    ap.add_argument('--out', default=None)
    ap.add_argument('--out-dir', default='.')
    args = ap.parse_args()
    with open(args.audit_json) as f:
        audit = json.load(f)
    out_path = args.out or os.path.join(args.out_dir, output_filename(audit))
    render_pdf(audit, out_path)
    print(f'PDF written to {out_path}')
