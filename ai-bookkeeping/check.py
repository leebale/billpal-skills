#!/usr/bin/env python3
"""Run ledger.py through two synthetic books and hostile input, and load every file with Beancount.

Set BEANCOUNT_REQUIRED=1 (CI does) to fail when the beancount package is missing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ledger  # noqa: E402

D = Decimal
WRITTEN: list[Path] = []


def cli(*args) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ledger.main([str(a) for a in args])
    out = json.loads(buf.getvalue())
    assert (code == 0) == out["ok"], out
    if out.get("ok") and out.get("written") and isinstance(out["written"], str):
        WRITTEN.append(Path(out["written"]))
    return out


def ok(*args) -> dict:
    out = cli(*args)
    assert out["ok"], f"{args[0]} failed: {out}"
    return out


def fails(fragment: str, *args) -> dict:
    out = cli(*args)
    assert not out["ok"], f"{args[0]} should fail with {fragment!r}: {out}"
    joined = " | ".join(out["errors"])
    assert fragment in joined, f"expected {fragment!r} in: {joined}"
    return out


def raises(fragment: str, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except ledger.LedgerError as exc:
        assert fragment in str(exc), f"expected {fragment!r} in: {exc}"
        return
    raise AssertionError(f"expected LedgerError with {fragment!r}")


def put(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def latest(folder: Path, label: str) -> Path:
    files = sorted(folder.glob(f"{label}-v*.beancount"), key=lambda p: int(p.stem.rsplit("-v", 1)[1]))
    return files[-1]


# ── Unit cases ───────────────────────────────────────────────────────────────

def unit() -> None:
    nasty = 'He said "hi" \\ back\\slash'
    assert ledger.unescape(ledger.bean_string(nasty)[1:-1], 1) == nasty
    assert ledger.clean_text("a\nb\tc\x00d") == "a b c d"
    assert ledger.account_component("Müller & Söhne GmbH") == "Mueller-Soehne-GmbH"
    assert ledger.account_component("3M Österreich") == "3M-Oesterreich"
    assert ledger.account_component("???") == "X"
    assert ledger.account_component("été café") == "Ete-cafe"
    raises("percentage points", ledger.parse_rate, "0.2")
    assert ledger.parse_rate("20") == D(20)
    raises("more than 2 decimals", ledger.money, "1.234", 2)
    raises("not floats", ledger.dec, 1.5)
    assert ledger.precision_for("JPY") == 0 and ledger.precision_for("KWD") == 3 and ledger.precision_for("EUR") == 2
    assert ledger.fmt(D("-0.004"), 2) == "0.00"
    assert ledger.q(D("0.125"), 2) == D("0.13")
    assert ledger.csv_guard("=SUM(A1)") == "'=SUM(A1)"
    assert ledger.csv_guard("-cmd") == "'-cmd" and ledger.csv_guard("@x") == "'@x" and ledger.csv_guard("plain") == "plain"
    html = ledger.json_for_html({"x": "</script><script>alert(1)</script>&\u2028"})
    assert "</script" not in html and "<" not in html and "&" not in html and "\u2028" not in html
    assert json.loads(html)["x"].startswith("</script>")
    for bad in ("2026-01-01 note Assets:Bank \"x\"", "pushtag #x", "2026-01-01\topen Assets:Bank EUR",
                "  orphan: \"x\"", "2026-01-01 * \"a\" \"b\" ; comment", 'option "x" "a\\n"'):
        raises("line", ledger.parse_ledger, bad + "\n")
    _, entries = ledger.parse_ledger('2026-01-01 custom "x" "{\\"a\\":\\"q\\\\\\"\\"}"\n')
    assert entries[0].value == {"a": 'q"'}
    assert ledger.next_path(Path("2026-v9.beancount"), 10).name == "2026-v10.beancount"
    assert ledger.next_path(Path("books.beancount.txt"), 2).name == "books-v2.beancount.txt"
    assert ledger.vendor_key({"text": "SEPA-Lastschrift SPAR 4711 Filiale"}) == "text:spar filiale"
    assert ledger.vendor_key({"iban": "at61 1904"}) == "iban:AT611904"
    assert ledger.row_method({"text": "SEPA-Lastschrift Spotify"}) == "direct_debit"
    assert ledger.row_method({"text": "Überweisung an Steuerberatung"}) is None
    print("unit: ok")


# ── Book A: Austria, cash basis (Einnahmen-Ausgaben-Rechnung), ÖKR ───────────

IBAN_A = "AT611904300234573201"


def camt(msg: str, opening: str, closing: str, start: str, end: str, entries: list[tuple]) -> str:
    def amt(v):
        v = D(v)
        return f'<Amt Ccy="EUR">{abs(v)}</Amt><CdtDbtInd>{"CRDT" if v > 0 else "DBIT"}</CdtDbtInd>'
    ntry = ""
    for ref, d, v, party, text, status in entries:
        who = "Dbtr" if D(v) > 0 else "Cdtr"
        ntry += (f"<Ntry>{amt(v)}<Sts><Cd>{status}</Cd></Sts><BookgDt><Dt>{d}</Dt></BookgDt><ValDt><Dt>{d}</Dt></ValDt>"
                 f"<AcctSvcrRef>{ref}</AcctSvcrRef><NtryDtls><TxDtls><RltdPties><{who}><Nm>{party}</Nm></{who}></RltdPties>"
                 f"<RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>")
    return (f'<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>'
            f"<GrpHdr><MsgId>{msg}</MsgId></GrpHdr><Stmt><Id>{msg}-1</Id><FrToDt><FrDtTm>{start}T00:00:00</FrDtTm>"
            f"<ToDtTm>{end}T23:59:59</ToDtTm></FrToDt><Acct><Id><IBAN>{IBAN_A}</IBAN></Id><Ccy>EUR</Ccy></Acct>"
            f"<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>{amt(opening)}<Dt><Dt>{start}</Dt></Dt></Bal>"
            f"<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>{amt(closing)}<Dt><Dt>{end}</Dt></Dt></Bal>"
            f"{ntry}</Stmt></BkToCstmrStmt></Document>")


def book_a(tmp: Path) -> None:
    f = tmp / "a"
    f.mkdir()
    init = {
        "schema": "init/1", "title": 'Huber "Design" e.U.', "file_label": "2026",
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "AT", "legal_form": "Einzelunternehmen", "basis": "cash", "vat_status": "regular",
                    "vat_basis": "payment", "vat_period": "quarter", "chart": "oekr", "language": "de"},
        "chart": "oekr",
        "accounts": [{"account": "Assets:Bank:Giro", "role": "bank", "code": "2800", "name": "Erste Giro", "iban": IBAN_A}],
        "opening": {"lines": [{"account": "Assets:Bank:Giro", "amount": "5000.00"},
                              {"account": "Assets:Cash", "amount": "200.00"},
                              {"account": "Equity:Retained-Earnings", "amount": "-5200.00"}]},
        "rules": [{"id": "svs", "text": "Sozialversicherung", "money": "out", "account": "Expenses:SVS",
                   "tax": {"regime": "out_of_scope", "source": "rule"}}],
    }
    ok("init", put(f / "init.json", init), "--out-dir", f)
    v1 = f / "2026-v1.beancount"
    fails("already exists", "init", f / "init.json", "--out-dir", f)

    jan = camt("M1", "5000.00", "1960.00", "2026-01-01", "2026-01-31", [
        ("R1", "2026-01-05", "-120.00", "Adobe Systems Software Ireland", "Creative Cloud 01/26", "BOOK"),
        ("R2", "2026-01-08", "-1800.00", "Apple Distribution", "MacBook Pro Rechnung A-123", "BOOK"),
        ("R3", "2026-01-12", "1200.00", "Kunde Maier GmbH", "Rechnung 2026-001", "BOOK"),
        ("R4", "2026-01-15", "-820.00", "Finanzamt Österreich", "Abgabenkonto USt 4/2025 und ESt-VZ", "BOOK"),
        ("R5", "2026-01-20", "-1500.00", "Sozialversicherung der Selbständigen", "SVS Beitrag Q1", "BOOK"),
        ("R6", "2026-01-30", "-99.00", "Pending Shop", "not booked yet", "PDNG"),
    ])
    (f / "jan.xml").write_text(jan, encoding="utf-8")
    out = ok("from-camt", f / "jan.xml", "--ledger", v1, "--out", f / "jan.statement.json")
    assert out["statements"][0]["rows"] == 5 and out["statements"][0]["account"] == "Assets:Bank:Giro"
    pend = ok("stage", v1, f / "jan.statement.json")
    st = pend["statements"][0]
    assert st["counts"] == {"new": 5} and st["with_rule"] == 1, st
    p_jan = Path(pend["pending"])
    decisions = {
        "schema": "decisions/1",
        "accept_suggested": True,
        "assets": [],
        "groups": [{"vendor_key": "text:adobe systems software", "lines": [{"account": "Expenses:Software",
                    "tax": {"regime": "reverse_charge", "rate": "20", "source": "user"}}], "confirmed": "user",
                    "classified_by": "user", "document": "billpal:adobe-jan"}],
        "rows": [
            {"ref": "1:2", "lines": [{"account": "Assets:Fixed:Furniture", "asset": "macbook",
                                      "tax": {"regime": "standard", "rate": "20", "source": "document"}}],
             "confirmed": "user", "document": "billpal:apple-123"},
            {"ref": "1:3", "lines": [{"account": "Income:Sales", "tax": {"regime": "standard", "rate": "20", "source": "document"}}],
             "confirmed": "user", "document": "billpal:out-2026-001"},
            {"ref": "1:4", "lines": [{"account": "Liabilities:Tax-Office", "amount": "320.00"},
                                     {"account": "Equity:Private"}], "confirmed": "user",
             "no_document": "Abgabenkonto: USt-Zahlung und ESt-Vorauszahlung", "narration": "Abgabenkonto split"},
        ],
    }
    ok("commit", v1, p_jan, "--decisions", put(f / "jan.decisions.json", decisions))
    v2 = latest(f, "2026")
    assert v2.name == "2026-v2.beancount"
    b = ledger.Book.load(v2)
    assert b.balance("Assets:Bank:Giro") == D("1960.00")
    assert b.balance("Assets:VAT:Input") == D("24.00") + D("300.00"), b.balance("Assets:VAT:Input")
    assert b.balance("Liabilities:VAT:Output") == D("-24.00") - D("200.00")
    assert b.balance("Equity:Private") == D("500.00")
    assert b.balance("Expenses:SVS") == D("1500.00")
    fails("already committed", "commit", v1, p_jan, "--decisions", f / "jan.decisions.json")
    fails("already exists", "journal", v1, put(f / "noop.json", {"schema": "journal/1", "note": "x"}))

    # same statement again: already booked, nothing new
    again = ok("stage", v2, f / "jan.statement.json", "--out", f / "jan-again.pending.json")
    assert again["statements"][0]["status"] == "already_booked"
    ok("commit", v2, f / "jan-again.pending.json")
    v3 = latest(f, "2026")
    assert len(ledger.Book.load(v3).txns) == len(b.txns)

    # overlapping CSV export of the same days with other text: rows go to review, never silently in
    ok("journal", v3, put(f / "map.json", {"schema": "journal/1", "csv_maps": [{
        "id": "erste", "delimiter": ";", "decimal": ",", "thousands": ".", "date_format": "%d.%m.%Y",
        "columns": {"booking_date": "Buchungsdatum", "amount": "Betrag", "text": "Text", "balance": "Saldo"}}]}))
    v4 = latest(f, "2026")
    (f / "overlap.csv").write_text(
        "Buchungsdatum;Betrag;Text;Saldo\n20.01.2026;-1.500,00;SVS anders formatiert;1.960,00\n"
        "05.02.2026;-30,00;Bankspesen Februar;1.930,00\n", encoding="utf-8")
    ok("from-csv", f / "overlap.csv", "--ledger", v4, "--map", "erste", "--account", "Assets:Bank:Giro",
       "--from", "2026-01-20", "--to", "2026-02-05", "--opening", "3460.00", "--out", f / "overlap.statement.json")
    pend = ok("stage", v4, f / "overlap.statement.json")
    s = pend["statements"][0]
    assert s["counts"] == {"ambiguous": 1, "new": 1}, s
    assert "must add up to 0.00" in s["ambiguous_hint"], s
    bad_open = json.loads((f / "overlap.statement.json").read_text())
    bad_open["statements"][0]["opening"] = "3400.00"
    bad_open["statements"][0]["rows"][0]["balance"] = None
    bad_open["statements"][0]["rows"][1]["balance"] = None
    bad_open["statements"][0]["closing"] = "1870.00"
    fails("opens at 3400.00", "stage", v4, put(f / "bad-open.json", bad_open))
    Path(pend["pending"]).unlink()

    # overlap inside a booked period goes to review
    ov = json.loads((f / "jan.statement.json").read_text())
    ov["statements"][0]["source_id"] = "other-export"
    ov["statements"][0]["rows"][0]["text"] = "ADOBE *CREATIVE CLOUD"
    ov["statements"][0]["rows"][0]["bank_id"] = None
    for r in ov["statements"][0]["rows"]:
        r["bank_id"] = None
    pend_ov = ok("stage", v4, put(f / "ov.statement.json", ov), "--out", f / "ov.pending.json")
    s = pend_ov["statements"][0]
    assert s["counts"].get("ambiguous") == 1 and s["counts"].get("duplicate") == 4, s
    assert "must add up to 0.00" in s["ambiguous_hint"], s
    fails("ambiguous", "commit", v4, f / "ov.pending.json")
    ok("commit", v4, f / "ov.pending.json", "--decisions",
       put(f / "ov.dec.json", {"rows": [{"ref": "1:1", "decision": "duplicate"}]}))
    v5 = latest(f, "2026")

    # Feb: card fee, cash withdrawal to the cash box, rule suggestion for SVS
    feb = {"schema": "statement/1", "statements": [{
        "source_id": "feb-pdf", "account": "Assets:Bank:Giro", "from": "2026-02-01", "to": "2026-02-28",
        "opening": "1960.00", "closing": "1130.00", "extraction": "model", "count": 3, "rows": [
            {"booking_date": "2026-02-05", "amount": "-30.00", "text": "Bankspesen Februar"},
            {"booking_date": "2026-02-10", "amount": "-300.00", "text": "Behebung Bankomat"},
            {"booking_date": "2026-02-20", "amount": "-500.00", "text": "Sozialversicherung SVS"}]}]}
    noc = json.loads(json.dumps(feb))
    noc["statements"][0]["count"] = None
    fails("without a transaction count", "stage", v5, put(f / "noc.json", noc))
    noc["statements"][0]["confirmed_by_user"] = True
    ok("stage", v5, put(f / "noc.json", noc), "--out", f / "noc.pending.json")
    (f / "noc.pending.json").unlink()
    bad = json.loads(json.dumps(feb))
    bad["statements"][0]["rows"][0]["amount"] = "-31.00"
    fails("Rows are missing or were read wrong", "stage", v5, put(f / "bad.json", bad))
    pend = ok("stage", v5, put(f / "feb.json", feb))
    ok("commit", v5, pend["pending"], "--decisions", put(f / "feb.dec.json", {
        "accept_suggested": True,
        "rows": [{"ref": "1:1", "lines": [{"account": "Expenses:Bank-Charges", "tax": {"regime": "exempt", "source": "user"}}],
                  "confirmed": "user", "no_document": "bank fee"},
                 {"ref": "1:2", "lines": [{"account": "Assets:Transit"}], "confirmed": "user", "no_document": "transfer"}]}))
    v6 = latest(f, "2026")
    b6 = ledger.Book.load(v6)
    assert b6.balance("Assets:Transit") == D("300.00")

    # cash book: a withdrawal without the deposit fails, the full book passes
    cash = {"schema": "statement/1", "statements": [{
        "source_id": "kassa-feb", "account": "Assets:Cash", "kind": "cashbook", "from": "2026-02-01", "to": "2026-02-28",
        "opening": "200.00", "closing": "430.00", "extraction": "manual", "rows": [
            {"booking_date": "2026-02-10", "amount": "300.00", "text": "Einlage aus Bank"},
            {"booking_date": "2026-02-12", "amount": "-70.00", "text": "Büromaterial Libro"}]}]}
    neg = json.loads(json.dumps(cash))
    neg["statements"][0]["rows"] = [{"booking_date": "2026-02-03", "amount": "-250.00", "text": "Einkauf"},
                                    {"booking_date": "2026-02-10", "amount": "480.00", "text": "Einlage"}]
    fails("cash goes negative", "stage", v6, put(f / "neg.json", neg))
    pend = ok("stage", v6, put(f / "cash.json", cash))
    ok("commit", v6, pend["pending"], "--decisions", put(f / "cash.dec.json", {"rows": [
        {"ref": "1:1", "lines": [{"account": "Assets:Transit"}], "confirmed": "user", "no_document": "transfer"},
        {"ref": "1:2", "lines": [{"account": "Expenses:Office-Supplies", "tax": {"regime": "standard", "rate": "20"}}],
         "document": "billpal:libro"}]}))
    v7 = latest(f, "2026")
    b7 = ledger.Book.load(v7)
    assert b7.balance("Assets:Cash") == D("430.00") and b7.balance("Assets:Transit") == 0
    pend = ok("stage", v7, put(f / "kassa-jan.json", {"schema": "statement/1", "statements": [{
        "source_id": "kassa-jan", "account": "Assets:Cash", "kind": "cashbook", "from": "2026-01-01", "to": "2026-01-31",
        "opening": "200.00", "closing": "200.00", "extraction": "manual", "rows": []}]}))
    ok("commit", v7, pend["pending"])
    v8 = latest(f, "2026")
    fails("cash goes negative", "journal", v8, put(f / "neg2.json", {"schema": "journal/1", "entries": [{
        "date": "2026-02-02", "payee": "x", "lines": [{"account": "Assets:Cash", "amount": "-450.00"},
                                                      {"account": "Equity:Private"}], "confirmed": "user"}]}))

    # readiness: the cash-book office supplies are agent-classified and unconfirmed
    rep = ledger.readiness(ledger.Book.load(v8), ledger.parse_date("2026-02-28"))
    codes = {x["code"] for x in rep["fails"]}
    assert codes == {"unconfirmed", "tax_unconfirmed", "statements_missing"} or codes == {"unconfirmed", "tax_unconfirmed"}, rep
    b8 = ledger.Book.load(v8)
    office = next(t for t in b8.txns if t.meta.get("document") == "billpal:libro")
    tid = office.meta["txn_id"]
    ok("journal", v8, put(f / "confirm.json", {"schema": "journal/1", "confirmations": [
        {"kind": "booking", "ref": tid}, {"kind": "tax", "ref": tid}]}))
    v9 = latest(f, "2026")
    rep = ok("check", v9, "--ready", "--through", "2026-02-28")
    assert rep["ready"]
    fails("statements_missing", "check", v9, "--ready", "--through", "2026-03-31")

    # gap: April before March is a broken chain
    apr = {"schema": "statement/1", "statements": [{
        "source_id": "apr", "account": "Assets:Bank:Giro", "from": "2026-04-01", "to": "2026-04-30",
        "opening": "900.00", "closing": "900.00", "extraction": "manual", "rows": []}]}
    fails("An earlier statement is missing", "stage", v9, put(f / "apr.json", apr))

    # assets: MacBook 1500 net, 36 months, half-year rule, 20 % private; GWG above the limit fails
    ok("journal", v9, put(f / "asset.json", {"schema": "journal/1", "assets": [{
        "id": "macbook", "description": "MacBook Pro", "document": "billpal:apple-123", "in_service": "2026-01-08",
        "cost": "1500.00", "life_months": 36, "private_share": "0.2", "asset_account": "Assets:Fixed:Furniture",
        "depreciation_account": "Expenses:Depreciation", "private_account": "Equity:Private"}]}))
    v10 = latest(f, "2026")
    fails("above the GWG limit", "journal", v10, put(f / "gwg.json", {"schema": "journal/1", "assets": [{
        "id": "chair", "method": "gwg", "in_service": "2026-02-01", "cost": "1100.00",
        "asset_account": "Assets:Fixed:GWG", "depreciation_account": "Expenses:Depreciation-GWG"}]}))
    prev = ok("depreciate", v10)
    assert prev["preview"] and prev["assets"][0]["due"] == "500.00", prev
    assert latest(f, "2026") == v10
    ok("depreciate", v10, "--write")
    v11 = latest(f, "2026")
    b11 = ledger.Book.load(v11)
    assert b11.balance("Expenses:Depreciation") == D("400.00") and b11.balance("Equity:Private") == D("600.00")
    assert ledger.depreciation_plan(b11)[0]["to_book"] == 0

    # hostile payee and narration survive the ledger, Beancount, HTML and CSV
    evil = '=HYPERLINK("http://x")</script><img src=x onerror=alert(1)> "quoted" \\ back\nline'
    ok("journal", v11, put(f / "evil.json", {"schema": "journal/1", "entries": [{
        "date": "2026-02-27", "payee": evil, "narration": evil, "confirmed": "user", "no_document": evil,
        "lines": [{"account": "Equity:Private", "amount": "10.00"}, {"account": "Liabilities:Tax-Office", "amount": "-10.00"}]}]}))
    v12 = latest(f, "2026")
    b12 = ledger.Book.load(v12)
    assert any(t.payee == ledger.clean_text(evil) for t in b12.txns)
    out = ok("report", v12, "--out-dir", f / "rep", "--by", "month", "--to", "2026-02-28")
    assert out["reconciled"], out
    html = (f / "rep" / "report.html").read_text(encoding="utf-8")
    assert html.count("</script>") == 2 and "<img src=x" not in html
    journal_csv = (f / "rep" / "journal.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in journal_csv and ";-10,00;" in journal_csv
    data = json.loads((f / "rep" / "report-data.json").read_text(encoding="utf-8"))
    q1 = next(t for t in data["vat"]["totals"] if t["period"] == "2026-Q1")
    # output: 200 sales + 24 reverse charge; input: 24 RC + 300 MacBook + 11.67 office supplies
    assert D(q1["tax"]) == D("224.00") and D(q1["input_tax"]) == D("335.67"), q1
    pnl = {r["account"]: D(r["total"]) for r in data["pnl"]}
    assert pnl["Income:Sales"] == D("1000.00") and pnl["Expenses:SVS"] == D("-2000.00")
    assert D(data["meta"]["result"]) == sum(pnl.values())

    # reverse and rebook the SVS February payment as private (Austrian owner insurance is not private, just a test)
    svs = next(t for t in b12.txns if t.meta.get("source_statement") == "feb-pdf" and t.meta.get("source_row") == "3")
    assert svs.meta["confirmed"] == "rule" and svs.meta["classified_by"] == "rule:svs"
    ok("reverse", v12, "--txn", svs.meta["txn_id"], "--date", "2026-02-28", "--reason", "test",
       "--rebook", put(f / "rebook.json", {"lines": [{"account": "Expenses:Insurance", "tax": {"regime": "exempt"}}],
                                           "confirmed": "user", "no_document": "SVS"}))
    v13 = latest(f, "2026")
    b13 = ledger.Book.load(v13)
    assert b13.balance("Expenses:SVS") == D("1500.00") and b13.balance("Expenses:Insurance") == D("500.00")
    assert b13.balance("Assets:Bank:Giro") == D("1130.00")
    fails("already reversed", "reverse", v13, "--txn", svs.meta["txn_id"], "--date", "2026-02-28", "--reason", "x")

    # close the period, then bookings inside it are refused
    ok("close-period", v13, "--through", "2026-02-28")
    v14 = latest(f, "2026")
    fails("closed period", "journal", v14, put(f / "late.json", {"schema": "journal/1", "entries": [{
        "date": "2026-02-15", "payee": "x", "confirmed": "user",
        "lines": [{"account": "Equity:Private", "amount": "1.00"}, {"account": "Assets:Cash", "amount": "-1.00"}]}]}))

    # fork: a second v15 written from v14 by another session
    stage_mar = {"schema": "statement/1", "statements": [{
        "source_id": "mar", "account": "Assets:Bank:Giro", "from": "2026-03-01", "to": "2026-12-31",
        "opening": "1130.00", "closing": "1130.00", "extraction": "manual", "rows": []}]}
    pend = ok("stage", v14, put(f / "mar.json", stage_mar))
    cash_rest = {"schema": "statement/1", "statements": [
        {"source_id": "kassa-rest", "account": "Assets:Cash", "kind": "cashbook", "from": "2026-03-01", "to": "2026-12-31",
         "opening": "430.00", "closing": "430.00", "extraction": "manual", "rows": []}]}
    pend_cash = ok("stage", v14, put(f / "cashrest.json", cash_rest))
    ok("commit", v14, pend["pending"])
    v15 = latest(f, "2026")
    fails("was staged on", "commit", v15, pend_cash["pending"])
    shutil.copy(v15, f / "2026-v15-copy.beancount")
    fork = f / "2026-v15-copy.beancount"
    fork.write_text(fork.read_text(encoding="utf-8").replace('"title" "', '"title" "Fork '), encoding="utf-8")
    st15 = ok("status", v15)
    assert st15["forks"], st15
    fork.unlink()
    (f / "cashrest.pending.json").unlink()
    pend_cash = ok("stage", v15, f / "cashrest.json")
    ok("commit", v15, pend_cash["pending"])
    v16 = latest(f, "2026")

    # year end: not closed yet, then close, then rollover
    for p in f.glob("*.pending.json"):
        data = json.loads(p.read_text())
        assert data.get("committed_in"), p
    fails("not_closed", "check", v16, "--year-end")
    ok("close-period", v16, "--through", "2026-12-31")
    v17 = latest(f, "2026")
    ok("check", v17, "--year-end")
    prev = ok("rollover", v17)
    assert prev["ready"] and not (f / "2027-v1.beancount").exists()
    ok("rollover", v17, "--write")
    n1 = f / "2027-v1.beancount"
    WRITTEN.append(n1)
    nb = ledger.Book.load(n1)
    old = ledger.Book.load(v17)
    assert nb.ledger["previous"]["sha256"] == old.sha and nb.fy_from.isoformat() == "2027-01-01"
    for acct in ("Assets:Bank:Giro", "Assets:Cash", "Assets:Fixed:Furniture", "Assets:VAT:Input", "Liabilities:VAT:Output"):
        assert nb.balance(acct) == old.balance(acct), acct
    assert nb.balance("Equity:Private") == 0 and nb.balance("Expenses:SVS") == 0
    result = -sum((old.balance(a) for a in old.accounts if a.split(":")[0] in ("Income", "Expenses")), D(0))
    assert nb.balance("Equity:Retained-Earnings") == old.balance("Equity:Retained-Earnings") - result + old.balance("Equity:Private")
    assert nb.assets["macbook"]["prior_depreciation"] == "500.00"
    assert ledger.depreciation_plan(nb)[0]["due"] == "500.00"
    fails("already exists", "rollover", v17, "--write")
    print("book A (AT, cash basis): ok")


# ── Book B: Germany, accrual (Bilanz), SKR03, invoice-basis VAT ──────────────

def book_b(tmp: Path) -> None:
    f = tmp / "b"
    f.mkdir()
    init = {
        "schema": "init/1", "title": "Beispiel GmbH", "file_label": "fy2026",
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "DE", "legal_form": "GmbH", "basis": "accrual", "vat_status": "regular",
                    "vat_basis": "invoice", "vat_period": "month", "chart": "skr03", "language": "en"},
        "chart": "skr03",
        "accounts": [
            {"account": "Assets:Bank:Giro", "role": "bank", "code": "1200", "name": "Sparkasse", "iban": "DE89370400440532013000"},
            {"account": "Assets:Receivables:10001-Kunde-Alt", "name": "Kunde Alt", "personenkonto": "10001"},
        ],
        "opening": {"lines": [
            {"account": "Assets:Bank:Giro", "amount": "10000.00"},
            {"account": "Assets:Receivables:10001-Kunde-Alt", "amount": "1190.00", "document": "billpal:old-1",
             "document_number": "RE-2025-77", "document_date": "2025-12-10", "due_date": "2026-01-09", "party": "Kunde Alt"},
            {"account": "Equity:Retained-Earnings", "amount": "-11000.00"}],
            "suspense_confirmed": True},
    }
    fails("opening", "init", put(f / "bad-init.json", {**init, "opening": {"lines": init["opening"]["lines"]}}), "--out-dir", f)
    ok("init", put(f / "init.json", init), "--out-dir", f)
    v1 = f / "fy2026-v1.beancount"
    b1 = ledger.Book.load(v1)
    assert b1.balance("Assets:Suspense") == D("-190.00")
    docs = {"schema": "documents/1", "documents": [
        {"id": "billpal:in-1", "kind": "invoice", "direction": "in", "number": "L-555", "date": "2026-01-10",
         "due_date": "2026-02-09", "party": {"name": "Lieferant Schulz KG", "iban": "DE02120300000000202051"},
         "total": "1190.00", "taxes": [{"regime": "standard", "rate": "19", "base": "1000.00", "amount": "190.00"}],
         "lines": [{"description": "Ware", "amount": "1000.00", "amount_is": "net", "account_code": "3400",
                    "tax": {"regime": "standard", "rate": "19"}}],
         "terms": {"skonto_percent": "2", "skonto_days": 10, "net_days": 30}, "linked": ["billpal:rcpt-1"]},
        {"id": "billpal:rcpt-1", "kind": "receipt", "direction": "in", "date": "2026-01-10", "total": "1190.00",
         "party": {"name": "Lieferant Schulz KG"}},
        {"id": "billpal:rem-1", "kind": "reminder", "direction": "in", "date": "2026-01-20", "total": "1190.00",
         "party": {"name": "Lieferant Schulz KG"}},
        {"id": "billpal:out-1", "kind": "invoice", "direction": "out", "number": "2026-0001", "date": "2026-01-15",
         "due_date": "2026-01-29", "party": {"name": "Kunde Neu AG", "personenkonto": "10002"}, "total": "2380.00",
         "taxes": [{"regime": "standard", "rate": "19", "base": "2000.00", "amount": "380.00"}],
         "lines": [{"amount": "1000.00", "amount_is": "net", "tax": {"regime": "standard", "rate": "19"}},
                   {"amount": "1000.00", "amount_is": "net", "tax": {"regime": "standard", "rate": "19"}}]},
        {"id": "billpal:cn-1", "kind": "credit_note", "direction": "out", "number": "GS-1", "date": "2026-01-20",
         "party": {"name": "Kunde Neu AG", "personenkonto": "10002"}, "total": "238.00", "applies_to": "billpal:out-1",
         "taxes": [{"regime": "standard", "rate": "19", "base": "200.00", "amount": "38.00"}]},
        {"id": "billpal:out-2", "kind": "invoice", "direction": "out", "number": "2026-0002", "date": "2026-01-16",
         "party": {"name": "Kunde Neu AG", "personenkonto": "10002"}, "total": "119.00",
         "taxes": [{"regime": "standard", "rate": "19", "base": "100.00", "amount": "19.00"}]},
        {"id": "billpal:out-3", "kind": "invoice", "direction": "out", "number": "2026-0003", "date": "2026-01-17",
         "party": {"name": "Kunde Neu AG", "personenkonto": "10002"}, "total": "238.00",
         "taxes": [{"regime": "standard", "rate": "19", "base": "200.00", "amount": "38.00"}]},
        {"id": "billpal:usd-1", "kind": "invoice", "direction": "in", "number": "INV-9", "date": "2026-01-18",
         "currency": "USD", "fx_rate": "0.9", "party": {"name": "Cloud Inc"}, "total": "100.00",
         "lines": [{"amount": "100.00", "amount_is": "net", "account_code": "4806",
                    "tax": {"regime": "reverse_charge", "rate": "19"}}]},
        {"id": "billpal:review-1", "kind": "invoice", "direction": "in", "date": "2026-01-19", "total": "50.00",
         "party": {"name": "Unklar"}, "review_open": True},
    ]}
    out = ok("stage-documents", v1, put(f / "docs.json", docs))
    assert out["counts"] == {"new": 6, "linked": 1, "excluded": 1, "review_blocked": 1}, out
    parties = {p["account"]: p for p in out["new_parties"]}
    assert "Liabilities:Payables:Lieferant-Schulz-KG" in parties and "Assets:Receivables:10002-Kunde-Neu-AG" in parties
    pend = json.loads(Path(out["pending"]).read_text())
    assert pend["documents"][0]["suggested"]["lines"][0]["account"] == "Expenses:Cost-Of-Goods"
    accounts = list({p["account"]: p for p in out["new_parties"]}.values())
    ok("commit-documents", v1, out["pending"], "--decisions", put(f / "docs.dec.json", {
        "accept_suggested": True, "accounts": accounts,
        "documents": [{"id": "billpal:out-1", "lines": [{"index": 0, "account": "Income:Sales"}, {"index": 1, "account": "Income:Sales"}], "confirmed": "user"},
                      {"id": "billpal:cn-1", "lines": [{"index": 0, "account": "Income:Sales"}], "confirmed": "user"},
                      {"id": "billpal:out-2", "lines": [{"index": 0, "account": "Income:Sales"}], "confirmed": "user"},
                      {"id": "billpal:out-3", "lines": [{"index": 0, "account": "Income:Sales"}], "confirmed": "user"}]}))
    v2 = latest(f, "fy2026")
    b2 = ledger.Book.load(v2)
    items, unalloc, errs = ledger.compute_items(b2)
    assert not errs, errs
    assert items["billpal:old-1"]["party"] == "Kunde Alt" and items["billpal:old-1"]["date"].isoformat() == "2025-12-10"
    # the Review document is held, not dropped: readiness fails from its date on
    assert [h["document"] for h in b2.holds] == ["billpal:review-1"], b2.holds
    early = {x["code"] for x in ledger.readiness(b2, ledger.parse_date("2026-01-18"))["fails"]}
    assert "review_open" not in early, early
    assert items["billpal:in-1"]["remaining"] == D("-1190.00")
    assert items["billpal:out-1"]["remaining"] == D("2142.00")
    assert items["billpal:usd-1"]["remaining"] == D("-90.00")
    assert b2.balance("Liabilities:VAT:Output-13b") == D("-17.10") and b2.balance("Assets:VAT:Input-13b") == D("17.10")
    assert b2.balance("Liabilities:VAT:Output-19") == D("-380.00") + D("38.00") - D("19.00") - D("38.00")

    bank = {"schema": "statement/1", "statements": [{
        "source_id": "spk-01", "account": "Assets:Bank:Giro", "from": "2026-01-01", "to": "2026-01-31",
        "opening": "10000.00", "closing": "12519.80", "extraction": "manual", "rows": [
            {"booking_date": "2026-01-12", "amount": "1190.00", "text": "RE-2025-77 Zahlung", "counterparty": "Kunde Alt"},
            {"booking_date": "2026-01-19", "amount": "-1166.20", "text": "L-555 abzgl 2% Skonto",
             "counterparty": "Lieferant Schulz KG", "iban": "DE02120300000000202051"},
            {"booking_date": "2026-01-25", "amount": "2142.00", "text": "Rechnung 2026-0001 abzgl Gutschrift",
             "counterparty": "Kunde Neu AG"},
            {"booking_date": "2026-01-28", "amount": "357.00", "text": "Kunde Neu AG Sammelzahlung"},
            {"booking_date": "2026-01-29", "amount": "-3.00", "text": "Kontofuehrung"}]}]}
    pend = ok("stage", v2, put(f / "bank.json", bank))
    cand = ok("candidates", v2, pend["pending"])
    by_ref = {r["ref"]: r for r in cand["rows"]}
    assert by_ref["1:1"]["candidates"][0]["type"] == "exact"
    assert by_ref["1:2"]["candidates"][0]["type"] == "skonto" and not by_ref["1:2"]["candidates"][0]["needs_user"]
    assert by_ref["1:3"]["candidates"][0]["type"] == "exact" and by_ref["1:3"]["candidates"][0]["documents"] == ["billpal:out-1"]
    batch = [c for c in by_ref["1:4"]["candidates"] if c["type"] == "batch"]
    assert batch and sorted(batch[0]["documents"]) == ["billpal:out-2", "billpal:out-3"], by_ref["1:4"]
    assert not by_ref["1:5"]["candidates"]
    dec = {"rows": [
        {"ref": "1:1", "lines": [{"account": "Assets:Receivables:10001-Kunde-Alt",
                                  "allocations": [{"document": "billpal:old-1", "amount": "-1190.00"}]}],
         "confirmed": "user", "match": by_ref["1:1"]["candidates"][0]["explanation"]},
        {"ref": "1:2", "lines": [{"account": "Liabilities:Payables:Lieferant-Schulz-KG",
                                  "allocations": [{"document": "billpal:in-1", "amount": "1190.00"}]},
                                 {"account": "Expenses:Discounts-Received",
                                  "tax": {"regime": "standard", "rate": "19", "source": "user"}}],
         "confirmed": "user", "match": by_ref["1:2"]["candidates"][0]["explanation"]},
        {"ref": "1:3", "lines": [{"account": "Assets:Receivables:10002-Kunde-Neu-AG",
                                  "allocations": [{"document": "billpal:out-1", "amount": "-2142.00"}]}], "confirmed": "user"},
        {"ref": "1:4", "lines": [{"account": "Assets:Receivables:10002-Kunde-Neu-AG",
                                  "allocations": [{"document": "billpal:out-2", "amount": "-119.00"},
                                                  {"document": None, "amount": "-238.00"}]}], "confirmed": "user"},
        {"ref": "1:5", "lines": [{"account": "Expenses:Bank-Charges", "tax": {"regime": "exempt", "source": "user"}}],
         "confirmed": "user", "no_document": "bank fee"}]}
    over = json.loads(json.dumps(dec))
    over["rows"][3]["lines"][0]["allocations"][0]["document"] = "billpal:out-1"
    fails("is on Assets:Receivables:10001", "commit", v2, pend["pending"], "--decisions", put(f / "wrong-party.json", {
        "rows": [*dec["rows"][:2], {**dec["rows"][2], "lines": [{**dec["rows"][2]["lines"][0], "allocations": [
            {"document": "billpal:old-1", "amount": "-2142.00"}]}]}, *dec["rows"][3:]]}))
    fails("goes past its open amount", "commit", v2, pend["pending"], "--decisions", put(f / "over.json", over))
    ok("commit", v2, pend["pending"], "--decisions", put(f / "bank.dec.json", dec))
    v3 = latest(f, "fy2026")
    b3 = ledger.Book.load(v3)
    items, unalloc, errs = ledger.compute_items(b3)
    assert not errs and items["billpal:in-1"]["remaining"] == 0 and items["billpal:old-1"]["remaining"] == 0
    assert unalloc == {"Assets:Receivables:10002-Kunde-Neu-AG": D("-238.00")}
    assert b3.balance("Expenses:Discounts-Received") == D("-20.00") and b3.balance("Assets:VAT:Input-19") == D("190.00") - D("3.80")
    rep = ledger.readiness(b3, ledger.parse_date("2026-01-31"))
    codes = {x["code"] for x in rep["fails"]}
    # a confirmed opening difference still blocks: the accountant has to clear it first
    assert {"unallocated", "suspense", "review_open"} <= codes, rep
    sus = next(x for x in rep["fails"] if x["code"] == "suspense")
    assert "opening difference" in sus["message"], sus
    review = next(x for x in rep["fails"] if x["code"] == "review_open")
    assert review["items"] == ["billpal:review-1"], review
    ok("journal", v3, put(f / "realloc.json", {"schema": "journal/1", "entries": [{
        "date": "2026-01-28", "payee": "Kunde Neu AG", "narration": "Sammelzahlung zuordnen", "confirmed": "user",
        "no_document": "allocation",
        "lines": [{"account": "Assets:Receivables:10002-Kunde-Neu-AG", "amount": "238.00",
                   "allocations": [{"document": None, "amount": "238.00"}]},
                  {"account": "Assets:Receivables:10002-Kunde-Neu-AG", "amount": "-238.00",
                   "allocations": [{"document": "billpal:out-3", "amount": "-238.00"}]}]}]}))
    v4 = latest(f, "fy2026")
    b4 = ledger.Book.load(v4)
    rep = ledger.readiness(b4, ledger.parse_date("2026-01-31"))
    codes = {x["code"] for x in rep["fails"]}
    assert codes == {"tax_unconfirmed", "unconfirmed"} or "unallocated" not in codes, rep
    wb = {d["document"]: d for d in ok("writeback", v4)["documents"]}
    in1 = wb["billpal:in-1"]["calls"]
    assert [c["tool"] for c in in1] == ["set_payment", "add_document_note"], in1
    assert in1[0]["arguments"] == {"document_ids": ["in-1"], "payment_date": "2026-01-19", "payment_status": "paid"}, in1
    assert "Skonto 23.80 EUR" in in1[1]["arguments"]["body"], in1
    assert wb["billpal:out-1"]["paid"] == "2142.00" and wb["billpal:out-1"]["calls"][0]["arguments"]["payment_status"] == "paid"
    assert wb["billpal:out-2"]["paid"] == "119.00" and wb["billpal:out-3"]["paid"] == "238.00", wb
    out = ok("report", v4, "--out-dir", f / "rep", "--to", "2026-01-31", "--lang", "en")
    assert out["reconciled"]
    data = json.loads((f / "rep" / "report-data.json").read_text())
    jan = next(t for t in data["vat"]["totals"] if t["period"] == "2026-01")
    assert D(jan["tax"]) == D("380.00") - D("38.00") + D("19.00") + D("38.00") + D("17.10"), jan
    assert D(jan["input_tax"]) == D("190.00") - D("3.80") + D("17.10"), jan
    open_docs = {r["document"]: r for r in data["open_items"]}
    assert set(open_docs) == {"billpal:usd-1"} and open_docs["billpal:usd-1"]["open"] == "90.00", open_docs
    assert "1200,00" not in (f / "rep" / "trial-balance.csv").read_text() and "Sparkasse,10000.00,3689.00,1169.20,12519.80" in (f / "rep" / "trial-balance.csv").read_text()
    # payment-basis VAT moves output tax to the payment month
    ok("journal", v4, put(f / "ist.json", {"schema": "journal/1", "profile": {"vat_basis": "payment"}}))
    v5 = latest(f, "fy2026")
    b5 = ledger.Book.load(v5)
    vat = ledger.vat_schedule(b5, b5.fy_from, ledger.parse_date("2026-01-31"))
    jan = next(t for t in vat["totals"] if t["period"] == "2026-01")
    assert jan["tax"] == D("380.00") - D("38.00") + D("19.00") + D("38.00") + D("17.10")
    vat_mid = ledger.vat_schedule(b5, b5.fy_from, ledger.parse_date("2026-01-24"))
    mid = next(t for t in vat_mid["totals"] if t["period"] == "2026-01")
    assert mid["tax"] == D("17.10"), mid
    # rollover carries open items per document
    prev = ledger.build_rollover(b5, None, None, False)
    lines = [l for l in prev["opening_lines"] if l.get("document")]
    assert [l["document"] for l in lines] == ["billpal:usd-1"] and lines[0]["amount"] == "-90.00", lines
    # payment basis: an unpaid invoice reversed a month later moves no VAT into any period
    out = ok("stage-documents", v5, put(f / "out4.json", {"schema": "documents/1", "documents": [
        {"id": "billpal:out-4", "kind": "invoice", "direction": "out", "number": "2026-0004", "date": "2026-01-20",
         "party": {"name": "Kunde Neu AG", "personenkonto": "10002"}, "total": "119.00",
         "taxes": [{"regime": "standard", "rate": "19", "base": "100.00", "amount": "19.00"}]}]}))
    ok("commit-documents", v5, out["pending"], "--decisions", put(f / "out4.dec.json", {"accept_suggested": True, "documents": [
        {"id": "billpal:out-4", "lines": [{"index": 0, "account": "Income:Sales"}], "confirmed": "user"}]}))
    totals = lambda b: {(t["period"], t["tax"]) for t in ledger.vat_schedule(b, b.fy_from, b.fy_to)["totals"]}
    before = totals(ledger.Book.load(latest(f, "fy2026")))
    out4 = next(t for t in ledger.Book.load(latest(f, "fy2026")).txns if t.meta.get("document") == "billpal:out-4")
    ok("reverse", latest(f, "fy2026"), "--txn", out4.meta["txn_id"], "--date", "2026-02-10", "--reason", "issued twice")
    assert totals(ledger.Book.load(latest(f, "fy2026"))) == before, (before, totals(ledger.Book.load(latest(f, "fy2026"))))

    # write-back across years: an invoice carried in with 500 open; amount_paid counts last year's payments too
    g = f / "carry"
    g.mkdir()
    ok("init", put(g / "init.json", {**init, "file_label": "c", "opening": {"lines": [
        {"account": "Assets:Bank:Giro", "amount": "1000.00"},
        {"account": "Assets:Receivables:10001-Kunde-Alt", "amount": "500.00", "document": "billpal:carry",
         "document_number": "RE-2025-80", "document_date": "2025-12-01", "party": "Kunde Alt"},
        {"account": "Equity:Retained-Earnings", "amount": "-1500.00"}]}}), "--out-dir", g)
    ok("journal", latest(g, "c"), put(g / "pay.json", {"schema": "journal/1", "entries": [{
        "date": "2026-01-10", "payee": "Kunde Alt", "confirmed": "user", "no_document": "test",
        "lines": [{"account": "Assets:Bank:Giro", "amount": "200.00"},
                  {"account": "Assets:Receivables:10001-Kunde-Alt", "amount": "-200.00",
                   "allocations": [{"document": "billpal:carry", "amount": "-200.00"}]}]}]}))
    wb = ok("writeback", latest(g, "c"))["documents"][0]
    assert not wb["calls"] and "before this ledger is unknown" in wb["note"], wb
    carry = put(g / "carry.json", {"schema": "documents/1", "documents": [{
        "id": "billpal:carry", "kind": "invoice", "direction": "out", "date": "2025-12-01", "total": "800.00",
        "party": {"name": "Kunde Alt"}}]})
    wb = ok("writeback", latest(g, "c"), "--documents", carry)["documents"][0]
    assert wb["paid"] == "500.00" and wb["calls"][0]["arguments"] == {"document_id": "carry", "amount_paid": 500.0}, wb
    print("book B (DE, accrual): ok")


# ── Book D: regressions from review ──────────────────────────────────────────

def book_d(tmp: Path) -> None:
    """DE accrual: discounts, unknown amount basis, Billpal null tax fields, review holds, linked receipts,
    Personenkonten with the same name, rebooking after a reversal, identical payments across an overlap,
    write-back, closed tax periods, fixed assets without a record, report codes, malformed input."""
    f = tmp / "d"
    f.mkdir()
    ok("init", put(f / "init.json", {
        "schema": "init/1", "title": "Regress GmbH", "file_label": "d",
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "DE", "legal_form": "GmbH", "basis": "accrual", "vat_status": "regular",
                    "vat_basis": "invoice", "vat_period": "month", "chart": "skr03", "language": "de"},
        "chart": "skr03", "new_business": True,
        "accounts": [{"account": "Liabilities:Payables:70001-Mueller", "name": "Müller", "personenkonto": "70001"}]}),
       "--out-dir", f)
    v = f / "d-v1.beancount"
    b = ledger.Book.load(v)
    asset = {"id": "x", "in_service": "2026-02-01", "cost": "100.00", "life_months": 12,
             "asset_account": "Assets:Fixed:Furniture", "depreciation_account": "Expenses:Depreciation"}
    raises("cost must be positive", ledger.validate_asset, {**asset, "cost": "-1.00"}, b)
    raises("residual is between", ledger.validate_asset, {**asset, "residual": "150.00"}, b)
    raises("prior_depreciation is between", ledger.validate_asset, {**asset, "prior_depreciation": "-5.00"}, b)
    raises("disposed is on or after", ledger.validate_asset, {**asset, "disposed": "2026-01-15"}, b)
    raises("on or after the acquisition", ledger.validate_asset, {**asset, "acquired": "2026-03-01"}, b)
    xml = camt("DD", "0.00", "-10.00", "2026-03-01", "2026-03-31", [("R9", "2026-03-02", "-10.00", "Versicherung", "Beitrag", "BOOK")])
    xml = xml.replace("<AcctSvcrRef>R9", "<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RDDT</Cd></Fmly></Domn></BkTxCd><AcctSvcrRef>R9")
    assert ledger.from_camt(xml.encode(), b)["statements"][0]["rows"][0]["method"] == "direct_debit"

    vat19 = {"regime": "standard", "rate": "19"}
    docs = {"schema": "documents/1", "documents": [
        # lines before a 10 % discount, total and tax base after it
        {"id": "billpal:disc", "kind": "invoice", "direction": "in", "number": "D-1", "date": "2026-01-05",
         "party": {"name": "Rabatt AG"}, "total": "214.20", "discount": "23.80",
         "taxes": [{"type": "vat", **vat19, "base": "180.00", "amount": "34.20"}],
         "lines": [{"amount": "119.00", "amount_is": "gross", "account_code": "4985", "tax": vat19},
                   {"amount": "119.00", "amount_is": "gross", "account_code": "4985", "tax": vat19}]},
        # a payment-method discount leaves the total alone
        {"id": "billpal:paydisc", "kind": "invoice", "direction": "in", "number": "D-2", "date": "2026-01-06",
         "party": {"name": "Müller", "personenkonto": "70001"}, "total": "119.00", "discount": "2.38",
         "taxes": [{**vat19, "base": "100.00", "amount": "19.00"}],
         "lines": [{"amount": "100.00", "amount_is": "net", "account_code": "4985", "tax": vat19}]},
        # Billpal: amount_basis unknown, line tax_type and tax_regime null, the header carries the regime
        {"id": "billpal:unknown", "kind": "invoice", "direction": "in", "number": "D-3", "date": "2026-01-07",
         "party": {"name": "Müller", "personenkonto": "70002"}, "total": "119.00",
         "taxes": [{"type": "vat", **vat19, "base": "100.00", "amount": "19.00"}],
         "lines": [{"amount": "100.00", "amount_is": "unknown", "account_code": "4985",
                    "tax": {"type": None, "regime": None, "rate": "19"}}]},
        {"id": "billpal:rc", "kind": "invoice", "direction": "in", "number": "D-4", "date": "2026-01-08",
         "party": {"name": "Cloud Ltd"}, "total": "50.00",
         "lines": [{"amount": "50.00", "amount_is": "unknown", "account_code": "4806",
                    "tax": {"regime": "reverse_charge", "rate": "19"}}]},
        {"id": "billpal:rcpt-early", "kind": "receipt", "direction": "in", "date": "2026-01-09",
         "party": {"name": "Shop KG"}, "total": "59.50",
         "lines": [{"amount": "59.50", "account_code": "4985", "tax": vat19}]},
        # three decimals in the document currency
        {"id": "billpal:kwd", "kind": "invoice", "direction": "in", "number": "K-1", "date": "2026-01-10",
         "currency": "KWD", "fx_rate": "3", "party": {"name": "Gulf Co"}, "total": "1.190",
         "taxes": [{**vat19, "base": "1.000", "amount": "0.190"}],
         "lines": [{"amount": "1.000", "amount_is": "net", "account_code": "4985", "tax": {**vat19, "amount": "0.190"}}]},
        {"id": "billpal:held", "kind": "invoice", "direction": "in", "number": "H-1", "date": "2026-01-11",
         "party": {"name": "Shop KG"}, "total": "11.90", "review_open": True,
         "lines": [{"amount": "11.90", "account_code": "4985", "tax": vat19}]},
    ]}
    out = ok("stage-documents", v, put(f / "docs1.json", docs))
    assert out["counts"] == {"new": 6, "review_blocked": 1}, out
    pend = json.loads(Path(out["pending"]).read_text())
    how = {d["ref"]: d["suggested"]["party_matched_by"] for d in pend["documents"]}
    assert how["billpal:paydisc"] == "personenkonto" and how["billpal:unknown"] == "new", how
    sugg = {d["ref"]: d["suggested"] for d in pend["documents"]}
    assert sugg["billpal:unknown"]["party_account"] == "Liabilities:Payables:70002-Mueller", sugg["billpal:unknown"]
    assert sugg["billpal:held"]["party_account"] == sugg["billpal:rcpt-early"]["party_account"]
    accounts = list({p["account"]: p for p in out["new_parties"]}.values())
    ok("commit-documents", v, out["pending"], "--decisions", put(f / "docs1.dec.json", {"accept_suggested": True, "accounts": accounts}))
    v = latest(f, "d")
    b = ledger.Book.load(v)
    txn = {t.meta.get("document"): t for t in b.txns if t.meta.get("document")}
    on = lambda t, a: sum((p.amount for p in t.postings if p.account == a), D(0))
    assert on(txn["billpal:disc"], "Expenses:Small-Equipment") == D("180.00") and txn["billpal:disc"].meta["discount"] == "23.80"
    assert on(txn["billpal:disc"], "Assets:VAT:Input-19") == D("34.20")
    assert on(txn["billpal:paydisc"], "Expenses:Small-Equipment") == D("100.00") and "discount" not in txn["billpal:paydisc"].meta
    assert on(txn["billpal:unknown"], "Expenses:Small-Equipment") == D("100.00")
    assert on(txn["billpal:rc"], "Expenses:Software") == D("50.00") and on(txn["billpal:rc"], "Assets:VAT:Input-13b") == D("9.50")
    assert on(txn["billpal:kwd"], "Liabilities:Payables:Gulf-Co") == D("-3.57")
    assert "billpal:held" not in txn and [h["document"] for h in b.holds] == ["billpal:held"]
    rep = ledger.readiness(b, ledger.parse_date("2026-01-31"))
    assert next(x for x in rep["fails"] if x["code"] == "review_open")["items"] == ["billpal:held"], rep

    # second fetch: the invoice for an already booked receipt, a name shared by two Personenkonten,
    # and the Review document resolved in Billpal
    docs2 = {"schema": "documents/1", "documents": [
        {"id": "billpal:inv-late", "kind": "invoice", "direction": "in", "number": "S-9", "date": "2026-01-09",
         "party": {"name": "Shop KG"}, "total": "59.50", "linked": ["billpal:rcpt-early"],
         "lines": [{"amount": "59.50", "account_code": "4985", "tax": vat19}]},
        {"id": "billpal:noid", "kind": "invoice", "direction": "in", "number": "M-7", "date": "2026-01-12",
         "party": {"name": "Müller"}, "total": "11.90", "lines": [{"amount": "11.90", "account_code": "4985", "tax": vat19}]},
        docs["documents"][-1],
    ]}
    out = ok("stage-documents", v, put(f / "docs2.json", docs2))
    pend = json.loads(Path(out["pending"]).read_text())
    st = {d["ref"]: d for d in pend["documents"]}
    assert st["billpal:inv-late"]["status"] == "already_booked", st["billpal:inv-late"]
    assert st["billpal:noid"]["suggested"]["party_matched_by"] == "ambiguous" and not st["billpal:noid"]["suggested"]["party_account"]
    fails("party account None", "commit-documents", v, out["pending"], "--decisions",
          put(f / "docs2.bad.json", {"accept_suggested": True}))
    # a name-only match is never accepted silently; unknown decision fields are rejected
    assert st["billpal:held"]["suggested"]["party_matched_by"] == "name", st["billpal:held"]
    fails("found by name only", "commit-documents", v, out["pending"], "--decisions", put(f / "docs2.name.json", {
        "accept_suggested": True, "documents": [{"id": "billpal:noid", "party_account": "Liabilities:Payables:70001-Mueller"},
                                                {"id": "billpal:held", "review_resolved": True}]}))
    fails("unknown fields ['acount']", "commit-documents", v, out["pending"], "--decisions", put(f / "docs2.typo.json", {
        "accept_suggested": True, "documents": [{"id": "billpal:noid", "acount": "x"}]}))
    fails("each line override", "commit-documents", v, out["pending"], "--decisions", put(f / "docs2.typo2.json", {
        "accept_suggested": True, "documents": [{"id": "billpal:noid", "lines": [{"index": 0, "acount": "x"}]}]}))
    ok("commit-documents", v, out["pending"], "--decisions", put(f / "docs2.dec.json", {"accept_suggested": True, "documents": [
        {"id": "billpal:noid", "party_account": "Liabilities:Payables:70001-Mueller"},
        {"id": "billpal:held", "review_resolved": True, "party_confirmed": True}]}))
    v = latest(f, "d")
    b = ledger.Book.load(v)
    assert "review_open" not in {x["code"] for x in ledger.readiness(b, ledger.parse_date("2026-01-31"))["fails"]}

    # a reversed document can be booked again
    pay = next(t for t in b.txns if t.meta.get("document") == "billpal:paydisc")
    ok("reverse", v, "--txn", pay.meta["txn_id"], "--date", "2026-01-31", "--reason", "wrong account")
    v = latest(f, "d")
    out = ok("stage-documents", v, put(f / "docs3.json", {"schema": "documents/1", "documents": [docs["documents"][1]]}))
    assert out["counts"] == {"new": 1}, out
    ok("commit-documents", v, out["pending"], "--decisions", put(f / "docs3.dec.json", {"accept_suggested": True}))
    v = latest(f, "d")
    items, _, errs = ledger.compute_items(ledger.Book.load(v))
    assert not errs and items["billpal:paydisc"]["remaining"] == D("-119.00"), (errs, items.get("billpal:paydisc"))

    # payments: two partial payments, one direct debit, a parking fee
    feb1 = {"schema": "statement/1", "statements": [{
        "source_id": "spk-feb-1", "account": "Assets:Bank:Giro", "from": "2026-02-01", "to": "2026-02-10",
        "opening": "0.00", "closing": "-279.00", "extraction": "manual", "rows": [
            {"booking_date": "2026-02-03", "amount": "-100.00", "text": "Teilzahlung D-1", "counterparty": "Rabatt AG"},
            {"booking_date": "2026-02-05", "amount": "-50.00", "text": "Teilzahlung D-1", "counterparty": "Rabatt AG"},
            {"booking_date": "2026-02-06", "amount": "-119.00", "text": "SEPA-Lastschrift Mandat M-1 D-2", "counterparty": "Müller"},
            {"booking_date": "2026-02-10", "amount": "-5.00", "text": "Parkgebuehr"},
            {"booking_date": "2026-02-10", "amount": "-5.00", "text": "Parkgebuehr"}]}]}
    pend = ok("stage", v, put(f / "feb1.json", feb1))
    assert pend["statements"][0]["counts"] == {"new": 5}, pend["statements"][0]
    ap = lambda acct, doc, amt: [{"account": acct, "allocations": [{"document": doc, "amount": amt}]}]
    fee = {"lines": [{"account": "Expenses:Bank-Charges", "tax": {"regime": "exempt", "source": "user"}}],
           "confirmed": "user", "no_document": "parking"}
    ok("commit", v, pend["pending"], "--decisions", put(f / "feb1.dec.json", {"rows": [
        {"ref": "1:1", "lines": ap("Liabilities:Payables:Rabatt-AG", "billpal:disc", "100.00"), "confirmed": "user"},
        {"ref": "1:2", "lines": ap("Liabilities:Payables:Rabatt-AG", "billpal:disc", "50.00"), "confirmed": "user"},
        {"ref": "1:3", "lines": ap("Liabilities:Payables:70001-Mueller", "billpal:paydisc", "119.00"), "confirmed": "user"},
        {"ref": "1:4", **fee}, {"ref": "1:5", **fee}]}))
    v = latest(f, "d")
    wb = {d["document"]: d for d in ok("writeback", v)["documents"]}
    calls = wb["billpal:disc"]["calls"]
    assert [c["tool"] for c in calls] == ["set_amount_paid", "set_payment"], calls
    assert calls[0]["arguments"] == {"document_id": "disc", "amount_paid": 150.0}, calls
    assert calls[1]["arguments"] == {"document_ids": ["disc"], "payment_date": "2026-02-05", "payment_status": "partially_paid"}, calls
    dd = wb["billpal:paydisc"]["calls"]
    assert dd == [{"tool": "set_payment", "arguments": {"document_ids": ["paydisc"], "payment_date": "2026-02-06",
                                                        "payment_method": "direct_debit", "payment_status": "paid"}}], dd

    # the same file id with other rows is refused; a new export overlapping one day finds both real fees booked
    changed = json.loads(json.dumps(feb1))
    changed["statements"][0]["rows"][3]["text"] = "Parkgebuehr Altstadt"
    fails("booked before with different rows", "stage", v, put(f / "feb1-changed.json", changed))
    feb2 = {"schema": "statement/1", "statements": [{
        "source_id": "spk-feb-2", "account": "Assets:Bank:Giro", "from": "2026-02-10", "to": "2026-02-20",
        "opening": "-269.00", "closing": "-280.00", "extraction": "manual", "rows": [
            {"booking_date": "2026-02-10", "amount": "-5.00", "text": "Parkgebuehr"},
            {"booking_date": "2026-02-10", "amount": "-5.00", "text": "Parkgebuehr"},
            {"booking_date": "2026-02-15", "amount": "-1.00", "text": "Kontofuehrung"}]}]}
    pend = ok("stage", v, put(f / "feb2.json", feb2))
    assert pend["statements"][0]["counts"] == {"duplicate": 2, "new": 1}, pend["statements"][0]
    ok("commit", v, pend["pending"], "--decisions", put(f / "feb2.dec.json", {"rows": [{"ref": "1:3", **fee}]}))
    v = latest(f, "d")
    assert ledger.Book.load(v).balance("Assets:Bank:Giro") == D("-280.00")

    # January is ready and closes; after that no tax point and no VAT basis may move into it
    ok("check", v, "--ready", "--through", "2026-01-31")
    ok("close-period", v, "--through", "2026-01-31")
    v = latest(f, "d")
    fails("tax point 2026-01-20 is in the period closed", "journal", v, put(f / "late-tax.json", {"schema": "journal/1", "entries": [{
        "date": "2026-02-12", "payee": "Nachtrag", "confirmed": "user", "no_document": "test",
        "lines": [{"account": "Expenses:Small-Equipment", "amount": "11.90",
                   "tax": {**vat19, "point": "2026-01-20", "source": "user"}}, {"account": "Equity:Private"}]}]}))
    fails("cannot change after", "journal", v, put(f / "ist.json", {"schema": "journal/1", "profile": {"vat_basis": "payment"}}))

    # one treatment, three report codes; fixed assets without a record or with another cost
    rc_codes = [{"code": "46", "part": "base"}, {"code": "47", "part": "tax"}, {"code": "67", "part": "input_tax"}]
    oos = {"regime": "out_of_scope", "source": "user"}
    ok("journal", v, put(f / "feb-misc.json", {"schema": "journal/1", "assets": [{**asset, "id": "chair", "cost": "400.00",
                                                                                 "in_service": "2026-02-12"}],
        "entries": [
            {"date": "2026-02-12", "payee": "Cloud Ltd", "confirmed": "user", "no_document": "test",
             "lines": [{"account": "Expenses:Software", "amount": "100.00",
                        "tax": {"regime": "reverse_charge", "rate": "19", "source": "user", "report_codes": rc_codes}},
                       {"account": "Equity:Private-Deposits"}]},
            {"date": "2026-02-12", "payee": "Möbel KG", "confirmed": "user", "no_document": "test",
             "lines": [{"account": "Assets:Fixed:Furniture", "amount": "500.00", "asset": "chair", "tax": oos},
                       {"account": "Assets:Fixed:Furniture", "amount": "100.00", "tax": oos},
                       {"account": "Equity:Private-Deposits"}]}]}))
    v = latest(f, "d")
    b = ledger.Book.load(v)
    codes = {c["code"]: c["amount"] for c in ledger.vat_schedule(b, b.fy_from, b.fy_to)["codes"] if c["period"] == "2026-02"}
    assert codes == {"46": D("100.00"), "47": D("19.00"), "67": D("19.00")}, codes
    feb = ledger.readiness(b, ledger.parse_date("2026-02-20"))
    assert {"assets_unregistered", "asset_cost"} <= {w["code"] for w in feb["warnings"]}, feb
    ye = {x["code"] for x in ledger.readiness(b, None, year_end=True)["fails"]}
    assert {"assets_unregistered", "asset_cost", "not_closed"} <= ye, ye
    fails("assets_unregistered", "close-period", v, "--through", "2026-12-31")
    # a credit note on an asset invoice lowers the asset's cost; a line override names the asset
    ok("journal", v, put(f / "desk.json", {"schema": "journal/1", "assets": [
        {**asset, "id": "desk", "cost": "400.00", "in_service": "2026-02-14", "document": "billpal:desk-inv"}]}))
    v = latest(f, "d")
    mk = {"party": {"name": "Möbel KG"}, "kind": "invoice", "direction": "in"}
    out = ok("stage-documents", v, put(f / "desk-docs.json", {"schema": "documents/1", "documents": [
        {**mk, "id": "billpal:desk-inv", "number": "MK-1", "date": "2026-02-14", "total": "595.00",
         "lines": [{"amount": "500.00", "amount_is": "net", "account_code": "0420", "tax": vat19}]},
        {**mk, "id": "billpal:desk-cn", "kind": "credit_note", "number": "MK-1-G", "date": "2026-02-16", "total": "119.00",
         "applies_to": "billpal:desk-inv", "lines": [{"amount": "100.00", "amount_is": "net", "account_code": "0420", "tax": vat19}]}]}))
    ok("commit-documents", v, out["pending"], "--decisions", put(f / "desk.dec.json", {
        "accept_suggested": True, "accounts": list({p["account"]: p for p in out["new_parties"]}.values()),
        "documents": [{"id": "billpal:desk-inv", "lines": [{"index": 0, "asset": "desk"}]},
                      {"id": "billpal:desk-cn", "party_confirmed": True}]}))
    v = latest(f, "d")
    b = ledger.Book.load(v)
    txn = {t.meta.get("document"): t for t in b.txns if t.meta.get("document")}
    assert [p.meta.get("asset") for p in txn["billpal:desk-inv"].postings if p.account == "Assets:Fixed:Furniture"] == ["desk"]
    assert on(txn["billpal:desk-cn"], "Assets:Fixed:Furniture") == D("-100.00")
    off = [x for w in ledger.readiness(b, ledger.parse_date("2026-02-28"))["warnings"] if w["code"] == "asset_cost" for x in w["items"]]
    assert not any(x.startswith("desk") for x in off), off
    out = ok("report", v, "--out-dir", f / "rep", "--to", "2026-12-31")
    data = json.loads((f / "rep" / "report-data.json").read_text())
    assert not out["ready"] and data["meta"]["year_end"] and any("assets_unregistered" in x for x in out["fails"]), out

    # accrual: the derived reverse charge is written onto the pending document lines that commit-documents books
    sd = ledger.stage_documents(b, {"schema": "documents/1", "documents": [
        {"id": "billpal:us-rc", "kind": "invoice", "direction": "in", "date": "2026-03-01", "total": "50.00",
         "party": {"name": "Cloud Inc", "country": "US"}}]})["documents"][0]
    assert sd["suggested"]["derived_tax"]["rate"] == "19" and sd["document"]["lines"][0]["tax"]["regime"] == "reverse_charge", sd

    # malformed input comes back as JSON errors, never a traceback
    for args in (("bogus",), ("stage", v), ("stage", v, f / "missing.json")):
        assert not cli(*args)["ok"], args
    fails("report dates must lie in the fiscal year", "report", v, "--out-dir", f / "rep2", "--from", "2025-12-01")
    fails("report dates must lie in the fiscal year", "report", v, "--out-dir", f / "rep2", "--from", "2026-03-01", "--to", "2026-02-01")
    bad_prec = f / "prec"
    bad_prec.mkdir()
    fails("precision 9", "init", put(bad_prec / "init.json", {
        "schema": "init/1", "file_label": "p", "chart": "skr03", "new_business": True,
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR", "precision": 9},
        "profile": {"country": "DE", "legal_form": "GmbH", "basis": "cash", "vat_status": "none", "chart": "skr03"}}),
          "--out-dir", bad_prec)
    assert not list(bad_prec.glob("*.beancount"))
    broken = put(f / "broken.json", {"schema": "statement/1", "statements": [{
        "source_id": "x", "account": "Assets:Bank:Giro", "from": "2026-03-01", "to": "2026-03-31", "rows": [{"booking_date": "2026-03-02", "amount": []}]}]})
    assert not cli("stage", v, broken)["ok"]
    print("book D (review regressions): ok")


# ── Book E: Austrian owner, cost shares, private statements, Billpal results ──

def billpal_doc(**kw) -> dict:
    """One get_document result with every field Billpal returns."""
    return {"id": None, "updated_at": "2026-02-02T10:00:00Z", "status": "processed", "type": "invoice",
            "direction": "incoming", "invoice_number": None, "receipt_number": None, "invoice_date": "2026-02-02",
            "payment_due_date": None, "currency": "EUR", "total_amount": None, "net_amount": None, "tip_amount": None,
            "tax_amount": None, "discount": None, "reverse_charge": False, "tax_lines": [], "line_items": [],
            "supplier": None, "customer": None, "bank_accounts": {"supplier": [], "customer": []},
            "linked_documents": [], "review_notes": [], **kw}


def billpal_line(total, rate, **kw) -> dict:
    return {"id": "li", "description": "Item", "quantity": 1, "unit_price": total, "total": total, "amount_basis": "net",
            "tax_type": None, "tax_regime": None, "tax_rate": rate, "tax_amount": None, "exemption_reason": None,
            "chart_account_id": None, "chart_account_set_by_user": False, **kw}


def book_e(tmp: Path) -> None:
    """AT owner on cash basis: a rule with a business share, private-account allocations, the flat-rate
    either/or warning, a bank account opened mid-year, Billpal get_document results end to end, and review
    holds that commit keeps without being asked."""
    f = tmp / "e"
    f.mkdir()
    oos = {"regime": "out_of_scope", "source": "rule"}
    v20 = {"regime": "standard", "rate": "20"}
    home = {"basis": "8.5 m² of 52 m²", "share": "8.5/52", "topic": "home_office"}
    ok("init", put(f / "init.json", {
        "schema": "init/1", "title": "Leeb e.U.", "file_label": "e",
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "AT", "legal_form": "Einzelunternehmen", "basis": "cash", "vat_status": "regular",
                    "vat_basis": "payment", "vat_period": "quarter", "chart": "oekr", "language": "de"},
        "chart": "oekr", "new_business": True,
        "accounts": [{"account": "Assets:Bank:Wise", "role": "bank", "name": "Wise"},
                     {"account": "Assets:Bank:Revolut", "role": "bank", "name": "Revolut", "date": "2026-03-01"}],
        "rules": [{"id": "phone", "text": "Magenta", "money": "out", "account": "Expenses:Phone", "share": "0.6", "tax": v20}],
        "allocations": [
            {"id": "rent", "text": "PMV Immobilien", "money": "out", "account": "Expenses:Rent", "tax": oos,
             "every": "month", "document": "Mietvertrag 2024", **home},
            {"id": "energy", "text": "Wien Energie", "money": "out", "account": "Expenses:Utilities", "tax": v20, **home}]}),
       "--out-dir", f)
    v = f / "e-v1.beancount"
    b = ledger.Book.load(v)
    assert ledger.parse_share("8.5/52", "x") == D("8.5") / D(52) and ledger.parse_share(1, "x") == 1
    for bad in ("0", "1.5", "x", "1/0", True, None):
        raises("share", ledger.parse_share, bad, "x")
    spec = {"id": "x", "text": "a", "account": "Expenses:Rent", "share": "0.5"}
    raises("every is month", ledger.validate_allocation, {**spec, "every": "week"}, b)
    raises("needs role private", ledger.validate_allocation, {**spec, "private_account": "Expenses:Phone"}, b)
    raises("needs share", ledger.validate_allocation, {**spec, "share": None}, b)
    raises("unknown fields", ledger.validate_allocation, {**spec, "shares": "1"}, b)
    raises("unknown fields", ledger.validate_rule, {**spec, "every": "month"}, b)

    # a phone bill paid from the business account: 60 % business with VAT, the rest is a private withdrawal
    jan = {"schema": "statement/1", "statements": [{
        "source_id": "wise-jan", "account": "Assets:Bank:Wise", "from": "2026-01-01", "to": "2026-01-31",
        "opening": "0.00", "closing": "1950.00", "extraction": "manual", "rows": [
            {"booking_date": "2026-01-05", "amount": "2000.00", "counterparty": "Kunde AG", "text": "Rechnung 1"},
            {"booking_date": "2026-01-20", "amount": "-50.00", "counterparty": "Magenta Telekom", "text": "Rechnung 01"}]}]}
    pend = ok("stage", v, put(f / "jan.json", jan))
    sug = json.loads(Path(pend["pending"]).read_text())["statements"][0]["rows"][1]["suggested"]["lines"]
    assert [(x["account"], x["amount"]) for x in sug] == [("Expenses:Phone", "30.00"), ("Equity:Private", "20.00")], sug
    ok("commit", v, pend["pending"], "--decisions", put(f / "jan.dec.json", {"accept_suggested": True, "rows": [
        {"ref": "1:1", "lines": [{"account": "Income:Services", "tax": {**v20, "source": "user"}}], "confirmed": "user",
         "no_document": "test"}, {"ref": "1:2", "document": "billpal:magenta-01"}]}))
    v = latest(f, "e")
    b = ledger.Book.load(v)
    assert (b.balance("Expenses:Phone"), b.balance("Assets:VAT:Input"), b.balance("Equity:Private")) == (
        D("25.00"), D("5.00"), D("20.00"))

    # rent and energy paid from the private N26: only the office share is booked, the account stays out
    n26 = {"schema": "statement/1", "statements": [{
        "source_id": "n26-q1", "account": "N26", "from": "2026-01-01", "to": "2026-03-31", "rows": [
            {"booking_date": "2026-01-03", "amount": "-910.00", "counterparty": "PMV Immobilien GmbH", "text": "Miete 01"},
            {"booking_date": "2026-01-10", "amount": "-64.20", "counterparty": "Spar", "text": "Einkauf"},
            {"booking_date": "2026-01-15", "amount": "-104.00", "counterparty": "Wien Energie", "text": "Teilbetrag"},
            {"booking_date": "2026-03-03", "amount": "-910.00", "counterparty": "PMV Immobilien GmbH", "text": "Miete 03"}]}]}
    out = ok("allocate", v, put(f / "n26.json", n26))
    assert out["rows_without_allocation"] == 1 and "Spar" not in json.dumps(out), out
    assert [(e["allocation"], e["lines"][0]["amount"], e["lines"][1]) for e in out["entries"]] == [
        ("rent", "148.75", {"account": "Equity:Private", "amount": "-148.75"}),
        ("energy", "17.00", {"account": "Equity:Private", "amount": "-17.00"}),
        ("rent", "148.75", {"account": "Equity:Private", "amount": "-148.75"})], out["entries"]
    fails("is in the books", "allocate", v, put(f / "wise-as-private.json", jan))
    ok("journal", v, out["written"])
    v = latest(f, "e")
    b = ledger.Book.load(v)
    assert (b.balance("Expenses:Rent"), b.balance("Expenses:Utilities"), b.balance("Assets:VAT:Input")) == (
        D("297.50"), D("14.17"), D("7.83"))
    assert b.balance("Equity:Private") == D("20.00") - D("314.50")
    fails("already allocated", "journal", v, out["written"])
    again = ok("allocate", v, f / "n26.json")
    assert not again["entries"] and {s["note"] for s in again["skipped"]} == {"already booked"}, again

    # readiness: the February rent is missing; Revolut, opened 1 March, owes statements from March on only
    ok("journal", v, put(f / "revolut.json", {"schema": "journal/1", "entries": [{
        "date": "2026-03-05", "payee": "Einlage", "confirmed": "user", "no_document": "private deposit",
        "lines": [{"account": "Assets:Bank:Revolut", "amount": "100.00"}, {"account": "Equity:Private"}]}]}))
    v = latest(f, "e")
    b = ledger.Book.load(v)
    rep = ledger.readiness(b, ledger.parse_date("2026-03-31"))
    warn = {w["code"]: w["items"] for w in rep["warnings"]}
    assert warn["allocation_missing"] == ["rent: 2026-02"], warn
    gaps = next(x for x in rep["fails"] if x["code"] == "statements_missing")["items"]
    assert "Assets:Bank:Revolut: 2026-03-01 to 2026-03-31" in gaps and "Assets:Bank:Wise: 2026-02-01 to 2026-03-31" in gaps, gaps
    feb = ledger.readiness(b, ledger.parse_date("2026-02-28"))
    assert not any("Revolut" in g for x in feb["fails"] for g in x["items"]), feb

    # Billpal: get_document results exactly as the MCP returns them
    shop = {"id": "c-shop", "company_name": "Shop KG", "brand_name": None, "tax_id": None, "website": None, "is_your_company": False}
    bp = {"documents": [
        billpal_doc(id="inv-1", invoice_number="S-1", total_amount=108.0, discount=10.0, supplier=shop,
                    tax_lines=[{"tax_type": "vat", "tax_regime": "standard", "tax_rate": 20, "tax_base": 90.0,
                                "tax_amount": 18.0, "label": None, "exemption_reason": None}],
                    line_items=[billpal_line(100.0, 20)],
                    bank_accounts={"supplier": [{"scheme": "iban", "accountIdentifier": IBAN_A, "routingIdentifier": None}],
                                   "customer": []}),
        billpal_doc(id="rcpt-1", type="receipt", total_amount=108.0, supplier=shop, linked_documents=[
            {"id": "inv-1", "link_id": "l1", "link_type": "payment_confirmation_for", "link_source": "auto", "type": "invoice",
             "invoice_date": "2026-02-02", "total_amount": 108.0, "currency": "EUR", "party_name": "Shop KG"}]),
        billpal_doc(id="cn-1", type="credit_note", total_amount=-24.0, supplier=shop, linked_documents=[
            {"id": "inv-1", "link_id": "l2", "link_type": "credit_for", "link_source": "user", "type": "invoice",
             "invoice_date": "2026-02-02", "total_amount": 108.0, "currency": "EUR", "party_name": "Shop KG"}]),
        billpal_doc(id="us-1", currency="usd", total_amount=108.88, supplier={
                        **shop, "id": "c-us", "company_name": "Staples", "brand_name": "Staples Store", "country": "US",
                        "tax_id_country": "EU"},
                    tax_lines=[{"tax_type": "sales_tax", "tax_regime": "standard", "tax_rate": 8.875,
                                "tax_base": 100.0, "tax_amount": 8.88, "label": None, "exemption_reason": None}],
                    line_items=[billpal_line(100.0, 8.875)]),
        billpal_doc(id="tip-1", type="receipt", total_amount=48.0, tip_amount=5.0, supplier=shop,
                    tax_lines=[{"tax_type": "vat", "tax_regime": "standard", "tax_rate": 20, "tax_base": 40.0,
                                "tax_amount": 8.0, "label": None, "exemption_reason": None}]),
        billpal_doc(id="held", status="review", invoice_date="2026-02-03", total_amount=11.9, supplier=shop),
        billpal_doc(id="nodate", invoice_date=None, supplier=shop),
        {"id": "gone", "error": "not found"}],
        "contacts": [{"contact": {"id": "c-shop", "account_code": "33001"}, "bank_accounts": []}],
        "chart_accounts": {"accounts": []}}
    bpf = f / "billpal.json"
    bpf.write_text(json.dumps(bp), encoding="utf-8")
    out = ok("from-billpal", bpf)
    docs = {d["id"]: d for d in json.loads(Path(out["written"]).read_text())["documents"]}
    assert sorted(docs) == ["billpal:cn-1", "billpal:held", "billpal:inv-1", "billpal:rcpt-1", "billpal:tip-1", "billpal:us-1"], docs
    notes = " | ".join(out["notes"])
    assert "billpal:gone: not found" in notes and "billpal:nodate: no direction or date" in notes and "Staples" in notes, notes
    inv = docs["billpal:inv-1"]
    assert D(inv["discount"]) == 10 and inv["party"] == {"name": "Shop KG", "iban": IBAN_A, "personenkonto": "33001",
                                                         "company_name": "Shop KG"}, inv
    # both names and both countries pass through unmerged; the reverse-charge rule picks between them later
    us = docs["billpal:us-1"]["party"]
    assert us == {"name": "Staples", "iban": None, "personenkonto": None, "company_name": "Staples",
                  "brand_name": "Staples Store", "country": "US", "tax_id_country": "EU"}, us
    assert inv["lines"][0]["tax"]["rate"] == "20" and inv["lines"][0]["amount_is"] == "net", inv
    assert docs["billpal:rcpt-1"]["linked"] == ["billpal:inv-1"] and docs["billpal:cn-1"]["applies_to"] == "billpal:inv-1"
    # Billpal prints the credit note as -24.00; documents/1 keeps it positive and the kind gives the direction
    assert D(docs["billpal:cn-1"]["total"]) == 24 and docs["billpal:held"]["review_open"] is True
    tip = docs["billpal:tip-1"]
    assert D(tip["total"]) == 53 and [D(ln["amount"]) for ln in tip["lines"]] == [40, 5] and tip["lines"][1]["tax"]["regime"] == "out_of_scope"
    # a line without type and regime takes both from the header tax with its rate, for US sales tax too
    for ref, ttype in (("billpal:inv-1", "vat"), ("billpal:us-1", "sales_tax")):
        nd = ledger.norm_doc({**docs[ref], "currency": "EUR"}, b)
        assert (nd["lines"][0]["tax"]["type"], nd["lines"][0]["tax"]["regime"]) == (ttype, "standard"), nd["lines"][0]

    # cash basis: the document in Review is held when the statement is matched, and commit keeps the hold
    feb = {"schema": "statement/1", "statements": [{
        "source_id": "wise-feb", "account": "Assets:Bank:Wise", "from": "2026-02-01", "to": "2026-02-28",
        "opening": "1950.00", "closing": "1938.10", "extraction": "manual", "rows": [
            {"booking_date": "2026-02-04", "amount": "-11.90", "counterparty": "Shop KG", "text": "Einkauf"}]}]}
    pend = ok("stage", v, put(f / "feb.json", feb))
    # a USD document without fx_rate stays in the pool for USD card rows, but never against a euro amount;
    # the receipt linked to inv-1 is counted once through the invoice, which is offered on the same row
    cand = ok("candidates", v, pend["pending"], "--documents", out["written"])
    offered = {x for row in cand["rows"] for c in row["candidates"] for x in c["documents"]}
    assert cand["pool_size"] == 5 and not offered & {"billpal:us-1", "billpal:rcpt-1"}, cand
    eur = put(f / "billpal-eur.json", {"schema": "documents/1", "documents": [d for k, d in docs.items() if k != "billpal:us-1"]})
    cand = ok("candidates", v, pend["pending"], "--documents", eur)
    assert [h["document"] for h in cand["held_in_review"]] == ["billpal:held"], cand
    assert json.loads(Path(pend["pending"]).read_text())["review_holds"][0]["document"] == "billpal:held"
    ok("commit", v, pend["pending"], "--decisions", put(f / "feb.dec.json", {"rows": [
        {"ref": "1:1", "lines": [{"account": "Expenses:Office-Supplies", "tax": {**v20, "source": "user"}}],
         "confirmed": "user", "no_document": "waiting for Billpal review"}]}))
    v = latest(f, "e")
    b = ledger.Book.load(v)
    assert [h["document"] for h in b.holds] == ["billpal:held"], b.holds
    assert "review_open" in {x["code"] for x in ledger.readiness(b, ledger.parse_date("2026-02-28"))["fails"]}

    # write-back on cash basis: Skonto from terms, a short payment the user says settles it, a partial payment
    # of this year's document, and one of last year's where the earlier payments are unknown
    sd = lambda i, d, total, **kw: {"id": f"billpal:{i}", "kind": "invoice", "direction": "in", "date": d, "total": total,
                                     "party": {"name": "Shop KG"}, **kw}
    wdocs = put(f / "wb-docs.json", {"schema": "documents/1", "documents": [
        docs["billpal:inv-1"], sd("t-1", "2026-03-01", "119.00", terms={"skonto_percent": "2", "skonto_days": 10}),
        sd("p-1", "2026-03-01", "200.00"), sd("old", "2025-12-15", "300.00")]})
    mar_rows = [("-100.00", "inv-1"), ("-116.62", "t-1"), ("-50.00", "p-1"), ("-100.00", "old")]
    mar = {"schema": "statement/1", "statements": [{
        "source_id": "wise-mar", "account": "Assets:Bank:Wise", "from": "2026-03-01", "to": "2026-03-31",
        "opening": "1938.10", "closing": "1571.48", "extraction": "manual",
        "rows": [{"booking_date": "2026-03-05", "amount": a, "counterparty": "Shop KG", "text": i} for a, i in mar_rows]}]}
    mar["statements"][0]["rows"][2]["method"] = "direct_debit"
    card = json.loads(json.dumps(mar))
    card["statements"][0]["rows"][0]["method"] = "card"
    fails("method is one of", "stage", v, put(f / "mar-card.json", card))
    pend = ok("stage", v, put(f / "mar.json", mar))
    pay = lambda n, i, **kw: {"ref": f"1:{n}", "lines": [{"account": "Expenses:Office-Supplies", "tax": {**v20, "source": "user"}}],
                              "confirmed": "user", "document": f"billpal:{i}", **kw}
    ok("commit", v, pend["pending"], "--decisions", put(f / "mar.dec.json", {"rows": [
        pay(1, "inv-1", settles=True), pay(2, "t-1"), pay(3, "p-1"), pay(4, "old")]}))
    v = latest(f, "e")
    wb = {d["document"]: d for d in ok("writeback", v, "--documents", wdocs)["documents"]}
    assert [c["tool"] for c in wb["billpal:inv-1"]["calls"]] == ["set_payment", "add_document_note"], wb["billpal:inv-1"]
    assert "Skonto 8.00 EUR" in wb["billpal:inv-1"]["calls"][1]["arguments"]["body"]
    assert wb["billpal:t-1"]["calls"][0]["arguments"]["payment_status"] == "paid", wb["billpal:t-1"]
    p1 = wb["billpal:p-1"]["calls"]
    assert p1[0]["arguments"] == {"document_id": "p-1", "amount_paid": 50.0} and p1[1]["arguments"] == {
        "document_ids": ["p-1"], "payment_date": "2026-03-05", "payment_method": "direct_debit", "payment_status": "partially_paid"}, p1
    assert not wb["billpal:old"]["calls"] and "before this ledger is unknown" in wb["billpal:old"]["note"], wb["billpal:old"]

    # an office flat rate next to the allocated home office share: usually either/or, so it warns
    ok("journal", v, put(f / "flat.json", {"schema": "journal/1", "entries": [{
        "date": "2026-12-31", "payee": "Arbeitsplatzpauschale", "confirmed": "user", "no_document": "flat rate",
        "meta": {"flat_rate": "home_office"},
        "lines": [{"account": "Expenses:Rent", "amount": "300.00", "tax": {"regime": "out_of_scope", "source": "user"}},
                  {"account": "Equity:Private"}]}]}))
    rep = ledger.readiness(ledger.Book.load(latest(f, "e")), None)
    assert {w["code"]: w["items"] for w in rep["warnings"]}["flat_rate_and_allocation"] == ["home_office"], rep["warnings"]
    print("book E (cost shares, private statements, Billpal results): ok")


# ── Book F: Wise CSV, USD card payments, reverse charge derived at commit ────

WISE_CSV = """TransferWise ID,Date,Amount,Currency,Description,Merchant,Payee Name,Payer Name,Exchange To,Exchange To Amount,Exchange Rate
CARD-1,04-01-2026,-89.14,EUR,Card transaction of 100.18 USD issued by Cursor,Cursor Ai Powered Ide,,,USD,100.18,1.12385
CARD-2,06-01-2026,-89.20,EUR,Card transaction of 100.18 USD issued by Cursor,Cursor Ai Powered Ide,,,USD,100.18,1.12309
FEE-1,06-01-2026,-0.41,EUR,Wise Charges for: CARD-2,Cursor Ai Powered Ide,,,,,
TR-1,07-01-2026,-50.00,eur,Sent money to Hetzner,,Hetzner Online GmbH,,EUR,0,
CARD-3,08-01-2026,-12.00,EUR,Card transaction issued by Shop KG,Shop KG,,,,,
CARD-4,10-01-2026,-20.00,EUR,Card transaction of 22.00 USD issued by Notion,Notion Labs,,,USD,22.00,1.1
TR-2,12-01-2026,-100.00,EUR,Sent money to Hetzner,,Hetzner Online GmbH,,,,
"""


def book_f(tmp: Path) -> None:
    f = tmp / "f"
    f.mkdir()
    wise = {"id": "wise", "delimiter": ",", "decimal": ".", "date_format": "%d-%m-%Y",
            "columns": {"booking_date": "Date", "amount": "Amount", "currency": "Currency", "text": "Description",
                        "counterparty": ["Merchant", "Payee Name", "Payer Name"], "bank_id": "TransferWise ID",
                        "original_amount": "Exchange To Amount", "original_currency": "Exchange To"}}
    raises("is one header", ledger.validate_csv_map, {**wise, "columns": {**wise["columns"], "text": ["Description"]}})
    raises("go together", ledger.validate_csv_map, {**wise, "columns": {**wise["columns"], "original_currency": None}})
    ok("init", put(f / "init.json", {
        "schema": "init/1", "title": "Leeb e.U.", "file_label": "f", "chart": "oekr", "new_business": True,
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "AT", "legal_form": "Einzelunternehmen", "basis": "cash", "vat_status": "regular",
                    "vat_basis": "payment", "vat_period": "quarter", "chart": "oekr"},
        "accounts": [{"account": "Assets:Bank:Wise", "role": "bank", "name": "Wise"}], "csv_maps": [wise]}), "--out-dir", f)
    v = f / "f-v1.beancount"
    (f / "wise.csv").write_text(WISE_CSV, encoding="utf-8")
    st = ok("from-csv", f / "wise.csv", "--ledger", v, "--map", "wise", "--account", "Assets:Bank:Wise", "--from",
            "2026-01-01", "--to", "2026-01-31", "--opening", "0.00", "--closing", "-360.75", "--out", f / "wise.json")
    rows = json.loads((f / "wise.json").read_text())["statements"][0]["rows"]
    # rate = ledger / original, the bank's inverse rate column is ignored; zero or same-currency originals leave fx unset
    assert rows[0]["fx"] == {"amount": "100.18", "currency": "USD", "rate": "0.88979836"}, rows[0]
    assert rows[3]["counterparty"] == "Hetzner Online GmbH" and "fx" not in rows[3] and "fx" not in rows[4], rows[3:5]
    assert ledger.vendor_key(rows[2]) == "text:wise charges for" and ledger.vendor_key(rows[0]) == "text:cursor ai powered"

    anysphere = {"name": "Anysphere, Inc.", "company_name": "Anysphere, Inc.", "brand_name": "Cursor", "country": "US"}
    hetzner = {"name": "Hetzner Online GmbH", "company_name": "Hetzner Online GmbH", "country": "DE"}
    inv = lambda i, d, total, party, **kw: {"id": f"billpal:{i}", "kind": "invoice", "direction": "in", "date": d,
                                            "total": total, "party": party, **kw}
    docs = put(f / "docs.json", {"schema": "documents/1", "documents": [
        inv("cur-1", "2026-01-01", "100.18", anysphere, currency="USD", number="INV-1", linked=["billpal:rcpt-1"]),
        {**inv("rcpt-1", "2026-01-01", "100.18", anysphere, currency="USD", linked=["billpal:cur-1"]), "kind": "receipt"},
        inv("cur-2", "2026-01-05", "100.18", anysphere, currency="USD", number="INV-2"),
        inv("het-1", "2026-01-02", "150.00", hetzner, number="H-1"),
        {**inv("shop-r", "2026-01-08", "12.00", {"name": "Shop KG", "country": "AT"}, linked=["billpal:elsewhere"]),
         "kind": "receipt"},
        inv("notion-1", "2026-01-09", "22.00", {"name": "Notion Labs, Inc.", "brand_name": "Notion", "country": "US"},
            currency="USD"),
    ]})
    pend = ok("stage", v, f / "wise.json")
    cand = ok("candidates", v, pend["pending"], "--documents", docs)
    by = {r["ref"]: r for r in cand["rows"]}
    ex = lambda ref: [(c["documents"], c["needs_user"]) for c in by[ref]["candidates"] if c["type"] == "exact"]
    # the 4 Jan payment takes the 1 Jan invoice, the 6 Jan one the 5 Jan invoice: 3 + 1 days beats 1 + 5
    assert ex("1:1") == [(["billpal:cur-1"], False)] and ex("1:2") == [(["billpal:cur-2"], False)], (by["1:1"], by["1:2"])
    assert not any(c["type"] == "fx_difference" for r in ("1:1", "1:2") for c in by[r]["candidates"]), by["1:2"]
    c1 = by["1:1"]["candidates"][0]
    assert "100.18 USD" in c1["explanation"] and c1["currency"] == "USD" and c1["linked_receipts"] == ["billpal:rcpt-1"], c1
    assert c1["derived_tax"] == {"type": "vat", "regime": "reverse_charge", "rate": "20", "source": "derived", "confirmed": True}
    assert by["1:3"]["bank_fee"] and not by["1:3"]["candidates"], by["1:3"]
    assert ex("1:5") == [(["billpal:shop-r"], False)], by["1:5"]
    assert [c["type"] for c in by["1:4"]["candidates"]] == ["partial"], by["1:4"]

    match = lambda ref: by[ref]["candidates"][0]["explanation"]
    rows_dec = [
        {"ref": "1:1", "document": "billpal:cur-1", "match": match("1:1")},
        {"ref": "1:2", "document": "billpal:cur-2", "match": match("1:2")},
        {"ref": "1:3", "lines": [{"account": "Expenses:Bank-Charges", "tax": {"regime": "exempt", "source": "user"}}],
         "confirmed": "user", "no_document": "bank fee"},
        {"ref": "1:4", "lines": [{"account": "Expenses:Software"}], "confirmed": "user", "document": "billpal:het-1",
         "match": match("1:4")},
        {"ref": "1:5", "lines": [{"account": "Expenses:Office-Supplies", "tax": {"regime": "standard", "rate": "20",
                                                                                 "source": "document"}}],
         "confirmed": "document", "document": "billpal:shop-r"},
        {"ref": "1:6", "lines": [{"account": "Expenses:Software", "amount": "12.00"}, {"account": "Equity:Private"}],
         "confirmed": "user", "document": "billpal:notion-1"},
        {"ref": "1:7", "lines": [{"account": "Expenses:Software"}], "confirmed": "user", "document": "billpal:het-1",
         "match": "partial: second instalment"},
    ]
    group = [{"vendor_key": "text:cursor ai powered", "lines": [{"account": "Expenses:Software"}], "confirmed": "user"}]
    twice = [{**rows_dec[0]}, {**rows_dec[1], "document": "billpal:cur-1"}, *rows_dec[2:]]
    fails("paid in full only once", "commit", v, pend["pending"], "--documents", docs, "--decisions",
          put(f / "twice.json", {"groups": group, "rows": twice}))
    fails("needs a tax treatment", "commit", v, pend["pending"], "--decisions", put(f / "dec.json", {"groups": group, "rows": rows_dec}))
    ok("commit", v, pend["pending"], "--documents", docs, "--decisions", f / "dec.json")
    b = ledger.Book.load(latest(f, "f"))
    by_row = {t.meta["source_row"]: t for t in b.txns if t.meta.get("source_row")}
    cursor = by_row["1"]
    soft = next(p for p in cursor.postings if p.account == "Expenses:Software")
    assert cursor.payee == "Cursor" and (soft.meta["tax_regime"], soft.meta["tax_rate"], soft.meta["tax_source"]) == (
        "reverse_charge", "20", "derived"), (cursor.payee, soft.meta)
    assert by_row["4"].payee == "Hetzner Online GmbH" and by_row["7"].meta["document"] == "billpal:het-1"
    notion = by_row["6"]
    assert [p.meta.get("tax_regime") for p in notion.postings if p.account in ("Expenses:Software", "Equity:Private")] == [
        "reverse_charge", None], notion.postings
    assert b.balance("Equity:Private") == D("8.00") and b.balance("Assets:Bank:Wise") == D("-360.75")
    card = lambda day, amt, who: {"booking_date": f"2026-02-0{day}", "amount": amt, "counterparty": who, "text": who}
    feb = {"schema": "statement/1", "statements": [{
        "source_id": "wise-feb", "account": "Assets:Bank:Wise", "from": "2026-02-01", "to": "2026-02-28",
        "opening": "-360.75", "closing": "-544.03", "extraction": "manual", "rows": [
            {**card(2, "-89.14", "Cursor Ai Powered Ide"), "fx": {"amount": "100.18", "currency": "USD", "rate": "0.88979836"}},
            card(3, "-89.14", "Cursor Ai Powered Ide"), card(4, "-5.00", "Spar Filiale 12")]}]}
    pend = ok("stage", latest(f, "f"), put(f / "feb.json", feb))
    rc = {"regime": "reverse_charge", "rate": "20", "source": "user"}
    feb_dec = {"groups": [{"vendor_key": "text:cursor ai powered", "payee": "Cursor", "confirmed": "user",
                           "lines": [{"account": "Expenses:Software", "tax": rc}]}],
               "rows": [{"ref": "1:3", "payee": "Spar", "confirmed": "user", "lines": [
                   {"account": "Expenses:Office-Supplies", "tax": {"regime": "standard", "rate": "20", "source": "user"}}]}]}
    fails("already booked, again on row 1:1", "commit", latest(f, "f"), pend["pending"], "--documents", docs, "--decisions",
          put(f / "feb.twice.json", {**feb_dec, "rows": [rows_dec[0], *feb_dec["rows"]]}))
    ok("commit", latest(f, "f"), pend["pending"], "--decisions", put(f / "feb.dec.json", feb_dec))
    out = ok("report", latest(f, "f"), "--out-dir", f / "rep", "--to", "2026-02-28")
    # missing receipts per clean payee, over every row and not only the 50 listed
    assert out["missing_receipts_count"] == 3 and out["missing_receipts_by_party"] == [
        {"payee": "Cursor", "count": 2, "amount": "-178.28"}, {"payee": "Spar", "count": 1, "amount": "-5.00"}], out
    data = json.loads((f / "rep" / "report-data.json").read_text())
    het = [j["document"] for j in data["journal"] if j["account"] == "Expenses:Software" and j["payee"] == "Hetzner Online GmbH"]
    assert het == ["billpal:het-1", "billpal:het-1"], het

    # the gates: country disagreement, an EU OSS number, a home supplier, VAT on the bill, DE 2020, small business
    nd = lambda party, **kw: ledger.norm_doc(inv("g", "2026-03-01", "10.00", party, **kw), b)
    assert ledger.derive_tax(nd({"name": "x", "tax_id_country": "IE"}), b)["rate"] == "20"
    for party, kw in (({"name": "x", "country": "US", "tax_id_country": "IE"}, {}), ({"name": "x", "tax_id_country": "EU"}, {}),
                      ({"name": "x", "country": "AT"}, {}), ({"name": "x", "country": "Hong Kong SAR China"}, {}),
                      ({"name": "x", "country": "DE"}, {"taxes": [{"regime": None, "rate": "19", "amount": "1.60"}]})):
        assert ledger.derive_tax(nd(party, **kw), b) is None, party
    de2020 = SimpleNamespace(profile={"country": "DE", "vat_status": "regular"})
    assert ledger.derive_tax({**nd({"name": "x", "country": "US"}), "date": ledger.date(2020, 9, 1)}, de2020) is None
    small = SimpleNamespace(profile={"country": "AT", "vat_status": "small_business"})
    assert ledger.derive_tax(nd({"name": "x", "country": "US"}), small)["confirmed"] is False

    # a supplier's "Service charge for March" is no bank fee; Wise's own "Charges for:" is
    assert ledger.is_fee_row({"text": "Wise Charges for: CARD-2"}) and not ledger.is_fee_row({"text": "Service charge for March"})

    # a batch of two bills derives the tax when both agree, the payee falls back to party.name, and
    # bills that disagree refuse instead of booking one side untaxed
    linear = {"name": "Linear Orbit, Inc.", "country": "US"}
    bd = lambda i, party: ledger.norm_doc(inv(i, "2026-03-01", "15.00", party), b, need_rate=False)
    st, row = {"account": "Assets:Bank:Wise", "source_id": "x"}, {"ref": "9:1", "booking_date": "2026-03-02",
                                                                     "amount": "-30.00", "seq": 1, "row_key": "k"}
    batch = {"lines": [{"account": "Expenses:Software"}], "documents": ["billpal:l1", "billpal:l2"], "confirmed": "user"}
    txt = ledger.statement_txn(b, st, row, batch, {"billpal:l1": bd("l1", linear), "billpal:l2": bd("l2", linear)})
    assert '"Linear Orbit, Inc."' in txt and 'tax_regime: "reverse_charge"' in txt, txt
    raises("different tax treatments", ledger.statement_txn, b, st, row, batch,
           {"billpal:l1": bd("l1", linear), "billpal:l2": bd("l2", {"name": "Spar", "country": "AT"})})

    # a receipt outside its invoice's window is offered on its own; commit counts invoice and receipt as one
    spar, vat20 = {"name": "Spar AG", "country": "AT"}, {"regime": "standard", "rate": "20", "source": "document"}
    mar_docs = put(f / "mar-docs.json", {"schema": "documents/1", "documents": [
        inv("sp-inv", "2026-01-01", "30.00", spar, linked=["billpal:sp-r"]),
        {**inv("sp-r", "2026-03-01", "30.00", spar, linked=["billpal:sp-inv"]), "kind": "receipt"}]})
    mar = {"schema": "statement/1", "statements": [{
        "source_id": "wise-mar", "account": "Assets:Bank:Wise", "from": "2026-03-01", "to": "2026-03-31",
        "opening": "-544.03", "closing": "-604.03", "extraction": "manual", "rows": [
            {"booking_date": "2026-03-02", "amount": "-30.00", "counterparty": "Spar AG", "text": "Spar AG"},
            {"booking_date": "2026-03-03", "amount": "-30.00", "counterparty": "Spar AG", "text": "Spar AG 2"}]}]}
    pend = ok("stage", latest(f, "f"), put(f / "mar.json", mar))
    cand = ok("candidates", latest(f, "f"), pend["pending"], "--documents", mar_docs)
    assert [c["documents"] for c in cand["rows"][0]["candidates"]] == [["billpal:sp-r"]], cand["rows"][0]
    line = [{"account": "Expenses:Office-Supplies", "tax": vat20}]
    fails("billpal:sp-inv / billpal:sp-r: rows 1:1, 1:2", "commit", latest(f, "f"), pend["pending"], "--documents", mar_docs,
          "--decisions", put(f / "mar.dec.json", {"rows": [
              {"ref": "1:1", "lines": line, "confirmed": "document", "document": "billpal:sp-r", "match": "exact: receipt"},
              {"ref": "1:2", "lines": line, "confirmed": "document", "document": "billpal:sp-inv", "match": "exact: invoice"}]}))
    print("book F (Wise CSV, USD matching, derived reverse charge): ok")


# ── Performance: 50,000 transactions ─────────────────────────────────────────

def book_g(tmp: Path) -> None:
    """A GmbH: owner money goes to the shareholder clearing account, which carries into the next year."""
    owner = {a["account"] for a in ledger.load_chart("skr04", "en")}
    assert "Equity:Private" in owner and "Liabilities:Shareholder" not in owner and "Expenses:Corporate-Tax" not in owner
    f = tmp / "g"
    f.mkdir()
    ok("init", put(f / "init.json", {
        "schema": "init/1", "title": "Beispiel GmbH", "file_label": "g", "chart": "skr04", "new_business": True,
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "DE", "legal_form": "GmbH", "company": True, "basis": "accrual", "vat_status": "regular",
                    "vat_basis": "invoice", "vat_period": "quarter", "chart": "skr04"}}), "--out-dir", f)
    v = f / "g-v1.beancount"
    b = ledger.Book.load(v)
    assert "Equity:Private" not in b.accounts and b.role_of("Liabilities:Shareholder") == "shareholder", sorted(b.accounts)
    assert b.accounts["Expenses:Corporate-Tax"]["meta"]["code"] == "7600"
    lines = ledger.share_lines(b, {"id": "car", "share": "0.6", "account": "Expenses:Corporate-Tax"}, D("-100.00"))
    assert lines[1] == {"account": "Liabilities:Shareholder", "amount": "40.00"}, lines
    ok("journal", v, put(f / "loan.json", {"schema": "journal/1", "entries": [{
        "date": "2026-03-02", "payee": "Gesellschafter", "confirmed": "user", "no_document": "shareholder loan",
        "lines": [{"account": "Assets:Bank:Giro", "amount": "500.00"}, {"account": "Liabilities:Shareholder"}]}]}))
    roll = ledger.build_rollover(ledger.Book.load(latest(f, "g")), None, None, False)
    assert {"account": "Liabilities:Shareholder", "amount": "-500.00"} in roll["opening_lines"], roll["opening_lines"]
    print("book G (DE GmbH, shareholder account): ok")


def performance(tmp: Path) -> None:
    f = tmp / "perf"
    f.mkdir()
    init = {
        "schema": "init/1", "file_label": "big",
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "EUR"},
        "profile": {"country": "AT", "legal_form": "GmbH", "basis": "cash", "vat_status": "none", "chart": "oekr"},
        "chart": "oekr", "new_business": True,
    }
    ok("init", put(f / "init.json", init), "--out-dir", f)
    v1 = f / "big-v1.beancount"
    book = ledger.Book.load(v1)
    parts = []
    for i in range(50_000):
        d = ledger.date(2026, 1 + i % 12, 1 + i % 28)
        meta = {"txn_id": f"p{i:07d}", "confirmed": "user", "classified_by": "rule:x", "no_document": "perf"}
        posts = [ledger.Posting("Assets:Bank:Giro", D("-12.34"), "EUR"), ledger.Posting("Expenses:Software", D("12.34"), "EUR")]
        parts.append(ledger.ser_txn(d, "*", f"Vendor {i % 300}", "test", meta, posts, 2))
    big = f / "big-v2.beancount"
    raw = v1.read_text(encoding="utf-8") + "\n" + "\n".join(parts)
    big.write_text(raw, encoding="utf-8")
    t0 = time.perf_counter()
    b = ledger.Book.load(big)
    errs = ledger.validate(b)
    rep = ledger.build_report(b, b.fy_from, b.fy_to, "month", "en")
    took = time.perf_counter() - t0
    assert not errs and rep["meta"]["reconciled"], errs[:3]
    assert took < 60, f"50k transactions took {took:.1f}s"
    print(f"performance: 50,000 transactions parsed, validated and reported in {took:.1f}s")
    del book


# ── Beancount ────────────────────────────────────────────────────────────────

def beancount_check() -> None:
    try:
        from beancount import loader
    except ImportError:
        if os.environ.get("BEANCOUNT_REQUIRED"):
            raise SystemExit("beancount is required (pip install beancount==3.2.3)")
        print("beancount: not installed, skipped (CI runs it)")
        return
    files = sorted({p.resolve() for p in WRITTEN if p.exists() and p.suffix == ".beancount"})
    for p in files:
        _, errors, _ = loader.load_file(str(p))
        assert not errors, f"{p.name}: {[e.message for e in errors][:5]}"
    print(f"beancount: {len(files)} ledger files load without errors")


def book_c(tmp: Path) -> None:
    """US, QBO chart: indirect depreciation with prior years, and country rules outside their years."""
    f = tmp / "c"
    f.mkdir()
    ok("init", put(f / "init.json", {
        "schema": "init/1", "title": "Acme LLC", "file_label": "acme-2026",
        "ledger": {"fiscal_year_from": "2026-01-01", "fiscal_year_to": "2026-12-31", "currency": "USD"},
        "profile": {"country": "US", "legal_form": "LLC", "basis": "cash", "vat_status": "none", "chart": "qbo",
                    "language": "en"},
        "chart": "qbo",
        "opening": {"lines": [{"account": "Assets:Bank:Checking", "amount": "1000.00"},
                              {"account": "Assets:Fixed:Furniture", "amount": "3600.00"},
                              {"account": "Assets:Fixed:Accumulated-Depreciation", "amount": "-1200.00"},
                              {"account": "Equity:Opening-Balance", "amount": "-3400.00"}]}}), "--out-dir", f)
    v1 = f / "acme-2026-v1.beancount"
    asset = {"id": "desk", "description": "Desks", "in_service": "2025-01-01", "cost": "3600.00", "life_months": 36,
             "first_year": "full_year", "prior_depreciation": "1200.00", "asset_account": "Assets:Fixed:Furniture",
             "depreciation_account": "Expenses:Depreciation"}
    fails("books depreciation indirectly", "journal", v1, put(f / "no-acc.json", {"schema": "journal/1", "assets": [asset]}))
    fails("first_year", "journal", v1, put(f / "no-first.json", {"schema": "journal/1", "assets": [
        {**asset, "first_year": None, "accumulated_account": "Assets:Fixed:Accumulated-Depreciation"}]}))
    ok("journal", v1, put(f / "asset.json", {"schema": "journal/1", "assets": [
        {**asset, "accumulated_account": "Assets:Fixed:Accumulated-Depreciation"}]}))
    ok("depreciate", latest(f, "acme-2026"), "--write")
    b = ledger.Book.load(latest(f, "acme-2026"))
    assert b.balance("Assets:Fixed:Furniture") == D("3600.00")
    assert b.balance("Assets:Fixed:Accumulated-Depreciation") == D("-2400.00")
    assert b.balance("Expenses:Depreciation") == D("1200.00")
    # US sales tax: collected on sales without a VAT registration, a cost on purchases, reported as its own type
    st = {"type": "sales_tax", "regime": "standard", "rate": "8.875", "source": "user"}
    sale = {"date": "2026-02-02", "payee": "Client", "confirmed": "user", "no_document": "test",
            "lines": [{"account": "Assets:Bank:Checking", "amount": "108.88"}, {"account": "Income:Sales", "tax": st}]}
    fails("type sales_tax", "journal", latest(f, "acme-2026"), put(f / "vat-sale.json", {"schema": "journal/1", "entries": [
        {**sale, "lines": [sale["lines"][0], {"account": "Income:Sales", "tax": {**st, "type": "vat"}}]}]}))
    ok("journal", latest(f, "acme-2026"), put(f / "sales-tax.json", {"schema": "journal/1", "entries": [sale, {
        "date": "2026-02-03", "payee": "Staples", "confirmed": "user", "no_document": "test",
        "lines": [{"account": "Assets:Bank:Checking", "amount": "-54.44"}, {"account": "Expenses:Office-Supplies", "tax": st}]}]}))
    b = ledger.Book.load(latest(f, "acme-2026"))
    assert b.balance("Liabilities:Sales-Tax-Payable") == D("-8.88") and b.balance("Income:Sales") == D("-100.00")
    assert b.balance("Expenses:Office-Supplies") == D("54.44") and b.balance("Assets:Input-Tax") == 0
    vat = ledger.vat_schedule(b, b.fy_from, b.fy_to)
    assert vat["types"] == ["sales_tax"] and vat["totals"] == [
        {"period": "2026", "type": "sales_tax", "tax": D("8.88"), "input_tax": D("0.00"), "payable": D("8.88")}], vat
    # cash basis: a document still in Billpal Review is never offered as a match, it is listed as held
    row = {"ref": "1:1", "status": "new", "amount": "-11.90", "booking_date": "2026-03-02", "text": "Shop Inc"}
    doc = {"id": "billpal:held", "kind": "receipt", "direction": "in", "date": "2026-03-01", "total": "11.90",
           "party": {"name": "Shop Inc"}, "review_open": True}
    cand = ledger.candidates(b, {"kind": "statement", "statements": [{"rows": [row]}]}, {"documents": [doc]})
    assert not cand["rows"][0]["candidates"] and cand["held_in_review"][0]["document"] == "billpal:held", cand
    cand = ledger.candidates(b, {"kind": "statement", "statements": [{"rows": [row]}]}, {"documents": [{**doc, "review_open": False}]})
    assert cand["rows"][0]["candidates"][0]["type"] == "exact", cand
    # country rules apply only inside their effective years
    at = ledger.Book.load(latest(tmp / "a", "2026"))
    assert ledger.country_rule(at, 2019) == {} and ledger.country_rule(at, 2021)["gwg_limit"] == "800"
    assert ledger.country_rule(at, 2026)["gwg_limit"] == "1000" and ledger.country_rule(b, 2026) == {}
    print("book C (US, QBO, indirect depreciation): ok")


def main() -> int:
    unit()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        book_a(tmp)
        book_b(tmp)
        book_c(tmp)
        book_d(tmp)
        book_e(tmp)
        book_f(tmp)
        book_g(tmp)
        performance(tmp)
        written = sorted({p for p in tmp.rglob("*.beancount")})
        WRITTEN.extend(written)
        beancount_check()
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
