---
name: sepa-file-generator
description: >
  Build one ISO pain.001.001.09 SEPA credit-transfer file (SEPA-Datei,
  Sammelüberweisung) for upload to business online banking. The generator
  runs with Node and needs this skill's files in the code sandbox. With
  only a link, ask the user to upload the skill ZIP. The model does not
  write the XML itself.
---

# SEPA file generator

Build one ISO `pain.001.001.09` file. Answer in the user's language.

Do not ask the user to pick a dialect. Business banks in Austria and Germany that import a SEPA XML file take this standard. If the user says their bank has no XML import (N26 and Wise do not), say that and stop. Do not build a per-bank CSV.

## Get the files first

The generator is `generate.mjs` with its `vendor/` and `schema/` folders. It runs with Node and needs no internet. Before anything else, check that these files are in your code sandbox.

If you only have a link to this skill, the sandbox usually cannot download them, and the schema checker is a binary file you cannot copy as text. Do not try. Tell the user, in one step:

> Download https://github.com/leebale/billpal-skills/releases/latest/download/sepa-file-generator.zip and drop the ZIP into this chat.

Then unzip it and continue from the folder that holds `generate.mjs`.

If `node` cannot run even with the files, stop. Say that no file was made, and why. Do not write the XML by hand. Do not talk about a file that was not schema-checked. There is no file.

A rejected bank import never executes a payment. After you hand the file over, say that if the bank rejects it, the user pastes the bank's error message here. You fix the input and run `generate.mjs` again.

## Company card

Remember one company card only if the user agrees. The card lives in this assistant, not in Billpal. Bank details are a separate yes from the company name.

The debtor name and the debtor IBAN are required for the file. Offer to remember both on the card. If assistant memory is off, give the card back as a short text block the user can paste next time.

## What goes in the file

Ask for the debtor name, the debtor IBAN, and the execution date. If they skip the date, the script uses the next weekday. It rejects a date in the past. It does not know public holidays.

Each payment has a creditor name, a creditor IBAN, an amount in integer cents, and either a structured reference or free text, never both.

- Structured reference (`creditorReference`): 35 characters or fewer. Austrian Zahlungsreferenz and a free-text Verwendungszweck are exclusive. If this value is present, do not also send free text.
- Otherwise free text (`remittance`): 140 characters or fewer, usually the invoice number.

The script rejects a value that is too long. It does not cut it. Shorten the text and run again.

The script, not you, sets batch booking, a unique message id, an end-to-end id of 35 characters or fewer, the control sum, the transaction count, and the IBAN checksum. Amounts stay integer cents until the script turns them into the file. You never write the XML.

`documentId` is optional. The script ignores it. Keep it on each payment so that, after the transfers, you know which Billpal documents to update.

## Two runs

Write `input.json` with:

- `debtorName`, `debtorIban`
- `executionDate` (optional, `YYYY-MM-DD`)
- `language`: `de` or `en`, so `&` becomes "und" or "and"
- `payments[]`: `creditorName`, `creditorIban`, `amountCents` (integer), optional `remittance`, optional `creditorReference`, optional `town` and `country` (both, or neither), optional `documentId`

Run `node generate.mjs --preview input.json` first. It prints the transliterated names, remittance, and addresses, the totals, and which reference each payment uses. It writes nothing. Show that preview and wait for a yes.

Characters outside the SEPA basic Latin set are transliterated. ä ö ü Ä Ö Ü become ae oe ue Ae Oe Ue. ß becomes ss. Other letters lose their accent. IBANs and amounts stay as they are. The script cannot ask the user anything, which is why the preview exists.

Do not send an unstructured postal address. If town and country are known, send only those two fields. Otherwise omit the address.

Then run `node generate.mjs input.json`. It writes `pain.001.001.09.xml` next to the input and validates it against the ISO schema. If this command fails, no file was made.

The vendored generator cannot write a schema-valid structured reference. When `creditorReference` is set, that value is sent alone as the free text. The preview says so.

## Without a Billpal account

The user supplies each payment. You still show the preview and wait.

## With a Billpal account

Ask "due by". Call `search_documents` for incoming invoices. `payment_status` takes one value, so call it three times (`unpaid`, `partially_paid`, `unknown`) or omit it and filter here. `unknown` is not unpaid. Page with `offset` (`limit` max 100) and stop when `truncated` is false.

`due_to` drops documents with no due date. Also list unpaid documents with no due date whose issue date (`date_from` / `date_to`) falls in the range they named.

Say once that Billpal read the payment status when the file arrived. The user confirms the list before anything goes into the file.

- A reminder with extra fees: pay the reminder amount, not the invoice plus the reminder. A reminder with no linked invoice is the only record, so it can be paid. Do not drop every reminder.
- A linked receipt settles the invoice: skip it. A linked credit note: subtract it. A credit note's payable amount comes back positive.
- Partially paid: pay `payable - amount_paid`, and only after they confirm the remainder. If that amount clears the remainder, the later write-back is `paid`, not `partially_paid`.
- `direct_debit` means the invoice is already collected. Skip it, and list it so the user can add it back. A line that only says PayPal or a card is accepted is not collection. Skip `credit_card`, `paypal`, and the other non-transfer methods (`visa`, `mastercard`, `amex`, `diners_club`, `debit_card`, `cash`, `apple_pay`, `google_pay`, `mobile_payment`, `stripe`, `gift_card_voucher`) only when that is the document's `payment_method`. List those too, so the user can add one back. `bank_transfer` stays in the file.
- Skip and list non-EUR documents, non-SEPA IBANs, and `us_ach` accounts.

### IBAN trust

Trust an IBAN only when its source on `get_contact` is `manual` or `import`.

A `document` IBAN is trusted only when the same IBAN was printed on an earlier invoice of that supplier. Search with `search_documents` and `supplier_id`, then read `get_document` `bank_accounts.supplier`. Comparing it to the contact's stored IBAN is not a check: that copy was taken from the invoice itself.

No earlier invoice (a new supplier, or the first invoice in Billpal): the IBAN is unverified. Stop, show the IBAN, and ask the user to confirm it with a source they trust.

If they verified a new IBAN, offer `get_import_template` and the contact CSV. The connection cannot write bank accounts.

## After the transfers

Do this only after the user says the transfers were made. Never when the file is created, and never on your own. Offer the update and wait for a yes.

- `set_payment`: `payment_status` is `paid` when the confirmed amount clears what remained, otherwise `partially_paid`. `payment_method` is `bank_transfer`. `payment_date` is required for both. At most 50 documents per call.
- `set_amount_paid`: one document. `expected_updated_at` comes from `get_document`. The amount is the new cumulative amount, not the delta.

On `write_consent_required`, say the connection is read-only and point them at Settings, AI agents, to connect again. Do not claim the update happened.
