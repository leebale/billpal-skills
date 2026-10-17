#!/usr/bin/env python3
"""Match orders, proofs of delivery (delivery notes, timesheets) and invoices.

Reads document JSON (files or folders), prints a JSON report, and can write a CSV.
Every difference is reported with its size. A tolerance only changes the label.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

KINDS = {"order", "proof", "invoice", "credit_note"}
BILLS = {"invoice", "credit_note"}
UNIT_KINDS = {"count", "hours", "measured"}
READS = {"printed", "handwritten", "unclear"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OK = {"ok", "within_tolerance"}
CSV_COLUMNS = [
    "status", "supplier", "invoices", "proofs", "orders", "item", "description", "unit",
    "billed", "delivered", "ordered", "difference", "unit_price_billed", "unit_price_agreed",
    "handwritten", "note", "sources",
]


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def key(text: object) -> str:
    return re.sub(r"[\W_]+", "", str(text or "")).casefold()


def decimal(value: object, where: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        fail(f"{where}: {value!r} is not a number")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        fail(f"{where}: {value!r} is not a number")
    return None


def minor(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{where}: money is integer minor units (cents), got {value!r}")
    return value


def date(value: object, where: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not DATE.match(value):
        fail(f"{where}: dates are YYYY-MM-DD, got {value!r}")
    return value


def validate(raw: object, index: int) -> dict:
    if not isinstance(raw, dict):
        fail(f"document {index + 1}: expected an object")
    where = f"document {index + 1} ({raw.get('number')!r})"
    if raw.get("kind") not in KINDS:
        fail(f"{where}: kind must be one of {sorted(KINDS)}")
    if not key(raw.get("supplier")) or not key(raw.get("number")):
        fail(f"{where}: supplier and number are required")
    period = raw.get("period")
    if period is not None and (not isinstance(period, list) or len(period) != 2):
        fail(f"{where}: period is [from, to]")
    lines = []
    for n, line in enumerate(raw.get("lines") or []):
        at = f"{where} line {n + 1}"
        if line.get("unit_kind", "count") not in UNIT_KINDS:
            fail(f"{at}: unit_kind must be one of {sorted(UNIT_KINDS)}")
        if line.get("read", "printed") not in READS:
            fail(f"{at}: read must be one of {sorted(READS)}")
        lines.append({
            "item": key(line.get("item")),
            "item_label": str(line.get("item") or ""),
            "description": str(line.get("description") or ""),
            "quantity": decimal(line.get("quantity"), at),
            "unit": str(line.get("unit") or "").strip(),
            "unit_kind": line.get("unit_kind", "count"),
            "unit_price_minor": minor(line.get("unit_price_minor"), at),
            "charge": bool(line.get("charge")),
            "read": line.get("read", "printed"),
        })
    return {
        "kind": raw["kind"],
        "supplier": str(raw["supplier"]),
        "skey": key(raw["supplier"]),
        "number": str(raw["number"]),
        "nkey": key(raw["number"]),
        "date": date(raw.get("date"), where),
        "period": [date(p, where) for p in period] if period else None,
        "currency": raw.get("currency"),
        "refs": [str(r) for r in raw.get("refs") or []],
        "refs_added": [str(r) for r in raw.get("refs_added") or []],
        "return": bool(raw.get("return")),
        "signed": raw.get("signed"),
        "total_minor": minor(raw.get("total_minor"), where),
        "source": str(raw.get("source") or ""),
        "lines": lines,
    }


def load(paths: list[str]) -> list[dict]:
    raw: list = []
    for path in paths:
        target = Path(path)
        for file in sorted(target.glob("*.json")) if target.is_dir() else [target]:
            data = json.loads(file.read_text(encoding="utf-8"))
            raw.extend(data.get("documents", [data]) if isinstance(data, dict) else data)
    return [validate(doc, i) for i, doc in enumerate(raw)]


def text(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize(), "f")


def match(docs: list[dict], measured_percent: Decimal = Decimal(1), hours_tolerance: Decimal = Decimal(0)) -> dict:
    rows: list[dict] = []
    checked_prices: set[int] = set()

    def numbers(ds: list[dict]) -> str:
        return ", ".join(sorted({d["number"] for d in ds}))

    def add(status: str, ds: list[dict], note: str = "", **fields: object) -> None:
        rows.append({
            "status": status,
            "supplier": ds[0]["supplier"],
            "invoices": numbers([d for d in ds if d["kind"] in BILLS]),
            "proofs": numbers([d for d in ds if d["kind"] == "proof"]),
            "orders": numbers([d for d in ds if d["kind"] == "order"]),
            "sources": ", ".join(sorted({d["source"] for d in ds if d["source"]})),
            "note": note,
            **fields,
        })

    def compare(left: Decimal, right: Decimal, unit_kind: str) -> str:
        diff = abs(left - right)
        if diff == 0:
            return "ok"
        tolerance = {
            "count": Decimal(0),
            "hours": hours_tolerance,
            "measured": max(abs(left), abs(right)) * measured_percent / 100,
        }[unit_kind]
        return "within_tolerance" if diff <= tolerance else "differs"

    def sign(doc: dict) -> int:
        return -1 if doc["kind"] == "credit_note" or doc["return"] else 1

    by_kind = defaultdict(list)
    for doc in docs:
        by_kind[(doc["kind"], doc["skey"], doc["nkey"])].append(doc)
    for group in by_kind.values():
        if len(group) > 1:
            add("duplicate_document", group, f"{group[0]['number']} appears {len(group)} times")

    same_bill = defaultdict(list)
    for doc in docs:
        if doc["kind"] == "invoice" and doc["total_minor"] is not None and doc["date"]:
            same_bill[(doc["skey"], doc["date"], doc["total_minor"], doc["currency"])].append(doc)
    for group in same_bill.values():
        if len({d["nkey"] for d in group}) > 1:
            add("possible_duplicate", group, "same supplier, date and total under different numbers")

    index = defaultdict(list)
    for i, doc in enumerate(docs):
        index[(doc["skey"], doc["nkey"])].append(i)
    parent = list(range(len(docs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    orders_of = defaultdict(set)
    invoices_of_proof = defaultdict(set)
    for i, doc in enumerate(docs):
        if doc["kind"] == "order":
            continue
        for ref in doc["refs"] + doc["refs_added"]:
            targets = [j for j in index.get((doc["skey"], key(ref)), []) if j != i]
            if not targets:
                add("reference_not_supplied", [doc], f"cites {ref}, which is not among the documents")
            for j in targets:
                if docs[j]["kind"] == "order":
                    orders_of[i].add(j)
                    continue
                parent[find(i)] = find(j)
                if doc["kind"] == "invoice" and docs[j]["kind"] == "proof":
                    invoices_of_proof[j].add(i)
                if doc["kind"] == "proof" and docs[j]["kind"] == "invoice":
                    invoices_of_proof[i].add(j)
    # Some suppliers print the same number on the delivery note and the invoice.
    for ids in index.values():
        linked = [j for j in ids if docs[j]["kind"] != "order"]
        for j in linked[1:]:
            parent[find(j)] = find(linked[0])
        for p in (j for j in linked if docs[j]["kind"] == "proof"):
            invoices_of_proof[p].update(j for j in linked if docs[j]["kind"] == "invoice")

    for p, invoices in invoices_of_proof.items():
        if len({docs[i]["nkey"] for i in invoices}) > 1:
            add("proof_on_several_invoices", [docs[p], *(docs[i] for i in invoices)],
                "check that the invoices do not bill the same lines")

    groups = defaultdict(list)
    for i, doc in enumerate(docs):
        if doc["kind"] != "order":
            groups[find(i)].append(i)

    group_orders = defaultdict(set)
    for root, members in groups.items():
        for i in members:
            group_orders[root].update(orders_of[i])

    for root, members in groups.items():
        ds = [docs[i] for i in members]
        bills = [d for d in ds if d["kind"] in BILLS]
        proofs = [d for d in ds if d["kind"] == "proof"]
        orders = [docs[o] for o in sorted(group_orders[root])]
        if not proofs:
            invoices = [d for d in bills if d["kind"] == "invoice"]
            if invoices:
                add("invoice_without_proof", invoices + orders, "no delivery note or timesheet for this invoice")
            continue
        if not bills:
            add("proof_without_invoice", proofs + orders, "delivered, not billed yet")
            continue

        for proof in proofs:
            if proof["signed"] is False:
                add("not_signed", [proof], "no signature or approval on this proof")
        for bill in bills:
            if not bill["period"] or None in bill["period"]:
                continue
            start, end = bill["period"]
            for proof in proofs:
                dates = [d for d in (proof["period"] or [proof["date"]]) if d]
                if any(d < start or d > end for d in dates):
                    add("outside_period", [bill, proof], f"proof dated outside the billed period {start} to {end}")

        billed, delivered = defaultdict(list), defaultdict(list)
        for doc in bills:
            for line in doc["lines"]:
                billed[line["item"]].append((doc, line))
        for doc in proofs:
            for line in doc["lines"]:
                delivered[line["item"]].append((doc, line))

        for doc, line in billed.pop("", []) + delivered.pop("", []):
            status = "charge_not_on_proof" if line["charge"] else "check_by_hand"
            add(status, [doc], "no item key on this line", description=line["description"],
                unit=line["unit"], billed=text(line["quantity"]) if doc["kind"] in BILLS else "",
                delivered=text(line["quantity"]) if doc["kind"] == "proof" else "")

        for item in sorted(set(billed) | set(delivered)):
            b, p = billed.get(item, []), delivered.get(item, [])
            lines = [line for _, line in b + p]
            involved = [doc for doc, _ in b + p]
            first = lines[0]
            fields = {
                "item": first["item_label"],
                "description": first["description"],
                "unit": first["unit"],
                "handwritten": "yes" if any(l["read"] == "handwritten" for l in lines) else "",
            }
            if any(l["quantity"] is None or l["read"] == "unclear" for l in lines):
                add("check_by_hand", involved + orders, "a quantity could not be read clearly", **fields)
                continue
            if len({(l["unit"].casefold(), l["unit_kind"]) for l in lines}) > 1:
                units = ", ".join(sorted({l["unit"] or l["unit_kind"] for l in lines}))
                add("unit_differs", involved + orders, f"units differ ({units}); convert first", **fields)
                continue
            billed_qty = sum((sign(d) * l["quantity"] for d, l in b), Decimal(0))
            delivered_qty = sum((sign(d) * l["quantity"] for d, l in p), Decimal(0))
            fields.update(billed=text(billed_qty), delivered=text(delivered_qty),
                          difference=text(billed_qty - delivered_qty))
            if not p and all(l["charge"] for l in lines):
                add("charge_not_on_proof", involved, "not a delivered item; check it against the order or contract", **fields)
                continue
            result = compare(billed_qty, delivered_qty, first["unit_kind"])
            if result == "differs":
                result = "billed_not_delivered" if billed_qty > delivered_qty else "delivered_not_billed"
            add(result, involved + orders, **fields)

        agreed = defaultdict(set)
        for doc in orders + proofs:
            for line in doc["lines"]:
                if line["item"] and line["unit_price_minor"] is not None:
                    agreed[(line["item"], doc["currency"])].add(line["unit_price_minor"])
        for doc in bills:
            if doc["kind"] != "invoice":
                continue
            for line in doc["lines"]:
                prices = agreed.get((line["item"], doc["currency"]))
                if line["unit_price_minor"] is None or not prices:
                    continue
                checked_prices.add(id(doc))
                if line["unit_price_minor"] not in prices:
                    add("price_differs", [doc] + orders, "unit price differs from the order or delivery note",
                        item=line["item_label"], description=line["description"], unit=line["unit"],
                        unit_price_billed=str(line["unit_price_minor"]),
                        unit_price_agreed=", ".join(str(x) for x in sorted(prices)))

    # ponytail: a group that cites several orders counts its lines toward each order that lists the same item.
    # Fine while one item sits on one order; upgrade path is a per-line order reference.
    for o, order in enumerate(docs):
        if order["kind"] != "order":
            continue
        roots = [root for root, os in group_orders.items() if o in os]
        related = [docs[i] for root in roots for i in groups[root]]
        for item in sorted({l["item"] for l in order["lines"] if l["item"]}):
            ordered_lines = [l for l in order["lines"] if l["item"] == item]
            delivered_lines = [(d, l) for d in related if d["kind"] == "proof" for l in d["lines"] if l["item"] == item]
            billed_lines = [(d, l) for d in related if d["kind"] in BILLS for l in d["lines"] if l["item"] == item]
            first = ordered_lines[0]
            fields = {"item": first["item_label"], "description": first["description"], "unit": first["unit"]}
            lines = ordered_lines + [l for _, l in delivered_lines + billed_lines]
            if any(l["quantity"] is None for l in lines):
                continue
            if len({(l["unit"].casefold(), l["unit_kind"]) for l in lines}) > 1:
                add("unit_differs", [order], "units differ between the order and what followed it", **fields)
                continue
            ordered = sum((l["quantity"] for l in ordered_lines), Decimal(0))
            delivered_qty = sum((sign(d) * l["quantity"] for d, l in delivered_lines), Decimal(0))
            billed_qty = sum((sign(d) * l["quantity"] for d, l in billed_lines), Decimal(0))
            fields.update(ordered=text(ordered), delivered=text(delivered_qty), billed=text(billed_qty))
            if billed_qty > ordered and compare(billed_qty, ordered, first["unit_kind"]) == "differs":
                add("billed_over_order", [order], difference=text(billed_qty - ordered), **fields)
            if compare(delivered_qty, ordered, first["unit_kind"]) == "differs":
                status = "delivered_over_order" if delivered_qty > ordered else "open_on_order"
                add(status, [order], difference=text(delivered_qty - ordered), **fields)

    invoices = [d for d in docs if d["kind"] == "invoice"]
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1
    for row in rows:
        for column in CSV_COLUMNS:
            row.setdefault(column, "")
    return {
        "documents": len(docs),
        "tolerances": {"count": "0", "hours": text(hours_tolerance), "measured_percent": text(measured_percent)},
        "counts": dict(sorted(counts.items())),
        "findings": [{k: v for k, v in row.items() if v} for row in rows if row["status"] not in OK],
        "rows": rows,
        "prices_not_checked": sorted({d["number"] for d in invoices if id(d) not in checked_prices}),
    }


def write_csv(report: dict, path: Path, delimiter: str, labels: dict) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow([labels.get(column, column) for column in CSV_COLUMNS])
        for row in report["rows"]:
            writer.writerow([labels.get(row[c], row[c]) if c == "status" else row[c] for c in CSV_COLUMNS])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="JSON files or folders of JSON files")
    parser.add_argument("--measured-percent", default="1", help="tolerance for weighed or measured goods, in percent")
    parser.add_argument("--hours-tolerance", default="0", help="tolerance for hours, for example 0.25")
    parser.add_argument("--csv", help="write every compared row to this CSV file")
    parser.add_argument("--delimiter", default=",", help="CSV delimiter, for example ; for spreadsheet apps that expect it")
    parser.add_argument("--labels", help="JSON file mapping column names and status codes to the user's language")
    args = parser.parse_args()

    measured = decimal(args.measured_percent, "--measured-percent")
    hours = decimal(args.hours_tolerance, "--hours-tolerance")
    if measured is None or hours is None or measured < 0 or hours < 0:
        fail("tolerances must be zero or more")
    report = match(load(args.inputs), measured, hours)
    if args.csv:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8")) if args.labels else {}
        write_csv(report, Path(args.csv), args.delimiter, labels)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
