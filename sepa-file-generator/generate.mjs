#!/usr/bin/env node
// Builds one ISO pain.001.001.09 file from input.json.
// sepa.js 3.1.0 writes a structured creditor reference as <CdorPrtry>, which
// is not the ISO element <CdOrPrtry>, so a creditorReference is sent as the
// only free text. ponytail: execution dates skip weekends only, not public
// holidays. Upgrade path: a country holiday calendar.

import { createRequire } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const SEPA = require(join(here, "vendor/sepa.es5.cjs"));
const { validateXML } = require(join(here, "vendor/xmllint/index-node.js"));

const ALLOWED = new Set(
  "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /-?:().,'+".split(""),
);
const UMLAUTS = {
  ä: "ae",
  ö: "oe",
  ü: "ue",
  Ä: "Ae",
  Ö: "Oe",
  Ü: "Ue",
  ß: "ss",
};

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function transliterate(value, ampWord) {
  let out = "";
  for (const ch of String(value)) {
    if (UMLAUTS[ch]) {
      out += UMLAUTS[ch];
      continue;
    }
    if (ch === "&") {
      out += ` ${ampWord} `;
      continue;
    }
    const base = ch.normalize("NFD").replace(/\p{M}/gu, "");
    for (const piece of base) {
      if (ALLOWED.has(piece)) out += piece;
    }
  }
  return out.replace(/ {2,}/g, " ").trim();
}

function localIso(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function todayLocal() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0);
}

export function nextWeekday(from) {
  const date = new Date(from.getFullYear(), from.getMonth(), from.getDate() + 1, 12, 0, 0);
  while (date.getDay() === 0 || date.getDay() === 6) {
    date.setDate(date.getDate() + 1);
  }
  return localIso(date);
}

function parseIsoDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) fail(`executionDate must be YYYY-MM-DD, got ${value}`);
  const [y, m, d] = value.split("-").map(Number);
  const date = new Date(y, m - 1, d, 12, 0, 0);
  if (localIso(date) !== value) fail(`executionDate is not a real date: ${value}`);
  return date;
}

function amountFromCents(cents) {
  if (!Number.isInteger(cents) || cents < 1) {
    fail("amountCents must be a positive integer");
  }
  const whole = Math.trunc(cents / 100);
  const frac = String(cents % 100).padStart(2, "0");
  return Number(`${whole}.${frac}`);
}

function cleanIban(value, label) {
  const iban = String(value ?? "").replace(/\s+/g, "").toUpperCase();
  if (!SEPA.validateIBAN(iban)) fail(`${label} is not a valid IBAN`);
  return iban;
}

export function prepare(input) {
  if (!input || typeof input !== "object") fail("input.json must be an object");
  const ampWord = input.language === "de" ? "und" : "and";
  const debtorName = transliterate(input.debtorName ?? "", ampWord);
  if (!debtorName) fail("debtorName is required");
  if (debtorName.length > 70) fail("debtorName is longer than 70 characters after transliteration");
  const debtorIban = cleanIban(input.debtorIban, "debtorIban");

  let executionDate;
  if (input.executionDate == null || input.executionDate === "") {
    executionDate = nextWeekday(todayLocal());
  } else {
    const parsed = parseIsoDate(String(input.executionDate));
    executionDate = localIso(parsed);
    if (executionDate < localIso(todayLocal())) fail("executionDate is in the past");
  }

  if (!Array.isArray(input.payments) || input.payments.length === 0) {
    fail("payments must be a non-empty array");
  }

  const payments = input.payments.map((payment, index) => {
    if (payment.creditorReference != null && payment.remittance != null && payment.remittance !== "") {
      fail(`payment ${index + 1} has both creditorReference and remittance`);
    }
    const creditorName = transliterate(payment.creditorName ?? "", ampWord);
    if (!creditorName) fail(`payment ${index + 1} is missing creditorName`);
    if (creditorName.length > 70) {
      fail(`payment ${index + 1} creditorName is longer than 70 characters after transliteration`);
    }
    const creditorIban = cleanIban(payment.creditorIban, `payment ${index + 1} creditorIban`);
    const amountCents = payment.amountCents;
    amountFromCents(amountCents);

    let referenceKind = "free text";
    let remittance = "";
    let referenceNote = null;
    if (payment.creditorReference != null && payment.creditorReference !== "") {
      remittance = transliterate(payment.creditorReference, ampWord);
      if (remittance.length > 35) {
        fail(`payment ${index + 1} creditorReference is longer than 35 characters`);
      }
      if (!remittance) fail(`payment ${index + 1} creditorReference is empty after transliteration`);
      referenceNote = "sent as the only free text; the generator cannot write a schema-valid structured reference";
    } else if (payment.remittance != null && payment.remittance !== "") {
      remittance = transliterate(payment.remittance, ampWord);
      if (remittance.length > 140) {
        fail(`payment ${index + 1} remittance is longer than 140 characters`);
      }
      if (!remittance) fail(`payment ${index + 1} remittance is empty after transliteration`);
    } else {
      fail(`payment ${index + 1} needs creditorReference or remittance`);
    }

    const townRaw = payment.town ?? "";
    const countryRaw = payment.country ?? "";
    const hasTown = String(townRaw).trim() !== "";
    const hasCountry = String(countryRaw).trim() !== "";
    let town = null;
    let country = null;
    if (hasTown || hasCountry) {
      if (!hasTown || !hasCountry) {
        fail(`payment ${index + 1} needs both town and country, or neither`);
      }
      town = transliterate(townRaw, ampWord);
      country = String(countryRaw).trim().toUpperCase();
      if (!/^[A-Z]{2}$/.test(country)) fail(`payment ${index + 1} country must be a two-letter code`);
      if (!town) fail(`payment ${index + 1} town is empty after transliteration`);
      if (town.length > 35) fail(`payment ${index + 1} town is longer than 35 characters`);
    }

    return {
      creditorName,
      creditorIban,
      amountCents,
      referenceKind,
      remittance,
      referenceNote,
      town,
      country,
    };
  });

  const controlSumCents = payments.reduce((sum, payment) => sum + payment.amountCents, 0);
  return { ampWord, debtorName, debtorIban, executionDate, payments, controlSumCents };
}

function buildXml(prepared) {
  const doc = new SEPA.Document("pain.001.001.09");
  doc.grpHdr.id = `M${randomBytes(8).toString("hex")}`;
  doc.grpHdr.created = new Date();
  doc.grpHdr.initiatorName = prepared.debtorName;

  const info = doc.createPaymentInfo();
  info.batchBooking = true;
  info.requestedExecutionDate = parseIsoDate(prepared.executionDate);
  info.debtorName = prepared.debtorName;
  info.debtorIBAN = prepared.debtorIban;
  info.debtorBIC = "";
  // Credit transfers do not emit these. sepa.js still requires them on validate.
  info.localInstrumentation = "CORE";
  info.sequenceType = "OOFF";
  // Before the transactions, so each InstrId gets the payment info id as prefix.
  doc.addPaymentInfo(info);

  prepared.payments.forEach((payment, index) => {
    const tx = info.createTransaction();
    tx.currency = "EUR";
    tx.amount = amountFromCents(payment.amountCents);
    tx.end2endId = `E${randomBytes(8).toString("hex")}${index}`;
    tx.creditorName = payment.creditorName;
    tx.creditorIBAN = payment.creditorIban;
    tx.remittanceInfo = payment.remittance;
    if (payment.town && payment.country) {
      tx.creditorTownName = payment.town;
      tx.creditorCountry = payment.country;
    }
    info.addTransaction(tx);
  });

  return doc.toString();
}

async function schemaCheck(xml) {
  const xsd = readFileSync(join(here, "schema/pain.001.001.09.xsd"), "utf8");
  const result = await validateXML({
    xml: [{ fileName: "pain.001.001.09.xml", contents: xml }],
    schema: [{ fileName: "pain.001.001.09.xsd", contents: xsd }],
  });
  if (!result.valid) {
    const detail = (result.errors ?? []).map((error) => error.message).join("\n");
    fail(`schema check failed\n${detail || result.rawOutput || ""}`);
  }
}

function readInput(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    fail(`could not read input.json: ${error.message}`);
  }
}

const isDirectRun = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isDirectRun) {
  const args = process.argv.slice(2);
  const preview = args[0] === "--preview";
  const inputPath = resolve(preview ? args[1] : args[0] ?? "");
  if (!inputPath || args.length > (preview ? 2 : 1)) {
    fail("usage: node generate.mjs --preview input.json | node generate.mjs input.json");
  }

  const prepared = prepare(readInput(inputPath));

  if (preview) {
    process.stdout.write(`${JSON.stringify({
      executionDate: prepared.executionDate,
      debtorName: prepared.debtorName,
      debtorIban: prepared.debtorIban,
      transactionCount: prepared.payments.length,
      controlSumCents: prepared.controlSumCents,
      payments: prepared.payments,
    }, null, 2)}\n`);
  } else {
    const xml = buildXml(prepared);
    await schemaCheck(xml);
    const outPath = join(dirname(inputPath), "pain.001.001.09.xml");
    writeFileSync(outPath, xml);
    process.stdout.write(`${JSON.stringify({ wrote: outPath, validated: true })}\n`);
  }
}
