# Country rules

Read the section for the user's country before the first booking. Every rule has the years it applies to and its source. Outside those years, say so and do not apply the rule. Ask the user, or their accountant, for the current value.

These rules set defaults and warnings. They are not legal advice and they are not a filing. When a rule decides how something is booked (a threshold, a rate, a method), say which rule you used.

Report codes (Kennzahlen on the VAT return) are `null` in this skill until each mapping has been checked against the current form. Do not write a Kennzahl into `report_codes` from memory. When the user gives you the mapping (for example from their accountant), store it on the rule or the booking and say where it came from.

`ledger.py` holds the depreciation defaults below in `COUNTRY_RULES`. Everything else here is guidance for you.

## Austria (AT)

| Topic | Rule | Years | Source |
|---|---|---|---|
| VAT rates | 20 % standard, 10 % and 13 % reduced | 2016– | UStG 1994 § 10 |
| Kleinunternehmer | VAT-exempt up to 55,000 EUR gross turnover a year (`vat_status` `small_business`). No VAT on sales, no input VAT | 2025– | UStG 1994 § 6 Abs. 1 Z 27 |
| Kleinunternehmer before 2025 | 35,000 EUR net | 2020–2024 | UStG 1994 § 6 Abs. 1 Z 27 (old version) |
| VAT period | UVA monthly when last year's turnover was above 100,000 EUR, otherwise quarterly | 2011– | UStG 1994 § 21 |
| Payment-basis VAT (Istbesteuerung) | Common for Einnahmen-Ausgaben-Rechner. Input VAT then also counts when paid (`input_vat_basis` follows `vat_basis`, which is the default for AT) | 2013– | UStG 1994 § 17, § 12 Abs. 1 Z 1 |
| Cash basis | Einnahmen-Ausgaben-Rechnung (`basis` `cash`) | – | EStG 1988 § 4 Abs. 3 |
| GWG | Assets up to 1,000 EUR net can be written off in full in the year bought | 2023– | EStG 1988 § 13 |
| GWG before 2023 | 800 EUR | 2020–2022 | EStG 1988 § 13 (old version) |
| First-year depreciation | Put into service in the second half of the year: half a year's depreciation (`first_year` `half_year`) | – | EStG 1988 § 7 Abs. 2 |
| Registrierkasse | Required once cash sales pass 7,500 EUR and total turnover passes 15,000 EUR a year. Book the daily totals (Tagesabschluss), not each receipt | 2016– | BAO § 131b |
| Arbeitszimmer in the home | Deductible only when it is the center of the whole business and professional activity. Then the costs (rent, energy, insurance) by floor area: an allocation with `share` by m² and `topic` `home_office` | – | EStG 1988 § 20 Abs. 1 Z 2 lit. d |
| Arbeitsplatzpauschale | When the Arbeitszimmer is not deductible: 1,200 EUR a year without other active income above the limit, otherwise 300 EUR. Either the Pauschale or the Arbeitszimmer, never both. Book it as a `journal` entry with `"meta": {"flat_rate": "home_office"}`. Check the income limit for the year | 2022– | EStG 1988 § 4 Abs. 4 Z 8 |

Things that come up in almost every Austrian small business:

- **Abgabenkonto.** One tax-office account for VAT, income tax prepayments, and other levies. A single payment often covers several taxes. Split every payment by what it pays: VAT payments settle the VAT liability (`Liabilities:Tax-Office` or the VAT account), income tax for a sole trader or partner is private (`Equity:Private`). Ask for the Buchungsmitteilung or the FinanzOnline account statement when the bank text does not say.
- **SVS.** Social insurance of a sole trader is a business expense, out of scope for VAT (`tax.regime` `out_of_scope`). A good first rule.
- **Private use.** A sole trader's private share of a car, phone, or computer: book the business share as the expense, the rest to `Equity:Private`. For bills that come every month, a rule with `share`; for costs paid from a private account, an allocation. For assets, use `private_share` on the asset record.
- **Home office.** Ask whether the room is used only for the business and is the center of the activity. If yes, allocate by floor area. If not, tell the user about the Arbeitsplatzpauschale. The accountant decides which applies.
- **Cash book (Kassabuch).** When the business takes cash, every month needs a cash statement, even an empty one, and the balance may never go below zero.
- **VAT summary.** The report shows VAT by period and treatment. It is a summary for the UVA, not a filing.

## Germany (DE)

| Topic | Rule | Years | Source |
|---|---|---|---|
| VAT rates | 19 % standard, 7 % reduced | 2007– (except 2nd half 2020: 16 % and 5 %) | UStG § 12 |
| Kleinunternehmer | No VAT when last year's turnover was at most 25,000 EUR and this year's stays at most 100,000 EUR (`vat_status` `small_business`) | 2025– | UStG § 19 |
| Kleinunternehmer before 2025 | 22,000 EUR last year, 50,000 EUR expected this year | 2020–2024 | UStG § 19 (old version) |
| Payment-basis VAT (Ist-Versteuerung) | Allowed up to 800,000 EUR turnover last year. Input VAT stays on the invoice basis (`input_vat_basis` `invoice`, the default for DE) | 2024– | UStG § 20, § 15 |
| Cash basis | Einnahmen-Überschuss-Rechnung allowed while not required to keep double-entry books: turnover up to 800,000 EUR and profit up to 80,000 EUR a year | 2024– | AO § 141 |
| GWG | Assets up to 800 EUR net can be written off in full in the year bought | 2018– | EStG § 6 Abs. 2 |
| First-year depreciation | Monthly, from the month put into service (`first_year` `monthly`) | – | EStG § 7 Abs. 1 Satz 4 |
| Computers and software | A useful life of one year may be used | 2021– | BMF-Schreiben vom 22.02.2022 |
| Reverse charge | The customer owes the VAT, for example on services from other EU countries. Book as `reverse_charge` with the rate | – | UStG § 13b |
| Skonto | A discount taken reduces the VAT on the invoice. Book the discount with the invoice's tax treatment | – | UStG § 17 |
| Häusliches Arbeitszimmer | Deductible when it is the center of the whole business and professional activity: the actual costs by floor area (an allocation with `topic` `home_office`) or a yearly flat amount (Jahrespauschale) instead | 2023– | EStG § 4 Abs. 5 Satz 1 Nr. 6b |
| Homeoffice-Pauschale | A flat amount per day worked at home, up to a yearly cap, when there is no deductible Arbeitszimmer. Not on top of the Arbeitszimmer. Book a flat amount as a `journal` entry with `"meta": {"flat_rate": "home_office"}` | 2023– | EStG § 4 Abs. 5 Satz 1 Nr. 6c |

The amounts of the Jahrespauschale and the Homeoffice-Pauschale are not in this file. Look them up in the official source for the year, or ask the accountant.

Not modeled: the Sammelposten for assets between 250 and 1,000 EUR (EStG § 6 Abs. 2a). When the user uses it, book the items as one asset per year with a 60-month life and `first_year` `full_year`, and say so.

## United States (US)

| Topic | Rule | Years | Source |
|---|---|---|---|
| VAT | None. Sales tax is set by each state and city. Book collected sales tax as a liability (`tax.type` `sales_tax`), never as income | – | State law |
| Basis | Cash or accrual, chosen on the first return | – | IRC § 446 |
| De minimis safe harbor | Items up to 2,500 USD each can be expensed (5,000 USD with an audited financial statement). Book them as GWG with `gwg_limit` `2500` when the user makes the election | 2016– | Treas. Reg. § 1.263(a)-1(f) |

`ledger.py` has no US depreciation default. Tax depreciation (MACRS, Section 179, bonus depreciation) is not modeled: book straight-line depreciation for the books, give `first_year`, and tell the user their CPA sets the tax depreciation.

## Other countries

Ask for the tax family (VAT, GST, sales tax), the rates, the treatment names the user's accountant uses, and the chart of accounts. Use the user's own chart. Give `first_year` and `gwg_limit` on each asset record, because the script has no default for the country.
