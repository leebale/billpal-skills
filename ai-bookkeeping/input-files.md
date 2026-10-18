# Input files

The JSON files `ledger.py` reads. Part of the AI bookkeeping skill: `SKILL.md` explains when each one is used.

Unknown fields are rejected. Money is a decimal string (`"1190.00"`), never a JSON number. Dates are `YYYY-MM-DD`. Tax rates are percentage points as strings.

## init.json

```json
{
  "schema": "init/1",
  "title": "Huber Design e.U.",
  "file_label": "2026",
  "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
  "profile": {"country": "AT", "legal_form": "Einzelunternehmen", "basis": "cash", "vat_status": "regular",
              "vat_basis": "payment", "vat_period": "quarter", "chart": "oekr", "language": "de"},
  "chart": "oekr",
  "accounts": [{"account": "Assets:Bank:Giro", "role": "bank", "code": "2800", "name": "Erste Giro", "iban": "AT61..."}],
  "opening": {"lines": [{"account": "Assets:Bank:Giro", "amount": "5000.00"},
                        {"account": "Equity:Retained-Earnings", "amount": "-5000.00"}]},
  "rules": [], "allocations": [], "csv_maps": [], "assets": []
}
```

- `profile`: also `company` (`true` for a company, including an S corporation; the chart then opens the shareholder account instead of the private accounts), `input_vat_basis` (defaults by country), `vat_extension` (`true` with a DE Dauerfristverlängerung), `business`, `employees`, `tax_advisor`, `notes`.
- `chart`: a built-in chart from `charts.csv`, or leave it out and give every account under `accounts`. `skip_accounts` drops chart accounts the business does not need.
- `accounts`: `account` (Beancount path), `name`, `code`, `role`, `personenkonto`, `iban`, `tax_regime`, `tax_rate`, and `tax_jurisdiction` (for tax accounts, for example one sales tax account per US state), `partner` and `share` (a partnership's capital and draw accounts, see `countries.md`), `auto_tax` (the VAT rate DATEV applies on this account by itself, for an Automatikkonto; the SKR charts set it), `tax_line` (the line on the owner's tax form, for example `11` on Schedule C, or `none` for a cost that is not deductible; the QBO chart sets the Schedule C lines), `billpal_account`, `date` (the day it was opened, when after the start of the year).
- Roles: `bank`, `cash`, `card`, `processor`, `clearing`, `ar`, `ap`, `vat_in`, `vat_out`, `suspense`, `discount_received`, `discount_granted`, `depreciation`, `accumulated_depreciation`, `fixed_asset`, `retained_earnings`, `private` (closed into retained earnings at year end), `shareholder` (carried forward), `opening`, `fx_diff`. A sub-account inherits its parent's role. A tax account matches by role, regime, and rate.
- `opening`: balanced lines on the first day. Accrual books give each open invoice its own line with `document`, `document_number`, `document_date`, `due_date`, and `party`. `suspense_confirmed` `true` when the user agreed to park a difference. Or `"new_business": true` without opening lines.
- `previous`: set by `rollover`.

## Statement (`statement/1`)

```json
{"schema": "statement/1", "statements": [{
  "source_id": "erste-2026-01", "account": "Assets:Bank:Giro", "kind": "bank",
  "from": "2026-01-01", "to": "2026-01-31", "opening": "5000.00", "closing": "1960.00",
  "extraction": "model", "count": 5,
  "rows": [{"booking_date": "2026-01-05", "value_date": "2026-01-05", "amount": "-120.00",
            "counterparty": "Adobe", "iban": null, "text": "Creative Cloud 01/26", "reference": null,
            "bank_id": null, "balance": "4880.00"}]}]}
```

- `kind`: `bank`, `card`, `cashbook`, `processor`. `extraction`: `camt`, `csv`, `ofx`, `model`, `connector`, `manual`.
- `check_number` on a row: the number of a paper check.
- `account` or `iban` identifies the account. `count` and row `balance` are checked when given. `confirmed_by_user` for a model-read statement with neither.
- A row in a foreign currency adds `"fx": {"amount": "100.00", "currency": "USD", "rate": "0.92"}`.
- `method` (`direct_debit` or `bank_transfer`) only when the statement proves it. `from-camt` fills it from the bank's transaction code.
- A statement that crosses the fiscal-year end is rejected: split it.

## Decisions (`decisions/1`)

```json
{"schema": "decisions/1",
 "accept_suggested": true,
 "groups": [{"vendor_key": "text:adobe systems software",
             "lines": [{"account": "Expenses:Software", "tax": {"regime": "reverse_charge", "rate": "20", "source": "user"}}],
             "confirmed": "user", "document": "billpal:1234"}],
 "rows": [{"ref": "1:4", "lines": [{"account": "Liabilities:Tax-Office", "amount": "320.00"}, {"account": "Equity:Private"}],
           "confirmed": "user", "no_document": "Abgabenkonto: VAT and income tax prepayment"}],
 "accounts": [], "rules": [], "assets": [], "confirmations": []}
```

- `groups` decide every row of a vendor group; `rows` (by `ref`, statement number and row number) decide single rows and win over groups.
- Per decision: `decision` (`book`, or `duplicate` for an ambiguous row that is already booked), `payee`, `narration`, `lines`, `document` or `documents`, `no_document`, `confirmed` (`user`, `rule`, `document`, `no`), `classified_by`, `match`, `document_review` (`open` when the document is still in review), `settles` (`true` when the user says a short payment settles the document, for write-back on cash basis), `meta`.
- `accept_suggested` books rows a stored rule matched.
- `review_holds`: documents still in review that the period depends on (`document`, `date`, `note`). Readiness fails up to that date until the document is booked. Holds found by `candidates` are added by `commit` on their own.
- Lines: `account`, `amount` (one line may leave it out and gets the rest), `amount_is` (`gross` by default, or `net`), `tax`, `deductible` (the share the tax law deducts, see "Tax treatment" in `SKILL.md`), `document`, `allocations` (`[{"document": "billpal:…", "amount": "-1190.00"}]`, signed like the posting; `document` `null` for not yet allocated), `asset`, `description`, `meta`.
- For documents: `documents` by `id`, with `party_account` or `party_confirmed`, `lines` by `index` (`account`, `tax`, `amount_is`, or `asset` overrides), `confirmed`, `classified_by`, `applies_to`, `review_resolved`, or `decision` `skip`.

## Tax object

```json
{"type": "vat", "regime": "standard", "rate": "20", "source": "document"}
```

`type`: `vat` (the default), `gst`, `sales_tax` (US: collected on sales, part of the cost on purchases), or `other`.

Also: `amount` (the tax printed on the document, when it differs from the calculation by rounding), `recoverable` (share of input VAT that can be deducted, `"1"` by default), `point` (tax date), `direction` (`purchase` or `sale`, defaults from the account), `exemption_reason`, `jurisdiction` (US sales tax: the state, or state and city, for example `CA` or `NY-NYC`; the matching `tax_jurisdiction` account takes the tax), `report_codes` (only when verified: `[{"code": "…", "part": "base|tax|input_tax"}]`), `confirmed`. `source` `derived` is set by the commit only, never by you.

## Documents (`documents/1`)

```json
{"schema": "documents/1", "documents": [{
  "id": "billpal:1234", "kind": "invoice", "direction": "in", "number": "L-555",
  "date": "2026-01-10", "due_date": "2026-02-09", "currency": "EUR",
  "party": {"name": "Lieferant Schulz KG", "iban": "DE02...", "personenkonto": "70001", "country": "DE"},
  "total": "1190.00",
  "taxes": [{"type": "vat", "regime": "standard", "rate": "19", "base": "1000.00", "amount": "190.00"}],
  "lines": [{"description": "Ware", "amount": "1000.00", "amount_is": "net", "account_code": "3400",
             "tax": {"regime": "standard", "rate": "19"}}],
  "terms": {"skonto_percent": "2", "skonto_days": 10, "net_days": 30},
  "linked": ["billpal:5678"], "review_open": false, "source": "billpal",
  "url": "https://billpal.io/dashboard/documents/1234?workspace=…"}]}
```

- `kind`: `invoice`, `credit_note`, `receipt`, `reminder`, `other`. A credit note gives `applies_to` with the invoice it reduces.
- A document in another currency needs `fx_rate` (1 unit = x in the ledger currency).
- Without `lines`, the tax lines (with bases) or the total become the lines.
- `discount`: a discount on the document that its lines do not include yet (positive). `total` and tax bases are after it.
- Line `amount_is`: `net`, `gross`, or `unknown`. Empty tax fields are `null`.
- `url`: where the document opens (http or https only).

## Journal (`journal/1`)

```json
{"schema": "journal/1",
 "entries": [{"date": "2026-02-28", "payee": "Umbuchung", "narration": "…", "confirmed": "user", "no_document": "transfer",
              "lines": [{"account": "Assets:Transit", "amount": "300.00"}, {"account": "Assets:Cash", "amount": "-300.00"}]}],
 "accounts": [], "rules": [], "allocations": [], "csv_maps": [], "assets": [], "profile": {},
 "confirmations": [{"kind": "booking", "ref": "<txn_id>"}],
 "questions": [{"id": "apple-0201", "question": "…", "proposal": "…", "evidence": "…"}]}
```

Confirmation kinds: `booking`, `tax`, `document_review`. `ref` is the booking's `txn_id`, or the document id for a review. A question stored again with the same `id` and an `answer` is answered.

## Rule

```json
{"id": "svs", "text": "Sozialversicherung", "money": "out", "account": "Expenses:SVS",
 "tax": {"regime": "out_of_scope", "source": "rule"}}
```

Also: `iban`, `amount_min`, `amount_max`, `on_account`, `payee`, `priority` (higher wins), `needs_review` (suggest, but leave unconfirmed), `share` (the business part; the rest goes to the private account), `deductible` (the deductible part of a business cost, not together with `share`), `private_account` (when the chart has more than one), `disabled`, `note`. A rule matches on `text` (in the payment text, counterparty, or reference) or `iban`. A rule stored again with the same `id` replaces the old one.

## Allocation

```json
{"id": "rent", "text": "PMV Immobilien", "money": "out", "account": "Expenses:Rent", "share": "8.5/52",
 "basis": "8.5 m² office of 52 m²", "tax": {"regime": "out_of_scope", "source": "rule"},
 "every": "month", "from": "2026-01-01", "topic": "home_office", "document": "Mietvertrag 2024"}
```

Also: `iban`, `amount_min`, `amount_max`, `to` (end date, for example after a move), `private_account`, `payee`, `disabled`, `note`. To change a share, store the allocation again with the same `id`; to end it, give `to` or `disabled`. Earlier bookings stay as they are.

## CSV map

```json
{"id": "erste-csv", "delimiter": ";", "encoding": "utf-8-sig", "decimal": ",", "thousands": ".",
 "date_format": "%d.%m.%Y", "skip_rows": 0, "order": "asc",
 "columns": {"booking_date": "Buchungsdatum", "value_date": "Valuta", "amount": "Betrag",
             "text": "Buchungstext", "counterparty": "Partnername", "iban": "Partner IBAN", "balance": "Saldo"}}
```

Columns: `booking_date`, `value_date`, `amount` (or `debit` and `credit`), `text`, `counterparty` (one header, or a list where the first filled one wins), `iban`, `reference`, `balance`, `bank_id`, `currency`, and `original_amount` with `original_currency` (together). The bank's own exchange rate column is not used: the rate is the booked amount divided by the original. `invert_sign` for banks that print debits as positive. `order` `desc` when the newest row comes first.

## Asset

```json
{"id": "macbook", "description": "MacBook Pro", "document": "billpal:1234", "in_service": "2026-01-08",
 "cost": "1500.00", "life_months": 36, "first_year": "half_year", "private_share": "0.2",
 "asset_account": "Assets:Fixed:Furniture", "depreciation_account": "Expenses:Depreciation",
 "private_account": "Equity:Private"}
```

Also: `method` (`straight_line` or `gwg`), `acquired`, `residual`, `prior_depreciation` (for assets bought in earlier years), `accumulated_account` (required when the chart has one), `gwg_limit`, `disposed`, `note`. `first_year`: `monthly`, `half_year`, or `full_year`; defaults by country, see `countries.md`.
