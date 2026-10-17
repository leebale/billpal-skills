# Billpal skills

Free skills for Claude and ChatGPT. Each skill runs on its own. A Billpal account makes some of them easier, because the assistant can read invoices and contacts the user already has. Without an account, the user supplies the same details in the chat.

- [Invoice generator](invoice-generator/): writes an invoice in a Billpal template, your own file, or a plain default. Optionally turns it into a ZUGFeRD / Factur-X e-invoice for sellers in the EU, checked by Billpal; that step needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip)
- [SEPA file generator](sepa-file-generator/): builds one SEPA credit-transfer file (pain.001.001.09) for upload to business online banking. Needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/sepa-file-generator.zip)
- [Delivery-invoice match](delivery-invoice-match/): checks invoices against delivery notes, timesheets, and purchase orders, and lists what was billed but not delivered, price differences, duplicates, and open orders. Documents can come from the chat, a local folder, Billpal, or another connection such as an ERP. Needs code execution turned on for more than a few documents. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/delivery-invoice-match.zip)
- [AI bookkeeping](ai-bookkeeping/): keeps double-entry books in Beancount, one file per fiscal year, from bank statements (CAMT, CSV, MT940, PDF), bank connections, the cash book, and invoices and receipts. Books VAT, open items, and depreciation, and checks every period before calling it ready for your accountant. Charts of accounts for Austria, Germany, and the US, or your own. Needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/ai-bookkeeping.zip)

To use a skill:

- **Claude:** upload the ZIP under Settings, Capabilities, Skills. Turn on code execution.
- **ChatGPT:** drop the ZIP into the chat and ask it to follow the skill. A link alone is not enough for the SEPA skill or the e-invoice step, because ChatGPT usually can't download files from the internet while it runs code.

Billpal: https://billpal.io
