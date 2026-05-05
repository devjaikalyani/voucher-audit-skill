# Learning Loop — How Admin Decisions Improve Future Audits

The skill never edits policy on its own; instead it accumulates a record of
what the admin actually decided and surfaces those precedents on future runs
so the admin can stay consistent.

## Workflow

1. Admin runs `scripts/run_audit.py` on a new voucher → gets the Rite Audit PDF.
2. Admin makes a final decision (approving, reducing, or rejecting amounts).
3. Admin runs `scripts/log_decision.py` recording the final approved total
   and free-text reasoning.
4. The next time the same employee submits a voucher, the audit conclusion
   includes a "History-driven observations" block listing past decisions on
   the same expense heads.

## Storage format

`history/decisions/<voucher_no>.json`:

```json
{
  "voucher_no": "3315",
  "voucher_date": "2026-03-23",
  "employee_code": "RWSIPL570",
  "employee_name": "Jai Dutt Sharma",
  "designation": "General Manager",
  "final_total_approved": 18000,
  "admin_reasoning": "Hotel reduced to VP/GM grade-C cap Rs.1500/night. Cruise approved as VP/GM had pre-approved client visit.",
  "recorded_at": "2026-04-28T10:00:00",
  "decisions": [
    {
      "date": "2026-03-13",
      "expense_head": "Hotel",
      "claimed": 15120,
      "reviewer_approved": 15120,
      "policy_eligible": 12000,
      "admin_final": 12000,
      "decision_pattern_note": "Reduced to VP/GM cap..."
    }
  ]
}
```

## What the engine does with this

`audit_engine.history_patterns()` reads every JSON in
`history/decisions/`, filters by employee_code and the expense heads in the
new voucher, and surfaces up to 8 short notes in the conclusion section.

The pattern is intentionally simple — it's a memory aid for the admin, not
an autonomous policy override. If the admin notices the audit engine is
making the same wrong call repeatedly, that's a signal to update
`assets/policy_rules.json` directly (with sign-off from finance/HR).

## Privacy / retention

Decisions live in plain JSON inside the skill folder. They do not contain
proof images or PII beyond what's already in the voucher. Retention is up to
the company — files can be archived or deleted at any time without breaking
the engine.
