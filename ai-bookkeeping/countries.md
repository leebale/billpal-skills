# Country rules

Read the section for the user's country before the first booking. Every rule has the years it applies to and its source. Outside those years, say so and do not apply the rule. Ask the user, or their accountant, for the current value.

These rules set defaults and warnings. They are not legal advice and they are not a filing. When a rule decides how something is booked (a threshold, a rate, a method), say which rule you used.

Report codes (Kennzahlen on the VAT return) are `null` in this skill until each mapping has been checked against the current form. Do not write a Kennzahl into `report_codes` from memory. When the user gives you the mapping (for example from their accountant), store it on the rule or the booking and say where it came from.

`ledger.py` holds the depreciation defaults and the standard VAT rate below in `COUNTRY_RULES`. Everything else here is guidance for you.

## Every country

**Last year's VAT.** A business that was VAT-registered last year usually still owes the tax office the VAT for the last month or quarter (or is owed a refund). At setup, ask for that amount and book it as an opening line on `Liabilities:Tax-Office` (a negative amount when owed, positive when refunded), balanced by the equity line like every opening line. The payment early in the year then settles it instead of looking like an expense. The accountant may keep it on a separate prior-year VAT account; follow their chart when they give one.

**A balance in another currency (for example a Wise USD balance).** The ledger has one currency, so a foreign balance is kept as a clearing account in the ledger currency:

- Open one clearing account per balance, for example `Assets:Wallet-USD` with role `clearing`.
- A conversion from the euro account books against it at the euro amount that left the euro account.
- Spending from the foreign balance (a card payment in USD paid from the USD balance) is booked with `journal`, from that balance's statement: the expense against `Assets:Wallet-USD` at the euro value of the conversion it came from, with the USD amount in the meta (`"meta": {"original": "100.00 USD"}`). Without this, those costs are missing from the books.
- At the end of each VAT period, when the foreign balance is zero, book what is left on the clearing account to the FX account (role `fx_diff`) with one journal entry.
- The script keeps no statement chain for the foreign balance (it cannot check balances in another currency). Ask the user for the balance's statement each period and compare the totals by hand.

**Reverse charge derived by the commit (AT and DE).** When a bill from a supplier abroad shows no VAT, `commit --documents` books reverse charge at the standard rate. It assumes a service. A goods invoice from abroad is rebooked as `import` or `intra_community`. Small businesses owe reverse charge VAT on some foreign services too, without deducting it, so their derived bookings, like everyone's, stay unconfirmed until the user (or the accountant) confirms them. Billpal stores reverse charge at 0 % or without a rate; the script fills in the standard rate below in the same way. Germany has no standard rate for 2020 in the script (16 % from July to December), so nothing is derived for that year.

## Austria (AT)

| Topic | Rule | Years | Source |
|---|---|---|---|
| VAT rates | 20 % standard, 10 % and 13 % reduced | 2016– | UStG 1994 § 10 |
| Kleinunternehmer | VAT-exempt up to 55,000 EUR gross turnover a year (`vat_status` `small_business`). No VAT on sales, no input VAT | 2025– | UStG 1994 § 6 Abs. 1 Z 27 |
| Kleinunternehmer before 2025 | 35,000 EUR net | 2020–2024 | UStG 1994 § 6 Abs. 1 Z 27 (old version) |
| VAT period | UVA monthly when last year's turnover was above 100,000 EUR, otherwise quarterly | 2011– | UStG 1994 § 21 |
| UVA due date | The return and the payment are due on the 15th of the second month after the period (for Q1: 15 May). The report shows the date next to each period, moved past a weekend; public holidays are not known | – | UStG 1994 § 21 Abs. 1; BAO § 108 |
| Payment-basis VAT (Istbesteuerung) | The law's default for freelancers (§ 22 Z 1 EStG, at any turnover), for traders not required to keep double-entry books, and for others whose turnover was at most 110,000 EUR in one of the two previous years. Sollbesteuerung only on application, or when the business must keep double-entry books (a GmbH or FlexCo always does). Propose `vat_basis` `payment` for a sole trader on cash basis and `invoice` for a company, then let the user confirm. Input VAT then also counts when paid (`input_vat_basis` follows `vat_basis`, which is the default for AT) | 2019– | UStG 1994 § 17 Abs. 1 and 2, § 12 Abs. 1 Z 1; USP "Zeitpunkt des Entstehens der Steuerschuld" (as of 1 Jan 2026) |
| Cash basis | Einnahmen-Ausgaben-Rechnung (`basis` `cash`) | – | EStG 1988 § 4 Abs. 3 |
| GWG | Assets up to 1,000 EUR net can be written off in full in the year bought | 2023– | EStG 1988 § 13 |
| GWG before 2023 | 800 EUR | 2020–2022 | EStG 1988 § 13 (old version) |
| First-year depreciation | Put into service in the second half of the year: half a year's depreciation (`first_year` `half_year`) | – | EStG 1988 § 7 Abs. 2 |
| Registrierkasse | Required once cash sales pass 7,500 EUR and total turnover passes 15,000 EUR a year. Book the daily totals (Tagesabschluss), not each receipt | 2016– | BAO § 131b |
| Arbeitszimmer in the home | Deductible only when it is the center of the whole business and professional activity. Then the costs (rent, energy, insurance) by floor area: an allocation with `share` by m² and `topic` `home_office` | – | EStG 1988 § 20 Abs. 1 Z 2 lit. d |
| Arbeitsplatzpauschale | When the Arbeitszimmer is not deductible: 1,200 EUR a year without other active income above the limit, otherwise 300 EUR. Either the Pauschale or the Arbeitszimmer, never both. Book it as a `journal` entry with `"meta": {"flat_rate": "home_office"}`. Check the income limit for the year | 2022– | EStG 1988 § 4 Abs. 4 Z 8 |

Things that come up in almost every Austrian small business:

- **Abgabenkonto.** One tax-office account for VAT, income tax prepayments, and other levies. A single payment often covers several taxes. Split every payment by what it pays: VAT payments settle the VAT liability (`Liabilities:Tax-Office` or the VAT account), income tax for a sole trader or partner is private (`Equity:Private`). When the bank text does not say, ask the user what the payment was: the UVA, an income tax prepayment, or both, and how much of each. The Buchungsmitteilung or the FinanzOnline account statement shows it.
- **GmbH and FlexCo.** Körperschaftsteuer prepayments, including the Mindestkörperschaftsteuer, also run through the Abgabenkonto. Book them to `Assets:Corporate-Tax-Prepaid` (EKR group 25); the accountant books the tax for the year (group 84) at the year end. Money between the company and a shareholder goes to `Liabilities:Shareholder` (EKR 346, Verbindlichkeiten gegenüber Gesellschaftern). Source: Österreichischer Einheitskontenrahmen, Kontenklassen 2, 3, and 8.
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
| VAT basis | The invoice basis (Soll-Versteuerung) is the default. The payment basis (Ist-Versteuerung) needs the tax office's permission, usually asked for in the registration questionnaire: allowed when last year's turnover was at most 800,000 EUR, when the business is exempt from keeping books, or for freelance turnover without double-entry books. Propose `vat_basis` `invoice` unless the user says they have the permission. Input VAT stays on the invoice basis either way (`input_vat_basis` `invoice`, the default for DE) | 2024– | UStG § 13 Abs. 1 Nr. 1, § 16, § 20, § 15; BFH V R 16/24 (16 Apr 2026) |
| Cash basis | Einnahmen-Überschuss-Rechnung allowed while not required to keep double-entry books: turnover up to 800,000 EUR and profit up to 80,000 EUR a year | 2024– | AO § 141 |
| GWG | Assets up to 800 EUR net can be written off in full in the year bought | 2018– | EStG § 6 Abs. 2 |
| First-year depreciation | Monthly, from the month put into service (`first_year` `monthly`) | – | EStG § 7 Abs. 1 Satz 4 |
| Computers and software | A useful life of one year may be used | 2021– | BMF-Schreiben vom 22.02.2022 |
| Reverse charge | The customer owes the VAT, for example on services from other EU countries. Book as `reverse_charge` with the rate | – | UStG § 13b |
| Skonto | A discount taken reduces the VAT on the invoice. Book the discount with the invoice's tax treatment | – | UStG § 17 |
| UStVA due date | The return and the payment are due on the 10th day after the month or quarter. A Dauerfristverlängerung adds a month (profile `vat_extension` `true`); monthly filers pay a special prepayment of 1/11 of last year's VAT for it. The report shows the date next to each period, moved past a weekend; public holidays are not known | – | UStG § 18 Abs. 1; UStDV §§ 46–48; AO § 108 Abs. 3 |
| Bewirtung | 70 % of business meals with customers or partners is deductible, the input VAT in full. Give the line `deductible` `"0.7"`: the rest goes to `Expenses:Meals:Nondeductible` (SKR03 4654, SKR04 6644). The receipt needs the occasion and the guests (Bewirtungsbeleg) | – | EStG § 4 Abs. 5 Satz 1 Nr. 2; UStG § 15 Abs. 1a |
| Häusliches Arbeitszimmer | Deductible when it is the center of the whole business and professional activity: the actual costs by floor area (an allocation with `topic` `home_office`) or a yearly flat amount (Jahrespauschale) instead | 2023– | EStG § 4 Abs. 5 Satz 1 Nr. 6b |
| Homeoffice-Pauschale | A flat amount per day worked at home, up to a yearly cap, when there is no deductible Arbeitszimmer. Not on top of the Arbeitszimmer. Book a flat amount as a `journal` entry with `"meta": {"flat_rate": "home_office"}` | 2023– | EStG § 4 Abs. 5 Satz 1 Nr. 6c |

The amounts of the Jahrespauschale and the Homeoffice-Pauschale are not in this file. Look them up in the official source for the year, or ask the accountant.

**GmbH and UG.** Book Körperschaftsteuer prepayments, with the Solidaritätszuschlag on them, to `Expenses:Corporate-Tax` (SKR03 2200, SKR04 7600). Money between the company and a shareholder goes to `Liabilities:Shareholder` (SKR03 1665, SKR04 3640, Verbindlichkeiten gegenüber GmbH-Gesellschaftern). When the shareholder owes the company at the year end, the accountant moves the balance to the receivable account (SKR03 1381, SKR04 1307). Source: DATEV-Kontenrahmen SKR03 and SKR04, 2026.

Not modeled: the Sammelposten for assets between 250 and 1,000 EUR (EStG § 6 Abs. 2a). When the user uses it, book the items as one asset per year with a 60-month life and `first_year` `full_year`, and say so.

## United States (US)

| Topic | Rule | Years | Source |
|---|---|---|---|
| VAT | None. Sales tax is set by each state and city. Book collected sales tax as a liability (`tax.type` `sales_tax`), never as income | – | State law |
| Basis | Cash or accrual, chosen on the first return | – | IRC § 446 |
| De minimis safe harbor | Items up to 2,500 USD each can be expensed (5,000 USD with an audited financial statement). Book them as GWG (`method` `gwg`; the 2,500 USD limit is built in) when the user makes the election. Readiness flags larger expenses on equipment accounts | 2016– | Treas. Reg. § 1.263(a)-1(f) |
| Estimated tax | A sole proprietor or partner pays income and self-employment tax in four installments (Form 1040-ES): 15 April, 15 June, 15 September, and 15 January of the next year. These payments are private (`Equity:Owners-Draw`), never a business expense | – | IRS Form 1040-ES |
| Meals | 50 % deductible when business is discussed. Give the line `deductible` `"0.5"`: half stays on `Expenses:Meals`, half goes to `Expenses:Meals:Nondeductible`, and the draft Schedule C shows line 24b | 2018– | IRC § 274(k), (n) |
| Entertainment | Not deductible (tickets, golf, shows). Book to `Expenses:Entertainment` so it stays out of the meals | 2018– | IRC § 274(a) as amended by TCJA |
| Business gifts | Deductible up to 25 USD per recipient per year | – | IRC § 274(b) |
| Mileage | Standard rate: 72.5 cents a mile from 1 January to 30 June 2026, 76 cents from 1 July 2026. Book a mileage claim as a `journal` entry from `Equity:Owners-Investment` (the owner paid the car) with `"meta": {"flat_rate": "mileage", "miles": "…"}`. The standard rate or actual car costs, not both for the same car | 2026 | IRS Notice 2026-10, Announcement 2026-11 |
| Home office | Used regularly and only for the business: either the simplified method (5 USD per square foot, up to 300 square feet, as a `journal` entry with `"meta": {"flat_rate": "home_office"}`), or actual costs by floor area on Form 8829 (an allocation with `topic` `home_office`) | 2013– | Rev. Proc. 2013-13; IRC § 280A |
| 1099-NEC | The business files a 1099-NEC for each contractor (not a corporation, with exceptions such as lawyers) paid at least 2,000 USD in the year for services. Card and PayPal payments are reported by the processor instead (1099-K) and do not count. Ask for a W-9 before the first payment | payments in 2026 (600 USD before) | IRS Instructions for Forms 1099-MISC and 1099-NEC (Rev. December 2026) |
| Startup costs | Up to 5,000 USD of costs before the business opened are deductible in the first year (less the amount above 50,000 USD), the rest over 180 months. Book them as normal expenses with `"meta": {"startup": "yes"}`; the CPA splits them | – | IRC § 195 |
| Self-employed health insurance, SEP-IRA, Solo 401(k) | For a sole proprietor or partner these are personal deductions on the 1040, not business expenses: book them to `Equity:Owners-Draw` with `"meta": {"topic": "health_insurance"}` or `"retirement"`, so the CPA finds them. In an S or C corporation, the company's own contributions are business expenses | – | IRS Form 7206; Pub. 560 |

**Sales tax by state.** Sales tax belongs to the state (and often the city or county) where the customer receives the goods. Give each tax a `jurisdiction`, for example `CA` or `NY-NYC`, and open one `vat_out` account per state the business is registered in, with `tax_jurisdiction` (for example `Liabilities:Sales-Tax:CA`). A state without its own account uses the general one. The report groups sales tax by jurisdiction and period.

- **Use tax.** A purchase from another state with no sales tax on it: the buyer owes use tax to their own state when the item is taxable there and no exemption applies (goods bought for resale are exempt, for example). Never add use tax on a guess. Book it as `{"type": "sales_tax", "regime": "reverse_charge", "rate": "…", "jurisdiction": "CA"}`. The cost goes up by the tax and the state's account gets the liability; the report calls it use tax.
- **Marketplace sales.** Amazon, Etsy, eBay, and similar platforms collect and pay the sales tax themselves (marketplace facilitator laws). Book the sale without that tax (`regime` `out_of_scope`, `exemption_reason` "collected by the marketplace"), and the platform's fees as expenses.
- **What is taxable.** States differ on whether software, SaaS, digital goods, and services are taxable at all. Before booking sales tax on a sale or a use tax on a purchase, ask which of the business's products and purchases are taxable in which states; the CPA knows.

**LLCs and corporations.** An LLC taxed as a sole proprietorship or a partnership keeps the owner's draw and investment accounts (`company` `false`). A C corporation, an S corporation, or an LLC that elected to be taxed as one is a company (`company` `true`): money between the company and a shareholder goes to `Liabilities:Due-to-Shareholder` until the CPA books it as a loan, a contribution, or a distribution. The `qbo` chart has no income tax account; add one when the CPA gives it.

- **S corporation.** The owner who works in the business is paid a salary through payroll (`Expenses:Payroll`, `Expenses:Payroll-Taxes`, `Liabilities:Payroll-Liabilities`). Other money the owner takes is a distribution (`Equity:Distributions`), which closes into retained earnings at year end. Ask which it is, never guess: the split matters for payroll tax. With two owner accounts, a rule with `share` needs `private_account`.
- **Partnership (or an LLC taxed as one).** Each partner has a capital account (role `retained_earnings`, with `partner` and `share`, the profit share as a decimal, for example `0.6`) and a draw account (role `private`, with `partner`). Skip the chart's single retained earnings and owner accounts at init (`skip_accounts`). At year end, the rollover splits the result by `share` and closes each partner's draws into their own capital. Shares must add up to 1. Guaranteed payments to a partner are an expense (`Expenses:Guaranteed-Payments`, opened when needed), not a draw.

`ledger.py` has no US depreciation default. Tax depreciation (MACRS, Section 179, bonus depreciation) is not modeled: book straight-line depreciation for the books, give `first_year`, and tell the user their CPA sets the tax depreciation.

## Other countries

Ask for the tax family (VAT, GST, sales tax), the rates, the treatment names the user's accountant uses, and the chart of accounts. Use the user's own chart. Give `first_year` and `gwg_limit` on each asset record, because the script has no default for the country. For a company (for example a UK Ltd), open the shareholder account (in the UK the director's loan account) with role `shareholder` and leave out private accounts.
