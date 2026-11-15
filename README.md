# Billpal skills

[![skills.sh](https://skills.sh/b/leebale/billpal-skills)](https://skills.sh/leebale/billpal-skills)

Free skills for invoices, SEPA payments, and bookkeeping. They work in Claude, ChatGPT, Claude Code, Codex, Cursor, and other agents. Each skill runs on its own. A Billpal account makes some of them easier, because the agent can read the invoices and contacts you already have. Without an account, you give the same details in the chat.

- [Invoice generator](invoice-generator/): writes an invoice in a Billpal template, your own file, or a plain default. Optionally turns it into a ZUGFeRD / Factur-X e-invoice for sellers in the EU, checked by Billpal. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip)
- [SEPA file generator](sepa-file-generator/): builds one SEPA credit-transfer file (pain.001.001.09) for upload to business online banking. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/sepa-file-generator.zip)
- [Delivery-invoice match](delivery-invoice-match/): checks invoices against delivery notes, timesheets, and purchase orders, and lists what was billed but not delivered, price differences, duplicates, and open orders. Documents can come from the chat, a local folder, Billpal, or another connection such as an ERP. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/delivery-invoice-match.zip)
- [AI bookkeeping](ai-bookkeeping/): keeps double-entry books in Beancount from bank statements, the cash book, and invoices and receipts, with VAT, open items, and depreciation. Charts of accounts for Austria, Germany, and the US, and import files for DATEV, BMD, DVO, QuickBooks Online, and Xero. Your books are a plain-text file you keep. [Step-by-step guide](https://help.billpal.io/en/ai-bookkeeping-skill), [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/ai-bookkeeping.zip)

## Install

**Claude Code, Codex, Cursor, and other coding agents:**

```bash
npx skills add leebale/billpal-skills
```

The [skills CLI](https://skills.sh) asks which skills to install and copies each folder with its scripts. For one skill, add `--skill ai-bookkeeping`.

**Claude:** turn on code execution under Settings, Capabilities. Then open Customize, Skills, select +, Create skill, Upload a skill, and choose the ZIP.

**ChatGPT:** open Plugins in the sidebar, then the Skills tab. Select Create, then Upload from your computer, and choose the ZIP.

In Claude and ChatGPT, upload the ZIP. A link to this repository in a chat is not enough. The SEPA file generator and AI bookkeeping need code execution. So do the e-invoice step and delivery matches with more than a few documents.

## License

MIT for Billpal's own files. Vendored third-party files keep their own license: see [NOTICE](NOTICE).

Billpal collects invoices and receipts from email, WhatsApp, and supplier portals, and exports them for your accountant: https://billpal.io
