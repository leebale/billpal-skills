#!/usr/bin/env node
// Publish gate for the SEPA file generator. No bank upload.

import { spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { nextWeekday } from "./generate.mjs";

const root = dirname(fileURLToPath(import.meta.url));
const generate = join(root, "generate.mjs");
const sentinel = "DOCID-SENTINEL-9f3a";
const futureDate = nextWeekday(new Date(new Date().getFullYear() + 1, 0, 5, 12));

function assert(condition, message) {
  if (!condition) {
    process.stderr.write(`FAIL ${message}\n`);
    process.exit(1);
  }
}

function freshDir() {
  return mkdtempSync(join(tmpdir(), "sepa-check-"));
}

function run(dir, input, preview) {
  const inputPath = join(dir, "input.json");
  writeFileSync(inputPath, JSON.stringify(input));
  const args = preview ? [generate, "--preview", inputPath] : [generate, inputPath];
  const result = spawnSync(process.execPath, args, { encoding: "utf8" });
  return { ...result, xmlPath: join(dir, "pain.001.001.09.xml") };
}

const base = {
  language: "de",
  debtorName: "Müller & Söhne GmbH",
  debtorIban: "DE89370400440532013000",
  executionDate: futureDate,
  payments: [
    {
      creditorName: "Café René GmbH",
      creditorIban: "AT611904300234573201",
      amountCents: 1050,
      remittance: "Rechnung 1042",
      town: "München",
      country: "DE",
      documentId: sentinel,
    },
    {
      creditorName: "Nordlicht AG",
      creditorIban: "BE71096123456769",
      amountCents: 250,
      creditorReference: "RF18539007547034",
      documentId: sentinel,
    },
  ],
};

assert(nextWeekday(new Date(2026, 9, 15, 12)) === "2026-10-16", "Thursday goes to Friday");
assert(nextWeekday(new Date(2026, 9, 16, 12)) === "2026-10-19", "Friday goes to Monday");
assert(nextWeekday(new Date(2026, 9, 17, 12)) === "2026-10-19", "Saturday goes to Monday");

const previewDir = freshDir();
const preview = run(previewDir, base, true);
assert(preview.status === 0, `preview failed\n${preview.stderr}`);
assert(!existsSync(preview.xmlPath), "preview wrote a file");
const shown = JSON.parse(preview.stdout);
assert(shown.debtorName === "Mueller und Soehne GmbH", `debtor name ${shown.debtorName}`);
assert(shown.executionDate === futureDate, "preview kept the execution date");
assert(shown.controlSumCents === 1300, "preview total");
assert(shown.payments[0].creditorName === "Cafe Rene GmbH", "creditor transliteration");
assert(shown.payments[0].town === "Muenchen" && shown.payments[0].country === "DE", "structured town");
assert(shown.payments[0].referenceKind === "free text", "remittance kind");
assert(shown.payments[1].referenceKind === "free text", "reference kind");
assert(shown.payments[1].remittance === "RF18539007547034", "reference text");
assert(
  shown.payments[1].referenceNote?.includes("free text"),
  "preview says the reference is free text",
);
assert(!JSON.stringify(shown).includes(sentinel), "preview leaked documentId");
rmSync(previewDir, { recursive: true, force: true });

const openDate = { ...base, executionDate: undefined };
const openDir = freshDir();
const open = run(openDir, openDate, true);
assert(open.status === 0, `default date failed\n${open.stderr}`);
const openShown = JSON.parse(open.stdout);
const openDay = new Date(`${openShown.executionDate}T12:00:00`).getDay();
assert(openDay !== 0 && openDay !== 6, "default date is a weekday");
assert(openShown.executionDate > new Date().toISOString().slice(0, 10), "default date is after today");
rmSync(openDir, { recursive: true, force: true });

function expectReject(input, label) {
  const dir = freshDir();
  const result = run(dir, input, false);
  assert(result.status !== 0, `${label} was accepted`);
  assert(!existsSync(result.xmlPath), `${label} wrote a file`);
  rmSync(dir, { recursive: true, force: true });
}

expectReject(
  { ...base, payments: [{ ...base.payments[0], remittance: "a".repeat(141) }] },
  "remittance over 140",
);
expectReject(
  {
    ...base,
    payments: [{ ...base.payments[1], creditorReference: "R".repeat(36), remittance: undefined }],
  },
  "reference over 35",
);
expectReject(
  {
    ...base,
    payments: [{ ...base.payments[0], creditorReference: "RF18539007547034" }],
  },
  "both reference kinds",
);
expectReject({ ...base, executionDate: "2020-01-06" }, "past date");
expectReject({ ...base, debtorIban: "DE00370400440532013000" }, "bad IBAN");

const okDir = freshDir();
const first = run(okDir, base, false);
assert(first.status === 0, `write failed\n${first.stderr}\n${first.stdout}`);
assert(existsSync(first.xmlPath), "write did not create the file");
const xml = readFileSync(first.xmlPath, "utf8");

assert(xml.includes("urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"), "namespace");
assert(!/[^\t\n\r\x20-\x7e]/.test(xml), "character outside basic Latin");
assert(!xml.includes(sentinel), "documentId was written into the file");
assert(!xml.includes("<AdrLine"), "unstructured address");
assert(!xml.includes("<Strd"), "structured remittance was written");
assert((xml.match(/<Ustrd>/g) ?? []).length === 2, "one free-text reference per payment");
assert(xml.includes("<TwnNm>Muenchen</TwnNm>"), "town");
assert(xml.includes("<Ctry>DE</Ctry>"), "country");
assert((xml.match(/<TwnNm>/g) ?? []).length === 1, "address omitted when town is absent");
assert(xml.includes("<BtchBookg>true</BtchBookg>"), "batch booking");
assert((xml.match(/Ccy="EUR"/g) ?? []).length === 2, "EUR only");

const instructed = [...xml.matchAll(/<InstdAmt\b[^>]*>([^<]*)<\/InstdAmt>/g)].map((match) => match[1]);
assert(instructed.includes("10.50") && instructed.includes("2.50"), `amounts ${instructed.join(",")}`);
for (const tag of ["CtrlSum", "NbOfTxs"]) {
  const values = [...xml.matchAll(new RegExp(`<${tag}>([^<]*)</${tag}>`, "g"))].map((match) => match[1]);
  const expected = tag === "CtrlSum" ? "13.00" : "2";
  assert(values.length > 0 && values.every((value) => value === expected), `${tag} ${values.join(",")}`);
}

const ends = [...xml.matchAll(/<EndToEndId>([^<]*)<\/EndToEndId>/g)].map((match) => match[1]);
assert(ends.length === 2 && new Set(ends).size === 2, "end-to-end ids are not unique");
assert(ends.every((id) => id.length <= 35 && !id.startsWith("/")), "end-to-end id shape");
const msgIds = [...xml.matchAll(/<MsgId>([^<]*)<\/MsgId>/g)].map((match) => match[1]);
assert(msgIds.length === 1 && msgIds[0].length <= 35, "message id");
const instrIds = [...xml.matchAll(/<InstrId>([^<]*)<\/InstrId>/g)].map((match) => match[1]);
assert(
  instrIds.length === 2 && instrIds.every((id) => id.startsWith(msgIds[0]) && id.length <= 35),
  `instruction ids ${instrIds.join(",")}`,
);

const second = run(okDir, base, false);
assert(second.status === 0, `second write failed\n${second.stderr}`);
const xml2 = readFileSync(second.xmlPath, "utf8");
const msg2 = [...xml2.matchAll(/<MsgId>([^<]*)<\/MsgId>/g)].map((match) => match[1]);
assert(msgIds[0] !== msg2[0], "message ids were reused");
const ends2 = [...xml2.matchAll(/<EndToEndId>([^<]*)<\/EndToEndId>/g)].map((match) => match[1]);
assert(ends.every((id) => !ends2.includes(id)), "end-to-end ids were reused");
rmSync(okDir, { recursive: true, force: true });

const exact = {
  ...base,
  payments: [{
    ...base.payments[0],
    remittance: "a".repeat(140),
    town: undefined,
    country: undefined,
  }],
};
const exactDir = freshDir();
const exactRun = run(exactDir, exact, false);
assert(exactRun.status === 0, `140-character remittance was rejected\n${exactRun.stderr}`);
const exactXml = readFileSync(exactRun.xmlPath, "utf8");
assert(!exactXml.includes("<AdrLine") && !exactXml.includes("<TwnNm"), "no-address file still validates");
rmSync(exactDir, { recursive: true, force: true });

process.stdout.write("ok\n");
