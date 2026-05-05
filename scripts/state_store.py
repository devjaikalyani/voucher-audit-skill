"""
Thin wrapper around history/processed_vouchers.json so daily_run.py and
audit_engine.py both update it the same way.

Every successful audit appends an entry; every new-claim scan checks the
'processed' list to skip vouchers we've already audited (so we never run
twice for the same voucher number on the same day, or any future day).
"""
import os, json, datetime

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(SKILL_ROOT, 'history', 'processed_vouchers.json')


def _empty():
    return {
        '_comment': 'Tracks every voucher that has been audited so we never run twice.',
        'processed': [],
    }


def load():
    if not os.path.exists(STATE_PATH):
        return _empty()
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return _empty()


def save(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def fingerprint(voucher_no, employee_code, voucher_date=None):
    return f"{employee_code or 'XXXX'}__voucher{voucher_no or 'X'}__{voucher_date or 'nodate'}"


def already_processed(voucher_no, employee_code, voucher_date=None):
    fp = fingerprint(voucher_no, employee_code, voucher_date)
    state = load()
    return any(p.get('fingerprint') == fp for p in state.get('processed', []))


def mark_processed(voucher_no, employee_code, employee_name, voucher_date,
                   voucher_pdf_path, proofs_path, audit_pdf_path):
    state = load()
    fp = fingerprint(voucher_no, employee_code, voucher_date)
    # Remove any older entry with the same fp (re-audit override)
    state['processed'] = [p for p in state.get('processed', []) if p.get('fingerprint') != fp]
    state['processed'].append({
        'fingerprint': fp,
        'voucher_no': voucher_no,
        'employee_code': employee_code,
        'employee_name': employee_name,
        'voucher_date': voucher_date,
        'downloaded_at': datetime.datetime.now().isoformat(),
        'audited_at': datetime.datetime.now().isoformat(),
        'voucher_pdf_path': voucher_pdf_path,
        'proofs_path': proofs_path,
        'audit_pdf_path': audit_pdf_path,
        'admin_decision_logged': False,
    })
    save(state)
    return fp


def list_processed():
    return load().get('processed', [])


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == 'list':
        for p in list_processed():
            print(f"{p['fingerprint']}\t{p.get('employee_name')}\t{p.get('audit_pdf_path')}")
    else:
        print(json.dumps(load(), indent=2))
