---
name: invoice-generator
description: >
  Write a normal invoice as a PDF or an editable file. Use when the user
  wants to create an invoice, write an invoice, or says "Rechnung schreiben"
  or "Rechnung erstellen". Fills a Billpal template, the user's own file, or
  a plain default.
---

# Invoice generator

Write one invoice the user can send. Answer in the user's language.

## Company card

Remember one company card only if the user agrees. The card lives in this assistant, not in Billpal.

Ask before saving. Bank details are a separate yes. Do not store an IBAN on the first yes to the company name.

Save: legal name, address, VAT id or German tax number (Steuernummer), invoice language, number format, date format, currency, the last invoice number, and (only after a separate yes) IBAN and BIC.

If assistant memory is off, give the card back as a short text block the user can paste next time. Do not pretend it was saved.

## Ask first

Never pick the tax regime. Ask.

Also ask, unless the card already has them and the user confirms they still apply:

- Language: German or English.
- Number format and date format.
- Currency.

Show the invoice and wait. Do not send it, and do not invent a VAT id, an IBAN, a tax rate, or an invoice number.

## Fields a usable AT or DE invoice has

Austria: § 11 UStG. Germany: § 14 UStG. Collect these before writing the file.

- A sequential invoice number, the issue date, and the service date or service period.
- Name and address of the seller and of the buyer.
- The seller's VAT id, or in Germany the Steuernummer. A Kleinunternehmer may have no VAT id. Say which case it is. Do not leave the field blank without that reason.
- The buyer's VAT id when the regime requires it: an Austrian B2B invoice over €10,000 gross, and always for reverse charge or an intra-EU supply.
- Quantity and a description of each item or service.
- Net amount per tax rate, the tax rate, and the tax amount. Or the exemption wording (Kleinunternehmer, reverse charge, intra-community supply). Then the gross total.

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

A sandbox with no internet cannot fetch a Billpal template. Ask the user to download it and attach it. The plain default still works.

Turning a filled `.docx` into a PDF needs LibreOffice. If it is missing, deliver the `.docx` or `.xlsx` and say that the PDF was not made. The plain default does not depend on that conversion.

## Filling a template

The templates are filled with sample data, not placeholders. Word splits a phrase across runs, so a sample string may not appear in one piece.

Before writing:

- Map the page. Name each region (seller, buyer, meta, lines, totals, payment, footer) and what you will put there.
- One cell per field in a spreadsheet. Amounts, quantities, and totals are formulas, not typed numbers.
- If a logo sits on a dark rectangle that is only there for the sample, remove that rectangle when the user's logo does not need it.

## Sample-data check

This check blocks delivery. Read `sample-denylist.json` in this skill folder.

1. Take the filled `.docx` body, and every text cell in the filled `.xlsx`.
2. Remove all whitespace from that text, and from each denylist entry.
3. If any entry still appears, the file still contains template sample data. Do not deliver it. Replace the sample and check again.
4. Compare against the untouched template, not against memory.

A sample VAT id, IBAN, tax id, email, or invoice number from the list never stays, even if it looks plausible.

If one entry is the user's own legal name, say so, and still remove every other sample string.

## With a Billpal account

Confirm every value before it lands on the invoice.

- Buyer: `get_contact` has the address. Confirm before use.
- Seller: `company_name`, `tax_id`, and `website` only, from a party with `is_your_company` on `get_document`. Address and logo are not on the connection. `list_workspaces` returns id, name, and role only. Ask the user for the address and the logo.
- Next number: search outgoing invoices and show the last few. Results are not sorted by number. Ask which number to use. After each invoice, update the last number on the company card.

## Before you deliver

The invoice is not done until all of these are true:

- Regime, language, number format, date format, and currency were asked, not guessed.
- Every field in the AT/DE list is present, or the exemption wording replaces the tax lines.
- No denylist entry remains, aside from a legal name the user confirmed is theirs.
- The user has seen the invoice and has not yet been told it was sent.
