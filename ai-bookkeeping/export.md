# Handing the books to the accountant's software

Part of the AI bookkeeping skill: read this before running `export`.

When the accountant works in their own software, give them an import file next to the report.

**One export or the other, never both.** Billpal's own DATEV, BMD, and DVO exports book each invoice against the supplier or customer account, for an accountant who does the bookkeeping. This export holds the whole ledger: costs against the bank (cash basis) or the payable and receivable accounts (accrual), plus transfers and private money. Imported together for the same period, every cost is booked twice. With a Billpal account, ask first: "Does your accountant already get Billpal's DATEV, BMD, or DVO export?" If yes, ask which one they want, and give only that one.


```
python3 ledger.py export <ledger> --format datev --out-dir books/export --consultant 1234567 --client 10001
```

- `datev`: a DATEV Buchungsstapel (EXTF) for a German Steuerberater. Needs the Beraternummer and Mandantennummer (the accountant has them), euro books, and a number on every account (the SKR03 and SKR04 charts have them). Accounts DATEV taxes by itself (Automatikkonten, `auto_tax`) get no tax key, and a rate that does not fit them is refused. Reverse charge and intra-EU purchases (keys 94, 95, 42, 43) carry the supplier's EU VAT ID from Billpal; without one the export warns, and the accountant adds it in DATEV. `--gl-length` when the accountant uses account numbers longer than 4 digits.
- `bmd` and `dvo`: a BuErf import (`Buchungen.csv`) for an Austrian accountant on BMD NTCS or DVO; both take the same file. The Austrian chart (`oekr`) has no numbers for some accounts, because firms number them differently: money in transit, suspense, tax office, discounts, depreciation, SVS, currency differences, and corporate tax prepayments. The export lists the ones the books use: ask the accountant for all of them at once and add each `code` with `journal` (`accounts`).
- `qbo-journal`: a QuickBooks Online journal import. The accounts must exist in QuickBooks with the same names; customer and supplier lines carry the name.
- `xero-journal`: a Xero manual journal import. Needs account codes, `--tax-rate` (the name of Xero's no-tax rate; the default is Tax Exempt in the US, No VAT in GB, BAS Excluded in AU, No GST in NZ), and `--clearing-code`: Xero does not take bank, receivable, payable, or retained-earnings lines in a manual journal, so those go to that clearing account, and the accountant clears it in Xero.

**Customer and supplier accounts (Personenkonten).** On accrual books, the payable and receivable lines go to each supplier's and customer's own account, and those accounts must exist in DATEV, BMD, or DVO before the bookings are imported. With a Billpal account, the ledger uses Billpal's account numbers, so the user exports them from Billpal: **Export**, dataset **Contacts**, format DATEV, BMD, or DVO. The accountant imports that file first (`EXTF_DebKred_Stamm.csv` or `Personenkonten.csv`), then this export. When a contact has no account number in Billpal, the export stops on its account: have the user give it a number in Billpal, then fetch the documents again. Without Billpal, the accountant creates the accounts.

DATEV, BMD, and DVO get one line per booking line, gross with its tax code, the way they book. QuickBooks and Xero get every line as booked. Opening balances stay out: the accountant carries them forward in their own system. `--from` and `--to` limit the period. The export warns when the books are not ready: say so when you hand it over.
