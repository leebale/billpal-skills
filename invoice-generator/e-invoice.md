# E-invoice step (optional)

Read this only when the user wants the invoice as an e-invoice. It turns the invoice PDF into one ZUGFeRD / Factur-X PDF: the same pages, with the EN 16931 XML attached. Answer in the user's language.

This is an EN 16931 invoice, guideline `urn:cen.eu:en16931:2017`, type code 380. The seller is in an EU country. No XRechnung, no credit note, no allowances.

If the seller is not in the EU, say that no e-invoice can be made yet and deliver the normal invoice. It is complete without the e-invoice.

## Get the files first

The generator is `generate.py` with its `vendor/` folder, in this skill's folder. It runs with Python 3.11 or newer and needs no internet and no pip. Before anything else, check that these files are in your code sandbox.

If you only have a link to this skill, the sandbox usually cannot download them. Do not try. Tell the user, in one step:

> Download https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip and drop the ZIP into this chat.

Then unzip it and continue from the folder that holds `generate.py`.

If `python3` cannot run even with the files, stop. Say that no e-invoice was made, and why, and hand over the normal invoice. Do not write the XML by hand. Do not talk about an e-invoice that was not made.

## What you need

1. The invoice PDF: the one you just made, or the user's own. The script attaches the XML to it and does not change the pages. With no PDF (for example only a `.docx` and no way to convert it), no e-invoice can be made.
2. The same details as on that PDF: the seller, the buyer, the invoice number, the date, and the lines. Amounts are integer cents. Pass `tax_cents`, `grand_total_cents`, and `due_cents` as printed on the PDF. The script checks them against the lines and the rate and says the right value if one is off. If the PDF is wrong, fix the PDF too.

`seller_country` and `buyer_country` are two-letter codes. The seller country must be an EU country. The seller needs `seller_vat_id`, or `seller_tax_number` (the national tax number, Steuernummer in Germany) if they have no VAT id. Optional: `due_date` (YYYY-MM-DD), `payment_terms`, `delivery_date`.

One tax category per invoice:

- `S`: standard rate at the rate the user states for their country.
- `E`: small-business exemption (Kleinunternehmer). Rate 0. `exemption_reason` is their exemption wording.
- `AE`: reverse charge. Rate 0. `exemption_reason` and `buyer_vat_id` are required.
- `K`: intra-EU supply. Rate 0. `exemption_reason`, `buyer_vat_id`, and `seller_vat_id` are required. Goods go to `buyer_country` unless you set `deliver_to_country`.

Never pick the rate or the category yourself. Use what the user confirmed for the invoice.

Write a JSON file and run:

```bash
python3 generate.py invoice.json invoice.pdf e-invoice.pdf
```

If the script exits with an error, fix the JSON and run it again. Do not patch the PDF or the XML.

## Check the file

POST `e-invoice.pdf` to `https://billpal.io/api/einvoice/check` with `Content-Type: application/pdf`. No account is required. Files up to 4 MB.

- `accept: true` means the EN 16931 XML passed. Tell the user that. Also say PDF/A-3 was not checked.
- `accept: false` means it failed. Show the rule ids and the English texts from the response. Fix the JSON and run `generate.py` again. Do not hand over a rejected file as a finished e-invoice.
- `accept: null` means no matching profile. Do not call it valid.

If the POST fails (no network, a timeout, or any other error), do not call the file valid. Hand over the PDF and say the check did not run. Give this link: https://billpal.io/einvoice-check

On Claude, if the sandbox cannot reach billpal.io, one sentence is enough: add billpal.io to the code-execution allowlist, or use the link above.

## A connected Billpal account

After the user confirms, you may fill buyer details and amounts from `get_document`. The seller address is not on the account. Ask for it. This step writes nothing back to Billpal.
