---
name: invoice-generator
description: >
  Write an invoice as a PDF or an editable file, optionally as a ZUGFeRD /
  Factur-X e-invoice. Use when the user wants to create an invoice, write an
  invoice, make an e-invoice, or says "Rechnung schreiben", "Rechnung
  erstellen", or "E-Rechnung". Fills a Billpal template, the user's own file,
  or a plain default.
---

# Invoice generator

Write one invoice the user can send. Answer in the user's language.

This skill is a folder: this file, `sample-denylist.json`, `e-invoice.md`, `generate.py`, and `vendor/`. If you only have a link to this file, try once to get the folder: download https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip, or `git clone https://github.com/leebale/billpal-skills`. If that fails, ask the user to attach that ZIP in your first message, not halfway through the invoice.

## Company card

Remember one company card only if the user agrees. The card lives in this assistant, not in Billpal.

Offer it once, after the user approves their first invoice. Ask before saving. Bank details are a separate yes. Do not store an IBAN on the first yes to the company name.

Save: legal name, address, VAT id or German tax number (Steuernummer), invoice language, number format, date format, currency, payment terms, the template, the last invoice number, and (only after a separate yes) IBAN and BIC.

If assistant memory is off, give the card back as a short text block the user can paste next time. Do not pretend it was saved.

## Ask first

Never pick the tax regime. Ask.

Ask in one message for everything the invoice still needs from the field list below. That usually means the invoice number, the issue date, the service date or period, the tax regime, the payment terms, and bank details if they go on the invoice. Ask for each one on its own. Do not bundle two fields into one question.

Language, number format, date format, and currency: propose what the request already shows (German text and amounts in euros suggest German and EUR, for example) and let the user confirm in the same message. Values on the company card count as proposals too.

If an answer leaves a required field open, ask again for that field. Never fill it with a guess, such as the current month for the service period.

Show the invoice and wait. Do not send it, and do not invent a VAT id, an IBAN, a tax rate, or an invoice number. When you change the invoice, save it under a new name (`-v2`, `-v3`), so an old link does not open the old file.

## Fields a usable invoice has

In the EU, these come from the VAT Directive (Art. 226) and the national VAT law, for example § 11 UStG in Austria and § 14 UStG in Germany. Collect these before writing the file.

- A sequential invoice number, the issue date, and the service date or service period.
- Name and address of the seller and of the buyer.
- The seller's VAT id, or the national tax number (Steuernummer in Germany). A small business under the exemption (Kleinunternehmer) may have no VAT id. Say which case it is. Do not leave the field blank without that reason.
- The buyer's VAT id when the regime requires it: always for reverse charge or an intra-EU supply, and in Austria on a B2B invoice over €10,000 gross.
- Quantity and a description of each item or service.
- Net amount per tax rate, the tax rate, and the tax amount. Or the exemption wording (small-business exemption, reverse charge, intra-community supply). Then the gross total.

If the user says the client has already paid, it is still an invoice. Keep the title. Ask for the payment date and method if they were not given, show them in the payment block with the amount paid, and show the amount due as the total minus what was paid (0 when paid in full). When it is paid in full, drop any line asking for payment or a due date.

Outside the EU, ask the user what their country requires and write that. Do not claim the invoice meets a law you were not told about.

## Where the layout comes from

Use the first one that fits.

1. A Billpal template, when the user wants one. Twelve designs. Files do not need this repo:

   Gallery: https://billpal.io/invoice-templates

   | Design | Word | Excel | Word copy | Excel copy |
   |---|---|---|---|---|
   | compact-gray | https://billpal.io/templates/compact-gray/invoice.docx | https://billpal.io/templates/compact-gray/invoice.xlsx | https://docs.google.com/document/d/1lqePN7iDRrxagSODj5Av82HbQEOEBc9nf5Wx27N4k6s/copy | https://docs.google.com/spreadsheets/d/1w8yQarfKvrkCMovzpmqb8wI1QMBVXEIcba57ittABqQ/copy |
   | modern-black | https://billpal.io/templates/modern-black/invoice.docx | https://billpal.io/templates/modern-black/invoice.xlsx | https://docs.google.com/document/d/1WG8N9j9bX5pDeKO3XUEZedU853isc6MG5E62CLCn4WQ/copy | https://docs.google.com/spreadsheets/d/1VyvzDHtRKaYyq1-_SJaAdyiqJqsuTROVrdCJBpwopew/copy |
   | professional-navy | https://billpal.io/templates/professional-navy/invoice.docx | https://billpal.io/templates/professional-navy/invoice.xlsx | https://docs.google.com/document/d/1T7bDHgKRbHk5wUolFVE2UbEtqSxiPrPxjHdItK9ktXs/copy | https://docs.google.com/spreadsheets/d/1r7PrB2ytaq7w--LyU-a1t6p84VucpjKI9bMlovK2uO8/copy |
   | vibrant-base | https://billpal.io/templates/vibrant-base/invoice.docx | https://billpal.io/templates/vibrant-base/invoice.xlsx | https://docs.google.com/document/d/1n2NyF_bNV36q2GK8I3T1XLGZZ9BZkckK3xLETEYcvZ0/copy | https://docs.google.com/spreadsheets/d/11T7X8RHOLvG0OPSQHRPQKj3PLcGcQ933y5kUzHBmGeY/copy |
   | bauhaus-special | https://billpal.io/templates/bauhaus-special/invoice.docx | https://billpal.io/templates/bauhaus-special/invoice.xlsx | https://docs.google.com/document/d/18JUXq20-JVUAe5EhcQSglQSAyaeKbX-epktv_YbzAsg/copy | https://docs.google.com/spreadsheets/d/1WNEloNB4K2ky_A8xjseuvRifLIB4A7pm-64FrPSmcag/copy |
   | minimal-white | https://billpal.io/templates/minimal-white/invoice.docx | https://billpal.io/templates/minimal-white/invoice.xlsx | https://docs.google.com/document/d/104E-OnlboMVOX0kCcRJreSDfFoR1Wp8qGtj64Wan-ms/copy | https://docs.google.com/spreadsheets/d/1bR4N-nENOdKQly5-DClLcUjfIiovYtvul34WeO4KKCs/copy |
   | mono-style | https://billpal.io/templates/mono-style/invoice.docx | https://billpal.io/templates/mono-style/invoice.xlsx | https://docs.google.com/document/d/1i-YTGG15pFnQ2fBY63qS3M6xS2_J19aN6HtbNZ4c304/copy | https://docs.google.com/spreadsheets/d/1C_ZvAULvLNkOnRt37ObyRt4Dw5plMPqHHnA6WaN48Zc/copy |
   | classic-blue | https://billpal.io/templates/classic-blue/invoice.docx | https://billpal.io/templates/classic-blue/invoice.xlsx | https://docs.google.com/document/d/1eVgKl-DNeyoeI7X9tKDuqnZLznE0sGqk7xTZTN1irjM/copy | https://docs.google.com/spreadsheets/d/1Q_qZ17FYnbEt9pgS-Zh2sUbxXq0-y7tnb2sXcMqenyc/copy |
   | bold-green | https://billpal.io/templates/bold-green/invoice.docx | https://billpal.io/templates/bold-green/invoice.xlsx | https://docs.google.com/document/d/1_H4A5kQdGMqhfVfyhdueqcheYViukV3i-SH_GdAREdg/copy | https://docs.google.com/spreadsheets/d/11nFCbx4NEwifoDa7GwHpn9bvZho8Ql11tJYT7SPzjdo/copy |
   | two-tables | https://billpal.io/templates/two-tables/invoice.docx | https://billpal.io/templates/two-tables/invoice.xlsx | https://docs.google.com/document/d/1Rabq7azUgG9UE5m2lVlsWHI6rheH6yiemde2qZA2_TU/copy | https://docs.google.com/spreadsheets/d/1xyF2G3sL71FQGq3ctrtecZLTmwR5bc4MfRHDAKqGYNg/copy |
   | signature-studio | https://billpal.io/templates/signature-studio/invoice.docx | https://billpal.io/templates/signature-studio/invoice.xlsx | https://docs.google.com/document/d/12qa1usT7hL2hZw_29n_bpIkoAGbuPuxvLZvIC5Gd90Q/copy | https://docs.google.com/spreadsheets/d/1yWDUVcfSH7oznF2Nj16eEqj8rlZqwbpO9HckjL9PGYE/copy |
   | modern-boutique | https://billpal.io/templates/modern-boutique/invoice.docx | https://billpal.io/templates/modern-boutique/invoice.xlsx | https://docs.google.com/document/d/1O812_cLMXyUZ59KgBSIDV9BrM_DHJccz4FmMj83-N6E/copy | https://docs.google.com/spreadsheets/d/1D6afa_g3PL3tUBcCM6U-KATiN94bzigNlsNz4XeZo4U/copy |

2. The user's own file. Match its sections and columns. Do not restyle it into a Billpal template.

3. A plain default, when there is no template and no file of their own. Build the PDF with the host's own PDF tooling. No template download and no LibreOffice. The same field list applies.

## Host limits

Download a Billpal template before you build anything with it. If the download fails, stop and tell the user. Never rebuild a Billpal design by hand. Offer these instead:

- The user downloads the file from the link above and attaches it.
- The user allows billpal.io for the sandbox. On Claude: Settings, Capabilities, Allow network egress, Domain allowlist, then add `billpal.io` under Additional allowed domains. Retry after their next message. On other hosts, say that the sandbox blocked billpal.io.
- The plain default.

Turning a filled `.docx` into a PDF needs LibreOffice. If it is missing, deliver the `.docx` or `.xlsx` and say that the PDF was not made. The plain default does not depend on that conversion.

## Filling a template

The templates are filled with sample data, not placeholders. Word splits a phrase across runs, so a sample string may not appear in one piece.

Before writing:

- Map the page. Name each region (seller, buyer, meta, lines, totals, payment, footer) and what you will put there.
- One cell per field in a spreadsheet. Amounts, quantities, and totals are formulas, not typed numbers.
- If a logo sits on a dark rectangle that is only there for the sample, remove that rectangle when the user's logo does not need it.
- Keep the template's font sizes. If a value does not fit, wrap it or add a line. Never shrink one field to make it fit.
- Render the page and look at it: nothing overlaps, and nothing runs past the margin.

## Sample-data check

This check blocks delivery. Read `sample-denylist.json` in this skill folder.

1. Take the filled `.docx` body, and every text cell in the filled `.xlsx`.
2. Remove all whitespace from that text, and from each denylist entry.
3. If any entry still appears, the file still contains template sample data. Do not deliver it. Replace the sample and check again.
4. Compare against the untouched template, not against memory.

A sample VAT id, IBAN, tax id, email, or invoice number from the list never stays, even if it looks plausible.

A value the user gave you for this invoice may match an entry, for example their legal name or their IBAN. Keep it, say so in one line, and still remove every other sample string.

## With a Billpal account

Confirm every value before it lands on the invoice.

- Buyer: `get_contact` has the address. Confirm before use.
- Seller: `company_name`, `tax_id`, and `website` only, from a party with `is_your_company` on `get_document`. Address and logo are not on the connection. `list_workspaces` returns id, name, and role only. Ask the user for the address and the logo.
- Next number: search outgoing invoices and show the last few. Results are not sorted by number. Ask which number to use. After each invoice, update the last number on the company card.

## E-invoice (optional)

The invoice is complete without this step. Offer ZUGFeRD once, in one sentence, when the buyer is a business in Germany. Offer a standalone XRechnung when the buyer is a German public body, or when they ask for XRechnung. Do it when the user asks for ZUGFeRD, Factur-X, XRechnung, or an e-invoice. If they say no, do not ask again. The check uploads the file to billpal.io, so ask for that in the same message.

For the e-invoice, read `e-invoice.md` in this skill folder and follow it. It needs the finished invoice PDF, code execution, and a seller in an EU country.

The e-invoice is a PDF with valid EN 16931 XML embedded. It is not a certified ZUGFeRD / Factur-X PDF/A-3 file, and the PDF wrapper is not checked. Do not claim more than that. `e-invoice.md` has the exact wording.

## Before you deliver

The invoice is not done until all of these are true:

- Regime, service date or period, language, number format, date format, and currency were asked or confirmed, not guessed.
- Every field in the list above is present, or the exemption wording replaces the tax lines.
- No denylist entry remains, aside from values the user gave for this invoice.
- The rendered page has no text smaller than the template's and nothing past the margin.
- The user has seen the invoice and has not yet been told it was sent.
