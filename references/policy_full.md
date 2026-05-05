# Rite Water Solutions — Tour & Travel Policy (Encoded Reference)

Effective 1 November 2023. This is a readable summary of what
`assets/policy_rules.json` encodes; the JSON is the source of truth used by the
audit engine. Update both together when policy changes.

## Designation buckets and entitlements

| Bucket | Hotel A | Hotel B | Hotel C | Food w/ Stay | Food w/o Stay (>12hr) | Travel Class | Local Conveyance |
|---|---|---|---|---|---|---|---|
| Director / CFO | Actual | Actual | Actual | Actual | Actual | Actual | Actual |
| VP / GM | 3000 | 2000 | 1500 | 750 | 375 | Flight up to Rs.5000 (>500km), 1AC/2AC Train | 4 wheeler |
| Sr. Manager / Regional Mgr | 2000 | 1500 | 1000 | 600 | 300 | 2AC Train / Bus AC | 4 wheeler |
| Manager | 1500 | 1200 | 750 | 500 | 250 | 3AC Train / AC Bus | Auto / 2-wheeler / 4-wheeler |
| Asst. Manager / Deputy Mgr | 1200 | 1000 | 750 | 500 | 250 | 3AC Train / AC Bus | Auto / 2-wheeler |
| Sr. Executive / Supervisor | 1100 | 1000 | 750 | 400 | 200 | Sleeper Train / Non-AC Bus | 2-wheeler / Auto / Share Auto |
| Technician / Trainee | 1000 | 900 | 700 | 400 | 200 | Sleeper Train / Non-AC Bus | 2-wheeler / Auto / Share Auto |

All amounts in INR per night (hotel) or per day (food).

## City grades

- **Grade A** (Metro & Tier 1): Delhi, Mumbai, Kolkata, Chennai, Ahmedabad,
  Bangalore, Hyderabad, Jaipur, Pune.
- **Grade B** (State Capitals & Tier II): Agra, Amritsar, Baroda, Faridabad,
  Ghaziabad, Indore, Jabalpur, Jamshedpur, Kanpur, Kochi, Mysuru, Nagpur,
  Surat, Vishakapatnam — and all other state capitals.
- **Grade C**: Everything else.

## Site Deputation (>30 days, no company guest house)

When an employee is on a single site for more than 30 days and no company
guest house is available, special lodging caps apply. If travel is more than
15 days in a month, fixed fooding policy with a Rs.7000 max applies for
Project Manager grade and above.

| Designation | Lodging A | Lodging B | Lodging C | Fooding |
|---|---|---|---|---|
| Project Manager | 12000 | 10000 | 8000 | 7000 |
| Asst. Mgr / Deputy Mgr | 10500 | 9000 | 7000 | 6000 |
| Engineer | 9000 | 7500 | 5000 | 5000 |
| Technician | 7000 | 5500 | 4000 | 4000 |

## Hard rules applied to every voucher

- **Max claim period** — 90 days. Claims older than 90 days are flagged.
- **Manager approval threshold** — any voucher over Rs.10,000 needs senior
  signature on file.
- **Duplicate proof detection** — `flag_duplicate_receipts = True`. Same ride
  ID, invoice number or txn ID submitted twice → reject the duplicate.
- **No handwritten receipts** — typed/printed bills only.
- **Hospitality / entertainment** — cruise bookings, client meals, dinners
  for external officials are NOT covered under standard travel reimbursement.
  Requires VP/GM pre-approval; cap Rs.5,000/month under Other Expense if
  approved.
- **Train class entitlement** — designation determines max class. Higher
  class fare requires designation override.
- **Checkout-day food** — if no overnight stay on checkout day, use
  without-stay (>12hr) rate, not with-stay rate.
- **Petrol charges** — Rs.3/km bike, Rs.9/km car, with km log.

## Proof requirements per category

- **Hotel** — tax invoice + payment receipt
- **Train** — PNR / e-ticket + fare receipt; internal debit voucher alone
  insufficient
- **Bus / Flight** — ticket / boarding pass + receipt
- **Cab / Auto** — receipt with ride ID, route, amount, date
- **Food (per-diem)** — no bills required; only payable with overnight stay
  proof (or duration > 12hr for without-stay)
- **Toll / FASTag** — toll receipt
- **Other expense** — original receipt; cap Rs.5,000/month
- **Site / Vehicle expense** — workshop tax invoice + payment receipt +
  asset reference (vehicle no., site code)
