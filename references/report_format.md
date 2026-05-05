# Audit Report PDF — Section Structure

The PDF mirrors the layout of
`PolicyBased_AuditReport_Voucher3315_JaiDuttSharma.pdf` (the reference sample).

| # | Section | Purpose |
|---|---|---|
| Header | Title bar + voucher/employee info + totals + match flag | At-a-glance summary |
| Alerts | Red-bordered callout listing every HIGH-severity finding | Forces attention |
| 1 | Policy Rules Applied | Shows which rules the engine applied (period, threshold, city grade, designation, duplicate flag, hospitality rule) |
| 2 | Proof Document Review | One row per file in the proofs ZIP with key extracted fields and validity status |
| 3 | Line-Item Policy Evaluation | One block per voucher line item: rule applied, proof status, audit note, recommended action |
| 4 | Hotel Detailed Analysis | Designation × cap × nights × claimed table (only when voucher has a Hotel line) |
| 5 | Food Allowance Designation Impact | Designation × per-day rate × days × claimed table (only when voucher has Food Allowance lines) |
| 6 | Policy Breach Summary | Numbered breach list with severity and amount-at-risk |
| 7 | Policy-Eligible Amount Calculation | Reconciliation table: claimed vs reviewer-approved vs policy-eligible per line |
| 8 | Recommendations | Numbered recommendations with priority (IMMEDIATE / SHORT-TERM / CONFIRMED / LONG-TERM) |
| 9 | Audit Conclusion | Narrative paragraph + history-driven observations + sign-off block |

Sections 4 and 5 are conditional — they only appear when the voucher contains
the relevant line items. This keeps short vouchers (1-2 line items) from
showing empty tables.

## Visual conventions

- Navy `#1F3A5F` for the header bar, matching the sample.
- Section titles in dark blue `#2C5282`, all caps, 12pt.
- Status colors:
  - APPROVE — green `#2F855A`
  - CONDITIONAL — amber `#B7791F`
  - FLAG — orange `#C05621`
  - REJECT — red `#C53030`
- Severity colors mirror status (HIGH red, MEDIUM amber, LOW blue).
- Tables use a light grey grid with white-on-blue header rows.
