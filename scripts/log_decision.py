"""
Record the admin's final verdict on an audited voucher to history/decisions/.

After the admin reviews the Rite Audit and decides the actual approved amount,
run:

    python log_decision.py <audit.json>                      \
        --final-amount <RUPEES>                              \
        --note "<reasoning>"                                 \
        [--per-line <expense_head>=<amount>] ...

The next audit run picks these decisions up automatically (history_patterns()
in audit_engine.py) so future vouchers benefit from the precedent.
"""
import sys, os, json, argparse, datetime

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DIR = os.path.join(SKILL_ROOT, 'history', 'decisions')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audit_json')
    ap.add_argument('--final-amount', type=float, required=True,
                    help='Total amount admin actually approved')
    ap.add_argument('--note', default='', help='Admin reasoning (free text)')
    ap.add_argument('--per-line', action='append', default=[],
                    help='Per-line override, format <expense_head>=<amount>. '
                         'Repeat for each line.')
    args = ap.parse_args()

    with open(args.audit_json) as f:
        audit = json.load(f)

    per_line = {}
    for tok in args.per_line:
        if '=' not in tok:
            continue
        k, v = tok.split('=', 1)
        try:
            per_line[k.strip()] = float(v.strip())
        except ValueError:
            continue

    decisions = []
    for item in audit['findings']:
        admin_final = per_line.get(item['expense_head'])
        if admin_final is None:
            # if no per-line override, default to policy_eligible
            admin_final = item.get('policy_eligible_inr') or item['claimed_inr']
        decisions.append({
            'date': item['date'],
            'expense_head': item['expense_head'],
            'claimed': item['claimed_inr'],
            'reviewer_approved': item['approved_by_reviewer_inr'],
            'policy_eligible': item['policy_eligible_inr'],
            'admin_final': admin_final,
            'decision_pattern_note': args.note,
        })

    record = {
        'voucher_no': audit['voucher'].get('voucher_no'),
        'voucher_date': audit['voucher'].get('voucher_date'),
        'employee_code': audit['voucher'].get('employee_code'),
        'employee_name': audit['voucher'].get('employee_name'),
        'designation': audit['employee'].get('designation_master'),
        'final_total_approved': args.final_amount,
        'admin_reasoning': args.note,
        'recorded_at': datetime.datetime.now().isoformat(),
        'decisions': decisions,
    }
    os.makedirs(HIST_DIR, exist_ok=True)
    out_path = os.path.join(HIST_DIR, f'{record["voucher_no"]}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(record, f, indent=2, ensure_ascii=False, default=str)
    print(f'Decision logged: {out_path}')


if __name__ == '__main__':
    main()
