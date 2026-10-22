#!/usr/bin/env python3
"""Accept good PDFs (S, E, AE, K, a French seller, a service period, already paid) and reject a bad one against the deployed checker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("EINVOICE_CHECK_URL", "https://billpal.io/api/einvoice/check")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import generate  # noqa: E402
from pypdf import PdfWriter  # noqa: E402  (vendor/ is on the path via generate)


def post(pdf: Path) -> dict:
    request = urllib.request.Request(
        URL,
        data=pdf.read_bytes(),
        method="POST",
        headers={"Content-Type": "application/pdf"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        print(f"checker HTTP {error.code}: {body[:300]}", file=sys.stderr)
        raise SystemExit(1) from error
    except urllib.error.URLError as error:
        print(f"checker unreachable: {error.reason}", file=sys.stderr)
        raise SystemExit(1) from error
    if status != 200:
        print(f"checker HTTP {status}: {body[:300]}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(body)


def base() -> dict:
    return {
        "seller_name": "Seller GmbH",
        "seller_street": "Hauptstr. 1",
        "seller_postcode": "10115",
        "seller_city": "Berlin",
        "seller_country": "DE",
        "seller_vat_id": "DE123456789",
        "buyer_name": "Buyer AG",
        "buyer_street": "Markt 2",
        "buyer_postcode": "80331",
        "buyer_city": "Munich",
        "buyer_country": "DE",
        "invoice_number": "RE-1",
        "issue_date": "2024-01-15",
        "due_date": "2024-02-14",
        "currency": "EUR",
        "tax_category": "S",
        "tax_rate_percent": 19,
        "lines": [{
            "description": "Widget",
            "quantity": "2",
            "unit_price_cents": 5000,
            "line_net_cents": 10000,
        }],
        "tax_cents": 1900,
        "grand_total_cents": 11900,
        "due_cents": 11900,
    }


def zero_rated(category: str, reason: str, **extra: str) -> dict:
    data = base()
    data.update(tax_category=category, tax_rate_percent=0, exemption_reason=reason,
                tax_cents=0, grand_total_cents=10000, due_cents=10000, **extra)
    return data


GOOD = {
    "S": base(),
    "E": {**zero_rated("E", "Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemaess 19 UStG"),
          "seller_vat_id": None, "seller_tax_number": "30/123/45678"},
    "AE": zero_rated("AE", "Steuerschuldnerschaft des Leistungsempfaengers", buyer_vat_id="ATU12345678", buyer_country="AT"),
    "K": zero_rated("K", "Steuerfreie innergemeinschaftliche Lieferung", buyer_vat_id="ATU12345678", buyer_country="AT"),
    "FR": {
        **base(),
        "seller_name": "Atelier Lumiere SARL",
        "seller_street": "10 rue de Rivoli",
        "seller_postcode": "75004",
        "seller_city": "Paris",
        "seller_country": "FR",
        "seller_vat_id": "FR40123456789",
        "buyer_name": "Client Rhone SAS",
        "buyer_street": "5 place Bellecour",
        "buyer_postcode": "69002",
        "buyer_city": "Lyon",
        "buyer_country": "FR",
        "invoice_number": "F-2024-001",
        "tax_rate_percent": 20,
        "tax_cents": 2000,
        "grand_total_cents": 12000,
        "due_cents": 12000,
    },
    "period": {**base(), "period_start": "2024-01-01", "period_end": "2024-01-08"},
    "paid": {**base(), "paid_cents": 11900, "due_cents": 0},
}

REFUSED = {
    "us": ({**base(), "seller_country": "US"}, "EU country"),
    "text_rate": ({**base(), "tax_rate_percent": "19"}, "must be a number"),
    "half_period": ({**base(), "period_start": "2024-01-01"}, "go together"),
    "backwards_period": ({**base(), "period_start": "2024-01-08", "period_end": "2024-01-01"}, "before period_start"),
    "paid_still_due": ({**base(), "paid_cents": 11900}, "due_cents must be 0"),
    "overpaid": ({**base(), "paid_cents": 20000, "due_cents": 0}, "cannot be more than"),
}


def refuses(folder: Path, visual: Path, name: str, payload: dict, message: str) -> None:
    source = folder / f"{name}.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(HERE / "generate.py"), str(source), str(visual), str(folder / f"{name}.pdf")],
        capture_output=True,
        text=True,
    )
    if run.returncode == 0 or message not in run.stderr:
        print(f"{name}: expected a refusal with '{message}', got exit {run.returncode}: {run.stderr}", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        visual = folder / "invoice.pdf"
        blank = PdfWriter()
        blank.add_blank_page(width=595, height=842)
        with visual.open("wb") as handle:
            blank.write(handle)

        for name, (payload, message) in REFUSED.items():
            refuses(folder, visual, name, payload, message)
        for name, payload in GOOD.items():
            source = folder / f"{name}.json"
            pdf = folder / f"{name}.pdf"
            source.write_text(json.dumps(payload), encoding="utf-8")
            subprocess.run([sys.executable, str(HERE / "generate.py"), str(source), str(visual), str(pdf)], check=True)
            result = post(pdf)
            if result.get("accept") is not True:
                print(f"{name}: expected accept, got {result}", file=sys.stderr)
                raise SystemExit(1)

        bad_input = base()
        bad_input.update(tax_cents=100, grand_total_cents=10100, due_cents=10100)
        bad_pdf = folder / "bad.pdf"
        generate.write_pdf(visual, generate.build_xml(bad_input, check_totals=False), bad_pdf)
        bad = post(bad_pdf)
        if bad.get("accept") is not False:
            print(f"expected reject, got {bad}", file=sys.stderr)
            raise SystemExit(1)
        if not [error.get("id") for error in bad.get("errors") or []]:
            print(f"reject listed no rules: {bad}", file=sys.stderr)
            raise SystemExit(1)
    print("e-invoice check passed")


if __name__ == "__main__":
    main()
