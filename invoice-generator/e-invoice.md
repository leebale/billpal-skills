# E-invoice step (optional)

Read this only when the user wants the invoice as an e-invoice. It turns the invoice PDF into one PDF with the EN 16931 invoice XML embedded as `factur-x.xml`: the same pages, with the XML attached. Answer in the user's language.

## What this does and does not do

- Does: embeds valid EN 16931 XML in the PDF, and the check below validates that XML.
- Does not: convert the PDF to PDF/A-3, embed fonts, add a colour profile, or validate the PDF wrapper.
- The result is not a certified ZUGFeRD / Factur-X PDF/A-3 file. Never call it "PDF/A-3", "ZUGFeRD-compliant", "archival", or "legally compliant". The only claim you may make is that the invoice data passed the EN 16931 check.
- If the user asks whether it is PDF/A-3, answer no. Do not try to convert it (no Ghostscript, no LibreOffice PDF/A export). Attaching the XML replaces the PDF's XMP metadata anyway, so a conversion before it would be undone.
- If the user's own PDF is already PDF/A, tell them that attaching the XML removes that claim.
- If the buyer is a public-sector body, a French portal, or an archiving system, tell the user that strict validators may reject the PDF wrapper. They can test it with veraPDF or Mustang, or use https://billpal.io/einvoice-check.

This is an EN 16931 invoice, guideline `urn:cen.eu:en16931:2017`, type code 380. The seller is in an EU country. No XRechnung, no credit note, no allowances.

If the seller is not in the EU, say that no e-invoice can be made yet and deliver the normal invoice. It is complete without the e-invoice.

## Get the files first

The generator is `generate.py` with its `vendor/` folder, in this skill's folder. It runs with Python 3.11 or newer and needs no internet and no pip. Before anything else, check that these files are in your code sandbox.

If you only have a link to this skill, try once to download https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip (or `git clone https://github.com/leebale/billpal-skills`). If that fails, tell the user, in one step:

> Download https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip and drop the ZIP into this chat.

Then unzip it and continue from the folder that holds `generate.py`.

If `python3` cannot run even with the files, stop. Say that no e-invoice was made, and why, and hand over the normal invoice. Do not write the XML by hand. Do not talk about an e-invoice that was not made.

## What you need

1. The invoice PDF: the one you just made, or the user's own. The script attaches the XML to it and does not change the pages. With no PDF (for example only a `.docx` and no way to convert it), no e-invoice can be made.
2. The same details as on that PDF: the seller, the buyer, the invoice number, the date, and the lines. Amounts are integer cents. Pass `tax_cents`, `grand_total_cents`, and `due_cents` as printed on the PDF. The script checks them against the lines and the rate and says the right value if one is off. If the PDF is wrong, fix the PDF too.

`seller_country` and `buyer_country` are two-letter codes. The seller country must be an EU country. The seller needs `seller_vat_id`, or `seller_tax_number` (the national tax number, Steuernummer in Germany) if they have no VAT id. Optional: `due_date`, `payment_terms`, and either `delivery_date` or a service period as `period_start` and `period_end`. Dates are YYYY-MM-DD. Use the same service date or period as the PDF.

If the client has already paid, set `paid_cents` to the amount received and `due_cents` to the grand total minus that amount (0 when paid in full). The type code stays 380. The XML has no field for the payment date or method, so those stay on the PDF only.

One tax category per invoice:

- `S`: standard rate at the rate the user states for their country.
- `E`: small-business exemption (Kleinunternehmer). Rate 0. `exemption_reason` is their exemption wording.
- `AE`: reverse charge. Rate 0. `exemption_reason` and `buyer_vat_id` are required.
- `K`: intra-EU supply. Rate 0. `exemption_reason`, `buyer_vat_id`, and `seller_vat_id` are required. Goods go to `buyer_country` unless you set `deliver_to_country`.

Never pick the rate or the category yourself. Use what the user confirmed for the invoice.

Write a JSON file. Amounts and the rate are numbers, not text. This one is 7 hours at €150 with 20 % VAT:

```json
{
  "invoice_number": "2026-7",
  "issue_date": "2026-10-18",
  "due_date": "2026-11-01",
  "period_start": "2026-08-01",
  "period_end": "2026-08-08",
  "payment_terms": "14 Tage netto",
  "currency": "EUR",
  "seller_name": "Seller GmbH",
  "seller_street": "Hauptstraße 1",
  "seller_postcode": "1010",
  "seller_city": "Wien",
  "seller_country": "AT",
  "seller_vat_id": "ATU12345678",
  "buyer_name": "Buyer GmbH",
  "buyer_street": "Markt 2",
  "buyer_postcode": "1140",
  "buyer_city": "Wien",
  "buyer_country": "AT",
  "buyer_vat_id": "ATU87654321",
  "tax_category": "S",
  "tax_rate_percent": 20,
  "lines": [
    {"description": "Consulting", "quantity": "7", "unit": "HUR", "unit_price_cents": 15000, "line_net_cents": 105000}
  ],
  "tax_cents": 21000,
  "grand_total_cents": 126000,
  "due_cents": 126000
}
```

`quantity` is text so that "1.5" stays exact. `unit` is a UN/ECE code: `HUR` for hours, `DAY` for days, `C62` (the default) for pieces.

Then run:

```bash
python3 generate.py invoice.json invoice.pdf e-invoice.pdf
```

If the script exits with an error, fix the JSON and run it again. Do not patch the PDF or the XML.

## Check the file

The check uploads the whole e-invoice to billpal.io, including the buyer, the tax ids, and the bank details. Ask before the upload, in the same message where you offer the e-invoice. If the user says no, hand over the file, say it was not checked, and give the link below.

POST `e-invoice.pdf` to `https://billpal.io/api/einvoice/check` with `Content-Type: application/pdf`. No account is required. Files up to 4 MB.

- `accept: true` means the EN 16931 XML passed. Say this sentence, translated to the user's language, and do not drop the second half: "The invoice data passed the EN 16931 check. The PDF wrapper (PDF/A-3) was not checked."
- `accept: false` means it failed. Show the rule ids and the English texts from the response. Fix the JSON and run `generate.py` again. Do not hand over a rejected file as a finished e-invoice.
- `accept: null` means no matching profile. Do not call it valid.

If the POST fails (no network, a timeout, or any other error), do not call the file valid. Hand over the PDF and say: "The check did not run, so I can't confirm it is valid." Give this link: https://billpal.io/einvoice-check

If the sandbox cannot reach billpal.io, tell the user how to allow it, as described under "Host limits" in `SKILL.md`, or give the link above.

## A connected Billpal account

After the user confirms, you may fill buyer details and amounts from `get_document`. The seller address is not on the account. Ask for it. This step writes nothing back to Billpal.
