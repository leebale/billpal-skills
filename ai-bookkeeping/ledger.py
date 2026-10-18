#!/usr/bin/env python3
"""Double-entry bookkeeping on plain Beancount files. Python standard library only.

The ledger is one Beancount file per fiscal year. Every write creates the next
version file (`2026-v7.beancount`) and never overwrites one. The script only
reads and writes a strict subset of Beancount, so `bean-check` and Fava accept
every file it writes.

Run `python ledger.py --help` for the commands. SKILL.md explains the workflow.
"""

from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import html
import io
import json
import re
import sys
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import combinations, product
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = "ai-bookkeeping/1"

# ── Frozen vocabulary ────────────────────────────────────────────────────────

ROOTS = ("Assets", "Liabilities", "Equity", "Income", "Expenses")
ROLES = {
    "bank", "cash", "card", "processor", "clearing", "ar", "ap", "vat_in", "vat_out",
    "suspense", "discount_received", "discount_granted", "depreciation",
    "accumulated_depreciation", "fixed_asset", "retained_earnings", "private", "shareholder",
    "opening", "fx_diff",
}
# Owner money: `private` (sole traders, partners) closes into retained earnings at year end,
# `shareholder` (companies) is a clearing account that carries forward.
OWNER_ROLES = {"private", "shareholder"}
MONEY_ROLES = {"bank", "cash", "card", "processor"}
TRANSFER_ROLES = MONEY_ROLES | {"clearing"}
PARTY_ROLES = {"ar", "ap"}
TAX_TYPES = {"vat", "gst", "sales_tax", "other"}
TAX_REGIMES = {
    "standard", "reverse_charge", "intra_community", "exempt", "zero", "export",
    "out_of_scope", "import",
}
TAXED_REGIMES = {"standard", "reverse_charge", "intra_community", "import"}
SELF_ASSESSED = {"reverse_charge", "intra_community"}
CUSTOM_TYPES = {
    "ledger", "version", "profile", "rule", "csv_map", "asset", "statement", "closed",
    "confirm", "review_hold", "allocation", "question",
}
QUESTION_KEYS = {"id", "question", "proposal", "evidence", "answer"}
CONFIRM_KINDS = {"booking", "tax", "document_review"}
# Tax types a VAT registration covers. US sales tax is a cost, not input tax.
VAT_LIKE = {"vat", "gst"}
PAYMENT_METHODS = {"direct_debit", "bank_transfer"}
# Changing these after a close would move amounts between closed tax periods.
PROFILE_FROZEN = {"basis", "vat_status", "vat_basis", "input_vat_basis", "vat_period", "country"}
STATEMENT_KINDS = {"bank", "card", "cashbook", "processor"}
EXTRACTIONS = {"camt", "csv", "ofx", "model", "connector", "manual"}

PROFILE_ENUMS = {
    "basis": {"cash", "accrual"},
    "vat_status": {"regular", "small_business", "exempt", "none"},
    "vat_basis": {"invoice", "payment"},
    "input_vat_basis": {"invoice", "payment"},
    "vat_period": {"month", "quarter", "year", "none"},
    "reports": {"artifact", "html_csv", "both"},
    "language": {"de", "en"},
}
PROFILE_TEXT = {"country", "legal_form", "business", "chart", "notes", "tax_advisor"}
PROFILE_BOOL = {"employees", "company", "vat_extension"}

# ISO 4217 minor units. Everything not listed has 2.
MINOR_UNITS_0 = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX",
    "VND", "VUV", "XAF", "XOF", "XPF",
}
MINOR_UNITS_3 = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}

# Depreciation defaults by country and effective year. `None` means no default:
# the asset record must say it. Sources are listed in countries.md.
COUNTRY_RULES = {
    "AT": [
        {"from": 2023, "to": None, "gwg_limit": "1000", "first_year": "half_year", "vat_standard": "20"},
        {"from": 2020, "to": 2022, "gwg_limit": "800", "first_year": "half_year", "vat_standard": "20"},
    ],
    # 2020 has no vat_standard: Germany charged 16 % from July to December.
    "DE": [
        {"from": 2021, "to": None, "gwg_limit": "800", "first_year": "monthly", "vat_standard": "19"},
        {"from": 2020, "to": 2020, "gwg_limit": "800", "first_year": "monthly"},
        {"from": 2018, "to": 2019, "gwg_limit": "800", "first_year": "monthly", "vat_standard": "19"},
    ],
    # The de minimis safe harbor, per item or invoice, for a business without audited statements.
    "US": [{"from": 2016, "to": None, "gwg_limit": "2500"}],
}
# VAT return and payment due: months after the period ends, and the day (AT UStG § 21 Abs. 1; DE UStG § 18 Abs. 1).
# A DE Dauerfristverlängerung (§ 46 UStDV, profile vat_extension) adds a month.
VAT_DUE = {"AT": (2, 15), "DE": (1, 10)}

# Text that banks put around the counterparty name. Removed for vendor grouping only.
NOISE_WORDS = re.compile(
    r"\b(sepa|lastschrift|ueberweisung|gutschrift|dauerauftrag|kartenzahlung|zahlung|"
    r"pos|visa|mastercard|maestro|debit|credit|card|payment|eref|mref|cred|svwz|abwa|"
    r"kref|ref|nr|online|bezahlung|einzug|basislastschrift|folgelastschrift|end|to|"
    r"ach|ppd|ccd|store|purchase|recurring|checkcard)\b"
)

ACCOUNT_RE = re.compile(r"(?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Z0-9][A-Za-z0-9-]*)+")
STR = r'"((?:[^"\\]|\\.)*)"'
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AMOUNT_RE = r"(-?\d+(?:\.\d+)?)"
CUR_RE = r"([A-Z]{3})"
DATED_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (open|close|balance|custom|\*|!)(?: (.*))?")
OPTION_RE = re.compile(rf"option {STR} {STR}")
OPEN_RE = re.compile(rf"({ACCOUNT_RE.pattern}) +{CUR_RE}")
CLOSE_RE = re.compile(rf"({ACCOUNT_RE.pattern})")
BALANCE_RE = re.compile(rf"({ACCOUNT_RE.pattern}) +{AMOUNT_RE} {CUR_RE}")
CUSTOM_RE = re.compile(rf"{STR} {STR}")
TXN_RE = re.compile(rf"{STR} {STR}")
META_RE = re.compile(rf"([a-z][a-z0-9_]*): {STR}")
POSTING_RE = re.compile(rf"({ACCOUNT_RE.pattern}) +{AMOUNT_RE} {CUR_RE}")
META_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")


class LedgerError(Exception):
    def __init__(self, messages):
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        super().__init__("; ".join(self.messages))


# ── Small helpers ────────────────────────────────────────────────────────────

def load_json(path) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_float=Decimal)
    except OSError as exc:
        raise LedgerError(f"{path}: cannot read ({exc.strerror})") from exc
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{path}: not valid JSON ({exc})") from exc


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(obj):
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def precision_for(currency: str) -> int:
    if currency in MINOR_UNITS_0:
        return 0
    if currency in MINOR_UNITS_3:
        return 3
    return 2


def dec(value, what="amount") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise LedgerError(f"{what}: missing or not a number")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerError(f"{what}: not a finite number")
        return value
    if isinstance(value, float):
        raise LedgerError(f"{what}: pass amounts as strings, not floats ({value!r})")
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
        raise LedgerError(f"{what}: {value!r} is not a plain decimal number like 1234.56")
    return Decimal(text)


def q(value: Decimal, prec: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-prec), rounding=ROUND_HALF_UP)


def money(value, prec: int, what="amount") -> Decimal:
    d = dec(value, what)
    if d.as_tuple().exponent < -prec:
        raise LedgerError(f"{what}: {value} has more than {prec} decimals")
    return q(d, prec)


def fmt(value: Decimal, prec: int) -> str:
    value = q(value, prec)
    if value == 0:
        value = abs(value)
    return format(value, "f")


def parse_rate(value, what="tax rate") -> Decimal:
    rate = dec(value, what)
    if rate < 0 or rate > 100:
        raise LedgerError(f"{what}: {value} must be between 0 and 100 (percentage points)")
    if 0 < rate < 1:
        raise LedgerError(f"{what}: {value} looks like a fraction. Rates are percentage points: 20 for 20 %")
    return rate


def rate_str(rate: Decimal) -> str:
    text = format(rate.normalize(), "f")
    return text


def parse_date(value, what="date") -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise LedgerError(f"{what}: {value!r} is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError(f"{what}: {value} is not a real date") from exc


def opt_date(value, what="date"):
    return None if value in (None, "") else parse_date(value, what)


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def clean_text(value) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return re.sub(r"\s+", " ", text).strip()


_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def translit(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.translate(_TRANSLIT))
    return "".join(c for c in text if not unicodedata.combining(c))


def norm_text(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", translit(clean_text(value)).lower()).strip()


def norm_iban(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def bean_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unescape(body: str, lineno: int) -> str:
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if nxt not in ('"', "\\"):
                raise LedgerError(f"line {lineno}: unsupported escape \\{nxt} in a string")
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def meta_value(key: str, value) -> str:
    """Metadata values are scalar strings. Lists and objects go into `_json` keys."""
    if not META_KEY_RE.fullmatch(key):
        raise LedgerError(f"metadata key {key!r}: use lowercase letters, digits and _")
    if key.endswith("_json"):
        return dump_json(value)
    if isinstance(value, (dict, list)):
        raise LedgerError(f"metadata {key}: lists and objects need a key ending in _json")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        return format(value, "f")
    return clean_text(value)


def account_component(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", translit(clean_text(name))).strip("-")[:60].strip("-")
    if not text:
        text = "X"
    return text[0].upper() + text[1:]


# ── Parsed ledger ────────────────────────────────────────────────────────────

@dataclass
class Posting:
    account: str
    amount: Decimal
    currency: str
    meta: dict = field(default_factory=dict)


@dataclass
class Entry:
    kind: str  # open, close, balance, custom, txn
    date: date
    line: int
    meta: dict = field(default_factory=dict)
    account: str | None = None
    currency: str | None = None
    amount: Decimal | None = None
    ctype: str | None = None
    value: object = None
    flag: str | None = None
    payee: str = ""
    narration: str = ""
    postings: list = field(default_factory=list)


def parse_ledger(text: str) -> tuple[dict, list]:
    """Parse the strict subset. Anything outside it is an error, never skipped."""
    if text.startswith("\ufeff"):
        text = text[1:]
    options, entries, errors = {}, [], []
    cur = None
    for no, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r").rstrip(" ")
        try:
            if "\t" in line:
                raise LedgerError(f"line {no}: tabs are not supported, use spaces")
            stripped = line.strip()
            if not stripped:
                cur = None
                continue
            if stripped.startswith(";"):
                continue
            if not line.startswith(" "):
                cur = None
                if line.startswith("option "):
                    m = OPTION_RE.fullmatch(line)
                    if not m:
                        raise LedgerError(f"line {no}: malformed option")
                    options[unescape(m[1], no)] = unescape(m[2], no)
                    continue
                m = DATED_RE.fullmatch(line)
                if not m:
                    raise LedgerError(f"line {no}: not part of the supported Beancount subset: {line[:80]}")
                d = parse_date(m[1], f"line {no}")
                kind, rest = m[2], m[3] or ""
                if kind == "open":
                    mm = OPEN_RE.fullmatch(rest)
                    if not mm:
                        raise LedgerError(f"line {no}: open needs an account and one currency")
                    cur = Entry("open", d, no, account=mm[1], currency=mm[2])
                elif kind == "close":
                    mm = CLOSE_RE.fullmatch(rest)
                    if not mm:
                        raise LedgerError(f"line {no}: malformed close")
                    cur = Entry("close", d, no, account=mm[1])
                elif kind == "balance":
                    mm = BALANCE_RE.fullmatch(rest)
                    if not mm:
                        raise LedgerError(f"line {no}: malformed balance")
                    cur = Entry("balance", d, no, account=mm[1], amount=Decimal(mm[2]), currency=mm[3])
                elif kind == "custom":
                    mm = CUSTOM_RE.fullmatch(rest)
                    if not mm:
                        raise LedgerError(f"line {no}: custom needs a type and exactly one string value")
                    ctype = unescape(mm[1], no)
                    try:
                        value = json.loads(unescape(mm[2], no), parse_float=Decimal)
                    except json.JSONDecodeError as exc:
                        raise LedgerError(f"line {no}: custom {ctype} value is not JSON ({exc})") from exc
                    cur = Entry("custom", d, no, ctype=ctype, value=value)
                else:
                    mm = TXN_RE.fullmatch(rest)
                    if not mm:
                        raise LedgerError(f"line {no}: transactions need \"payee\" \"narration\"")
                    cur = Entry("txn", d, no, flag=kind, payee=unescape(mm[1], no), narration=unescape(mm[2], no))
                entries.append(cur)
                continue
            if cur is None:
                raise LedgerError(f"line {no}: indented line outside an entry")
            indent = len(line) - len(line.lstrip(" "))
            mm = META_RE.fullmatch(stripped)
            if mm:
                key, value = mm[1], unescape(mm[2], no)
                if cur.kind == "txn" and cur.postings:
                    if indent < 4:
                        raise LedgerError(f"line {no}: transaction metadata must come before the postings")
                    target = cur.postings[-1].meta
                else:
                    target = cur.meta
                if key in target:
                    raise LedgerError(f"line {no}: duplicate metadata key {key}")
                target[key] = value
                continue
            pm = POSTING_RE.fullmatch(stripped)
            if pm and cur.kind == "txn" and indent == 2:
                cur.postings.append(Posting(pm[1], Decimal(pm[2]), pm[3]))
                continue
            raise LedgerError(f"line {no}: not part of the supported Beancount subset: {stripped[:80]}")
        except LedgerError as exc:
            errors.extend(exc.messages)
            if len(errors) > 50:
                break
    if errors:
        raise LedgerError(errors)
    return options, entries


def ser_meta(meta: dict, indent: int) -> str:
    return "".join(f"{' ' * indent}{k}: {bean_string(meta_value(k, v))}\n" for k, v in meta.items() if v is not None)


def ser_custom(d: date, ctype: str, value) -> str:
    return f"{d.isoformat()} custom {bean_string(ctype)} {bean_string(dump_json(value))}\n"


def ser_open(d: date, account: str, currency: str, meta: dict) -> str:
    return f"{d.isoformat()} open {account} {currency}\n" + ser_meta(meta, 2)


def ser_balance(d: date, account: str, amount: Decimal, currency: str, prec: int) -> str:
    return f"{d.isoformat()} balance {account}  {fmt(amount, prec)} {currency}\n"


def ser_txn(d: date, flag: str, payee: str, narration: str, meta: dict, postings: list, prec: int) -> str:
    out = f"{d.isoformat()} {flag} {bean_string(clean_text(payee))} {bean_string(clean_text(narration))}\n"
    out += ser_meta(meta, 2)
    for p in postings:
        out += f"  {p.account}  {fmt(p.amount, prec)} {p.currency}\n" + ser_meta(p.meta, 4)
    return out


# ── Book: the parsed ledger plus derived state ───────────────────────────────

class Book:
    def __init__(self, path: Path, raw: bytes):
        self.path = Path(path)
        self.raw = raw
        self.sha = sha256_bytes(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError(f"{self.path.name}: not UTF-8 text") from exc
        self.options, self.entries = parse_ledger(text)
        self._idx = None
        self._derive()

    @classmethod
    def load(cls, path) -> "Book":
        path = Path(path)
        if not path.exists():
            raise LedgerError(f"{path}: file not found")
        return cls(path, path.read_bytes())

    def _derive(self) -> None:
        self.ledger = None
        self.versions, self.statements, self.closed, self.confirms = [], [], [], []
        self.profile, self.rules, self.csv_maps, self.assets, self.allocations = {}, {}, {}, {}, {}
        self.questions = {}
        self.accounts, self.txns, self.balances, self.holds = {}, [], [], []
        self.custom_errors = []
        for e in self.entries:
            if e.kind == "open":
                if e.account in self.accounts:
                    self.custom_errors.append(f"line {e.line}: {e.account} opened twice")
                    continue
                self.accounts[e.account] = {"open": e.date, "currency": e.currency, "meta": e.meta, "close": None, "line": e.line}
            elif e.kind == "close":
                if e.account in self.accounts:
                    self.accounts[e.account]["close"] = e.date
                else:
                    self.custom_errors.append(f"line {e.line}: close of unopened account {e.account}")
            elif e.kind == "balance":
                self.balances.append(e)
            elif e.kind == "txn":
                self.txns.append(e)
            else:
                v = e.value
                if e.ctype not in CUSTOM_TYPES:
                    self.custom_errors.append(f"line {e.line}: unknown custom type {e.ctype!r}")
                elif not isinstance(v, dict):
                    self.custom_errors.append(f"line {e.line}: custom {e.ctype} value must be a JSON object")
                elif e.ctype == "ledger":
                    if self.ledger is not None:
                        self.custom_errors.append(f"line {e.line}: second custom \"ledger\" entry")
                    else:
                        self.ledger = v
                elif e.ctype == "version":
                    self.versions.append((e.line, v))
                elif e.ctype == "profile":
                    self.profile = {**self.profile, **v}
                elif e.ctype == "rule":
                    self.rules[str(v.get("id"))] = v
                elif e.ctype == "csv_map":
                    self.csv_maps[str(v.get("id"))] = v
                elif e.ctype == "asset":
                    self.assets[str(v.get("id"))] = v
                elif e.ctype == "allocation":
                    self.allocations[str(v.get("id"))] = v
                elif e.ctype == "statement":
                    self.statements.append(v)
                elif e.ctype == "closed":
                    self.closed.append((e.line, v))
                elif e.ctype == "confirm":
                    self.confirms.append(v)
                elif e.ctype == "review_hold":
                    self.holds.append(v)
                elif e.ctype == "question":
                    self.questions[str(v.get("id"))] = {**self.questions.get(str(v.get("id")), {}), **v}
        if self.ledger is None:
            raise LedgerError(f"{self.path.name}: no custom \"ledger\" entry. Is this a ledger written by this skill?")
        lg = self.ledger
        try:
            self.currency = str(lg["currency"])
            self.prec = int(lg.get("precision", precision_for(self.currency)))
            self.fy_from = parse_date(lg["fiscal_year_from"], "ledger fiscal_year_from")
            self.fy_to = parse_date(lg["fiscal_year_to"], "ledger fiscal_year_to")
            self.ledger_id = str(lg["id"])
        except KeyError as exc:
            raise LedgerError(f"custom \"ledger\" is missing {exc}") from exc
        if not 0 <= self.prec <= 4:
            raise LedgerError(f"ledger precision {self.prec} must be between 0 and 4")
        self.version = max((int(v.get("n", 0)) for _, v in self.versions), default=0)
        self.closed_through = max((parse_date(v["through"], "closed through") for _, v in self.closed if "through" in v), default=None)
        self.txn_by_id = {}
        self.reversed = {}
        for t in self.txns:
            tid = t.meta.get("txn_id")
            if tid:
                self.txn_by_id.setdefault(tid, t)
            if t.meta.get("reverses"):
                self.reversed[t.meta["reverses"]] = t
        self.statement_accounts = {s.get("source_id"): s.get("account") for s in self.statements}
        self.statement_sha = {s.get("source_id"): s.get("rows_sha256") for s in self.statements}

    # roles and accounts
    def role_of(self, account: str) -> str | None:
        parts = account.split(":")
        while parts:
            info = self.accounts.get(":".join(parts))
            if info and info["meta"].get("role"):
                return info["meta"]["role"]
            parts.pop()
        return None

    def accounts_with_role(self, role: str) -> list[str]:
        return sorted(a for a, info in self.accounts.items() if info["meta"].get("role") == role)

    def money_accounts(self) -> list[str]:
        return sorted(a for a in self.accounts if self.role_of(a) in MONEY_ROLES)

    def one_role(self, role: str) -> str:
        found = self.accounts_with_role(role)
        if len(found) != 1:
            raise LedgerError(f"need exactly one account with role {role}, found {len(found)}: {found}")
        return found[0]

    def require_open(self, account: str, d: date) -> None:
        info = self.accounts.get(account)
        if info is None:
            raise LedgerError(f"account {account} is not opened. Add it under \"accounts\" first")
        if d < info["open"]:
            raise LedgerError(f"account {account} opens on {info['open']}, booking is on {d}")
        if info["close"] and d >= info["close"]:
            raise LedgerError(f"account {account} was closed on {info['close']}")

    def tax_account(self, role: str, regime: str, rate: Decimal, jurisdiction: str | None = None) -> str:
        cands = [(a, self.accounts[a]["meta"]) for a in self.accounts_with_role(role)]
        general = [c for c in cands if not c[1].get("tax_jurisdiction")]
        if jurisdiction:
            cands = [c for c in cands if c[1].get("tax_jurisdiction") == jurisdiction] or general
        else:
            cands = general or cands
        r = rate_str(rate)
        for test in (
            lambda m: m.get("tax_regime") == regime and m.get("tax_rate") == r,
            lambda m: m.get("tax_regime") == regime and not m.get("tax_rate"),
            lambda m: not m.get("tax_regime") and m.get("tax_rate") == r,
            lambda m: not m.get("tax_regime") and not m.get("tax_rate"),
        ):
            hits = [a for a, m in cands if test(m)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise LedgerError(f"several {role} accounts fit {regime} {r} %: {hits}. Mark them with tax_regime/tax_rate")
        raise LedgerError(f"no {role} account for {regime} {r} %. Open one with role {role}")

    @property
    def basis(self) -> str:
        return self.profile.get("basis", "cash")

    @property
    def vat_regular(self) -> bool:
        return self.profile.get("vat_status") == "regular"

    def vat_basis_for(self, direction: str) -> str:
        if direction == "sale":
            return self.profile.get("vat_basis", "invoice")
        return self.profile.get("input_vat_basis") or default_input_vat_basis(self.profile)

    # balances
    def _index(self):
        if self._idx is None:
            rows = defaultdict(list)
            for t in self.txns:
                for p in t.postings:
                    rows[p.account].append((t.date, p.amount))
            idx = {}
            for acct, items in rows.items():
                items.sort(key=lambda r: r[0])
                dates, cum, total = [], [], Decimal(0)
                for d, amt in items:
                    total += amt
                    dates.append(d)
                    cum.append(total)
                idx[acct] = (dates, cum)
            self._idx = idx
        return self._idx

    def balance(self, account: str, before: date | None = None, through: date | None = None) -> Decimal:
        """Sum of the account and its children, dated before `before` or up to `through`."""
        total = Decimal(0)
        prefix = account + ":"
        for acct, (dates, cum) in self._index().items():
            if acct != account and not acct.startswith(prefix):
                continue
            if before is not None:
                i = bisect_left(dates, before)
            elif through is not None:
                i = bisect_right(dates, through)
            else:
                i = len(dates)
            if i:
                total += cum[i - 1]
        return total

    def live_txns(self):
        """Transactions that are neither reversed nor reversals."""
        for t in self.txns:
            tid = t.meta.get("txn_id")
            if t.meta.get("reverses") or (tid and tid in self.reversed):
                continue
            yield t

    def confirmed(self, kind: str, key: str) -> bool:
        return any(c.get("kind") == kind and c.get("ref") == key for c in self.confirms)

    def documents_in_ledger(self) -> set:
        """Documents a live booking covers, including receipts linked to a booked invoice."""
        found = set()
        for t in self.live_txns():
            if t.meta.get("document"):
                found.add(t.meta["document"])
            for key in ("documents_json", "linked_documents_json"):
                if t.meta.get(key):
                    found.update(json.loads(t.meta[key]))
            for p in t.postings:
                if p.meta.get("document"):
                    found.add(p.meta["document"])
        return found


def default_input_vat_basis(profile: dict) -> str:
    if profile.get("country") == "AT":
        return profile.get("vat_basis", "invoice")
    return "invoice"


def validate_profile(profile: dict) -> list[str]:
    errs = []
    for k, v in profile.items():
        if k in PROFILE_ENUMS:
            if v not in PROFILE_ENUMS[k]:
                errs.append(f"profile {k}: {v!r} is not one of {sorted(PROFILE_ENUMS[k])}")
        elif k in PROFILE_BOOL:
            if not isinstance(v, bool):
                errs.append(f"profile {k}: true or false")
        elif k in PROFILE_TEXT:
            if not isinstance(v, str):
                errs.append(f"profile {k}: text")
        else:
            errs.append(f"profile: unknown field {k!r}")
    for k in ("country", "legal_form", "basis", "vat_status", "chart"):
        if not profile.get(k):
            errs.append(f"profile {k} is required")
    if profile.get("country") and not re.fullmatch(r"[A-Z]{2}", str(profile["country"])):
        errs.append("profile country: two-letter ISO code like AT")
    if profile.get("vat_status") == "regular":
        for k in ("vat_basis", "vat_period"):
            if not profile.get(k):
                errs.append(f"profile {k} is required for VAT-registered businesses")
    return errs


# ── Open items (accrual) ─────────────────────────────────────────────────────

def parse_allocations(raw: str, where: str, prec: int) -> list[dict]:
    try:
        allocs = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{where}: allocations_json is not JSON") from exc
    if not isinstance(allocs, list) or not allocs:
        raise LedgerError(f"{where}: allocations_json must be a non-empty list")
    out = []
    for a in allocs:
        if not isinstance(a, dict) or set(a) - {"document", "amount"}:
            raise LedgerError(f"{where}: each allocation is {{document, amount}}")
        doc = a.get("document")
        if doc is not None and not isinstance(doc, str):
            raise LedgerError(f"{where}: allocation document must be a string or null")
        out.append({"document": doc, "amount": money(a.get("amount"), prec, f"{where} allocation amount")})
    return out


def compute_items(book: Book, through: date | None = None):
    """Open items per document from origin postings and allocations.

    Returns (items, unallocated, errors). `through` limits by date."""
    items, unalloc, errors = {}, defaultdict(Decimal), []
    ordered = sorted(book.txns, key=lambda t: (t.date, t.line))
    # A reversed booking and its reversal cancel out, so the document can be booked again.
    gone = {tid for tid, rev in book.reversed.items() if not through or rev.date <= through}
    for t in ordered:
        if through and t.date > through:
            continue
        if t.meta.get("txn_id") in gone or t.meta.get("reverses") in gone:
            continue
        for p in t.postings:
            if book.role_of(p.account) not in PARTY_ROLES:
                continue
            doc = p.meta.get("document")
            if doc and "allocations_json" not in p.meta:
                if doc in items:
                    errors.append(f"line {t.line}: document {doc} is booked twice on Personenkonten")
                    continue
                try:
                    ddate = parse_date(p.meta["document_date"], f"line {t.line} document_date") if p.meta.get("document_date") else t.date
                except LedgerError as exc:
                    errors.extend(exc.messages)
                    ddate = t.date
                items[doc] = {
                    "document": doc, "account": p.account, "amount": p.amount, "remaining": p.amount,
                    "date": ddate, "number": p.meta.get("document_number") or t.meta.get("document_number", ""),
                    "due": p.meta.get("due_date") or t.meta.get("due_date", ""), "party": p.meta.get("party") or t.payee,
                    "txn_id": t.meta.get("txn_id", ""), "allocs": [],
                }
    for t in ordered:
        if through and t.date > through:
            continue
        reversal = bool(t.meta.get("reverses"))
        for p in t.postings:
            if book.role_of(p.account) not in PARTY_ROLES or "allocations_json" not in p.meta:
                if book.role_of(p.account) in PARTY_ROLES and not p.meta.get("document") and book.basis == "accrual":
                    errors.append(f"line {t.line}: {p.account} posting needs document or allocations_json")
                continue
            where = f"line {t.line}"
            try:
                allocs = parse_allocations(p.meta["allocations_json"], where, book.prec)
            except LedgerError as exc:
                errors.extend(exc.messages)
                continue
            if sum(a["amount"] for a in allocs) != p.amount:
                errors.append(f"{where}: allocations add up to {sum(a['amount'] for a in allocs)}, posting is {p.amount}")
                continue
            for a in allocs:
                if a["document"] is None:
                    unalloc[p.account] += a["amount"]
                    continue
                item = items.get(a["document"])
                if item is None:
                    errors.append(f"{where}: allocation to {a['document']}, which is not booked as an open item")
                    continue
                if item["account"] != p.account:
                    errors.append(f"{where}: {a['document']} is on {item['account']}, allocation is on {p.account}")
                    continue
                before, after = item["remaining"], item["remaining"] + a["amount"]
                if not reversal and (abs(after) > abs(before) or (after != 0 and (after > 0) != (before > 0))):
                    errors.append(
                        f"{where}: allocation {a['amount']} to {a['document']} goes past its open amount {before}"
                    )
                    continue
                item["remaining"] = after
                item["allocs"].append({"date": t.date, "amount": a["amount"], "txn_id": t.meta.get("txn_id", "")})
    return items, {k: v for k, v in unalloc.items() if v != 0}, errors


# ── Validation (hard rules: a file that fails is never written) ──────────────

def validate(book: Book) -> list[str]:
    errs = list(book.custom_errors)
    lg = book.ledger
    if book.options.get("operating_currency") != book.currency:
        errs.append("option operating_currency must match the ledger currency")
    if book.fy_from > book.fy_to:
        errs.append("fiscal_year_from is after fiscal_year_to")
    if lg.get("opening") not in ("booked", "new_business", "missing"):
        errs.append("ledger opening must be booked, new_business or missing")
    ns = [int(v.get("n", 0)) for _, v in book.versions]
    if ns != list(range(1, len(ns) + 1)):
        errs.append(f"version chain is broken: {ns}")
    errs += validate_profile(book.profile)
    need = ["suspense", "retained_earnings"]
    if book.vat_regular:
        need += ["vat_in", "vat_out"]
    if book.basis == "accrual":
        need += ["ar", "ap"]
    have = {info["meta"].get("role") for info in book.accounts.values()}
    errs += [f"the chart needs an account with role {r}" for r in need if r not in have]
    if not have & MONEY_ROLES:
        errs.append("the chart needs at least one bank, cash, card or processor account")
    for acct, info in book.accounts.items():
        if info["currency"] != book.currency:
            errs.append(f"{acct}: currency {info['currency']}, the ledger uses {book.currency}")
        role = info["meta"].get("role")
        if role and role not in ROLES:
            errs.append(f"{acct}: unknown role {role!r}")
    for key, rule in book.rules.items():
        try:
            validate_rule(rule, book)
        except LedgerError as exc:
            errs += exc.messages
    seen_ids = set()
    closed_marks = [(line, parse_date(v["through"], "closed through")) for line, v in book.closed if "through" in v]
    for t in book.txns:
        where = f"line {t.line}"
        if not book.fy_from <= t.date <= book.fy_to:
            errs.append(f"{where}: {t.date} is outside the fiscal year {book.fy_from} to {book.fy_to}")
        tid = t.meta.get("txn_id")
        if tid:
            if tid in seen_ids:
                errs.append(f"{where}: txn_id {tid} used twice")
            seen_ids.add(tid)
        for cline, through in closed_marks:
            if t.line > cline and t.date <= through:
                errs.append(f"{where}: booked on {t.date} after the period was closed through {through}")
        if len(t.postings) < 2:
            errs.append(f"{where}: a transaction needs at least two postings")
        total = Decimal(0)
        for p in t.postings:
            total += p.amount
            info = book.accounts.get(p.account)
            if info is None:
                errs.append(f"{where}: account {p.account} is not opened")
            else:
                if t.date < info["open"]:
                    errs.append(f"{where}: {p.account} opens on {info['open']}")
                if info["close"] and t.date >= info["close"]:
                    errs.append(f"{where}: {p.account} is closed")
            if p.currency != book.currency:
                errs.append(f"{where}: {p.currency} posting in a {book.currency} ledger. Convert and keep fx_json")
            if p.amount.as_tuple().exponent < -book.prec:
                errs.append(f"{where}: {p.amount} has more than {book.prec} decimals")
            for k in ("tax_regime",):
                if k in p.meta and p.meta[k] not in TAX_REGIMES:
                    errs.append(f"{where}: tax_regime {p.meta[k]!r} unknown")
            if "tax_type" in p.meta and p.meta["tax_type"] not in TAX_TYPES:
                errs.append(f"{where}: tax_type {p.meta['tax_type']!r} unknown")
            if "tax_rate" in p.meta:
                try:
                    parse_rate(p.meta["tax_rate"])
                except LedgerError as exc:
                    errs.append(f"{where}: {exc.messages[0]}")
            if "tax_point" in p.meta and closed_marks:
                try:
                    tp = parse_date(p.meta["tax_point"], f"{where} tax_point")
                except LedgerError as exc:
                    errs += exc.messages
                    continue
                for cline, through in closed_marks:
                    if t.line > cline and tp <= through:
                        errs.append(f"{where}: tax point {tp} is in the period closed through {through}. "
                                    "A late correction goes into an open period")
                        break
        if total != 0:
            errs.append(f"{where}: transaction does not balance (off by {total})")
        if "fx_json" in t.meta:
            try:
                fx = json.loads(t.meta["fx_json"], parse_float=Decimal)
                if dec(fx.get("rate"), "fx rate") <= 0:
                    raise LedgerError("fx rate must be positive")
            except (LedgerError, json.JSONDecodeError, AttributeError) as exc:
                errs.append(f"{where}: fx_json needs amount, currency and rate ({exc})")
    for target, rev in book.reversed.items():
        orig = book.txn_by_id.get(target)
        if orig is None:
            errs.append(f"line {rev.line}: reverses unknown txn {target}")
            continue
        a = sorted((p.account, p.amount) for p in orig.postings)
        b = sorted((p.account, -p.amount) for p in rev.postings)
        if a != b:
            errs.append(f"line {rev.line}: reversal does not mirror {target}")
    for b in book.balances:
        actual = book.balance(b.account, before=b.date)
        if b.account not in book.accounts:
            errs.append(f"line {b.line}: balance on unopened account {b.account}")
        elif actual != b.amount:
            errs.append(
                f"line {b.line}: balance check failed for {b.account} on {b.date}: "
                f"statement says {b.amount}, ledger has {actual} (difference {b.amount - actual})"
            )
    for acct in (a for a in book.accounts if book.role_of(a) == "cash"):
        dates, cum = book._index().get(acct, ([], []))
        for i, d in enumerate(dates):
            if (i + 1 == len(dates) or dates[i + 1] != d) and cum[i] < 0:
                errs.append(f"{acct}: cash goes negative on {d} ({cum[i]}). A deposit or withdrawal is missing")
                break
    _, _, item_errs = compute_items(book)
    errs += item_errs
    return errs


def validate_rule(rule: dict, book: Book) -> None:
    rid = rule.get("id")
    if not rid or not isinstance(rid, str):
        raise LedgerError("rule needs a text id")
    allowed = {"id", "text", "iban", "money", "amount_min", "amount_max", "on_account", "account", "tax",
               "payee", "priority", "disabled", "needs_review", "note", "share", "private_account", "deductible"}
    match_spec(rule, book, f"rule {rid}", allowed)
    if rule.get("deductible") is not None:
        parse_share(rule["deductible"], f"rule {rid} deductible")
        if rule.get("share"):
            raise LedgerError(f"rule {rid}: deductible and share do not go together")


ALLOCATION_KEYS = {"id", "text", "iban", "money", "amount_min", "amount_max", "account", "tax", "share", "basis",
                   "private_account", "from", "to", "every", "topic", "document", "payee", "note", "disabled"}


def validate_allocation(a: dict, book: Book) -> None:
    aid = a.get("id")
    if not aid or not isinstance(aid, str):
        raise LedgerError("allocation needs a text id")
    where = f"allocation {aid}"
    match_spec(a, book, where, ALLOCATION_KEYS)
    if a.get("disabled"):
        return
    if not a.get("share"):
        raise LedgerError(f"{where}: needs share, the business part (0.25 or 8.5/52)")
    if a.get("every") not in (None, "month", "quarter", "year"):
        raise LedgerError(f"{where}: every is month, quarter or year")
    lo, hi = opt_date(a.get("from"), f"{where} from"), opt_date(a.get("to"), f"{where} to")
    if lo and hi and lo > hi:
        raise LedgerError(f"{where}: from is after to")


def match_spec(spec: dict, book: Book, where: str, allowed: set) -> None:
    """Shared checks for rules and allocations."""
    extra = set(spec) - allowed
    if extra:
        raise LedgerError(f"{where}: unknown fields {sorted(extra)}")
    if spec.get("disabled"):
        return
    if not spec.get("text") and not spec.get("iban"):
        raise LedgerError(f"{where}: needs text or iban to match on")
    if spec.get("money") not in (None, "in", "out"):
        raise LedgerError(f"{where}: money is in or out")
    if not spec.get("account") or spec["account"] not in book.accounts:
        raise LedgerError(f"{where}: account {spec.get('account')} is not opened")
    if "tax" in spec and spec["tax"] is not None:
        normalize_tax(spec["tax"], book, where)
    if spec.get("share") is not None:
        parse_share(spec["share"], where)
        acct = spec.get("private_account") or private_account(book, where)
        if book.role_of(acct) not in OWNER_ROLES:
            raise LedgerError(f"{where}: private_account {acct} needs role private or shareholder")


def parse_share(value, where: str) -> Decimal:
    """0.25, "0.25" or "8.5/52": the business part, above 0 and at most 1."""
    try:
        num, _, den = str(value).partition("/")
        share = dec(num.strip(), where) / (dec(den.strip(), where) if den else 1)
    except (LedgerError, ArithmeticError):
        share = None
    if isinstance(value, bool) or share is None or not 0 < share <= 1:
        raise LedgerError(f"{where}: share {value!r} must be a number or fraction above 0 and at most 1 (8.5/52)")
    return share


def private_account(book: Book, where: str) -> str:
    accts = sorted(a for a in book.accounts if book.role_of(a) in OWNER_ROLES)
    if len(accts) != 1:
        raise LedgerError(f"{where}: give private_account, the ledger has {len(accts)} accounts with role private or shareholder")
    return accts[0]


# ── Tax engine and booking lines ─────────────────────────────────────────────

TAX_KEYS = {"type", "regime", "rate", "amount", "recoverable", "point", "report_codes", "source",
            "confirmed", "direction", "base", "exemption_reason", "jurisdiction"}
JURISDICTION_RE = re.compile(r"[A-Z]{2}(?:-[A-Z0-9]{1,20}){0,3}")
LINE_KEYS = {"account", "amount", "amount_is", "tax", "document", "allocations", "asset", "meta", "description",
             "deductible"}
RESERVED_META = {"txn_id", "reverses", "allocations_json", "document", "asset", "opening"}


def normalize_tax(tax, book: Book, where: str, prec: int | None = None) -> dict:
    """`prec` is the precision of the amounts given, for documents in another currency."""
    if not isinstance(tax, dict):
        raise LedgerError(f"{where}: tax must be an object")
    extra = set(tax) - TAX_KEYS
    if extra:
        raise LedgerError(f"{where}: unknown tax fields {sorted(extra)}")
    prec = book.prec if prec is None else prec
    regime = tax.get("regime")
    if regime not in TAX_REGIMES:
        raise LedgerError(f"{where}: tax regime {regime!r} is not one of {sorted(TAX_REGIMES)}")
    ttype = tax.get("type") or "vat"
    if ttype not in TAX_TYPES:
        raise LedgerError(f"{where}: tax type {ttype!r} is not one of {sorted(TAX_TYPES)}")
    if regime in TAXED_REGIMES and tax.get("rate") is None:
        raise LedgerError(f"{where}: {regime} needs a rate")
    rate = parse_rate(tax.get("rate", "0"), f"{where} tax rate")
    recoverable = None
    if tax.get("recoverable") is not None:
        recoverable = dec(tax["recoverable"], f"{where} recoverable")
        if not 0 <= recoverable <= 1:
            raise LedgerError(f"{where}: recoverable is a share between 0 and 1")
    if tax.get("direction") not in (None, "purchase", "sale"):
        raise LedgerError(f"{where}: tax direction is purchase or sale")
    codes = tax.get("report_codes") or []
    if not isinstance(codes, list) or any(
        not isinstance(c, dict) or set(c) - {"code", "part"} or not isinstance(c.get("code"), str)
        or c.get("part") not in ("base", "tax", "input_tax") for c in codes
    ):
        raise LedgerError(f"{where}: report_codes is a list of {{code, part: base|tax|input_tax}}")
    source = tax.get("source", "agent")
    if source not in ("document", "rule", "agent", "user", "derived"):
        raise LedgerError(f"{where}: tax source is document, rule, agent, user or derived")
    confirmed = tax.get("confirmed", source != "agent")
    if not isinstance(confirmed, bool):
        raise LedgerError(f"{where}: tax confirmed is true or false")
    jur = tax.get("jurisdiction")
    if jur is not None and not (isinstance(jur, str) and JURISDICTION_RE.fullmatch(jur)):
        raise LedgerError(f"{where}: tax jurisdiction is a code like CA, NY or NY-NYC")
    return {
        "jurisdiction": jur,
        "type": ttype, "regime": regime, "rate": rate, "recoverable": recoverable,
        "direction": tax.get("direction"), "point": opt_date(tax.get("point"), f"{where} tax point"),
        "codes": codes, "source": source, "confirmed": confirmed,
        "amount": None if tax.get("amount") is None else money(tax["amount"], prec, f"{where} tax amount"),
        "base": None if tax.get("base") is None else money(tax["base"], prec, f"{where} tax base"),
        "exemption_reason": clean_text(tax.get("exemption_reason")) or None,
    }


def line_meta(book: Book, line: dict, where: str) -> dict:
    meta = {}
    if line.get("document"):
        meta["document"] = str(line["document"])
    if line.get("allocations") is not None:
        allocs = line["allocations"]
        if not isinstance(allocs, list) or not allocs:
            raise LedgerError(f"{where}: allocations must be a non-empty list")
        meta["allocations_json"] = [
            {"document": a.get("document"), "amount": fmt(money(a.get("amount"), book.prec, f"{where} allocation"), book.prec)}
            for a in allocs
        ]
        parse_allocations(dump_json(meta["allocations_json"]), where, book.prec)
    if line.get("asset"):
        meta["asset"] = str(line["asset"])
    if line.get("description"):
        meta["description"] = line["description"]
    extra = line.get("meta") or {}
    if not isinstance(extra, dict):
        raise LedgerError(f"{where}: meta must be an object")
    for k, v in extra.items():
        if k in RESERVED_META or k.startswith("tax_"):
            raise LedgerError(f"{where}: meta key {k} is reserved")
        meta_value(k, v)
        meta[k] = v
    return meta


def expand_line(book: Book, line: dict, amount: Decimal, d: date, where: str, **kw) -> list[Posting]:
    """The postings of one line; a `deductible` share moves the rest of the base to <account>:Nondeductible."""
    posts = _expand_line(book, line, amount, d, where, **kw)
    if line.get("deductible") is None:
        return posts
    share = parse_share(line["deductible"], f"{where} deductible")
    base = posts[0]
    if not base.account.startswith("Expenses:") or line.get("allocations") or line.get("asset"):
        raise LedgerError(f"{where}: deductible goes on an expense line without allocations or asset")
    rest = f"{base.account}:Nondeductible"
    if rest not in book.accounts:
        raise LedgerError(f"{where}: open {rest} (under accounts) for the part that is not deductible")
    book.require_open(rest, d)
    if share == 1:
        return posts
    keep, m1, m2 = q(base.amount * share, book.prec), dict(base.meta), dict(base.meta)
    for k in ("tax_base", "tax_amount"):
        if k in base.meta:
            whole = dec(base.meta[k])
            part = q(whole * share, book.prec)
            m1[k], m2[k] = fmt(part, book.prec), fmt(whole - part, book.prec)
    return [Posting(base.account, keep, base.currency, m1), Posting(rest, base.amount - keep, base.currency, m2), *posts[1:]]


def _expand_line(book: Book, line: dict, amount: Decimal, d: date, where: str, *, require_tax: bool,
                 default_point: date | None = None, cash_rows: bool = False) -> list[Posting]:
    """Turn one booking line into postings: the base posting plus VAT postings."""
    acct = line.get("account")
    if not isinstance(acct, str) or not ACCOUNT_RE.fullmatch(acct):
        raise LedgerError(f"{where}: {acct!r} is not an account path")
    book.require_open(acct, d)
    meta = line_meta(book, line, where)
    cur, prec = book.currency, book.prec
    root, role = acct.split(":")[0], book.role_of(acct)
    taxable = root in ("Income", "Expenses") or role == "fixed_asset"
    if line.get("tax") is None:
        if require_tax and taxable and book.vat_regular:
            raise LedgerError(
                f"{where}: {acct} needs a tax treatment. Use regime exempt or out_of_scope when no VAT applies"
            )
        return [Posting(acct, amount, cur, meta)]
    t = normalize_tax(line["tax"], book, where)
    direction = t["direction"] or ("sale" if root == "Income" or role == "discount_granted" else "purchase")
    regime, rate = t["regime"], t["rate"]
    if (cash_rows and book.basis == "cash" and regime in TAXED_REGIMES and rate
            and book.vat_basis_for(direction) == "invoice" and t["point"] is None):
        raise LedgerError(f"{where}: VAT is due on the invoice date here. Give tax.point (the document date)")
    point = t["point"] or default_point or d
    base_meta = {
        "tax_type": t["type"], "tax_regime": regime, "tax_rate": rate_str(rate), "tax_direction": direction,
        "tax_point": point.isoformat(), "tax_source": t["source"], "tax_confirmed": "yes" if t["confirmed"] else "no",
    }
    if t["codes"]:
        base_meta["report_codes_json"] = t["codes"]
    if t["exemption_reason"]:
        base_meta["exemption_reason"] = t["exemption_reason"]
    tax_meta = {"tax_part": "tax", "tax_type": t["type"], "tax_regime": regime, "tax_rate": rate_str(rate),
                "tax_direction": direction, "tax_point": point.isoformat()}
    jur = t["jurisdiction"]
    if jur:
        base_meta["tax_jurisdiction"] = tax_meta["tax_jurisdiction"] = jur
    if (regime in SELF_ASSESSED and direction == "purchase" and rate == 0 and t["type"] in VAT_LIKE
            and book.profile.get("vat_status") in ("regular", "small_business")):
        raise LedgerError(f"{where}: {regime} at 0 % books no VAT. The 0 % is what the supplier charged; the buyer "
                          "owes the domestic rate. Give that rate, or leave tax off and pass --documents")
    if regime not in TAXED_REGIMES or rate == 0 or (regime in SELF_ASSESSED and direction == "sale"):
        return [Posting(acct, amount, cur, {**meta, **base_meta, "tax_base": fmt(amount, prec), "tax_amount": "0"})]
    if regime == "import":
        if role != "vat_in":
            raise LedgerError(f"{where}: import VAT is booked on a vat_in account, the base stays with the goods")
        if t["base"] is None:
            raise LedgerError(f"{where}: import VAT needs tax.base (the customs value)")
        return [Posting(acct, amount, cur, {**meta, **base_meta, "tax_part": "input_tax",
                                            "tax_base": fmt(t["base"], prec), "tax_amount": fmt(amount, prec)})]
    if direction == "sale" and t["type"] in VAT_LIKE and not book.vat_regular:
        raise LedgerError(f"{where}: the profile says the business does not charge VAT. "
                          "US sales tax is type sales_tax")
    given = t["amount"]
    if given and amount and (given > 0) != (amount > 0):
        raise LedgerError(f"{where}: tax.amount must have the same sign as the line amount")
    if regime in SELF_ASSESSED or line.get("amount_is") == "net":
        net = amount
        tax_amt = given if given is not None else q(net * rate / 100, prec)
    else:
        tax_amt = given if given is not None else q(amount * rate / (100 + rate), prec)
        net = amount - tax_amt
    if direction == "sale":
        return [
            Posting(acct, net, cur, {**meta, **base_meta, "tax_base": fmt(net, prec), "tax_amount": fmt(tax_amt, prec)}),
            Posting(book.tax_account("vat_out", regime, rate, jur), tax_amt, cur, tax_meta),
        ]
    recov = t["recoverable"] if t["recoverable"] is not None else Decimal(
        1 if book.vat_regular and t["type"] in VAT_LIKE else 0)
    rec = q(tax_amt * recov, prec)
    out = [Posting(acct, net + tax_amt - rec, cur, {**meta, **base_meta, "tax_base": fmt(net, prec),
                                                     "tax_amount": fmt(tax_amt, prec), "recoverable": rate_str(recov)})]
    if rec:
        out.append(Posting(book.tax_account("vat_in", regime, rate, jur), rec, cur, {**tax_meta, "tax_part": "input_tax"}))
    if regime in SELF_ASSESSED:
        out.append(Posting(book.tax_account("vat_out", regime, rate, jur), -tax_amt, cur, tax_meta))
    return out


def user_tax(lines, confirmed):
    """A booking the user confirmed also confirms the tax treatment they saw in the preview, including a
    derived reverse charge (the preview line names it)."""
    if confirmed != "user" or not isinstance(lines, list):
        return lines
    return [{**ln, "tax": confirm_tax(ln["tax"])} if isinstance(ln, dict) and isinstance(ln.get("tax"), dict) else ln
            for ln in lines]


def confirm_tax(tax: dict) -> dict:
    tax = {**tax, "source": tax.get("source", "user")}
    return {**tax, "confirmed": True} if tax["source"] == "derived" else tax


def expand_lines(book: Book, lines, fixed: Decimal, d: date, where: str, *, require_tax: bool,
                 default_point: date | None = None, cash_rows: bool = False) -> list[Posting]:
    """Expand lines so they balance `fixed`. One line may omit its amount and takes the rest."""
    if not isinstance(lines, list) or not lines:
        raise LedgerError(f"{where}: lines must be a non-empty list")
    parts, rest_at = [None] * len(lines), None
    kw = dict(require_tax=require_tax, default_point=default_point, cash_rows=cash_rows)
    for i, ln in enumerate(lines):
        lw = f"{where} line {i + 1}"
        if not isinstance(ln, dict):
            raise LedgerError(f"{lw}: must be an object")
        extra = set(ln) - LINE_KEYS
        if extra:
            raise LedgerError(f"{lw}: unknown fields {sorted(extra)}")
        if ln.get("amount_is", "gross") not in ("gross", "net"):
            raise LedgerError(f"{lw}: amount_is is gross or net")
        if ln.get("amount") is None:
            if ln.get("allocations"):
                amt = sum((money(a.get("amount"), book.prec, f"{lw} allocation") for a in ln["allocations"]), Decimal(0))
            else:
                if rest_at is not None:
                    raise LedgerError(f"{where}: only one line may leave out its amount")
                if ln.get("amount_is") == "net":
                    raise LedgerError(f"{lw}: the line without amount takes the gross rest")
                rest_at = i
                continue
        else:
            amt = money(ln["amount"], book.prec, f"{lw} amount")
        parts[i] = expand_line(book, ln, amt, d, lw, **kw)
    if rest_at is not None:
        so_far = fixed + sum((p.amount for ps in parts if ps for p in ps), Decimal(0))
        parts[rest_at] = expand_line(book, lines[rest_at], -so_far, d, f"{where} line {rest_at + 1}", **kw)
    posts = [p for ps in parts for p in ps]
    total = fixed + sum((p.amount for p in posts), Decimal(0))
    if total != 0:
        raise LedgerError(f"{where}: lines are off by {total}. They must add up to {-fixed}")
    return [p for p in posts if p.amount != 0 or p.meta]


# ── Writing ──────────────────────────────────────────────────────────────────

def next_path(path: Path, n: int) -> Path:
    m = re.fullmatch(r"(.*)-v(\d+)(\.beancount(?:\.txt)?)", path.name)
    if m:
        return path.with_name(f"{m[1]}-v{n}{m[3]}")
    suffix = ".beancount.txt" if path.name.endswith(".beancount.txt") else ".beancount"
    stem = path.name[: -len(suffix)] if path.name.endswith(suffix) else path.stem
    return path.with_name(f"{stem}-v{n}{suffix}")


def extend(book: Book, text: str) -> Book:
    """In-memory book with extra entries, used to expand lines against new accounts."""
    raw = book.raw.decode("utf-8")
    if not raw.endswith("\n"):
        raw += "\n"
    return Book(book.path, (raw + "\n" + text).encode("utf-8"))


def write_next(book: Book, body: str, note: str) -> Book:
    n = book.version + 1
    target = next_path(book.path, n)
    if target.exists():
        raise LedgerError(
            f"{target.name} already exists. Another session wrote version {n}: load the newest file and start again"
        )
    raw = book.raw.decode("utf-8")
    if not raw.endswith("\n"):
        raw += "\n"
    version = {"n": n, "parent": book.path.name, "parent_sha256": book.sha, "note": clean_text(note)}
    text = raw + "\n" + (body.rstrip("\n") + "\n\n" if body.strip() else "") + ser_custom(book.fy_from, "version", version)
    data = text.encode("utf-8")
    nb = Book(target, data)
    errs = validate(nb)
    if errs:
        raise LedgerError(errs)
    with open(target, "xb") as fh:
        fh.write(data)
    return nb


ACCOUNT_SPEC_KEYS = {"account", "name", "code", "role", "personenkonto", "iban", "tax_regime", "tax_rate",
                     "description", "date", "billpal_account", "tax_jurisdiction", "partner", "share", "auto_tax", "tax_line"}


def open_text(book: Book, specs) -> str:
    if not specs:
        return ""
    if not isinstance(specs, list):
        raise LedgerError("accounts must be a list")
    out, seen = [], set()
    for s in specs:
        if not isinstance(s, dict):
            raise LedgerError("each account is an object")
        extra = set(s) - ACCOUNT_SPEC_KEYS
        if extra:
            raise LedgerError(f"account {s.get('account')}: unknown fields {sorted(extra)}")
        acct = s.get("account")
        if not isinstance(acct, str) or not ACCOUNT_RE.fullmatch(acct):
            raise LedgerError(f"{acct!r} is not a valid account path (Root:Part:Part, letters, digits and -)")
        if acct in book.accounts or acct in seen:
            raise LedgerError(f"account {acct} already exists")
        seen.add(acct)
        if s.get("role") and s["role"] not in ROLES:
            raise LedgerError(f"account {acct}: unknown role {s['role']!r}")
        if s.get("tax_regime") and s["tax_regime"] not in TAX_REGIMES:
            raise LedgerError(f"account {acct}: unknown tax_regime {s['tax_regime']!r}")
        meta = {}
        for k in ("role", "code", "name", "personenkonto", "iban", "tax_regime", "tax_rate", "description", "billpal_account",
                  "tax_jurisdiction", "partner", "share", "auto_tax", "tax_line"):
            if s.get(k) not in (None, ""):
                meta[k] = s[k]
        for k in ("tax_rate", "auto_tax"):
            if k in meta:
                meta[k] = rate_str(parse_rate(meta[k], f"account {acct} {k}"))
        if "tax_line" in meta and not re.fullmatch(r"[A-Za-z0-9 .()-]{1,20}", str(meta["tax_line"])):
            raise LedgerError(f"account {acct}: tax_line is a form line like 24b, or none")
        if "tax_jurisdiction" in meta and not JURISDICTION_RE.fullmatch(str(meta["tax_jurisdiction"])):
            raise LedgerError(f"account {acct}: tax_jurisdiction is a code like CA, NY or NY-NYC")
        if "share" in meta:
            share = dec(meta["share"], f"account {acct} share")
            if not (0 < share <= 1 and meta.get("partner") and s.get("role") == "retained_earnings"):
                raise LedgerError(f"account {acct}: share (above 0, at most 1) goes with partner on a retained_earnings account")
            meta["share"] = format(share, "f")
        if "partner" in meta and s.get("role") not in ("retained_earnings", "private"):
            raise LedgerError(f"account {acct}: partner goes on a partner's capital (retained_earnings) or draw (private) account")
        if "iban" in meta:
            meta["iban"] = norm_iban(meta["iban"])
        d = opt_date(s.get("date"), f"account {acct} date") or book.fy_from
        out.append(ser_open(d, acct, book.currency, meta))
    return "".join(out)


def account_path(book: Book, parent: str, name: str, code: str | None = None,
                 reuse: bool = True, taken: set | None = None) -> tuple[str, bool]:
    """A free path under `parent`. With `reuse`, an account with the same name and the same
    Personenkonto number is returned instead; a name alone never merges two Personenkonten."""
    if parent not in book.accounts:
        raise LedgerError(f"parent account {parent} is not opened")
    comp = account_component(f"{code} {name}" if code else name)
    base = f"{parent}:{comp}"
    cand, i = base, 2
    while cand in book.accounts or cand in (taken or ()):
        m = book.accounts.get(cand, {}).get("meta", {})
        if (reuse and code and cand in book.accounts and norm_text(m.get("name", "")) == norm_text(name)
                and m.get("personenkonto") == code):
            return cand, True
        cand, i = f"{base}-{i}", i + 1
    return cand, False


def custom_entries(book: Book, spec: dict) -> str:
    """Rules, CSV maps, assets and confirmations from an input file, validated."""
    out = []
    for rule in spec.get("rules") or []:
        validate_rule(rule, book)
        out.append(ser_custom(book.fy_from, "rule", rule))
    for cmap in spec.get("csv_maps") or []:
        validate_csv_map(cmap)
        out.append(ser_custom(book.fy_from, "csv_map", cmap))
    for asset in spec.get("assets") or []:
        validate_asset(asset, book)
        out.append(ser_custom(book.fy_from, "asset", asset))
    for a in spec.get("allocations") or []:
        validate_allocation(a, book)
        out.append(ser_custom(book.fy_from, "allocation", a))
    for c in spec.get("confirmations") or []:
        if not isinstance(c, dict) or c.get("kind") not in CONFIRM_KINDS or not isinstance(c.get("ref"), str):
            raise LedgerError(f"confirmation {c!r}: needs kind ({sorted(CONFIRM_KINDS)}) and ref")
        out.append(ser_custom(book.fy_from, "confirm", {"kind": c["kind"], "ref": c["ref"], "note": clean_text(c.get("note"))}))
    for h in spec.get("review_holds") or []:
        if not isinstance(h, dict) or not isinstance(h.get("document"), str) or set(h) - {"document", "date", "note"}:
            raise LedgerError(f"review hold {h!r}: needs document, date and an optional note")
        hd = parse_date(h.get("date"), f"review hold {h['document']} date")
        out.append(ser_custom(min(max(hd, book.fy_from), book.fy_to), "review_hold",
                              {"document": h["document"], "date": hd.isoformat(), "note": clean_text(h.get("note"))}))
    for qn in spec.get("questions") or []:
        if not isinstance(qn, dict) or set(qn) - QUESTION_KEYS or not isinstance(qn.get("id"), str) or not qn["id"]:
            raise LedgerError(f"question {qn!r}: needs an id, and only {sorted(QUESTION_KEYS)}")
        if qn["id"] not in book.questions and not qn.get("question"):
            raise LedgerError(f"question {qn['id']}: a new question needs the question")
        out.append(ser_custom(book.fy_from, "question", {k: clean_text(v) for k, v in qn.items()}))
    return "".join(out)


def new_txn_id() -> str:
    return uuid.uuid4().hex[:12]


# ── init ─────────────────────────────────────────────────────────────────────

def load_chart(chart: str, lang: str, company: bool = False) -> list[dict]:
    path = HERE / "charts.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["chart"] == chart]
    if not rows:
        raise LedgerError(f"chart {chart!r} is not in charts.csv. Use skr03, skr04, oekr, qbo or give your own accounts")
    out = []
    for r in rows:
        if r.get("entity") and r["entity"] != ("company" if company else "owner"):
            continue
        spec = {"account": r["account"], "name": (r["name_de"] if lang == "de" else r["name_en"]) or r["name_en"]}
        for k in ("role", "code", "tax_regime", "tax_rate", "auto_tax", "tax_line"):
            if r.get(k):
                spec[k] = r[k]
        out.append(spec)
    return out


@functools.cache
def chart_rows(chart: str) -> dict:
    with (HERE / "charts.csv").open(encoding="utf-8", newline="") as fh:
        return {r["account"]: r for r in csv.DictReader(fh) if r["chart"] == chart}


def account_attr(book: Book, acct: str, key: str) -> str:
    """An account's own value, else the built-in chart's, so ledgers opened before a chart column existed get it."""
    own = book.accounts.get(acct, {}).get("meta", {}).get(key)
    if own:
        return own
    return (chart_rows(book.profile.get("chart") or "").get(acct) or {}).get(key) or ""


INIT_KEYS = {"schema", "title", "file_label", "ledger", "profile", "chart", "accounts", "skip_accounts",
             "opening", "rules", "csv_maps", "assets", "allocations", "new_business", "previous"}


def build_init(spec: dict, out_dir: Path) -> Book:
    extra = set(spec) - INIT_KEYS
    if extra:
        raise LedgerError(f"init: unknown fields {sorted(extra)}")
    lg_in = spec.get("ledger") or {}
    cur = lg_in.get("currency")
    if not isinstance(cur, str) or not re.fullmatch(r"[A-Z]{3}", cur):
        raise LedgerError("ledger currency: ISO code like EUR")
    fy_from = parse_date(lg_in.get("fiscal_year_from"), "fiscal_year_from")
    fy_to = parse_date(lg_in.get("fiscal_year_to"), "fiscal_year_to")
    profile = spec.get("profile") or {}
    errs = validate_profile(profile)
    if errs:
        raise LedgerError(errs)
    label = clean_text(spec.get("file_label") or str(fy_from.year))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise LedgerError("file_label: letters, digits, dot, dash and underscore only")
    target = Path(out_dir) / f"{label}-v1.beancount"
    if target.exists():
        raise LedgerError(f"{target} already exists")
    opening = spec.get("opening")
    ledger = {
        "schema": 1, "id": uuid.uuid4().hex, "fiscal_year_from": fy_from.isoformat(), "fiscal_year_to": fy_to.isoformat(),
        "currency": cur, "precision": int(lg_in.get("precision", precision_for(cur))),
        "opening": "booked" if opening and opening.get("lines") else ("new_business" if spec.get("new_business") else "missing"),
        "previous": spec.get("previous"), "tool": TOOL,
    }
    head = (
        f"option \"title\" {bean_string(clean_text(spec.get('title') or 'Books'))}\n"
        f"option \"operating_currency\" \"{cur}\"\n\n"
        + ser_custom(fy_from, "ledger", ledger) + ser_custom(fy_from, "profile", profile)
    )
    seed = Book(target, head.encode("utf-8"))
    accounts = []
    if spec.get("chart"):
        skip = set(spec.get("skip_accounts") or [])
        accounts = [a for a in load_chart(spec["chart"], profile.get("language", "en"), bool(profile.get("company")))
                    if a["account"] not in skip]
    override = {a.get("account"): a for a in spec.get("accounts") or []}
    accounts = [override.pop(a["account"], a) for a in accounts] + list(override.values())
    opens = open_text(seed, accounts)
    book = extend(seed, opens)
    customs = custom_entries(book, spec)
    body = opens + "\n" + customs
    if ledger["opening"] == "booked":
        book = extend(book, customs)
        body += "\n" + opening_txn(book, opening)
    text = head + body.rstrip("\n") + "\n\n" + ser_custom(fy_from, "version", {"n": 1, "parent": None, "parent_sha256": None, "note": "init"})
    data = text.encode("utf-8")
    nb = Book(target, data)
    errs = validate(nb)
    if errs:
        raise LedgerError(errs)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as fh:
        fh.write(data)
    return nb


def opening_txn(book: Book, opening: dict) -> str:
    lines = opening.get("lines")
    if not isinstance(lines, list) or not lines:
        raise LedgerError("opening lines must be a list")
    posts = []
    for i, ln in enumerate(lines):
        where = f"opening line {i + 1}"
        extra = set(ln) - {"account", "amount", "document", "document_number", "document_date", "due_date", "party"}
        if extra:
            raise LedgerError(f"{where}: unknown fields {sorted(extra)}")
        acct = ln.get("account")
        book.require_open(acct, book.fy_from)
        if acct.split(":")[0] in ("Income", "Expenses"):
            raise LedgerError(f"{where}: opening balances are balance-sheet accounts only")
        meta = {}
        if book.role_of(acct) == "suspense":
            if not opening.get("suspense_confirmed"):
                raise LedgerError(f"{where}: an opening amount on suspense needs suspense_confirmed: true")
            meta = {"suspense_confirmed": "yes"}
        if book.role_of(acct) in PARTY_ROLES and book.basis == "accrual":
            if not ln.get("document"):
                raise LedgerError(f"{where}: open items need document (one line per open invoice)")
            meta = {"document": ln["document"], "document_number": ln.get("document_number"),
                    "document_date": ln.get("document_date"), "due_date": ln.get("due_date"), "party": ln.get("party")}
        posts.append(Posting(acct, money(ln.get("amount"), book.prec, f"{where} amount"), book.currency, meta))
    total = sum((p.amount for p in posts), Decimal(0))
    if total:
        if not opening.get("suspense_confirmed"):
            raise LedgerError(
                f"opening balances are off by {total}. Fix them, or confirm with the user that the "
                "difference goes to suspense until the accountant clears it (suspense_confirmed: true)"
            )
        posts.append(Posting(book.one_role("suspense"), -total, book.currency, {"suspense_confirmed": "yes"}))
    meta = {"txn_id": new_txn_id(), "opening": "yes", "classified_by": "user", "confirmed": "user"}
    return ser_txn(book.fy_from, "*", "Opening balances", "", meta, posts, book.prec)


def validate_csv_map(cmap: dict) -> None:
    if not isinstance(cmap, dict) or not cmap.get("id"):
        raise LedgerError("csv_map needs an id")
    allowed = {"id", "delimiter", "encoding", "decimal", "thousands", "date_format", "skip_rows", "columns",
               "order", "invert_sign", "note", "header"}
    extra = set(cmap) - allowed
    if extra:
        raise LedgerError(f"csv_map {cmap['id']}: unknown fields {sorted(extra)}")
    cols = cmap.get("columns") or {}
    known = {"booking_date", "value_date", "amount", "debit", "credit", "text", "counterparty", "iban",
             "reference", "balance", "bank_id", "currency", "original_amount", "original_currency", "check_number"}
    if set(cols) - known:
        raise LedgerError(f"csv_map {cmap['id']}: unknown columns {sorted(set(cols) - known)}")
    if cmap.get("header") not in (None, True, False):
        raise LedgerError(f"csv_map {cmap['id']}: header is true or false")
    if cmap.get("header") is False and not all(c.isdigit() for spec in cols.values() for c in csv_headers(spec)):
        raise LedgerError(f"csv_map {cmap['id']}: without a header row, columns are positions (\"1\" is the first)")
    for key, spec in cols.items():
        many = key == "counterparty" and isinstance(spec, list)
        if not (isinstance(spec, str) or spec is None or (many and spec and all(isinstance(c, str) and c for c in spec))):
            raise LedgerError(f"csv_map {cmap['id']}: column {key} is one header"
                              + (", or a list of headers where the first filled one wins" if key == "counterparty" else ""))
    if not cols.get("booking_date") or not (cols.get("amount") or (cols.get("debit") and cols.get("credit"))):
        raise LedgerError(f"csv_map {cmap['id']}: needs booking_date and amount (or debit and credit)")
    if bool(cols.get("original_amount")) != bool(cols.get("original_currency")):
        raise LedgerError(f"csv_map {cmap['id']}: original_amount and original_currency go together")


def csv_headers(spec) -> list[str]:
    return [] if not spec else spec if isinstance(spec, list) else [spec]


def csv_cell(row: dict, spec) -> str:
    """The first filled cell of a column spec (one header, or a list for counterparty)."""
    return next((v for v in ((row.get(c) or "").strip() for c in csv_headers(spec)) if v), "")


ASSET_KEYS = {"id", "description", "document", "acquired", "in_service", "cost", "life_months", "residual",
              "prior_depreciation", "private_share", "method", "first_year", "asset_account",
              "depreciation_account", "accumulated_account", "private_account", "disposed", "gwg_limit", "note"}


def validate_asset(a: dict, book: Book) -> dict:
    if not isinstance(a, dict) or not a.get("id"):
        raise LedgerError("asset needs an id")
    where = f"asset {a['id']}"
    extra = set(a) - ASSET_KEYS
    if extra:
        raise LedgerError(f"{where}: unknown fields {sorted(extra)}")
    method = a.get("method", "straight_line")
    if method not in ("straight_line", "gwg"):
        raise LedgerError(f"{where}: method is straight_line or gwg")
    out = {
        "id": a["id"], "method": method,
        "in_service": parse_date(a.get("in_service"), f"{where} in_service"),
        "cost": money(a.get("cost"), book.prec, f"{where} cost"),
        "residual": money(a.get("residual", "0"), book.prec, f"{where} residual"),
        "prior": money(a.get("prior_depreciation", "0"), book.prec, f"{where} prior_depreciation"),
        "private_share": dec(a.get("private_share", "0"), f"{where} private_share"),
        "disposed": opt_date(a.get("disposed"), f"{where} disposed"),
    }
    acquired = opt_date(a.get("acquired"), f"{where} acquired")
    if out["cost"] <= 0:
        raise LedgerError(f"{where}: cost must be positive")
    if not 0 <= out["residual"] <= out["cost"]:
        raise LedgerError(f"{where}: residual is between 0 and the cost")
    if not 0 <= out["prior"] <= out["cost"] - out["residual"]:
        raise LedgerError(f"{where}: prior_depreciation is between 0 and cost minus residual")
    if acquired and acquired > out["in_service"]:
        raise LedgerError(f"{where}: in_service is on or after the acquisition date")
    if out["disposed"] and out["disposed"] < out["in_service"]:
        raise LedgerError(f"{where}: disposed is on or after in_service")
    if not 0 <= out["private_share"] < 1:
        raise LedgerError(f"{where}: private_share is between 0 and 1")
    if method == "straight_line":
        lm = a.get("life_months")
        if not isinstance(lm, int) or isinstance(lm, bool) or lm < 1:
            raise LedgerError(f"{where}: life_months is a whole number of months, like 36")
        out["life_months"] = lm
    first = a.get("first_year") or country_rule(book, out["in_service"].year).get("first_year")
    if method == "straight_line" and first not in ("monthly", "half_year", "full_year"):
        raise LedgerError(f"{where}: first_year (monthly, half_year or full_year) is needed for this country")
    out["first_year"] = first
    for k in ("asset_account", "depreciation_account"):
        if not a.get(k) or a[k] not in book.accounts:
            raise LedgerError(f"{where}: {k} {a.get(k)} is not opened")
    out["asset_account"], out["depreciation_account"] = a["asset_account"], a["depreciation_account"]
    acc = a.get("accumulated_account")
    if acc is None and book.accounts_with_role("accumulated_depreciation"):
        raise LedgerError(f"{where}: the chart books depreciation indirectly. Give accumulated_account")
    if acc is not None and acc not in book.accounts:
        raise LedgerError(f"{where}: accumulated_account {acc} is not opened")
    out["accumulated_account"] = acc
    if out["private_share"] and (not a.get("private_account") or a["private_account"] not in book.accounts):
        raise LedgerError(f"{where}: private_share needs private_account")
    out["private_account"] = a.get("private_account")
    if method == "gwg":
        limit = a.get("gwg_limit") or country_rule(book, out["in_service"].year).get("gwg_limit")
        if limit is None:
            raise LedgerError(f"{where}: no GWG limit known for this country and year. Give gwg_limit")
        if out["cost"] > dec(limit, f"{where} gwg_limit"):
            raise LedgerError(f"{where}: cost {out['cost']} is above the GWG limit {limit}. Use straight_line")
    return out


def country_rule(book: Book, year: int) -> dict:
    for r in COUNTRY_RULES.get(book.profile.get("country", ""), []):
        if r["from"] <= year and (r["to"] is None or year <= r["to"]):
            return r
    return {}


# ── journal / reverse / close-period ─────────────────────────────────────────

JOURNAL_KEYS = {"schema", "entries", "accounts", "rules", "csv_maps", "assets", "profile", "confirmations",
                "review_holds", "allocations", "questions", "note"}
ENTRY_KEYS = {"date", "payee", "narration", "lines", "meta", "confirmed", "classified_by", "document",
              "documents", "no_document", "document_review", "opening"}


def entry_meta(entry: dict, where: str) -> dict:
    meta = {"txn_id": new_txn_id()}
    confirmed = entry.get("confirmed", "no")
    if confirmed not in ("user", "rule", "document", "no"):
        raise LedgerError(f"{where}: confirmed is user, rule, document or no")
    meta["classified_by"] = clean_text(entry.get("classified_by") or "agent")
    meta["confirmed"] = confirmed
    docs = entry.get("documents") or ([entry["document"]] if entry.get("document") else [])
    if len(docs) == 1:
        meta["document"] = docs[0]
    elif docs:
        meta["documents_json"] = docs
    if entry.get("no_document"):
        meta["no_document"] = entry["no_document"]
    if entry.get("document_review") not in (None, "open"):
        raise LedgerError(f"{where}: document_review is open or left out")
    if entry.get("document_review"):
        meta["document_review"] = "open"
    if entry.get("opening"):
        meta["opening"] = "yes"
    for k, v in (entry.get("meta") or {}).items():
        if k in RESERVED_META or k in meta:
            raise LedgerError(f"{where}: meta key {k} is reserved")
        meta_value(k, v)
        meta[k] = v
    return meta


def build_journal(book: Book, spec: dict) -> Book:
    extra = set(spec) - JOURNAL_KEYS
    if extra:
        raise LedgerError(f"journal: unknown fields {sorted(extra)}")
    body = ""
    if spec.get("profile"):
        profile = {**book.profile, **spec["profile"]}
        errs = validate_profile(profile)
        if errs:
            raise LedgerError(errs)
        moved = sorted(k for k in PROFILE_FROZEN if profile.get(k) != book.profile.get(k))
        if book.closed_through and moved:
            raise LedgerError(f"profile {', '.join(moved)} cannot change after the books were closed through "
                              f"{book.closed_through}. Start the change with the next fiscal year (rollover)")
        body += ser_custom(book.fy_from, "profile", spec["profile"])
    opens = open_text(book, spec.get("accounts"))
    work = extend(book, body + opens)
    customs = custom_entries(work, spec)
    work = extend(work, customs)
    body += opens + customs
    done = allocated_rows(book)
    for i, entry in enumerate(spec.get("entries") or []):
        where = f"entry {i + 1}"
        extra = set(entry) - ENTRY_KEYS
        if extra:
            raise LedgerError(f"{where}: unknown fields {sorted(extra)}")
        row = (entry.get("meta") or {}).get("allocation_row")
        if row and row in done:
            raise LedgerError(f"{where}: this private payment is already allocated. Run allocate again for a fresh proposal")
        done.add(row)
        d = parse_date(entry.get("date"), f"{where} date")
        if book.closed_through and d <= book.closed_through:
            raise LedgerError(f"{where}: {d} is in a closed period (through {book.closed_through}). Book a correction later")
        posts = expand_lines(work, user_tax(entry.get("lines"), entry.get("confirmed")), Decimal(0), d, where,
                             require_tax=True)
        meta = entry_meta(entry, where)
        flag = "!" if meta["confirmed"] == "no" else "*"
        body += "\n" + ser_txn(d, flag, entry.get("payee") or "", entry.get("narration") or "", meta, posts, book.prec)
    return write_next(book, body, clean_text(spec.get("note") or "journal"))


def build_reverse(book: Book, txn_id: str, d: date, reason: str, rebook: dict | None) -> Book:
    orig = book.txn_by_id.get(txn_id)
    if orig is None:
        raise LedgerError(f"no transaction with txn_id {txn_id}")
    if txn_id in book.reversed:
        raise LedgerError(f"{txn_id} is already reversed")
    if orig.meta.get("reverses"):
        raise LedgerError(f"{txn_id} is itself a reversal")
    if book.closed_through and d <= book.closed_through:
        raise LedgerError(f"{d} is in a closed period (through {book.closed_through})")
    if d < orig.date:
        raise LedgerError("the reversal date cannot be before the original booking")
    rev_posts = []
    for p in orig.postings:
        meta = {k: v for k, v in p.meta.items() if k in ("document", "tax_part", "tax_regime", "tax_rate", "tax_type",
                                                          "tax_direction", "tax_point", "asset", "recoverable",
                                                          "report_codes_json")}
        if "report_codes_json" in meta:
            meta["report_codes_json"] = json.loads(meta["report_codes_json"])
        if "tax_point" in meta:
            meta["tax_point"] = d.isoformat()
        if "tax_base" in p.meta:
            meta.update({"tax_base": fmt(-Decimal(p.meta["tax_base"]), book.prec),
                         "tax_amount": fmt(-Decimal(p.meta.get("tax_amount", "0")), book.prec),
                         "tax_source": p.meta.get("tax_source", "agent"), "tax_confirmed": "yes"})
        if "allocations_json" in p.meta:
            allocs = json.loads(p.meta["allocations_json"])
            meta["allocations_json"] = [{"document": a["document"], "amount": fmt(-Decimal(a["amount"]), book.prec)} for a in allocs]
        rev_posts.append(Posting(p.account, -p.amount, p.currency, meta))
    meta = {"txn_id": new_txn_id(), "reverses": txn_id, "reason": reason, "classified_by": "user", "confirmed": "user"}
    body = ser_txn(d, "*", orig.payee, f"Reversal: {orig.narration}", meta, rev_posts, book.prec)
    if rebook:
        lines = rebook.get("lines")
        money_posts = [p for p in orig.postings if book.role_of(p.account) in MONEY_ROLES]
        if not money_posts:
            raise LedgerError("rebook works for bank, cash and card bookings; use journal for others")
        fixed = sum((p.amount for p in money_posts), Decimal(0))
        posts = expand_lines(book, user_tax(lines, rebook.get("confirmed")), fixed, d, "rebook", require_tax=True,
                             cash_rows=True)
        keep = {k: v for k, v in orig.meta.items() if k in ("source_statement", "source_row", "row_key", "bank_id",
                                                             "value_date", "counterparty", "iban", "reference", "fx_json")}
        rmeta = {**entry_meta({**rebook, "lines": None, "date": None}, "rebook"), **keep, "rebooks": txn_id}
        rmeta.pop("lines", None)
        mposts = [Posting(p.account, p.amount, p.currency, {}) for p in money_posts]
        body += "\n" + ser_txn(d, "*" if rmeta["confirmed"] != "no" else "!", rebook.get("payee") or orig.payee,
                               rebook.get("narration") or orig.narration, rmeta, mposts + posts, book.prec)
    return write_next(book, body, f"reverse {txn_id}: {reason}")


# ── Readiness ────────────────────────────────────────────────────────────────

def scan_folder(folder: Path, ledger_id: str) -> dict:
    """Versions of this ledger in a folder, by header scan. Forks show up only if both files are here."""
    found = defaultdict(list)
    pend = []
    for p in sorted(folder.iterdir()) if folder.exists() else []:
        if p.name.endswith((".beancount", ".beancount.txt")):
            try:
                raw = p.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            m = re.search(r'custom "ledger" "((?:[^"\\]|\\.)*)"', text)
            if not m:
                continue
            try:
                lid = json.loads(unescape(m[1], 0)).get("id")
            except (json.JSONDecodeError, LedgerError):
                continue
            if lid != ledger_id:
                continue
            versions = [json.loads(unescape(v, 0)) for v in re.findall(r'custom "version" "((?:[^"\\]|\\.)*)"', text)]
            n = max((int(v.get("n", 0)) for v in versions), default=0)
            parent = next((v.get("parent_sha256") for v in versions if int(v.get("n", 0)) == n), None)
            found[n].append({"file": p.name, "sha256": sha256_bytes(raw), "parent_sha256": parent})
        elif p.name.endswith(".pending.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("ledger_id") == ledger_id and not data.get("committed_in"):
                pend.append(p.name)
    forks = []
    by_sha = {f["sha256"]: (n, f["file"]) for n, fs in found.items() for f in fs}
    for n, files in found.items():
        if len({f["sha256"] for f in files}) > 1:
            forks.append(f"version {n} exists as different files: {[f['file'] for f in files]}")
        for f in files:
            if n > 1 and f["parent_sha256"] and found.get(n - 1) and f["parent_sha256"] not in by_sha:
                forks.append(f"{f['file']} was written from a version {n - 1} that is not in the folder")
    latest = max(found) if found else None
    return {"versions": {n: [f["file"] for f in fs] for n, fs in sorted(found.items())},
            "latest": found[latest][0]["file"] if latest is not None and len(found[latest]) == 1 else None,
            "forks": forks, "pending": pend}


def coverage_gaps(book: Book, account: str, start: date, end: date) -> list[str]:
    spans = sorted((parse_date(s["from"]), parse_date(s["to"])) for s in book.statements if s.get("account") == account)
    gaps, cursor = [], start
    for a, b in spans:
        if b < cursor:
            continue
        if a > cursor:
            gaps.append(f"{cursor} to {min(a - timedelta(days=1), end)}")
        cursor = max(cursor, b + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append(f"{cursor} to {end}")
    return gaps


def has_document(t: Entry) -> bool:
    if t.meta.get("document") or t.meta.get("documents_json") or t.meta.get("no_document") or t.meta.get("opening"):
        return True
    for p in t.postings:
        if p.meta.get("document"):
            return True
        if p.meta.get("allocations_json") and any(a.get("document") for a in json.loads(p.meta["allocations_json"])):
            return True
    return False


def money_amount(book: Book, t: Entry) -> Decimal:
    return sum((p.amount for p in t.postings if book.role_of(p.account) in MONEY_ROLES), Decimal(0))


def missing_receipt_txns(book: Book, scope) -> list[Entry]:
    """Money movements with neither a document nor a reason, own transfers excepted."""
    return [t for t in scope
            if any(book.role_of(p.account) in MONEY_ROLES for p in t.postings)
            and not all(book.role_of(p.account) in TRANSFER_ROLES for p in t.postings)
            and not has_document(t)]


def readiness(book: Book, through: date | None, year_end: bool = False, folder: Path | None = None) -> dict:
    through = book.fy_to if year_end else (through or book.fy_to)
    fails, warns = [], []

    def fail(code, message, items=None):
        fails.append({"code": code, "message": message, "items": (items or [])[:50], "count": len(items or [])})

    def warn(code, message, items=None):
        warns.append({"code": code, "message": message, "items": (items or [])[:50], "count": len(items or [])})

    errs = validate(book)
    if errs:
        fail("invalid", "The ledger has errors", errs)
    scope = [t for t in book.live_txns() if t.date <= through]
    unconf = [f"{t.date} {t.payee} ({t.meta.get('txn_id')})" for t in scope
              if t.meta.get("confirmed") == "no" and not book.confirmed("booking", t.meta.get("txn_id", ""))]
    if unconf:
        fail("unconfirmed", "Bookings the user has not confirmed", unconf)
    untax = [f"{t.date} {t.payee} ({t.meta.get('txn_id')})" for t in scope
             if any(p.meta.get("tax_confirmed") == "no" for p in t.postings)
             and not book.confirmed("tax", t.meta.get("txn_id", ""))]
    if untax:
        fail("tax_unconfirmed", "Tax treatments the user has not confirmed", untax)
    review = {t.meta.get("document", t.meta.get("txn_id")) for t in scope if t.meta.get("document_review") == "open"
              and not book.confirmed("document_review", t.meta.get("document") or t.meta.get("txn_id", ""))}
    booked = book.documents_in_ledger()
    for h in book.holds:
        if (h["document"] not in booked and parse_date(h["date"], "review hold date") <= through
                and not book.confirmed("document_review", h["document"])):
            review.add(h["document"])
    if review:
        fail("review_open", "Documents still in Billpal Review", sorted(review))
    for acct in book.accounts_with_role("suspense"):
        bal = book.balance(acct, through=through)
        if bal != 0:
            confirmed_open = sum((p.amount for t in scope if t.meta.get("opening") for p in t.postings
                                  if p.account == acct and p.meta.get("suspense_confirmed") == "yes"), Decimal(0))
            note = f", {confirmed_open} of it the opening difference the user confirmed" if confirmed_open else ""
            fail("suspense", f"{acct} holds {bal}{note}. Every amount needs a real account", [acct])
    by_doc = {a.get("document"): aid for aid, a in book.assets.items() if a.get("document")}

    def asset_of(t, p):
        docs = [p.meta.get("asset"), by_doc.get(p.meta.get("document") or t.meta.get("document"))]
        # a credit note reduces the cost of the asset on the document it applies to
        docs += [by_doc.get(a.get("document")) for q in t.postings if q.meta.get("allocations_json")
                 for a in json.loads(q.meta["allocations_json"])]
        return next((x for x in docs if x), None)

    def disposal(aid, t):
        gone = (book.assets.get(aid) or {}).get("disposed")
        return bool(gone) and t.date >= parse_date(gone, f"asset {aid} disposed")

    fixed = [(aid, t, p) for t in scope if not t.meta.get("opening") and t.meta.get("depreciation") != "yes"
             for p in t.postings if book.role_of(p.account) == "fixed_asset" and p.amount != 0
             for aid in [asset_of(t, p)] if not disposal(aid, t)]
    unreg = [f"{t.date} {t.payee} {p.account} {p.amount}" for aid, t, p in fixed if aid not in book.assets]
    cost = defaultdict(Decimal)
    for aid, _, p in fixed:
        if aid in book.assets:
            cost[aid] += p.amount
    off = []
    for aid, booked_cost in sorted(cost.items()):
        try:
            rec = validate_asset(book.assets[aid], book)["cost"]
        except LedgerError as exc:
            off.append(f"{aid}: {exc.messages[0]}")
            continue
        if booked_cost != rec:
            off.append(f"{aid}: booked {booked_cost}, the asset record says {rec}")
    for code, message, items in (("assets_unregistered", "Fixed-asset bookings without an asset record", unreg),
                                 ("asset_cost", "Fixed-asset bookings that do not match the asset record", off)):
        if items:
            (fail if year_end else warn)(code, message, items)
    gaps = []
    used = {p.account for t in scope for p in t.postings}
    for acct in book.money_accounts():
        if acct not in used:
            continue
        info = book.accounts.get(acct) or {}
        start = max(book.fy_from, info.get("open") or book.fy_from)
        end = min(through, info["close"]) if info.get("close") else through
        if start <= end:
            gaps += [f"{acct}: {g}" for g in coverage_gaps(book, acct, start, end)]
    if gaps:
        fail("statements_missing", "Periods without a statement", gaps)
    if book.ledger.get("opening") == "missing" and not any(t.meta.get("opening") for t in book.txns):
        fail("opening_missing", "Opening balances are not booked")
    if book.basis == "accrual":
        _, unalloc, _ = compute_items(book, through)
        if unalloc:
            fail("unallocated", "Payments on Personenkonten not assigned to a document",
                 [f"{a}: {v}" for a, v in unalloc.items()])
    folder = folder or book.path.parent
    scan = scan_folder(folder, book.ledger_id)
    if scan["forks"]:
        fail("fork", "Two different versions exist. Merge them before going on", scan["forks"])
    if scan["pending"]:
        fail("pending", "Staged files that were never committed", scan["pending"])
    money = lambda t: money_amount(book, t)
    no_doc = missing_receipt_txns(book, scope)
    if no_doc:
        warn("missing_receipts", "Bookings without a receipt or invoice",
             [f"{t.date} {t.payee} {money(t)} ({t.meta.get('txn_id')})" for t in no_doc])
    by_party = defaultdict(lambda: [0, Decimal(0)])
    for t in no_doc:
        by_party[t.payee][0] += 1
        by_party[t.payee][1] += money(t)
    booked = defaultdict(list)
    for t in scope:
        if t.meta.get("allocation"):
            booked[t.meta["allocation"]].append(t.date)
    alloc_gaps = []
    for aid, a in book.allocations.items():
        if a.get("every") and not a.get("disabled"):
            lo = max(book.fy_from, opt_date(a.get("from")) or book.fy_from)
            hi = min(through, opt_date(a.get("to")) or through)
            alloc_gaps += [f"{aid}: {period_key(s, a['every'])}" for s, e in full_periods(lo, hi, a["every"])
                           if not any(s <= d <= e for d in booked[aid])]
    if alloc_gaps:
        warn("allocation_missing", "Recurring costs with a period where nothing was allocated. "
                                   "Run allocate on the private statement, or note why", alloc_gaps)
    for code, message, items in review_flags(book, scope):
        if items:
            warn(code, message, items)
    gap = vat_payment_gap(book, through)
    if gap:
        warn("vat_payments", gap)
    open_q = [f"{qn['id']}: {qn.get('question')}" for qn in book.questions.values() if not qn.get("answer")]
    if open_q:
        warn("open_questions", "Questions for the user or the accountant that are still open", open_q)
    flat = {t.meta["flat_rate"] for t in scope if t.meta.get("flat_rate")}
    both = sorted(flat & {(book.allocations.get(aid) or {}).get("topic") for aid in booked})
    if both:
        warn("flat_rate_and_allocation", "The year has a flat rate and an allocated cost share for the same topic. "
                                         "These are usually either/or (countries.md): ask the tax advisor", both)
    if year_end:
        todo = depreciation_plan(book)
        open_dep = [f"{r['asset']}: {r['to_book']}" for r in todo if r["to_book"] != 0]
        if open_dep:
            fail("depreciation", "Depreciation for the year is not booked", open_dep)
        if book.closed_through != book.fy_to:
            fail("not_closed", f"Close the year first (close-period --through {book.fy_to})")
        try:
            result_split(book)
        except LedgerError as exc:
            fail("no_result_account", str(exc))
    return {"ready": not fails, "through": through.isoformat(), "year_end": year_end,
            "fails": fails, "warnings": warns, "missing_receipts_count": len(no_doc),
            "missing_receipts_by_party": [{"payee": k, "count": c, "amount": a} for k, (c, a) in
                                          sorted(by_party.items(), key=lambda kv: (-abs(kv[1][1]), kv[0]))]}


REVIEW_CODES = {"double_charge", "price_increase", "above_asset_limit", "contractors_1099"}
ASSETISH = re.compile(r"Equipment|Hardware|Computer|Office|Supplies|Materials|Tools|Furniture|Software|Other")


def review_flags(book: Book, scope: list) -> list[tuple[str, str, list]]:
    """What an accountant would ask about. Warnings only: each one can be fine."""
    cur, out = book.currency, []
    pays = []
    for t in scope:
        money = [p for p in t.postings if book.role_of(p.account) in MONEY_ROLES]
        if not t.meta.get("opening") and t.payee and len(money) == 1 and money[0].amount < 0:
            pays.append((t, norm_text(t.payee), -money[0].amount, book.role_of(money[0].account)))
    pays.sort(key=lambda x: (x[1], x[0].date))
    dup = [f"{a[0].date} and {b[0].date}: {a[0].payee} {a[2]}" for a, b in zip(pays, pays[1:])
           if a[1] == b[1] and a[2] == b[2] and (b[0].date - a[0].date).days <= 3]
    out.append(("double_charge", "The same payee and amount twice within three days: two purchases, or a charge to reclaim?", dup))
    # ponytail: a step up after two equal payments; the latest such step per payee is shown.
    by_payee = defaultdict(list)
    for t, key, amount, _ in pays:
        by_payee[key].append((t, amount))
    up = []
    for xs in by_payee.values():
        steps = [i for i in range(2, len(xs)) if xs[i][1] > xs[i - 1][1] == xs[i - 2][1]]
        if steps:
            i = steps[-1]
            up.append(f"{xs[i][0].payee}: {xs[i - 1][1]} to {xs[i][1]} {cur} on {xs[i][0].date}")
    out.append(("price_increase", "A recurring payment that went up", up))
    limit = country_rule(book, book.fy_from.year).get("gwg_limit")
    if limit:
        # ponytail: only accounts whose name suggests equipment; a big "Repairs" bill is not flagged.
        big = [f"{t.date} {t.payee} {p.account} {base}" for t in scope if not t.meta.get("opening")
               for p in t.postings if p.account.startswith("Expenses:") and ASSETISH.search(p.account) and not p.meta.get("asset")
               for base in [dec(p.meta["tax_base"]) if book.vat_regular and "tax_base" in p.meta else p.amount]
               if base > dec(limit)]
        out.append(("above_asset_limit", f"Expenses above the {limit} {cur} limit for low-value assets: "
                                         "equipment that lasts several years is an asset", big))
    if book.profile.get("country") == "US":
        floor = Decimal(2000 if book.fy_from.year >= 2026 else 600)
        paid = defaultdict(Decimal)
        for t, key, amount, role in pays:
            if role in ("bank", "cash") and any(account_attr(book, p.account, "tax_line") == "11" or "Contractors" in p.account
                                                for p in t.postings):
                paid[t.payee] += amount
        out.append(("contractors_1099", f"Contractors paid {floor} USD or more by bank transfer or check: 1099-NEC "
                                        "candidates (ask for a W-9; card and PayPal payments are reported on 1099-K)",
                    [f"{k}: {v}" for k, v in sorted(paid.items()) if v >= floor]))
    return out


def period_due(book: Book, key: str, by: str) -> str | None:
    rule = VAT_DUE.get(book.profile.get("country") or "")
    if not rule or by not in ("month", "quarter"):
        return None
    end_month = int(key[5:7]) if by == "month" else int(key[-1]) * 3
    months = rule[0] + (1 if book.profile.get("vat_extension") else 0)
    first = add_months(date(int(key[:4]), end_month, 1), months)
    d = date(first.year, first.month, rule[1])
    # ponytail: moves past weekends only; a public holiday on the due date is not known here.
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def vat_payment_gap(book: Book, through: date) -> str | None:
    """VAT payable for the periods due by `through` against the payments to the tax office account."""
    if not book.vat_regular or not VAT_DUE.get(book.profile.get("country") or ""):
        return None
    # Payments go to the tax office account or straight to a VAT account (lines without tax_part).
    settles = lambda p: p.account == "Liabilities:Tax-Office" or (
        book.role_of(p.account) in ("vat_in", "vat_out") and not p.meta.get("tax_part"))
    totals = [t for t in vat_schedule(book, book.fy_from, book.fy_to)["totals"] if t["due"]]
    opening, paid = Decimal(0), Decimal(0)
    for t in book.live_txns():
        if t.date > through:
            continue
        if t.meta.get("opening"):
            opening += sum((p.amount for p in t.postings if settles(p)), Decimal(0))
        elif any(book.role_of(p.account) in MONEY_ROLES for p in t.postings):
            paid += sum((p.amount for p in t.postings if settles(p)), Decimal(0))
    owed = sum((t["payable"] for t in totals if parse_date(t["due"]) <= through), Decimal(0)) - opening
    # payments made early for a period that has ended but is not due yet are fine too
    fine, running = {owed}, owed
    for t in totals:
        if parse_date(t["due"]) > through and parse_date(t["due"]) - timedelta(days=75) <= through:
            running += t["payable"]
            fine.add(running)
    if paid in fine:
        return None
    return (f"VAT due by {through} is {owed} {book.currency}, last year's balance included; payments to the tax office are "
            f"{paid}. A gap usually means costs paid from another account are missing, or a payment is booked elsewhere")


def full_periods(lo: date, hi: date, every: str) -> list[tuple[date, date]]:
    """Calendar months, quarters or years from lo that ended by hi, clipped to start at lo."""
    step = {"month": 1, "quarter": 3, "year": 12}[every]
    start = date(lo.year, (lo.month - 1) // step * step + 1, 1) if step < 12 else date(lo.year, 1, 1)
    out = []
    while (end := add_months(start, step) - timedelta(days=1)) <= hi:
        out.append((max(start, lo), end))
        start = add_months(start, step)
    return out


def build_close(book: Book, through: date) -> Book:
    if not book.fy_from <= through <= book.fy_to:
        raise LedgerError(f"{through} is outside the fiscal year")
    if book.closed_through and through <= book.closed_through:
        raise LedgerError(f"already closed through {book.closed_through}")
    # The final close is the last chance to fix the year, so it runs the year-end checks except the close itself.
    rep = readiness(book, through, year_end=through == book.fy_to)
    rep["fails"] = [f for f in rep["fails"] if f["code"] != "not_closed"]
    if rep["fails"]:
        raise LedgerError([f"{f['code']}: {f['message']} {f['items'][:5]}" for f in rep["fails"]])
    return write_next(book, ser_custom(through, "closed", {"through": through.isoformat()}), f"closed through {through}")


def status(book: Book) -> dict:
    scan = scan_folder(book.path.parent, book.ledger_id)
    accts = []
    for a in book.money_accounts():
        spans = [s for s in book.statements if s.get("account") == a]
        accts.append({"account": a, "role": book.role_of(a), "balance": fmt(own_balance(book, a), book.prec),
                      "statements_through": max((s["to"] for s in spans), default=None)})
    return {
        "file": book.path.name, "version": book.version, "sha256": book.sha, "ledger_id": book.ledger_id,
        "fiscal_year": [book.fy_from.isoformat(), book.fy_to.isoformat()], "currency": book.currency,
        "profile": book.profile, "transactions": len(book.txns),
        "last_booking": max((t.date for t in book.txns), default=None),
        "closed_through": book.closed_through, "money_accounts": accts,
        "newest_in_folder": scan["latest"], "is_newest": scan["latest"] in (None, book.path.name),
        "forks": scan["forks"], "pending": scan["pending"], "rules": len(book.rules), "assets": len(book.assets),
        "open_questions": [qn for qn in book.questions.values() if not qn.get("answer")],
    }


# ── Statements: CAMT.053 and CSV into statement/1 ────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, path: str):
    for part in path.split("/"):
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == part), None)
    return el


def _findall(el, name: str):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _text(el, path: str) -> str:
    found = _find(el, path)
    return (found.text or "").strip() if found is not None else ""


def _camt_amount(el) -> Decimal:
    amt = dec(_text(el, "Amt"), "CAMT amount")
    return -amt if _text(el, "CdtDbtInd") == "DBIT" else amt


def _camt_date(el, path: str) -> date | None:
    value = _text(el, f"{path}/Dt") or _text(el, f"{path}/DtTm")[:10]
    return parse_date(value, f"CAMT {path}") if value else None


# ISO 20022 bank transaction family codes that prove how money moved.
# Card codes are left out: Billpal tells debit from credit cards, and the bank code does not.
CAMT_METHODS = {"RDDT": "direct_debit", "IDDT": "direct_debit", "ICDT": "bank_transfer", "RCDT": "bank_transfer"}
# ponytail: text evidence is the German and English wording banks print. Other languages fall back
# to no method, which the write-back leaves untouched.
DIRECT_DEBIT_TEXT = re.compile(r"\b(lastschrift|basislastschrift|direct debit|mandat\w*|mref)\b")


def row_method(r: dict) -> str | None:
    """How a statement row moved money, only when the statement proves it."""
    if r.get("method"):
        return r["method"]
    return "direct_debit" if DIRECT_DEBIT_TEXT.search(norm_text(f"{r.get('text') or ''} {r.get('reference') or ''}")) else None


def from_camt(data: bytes, book: Book) -> dict:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise LedgerError("CAMT file contains a DOCTYPE or ENTITY declaration. Refusing to parse it")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise LedgerError(f"not a valid XML file ({exc})") from exc
    container = next((c for c in root.iter() if _local(c.tag) in ("BkToCstmrStmt", "BkToCstmrAcctRpt")), None)
    if container is None:
        raise LedgerError("no CAMT.053 statement in this file")
    msg_id = _text(container, "GrpHdr/MsgId")
    statements, skipped = [], 0
    for st in [c for c in container if _local(c.tag) in ("Stmt", "Rpt")]:
        iban = norm_iban(_text(st, "Acct/Id/IBAN"))
        acct = next((a for a, i in book.accounts.items() if i["meta"].get("iban") == iban), None) if iban else None
        opening = closing = None
        start = end = None
        for bal in _findall(st, "Bal"):
            code = _text(bal, "Tp/CdOrPrtry/Cd")
            amount = _camt_amount(bal)
            d = _camt_date(bal, "Dt")
            if code in ("OPBD", "PRCD"):
                opening = amount
                if d:
                    start = d + timedelta(days=1) if code == "PRCD" else d
            elif code in ("CLBD",):
                closing = amount
                end = d
        if _text(st, "FrToDt/FrDtTm"):
            start = parse_date(_text(st, "FrToDt/FrDtTm")[:10])
        if _text(st, "FrToDt/ToDtTm"):
            end = parse_date(_text(st, "FrToDt/ToDtTm")[:10])
        rows = []
        for n in _findall(st, "Ntry"):
            status_code = _text(n, "Sts/Cd") or _text(n, "Sts")
            if status_code and status_code != "BOOK":
                skipped += 1
                continue
            amount = _camt_amount(n)
            tx = [c for d in _findall(n, "NtryDtls") for c in _findall(d, "TxDtls")]
            credit = amount > 0
            party = iban_cp = ""
            refs, texts = [], []
            fx = None
            method = CAMT_METHODS.get(_text(n, "BkTxCd/Domn/Fmly/Cd"))
            for t in tx:
                if _text(t, "Refs/MndtId"):
                    method = "direct_debit"
                method = CAMT_METHODS.get(_text(t, "BkTxCd/Domn/Fmly/Cd")) or method
                rp = _find(t, "RltdPties")
                who = "Dbtr" if credit else "Cdtr"
                party = party or _text(rp, f"{who}/Nm") or _text(rp, f"{who}/Pty/Nm")
                iban_cp = iban_cp or norm_iban(_text(rp, f"{who}Acct/Id/IBAN"))
                texts += [u.text.strip() for u in _findall(_find(t, "RmtInf"), "Ustrd") if u.text]
                ref = _text(t, "Refs/EndToEndId")
                if ref and ref != "NOTPROVIDED":
                    refs.append(ref)
                inst = _find(t, "AmtDtls/InstdAmt/Amt")
                rate = _text(t, "AmtDtls/InstdAmt/CcyXchg/XchgRate") or _text(t, "AmtDtls/TxAmt/CcyXchg/XchgRate")
                if inst is not None and inst.get("Ccy") and inst.get("Ccy") != book.currency:
                    fx = {"amount": (inst.text or "").strip(), "currency": inst.get("Ccy"), "rate": rate or None}
            texts.append(_text(n, "AddtlNtryInf"))
            rows.append({
                "booking_date": (_camt_date(n, "BookgDt") or _camt_date(n, "ValDt")).isoformat(),
                "value_date": (_camt_date(n, "ValDt") or _camt_date(n, "BookgDt")).isoformat(),
                "amount": format(amount, "f"), "text": clean_text(" ".join(x for x in texts if x)),
                "counterparty": clean_text(party), "iban": iban_cp, "reference": " ".join(refs),
                "bank_id": _text(n, "AcctSvcrRef") or _text(n, "NtryRef") or None, "fx": fx, "method": method,
            })
        dates = [r["booking_date"] for r in rows]
        count = _text(st, "TxsSummry/TtlNtries/NbOfNtries")
        statements.append({
            "source_id": f"camt:{msg_id}:{_text(st, 'Id')}", "account": acct, "iban": iban,
            "currency": _text(st, "Acct/Ccy") or book.currency, "kind": "bank",
            "from": (start.isoformat() if start else min(dates, default=None)),
            "to": (end.isoformat() if end else max(dates, default=None)),
            "opening": None if opening is None else format(opening, "f"),
            "closing": None if closing is None else format(closing, "f"),
            "extraction": "camt", "count": int(count) if count.isdigit() else None, "rows": rows,
        })
    return {"schema": "statement/1", "statements": statements, "skipped_not_booked": skipped}


CURRENCY_MARKS = re.compile(r"[$€£¥]|^[A-Z]{3}|[A-Z]{3}$")


def _csv_amount(text: str, cmap: dict) -> Decimal:
    s = (text or "").strip().replace("\u00a0", "").replace(" ", "").replace("\u2212", "-")
    s = CURRENCY_MARKS.sub("", s)
    if not s:
        return Decimal(0)
    neg = s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()").rstrip("-")
    if cmap.get("thousands"):
        s = s.replace(cmap["thousands"], "")
    elif cmap.get("decimal", ".") == "." and re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    s = s.replace(cmap.get("decimal", "."), ".")
    value = dec(s, f"CSV amount {text!r}")
    return -value if neg else value


def from_csv(data: bytes, book: Book, map_id: str, account: str, start: str, end: str,
             opening: str | None, closing: str | None, source_id: str | None) -> dict:
    cmap = book.csv_maps.get(map_id)
    if cmap is None:
        raise LedgerError(f"no csv_map {map_id!r} in the ledger. Add one with journal first")
    text = data.decode(cmap.get("encoding", "utf-8-sig"))
    lines = text.splitlines()[int(cmap.get("skip_rows", 0)):]
    delim = cmap.get("delimiter", ",")
    names = None
    if cmap.get("header") is False:
        width = max((len(r) for r in csv.reader(lines, delimiter=delim)), default=0)
        names = [str(i + 1) for i in range(width)]
    reader = csv.DictReader(lines, fieldnames=names, delimiter=delim)
    cols = cmap["columns"]
    missing = [c for spec in cols.values() for c in csv_headers(spec) if c not in (reader.fieldnames or [])]
    if missing:
        raise LedgerError(f"CSV is missing columns {missing}. Columns found: {reader.fieldnames}")
    fmt_in = cmap.get("date_format", "%Y-%m-%d")
    rows = []
    for n, r in enumerate(reader, 1):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue

        def get(key):
            return csv_cell(r, cols.get(key))

        def day(key):
            v = get(key)
            if not v:
                return None
            try:
                return datetime.strptime(v, fmt_in).date().isoformat()
            except ValueError as exc:
                raise LedgerError(f"CSV row {n}: {key} {v!r} does not match {fmt_in}") from exc

        if cols.get("amount"):
            amount = _csv_amount(get("amount"), cmap)
        else:
            amount = _csv_amount(get("credit"), cmap) - abs(_csv_amount(get("debit"), cmap))
        if cmap.get("invert_sign"):
            amount = -amount
        if get("currency") and get("currency").upper() != book.currency:
            raise LedgerError(f"CSV row {n}: currency {get('currency')}, ledger is {book.currency}")
        # The bank's own rate column is ignored: banks print it either way round (Wise: USD per EUR).
        fx = None
        orig_cur = get("original_currency").upper()
        orig = _csv_amount(get("original_amount"), cmap) if orig_cur and orig_cur != book.currency else Decimal(0)
        if orig and amount:
            fx = {"amount": format(abs(orig), "f"), "currency": orig_cur,
                  "rate": format(q(abs(amount) / abs(orig), 8).normalize(), "f")}
        bal = get("balance")
        rows.append({
            "booking_date": day("booking_date"), "value_date": day("value_date"), "amount": format(amount, "f"),
            "text": clean_text(get("text")), "counterparty": clean_text(get("counterparty")),
            "iban": norm_iban(get("iban")), "reference": clean_text(get("reference")),
            "bank_id": get("bank_id") or None,
            "balance": format(_csv_amount(bal, cmap), "f") if bal else None,
            **({"fx": fx} if fx else {}),
            **({"check_number": get("check_number")} if get("check_number") else {}),
        })
    if cmap.get("order") == "desc":
        rows.reverse()
    lo, hi = parse_date(start, "--from").isoformat(), parse_date(end, "--to").isoformat()
    rows = [r for r in rows if not r["booking_date"] or lo <= r["booking_date"] <= hi]
    if rows and all(r["balance"] for r in rows):
        col_open, col_close = dec(rows[0]["balance"]) - dec(rows[0]["amount"]), dec(rows[-1]["balance"])
        for name, given, col in (("--opening", opening, col_open), ("--closing", closing, col_close)):
            if given not in (None, "") and dec(given) != col:
                raise LedgerError(f"{name} {given} differs from the balance column ({col}) for this period. Leave {name} out")
        opening, closing = format(col_open, "f"), format(col_close, "f")
    sid = source_id or "csv:" + sha256_bytes(data)[:16]
    return {"schema": "statement/1", "statements": [{
        "source_id": sid, "account": account, "currency": book.currency, "kind": "bank",
        "from": start, "to": end, "opening": opening, "closing": closing, "extraction": "csv", "rows": rows,
    }]}


OFX_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)")


def _ofx_fields(block: str) -> dict:
    """Leaf values of one OFX aggregate. OFX 1.x leaves leaf tags unclosed, 2.x closes them; both work."""
    out = {}
    for tag, value in OFX_TAG.findall(block):
        out.setdefault(tag, html.unescape(value.strip()))
    return out


def _ofx_date(value: str, where: str) -> str:
    return parse_date(f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) >= 8 else "", where).isoformat()


def from_ofx(data: bytes, book: Book, account: str, source_id: str | None) -> dict:
    """OFX / QFX / QBO bank or credit card download. Amounts are signed by the bank: negative is money out
    (on a card, a charge)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    statements = []
    for kind, body in re.findall(r"<(STMTRS|CCSTMTRS)>(.*?)</\1>", text, re.S):
        head = _ofx_fields(re.sub(r"<STMTTRN>.*?</STMTTRN>", "", body, flags=re.S))
        cur = head.get("CURDEF") or book.currency
        if cur.upper() != book.currency:
            raise LedgerError(f"OFX statement is in {cur}, the ledger is in {book.currency}")
        rows = []
        for n, block in enumerate(re.findall(r"<STMTTRN>(.*?)</STMTTRN>", body, re.S), 1):
            f = _ofx_fields(block)
            where = f"OFX transaction {n}"
            day = _ofx_date(f.get("DTPOSTED", ""), f"{where} DTPOSTED")
            rows.append({
                "booking_date": day, "value_date": _ofx_date(f["DTUSER"], f"{where} DTUSER") if f.get("DTUSER") else day,
                "amount": format(_csv_amount(f.get("TRNAMT"), {}), "f"),
                "text": clean_text(f.get("MEMO") or f.get("NAME")), "counterparty": clean_text(f.get("NAME") or f.get("PAYEE")),
                "iban": "", "reference": clean_text(f.get("REFNUM")), "bank_id": f.get("FITID") or None,
                **({"check_number": f["CHECKNUM"]} if f.get("CHECKNUM") else {}),
            })
        rows.sort(key=lambda r: r["booking_date"])
        bal = re.search(r"<LEDGERBAL>(.*?)</LEDGERBAL>", body, re.S)
        closing = _csv_amount(_ofx_fields(bal.group(1)).get("BALAMT"), {}) if bal else None
        total = sum((dec(r["amount"]) for r in rows), Decimal(0))
        dates = [r["booking_date"] for r in rows]
        start = _ofx_date(head["DTSTART"], "OFX DTSTART") if head.get("DTSTART") else min(dates, default=None)
        end = _ofx_date(head["DTEND"], "OFX DTEND") if head.get("DTEND") else max(dates, default=None)
        acct_id = head.get("ACCTID", "")
        sid = source_id or f"ofx:{acct_id[-4:]}:{start}:{end}:" + sha256_bytes(body.encode("utf-8"))[:8]
        statements.append({
            "source_id": sid if len(statements) == 0 else f"{sid}:{len(statements)}",
            "account": account, "currency": book.currency, "kind": "card" if kind == "CCSTMTRS" else "bank",
            "from": start, "to": end,
            # ponytail: OFX gives only the balance at the end of the download, so the opening is derived from it;
            # stage still checks it against the ledger and refuses a gap.
            "opening": None if closing is None else format(closing - total, "f"),
            "closing": None if closing is None else format(closing, "f"),
            "extraction": "ofx", "rows": rows,
        })
    if not statements:
        raise LedgerError("no bank or credit card statement in this OFX file")
    return {"schema": "statement/1", "statements": statements}


# ── Stage and commit statements ──────────────────────────────────────────────

STATEMENT_KEYS = {"source_id", "account", "iban", "currency", "kind", "from", "to", "opening", "closing",
                  "extraction", "count", "confirmed_by_user", "rows", "note", "file"}
ROW_KEYS = {"booking_date", "value_date", "amount", "text", "counterparty", "iban", "reference", "bank_id",
            "balance", "fx", "method", "check_number"}


def row_key(account: str, r: dict, prec: int) -> str:
    parts = [account, r["booking_date"], r.get("value_date") or "", fmt(r["_amount"], prec),
             norm_text(" ".join(str(r.get(k) or "") for k in ("text", "counterparty", "reference"))),
             fmt(r["_balance"], prec) if r.get("_balance") is not None else ""]
    return sha256_bytes("|".join(parts).encode("utf-8"))[:20]


FEE_TEXT = re.compile(r"\bgebuehren?\b|^(?:fees?|spesen|entgelt)\b")
BANK_CHARGE = re.compile(r"\bcharges? for:", re.I)


def is_fee_row(r: dict) -> bool:
    """A bank's own fee, e.g. Wise "Charges for: CARD-...", even when the merchant column is filled.
    The colon matters: "Service charge for March" is a supplier's bill."""
    return bool(BANK_CHARGE.search(clean_text(r.get("text"))) or FEE_TEXT.search(norm_text(r.get("text"))))


# ponytail: text heuristic for card payments on a bank statement; a bank wording outside this list falls
# back to "skonto_unconfirmed", which still asks the user. Extend the list when one shows up.
CARD_TEXT = re.compile(r"card transaction|kartenzahlung|purchase authori[sz]ed|checkcard|debit card|\bpos\b|"
                       r"\bbankomat\b|\bmaestro\b")


def is_card_row(st: dict, r: dict) -> bool:
    return st.get("kind") == "card" or bool(r.get("fx")) or bool(CARD_TEXT.search(norm_text(r.get("text"))))


# US bank wording around the merchant name (Chase, Wells Fargo, Bank of America).
ACH_ORIG = re.compile(r"\bORIG CO NAME:\s*(.+?)(?=\s+(?:ORIG ID|CO ENTRY|DESC DATE|SEC|TRACE|IND ID|IND NAME|EED|DES:|INDN:|CO ID:|PPD ID:|CCD ID:|WEB ID:)|$)", re.I)
CARD_PREFIX = re.compile(r"^(?:(?:RECURRING\s+)?(?:PURCHASE|PAYMENT)(?:\s+RETURN)?\s+AUTHORI[SZ]ED\s+ON\s+\d\d/\d\d|"
                         r"CHECKCARD\s+\d{4}|(?:DEBIT\s+CARD|POS)\s+(?:PURCHASE|DEBIT)(?:\s+-)?)\s+", re.I)
ACH_TAIL = re.compile(r"\s+(?:DES|ID|INDN|CO ID|PPD ID|CCD ID|WEB ID):.*$", re.I)
PROCESSOR_PREFIX = re.compile(r"^(?:SQ|TST|SP|PP|PAYPAL|IN)\s?\*\s*", re.I)
CHECK_TEXT = re.compile(r"^(?:CHECK|CHK|CHEQUE)\s*#?\s*(\d+)\b", re.I)


def merchant_text(text: str) -> str:
    """The merchant part of a US bank description; other texts come back unchanged."""
    s = clean_text(text)
    orig = ACH_ORIG.search(s)
    if orig:
        return orig.group(1).strip()
    s = ACH_TAIL.sub("", CARD_PREFIX.sub("", s))
    s = PROCESSOR_PREFIX.sub("", s)
    return re.sub(r"\*\S*", " ", s).strip()


def check_number(r: dict) -> str:
    m = CHECK_TEXT.match(clean_text(r.get("text")))
    return str(r.get("check_number") or (m.group(1) if m else "")).strip()


def vendor_key(r: dict) -> str:
    fee = is_fee_row(r)
    if r.get("iban") and not fee:
        return "iban:" + norm_iban(r["iban"])
    # ponytail: every check is its own group; the payee is not on the statement, so ask per check.
    if check_number(r) and not r.get("counterparty"):
        return "check:" + check_number(r)
    t = NOISE_WORDS.sub(" ", norm_text(r.get("text") if fee else merchant_text(r.get("counterparty") or r.get("text"))))
    words = [w for w in re.sub(r"\b\w*\d\w*\b", " ", t).split() if len(w) > 1][:3]
    return "text:" + (" ".join(words) or "?")


def ledger_rows(book: Book, account: str):
    ids, keys, weak = set(), Counter(), Counter()
    for t in book.live_txns():
        if book.statement_accounts.get(t.meta.get("source_statement")) != account:
            continue
        if t.meta.get("bank_id"):
            ids.add(t.meta["bank_id"])
        if t.meta.get("row_key"):
            keys[t.meta["row_key"]] += 1
        amount = sum((p.amount for p in t.postings if p.account == account), Decimal(0))
        weak[(t.date.isoformat(), fmt(amount, book.prec))] += 1
    return ids, keys, weak


def match_rule(book: Book, account: str, r: dict) -> dict | None:
    best = None
    for rule in book.rules.values():
        if rule.get("on_account") and rule["on_account"] != account or not rule_hits(rule, r):
            continue
        rank = (int(rule.get("priority", 0)), bool(rule.get("iban")), len(rule.get("text") or ""), rule["id"])
        if best is None or rank > best[0]:
            best = (rank, rule)
    return best[1] if best else None


def rule_hits(rule: dict, r: dict) -> bool:
    """Whether a rule or allocation matches a statement row (r["_amount"] is the signed amount)."""
    amt = r["_amount"]
    hay = norm_text(" ".join(str(r.get(k) or "") for k in ("text", "counterparty", "reference")))
    return not (rule.get("disabled")
                or rule.get("money") == "in" and amt <= 0 or rule.get("money") == "out" and amt >= 0
                or rule.get("amount_min") is not None and abs(amt) < dec(rule["amount_min"])
                or rule.get("amount_max") is not None and abs(amt) > dec(rule["amount_max"])
                or rule.get("iban") and norm_iban(rule["iban"]) != norm_iban(r.get("iban"))
                or rule.get("text") and norm_text(rule["text"]) not in hay)


def share_lines(book: Book, spec: dict, amount: Decimal, paid_privately: bool = False) -> list[dict]:
    """Business share of a payment to the spec's account (with tax). Paid from a business account, the rest is a
    private withdrawal; paid privately, the business share is a private contribution."""
    business = q(-amount * parse_share(spec["share"], spec["id"]), book.prec)
    private = spec.get("private_account") or private_account(book, spec["id"])
    tax = {"tax": {**spec["tax"], "source": spec["tax"].get("source", "rule")}} if spec.get("tax") else {}
    return [{"account": spec["account"], "amount": fmt(business, book.prec), **tax},
            {"account": private, "amount": fmt(-business if paid_privately else -amount - business, book.prec)}]


def allocate(book: Book, data: dict) -> dict:
    """Business shares of costs paid from a private account. The account stays out of the books: only the
    share is booked, against the private account (a private contribution). Returns a journal/1 proposal."""
    if not isinstance(data, dict) or data.get("schema") != "statement/1":
        raise LedgerError("statement file needs schema statement/1")
    allocs = [a for a in book.allocations.values() if not a.get("disabled")]
    if not allocs:
        raise LedgerError("the ledger has no allocations. Add them with journal first (allocations)")
    done = allocated_rows(book)
    entries, skipped, unmatched, labels = [], [], 0, []
    for si, st in enumerate(data.get("statements") or [], 1):
        where = f"statement {si}"
        if st.get("account") in book.accounts:
            raise LedgerError(f"{where}: {st['account']} is in the books. Book it with stage, not allocate")
        if (st.get("currency") or book.currency) != book.currency:
            raise LedgerError(f"{where}: {st['currency']} statement. The ledger keeps {book.currency} only")
        label = str(st.get("account") or st.get("iban") or st.get("source_id") or where)
        labels.append(label)
        for i, row in enumerate(st.get("rows") or [], 1):
            rw = f"{where} row {i}"
            if set(row) - ROW_KEYS:
                raise LedgerError(f"{rw}: unknown fields {sorted(set(row) - ROW_KEYS)}")
            r = {**row, "_amount": money(row.get("amount"), book.prec, f"{rw} amount"), "_balance": None}
            d = parse_date(r.get("booking_date"), f"{rw} booking_date")
            hits = [a for a in allocs if rule_hits(a, r) and not (a.get("from") and d < parse_date(a["from"]))
                    and not (a.get("to") and d > parse_date(a["to"]))]
            if not hits:
                unmatched += 1
                continue
            key = row_key("private:" + label, r, book.prec)
            seen = {"row": rw, "date": d.isoformat(), "amount": fmt(r["_amount"], book.prec),
                    "text": clean_text(r.get("counterparty") or r.get("text"))}
            if len(hits) > 1:
                skipped.append({**seen, "note": f"matches allocations {sorted(a['id'] for a in hits)}. Make them more specific"})
            elif key in done:
                skipped.append({**seen, "note": "already booked"})
            elif not book.fy_from <= d <= book.fy_to:
                skipped.append({**seen, "note": "outside this fiscal year"})
            elif book.closed_through and d <= book.closed_through:
                skipped.append({**seen, "note": f"in a closed period (through {book.closed_through}). "
                                                "Ask whether to book it at a later date"})
            else:
                a = hits[0]
                done.add(key)
                paid = fmt(abs(r["_amount"]), book.prec)
                entries.append({
                    "date": d.isoformat(), "payee": a.get("payee") or seen["text"],
                    "narration": f"Business share {a.get('basis') or a['share']} of {paid} paid privately",
                    "lines": share_lines(book, a, r["_amount"], paid_privately=True), "confirmed": "rule", "classified_by": f"allocation:{a['id']}",
                    **({"document": a["document"]} if a.get("document") else {"no_document": f"paid privately, allocation {a['id']}"}),
                    "meta": {"allocation": a["id"], "allocation_row": key},
                })
    return {"journal": {"schema": "journal/1", "entries": entries, "note": f"allocations from {', '.join(labels)}"},
            "skipped": skipped, "rows_without_allocation": unmatched}


def allocated_rows(book: Book) -> set:
    return {t.meta["allocation_row"] for t in book.txns
            if t.meta.get("allocation_row") and t.meta.get("txn_id") not in book.reversed}


def stage(book: Book, data: dict) -> dict:
    if not isinstance(data, dict) or data.get("schema") != "statement/1":
        raise LedgerError("statement file needs schema statement/1")
    staged = defaultdict(list)
    out = []
    for si, st in enumerate(data.get("statements") or [], 1):
        out.append(stage_one(book, st, si, staged))
    if not out:
        raise LedgerError("no statements in the file")
    return {
        "schema": "pending/1", "kind": "statement", "ledger_id": book.ledger_id, "ledger_file": book.path.name,
        "ledger_version": book.version, "ledger_sha256": book.sha, "statements": out, "committed_in": None,
    }


def stage_one(book: Book, st: dict, si: int, staged: dict) -> dict:
    where = f"statement {si}"
    extra = set(st) - STATEMENT_KEYS
    if extra:
        raise LedgerError(f"{where}: unknown fields {sorted(extra)}")
    acct = st.get("account")
    if not acct and st.get("iban"):
        acct = next((a for a, i in book.accounts.items() if i["meta"].get("iban") == norm_iban(st["iban"])), None)
    if not acct or acct not in book.accounts:
        raise LedgerError(f"{where}: account {acct or st.get('iban')} is not in the ledger. Open it with its iban first")
    if book.role_of(acct) not in MONEY_ROLES:
        raise LedgerError(f"{where}: {acct} is not a bank, cash, card or processor account")
    if (st.get("currency") or book.currency) != book.currency:
        raise LedgerError(f"{where}: {st['currency']} statement. The ledger keeps {book.currency} only")
    kind = st.get("kind", "bank")
    if kind not in STATEMENT_KINDS:
        raise LedgerError(f"{where}: kind is one of {sorted(STATEMENT_KINDS)}")
    extraction = st.get("extraction", "manual")
    if extraction not in EXTRACTIONS:
        raise LedgerError(f"{where}: extraction is one of {sorted(EXTRACTIONS)}")
    sid = st.get("source_id")
    if not isinstance(sid, str) or not sid:
        raise LedgerError(f"{where}: source_id is required (a stable id for this statement)")
    start, end = parse_date(st.get("from"), f"{where} from"), parse_date(st.get("to"), f"{where} to")
    if start > end:
        raise LedgerError(f"{where}: from is after to")
    prec = book.prec
    rows = st.get("rows") or []
    for i, r in enumerate(rows, 1):
        rw = f"{where} row {i}"
        if set(r) - ROW_KEYS:
            raise LedgerError(f"{rw}: unknown fields {sorted(set(r) - ROW_KEYS)}")
        if r.get("method") not in (None, *PAYMENT_METHODS):
            raise LedgerError(f"{rw}: method is one of {sorted(PAYMENT_METHODS)} or left out")
        r["_amount"] = money(r.get("amount"), prec, f"{rw} amount")
        r["_balance"] = None if r.get("balance") in (None, "") else money(r["balance"], prec, f"{rw} balance")
        d = parse_date(r.get("booking_date"), f"{rw} booking_date")
        opt_date(r.get("value_date"), f"{rw} value_date")
        if not start <= d <= end:
            raise LedgerError(f"{rw}: booked {d}, outside the statement period {start} to {end}")
        if r.get("fx"):
            fx = r["fx"]
            if not isinstance(fx, dict) or not fx.get("currency") or fx.get("amount") is None:
                raise LedgerError(f"{rw}: fx needs amount, currency and rate")
            if fx.get("rate") in (None, ""):
                raise LedgerError(f"{rw}: foreign-currency row without an exchange rate. Take it from the bank or ask")
            if dec(fx["rate"], f"{rw} fx rate") <= 0:
                raise LedgerError(f"{rw}: fx rate must be positive")
    total = sum((r["_amount"] for r in rows), Decimal(0))
    if end > book.fy_to or start < book.fy_from:
        cut = sum((r["_amount"] for r in rows if parse_date(r["booking_date"]) <= book.fy_to), Decimal(0))
        hint = ""
        if st.get("opening") is not None and end > book.fy_to:
            hint = f" The part up to {book.fy_to} closes at {fmt(money(st['opening'], prec) + cut, prec)}."
        raise LedgerError(f"{where}: crosses the fiscal year {book.fy_from} to {book.fy_to}. Split it at the year end.{hint}")
    rows_sha = sha256_bytes(dump_json([[r.get("booking_date"), fmt(r["_amount"], prec), r.get("text"), r.get("counterparty"),
                                        r.get("reference"), r.get("bank_id")] for r in rows]).encode("utf-8"))
    already = sid in book.statement_accounts
    if already and book.statement_sha.get(sid) not in (None, rows_sha):
        raise LedgerError(f"{where}: {sid} was booked before with different rows. Give this file its own source_id "
                          "(for example with the download date); the overlap check then finds the rows already booked")
    prior = sum((a for d, a in staged[acct] if d < start), Decimal(0))
    opening_entry = sum((p.amount for t in book.txns if t.meta.get("opening") and t.date == start
                         for p in t.postings if p.account == acct), Decimal(0))
    ledger_open = book.balance(acct, before=start) + opening_entry + prior
    opening = None if st.get("opening") in (None, "") else money(st["opening"], prec, f"{where} opening")
    closing = None if st.get("closing") in (None, "") else money(st["closing"], prec, f"{where} closing")
    notes = []
    if opening is None:
        opening = ledger_open
        notes.append("opening taken from the ledger")
    if closing is None:
        if not st.get("confirmed_by_user"):
            raise LedgerError(f"{where}: no closing balance. Ask for it (bank app, statement), or have the user confirm the rows")
        closing = opening + total
        notes.append("closing not printed, rows confirmed by the user")
    if opening + total != closing:
        raise LedgerError(
            f"{where}: opening {opening} plus rows {total} is {opening + total}, but the closing balance is {closing}. "
            "Rows are missing or were read wrong"
        )
    running = opening
    for i, r in enumerate(rows, 1):
        running += r["_amount"]
        if r["_balance"] is not None and r["_balance"] != running:
            raise LedgerError(f"{where} row {i}: running balance {r['_balance']} does not fit (expected {running})")
    if st.get("count") is not None and int(st["count"]) != len(rows):
        raise LedgerError(f"{where}: the statement lists {st['count']} transactions, {len(rows)} were read")
    if (extraction == "model" and st.get("count") is None and not any(r["_balance"] is not None for r in rows)
            and not st.get("confirmed_by_user")):
        raise LedgerError(
            f"{where}: read from a PDF or image without a transaction count or running balances. "
            "Show the rows to the user and set confirmed_by_user"
        )
    if not already and opening != ledger_open:
        raise LedgerError(
            f"{where}: opens at {opening}, but the ledger has {ledger_open} on {start}. "
            "An earlier statement is missing, or rows were booked twice"
        )
    if book.role_of(acct) == "cash" or kind == "cashbook":
        bal = opening
        for i, r in enumerate(rows):
            bal += r["_amount"]
            if (i + 1 == len(rows) or rows[i + 1]["booking_date"] != r["booking_date"]) and bal < 0:
                raise LedgerError(f"{where}: cash goes negative on {r['booking_date']} ({bal}). A deposit is missing")
    ids, keys, weak = ledger_rows(book, acct)
    covered = [(parse_date(s["from"]), parse_date(s["to"])) for s in book.statements if s.get("account") == acct]
    out_rows = []
    for i, r in enumerate(rows, 1):
        key = row_key(acct, r, prec)
        out_rows.append({
            "ref": f"{si}:{i}", "seq": i, "status": "new", "note": "", "row_key": key,
            "vendor_key": vendor_key(r), **{k: r.get(k) for k in ROW_KEYS},
            "amount": fmt(r["_amount"], prec),
        })
    if already:
        for o in out_rows:
            o["status"], o["note"] = "duplicate", "statement already booked"
    else:
        groups = defaultdict(list)
        for o in out_rows:
            if o.get("bank_id") and o["bank_id"] in ids:
                o["status"], o["note"] = "duplicate", "bank id already booked"
            else:
                groups[o["row_key"]].append(o)
        for key, group in groups.items():
            booked = keys.get(key, 0)
            if booked >= len(group):
                # Identical real payments on one day are counted, not collapsed: n booked covers n rows.
                for o in group:
                    o["status"], o["note"] = "duplicate", "same row already booked"
            elif booked:
                for o in group:
                    o["status"] = "ambiguous"
                    o["note"] = f"ledger has {booked} identical rows, this statement {len(group)}"
            else:
                for o in group:
                    d = parse_date(o["booking_date"])
                    if any(a <= d <= b for a, b in covered):
                        o["status"] = "ambiguous"
                        o["note"] = ("a booked row on the same day has the same amount"
                                     if weak.get((o["booking_date"], o["amount"])) else
                                     "this day was already booked from another statement")
    target = closing - book.balance(acct, before=end + timedelta(days=1)) - sum(
        (a for d, a in staged[acct] if d <= end), Decimal(0))
    new_sum = sum((dec(o["amount"]) for o in out_rows if o["status"] == "new"), Decimal(0))
    amb = [o for o in out_rows if o["status"] == "ambiguous"]
    hint = None
    if not already:
        if amb:
            hint = f"ambiguous rows to book must add up to {fmt(target - new_sum, prec)} for the closing balance to fit"
        elif target != new_sum:
            raise LedgerError(
                f"{where}: after removing duplicates the rows add up to {new_sum}, the closing balance needs {target}. "
                "Some rows were booked differently before: check the overlap with the earlier statement, and "
                "transfers already booked from the other account (book both sides through the clearing account)"
            )
    for o in out_rows:
        if o["status"] in ("new", "ambiguous"):
            rule = match_rule(book, acct, {**o, "_amount": dec(o["amount"])})
            o["suggested"] = None if rule is None else {
                "rule": rule["id"], "payee": rule.get("payee"),
                "lines": share_lines(book, rule, dec(o["amount"])) if rule.get("share") else
                         [{"account": rule["account"], **({"tax": rule["tax"]} if rule.get("tax") else {}),
                           **({"deductible": rule["deductible"]} if rule.get("deductible") is not None else {})}],
                "confirmed": "no" if rule.get("needs_review") else "rule", "classified_by": f"rule:{rule['id']}",
            }
            staged[acct].append((parse_date(o["booking_date"]), dec(o["amount"])))
    return {
        "source_id": sid, "account": acct, "kind": kind, "from": start.isoformat(), "to": end.isoformat(),
        "opening": fmt(opening, prec), "closing": fmt(closing, prec), "count": len(rows), "extraction": extraction,
        "status": "already_booked" if already else "staged", "notes": notes, "ambiguous_hint": hint,
        "file": st.get("file"), "rows_sha256": rows_sha, "rows": out_rows,
    }


def pending_summary(pending: dict) -> dict:
    out = []
    for st in pending["statements"]:
        counts = Counter(r["status"] for r in st["rows"])
        groups = defaultdict(lambda: {"rows": 0, "total": Decimal(0), "sample": ""})
        for r in st["rows"]:
            if r["status"] == "new" and not r.get("suggested"):
                g = groups[r["vendor_key"]]
                g["rows"] += 1
                g["total"] += dec(r["amount"])
                g["sample"] = g["sample"] or (r.get("counterparty") or r.get("text") or "")[:80]
        out.append({
            "source_id": st["source_id"], "account": st["account"], "period": [st["from"], st["to"]],
            "status": st["status"], "counts": dict(counts),
            "with_rule": sum(1 for r in st["rows"] if r["status"] == "new" and r.get("suggested")),
            "ambiguous": [{"ref": r["ref"], "date": r["booking_date"], "amount": r["amount"], "text": r.get("text"),
                           "note": r["note"]} for r in st["rows"] if r["status"] == "ambiguous"],
            "ambiguous_hint": st.get("ambiguous_hint"),
            "groups_to_classify": sorted(
                ({"vendor_key": k, **v} for k, v in groups.items()), key=lambda g: (-g["rows"], g["vendor_key"])),
        })
    return {"statements": out}


DECISION_KEYS = {"schema", "groups", "rows", "documents", "accounts", "rules", "assets", "confirmations",
                 "accept_suggested", "note", "csv_maps", "review_holds"}
ROW_DECISION_KEYS = {"ref", "vendor_key", "decision", "payee", "narration", "lines", "documents", "document",
                     "no_document", "confirmed", "classified_by", "match", "document_review", "meta", "settles"}


def check_pending(book: Book, pending: dict) -> None:
    if pending.get("schema") != "pending/1":
        raise LedgerError("not a pending file")
    if pending.get("committed_in"):
        raise LedgerError(f"already committed in {pending['committed_in']}")
    if pending.get("ledger_sha256") != book.sha:
        raise LedgerError(
            f"the pending file was staged on {pending.get('ledger_file')} (version {pending.get('ledger_version')}). "
            f"The ledger is now {book.path.name}. Stage the input again"
        )


def prepare_decisions(book: Book, decisions: dict) -> tuple[Book, str]:
    decisions = decisions or {}
    extra = set(decisions) - DECISION_KEYS
    if extra:
        raise LedgerError(f"decisions: unknown fields {sorted(extra)}")
    opens = open_text(book, decisions.get("accounts"))
    work = extend(book, opens)
    customs = custom_entries(work, decisions)
    return extend(work, customs), opens + customs


FULL_MATCH_TYPES = {"exact", "skonto", "skonto_unconfirmed", "fx_difference", "batch"}


def full_match(book: Book, r: dict, source: dict, doc: dict | None) -> bool:
    """The row pays the document in full: by the match type `candidates` gave, or by the amounts."""
    kind = str(source.get("match") or "").split(":", 1)[0]
    if kind in FULL_MATCH_TYPES:
        return True
    if kind == "partial" or not doc or doc["total"] is None:
        return False
    if doc["currency"] == book.currency:
        return abs(dec(r["amount"])) >= doc["total"]
    fx = r.get("fx") or {}
    return str(fx.get("currency") or "").upper() == doc["currency"] and abs(dec(fx["amount"])) >= doc["total"]


def build_commit(book: Book, pending: dict, decisions: dict | None, documents: dict | None = None) -> Book:
    check_pending(book, pending)
    if pending.get("kind") != "statement":
        raise LedgerError("this pending file holds documents. Use commit-documents")
    decisions = decisions or {}
    parsed, bad = parse_docs(book, (documents or {}).get("documents"))
    docs, unreadable = {d["id"]: d for d in parsed}, {b["document"]: b["reason"] for b in bad}
    full_rows = defaultdict(list)
    # Documents `candidates` found in Billpal Review are held by the commit itself, not by the model.
    known = {h["document"] for h in book.holds} | {h.get("document") for h in decisions.get("review_holds") or []}
    auto = [h for h in pending.get("review_holds") or [] if h["document"] not in known
            and book.fy_from <= parse_date(h["date"], "review hold date") <= book.fy_to]
    if auto:
        decisions = {**decisions, "review_holds": [*(decisions.get("review_holds") or []), *auto]}
    work, body = prepare_decisions(book, decisions)
    by_ref, by_group = {}, {}
    for d in decisions.get("rows") or []:
        if set(d) - ROW_DECISION_KEYS:
            raise LedgerError(f"row decision {d.get('ref')}: unknown fields {sorted(set(d) - ROW_DECISION_KEYS)}")
        by_ref[d.get("ref")] = d
    for g in decisions.get("groups") or []:
        if set(g) - ROW_DECISION_KEYS:
            raise LedgerError(f"group decision {g.get('vendor_key')}: unknown fields {sorted(set(g) - ROW_DECISION_KEYS)}")
        by_group[g.get("vendor_key")] = g
    unresolved, txns = [], []
    for st in pending["statements"]:
        if st["status"] == "already_booked":
            continue
        acct = st["account"]
        booked = 0
        for r in st["rows"]:
            rd = by_ref.get(r["ref"])
            if rd and rd.get("decision") == "duplicate":
                continue
            if r["status"] == "duplicate" and not (rd and rd.get("decision") == "book"):
                continue
            if r["status"] == "ambiguous" and not (rd and rd.get("decision") in ("book", "duplicate")):
                unresolved.append(f"{r['ref']} (ambiguous: book or duplicate?)")
                continue
            source = None
            if rd and rd.get("lines"):
                source = rd
            elif by_group.get(r["vendor_key"]):
                source = {**by_group[r["vendor_key"]], **{k: v for k, v in (rd or {}).items() if v is not None}}
            elif r.get("suggested") and decisions.get("accept_suggested"):
                source = {**r["suggested"], **{k: v for k, v in (rd or {}).items() if v is not None and k != "lines"}}
            if not source or not source.get("lines"):
                unresolved.append(f"{r['ref']} {r['booking_date']} {r['amount']} {r.get('counterparty') or r.get('text')}")
                continue
            for doc_id in [source["document"]] if source.get("document") else source.get("documents") or []:
                if doc_id in unreadable:
                    raise LedgerError(f"{r['ref']}: document {doc_id} could not be read: {unreadable[doc_id]}")
            txns.append(statement_txn(work, st, r, source, docs))
            booked += 1
            for doc_id in [source["document"]] if source.get("document") else source.get("documents") or []:
                if full_match(book, r, source, docs.get(doc_id)):
                    full_rows[doc_id].append(r["ref"])
        end = parse_date(st["to"])
        info = {k: st[k] for k in ("source_id", "account", "kind", "from", "to", "opening", "closing", "count", "extraction")}
        info.update({"booked": booked, "file": st.get("file"), "rows_sha256": st.get("rows_sha256")})
        txns.append(ser_custom(end, "statement", info) + ser_balance(end + timedelta(days=1), acct, dec(st["closing"]), book.currency, book.prec))
    if unresolved:
        raise LedgerError([f"{len(unresolved)} rows have no decision yet:"] + unresolved[:30])
    in_ledger = book.documents_in_ledger() if book.basis == "cash" else set()
    # An invoice and its linked receipt are one purchase: a full match of either is a full match of both.
    kind = lambda x: (docs.get(x) or {}).get("kind")
    linked_of = lambda x, k: sorted(y for y in (docs.get(x) or {}).get("linked") or [] if kind(y) == k)
    group, rows_of = defaultdict(set), defaultdict(list)
    for doc_id in full_rows:
        key = (linked_of(doc_id, "invoice") or [doc_id])[0] if kind(doc_id) == "receipt" else doc_id
        group[key] |= {key, doc_id} | (set(linked_of(key, "receipt")) if kind(key) == "invoice" else set())
        rows_of[key] += full_rows[doc_id]
    clash = [f"{' / '.join(sorted(group[k]))}: rows {', '.join(v)}" for k, v in rows_of.items() if len(v) > 1]
    clash += [f"{' / '.join(sorted(group[k]))}: already booked, again on row {v[0]}"
              for k, v in rows_of.items() if group[k] & in_ledger]
    if clash:
        raise LedgerError(["A document is paid in full only once. Keep one row per document, or book the other "
                           "as a partial payment:"] + clash[:30])
    body += "\n" + "\n".join(txns)
    note = clean_text(decisions.get("note") or f"statements {', '.join(s['source_id'] for s in pending['statements'])}")
    return write_next(book, body, note)


def doc_tax(book: Book, doc: dict) -> dict | None:
    tax = derive_tax(doc, book)
    if tax and book.basis == "cash" and book.vat_basis_for("purchase") == "invoice":
        tax = {**tax, "point": doc["date"].isoformat()}
    return tax


def untaxed(book: Book, ln) -> bool:
    """An income, expense or fixed-asset line without tax: a private share stays untaxed on purpose."""
    a = ln.get("account") if isinstance(ln, dict) else None
    return (isinstance(a, str) and ln.get("tax") is None
            and (a.split(":")[0] in ("Income", "Expenses") or book.role_of(a) == "fixed_asset"))


def derived_lines(book: Book, lines, tax: dict | None) -> list:
    if not tax or not isinstance(lines, list):
        return lines
    return [{**ln, "tax": tax} if untaxed(book, ln) else ln for ln in lines]


def party_label(party: dict) -> str | None:
    return party.get("brand_name") or party.get("company_name") or party.get("name")


def statement_txn(book: Book, st: dict, r: dict, source: dict, docs: dict | None = None) -> str:
    where = f"row {r['ref']}"
    d = parse_date(r["booking_date"])
    if book.closed_through and d <= book.closed_through:
        raise LedgerError(f"{where}: {d} is in a closed period (through {book.closed_through})")
    amount = dec(r["amount"])
    lines, payee = source["lines"], source.get("payee")
    ids = [source["document"]] if source.get("document") else source.get("documents") or []
    attached = [docs[i] for i in ids if i in (docs or {})]
    if attached and len(attached) == len(ids):
        taxes = [doc_tax(book, x) for x in attached]
        if all(t == taxes[0] for t in taxes):
            lines = derived_lines(book, lines, taxes[0])
        elif isinstance(lines, list) and any(untaxed(book, ln) for ln in lines):
            raise LedgerError(f"{where}: {', '.join(ids)} have different tax treatments. Give tax on each line")
        names = {party_label(x["party"]) for x in attached}
        if len(names) == 1:
            payee = payee or names.pop()
    posts = [Posting(st["account"], amount, book.currency, {})]
    posts += expand_lines(book, user_tax(lines, source.get("confirmed")), amount, d, where,
                          require_tax=True, cash_rows=True)
    meta = entry_meta({k: source.get(k) for k in ("confirmed", "classified_by", "document", "documents", "no_document",
                                                  "document_review", "meta") if source.get(k) is not None}
                      | {"confirmed": source.get("confirmed", "no")}, where)
    meta.update({"source_statement": st["source_id"], "source_row": str(r["seq"]), "row_key": r["row_key"]})
    for k in ("bank_id", "value_date", "counterparty", "iban", "reference", "check_number"):
        if r.get(k):
            meta[k] = r[k]
    if row_method(r):
        meta["payment_method"] = row_method(r)
    if r.get("fx"):
        meta["fx_json"] = r["fx"]
    # ponytail: one link per booking; a batch payment of several documents keeps only their ids.
    if len(attached) == 1 and attached[0].get("url"):
        meta["document_url"] = attached[0]["url"]
    tax_ids = {(x["party"] or {}).get("tax_id") for x in attached}
    if len(tax_ids) == 1 and None not in tax_ids:
        meta["party_tax_id"] = clean_text(tax_ids.pop())
    if source.get("match"):
        meta["match"] = source["match"]
    if source.get("settles") not in (None, True):
        raise LedgerError(f"{where}: settles is true or left out")
    if source.get("settles"):
        meta["settles"] = "yes"
    payee = payee or merchant_text(r.get("counterparty") or r.get("text") or "")[:60]
    narration = source.get("narration") or r.get("text") or ""
    flag = "!" if meta["confirmed"] == "no" else "*"
    return ser_txn(d, flag, payee, narration[:200], meta, posts, book.prec)


# ── Documents (accrual books: invoices become open items) ────────────────────

def from_billpal(data) -> dict:
    """Billpal `get_document` results (with `get_contact` and `list_chart_accounts`) to documents/1.
    Returns the documents and notes for what the user still has to give."""
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        raise LedgerError('from-billpal reads {"documents": [get_document results], "contacts": [get_contact results], '
                          '"chart_accounts": the list_chart_accounts result}')
    contacts = {}
    for c in data.get("contacts") or []:
        row = (c.get("contact") or {}) if isinstance(c, dict) else {}
        if isinstance(row, dict) and row.get("id"):
            ibans = [b.get("account_identifier") for b in c.get("bank_accounts") or [] if b.get("scheme") == "iban"]
            contacts[row["id"]] = {"account_code": row.get("account_code"), "ibans": [i for i in ibans if i]}
    chart = data.get("chart_accounts") or []
    chart = {a.get("id"): a.get("code") for a in (chart.get("accounts") or [] if isinstance(chart, dict) else chart)
             if isinstance(a, dict)}
    out, notes = [], []
    for g in data["documents"]:
        if not isinstance(g, dict) or not g.get("id"):
            raise LedgerError("each entry in documents is one get_document result")
        did = f"billpal:{g['id']}"
        if g.get("error"):
            notes.append(f"{did}: {g['error']}, left out")
            continue
        if g.get("status") == "excluded":
            who = (g.get("supplier") or g.get("customer") or {})
            notes.append(f"{did}: excluded in Billpal ({who.get('brand_name') or who.get('company_name') or 'no party'}, "
                         f"{g.get('invoice_date') or 'no date'}, {g.get('total_amount')} {g.get('currency') or ''}), "
                         "left out. Ask whether to include it")
            continue
        direction = {"incoming": "in", "outgoing": "out"}.get(g.get("direction"))
        if direction is None or not g.get("invoice_date"):
            notes.append(f"{did}: no direction or date in Billpal, left out. Ask the user to fix it in Billpal")
            continue
        total, tip = g.get("total_amount"), g.get("tip_amount") or 0
        sign = Decimal(-1) if total is not None and total < 0 else Decimal(1)
        m = lambda v: None if v is None else format(sign * Decimal(str(v)), "f")
        rate = lambda v: None if v is None else rate_str(Decimal(str(v)))
        side = "supplier" if direction == "in" else "customer"
        p = g.get(side) or {}
        contact = contacts.get(p.get("id")) or {}
        ibans = [b.get("accountIdentifier") for b in (g.get("bank_accounts") or {}).get(side) or []
                 if b.get("scheme") == "iban"] + contact.get("ibans", [])
        party = {"name": p.get("company_name") or p.get("brand_name"), "iban": next((i for i in ibans if i), None),
                 "personenkonto": contact.get("account_code")}
        party |= {k: p[k] for k in ("company_name", "brand_name", "country", "tax_id", "tax_id_country") if p.get(k)}
        if p.get("id") and p.get("id") not in contacts:
            notes.append(f"{did}: add get_contact for {party['name']} to read its Personenkonto")
        taxes = [{"type": t.get("tax_type"), "rate": rate(t.get("tax_rate")), "base": m(t.get("tax_base")),
                  "amount": m(t.get("tax_amount")), "exemption_reason": t.get("exemption_reason"),
                  "regime": t.get("tax_regime") or ("reverse_charge" if g.get("reverse_charge") else None)}
                 for t in g.get("tax_lines") or []]
        if g.get("reverse_charge") and not taxes:
            taxes = [{"type": "vat", "regime": "reverse_charge", "rate": None, "base": m(g.get("net_amount")),
                      "amount": None, "exemption_reason": None}]
        lines = []
        for li in g.get("line_items") or []:
            tax = {"type": li.get("tax_type"), "regime": li.get("tax_regime"), "rate": rate(li.get("tax_rate")),
                   "exemption_reason": li.get("exemption_reason")}
            lines.append({"description": li.get("description"), "amount": m(li.get("total")),
                          "amount_is": li.get("amount_basis") if li.get("amount_basis") in ("net", "gross") else "unknown",
                          **({"tax": tax} if any(v is not None for v in tax.values()) else {}),
                          **({"account_code": chart[li["chart_account_id"]]} if chart.get(li.get("chart_account_id")) else {})})
        if any(ln["amount"] is None for ln in lines):
            notes.append(f"{did}: a line has no amount, booked from the tax lines instead")
            lines = []
        if tip:
            if not lines and taxes and all(t["base"] is not None for t in taxes):
                lines = [{"amount": t["base"], "amount_is": "net",
                          **({"tax": {"type": t["type"], "regime": t["regime"], "rate": t["rate"]}} if t["regime"] else {})}
                         for t in taxes]
            if lines:
                lines.append({"description": "Tip", "amount": m(tip), "amount_is": "gross",
                              "tax": {"regime": "out_of_scope", "rate": "0"}})
            else:
                notes.append(f"{did}: has a tip but no lines or tax bases. Ask the user how to split it")
        doc = {"id": did, "kind": g.get("type") if g.get("type") in DOC_KINDS else "other", "direction": direction,
               "number": g.get("invoice_number") or g.get("receipt_number"), "date": g["invoice_date"],
               "due_date": g.get("payment_due_date"), "currency": (g.get("currency") or "").upper() or None,
               "party": party, "total": None if total is None else m(Decimal(str(total)) + Decimal(str(tip))),
               "discount": format(abs(Decimal(str(g["discount"]))), "f") if g.get("discount") else None,
               "taxes": taxes, "lines": lines, "linked": [], "source": "billpal", "url": g.get("app_url"),
               "review_open": g.get("status") == "review" or bool(g.get("review_notes"))}
        for ld in g.get("linked_documents") or []:
            peer = f"billpal:{ld.get('id')}"
            if ld.get("link_type") == "payment_confirmation_for":
                doc["linked"].append(peer)
            elif ld.get("link_type") == "credit_for" and doc["kind"] == "credit_note":
                if doc.get("applies_to"):
                    notes.append(f"{did}: credits more than one document. Ask which one it reduces")
                doc["applies_to"] = peer
        out.append({k: v for k, v in doc.items() if v not in (None, [])})
    return {"schema": "documents/1", "documents": out, "notes": notes}


DOC_KEYS = {"schema", "id", "kind", "direction", "number", "date", "due_date", "tax_point", "currency", "fx_rate",
            "party", "total", "discount", "taxes", "lines", "applies_to", "linked", "review_open", "terms", "source",
            "note", "url"}
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def doc_url(value, where: str) -> str | None:
    """Where the document opens (Billpal's app_url). Only plain http(s), since the report turns it into a link."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not URL_RE.fullmatch(value):
        raise LedgerError(f"{where}: url must be an http(s) address")
    return value
DOC_LINE_KEYS = {"description", "amount", "amount_is", "tax", "account", "account_code"}
DOC_DECISION_KEYS = {"id", "decision", "party_account", "lines", "confirmed", "classified_by", "applies_to",
                     "review_resolved", "party_confirmed"}
DOC_LINE_DECISION_KEYS = {"index", "account", "tax", "amount_is", "asset"}
DOC_KINDS = {"invoice", "credit_note", "receipt", "reminder", "other"}


def norm_doc(doc: dict, book: Book, need_rate: bool = True) -> dict:
    """need_rate=False only where no amount is booked from the document (cash matching and commit)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not doc["id"]:
        raise LedgerError("each document needs a text id (for Billpal: billpal:<document id>)")
    where = f"document {doc['id']}"
    extra = set(doc) - DOC_KEYS
    if extra:
        raise LedgerError(f"{where}: unknown fields {sorted(extra)}")
    if doc.get("kind") not in DOC_KINDS:
        raise LedgerError(f"{where}: kind is one of {sorted(DOC_KINDS)}")
    if doc.get("direction") not in ("in", "out"):
        raise LedgerError(f"{where}: direction is in (from a supplier) or out (to a customer)")
    cur = doc.get("currency") or book.currency
    prec = precision_for(cur) if cur != book.currency else book.prec
    out = {
        **doc, "currency": cur, "prec": prec,
        "date": parse_date(doc.get("date"), f"{where} date"),
        "due_date": opt_date(doc.get("due_date"), f"{where} due_date"),
        "tax_point": opt_date(doc.get("tax_point"), f"{where} tax_point"),
        "total": None if doc.get("total") is None else money(doc["total"], prec, f"{where} total"),
        "discount": None if doc.get("discount") in (None, "") else abs(money(doc["discount"], prec, f"{where} discount")) or None,
        "party": doc.get("party") or {},
        "url": doc_url(doc.get("url"), where),
    }
    if cur != book.currency:
        if doc.get("fx_rate") in (None, ""):
            if need_rate:
                raise LedgerError(f"{where}: {cur} document needs fx_rate (1 {cur} = x {book.currency})")
            out["fx_rate"] = None
        elif (rate := dec(doc["fx_rate"], f"{where} fx_rate")) <= 0:
            raise LedgerError(f"{where}: fx_rate must be positive")
        else:
            out["fx_rate"] = rate
    filled = {}

    def fill_rc(t: dict, what: str) -> dict:
        """Billpal keeps reverse charge at the 0 % the supplier charged, or with no rate. The buyer owes the
        domestic rate: fill it in as derived and unconfirmed. A sale keeps 0; without a known rate the
        treatment is dropped and the line needs one."""
        if t.get("regime") not in SELF_ASSESSED or (t.get("rate") is not None and parse_rate(t["rate"], what) != 0):
            return t
        if doc["direction"] == "out":
            return {**t, "rate": "0"}
        rate = (country_rule(book, out["date"].year).get("vat_standard")
                if book.profile.get("vat_status") in ("regular", "small_business") else None)
        if not rate:
            return {k: v for k, v in t.items() if k not in ("regime", "rate", "amount")}
        filled.setdefault("tax", {"type": t.get("type") or "vat", "regime": t["regime"], "rate": rate,
                                  "source": "derived", "confirmed": False})
        return {**{k: v for k, v in t.items() if k != "amount"}, "rate": rate, "source": "derived", "confirmed": False}

    taxes = []
    for t in doc.get("taxes") or []:
        if not isinstance(t, dict):
            raise LedgerError(f"{where}: each tax line is an object")
        t = fill_rc({k: v for k, v in t.items() if v is not None}, f"{where} tax rate")
        if t.get("regime"):
            nt = normalize_tax({"source": "document", **{k: v for k, v in t.items() if k != "base"}}, book, where, prec)
        else:
            nt = {"type": t.get("type") or "vat", "regime": None, "rate": parse_rate(t.get("rate", "0"), f"{where} tax rate"),
                  "amount": None if t.get("amount") is None else money(t["amount"], prec, f"{where} tax amount")}
        nt["base"] = None if t.get("base") is None else money(t["base"], prec, f"{where} tax base")
        taxes.append(nt)
    out["taxes"] = taxes
    lines = []
    for i, ln in enumerate(doc.get("lines") or [], 1):
        if not isinstance(ln, dict) or set(ln) - DOC_LINE_KEYS:
            raise LedgerError(f"{where} line {i}: unknown fields {sorted(set(ln) - DOC_LINE_KEYS) if isinstance(ln, dict) else ln}")
        money(ln.get("amount"), prec, f"{where} line {i} amount")
        if ln.get("amount_is") not in (None, "gross", "net", "unknown"):
            raise LedgerError(f"{where} line {i}: amount_is is gross, net or unknown")
        ln = dict(ln)
        tax = ln.pop("tax", None)
        if tax is not None:
            if not isinstance(tax, dict):
                raise LedgerError(f"{where} line {i}: tax must be an object")
            tax = fill_rc({k: v for k, v in tax.items() if v is not None}, f"{where} line {i} tax rate")
            if not tax.get("regime") or not tax.get("type"):
                # Billpal leaves line type and regime empty when the header tax line carries them.
                rate = parse_rate(tax["rate"], f"{where} line {i} tax rate") if tax.get("rate") is not None else None
                if filled and not tax.get("regime") and not rate:
                    tax = {**tax, "rate": filled["tax"]["rate"], "source": "derived", "confirmed": False}
                    rate = parse_rate(tax["rate"])
                header = {(t["type"], t["regime"]) for t in taxes if t["regime"] and t["rate"] == rate
                          and t["type"] == tax.get("type", t["type"]) and t["regime"] == tax.get("regime", t["regime"])}
                if rate is not None and len(header) == 1:
                    ttype, regime = header.pop()
                    tax = {**tax, "type": ttype, "regime": regime}
                elif not tax.get("regime"):
                    tax = None
        if tax is not None:
            ln["tax"] = tax
        lines.append(ln)
    out["lines_from"] = "document"
    if not lines:
        if taxes and all(t["base"] is not None for t in taxes):
            lines = [{"amount": format(t["base"], "f"), "amount_is": "net",
                      **({"tax": {"type": t["type"], "regime": t["regime"], "rate": rate_str(t["rate"]),
                                  **({"source": "derived", "confirmed": False} if t.get("source") == "derived" else {})}}
                         if t["regime"] else {})}
                     for t in taxes]
            out["lines_from"] = "taxes"
        elif out["total"] is not None:
            lines = [{"amount": format(out["total"], "f"), "amount_is": "gross"}]
            out["lines_from"] = "total"
        else:
            raise LedgerError(f"{where}: needs lines, tax lines with bases, or a total")
    out["lines"] = lines
    out["rc_filled"] = filled.get("tax")
    return out


def derive_tax(doc: dict, book: Book) -> dict | None:
    """Reverse charge for a bill from a supplier abroad that shows no VAT, from a normalized document.
    Assumes a service: a goods invoice is rebooked as import or intra_community."""
    status = book.profile.get("vat_status")
    if doc["direction"] != "in" or status not in ("regular", "small_business"):
        return None
    if doc.get("rc_filled"):
        return dict(doc["rc_filled"])
    party = doc.get("party") or {}
    iso = lambda v: (v or "").strip().upper() if re.fullmatch(r"[A-Za-z]{2}", (v or "").strip()) else None
    country, id_country = iso(party.get("country")), iso(party.get("tax_id_country"))
    if party.get("country") and not country:
        return None
    id_country = None if id_country == "EU" else id_country
    if country and id_country and country != id_country:
        return None
    supplier = country or id_country
    if not supplier or supplier == book.profile.get("country"):
        return None
    if any(t.get("regime") or (t.get("amount") or 0) > 0 for t in doc["taxes"]):
        return None
    if any(isinstance(ln.get("tax"), dict) and ln["tax"].get("regime") for ln in doc["lines"]):
        return None
    rate = country_rule(book, doc["date"].year).get("vat_standard")
    if not rate:
        return None
    return {"type": "vat", "regime": "reverse_charge", "rate": rate, "source": "derived", "confirmed": False}


def find_party_account(book: Book, doc: dict, taken: dict | None = None) -> tuple[str | None, dict | None, str]:
    """The Personenkonto for a document: by number, then IBAN, then a unique name without a conflicting
    number or IBAN. Returns (account, new account spec, how it matched). `taken` holds new accounts
    already proposed in this batch, by path."""
    role = "ap" if doc["direction"] == "in" else "ar"
    parents = book.accounts_with_role(role)
    party = doc["party"]
    code = clean_text(party.get("personenkonto")) or None
    name = clean_text(party.get("name"))
    iban = norm_iban(party["iban"]) if party.get("iban") else None
    taken = {} if taken is None else taken
    accts = [(a, book.accounts[a]["meta"]) for a in sorted(book.accounts)
             if book.role_of(a) == role and a not in parents]
    accts += [(s["account"], s) for s in taken.values() if s.get("_role") == role]
    for key, value, how in (("personenkonto", code, "personenkonto"), ("iban", iban, "iban")):
        hits = [a for a, m in accts if value and m.get(key) == value]
        if len(hits) == 1:
            return hits[0], None, how
        if len(hits) > 1:
            return None, None, "ambiguous"
    if name:
        hits = [a for a, m in accts if norm_text(m.get("name", "")) == norm_text(name)
                and not (code and m.get("personenkonto") and m["personenkonto"] != code)
                and not (iban and m.get("iban") and m["iban"] != iban)]
        if len(hits) == 1:
            return hits[0], None, "name"
        if len(hits) > 1:
            return None, None, "ambiguous"
    if not parents or not name:
        return None, None, "none"
    path, _ = account_path(book, parents[0], name, code, reuse=False, taken=set(taken))
    spec = {"account": path, "name": name}
    if code:
        spec["personenkonto"] = code
    if iban:
        spec["iban"] = iban
    taken[path] = {**spec, "_role": role}
    return None, spec, "new"


def stage_documents(book: Book, data: dict) -> dict:
    if book.basis != "accrual":
        raise LedgerError("cash-basis books attach documents to bank bookings. Use candidates and commit instead")
    if not isinstance(data, dict) or data.get("schema") != "documents/1":
        raise LedgerError("documents file needs schema documents/1")
    originals = data.get("documents") or []
    docs, errors = [], []
    for raw in originals:
        try:
            docs.append(norm_doc(raw, book))
            errors.append(None)
        except LedgerError as exc:
            docs.append(None)
            errors.append("; ".join(exc.messages))
    ok_docs = [d for d in docs if d]
    booked = book.documents_in_ledger()
    linked = {l for d in ok_docs if d["kind"] in ("invoice", "credit_note") for l in (d.get("linked") or [])}
    invoices = {d["id"] for d in ok_docs if d["kind"] in ("invoice", "credit_note")}
    linked |= {d["id"] for d in ok_docs if d["kind"] == "receipt" and invoices & set(d.get("linked") or [])}
    out, taken = [], {}
    for d, original, error in zip(docs, originals, errors):
        if d is None:
            raw = original if isinstance(original, dict) else {}
            try:
                held = bool(raw.get("review_open")) and bool(parse_date(raw.get("date"), "date"))
            except LedgerError:
                held = False
            out.append({"ref": raw.get("id"), "status": "review_blocked" if held else "invalid",
                        "note": f"could not be read: {error}" + (". Still in Billpal Review, recorded as held" if held else ""),
                        "document": original, "suggested": {"lines": []}})
            continue
        status, note = "new", ""
        booked_link = next((l for l in d.get("linked") or [] if l in booked), None)
        if d["kind"] == "reminder":
            status, note = "excluded", "reminders are not bookings"
        elif d["id"] in booked:
            status, note = "already_booked", ""
        elif booked_link and d["kind"] in ("invoice", "receipt"):
            status, note = "already_booked", f"its linked document {booked_link} is booked"
        elif d["kind"] == "receipt" and d["id"] in linked:
            status, note = "linked", "counted once through the linked invoice"
        elif not book.fy_from <= d["date"] <= book.fy_to:
            status, note = "other_year", "dated outside this fiscal year"
        elif d.get("review_open"):
            status, note = "review_blocked", ("still in Billpal Review. Committing records it as held, and the "
                                              "books are not ready until it is resolved and booked")
        acct, new_party, how = find_party_account(book, d, taken) if status in ("new", "review_blocked") else (None, None, "none")
        lines = []
        for ln in d["lines"]:
            sug = ln.get("account")
            if not sug and ln.get("account_code"):
                sug = next((a for a, i in book.accounts.items() if i["meta"].get("code") == str(ln["account_code"])), None)
            lines.append({"account": sug, "needs_tax": "tax" not in ln, "amount_is": ln.get("amount_is") or "gross"})
        derived = derive_tax(d, book) if status in ("new", "review_blocked") else None
        if derived:
            original = {**original, "lines": [ln if "tax" in ln else {**ln, "tax": derived} for ln in d["lines"]]}
            lines = [{**ln, "needs_tax": False} for ln in lines]
        out.append({"ref": d["id"], "status": status, "note": note, "document": original,
                    "suggested": {"party_account": acct or (new_party or {}).get("account"), "new_party": new_party,
                                  "party_matched_by": how, "lines": lines, **({"derived_tax": derived} if derived else {})}})
    return {"schema": "pending/1", "kind": "documents", "ledger_id": book.ledger_id, "ledger_file": book.path.name,
            "ledger_version": book.version, "ledger_sha256": book.sha, "documents": out, "committed_in": None}


def doc_work(book: Book, d: dict, overrides: dict, sug: dict, basis: list[str], discounted: bool,
             where: str) -> tuple[list[dict], Decimal]:
    """Lines with their tax for one reading of the document, and what that reading makes payable.

    With `discounted`, the document discount is spread over the lines pro rata. Billpal gives the
    discount positive, with totals and tax bases after it and the lines possibly before it."""
    prec, unit = d["prec"], Decimal(1).scaleb(-d["prec"])
    amounts = [money(ln["amount"], prec, f"{where} line {i + 1}") for i, ln in enumerate(d["lines"])]
    if discounted:
        if len(set(basis)) > 1:
            raise LedgerError(f"{where}: lines mix net and gross, so the discount cannot be spread over them")
        s = sum(amounts, Decimal(0))
        if s == 0 or abs(s) <= d["discount"]:
            raise LedgerError(f"{where}: the discount {d['discount']} does not fit lines adding up to {s}")
        target = s - d["discount"] if s > 0 else s + d["discount"]
        spread = [q(a * target / s, prec) for a in amounts]
        spread[max(range(len(spread)), key=lambda i: abs(amounts[i]))] += target - sum(spread, Decimal(0))
        amounts = spread
    work = []
    for i, ln in enumerate(d["lines"]):
        o = overrides.get(i, {})
        tax = o.get("tax", ln.get("tax"))
        acct = o.get("account") or ln.get("account") or (sug.get("lines") or [{}] * (i + 1))[i].get("account")
        if not acct:
            raise LedgerError(f"{where} line {i + 1}: no account. Give one in the decisions")
        amt = amounts[i]
        t = None if tax is None else normalize_tax(
            {**tax, "source": tax.get("source", "document" if "tax" in ln else "agent")}, book, where, prec)
        if t:
            tax = {**tax, "source": t["source"]}
        if t and discounted:
            t["amount"] = None
            tax = {k: v for k, v in tax.items() if k != "amount"}
        tax_amt = Decimal(0)
        is_net = basis[i] == "net"
        if t and t["regime"] in TAXED_REGIMES and t["rate"]:
            if t["amount"] is not None:
                tax_amt = t["amount"]
            elif t["regime"] in SELF_ASSESSED or is_net:
                tax_amt = q(amt * t["rate"] / 100, prec)
            else:
                tax_amt = q(amt * t["rate"] / (100 + t["rate"]), prec)
        work.append({"account": acct, "amount": amt, "net": is_net or (t is not None and t["regime"] in SELF_ASSESSED),
                     "tax": tax, "t": t, "tax_amt": tax_amt, "asset": o.get("asset")})
    for ht in d["taxes"]:
        if ht["amount"] is None or ht["regime"] not in TAXED_REGIMES:
            continue
        group = [w for w in work if w["t"] and w["t"]["regime"] == ht["regime"] and w["t"]["rate"] == ht["rate"]]
        if not group:
            continue
        diff = ht["amount"] - sum((w["tax_amt"] for w in group), Decimal(0))
        if diff:
            if abs(diff) > unit * len(group):
                raise LedgerError(f"{where}: line taxes for {ht['regime']} {rate_str(ht['rate'])} % add up to "
                                  f"{ht['amount'] - diff}, the document says {ht['amount']}")
            max(group, key=lambda w: abs(w["amount"]))["tax_amt"] += diff
    payable = sum((w["amount"] + (w["tax_amt"] if w["net"] and not (w["t"] and w["t"]["regime"] in SELF_ASSESSED) else 0)
                   for w in work), Decimal(0))
    return work, payable


def doc_txn(book: Book, raw: dict, dd: dict, sug: dict) -> str:
    d = norm_doc(raw, book)
    where = f"document {d['id']}"
    if book.closed_through and d["date"] <= book.closed_through:
        raise LedgerError(f"{where}: {d['date']} is in a closed period")
    purchase = d["direction"] == "in"
    sign = Decimal(1 if purchase else -1) * (-1 if d["kind"] == "credit_note" else 1)
    party = dd.get("party_account") or sug.get("party_account")
    if not party or party not in book.accounts:
        raise LedgerError(f"{where}: party account {party} is not opened. Add it under accounts")
    if book.role_of(party) != ("ap" if purchase else "ar"):
        raise LedgerError(f"{where}: {party} is not a {'supplier (ap)' if purchase else 'customer (ar)'} account")
    overrides = {o.get("index"): o for o in dd.get("lines") or []}
    basis_of = [(overrides.get(i) or {}).get("amount_is") or ln.get("amount_is") or "gross" for i, ln in enumerate(d["lines"])]
    unknown = "unknown" in basis_of
    if unknown and d["total"] is None:
        raise LedgerError(f"{where}: amount_is unknown needs the document total to tell net from gross. "
                          "Give amount_is for the lines in the decisions")
    if d["discount"] and d["total"] is None:
        raise LedgerError(f"{where}: a discount without the document total. Give the total")
    can_discount = bool(d["discount"]) and d["lines_from"] == "document"
    fits, last = [], None
    for guess in (("net", "gross") if unknown else ("gross",)):
        for discounted in ((True, False) if can_discount else (False,)):
            try:
                work, payable = doc_work(book, d, overrides, sug, [guess if b == "unknown" else b for b in basis_of],
                                         discounted, where)
            except LedgerError as exc:
                last = exc
                continue
            if d["total"] is None or payable == d["total"]:
                fits.append((discounted, work))
            else:
                last = LedgerError(f"{where}: lines and tax give {payable}, the document total is {d['total']}"
                                   + (". Neither net nor gross lines fit" if unknown else ""))
    if not fits:
        raise last
    if len({tuple((w["account"], w["amount"], w["tax_amt"]) for w in f[1]) for f in fits}) > 1:
        raise LedgerError(f"{where}: the lines fit the total in more than one way (net or gross, with or without the "
                          "discount). Give amount_is for the lines in the decisions")
    discounted, work = fits[0]
    prec = d["prec"]
    rate = d.get("fx_rate", Decimal(1))
    conv = (lambda x: q(x * rate, book.prec)) if d["currency"] != book.currency else (lambda x: x)
    posts = []
    point = d["tax_point"] or d["date"]
    confirmed = dd.get("confirmed", "document" if not dd.get("lines") else "no")
    for w in work:
        line = {"account": w["account"], "amount_is": "net" if w["net"] else "gross",
                **({"asset": w["asset"]} if w["asset"] else {})}
        if w["tax"] is not None:
            line["tax"] = {**{k: v for k, v in w["tax"].items() if k not in ("amount", "point", "direction")},
                           "amount": format(sign * conv(w["tax_amt"]), "f"), "point": point.isoformat(),
                           "direction": "purchase" if purchase else "sale"}
            if confirmed == "user":
                line["tax"] = confirm_tax(line["tax"])
        posts += expand_line(book, line, sign * conv(w["amount"]), d["date"], where, require_tax=True, default_point=point)
    total = sum((p.amount for p in posts), Decimal(0))
    if d["currency"] != book.currency and d["total"] is not None:
        expected = sign * conv(d["total"])
        diff = expected - total
        if diff and abs(diff) <= Decimal(1).scaleb(-book.prec) * len(work):
            big = max((p for p in posts if "tax_part" not in p.meta), key=lambda p: abs(p.amount))
            big.amount += diff
            big.meta["fx_rounding"] = fmt(diff, book.prec)
            total = expected
    pmeta = {}
    applies = dd.get("applies_to", raw.get("applies_to"))
    if d["kind"] == "credit_note" and applies:
        pmeta["allocations_json"] = [{"document": applies, "amount": fmt(-total, book.prec)}]
    else:
        pmeta = {"document": d["id"], "document_number": d.get("number"), "due_date": d["due_date"],
                 "document_date": d["date"]}
    posts.append(Posting(party, -total, book.currency, pmeta))
    meta = entry_meta({"confirmed": confirmed, "classified_by": dd.get("classified_by") or "document",
                       "document": d["id"]}, where)
    meta.update({"document_kind": d["kind"], "document_number": d.get("number"), "document_date": d["date"].isoformat(),
                 "due_date": d["due_date"].isoformat() if d["due_date"] else None, "source": d.get("source")})
    if d.get("terms"):
        meta["terms_json"] = d["terms"]
    if discounted:
        meta["discount"] = fmt(d["discount"], prec)
    if d.get("linked"):
        meta["linked_documents_json"] = d["linked"]
    if d.get("url"):
        meta["document_url"] = d["url"]
    if d["party"].get("tax_id"):
        meta["party_tax_id"] = clean_text(d["party"]["tax_id"])
    if d["currency"] != book.currency:
        meta["fx_json"] = {"amount": format(d["total"] or 0, "f"), "currency": d["currency"], "rate": format(rate, "f")}
    party_name = clean_text(d["party"].get("name")) or party.rsplit(":", 1)[-1]
    flag = "!" if meta["confirmed"] == "no" else "*"
    return ser_txn(d["date"], flag, party_name, f"{d['kind'].replace('_', ' ')} {d.get('number') or ''}".strip(),
                   meta, posts, book.prec)


def build_commit_documents(book: Book, pending: dict, decisions: dict | None) -> Book:
    check_pending(book, pending)
    if pending.get("kind") != "documents":
        raise LedgerError("this pending file holds statements. Use commit")
    decisions = decisions or {}
    work, body = prepare_decisions(book, decisions)
    by_id = {}
    for d in decisions.get("documents") or []:
        if not isinstance(d, dict) or set(d) - DOC_DECISION_KEYS:
            raise LedgerError(f"document decision {d.get('id') if isinstance(d, dict) else d}: unknown fields "
                              f"{sorted(set(d) - DOC_DECISION_KEYS) if isinstance(d, dict) else ''}")
        for o in d.get("lines") or []:
            if not isinstance(o, dict) or set(o) - DOC_LINE_DECISION_KEYS or not isinstance(o.get("index"), int):
                raise LedgerError(f"document decision {d.get('id')}: each line override has an index and only "
                                  f"{sorted(DOC_LINE_DECISION_KEYS - {'index'})}")
        by_id[d.get("id")] = d
    unresolved, txns, name_only = [], [], []
    held = {h["document"] for h in book.holds}
    for item in pending["documents"]:
        dd = by_id.get(item["ref"])
        if item["status"] == "review_blocked":
            if not (dd and dd.get("review_resolved") is True):
                if item["ref"] not in held:
                    hd = parse_date(item["document"].get("date"), f"document {item['ref']} date")
                    body += custom_entries(work, {"review_holds": [
                        {"document": item["ref"], "date": hd.isoformat(), "note": "in Billpal Review when staged"}]})
                    held.add(item["ref"])
                continue
        elif item["status"] != "new":
            continue
        if dd and dd.get("decision") == "skip":
            continue
        if not dd and not decisions.get("accept_suggested"):
            unresolved.append(item["ref"])
            continue
        if item["suggested"].get("party_matched_by") == "name" and not (
                dd and (dd.get("party_account") or dd.get("party_confirmed") is True)):
            name_only.append(f"{item['ref']}: {item['suggested'].get('party_account')}")
            continue
        txns.append(doc_txn(work, item["document"], dd or {}, item["suggested"]))
    if unresolved:
        raise LedgerError([f"{len(unresolved)} documents have no decision yet:"] + unresolved[:30])
    if name_only:
        raise LedgerError(["The account was found by name only. Ask the user, then give party_account or "
                           "party_confirmed true:"] + name_only[:30])
    body += "\n" + "\n".join(txns)
    return write_next(book, body, clean_text(decisions.get("note") or f"{len(txns)} documents"))


# ── Matching candidates ──────────────────────────────────────────────────────

GENERIC_NAME_WORDS = {"gmbh", "ag", "kg", "og", "ug", "ltd", "inc", "llc", "co", "und", "and", "the", "der", "die",
                      "das", "e", "u", "mbh", "haftungsbeschraenkt", "gesellschaft", "group", "gruppe"}
BATCH_POOL_CAP = 20
BATCH_MAX_SIZE = 6


def doc_links(row: dict, doc: dict) -> list[str]:
    hay = norm_text(" ".join(str(row.get(k) or "") for k in ("text", "counterparty", "reference")))
    flat = re.sub(r"[^a-z0-9]", "", hay)
    links = []
    names = " ".join(str(doc.get(k) or "") for k in ("party_name", "company_name", "brand_name"))
    words = {w for w in norm_text(names).split() if len(w) > 2 and w not in GENERIC_NAME_WORDS}
    if words and any(re.search(rf"\b{re.escape(w)}\b", hay) for w in words):
        links.append("name")
    if doc.get("iban") and norm_iban(doc["iban"]) == norm_iban(row.get("iban")):
        links.append("iban")
    num = re.sub(r"[^a-z0-9]", "", (doc.get("number") or "").lower())
    if len(num) >= 3 and num in flat:
        links.append(f"number {doc.get('number')}")
    return links


def in_window(d: date, doc: dict) -> bool:
    if doc["kind"] == "receipt":
        return abs((d - doc["date"]).days) <= 7
    latest = max(doc["date"], doc.get("due") or doc["date"])
    return doc["date"] - timedelta(days=14) <= d <= latest + timedelta(days=42)


def parse_docs(book: Book, raws, need_rate: bool = False, skip_review: bool = False) -> tuple[list[dict], list[dict]]:
    """Normalized documents and the ones that could not be read, so one bad document never stops a run."""
    docs, skipped = [], []
    for raw in raws or []:
        if skip_review and isinstance(raw, dict) and raw.get("review_open"):
            continue
        try:
            docs.append(norm_doc(raw, book, need_rate=need_rate))
        except LedgerError as exc:
            skipped.append({"document": raw.get("id") if isinstance(raw, dict) else None, "reason": "; ".join(exc.messages)})
    return docs, skipped


def candidate_pool(book: Book, documents: dict | None, skipped: list | None = None) -> list[dict]:
    pool = []
    if book.basis == "accrual":
        items, _, _ = compute_items(book)
        for it in items.values():
            if it["remaining"] == 0:
                continue
            meta = book.accounts.get(it["account"], {}).get("meta", {})
            origin = next((t for t in book.txns if t.meta.get("txn_id") == it["txn_id"]), None)
            terms = json.loads(origin.meta["terms_json"]) if origin and origin.meta.get("terms_json") else None
            pool.append({
                "document": it["document"], "kind": (origin.meta.get("document_kind") if origin else None) or "invoice",
                "pay": it["remaining"], "full": it["amount"], "sign": 1 if it["remaining"] > 0 else -1,
                "date": it["date"], "due": opt_date(it["due"] or None),
                "number": it["number"], "party_name": meta.get("name") or it["party"], "iban": meta.get("iban"),
                "terms": terms, "account": it["account"], "fx": False, "currency": book.currency,
            })
        return pool
    booked = book.documents_in_ledger()
    docs, bad = parse_docs(book, (documents or {}).get("documents"), skip_review=True)
    if skipped is not None:
        skipped += bad
    kinds = {d["id"]: d["kind"] for d in docs}
    for d in docs:
        linked = set(d.get("linked") or [])
        if (d["id"] in booked or d["kind"] == "reminder" or d.get("review_open") or d["total"] is None
                or d["kind"] in ("invoice", "receipt") and linked & booked):
            continue
        sign = Decimal(-1 if d["direction"] == "in" else 1) * (-1 if d["kind"] == "credit_note" else 1)
        foreign, party = d["currency"] != book.currency, d["party"]
        pay = None if foreign and d["fx_rate"] is None else sign * (q(d["total"] * d["fx_rate"], book.prec) if foreign else d["total"])
        pool.append({
            "document": d["id"], "kind": d["kind"], "pay": pay, "full": pay, "sign": sign, "date": d["date"],
            "due": d["due_date"], "number": d.get("number"), "party_name": party.get("name"),
            "company_name": party.get("company_name"), "brand_name": party.get("brand_name"),
            "iban": party.get("iban"), "terms": d.get("terms"), "account": None, "fx": foreign,
            "currency": d["currency"], "original_total": d["total"],
            "receipts": sorted(x for x in linked if kinds.get(x) == "receipt") if d["kind"] != "receipt" else [],
            "invoices": sorted(x for x in linked if kinds.get(x) == "invoice") if d["kind"] == "receipt" else [],
            "derived_tax": derive_tax(d, book),
        })
    return pool


def explain(kind: str, paid: str, docs: list[dict], links: list[str], d: date) -> str:
    days = (d - docs[0]["date"]).days
    when = f"{abs(days)} days {'after' if days >= 0 else 'before'} the document"
    nums = ", ".join(x.get("number") or x["document"] for x in docs)
    return f"{kind}: payment {paid} for {nums}; links: {', '.join(links) or 'none'}; {when}"


PAIR_MAX = 4
PAIR_TIE_DAYS = 1


def pair_exact(rows: list[dict], dates: dict) -> None:
    """Pair same-amount payments and documents as a whole, by the least total date gap. Walking the rows
    by date and taking the closest free document steals: invoices 1 and 5 Jan, payments 4 and 6 Jan."""
    exact = {o["ref"]: [f for f in o["candidates"] if f["type"] == "exact"] for o in rows}
    by_doc, when = defaultdict(set), {o["ref"]: parse_date(o["date"]) for o in rows}
    for ref, fs in exact.items():
        for f in fs:
            by_doc[f["documents"][0]].add(ref)
    seen = set()
    for start in exact:
        if start in seen or not exact[start]:
            continue
        refs, docs, stack = set(), set(), [start]
        while stack:
            ref = stack.pop()
            if ref not in refs:
                refs.add(ref)
                for f in exact[ref]:
                    docs.add(f["documents"][0])
                    stack.extend(by_doc[f["documents"][0]])
        seen |= refs
        if len(refs) == 1 and len(docs) == 1:
            continue
        refs = sorted(refs)
        if len(refs) > PAIR_MAX or len(docs) > PAIR_MAX:
            for ref in refs:
                for f in exact[ref]:
                    f["needs_user"] = True
            continue
        scored = []
        for pick in product(*[exact[ref] + [None] for ref in refs]):
            used = [f["documents"][0] for f in pick if f]
            if len(used) == len(set(used)):
                gap = sum(abs((when[ref] - dates[f["documents"][0]]).days) for ref, f in zip(refs, pick) if f)
                scored.append((-len(used), gap, pick))
        scored.sort(key=lambda s: s[:2])
        best = scored[0]
        tie = len(scored) > 1 and scored[1][0] == best[0] and scored[1][1] - best[1] <= PAIR_TIE_DAYS
        for o in rows:
            if o["ref"] not in refs:
                continue
            f = best[2][refs.index(o["ref"])]
            if tie:
                for g in exact[o["ref"]]:
                    g["needs_user"] = True
                continue
            o["candidates"] = [g for g in o["candidates"] if g["type"] != "exact" or g is f]
            if f:
                f["needs_user"] = False


def candidates(book: Book, pending: dict, documents: dict | None) -> dict:
    if pending.get("kind") != "statement":
        raise LedgerError("candidates works on a staged statement")
    skipped = []
    pool = candidate_pool(book, documents, skipped)
    out, seen = [], set()
    for st in pending["statements"]:
        for r in st["rows"]:
            if r["status"] not in ("new", "ambiguous"):
                continue
            amt, d, fee = dec(r["amount"]), parse_date(r["booking_date"]), is_fee_row(r)
            card = is_card_row(st, r)
            # Paid amounts by currency: the ledger amount, plus the original one on a foreign card payment.
            got_by = {book.currency: abs(amt)}
            fx = r.get("fx") or {}
            if fx.get("currency") and fx.get("amount") not in (None, ""):
                got_by[str(fx["currency"]).upper()] = abs(dec(fx["amount"]))
            found, too_many, linked = [], False, []

            def hit(kind, docs, links, needs_user, want, got, cur):
                prec = book.prec if cur == book.currency else precision_for(cur)
                paid = fmt(got, prec) + ("" if cur == book.currency else f" {cur}")
                f = {"type": kind, "documents": [x["document"] for x in docs], "needs_user": needs_user,
                     "open_amount": fmt(want, prec), "difference": fmt(want - got, prec),
                     "explanation": explain(kind, paid, docs, links, d)}
                if cur != book.currency:
                    f["currency"] = cur
                receipts = [x for doc in docs for x in doc.get("receipts") or []]
                if receipts:
                    f["linked_receipts"] = receipts
                if len(docs) == 1 and docs[0].get("derived_tax"):
                    f["derived_tax"] = docs[0]["derived_tax"]
                return f

            for doc in [] if fee else pool:
                if (doc["sign"] > 0) != (amt > 0) or not in_window(d, doc):
                    continue
                if doc["currency"] != book.currency and doc["currency"] in got_by:
                    cur, want = doc["currency"], abs(doc["original_total"])
                elif doc["pay"] is not None:
                    cur, want = book.currency, abs(doc["pay"])
                else:
                    continue
                links = doc_links(r, doc)
                if not links or not want:
                    continue
                linked.append((doc, links, cur, want))
                got = got_by[cur]
                prec = book.prec if cur == book.currency else precision_for(cur)
                unit = Decimal(1).scaleb(-prec)
                fx_gap = doc["fx"] and cur == book.currency
                kind, needs_user = None, True
                if got == want:
                    kind, needs_user = "exact", False
                elif got < want:
                    terms = doc.get("terms") or {}
                    pct = dec(terms["skonto_percent"]) if terms.get("skonto_percent") is not None else None
                    in_time = terms.get("skonto_days") is not None and d <= doc["date"] + timedelta(days=int(terms["skonto_days"]))
                    if pct is not None and pct <= 3 and abs(q(want * (1 - pct / 100), prec) - got) <= unit and in_time:
                        kind, needs_user = "skonto", False
                    elif (want - got) / want <= Decimal("0.03"):
                        # A card payment is never paid with a cash discount: a small gap is a partial or a rounding.
                        kind = "fx_difference" if fx_gap else "partial" if card else "skonto_unconfirmed"
                    else:
                        kind = "partial"
                elif fx_gap and (got - want) / want <= Decimal("0.03"):
                    kind = "fx_difference"
                if kind:
                    found.append(hit(kind, [doc], links, needs_user, want, got, cur))
            # A receipt counts as its invoice where the invoice is offered too; elsewhere (outside the
            # invoice's window, or the invoice in review) the receipt is offered on its own.
            offered = {x[0]["document"] for x in linked}
            seen |= offered
            drop = {x[0]["document"] for x in linked if set(x[0].get("invoices") or []) & offered}
            linked = [x for x in linked if x[0]["document"] not in drop]
            found = [f for f in found if f["documents"][0] not in drop]
            if not any(f["type"] == "exact" for f in found) and len(linked) >= 2:
                if len(linked) > BATCH_POOL_CAP:
                    too_many = True
                else:
                    hits = []
                    for cur in sorted({x[2] for x in linked}):
                        vals = [x for x in linked if x[2] == cur]
                        for size in range(2, min(BATCH_MAX_SIZE, len(vals)) + 1):
                            for combo in combinations(vals, size):
                                if sum((v[3] for v in combo), Decimal(0)) == got_by[cur]:
                                    hits.append((cur, combo))
                                    if len(hits) > 5:
                                        break
                            if len(hits) > 5:
                                break
                    for cur, combo in hits[:5]:
                        links = sorted({l for c in combo for l in c[1]})
                        found.append(hit("batch", [c[0] for c in combo], links, len(hits) > 1, got_by[cur], got_by[cur], cur))
            order = {"exact": 0, "skonto": 1, "batch": 2, "skonto_unconfirmed": 3, "fx_difference": 4, "partial": 5}
            found.sort(key=lambda f: (order[f["type"]], f["explanation"]))
            out.append({"ref": r["ref"], "date": r["booking_date"], "amount": r["amount"],
                        "text": r.get("counterparty") or r.get("text"), "candidates": found, "too_many_candidates": too_many,
                        **({"bank_fee": True} if fee else {})})
    pair_exact(out, {doc["document"]: doc["date"] for doc in pool})
    held = [] if book.basis == "accrual" else [
        {"document": d.get("id"), "date": d.get("date"), "total": d.get("total"), "party": (d.get("party") or {}).get("name")}
        for d in (documents or {}).get("documents") or [] if d.get("review_open") and d.get("id") not in book.documents_in_ledger()]
    # Cash basis: a cost document in the statements' span that no row pays was likely paid privately or
    # from another account. The skill asks; it never books these on its own.
    spans = [(parse_date(st["from"]), parse_date(st["to"])) for st in pending["statements"] if st.get("from") and st.get("to")]
    unpaid = [] if book.basis == "accrual" or not spans else [
        {"document": doc["document"], "date": doc["date"].isoformat(), "party_name": doc["party_name"],
         "number": doc["number"], "total": fmt(abs(doc["original_total"]), precision_for(doc["currency"])),
         "currency": doc["currency"]}
        for doc in pool if doc["sign"] < 0 and doc["document"] not in seen
        and min(a for a, _ in spans) <= doc["date"] <= max(b for _, b in spans)]
    return {"rows": out, "pool_size": len(pool), "held_in_review": held, "skipped_documents": skipped,
            "documents_without_payment": unpaid}


# ── Billpal write-back ───────────────────────────────────────────────────────

def writeback(book: Book, documents: dict | None) -> dict:
    """Payment updates for Billpal documents that the ledger proves: the cumulative amount paid, the
    date of the last payment, and the method only when the bank statement shows it. A foreign-currency
    document is compared and reported in its own currency, from the original amount on the bank row."""
    money_of = lambda t: sum((p.amount for p in t.postings if book.role_of(p.account) in MONEY_ROLES), Decimal(0))
    live = {t.meta.get("txn_id") for t in book.live_txns()}
    parsed, skipped = parse_docs(book, (documents or {}).get("documents"))
    given = {d["id"]: d for d in parsed if d["total"] is not None}
    cur_of = lambda doc: given[doc]["currency"] if doc in given else book.currency
    prec_of = lambda doc: book.prec if cur_of(doc) == book.currency else precision_for(cur_of(doc))

    def original(t, doc):
        if cur_of(doc) == book.currency:
            return None
        fx = json.loads(t.meta["fx_json"], parse_float=Decimal) if t.meta.get("fx_json") else {}
        if str(fx.get("currency") or "").upper() != cur_of(doc) or fx.get("amount") in (None, ""):
            return None
        return abs(dec(fx["amount"]))

    # pays[doc]: (date, money paid, discount taken, method, paid in the document's currency when foreign);
    # prior[doc]: paid before this ledger, None if unknown
    pays, settled, unsplit, prior = defaultdict(list), {}, set(), {}
    if book.basis == "accrual":
        items, _, _ = compute_items(book)
        docs_in = defaultdict(set)
        for t in book.live_txns():
            for p in t.postings:
                for a in json.loads(p.meta["allocations_json"]) if p.meta.get("allocations_json") else []:
                    if a.get("document"):
                        docs_in[t.meta.get("txn_id")].add(a["document"])
        for doc, it in items.items():
            origin = book.txn_by_id.get(it["txn_id"])
            carried = bool(origin and origin.meta.get("opening"))
            if cur_of(doc) != book.currency:
                total, prior[doc] = given[doc]["total"], None if carried else Decimal(0)
            else:
                total = given[doc]["total"] if doc in given else (None if carried else abs(it["amount"]))
                prior[doc] = None if total is None else total - abs(it["amount"])
            settled[doc] = (total, it["remaining"] == 0)
            for a in it["allocs"]:
                t = book.txn_by_id.get(a["txn_id"])
                if t is None or a["txn_id"] not in live or t.meta.get("document_kind"):
                    continue
                cash, alloc = abs(money_of(t)), abs(a["amount"])
                alone = len(docs_in[a["txn_id"]]) <= 1
                if not cash or not alone:
                    cash = alloc
                pays[doc].append((t.date, min(cash, alloc), max(alloc - cash, Decimal(0)), t.meta.get("payment_method"),
                                  original(t, doc) if alone else None))
    else:
        settles = set()
        for t in book.live_txns():
            docs = [t.meta["document"]] if t.meta.get("document") else json.loads(t.meta.get("documents_json") or "[]")
            cash = abs(money_of(t))
            if not cash or not docs:
                continue
            if len(docs) > 1:
                unsplit.update(docs)
                continue
            pays[docs[0]].append((t.date, cash, Decimal(0), t.meta.get("payment_method"), original(t, docs[0])))
            if t.meta.get("settles") == "yes":
                settles.add(docs[0])
        for doc, rows in pays.items():
            d, foreign = given.get(doc), cur_of(doc) != book.currency
            total = d["total"] if d else None
            # Cash books keep no open items: payments from an earlier year are only known for this year's documents.
            prior[doc] = Decimal(0) if d and d["date"] >= book.fy_from else None
            if foreign and any(r[4] is None for r in rows):
                settled[doc] = (total, False)
                continue
            cash = sum((r[4] if foreign else r[1] for r in rows), Decimal(0))
            unit = Decimal(1).scaleb(-prec_of(doc))
            terms = (d or {}).get("terms") or {}
            last = max(r[0] for r in rows)
            skonto = (total is not None and terms.get("skonto_percent") is not None and terms.get("skonto_days") is not None
                      and dec(terms["skonto_percent"]) <= 3 and last <= d["date"] + timedelta(days=int(terms["skonto_days"]))
                      and abs(q(total * (1 - dec(terms["skonto_percent"]) / 100), prec_of(doc)) - cash) <= unit)
            full = total is not None and (cash >= total or skonto or doc in settles)
            settled[doc] = (total, full)
            if full and cash < total:
                pays[doc].append((last, Decimal(0), total - cash, None, Decimal(0)))
    out = []
    for doc in sorted(set(pays) | unsplit):
        if not doc.startswith("billpal:"):
            continue
        uid, rows = doc.split(":", 1)[1], pays.get(doc, [])
        cur, prec = cur_of(doc), prec_of(doc)
        foreign = cur != book.currency
        paid = sum((r[1] for r in rows), Decimal(0))
        entry = {"document": doc, "paid": fmt(paid, book.prec), "calls": []}
        if doc in unsplit:
            entry["note"] = "paid together with other documents in one booking; the split is not in the ledger"
            out.append(entry)
            continue
        if not paid:
            continue
        if foreign and any(r[1] and r[4] is None for r in rows):
            entry["note"] = (f"paid {fmt(paid, book.prec)} {book.currency}, but the bank row has no original {cur} "
                             f"amount. Compare with amount_paid in get_document before writing")
            out.append(entry)
            continue
        if foreign:
            paid = sum((r[4] or Decimal(0) for r in rows if r[1]), Decimal(0))
            entry.update({"paid": fmt(paid, prec), "currency": cur})
        total, full = settled.get(doc, (None, False))
        last = max(r[0] for r in rows).isoformat()
        methods = {r[3] for r in rows if r[1]}
        method = next(iter(methods)) if len(methods) == 1 and None not in methods else None
        pay = {"document_ids": [uid], "payment_date": last, **({"payment_method": method} if method else {})}
        discount = sum((r[2] for r in rows), Decimal(0))
        disc_cur, disc_prec = (cur, prec) if book.basis == "cash" else (book.currency, book.prec)
        if total is None and book.basis == "cash":
            entry["note"] = "needs the document total (--documents) to say paid or partially paid"
        elif full:
            entry["calls"].append({"tool": "set_payment", "arguments": {**pay, "payment_status": "paid"}})
            if discount:
                entry["calls"].append({"tool": "add_document_note", "arguments": {
                    "document_ids": [uid], "body": f"Skonto {fmt(discount, disc_prec)} {disc_cur} taken, "
                                                   f"paid {fmt(paid, prec)} {cur} on {last}"}})
        elif prior.get(doc) is None:
            entry["note"] = ("partly paid, but what was paid before this ledger is unknown. Give --documents with the "
                             "total, or compare with amount_paid in get_document before writing")
        else:
            cumulative = prior[doc] + paid
            entry["paid"] = fmt(cumulative, prec)
            entry["calls"].append({"tool": "set_amount_paid", "arguments": {"document_id": uid, "amount_paid": float(cumulative)},
                                   "needs": "expected_updated_at from get_document"})
            entry["calls"].append({"tool": "set_payment", "arguments": {**pay, "payment_status": "partially_paid"}})
        entry["total"] = None if total is None else fmt(total, prec)
        out.append(entry)
    return {"documents": out, "skipped_documents": skipped}


# ── Assets and depreciation ──────────────────────────────────────────────────

def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month + 1


def year_amount(a: dict, book: Book) -> Decimal:
    base = a["cost"] - a["residual"]
    remaining = base - a["prior"]
    if remaining <= 0:
        return Decimal(0)
    if a["method"] == "gwg":
        return remaining
    annual = base * 12 / a["life_months"]
    start = max(a["in_service"], book.fy_from)
    end = min(a["disposed"], book.fy_to) if a["disposed"] else book.fy_to
    months = months_between(start, end)
    if a["in_service"] >= book.fy_from:
        if a["first_year"] == "monthly":
            amount = annual * months / 12
        elif a["first_year"] == "half_year":
            amount = annual if months > 6 else annual / 2
        else:
            amount = annual
    elif months_between(book.fy_from, book.fy_to) < 12 or a["disposed"]:
        amount = annual * months / 12
    else:
        amount = annual
    return q(min(amount, remaining), book.prec)


def depreciation_plan(book: Book) -> list[dict]:
    rows = []
    for aid, raw in sorted(book.assets.items()):
        a = validate_asset(raw, book)
        if (a["disposed"] and a["disposed"] < book.fy_from) or a["in_service"] > book.fy_to:
            continue
        credit = {a["asset_account"], a["accumulated_account"]}
        booked = -sum((p.amount for t in book.live_txns() if t.meta.get("depreciation") == "yes"
                       for p in t.postings if p.meta.get("asset") == aid and p.account in credit), Decimal(0))
        due = year_amount(a, book)
        rows.append({"asset": aid, "description": raw.get("description", ""), "cost": fmt(a["cost"], book.prec),
                     "prior": fmt(a["prior"], book.prec), "due": fmt(due, book.prec), "booked": fmt(booked, book.prec),
                     "to_book": due - booked, "book_value_end": fmt(a["cost"] - a["prior"] - due, book.prec),
                     "_a": a})
    return rows


def build_depreciation(book: Book) -> tuple[list, str]:
    plan = depreciation_plan(book)
    body = []
    if book.closed_through and book.closed_through >= book.fy_to:
        raise LedgerError("the year is closed. Depreciation must be booked before closing")
    for r in plan:
        amt = r["to_book"]
        if amt == 0:
            continue
        a = r["_a"]
        business = q(amt * (1 - a["private_share"]), book.prec)
        posts = [Posting(a["depreciation_account"], business, book.currency, {"asset": a["id"]})]
        if amt - business:
            posts.append(Posting(a["private_account"], amt - business, book.currency, {"asset": a["id"]}))
        posts.append(Posting(a["accumulated_account"] or a["asset_account"], -amt, book.currency, {"asset": a["id"]}))
        meta = {"txn_id": new_txn_id(), "depreciation": "yes", "asset": a["id"], "classified_by": "script",
                "confirmed": "user", "no_document": "depreciation"}
        body.append(ser_txn(book.fy_to, "*", "Depreciation", r["description"] or a["id"], meta, posts, book.prec))
    return [{k: (fmt(v, book.prec) if isinstance(v, Decimal) else v) for k, v in r.items() if k != "_a"} for r in plan], "\n".join(body)


# ── Year-end rollover ────────────────────────────────────────────────────────

def own_balance(book: Book, account: str) -> Decimal:
    dates, cum = book._index().get(account, ([], []))
    return cum[-1] if cum else Decimal(0)


def result_split(book: Book) -> tuple[dict, dict]:
    """Where the year's result and the owners' draws go: one retained_earnings account, or in a partnership
    one capital account per partner (`partner`, `share`) that also takes that partner's draws."""
    caps = book.accounts_with_role("retained_earnings")
    partners = {a: book.accounts[a]["meta"] for a in caps if book.accounts[a]["meta"].get("partner")}
    if not partners:
        if len(caps) != 1:
            raise LedgerError("Need exactly one retained_earnings account for the rollover, or one per partner with partner and share")
        return {caps[0]: Decimal(1)}, {}
    if len(partners) != len(caps):
        raise LedgerError("In a partnership every retained_earnings account needs partner and share")
    shares = {a: dec(m.get("share") or "0") for a, m in partners.items()}
    if sum(shares.values()) != 1:
        raise LedgerError(f"Partner shares add up to {sum(shares.values())}, not 1")
    capital = {m["partner"]: a for a, m in partners.items()}
    draws = {}
    for a in book.accounts:
        if book.role_of(a) == "private":
            who = book.accounts[a]["meta"].get("partner")
            if who not in capital:
                raise LedgerError(f"{a}: name the partner (partner) so the draws close into their capital account")
            draws[a] = capital[who]
    return shares, draws


def build_rollover(book: Book, to: date | None, label: str | None, write: bool) -> dict:
    rep = readiness(book, None, year_end=True)
    new_from = book.fy_to + timedelta(days=1)
    new_to = to or (add_months(new_from, 12) - timedelta(days=1))
    shares, draws = result_split(book)
    re_acct = next(iter(shares))
    items, _, _ = compute_items(book)
    result = profit = Decimal(0)
    lines = {}
    for acct in sorted(book.accounts):
        bal = own_balance(book, acct)
        root, role = acct.split(":")[0], book.role_of(acct)
        if acct in draws:
            lines[draws[acct]] = lines.get(draws[acct], Decimal(0)) + bal
            continue
        if root in ("Income", "Expenses") or role == "private":
            result += bal
            profit += bal if root in ("Income", "Expenses") else 0
            continue
        if role in PARTY_ROLES and book.basis == "accrual":
            continue
        if bal:
            lines[acct] = lines.get(acct, Decimal(0)) + bal
    left = result
    for i, (acct, share) in enumerate(sorted(shares.items())):
        part = left if i == len(shares) - 1 else q(result * share, book.prec)
        left -= part
        lines[acct] = lines.get(acct, Decimal(0)) + part
    opening = [{"account": a, "amount": fmt(v, book.prec)} for a, v in lines.items() if v]
    for it in sorted(items.values(), key=lambda i: (i["account"], i["date"], i["document"])):
        if it["remaining"]:
            opening.append({"account": it["account"], "amount": fmt(it["remaining"], book.prec), "document": it["document"],
                            "document_number": it["number"] or None, "document_date": it["date"].isoformat(),
                            "due_date": it["due"] or None, "party": it["party"] or None})
    accounts = [{"account": a, **{k: v for k, v in i["meta"].items() if k in ACCOUNT_SPEC_KEYS}}
                for a, i in sorted(book.accounts.items()) if not i["close"]]
    assets = []
    for r in depreciation_plan(book):
        a, raw = r["_a"], dict(book.assets[r["asset"]])
        prior = a["prior"] + dec(r["booked"])
        if prior < a["cost"] - a["residual"] and not a["disposed"]:
            raw["prior_depreciation"] = fmt(prior, book.prec)
            assets.append(raw)
    preview = {
        "ready": rep["ready"], "fails": rep["fails"], "new_fiscal_year": [new_from.isoformat(), new_to.isoformat()],
        "result_to": re_acct if len(shares) == 1 else sorted(shares), "result": fmt(-profit, book.prec), "draws": fmt(result - profit, book.prec), "opening_lines": opening,
        "open_items": sum(1 for i in items.values() if i["remaining"]), "assets_carried": len(assets),
    }
    if not write:
        return preview
    if not rep["ready"]:
        raise LedgerError(["the year is not ready for rollover:"] + [f"{f['code']}: {f['message']}" for f in rep["fails"]])
    spec = {
        "title": book.options.get("title"), "file_label": label or str(new_from.year),
        "ledger": {"fiscal_year_from": new_from.isoformat(), "fiscal_year_to": new_to.isoformat(),
                   "currency": book.currency, "precision": book.prec},
        "profile": book.profile, "accounts": accounts,
        "opening": {"lines": opening, "suspense_confirmed": any(book.role_of(l["account"]) == "suspense" for l in opening)},
        "rules": [r for r in book.rules.values() if not r.get("disabled")], "csv_maps": list(book.csv_maps.values()),
        "assets": assets,
        "allocations": [a for a in book.allocations.values() if not a.get("disabled")
                        and not (a.get("to") and parse_date(a["to"]) < new_from)],
        "previous": {"id": book.ledger_id, "version": book.version, "sha256": book.sha, "file": book.path.name},
    }
    nb = build_init(spec, book.path.parent)
    preview["written"] = nb.path.name
    return preview


# ── Reports ──────────────────────────────────────────────────────────────────

def period_key(d: date, by: str) -> str:
    if by == "month":
        return f"{d.year}-{d.month:02d}"
    if by == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    return str(d.year)


def column_spans(start: date, end: date, by: str) -> list[tuple[str, date, date]]:
    if by == "none":
        return [("total", start, end)]
    step = 1 if by == "month" else 3
    cur = date(start.year, start.month, 1) if by == "month" else date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    cols = []
    while cur <= end:
        nxt = add_months(cur, step)
        cols.append((period_key(cur, by), max(cur, start), min(nxt - timedelta(days=1), end)))
        cur = nxt
    return cols


def account_label(book: Book, acct: str) -> tuple[str, str]:
    meta = book.accounts.get(acct, {}).get("meta", {})
    return meta.get("code", ""), meta.get("name", "") or acct.rsplit(":", 1)[-1]


def vat_schedule(book: Book, start: date, end: date) -> dict:
    """VAT by period on the tax point. Accrual books on payment basis recognise VAT when items are settled."""
    by = book.profile.get("vat_period") or "year"
    by = "year" if by == "none" else by
    # Payment basis on accrual books: a document's VAT follows the payments allocated to it, also after
    # the document is reversed, and the reversal takes back what was recognised on its own date. So a
    # correction never moves tax into a period that is already reported. (A document with payments
    # cannot be reversed before they are, so a rebook never inherits earlier payments.)
    paid = defaultdict(list)
    for t in book.txns:
        for p in t.postings:
            if book.role_of(p.account) in PARTY_ROLES and "allocations_json" in p.meta:
                for a in json.loads(p.meta["allocations_json"]):
                    if a.get("document"):
                        paid[a["document"]].append((t.date, dec(a["amount"])))

    def payment_schedule(t: Entry) -> list | None:
        orig = book.txn_by_id.get(t.meta["reverses"]) if t.meta.get("reverses") else t
        if orig is None or not orig.meta.get("document_kind"):
            return None
        doc = orig.meta.get("document")
        amount = sum((p.amount for p in orig.postings if book.role_of(p.account) in PARTY_ROLES
                      and p.meta.get("document") and "allocations_json" not in p.meta), Decimal(0))
        if not amount:
            return None
        if orig is not t:
            return [(t.date, -a / amount) for d, a in paid[doc] if d < t.date]
        rev = book.reversed.get(t.meta.get("txn_id"))
        return [(d, -a / amount) for d, a in paid[doc] if rev is None or d < rev.date]

    rows = defaultdict(lambda: {"base": Decimal(0), "tax": Decimal(0), "input_tax": Decimal(0)})
    codes = defaultdict(Decimal)
    for t in book.txns:
        for p in t.postings:
            if "tax_regime" not in p.meta:
                continue
            part = p.meta.get("tax_part") or "base"
            direction = p.meta.get("tax_direction") or ("sale" if p.account.startswith("Income") else "purchase")
            sched = [(parse_date(p.meta.get("tax_point") or t.date.isoformat()), Decimal(1))]
            if book.basis == "accrual" and book.vat_basis_for(direction) == "payment":
                sched = ps if (ps := payment_schedule(t)) is not None else sched
            value = dec(p.meta["tax_base"]) if part == "base" else p.amount
            sign = Decimal(-1 if direction == "sale" else 1)
            for d, frac in sched:
                if not start <= d <= end:
                    continue
                key = (period_key(d, by), p.meta.get("tax_type", "vat"), direction, p.meta["tax_regime"],
                       p.meta.get("tax_rate", "0"), p.meta.get("tax_jurisdiction") or "")
                if part == "base":
                    rows[key]["base"] += sign * value * frac
                elif part == "tax":
                    rows[key]["tax"] += -value * frac
                else:
                    rows[key]["input_tax"] += value * frac
                if part == "base" and p.meta.get("report_codes_json"):
                    tax_amt = dec(p.meta.get("tax_amount", "0"))
                    rec = dec(p.meta.get("recoverable", "1"))
                    for c in json.loads(p.meta["report_codes_json"]):
                        v = {"base": value, "tax": tax_amt, "input_tax": tax_amt * rec}[c["part"]]
                        codes[(period_key(d, by), c["code"])] += sign * v * frac
    prec = book.prec
    lines = [{"period": k[0], "type": k[1], "direction": k[2], "regime": k[3], "rate": k[4], "jurisdiction": k[5] or None,
              "base": q(v["base"], prec), "tax": q(v["tax"], prec), "input_tax": q(v["input_tax"], prec)}
             for k, v in sorted(rows.items())]
    periods = defaultdict(lambda: {"tax": Decimal(0), "input_tax": Decimal(0)})
    for ln in lines:
        periods[(ln["period"], ln["type"], ln["jurisdiction"] or "")]["tax"] += ln["tax"]
        periods[(ln["period"], ln["type"], ln["jurisdiction"] or "")]["input_tax"] += ln["input_tax"]
    return {
        "period": by, "lines": lines, "types": sorted({ln["type"] for ln in lines}),
        "codes": [{"period": k[0], "code": k[1], "amount": q(v, prec)} for k, v in sorted(codes.items())],
        "totals": [{"period": p, "type": ty, "jurisdiction": j or None, "tax": v["tax"], "input_tax": v["input_tax"],
                    "payable": v["tax"] - v["input_tax"], "due": period_due(book, p, by) if ty in VAT_LIKE else None}
                   for (p, ty, j), v in sorted(periods.items())],
    }


def build_report(book: Book, start: date, end: date, by: str, lang: str) -> dict:
    prec = book.prec
    cols = column_spans(start, end, by)
    tb, pnl, bs = [], [], {"Assets": [], "Liabilities": [], "Equity": []}
    sums = {"debit": Decimal(0), "credit": Decimal(0)}
    period_moves = defaultdict(lambda: defaultdict(Decimal))
    opening_entries = defaultdict(Decimal)
    for t in book.txns:
        if not start <= t.date <= end:
            continue
        for p in t.postings:
            if t.meta.get("opening"):
                opening_entries[p.account] += p.amount
            else:
                period_moves[p.account]["debit" if p.amount > 0 else "credit"] += abs(p.amount)
    for acct in sorted(book.accounts):
        code, name = account_label(book, acct)
        root = acct.split(":")[0]
        opening = own_before(book, acct, start) + opening_entries[acct]
        closing = own_before(book, acct, end + timedelta(days=1))
        mv = period_moves.get(acct, {})
        debit, credit = mv.get("debit", Decimal(0)), mv.get("credit", Decimal(0))
        if opening or debit or credit or closing:
            tb.append({"account": acct, "code": code, "name": name, "opening": q(opening, prec), "debit": q(debit, prec),
                       "credit": q(credit, prec), "closing": q(closing, prec)})
            sums["debit"] += debit
            sums["credit"] += credit
        if root in ("Income", "Expenses"):
            vals = [-(own_before(book, acct, b + timedelta(days=1)) - own_before(book, acct, a)) for _, a, b in cols]
            if any(vals):
                pnl.append({"account": acct, "code": code, "name": name, "group": root, "values": vals,
                            "total": sum(vals, Decimal(0))})
        elif closing:
            bs[root].append({"account": acct, "code": code, "name": name, "amount": closing if root == "Assets" else -closing})
    result = sum((r["total"] for r in pnl), Decimal(0))
    assets_total = sum((r["amount"] for r in bs["Assets"]), Decimal(0))
    liab_total = sum((r["amount"] for r in bs["Liabilities"]), Decimal(0))
    eq_total = sum((r["amount"] for r in bs["Equity"]), Decimal(0))
    fy_result = -sum((own_before(book, a, end + timedelta(days=1)) for a in book.accounts
                      if a.split(":")[0] in ("Income", "Expenses")), Decimal(0))
    reconciled = sums["debit"] == sums["credit"] and assets_total == liab_total + eq_total + fy_result
    items, unalloc, _ = compute_items(book, end)
    open_items = []
    for it in sorted(items.values(), key=lambda i: (i["account"], i["date"])):
        if not it["remaining"]:
            continue
        due = opt_date(it["due"] or None) or it["date"]
        days = (end - due).days
        bucket = "not_due" if days < 0 else "0-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
        side = "receivable" if book.role_of(it["account"]) == "ar" else "payable"
        open_items.append({"side": side, "document": it["document"], "number": it["number"], "party": it["party"],
                           "account": it["account"], "date": it["date"].isoformat(), "due": due.isoformat(),
                           "amount": abs(it["amount"]), "open": abs(it["remaining"]), "days_overdue": max(days, 0),
                           "bucket": bucket})
    assets = [{k: v for k, v in r.items() if k != "_a"} for r in depreciation_plan(book)]
    for a in assets:
        a["to_book"] = fmt(a["to_book"], prec)
    rep = readiness(book, min(end, book.fy_to), year_end=end >= book.fy_to)
    missing = [{"date": t.date.isoformat(), "payee": t.payee, "amount": money_amount(book, t),
                "account": ", ".join(dict.fromkeys(p.account for p in t.postings if book.role_of(p.account)
                                                   not in MONEY_ROLES | {"vat_in", "vat_out"})),
                "narration": t.narration, "txn_id": t.meta.get("txn_id", "")}
               for t in missing_receipt_txns(book, [t for t in book.live_txns() if t.date <= min(end, book.fy_to)])]
    journal = []
    for t in sorted(book.txns, key=lambda t: (t.date, t.line)):
        if not start <= t.date <= end:
            continue
        for p in t.postings:
            code, _ = account_label(book, p.account)
            journal.append({"date": t.date.isoformat(), "txn_id": t.meta.get("txn_id", ""), "payee": t.payee,
                            "narration": t.narration, "account": p.account, "code": code, "amount": p.amount,
                            "tax_type": p.meta.get("tax_type", ""), "tax_regime": p.meta.get("tax_regime", ""),
                            "tax_rate": p.meta.get("tax_rate", ""), "tax_part": p.meta.get("tax_part", ""),
                            "tax_point": p.meta.get("tax_point", ""),
                            "document": p.meta.get("document") or t.meta.get("document")
                            or ", ".join(json.loads(t.meta.get("documents_json") or "[]")),
                            "document_url": t.meta.get("document_url", ""),
                            "confirmed": t.meta.get("confirmed", "")})
    return {
        "meta": {"title": book.options.get("title", ""), "currency": book.currency, "from": start.isoformat(),
                 "to": end.isoformat(), "basis": book.basis, "lang": lang, "country": book.profile.get("country"),
                 "legal_form": book.profile.get("legal_form"), "columns": [c[0] for c in cols],
                 "file": book.path.name, "version": book.version, "sha256": book.sha,
                 "ready": rep["ready"], "year_end": rep["year_end"], "precision": prec,
                 "fails": [f"{f['code']}: {f['message']}" + (f" ({f['count']})" if f["count"] else "") for f in rep["fails"]],
                 "warnings": [f"{w['code']}: {w['message']}" + (f" ({'; '.join(w['items'][:5])})" if w["code"] in REVIEW_CODES
                                                                 else f" ({w['count']})" if w["count"] else "")
                              for w in rep["warnings"] if w["code"] != "missing_receipts"],
                 "reconciled": reconciled, "result": result, "fiscal_year_result": fy_result,
                 "missing_receipts_count": rep["missing_receipts_count"]},
        "missing_receipts_by_party": rep["missing_receipts_by_party"],
        "trial_balance": tb, "trial_totals": sums, "pnl": pnl,
        "balance_sheet": {**{k.lower(): v for k, v in bs.items()}, "assets_total": assets_total,
                          "liabilities_total": liab_total, "equity_total": eq_total, "result": fy_result},
        "vat": vat_schedule(book, start, end), "tax_form": tax_form(book, pnl), "open_items": open_items,
        "unallocated": [{"account": a, "amount": v} for a, v in unalloc.items()],
        "assets": assets, "missing_receipts": missing, "journal": journal,
    }


TAX_FORMS = {"US": "Schedule C", "DE": "Anlage EÜR", "AT": "E1a"}
TAX_LINE_NAMES = {"Schedule C": {
    "1": "Gross receipts or sales", "2": "Returns and allowances", "4": "Cost of goods sold", "6": "Other income",
    "8": "Advertising", "9": "Car and truck expenses", "10": "Commissions and fees", "11": "Contract labor",
    "13": "Depreciation and section 179", "15": "Insurance (other than health)", "16b": "Interest (other)",
    "17": "Legal and professional services", "18": "Office expense", "20b": "Rent (other business property)",
    "21": "Repairs and maintenance", "22": "Supplies", "23": "Taxes and licenses", "24a": "Travel",
    "24b": "Deductible meals", "25": "Utilities", "26": "Wages", "27b": "Other expenses"}}


def tax_form(book: Book, pnl: list) -> dict | None:
    """A draft of the owner's business tax form from each account's tax_line. Only a business taxed
    as its single owner; lines are positive amounts as the form shows them."""
    form = TAX_FORMS.get(book.profile.get("country") or "")
    if not form or book.profile.get("company") or any(i["meta"].get("partner") for i in book.accounts.values()):
        return None
    lines, unmapped, nondeductible = defaultdict(Decimal), [], []
    for r in pnl:
        line = account_attr(book, r["account"], "tax_line")
        amount = r["total"] if r["group"] == "Income" and line != "2" else -r["total"]
        item = {"account": r["account"], "name": r["name"], "amount": amount}
        if line == "none":
            nondeductible.append(item)
        elif line:
            lines[line] += amount
        else:
            unmapped.append(item)
    if not lines:
        return None
    order = lambda k: (int(re.match(r"\d*", k).group() or 0), k)
    names = TAX_LINE_NAMES.get(form, {})
    return {"form": form, "lines": [{"line": k, "name": names.get(k, ""), "amount": lines[k]} for k in sorted(lines, key=order)],
            "unmapped": unmapped, "not_deductible": nondeductible}


def own_before(book: Book, account: str, before: date) -> Decimal:
    dates, cum = book._index().get(account, ([], []))
    i = bisect_left(dates, before)
    return cum[i - 1] if i else Decimal(0)


def json_for_html(data) -> str:
    text = json.dumps(data, ensure_ascii=False, default=_json_default)
    for a, b in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(a, b)
    return text


def csv_guard(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def write_csv(path: Path, header: list, rows: list, delimiter: str, decimal: str, bom: bool) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        out = []
        for v in r:
            if isinstance(v, Decimal):
                out.append(format(v, "f").replace(".", decimal))
            elif v is None:
                out.append("")
            else:
                out.append(csv_guard(str(v)))
        w.writerow(out)
    path.write_text(("\ufeff" if bom else "") + buf.getvalue(), encoding="utf-8", newline="")


MISSING_COLS = ["date", "payee", "amount", "account", "narration", "txn_id"]
VAT_COLS = ["period", "type", "jurisdiction", "direction", "regime", "rate", "base", "tax", "input_tax"]
JOURNAL_COLS = ["date", "txn_id", "payee", "narration", "account", "code", "amount", "tax_type", "tax_regime", "tax_rate",
                "tax_point", "document", "document_url", "confirmed"]


def write_report(data: dict, out_dir: Path, formats: set, delimiter: str, decimal: str, bom: bool) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "json" in formats:
        p = out_dir / "report-data.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
        written.append(p.name)
    if "html" in formats:
        template = (HERE / "report.html").read_text(encoding="utf-8")
        if "__REPORT_DATA__" not in template:
            raise LedgerError("report.html is missing the __REPORT_DATA__ placeholder")
        p = out_dir / "report.html"
        p.write_text(template.replace("__REPORT_DATA__", json_for_html(data)), encoding="utf-8")
        written.append(p.name)
    if "csv" in formats:
        args = (delimiter, decimal, bom)
        cols = data["meta"]["columns"]
        tables = {
            "trial-balance.csv": (["account", "code", "name", "opening", "debit", "credit", "closing"],
                                  [[r[k] for k in ("account", "code", "name", "opening", "debit", "credit", "closing")]
                                   for r in data["trial_balance"]]),
            "profit-and-loss.csv": (["account", "code", "name", "group", *cols, "total"],
                                    [[r["account"], r["code"], r["name"], r["group"], *r["values"], r["total"]] for r in data["pnl"]]),
            "balance-sheet.csv": (["side", "account", "code", "name", "amount"],
                                  [[side, r["account"], r["code"], r["name"], r["amount"]]
                                   for side in ("assets", "liabilities", "equity") for r in data["balance_sheet"][side]]
                                  + [["result", "", "", "", data["balance_sheet"]["result"]]]),
            "vat.csv": (VAT_COLS, [[r[k] for k in VAT_COLS] for r in data["vat"]["lines"]]),
            "vat-codes.csv": (["period", "code", "amount"], [[r["period"], r["code"], r["amount"]] for r in data["vat"]["codes"]]),
            "open-items.csv": (["side", "document", "number", "party", "account", "date", "due", "amount", "open",
                                "days_overdue", "bucket"],
                               [[r[k] for k in ("side", "document", "number", "party", "account", "date", "due", "amount",
                                                "open", "days_overdue", "bucket")] for r in data["open_items"]]),
            "assets.csv": (["asset", "description", "cost", "prior", "due", "booked", "book_value_end"],
                           [[r[k] for k in ("asset", "description", "cost", "prior", "due", "booked", "book_value_end")]
                            for r in data["assets"]]),
            "missing-receipts.csv": (MISSING_COLS, [[m[k] for k in MISSING_COLS] for m in data["missing_receipts"]]),
            "journal.csv": (JOURNAL_COLS, [[r[k] for k in JOURNAL_COLS] for r in data["journal"]]),
        }
        for name, (header, rows) in tables.items():
            write_csv(out_dir / name, header, rows, *args)
            written.append(name)
    return written


# ── Exports for the accountant's software ────────────────────────────────────

# Buchungsstapel format 13, the same column line as Billpal's own DATEV export.
DATEV_COLUMNS = (
    "Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen;WKZ Umsatz;Kurs;Basis-Umsatz;WKZ Basis-Umsatz;"
    "Konto;Gegenkonto (ohne BU-Schlüssel);BU-Schlüssel;Belegdatum;Belegfeld 1;Belegfeld 2;Skonto;"
    "Buchungstext;Postensperre;Diverse Adressnummer;Geschäftspartnerbank;Sachverhalt;Zinssperre;"
    "Beleglink;Beleginfo - Art 1;Beleginfo - Inhalt 1;Beleginfo - Art 2;Beleginfo - Inhalt 2;"
    "Beleginfo - Art 3;Beleginfo - Inhalt 3;Beleginfo - Art 4;Beleginfo - Inhalt 4;Beleginfo - Art 5;"
    "Beleginfo - Inhalt 5;Beleginfo - Art 6;Beleginfo - Inhalt 6;Beleginfo - Art 7;Beleginfo - Inhalt 7;"
    "Beleginfo - Art 8;Beleginfo - Inhalt 8;KOST1 - Kostenstelle;KOST2 - Kostenstelle;Kost-Menge;"
    "EU-Land u. UStID (Bestimmung);EU-Steuersatz (Bestimmung);Abw. Versteuerungsart;Sachverhalt L+L;"
    "Funktionsergänzung L+L;BU 49 Hauptfunktionstyp;BU 49 Hauptfunktionsnummer;BU 49 Funktionsergänzung;"
    "Zusatzinformation - Art 1;Zusatzinformation- Inhalt 1;Zusatzinformation - Art 2;"
    "Zusatzinformation- Inhalt 2;Zusatzinformation - Art 3;Zusatzinformation- Inhalt 3;"
    "Zusatzinformation - Art 4;Zusatzinformation- Inhalt 4;Zusatzinformation - Art 5;"
    "Zusatzinformation- Inhalt 5;Zusatzinformation - Art 6;Zusatzinformation- Inhalt 6;"
    "Zusatzinformation - Art 7;Zusatzinformation- Inhalt 7;Zusatzinformation - Art 8;"
    "Zusatzinformation- Inhalt 8;Zusatzinformation - Art 9;Zusatzinformation- Inhalt 9;"
    "Zusatzinformation - Art 10;Zusatzinformation- Inhalt 10;Zusatzinformation - Art 11;"
    "Zusatzinformation- Inhalt 11;Zusatzinformation - Art 12;Zusatzinformation- Inhalt 12;"
    "Zusatzinformation - Art 13;Zusatzinformation- Inhalt 13;Zusatzinformation - Art 14;"
    "Zusatzinformation- Inhalt 14;Zusatzinformation - Art 15;Zusatzinformation- Inhalt 15;"
    "Zusatzinformation - Art 16;Zusatzinformation- Inhalt 16;Zusatzinformation - Art 17;"
    "Zusatzinformation- Inhalt 17;Zusatzinformation - Art 18;Zusatzinformation- Inhalt 18;"
    "Zusatzinformation - Art 19;Zusatzinformation- Inhalt 19;Zusatzinformation - Art 20;"
    "Zusatzinformation- Inhalt 20;Stück;Gewicht;Zahlweise;Forderungsart;Veranlagungsjahr;"
    "Zugeordnete Fälligkeit;Skontotyp;Auftragsnummer;Buchungstyp;USt-Schlüssel (Anzahlungen);"
    "EU-Land (Anzahlungen);Sachverhalt L+L (Anzahlungen);EU-Steuersatz (Anzahlungen);"
    "Erlöskonto (Anzahlungen);Herkunft-Kz;Buchungs GUID;KOST-Datum;SEPA-Mandatsreferenz;Skontosperre;"
    "Gesellschaftername;Beteiligtennummer;Identifikationsnummer;Zeichnernummer;Postensperre bis;"
    "Bezeichnung SoBil-Sachverhalt;Kennzeichen SoBil-Buchung;Festschreibung;Leistungsdatum;"
    "Datum Zuord. Steuerperiode;Fälligkeit;Generalumkehr (GU);Steuersatz;Land;Abrechnungsreferenz;"
    "BVV-Position;EU-Land u. UStID (Ursprung);EU-Steuersatz (Ursprung);Abw. Skontokonto"
).split(";")
DATEV_NUMBER_COLS = {0, 3, 4, 6, 7, 9, 12, DATEV_COLUMNS.index("EU-Steuersatz (Bestimmung)")}
DATEV_HEADER_QUOTED = {0, 3, 7, 8, 9, 16, 17, 21, 23, 26, 29, 30}
DATEV_EU_VAT, DATEV_EU_RATE = DATEV_COLUMNS.index("EU-Land u. UStID (Bestimmung)"), DATEV_COLUMNS.index("EU-Steuersatz (Bestimmung)")
EU_VAT_PREFIXES = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE", "IT", "LT",
                   "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI"}


def eu_vat_id(raw: str) -> str:
    """An EU VAT ID with its country prefix, or "" (a US EIN or a bare number is not one)."""
    s = re.sub(r"[\s.\-]", "", raw or "").upper()
    return s if re.fullmatch(r"[A-Z]{2}[0-9A-Z+*]{2,12}", s) and s[:2] in EU_VAT_PREFIXES else ""
# ponytail: 94/95 are the § 13b keys for services from EU suppliers; domestic § 13b (construction, scrap)
# needs another key, which the accountant sets in DATEV.
DATEV_KEYS = {("purchase", "standard", "19"): "9", ("purchase", "standard", "7"): "8",
              ("sale", "standard", "19"): "3", ("sale", "standard", "7"): "2",
              ("purchase", "reverse_charge", "19"): "94", ("purchase", "reverse_charge", "7"): "95",
              ("purchase", "intra_community", "19"): "42", ("purchase", "intra_community", "7"): "43"}
BMD_HEADER = ["Satzart", "Konto", "Gkonto", "Buchsymbol", "Belegnr", "Extbelegnr", "Buchdatum", "Belegdatum", "Buchcode",
              "Steuercode", "Betrag", "Prozent", "Steuer", "Text", "Zziel", "Waehrung", "FWBetrag", "FWSteuer", "FWKurs",
              "Dokument"]
BMD_CODES = {("purchase", "standard"): "2", ("sale", "standard"): "1", ("purchase", "reverse_charge"): "79",
             ("sale", "reverse_charge"): "77", ("purchase", "intra_community"): "9", ("sale", "intra_community"): "7",
             ("sale", "export"): "5", ("purchase", "export"): "80"}
# ponytail: BMD's common default symbols; the accountant maps others on import.
BMD_SYMBOLS = {"bank": "BK", "card": "BK", "processor": "BK", "clearing": "BK", "cash": "KA", "ap": "ER", "ar": "AR"}
XERO_NO_TAX = {"US": "Tax Exempt", "GB": "No VAT", "AU": "BAS Excluded", "NZ": "No GST"}
XERO_SYSTEM_ROLES = MONEY_ROLES | PARTY_ROLES | {"retained_earnings"}
# DVO imports the same BuErf file as BMD.
EXPORT_FORMATS = ("datev", "bmd", "dvo", "qbo-journal", "xero-journal")


def export_clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[;\r\n]+", " ", str(value or ""))).strip()[:limit]


def export_txns(book: Book, start: date, end: date) -> list[Entry]:
    """Live bookings in the range. The opening balances stay out: the accountant carries them forward."""
    return [t for t in sorted(book.live_txns(), key=lambda t: (t.date, t.line))
            if start <= t.date <= end and not t.meta.get("opening")]


def export_rows(book: Book, txns: list[Entry]) -> tuple[list[dict], list[str]]:
    """One row per posting against the booking's money or party posting, gross with its VAT, the way
    DATEV and BMD book. The VAT postings fold into their base posting."""
    rows, warnings = [], []
    for t in txns:
        posts = [p for p in t.postings if not (p.meta.get("tax_part") in ("tax", "input_tax") and "tax_base" not in p.meta)]
        anchor = (next((p for p in posts if book.role_of(p.account) in MONEY_ROLES | PARTY_ROLES), None)
                  or next((p for p in posts if "tax_regime" not in p.meta), posts[0]))
        total = Decimal(0)
        for p in posts:
            if p is anchor:
                continue
            m = p.meta
            regime, direction = m.get("tax_regime", ""), m.get("tax_direction", "")
            ta = dec(m.get("tax_amount", "0")) if regime and m.get("tax_part") != "input_tax" else Decimal(0)
            if direction == "sale":
                tax, gross = ta, p.amount + ta
            else:
                rec = dec(m.get("recoverable", "1"))
                if ta and rec != 1:
                    warnings.append(f"{t.date} {t.payee}: only part of the input VAT is deductible. Check this booking")
                tax = q(ta * rec, book.prec)
                gross = p.amount + tax - (ta if regime in SELF_ASSESSED else 0)
            total += gross
            if gross:
                rows.append({"txn": t, "date": t.date, "account": p.account, "anchor": anchor.account, "gross": gross,
                             "tax": tax, "regime": regime, "rate": m.get("tax_rate", ""), "direction": direction,
                             "type": m.get("tax_type", ""), "party_tax_id": t.meta.get("party_tax_id", ""),
                             "ref": t.meta.get("document_number") or p.meta.get("document_number") or t.meta.get("txn_id", ""),
                             "text": " ".join(x for x in (t.payee, t.narration) if x)})
        if total != -anchor.amount:
            warnings.append(f"{t.date} {t.payee}: the rows are off by {total + anchor.amount} after folding in VAT")
    return rows, warnings


def need_codes(book: Book, accounts) -> dict:
    # A customer or supplier account books under its Personenkonto number (from Billpal's contacts).
    codes = {a: str(account_attr(book, a, "personenkonto") or account_attr(book, a, "code")) for a in accounts}
    missing = sorted(a for a, c in codes.items() if not c.isdigit())
    if missing:
        raise LedgerError([f"{a}: no account number. Add its code, or for a customer or supplier its Personenkonto "
                           "(with Billpal: the contact's account number)" for a in missing]
                          + ["Ask the accountant for all of these numbers at once"])
    return codes


def export_datev(book: Book, rows: list[dict], start: date, end: date, consultant: str | None, client: str | None,
                 gl_length: int) -> tuple[bytes, list[str]]:
    errs = []
    if book.currency != "EUR":
        errs.append("DATEV takes books in EUR")
    if not (consultant or "").isdigit() or not (client or "").isdigit():
        errs.append("DATEV needs the Beraternummer (--consultant) and Mandantennummer (--client) from the accountant")
    if errs:
        raise LedgerError(errs)
    codes = need_codes(book, {r[k] for r in rows for k in ("account", "anchor")})
    out, warnings = [], []
    for r in rows:
        key, auto = "", account_attr(book, r["account"], "auto_tax")
        taxed = r["regime"] in ("standard", "reverse_charge", "intra_community") and r["rate"] not in ("", "0")
        if auto:
            if r["regime"] != "standard" or r["rate"] != auto:
                errs.append(f"{r['date']} {r['text']}: {r['account']} applies {auto} % VAT by itself in DATEV, "
                            f"this booking is {r['regime']} {r['rate'] or '0'} %. Book it on another account")
        elif taxed and r["type"] in VAT_LIKE:
            key = DATEV_KEYS.get((r["direction"], r["regime"], r["rate"]))
            if key is None:
                errs.append(f"{r['date']} {r['text']}: no DATEV tax key for a {r['direction']} {r['regime']} {r['rate']} %")
                continue
        elif r["direction"] == "sale" and r["regime"] not in ("", "standard", "out_of_scope"):
            warnings.append(f"{r['date']} {r['text']}: a {r['regime']} sale needs the matching DATEV account "
                            "(SKR03 8125, 8120, 8336). Check it with the accountant")
        f = [""] * len(DATEV_COLUMNS)
        f[0] = format(abs(r["gross"]), "f").replace(".", ",")
        f[1] = "S" if r["gross"] < 0 else "H"
        f[6], f[7], f[8] = codes[r["anchor"]], codes[r["account"]], key
        f[9] = r["date"].strftime("%d%m")
        f[10], f[13] = export_clean(r["ref"], 36), export_clean(r["text"], 60)
        if key in ("94", "95", "42", "43"):
            vat_id = eu_vat_id(r["party_tax_id"])
            if vat_id:
                f[DATEV_EU_VAT] = vat_id
                f[DATEV_EU_RATE] = f"{r['rate']},00" if key in ("42", "43") else ""
            else:
                warnings.append(f"{r['date']} {r['text']}: no EU VAT ID of the supplier for tax key {key}. "
                                "The accountant adds it in DATEV, or add it to the contact in Billpal")
        out.append(";".join(v if i in DATEV_NUMBER_COLS or v == "" else '"' + v.replace('"', '""') + '"'
                            for i, v in enumerate(f)))
    if errs:
        raise LedgerError(errs)
    skr = {"skr03": "03", "skr04": "04"}.get(book.profile.get("chart") or "", "")
    head = [""] * 31
    head[:6] = ["EXTF", "700", "21", "Buchungsstapel", "13", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]]
    head[7], head[10], head[11] = "RE", consultant, client
    head[12], head[13] = book.fy_from.strftime("%Y%m%d"), str(gl_length)
    head[14], head[15], head[16] = start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "Buchungsstapel"
    head[18], head[19], head[20], head[21], head[26] = "1", "0", "0", "EUR", skr
    header = ";".join('"' + v + '"' if i in DATEV_HEADER_QUOTED else v for i, v in enumerate(head))
    text = "\r\n".join([header, ";".join(DATEV_COLUMNS), *out]) + "\r\n"
    return text.encode("cp1252", errors="replace"), warnings


def export_bmd(book: Book, rows: list[dict]) -> tuple[bytes, list[str]]:
    if book.currency != "EUR":
        raise LedgerError("BMD takes books in EUR")
    codes = need_codes(book, {r[k] for r in rows for k in ("account", "anchor")})
    comma = lambda v: format(v, "f").replace(".", ",")
    numbers, out = {}, []
    for r in rows:
        t, betrag = r["txn"], -r["gross"]
        taxed = r["type"] in VAT_LIKE and r["regime"]
        code = (BMD_CODES.get((r["direction"], r["regime"]), "80" if r["direction"] == "purchase" else "81")
                if taxed else "")
        belegnr = numbers.setdefault(id(t), str(len(numbers) + 1))
        doc_day = opt_date(t.meta.get("document_date"), "document_date") or r["date"]
        out.append(["0", codes[r["anchor"]], codes[r["account"]], BMD_SYMBOLS.get(book.role_of(r["anchor"]) or "", "SO"),
                    belegnr, export_clean(r["ref"], 60), r["date"].strftime("%d.%m.%Y"), doc_day.strftime("%d.%m.%Y"),
                    "1" if betrag > 0 else "2", code, comma(betrag), r["rate"] if taxed and r["rate"] != "0" else "",
                    comma(r["tax"]) if taxed and r["tax"] else "", export_clean(r["text"], 255), "", "EUR", "", "", "", ""])
    text = "\r\n".join(";".join(row) for row in [BMD_HEADER, *out]) + "\r\n"
    return text.encode("utf-8"), []


def us_date(book: Book, d: date) -> str:
    return d.strftime("%m/%d/%Y" if book.profile.get("country") == "US" else "%d/%m/%Y")


def export_journal(book: Book, txns: list[Entry], fmt_name: str, tax_rate: str | None,
                   clearing: str | None) -> tuple[bytes, list[str]]:
    """Every posting as a journal line; the VAT accounts are lines like any other."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if fmt_name == "qbo-journal":
        w.writerow(["Journal No.", "Journal Date", "Account Name", "Debits", "Credits", "Description", "Name"])
        for t in txns:
            for p in t.postings:
                name = book.accounts[p.account]["meta"].get("name") or p.account.split(":")[-1]
                party = (p.meta.get("party") or t.payee) if book.role_of(p.account) in PARTY_ROLES else ""
                w.writerow([t.meta.get("txn_id", ""), us_date(book, t.date), csv_guard(name),
                            fmt(p.amount, book.prec) if p.amount > 0 else "", fmt(-p.amount, book.prec) if p.amount < 0 else "",
                            csv_guard(export_clean(f"{t.payee} {t.narration}", 4000)), csv_guard(party)])
        return buf.getvalue().encode("utf-8"), []
    rate = tax_rate or XERO_NO_TAX.get(book.profile.get("country") or "")
    if not rate:
        raise LedgerError("give --tax-rate: the name of the no-tax rate in the Xero organisation, for example No VAT")
    used = {p.account for t in txns for p in t.postings}
    system = {a for a in used if book.role_of(a) in XERO_SYSTEM_ROLES}
    if system and not clearing:
        raise LedgerError("Xero takes no bank, receivable, payable or retained earnings account in a manual journal. "
                          f"Give --clearing-code, a clearing account in Xero, for {sorted(system)}")
    codes = need_codes(book, used - system)
    w.writerow(["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount"])
    for t in txns:
        for p in t.postings:
            w.writerow([csv_guard(export_clean(f"{t.payee} {t.narration}", 4000)), us_date(book, t.date),
                        csv_guard(p.account), clearing if p.account in system else codes[p.account], rate,
                        fmt(p.amount, book.prec)])
    warnings = [f"lines on {sorted(system)} went to the clearing account {clearing}. Clear it against the bank in Xero"] if system else []
    return buf.getvalue().encode("utf-8"), warnings


def build_export(book: Book, fmt_name: str, start: date, end: date, out_dir: Path, **opts) -> dict:
    txns = export_txns(book, start, end)
    if fmt_name in ("datev", "bmd", "dvo"):
        rows, warnings = export_rows(book, txns)
        if fmt_name == "datev":
            data, more = export_datev(book, rows, start, end, opts.get("consultant"), opts.get("client"), opts.get("gl_length") or 4)
            name = "EXTF_Buchungsstapel.csv"
        else:
            data, more = export_bmd(book, rows)
            name = "Buchungen.csv"
        count = len(rows)
    else:
        warnings = []
        data, more = export_journal(book, txns, fmt_name, opts.get("tax_rate"), opts.get("clearing"))
        name, count = f"{fmt_name}.csv", sum(len(t.postings) for t in txns)
    rep = readiness(book, min(end, book.fy_to))
    if not rep["ready"]:
        warnings.insert(0, "the books are not ready for this period: " + "; ".join(f["code"] for f in rep["fails"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_bytes(data)
    return {"written": str(out_dir / name), "format": fmt_name, "rows": count, "bookings": len(txns),
            "warnings": warnings + more}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _out(path_arg, default: Path) -> Path:
    return Path(path_arg) if path_arg else default


class ArgParser(argparse.ArgumentParser):
    """Usage errors come back as the same JSON as every other error."""

    def error(self, message):
        raise LedgerError(f"{self.prog}: {message}")


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        result = run(args)
    except LedgerError as exc:
        emit({"ok": False, "errors": exc.messages})
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, json.JSONDecodeError,
            InvalidOperation) as exc:
        emit({"ok": False, "errors": [f"could not read the input: {type(exc).__name__}: {exc}"]})
        return 1
    emit({"ok": True, **result})
    return 0


def parser() -> argparse.ArgumentParser:
    ap = ArgParser(prog="ledger.py", description="AI bookkeeping on Beancount files")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=ArgParser)

    p = sub.add_parser("init", help="create the first ledger file from init.json")
    p.add_argument("input")
    p.add_argument("--out-dir", default=".")
    p = sub.add_parser("status", help="version, balances, newest file, forks and pending files")
    p.add_argument("ledger")
    p = sub.add_parser("check", help="validate; --ready for a period, --year-end for closing")
    p.add_argument("ledger")
    p.add_argument("--ready", action="store_true")
    p.add_argument("--through")
    p.add_argument("--year-end", action="store_true")
    p = sub.add_parser("account-path", help="deterministic path for a new account")
    p.add_argument("ledger")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--parent")
    g.add_argument("--parent-role")
    p.add_argument("--name", required=True)
    p.add_argument("--code")
    p = sub.add_parser("from-camt", help="CAMT.053 XML to statement JSON")
    p.add_argument("file")
    p.add_argument("--ledger", required=True)
    p.add_argument("--out")
    p = sub.add_parser("from-ofx", help="OFX, QFX or QBO download to statement JSON")
    p.add_argument("file")
    p.add_argument("--ledger", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--source-id")
    p.add_argument("--out")
    p = sub.add_parser("from-csv", help="bank CSV to statement JSON with a csv_map from the ledger")
    p.add_argument("file")
    p.add_argument("--ledger", required=True)
    p.add_argument("--map", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--opening")
    p.add_argument("--closing")
    p.add_argument("--source-id")
    p.add_argument("--out")
    p = sub.add_parser("stage", help="check a statement and classify its rows into a pending file")
    p.add_argument("ledger")
    p.add_argument("statement")
    p.add_argument("--out")
    p = sub.add_parser("allocate", help="business shares of costs paid from a private account, as a journal proposal")
    p.add_argument("ledger")
    p.add_argument("statement")
    p.add_argument("--out")
    p = sub.add_parser("candidates", help="matching documents for staged rows")
    p.add_argument("ledger")
    p.add_argument("pending")
    p.add_argument("--documents")
    p = sub.add_parser("commit", help="book a staged statement with decisions")
    p.add_argument("ledger")
    p.add_argument("pending")
    p.add_argument("--decisions")
    p.add_argument("--documents", help="the documents file candidates read: derives reverse charge and payees")
    p = sub.add_parser("stage-documents", help="accrual books: stage invoices and credit notes")
    p.add_argument("ledger")
    p.add_argument("documents")
    p.add_argument("--out")
    p = sub.add_parser("commit-documents", help="book staged documents with decisions")
    p.add_argument("ledger")
    p.add_argument("pending")
    p.add_argument("--decisions")
    p = sub.add_parser("journal", help="append entries, accounts, rules, assets, profile updates, confirmations")
    p.add_argument("ledger")
    p.add_argument("input")
    p = sub.add_parser("reverse", help="reverse a booking, optionally rebook it")
    p.add_argument("ledger")
    p.add_argument("--txn", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--rebook")
    p = sub.add_parser("close-period", help="lock bookings through a date")
    p.add_argument("ledger")
    p.add_argument("--through", required=True)
    p = sub.add_parser("depreciate", help="depreciation for the fiscal year (preview unless --write)")
    p.add_argument("ledger")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("rollover", help="open the next fiscal year (preview unless --write)")
    p.add_argument("ledger")
    p.add_argument("--to")
    p.add_argument("--label")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("report", help="HTML, CSV and JSON reports")
    p.add_argument("ledger")
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--by", choices=("month", "quarter", "none"), default="none")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--format", default="html,csv,json")
    p.add_argument("--lang", choices=("de", "en"))
    p.add_argument("--delimiter")
    p.add_argument("--decimal")
    p.add_argument("--bom", action=argparse.BooleanOptionalAction, default=None)
    p = sub.add_parser("export", help="bookings for the accountant's software: DATEV, BMD, QuickBooks or Xero")
    p.add_argument("ledger")
    p.add_argument("--format", choices=EXPORT_FORMATS, required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--consultant", help="DATEV Beraternummer")
    p.add_argument("--client", help="DATEV Mandantennummer")
    p.add_argument("--gl-length", type=int, help="DATEV Sachkontenlänge, 4 by default")
    p.add_argument("--tax-rate", help="Xero: the name of the no-tax rate")
    p.add_argument("--clearing-code", help="Xero: clearing account for bank, receivable and payable lines")
    p = sub.add_parser("writeback", help="Billpal payment updates proven by the ledger")
    p.add_argument("ledger")
    p.add_argument("--documents", help="documents/1 file, for totals in cash-basis books")
    p = sub.add_parser("from-billpal", help="Billpal get_document results to documents/1 JSON")
    p.add_argument("file")
    p.add_argument("--out")
    return ap


def run(args) -> dict:
    if args.cmd == "init":
        book = build_init(load_json(args.input), Path(args.out_dir))
        return {"written": str(book.path), "version": 1, "ledger_id": book.ledger_id}
    if args.cmd == "from-billpal":
        docs = from_billpal(load_json(args.file))
        notes = docs.pop("notes")
        out = _out(args.out, Path(args.file).with_suffix(".documents.json"))
        out.write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"written": str(out), "documents": len(docs["documents"]), "notes": notes}
    book = Book.load(args.ledger)
    if args.cmd == "status":
        return status(book)
    if args.cmd == "check":
        errs = validate(book)
        if args.ready or args.year_end:
            rep = readiness(book, opt_date(args.through), year_end=args.year_end)
            if not rep["ready"]:
                raise LedgerError([f"{f['code']}: {f['message']}: {f['items'][:10]}" for f in rep["fails"]]
                                  + [f"warning {w['code']}: {w['message']} ({w['count']})" for w in rep["warnings"]])
            return rep
        if errs:
            raise LedgerError(errs)
        return {"valid": True, "file": book.path.name, "version": book.version}
    if args.cmd == "account-path":
        parent = args.parent or book.one_role(args.parent_role)
        path, existing = account_path(book, parent, args.name, args.code)
        return {"account": path, "exists": existing}
    if args.cmd in ("from-camt", "from-csv", "from-ofx"):
        data = Path(args.file).read_bytes()
        if args.cmd == "from-camt":
            st = from_camt(data, book)
        elif args.cmd == "from-ofx":
            st = from_ofx(data, book, args.account, args.source_id)
        else:
            st = from_csv(data, book, args.map, args.account, args.start, args.end, args.opening, args.closing, args.source_id)
        for s in st["statements"]:
            s["file"] = Path(args.file).name
        out = _out(args.out, book.path.with_name(f"{Path(args.file).stem}.statement.json"))
        out.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"written": str(out), "statements": [{"source_id": s["source_id"], "account": s["account"], "rows": len(s["rows"]),
                                                     "from": s["from"], "to": s["to"]} for s in st["statements"]]}
    if args.cmd == "stage":
        pending = stage(book, load_json(args.statement))
        out = _out(args.out, book.path.with_name(f"{Path(args.statement).stem}.pending.json"))
        out.write_text(json.dumps(pending, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
        return {"pending": str(out), **pending_summary(pending)}
    if args.cmd == "allocate":
        res = allocate(book, load_json(args.statement))
        out = _out(args.out, book.path.with_name(f"{Path(args.statement).stem}.allocations.json"))
        out.write_text(json.dumps(res["journal"], ensure_ascii=False, indent=1), encoding="utf-8")
        return {"written": str(out), "entries": [{"date": e["date"], "payee": e["payee"], "allocation": e["meta"]["allocation"],
                                                  "lines": e["lines"]} for e in res["journal"]["entries"]],
                "skipped": res["skipped"], "rows_without_allocation": res["rows_without_allocation"]}
    if args.cmd == "candidates":
        pending = load_json(args.pending)
        res = candidates(book, pending, load_json(args.documents) if args.documents else None)
        holds = [{"document": h["document"], "date": h["date"], "note": "in Billpal Review when the statement was matched"}
                 for h in res["held_in_review"] if h["document"] and h["date"]]
        if holds:
            pending["review_holds"] = holds
            Path(args.pending).write_text(json.dumps(pending, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
        return res
    if args.cmd in ("commit", "commit-documents"):
        pending = load_json(args.pending)
        decisions = load_json(args.decisions) if args.decisions else None
        if args.cmd == "commit":
            nb = build_commit(book, pending, decisions, load_json(args.documents) if args.documents else None)
        else:
            nb = build_commit_documents(book, pending, decisions)
        pending["committed_in"] = nb.path.name
        Path(args.pending).write_text(json.dumps(pending, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
        return {"written": str(nb.path), "version": nb.version, "transactions": len(nb.txns) - len(book.txns)}
    if args.cmd == "stage-documents":
        pending = stage_documents(book, load_json(args.documents))
        out = _out(args.out, book.path.with_name(f"{Path(args.documents).stem}.pending.json"))
        out.write_text(json.dumps(pending, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
        return {"pending": str(out), "counts": dict(Counter(d["status"] for d in pending["documents"])),
                "new_parties": [d["suggested"]["new_party"] for d in pending["documents"] if d["suggested"].get("new_party")]}
    if args.cmd == "journal":
        nb = build_journal(book, load_json(args.input))
        return {"written": str(nb.path), "version": nb.version}
    if args.cmd == "reverse":
        rebook = load_json(args.rebook) if args.rebook else None
        nb = build_reverse(book, args.txn, parse_date(args.date), clean_text(args.reason), rebook)
        return {"written": str(nb.path), "version": nb.version}
    if args.cmd == "close-period":
        nb = build_close(book, parse_date(args.through))
        return {"written": str(nb.path), "version": nb.version, "closed_through": args.through}
    if args.cmd == "depreciate":
        plan, body = build_depreciation(book)
        if not args.write or not body:
            return {"preview": not args.write, "assets": plan}
        nb = write_next(book, body, "depreciation")
        return {"written": str(nb.path), "version": nb.version, "assets": plan}
    if args.cmd == "rollover":
        return build_rollover(book, opt_date(args.to), args.label, args.write)
    if args.cmd == "writeback":
        return writeback(book, load_json(args.documents) if args.documents else None)
    if args.cmd in ("report", "export"):
        start = opt_date(args.start) or book.fy_from
        end = opt_date(args.end) or min(book.fy_to, max((t.date for t in book.txns), default=book.fy_from))
        if not book.fy_from <= start <= end <= book.fy_to:
            raise LedgerError(f"{args.cmd} dates must lie in the fiscal year {book.fy_from} to {book.fy_to}, from before to")
    if args.cmd == "export":
        return build_export(book, args.format, start, end, Path(args.out_dir), consultant=args.consultant,
                            client=args.client, gl_length=args.gl_length, tax_rate=args.tax_rate, clearing=args.clearing_code)
    if args.cmd == "report":
        lang = args.lang or book.profile.get("language", "en")
        data = build_report(book, start, end, args.by, lang)
        formats = {f.strip() for f in args.format.split(",") if f.strip()}
        delimiter = args.delimiter or (";" if lang == "de" else ",")
        decimal = args.decimal or ("," if lang == "de" else ".")
        if delimiter == decimal:
            raise LedgerError("delimiter and decimal separator must differ")
        bom = args.bom if args.bom is not None else lang == "de"
        written = write_report(data, Path(args.out_dir), formats, delimiter, decimal, bom)
        m = data["meta"]
        return {"written": written, "out_dir": args.out_dir, "reconciled": m["reconciled"], "ready": m["ready"],
                "fails": m["fails"], "result": m["result"], "period": [m["from"], m["to"]],
                "missing_receipts_count": m["missing_receipts_count"],
                "missing_receipts_by_party": data["missing_receipts_by_party"]}
    raise LedgerError(f"unknown command {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
