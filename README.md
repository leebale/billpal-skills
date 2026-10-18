# Billpal skills

Free skills for Claude and ChatGPT. Each skill runs on its own. A Billpal account makes some of them easier, because the assistant can read invoices and contacts the user already has. Without an account, the user supplies the same details in the chat.

- [Invoice generator](invoice-generator/): writes an invoice in a Billpal template, your own file, or a plain default. Optionally turns it into a ZUGFeRD / Factur-X e-invoice for sellers in the EU, checked by Billpal; that step needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip)
- [SEPA file generator](sepa-file-generator/): builds one SEPA credit-transfer file (pain.001.001.09) for upload to business online banking. Needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/sepa-file-generator.zip)
- [Delivery-invoice match](delivery-invoice-match/): checks invoices against delivery notes, timesheets, and purchase orders, and lists what was billed but not delivered, price differences, duplicates, and open orders. Documents can come from the chat, a local folder, Billpal, or another connection such as an ERP. Needs code execution turned on for more than a few documents. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/delivery-invoice-match.zip)
- [AI bookkeeping](ai-bookkeeping/): keeps double-entry books in Beancount, one file per fiscal year, from bank statements (CAMT, CSV, MT940, OFX, PDF), bank connections, the cash book, and invoices and receipts. Books VAT, open items, and depreciation, and checks every period before calling it ready for your accountant. Charts of accounts for Austria, Germany, and the US, or your own, and import files for DATEV, BMD, QuickBooks Online, and Xero. Your books are a plain-text file you keep: Billpal holds the documents, never the ledger. Works with a Billpal account, linking each booking to its document, and without one. Needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/ai-bookkeeping.zip)

To use a skill:

- **Claude:** turn on code execution under Settings, Capabilities. Then open Customize, Skills, select +, Create skill, Upload a skill, and choose the ZIP.
- **ChatGPT:** open Plugins in the sidebar, then the Skills tab. Select Create, then Upload from your computer, and choose the ZIP.

A link to this repository in a chat is not enough: neither assistant installs a skill from a link. Step-by-step guide: https://help.billpal.io/en/ai-bookkeeping-skill

Billpal: https://billpal.io
