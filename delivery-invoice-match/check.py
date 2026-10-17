#!/usr/bin/env python3
"""Run match.py on known cases and assert the statuses it reports."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import match  # noqa: E402


def doc(kind: str, number: str, lines: list[dict], supplier: str = "Acme Ltd", **extra: object) -> dict:
    return {"kind": kind, "supplier": supplier, "number": number, "currency": "EUR", "lines": lines, **extra}


def line(item: str | None, quantity: object, **extra: object) -> dict:
    return {"item": item, "quantity": quantity, "unit": "pcs", **extra}


def statuses(report: dict, status: str) -> list[dict]:
    return [row for row in report["rows"] if row["status"] == status]


def expect(report: dict, status: str, count: int = 1, **fields: str) -> None:
    found = [row for row in statuses(report, status) if all(row.get(k) == v for k, v in fields.items())]
    assert len(found) == count, f"expected {count} x {status} {fields}, got {found}\nall: {report['counts']}"


def run(docs: list[dict], **kwargs: Decimal) -> dict:
    return match.match([match.validate(d, i) for i, d in enumerate(docs)], **kwargs)


def main() -> None:
    collective = run([
        doc("proof", "DN-1", [line("A", 5)]),
        doc("proof", "DN-2", [line("A", 5)]),
        doc("invoice", "INV-1", [line("A", 5), line("A", 5)], refs=["DN-1", "DN 2"]),
    ])
    expect(collective, "ok", item="A", billed="10", delivered="10")
    assert not collective["findings"], collective["findings"]

    short = run([
        doc("proof", "DN-3", [line("B", 8, read="handwritten")]),
        doc("invoice", "INV-2", [line("B", 10)], refs=["DN-3"]),
    ])
    expect(short, "billed_not_delivered", item="B", difference="2", handwritten="yes")

    twice = run([
        doc("proof", "DN-4", [line("C", 4)]),
        doc("invoice", "INV-3", [line("C", 4)], refs=["DN-4"]),
        doc("invoice", "INV-4", [line("C", 4)], refs=["DN-4"]),
    ])
    expect(twice, "proof_on_several_invoices")
    expect(twice, "billed_not_delivered", item="C", billed="8", delivered="4")

    timesheet = run([
        doc("proof", "TS-10", [line("consulting", "37.5", unit="h", unit_kind="hours")],
            signed=False, date="2024-04-30"),
        doc("invoice", "INV-5", [line("consulting", 40, unit="h", unit_kind="hours")],
            refs=["TS-10"], period=["2024-03-01", "2024-03-31"]),
    ])
    expect(timesheet, "billed_not_delivered", difference="2.5")
    expect(timesheet, "not_signed")
    expect(timesheet, "outside_period")
    rounded = run([
        doc("proof", "TS-11", [line("consulting", "7.75", unit="h", unit_kind="hours")]),
        doc("invoice", "INV-6", [line("consulting", 8, unit="h", unit_kind="hours")], refs=["TS-11"]),
    ], hours_tolerance=Decimal("0.25"))
    expect(rounded, "within_tolerance")

    weighed = [
        doc("proof", "DN-5", [line("flour", "100.5", unit="kg", unit_kind="measured")]),
        doc("invoice", "INV-7", [line("flour", 101, unit="kg", unit_kind="measured")], refs=["DN-5"]),
    ]
    expect(run(weighed), "within_tolerance", difference="0.5")
    expect(run(weighed, measured_percent=Decimal(0)), "billed_not_delivered")

    unclear = run([
        doc("proof", "DN-6", [line("D", None, read="unclear")]),
        doc("invoice", "INV-8", [line("D", 3)], refs=["DN-6"]),
    ])
    expect(unclear, "check_by_hand", item="D")

    units = run([
        doc("proof", "DN-7", [line("E", 1, unit="box")]),
        doc("invoice", "INV-9", [line("E", 12)], refs=["DN-7"]),
    ])
    expect(units, "unit_differs")

    returned = run([
        doc("proof", "DN-8", [line("F", 10)]),
        doc("proof", "RET-1", [line("F", 2)], refs=["DN-8"], **{"return": True}),
        doc("invoice", "INV-10", [line("F", 10)], refs=["DN-8"]),
        doc("credit_note", "CN-1", [line("F", 2)], refs=["INV-10"]),
    ])
    expect(returned, "ok", item="F", billed="8", delivered="8")

    ordered = run([
        doc("order", "PO-1", [line("G", 10, unit_price_minor=1000), line("H", 5, unit_price_minor=200)]),
        doc("proof", "DN-9", [line("G", 6)], refs=["PO-1"]),
        doc("invoice", "INV-11", [line("G", 6, unit_price_minor=1100), line(None, 1, charge=True)], refs=["DN-9", "PO-1"]),
        doc("invoice", "INV-12", [line("K", 1, unit_price_minor=500)]),
    ])
    expect(ordered, "price_differs", item="G", unit_price_billed="1100", unit_price_agreed="1000")
    expect(ordered, "open_on_order", item="G", ordered="10", delivered="6")
    expect(ordered, "open_on_order", item="H", delivered="0")
    expect(ordered, "charge_not_on_proof")
    expect(ordered, "invoice_without_proof", invoices="INV-12")
    assert ordered["prices_not_checked"] == ["INV-12"], ordered["prices_not_checked"]

    leftovers = run([
        doc("proof", "DN-20", [line("L", 1)]),
        doc("invoice", "INV-20", [line("M", 1)], refs=["DN-99"], date="2024-05-02", total_minor=1190),
        doc("invoice", "INV-21", [line("M", 1)], date="2024-05-02", total_minor=1190),
        doc("invoice", "INV-21", [line("M", 1)]),
        doc("proof", "LS-30", [line("N", 2)], supplier="Other Co"),
        doc("invoice", "LS-30", [line("N", 2)], supplier="Other Co"),
    ])
    expect(leftovers, "proof_without_invoice", proofs="DN-20")
    expect(leftovers, "reference_not_supplied")
    expect(leftovers, "possible_duplicate")
    expect(leftovers, "duplicate_document", invoices="INV-21")
    expect(leftovers, "ok", item="N")

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "docs").mkdir()
        (folder / "docs" / "a.json").write_text(json.dumps(doc("proof", "DN-3", [line("B", 8)])), encoding="utf-8")
        (folder / "docs" / "b.json").write_text(
            json.dumps({"documents": [doc("invoice", "INV-2", [line("B", 10)], refs=["DN-3"])]}), encoding="utf-8")
        (folder / "labels.json").write_text(json.dumps({"status": "Status", "billed_not_delivered": "Billed, not delivered"}), encoding="utf-8")
        out = folder / "report.csv"
        cli = subprocess.run(
            [sys.executable, str(HERE / "match.py"), str(folder / "docs"), "--csv", str(out), "--delimiter", ";",
             "--labels", str(folder / "labels.json")],
            capture_output=True, text=True, check=True,
        )
        assert json.loads(cli.stdout)["counts"] == {"billed_not_delivered": 1}, cli.stdout
        rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
        assert rows[0][0] == "Status" and rows[1][0] == "Billed, not delivered", rows

        (folder / "bad.json").write_text(json.dumps(doc("invoice", "X", [line("A", 1, unit_price_minor=10.5)])), encoding="utf-8")
        bad = subprocess.run([sys.executable, str(HERE / "match.py"), str(folder / "bad.json")], capture_output=True, text=True)
        assert bad.returncode == 2 and "minor units" in bad.stderr, (bad.returncode, bad.stderr)

    print("delivery-invoice match check passed")


if __name__ == "__main__":
    main()
