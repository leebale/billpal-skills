#!/usr/bin/env python3
"""Attach one Factur-X EN 16931 XML to an existing invoice PDF. The model does not write the XML."""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import (  # noqa: E402
    ArrayObject,
    DecodedStreamObject,
    NameObject,
)

GUIDELINE = "urn:cen.eu:en16931:2017"
XRECHNUNG_GUIDELINE = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
ATTACHMENT = "factur-x.xml"
# ISO 3166 codes, so Greece is GR here even though its VAT ids start with EL.
EU_COUNTRIES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
}


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def xml_text(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def cents_text(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    amount = abs(cents)
    return f"{sign}{amount // 100}.{amount % 100:02d}"


def optional_str(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def require_str(data: dict, key: str) -> str:
    value = optional_str(data, key)
    if value is None:
        fail(f"missing {key}")
    return value


def require_int(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{key} must be integer cents")
    return value


def date_102(value: str, key: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%Y%m%d")
    except ValueError:
        fail(f"{key} must be YYYY-MM-DD")


def tax_registrations(vat_id: str | None, tax_number: str | None) -> str:
    out = ""
    if vat_id:
        out += f"<ram:SpecifiedTaxRegistration><ram:ID schemeID=\"VA\">{xml_text(vat_id)}</ram:ID></ram:SpecifiedTaxRegistration>"
    if tax_number:
        out += f"<ram:SpecifiedTaxRegistration><ram:ID schemeID=\"FC\">{xml_text(tax_number)}</ram:ID></ram:SpecifiedTaxRegistration>"
    return out


def build_xml(data: dict, check_totals: bool = True, profile: str = "facturx") -> str:
    seller_country = require_str(data, "seller_country")
    if seller_country not in EU_COUNTRIES:
        fail("seller_country must be an EU country code")
    category = require_str(data, "tax_category")
    if category not in {"S", "E", "AE", "K"}:
        fail("tax_category must be S, E, AE, or K")
    rate = data.get("tax_rate_percent")
    if rate is None:
        fail("tax_rate_percent is required")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        fail("tax_rate_percent must be a number like 20, not text like \"20\"")
    if category == "S":
        if rate <= 0:
            fail("category S needs a tax rate above 0")
        exemption = None
    else:
        if rate != 0:
            fail("categories E, AE, and K use tax rate 0")
        exemption = require_str(data, "exemption_reason")

    seller_vat = optional_str(data, "seller_vat_id")
    seller_tax_number = optional_str(data, "seller_tax_number")
    buyer_vat = optional_str(data, "buyer_vat_id")
    buyer_country = require_str(data, "buyer_country")
    if category == "K" and not seller_vat:
        fail("category K needs seller_vat_id")
    if not seller_vat and not seller_tax_number:
        fail("seller_vat_id or seller_tax_number is required")
    if category in {"AE", "K"} and not buyer_vat:
        fail(f"category {category} needs buyer_vat_id")

    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        fail("at least one line is required")

    line_xml: list[str] = []
    net_cents = 0
    rate_text = f"{rate:g}"
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            fail(f"line {index} is not an object")
        description = require_str(line, "description")
        quantity = require_str(line, "quantity")
        unit = optional_str(line, "unit") or "C62"
        unit_price = require_int(line, "unit_price_cents")
        line_net = require_int(line, "line_net_cents")
        try:
            expected = (Decimal(quantity) * Decimal(unit_price)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            fail(f"line {index} quantity must be a number like \"2\" or \"1.5\"")
        if expected != Decimal(line_net):
            fail(f"line {index} line_net_cents does not match quantity times unit_price_cents")
        net_cents += line_net
        line_xml.append(
            "<ram:IncludedSupplyChainTradeLineItem>"
            f"<ram:AssociatedDocumentLineDocument><ram:LineID>{index}</ram:LineID></ram:AssociatedDocumentLineDocument>"
            f"<ram:SpecifiedTradeProduct><ram:Name>{xml_text(description)}</ram:Name></ram:SpecifiedTradeProduct>"
            "<ram:SpecifiedLineTradeAgreement>"
            f"<ram:NetPriceProductTradePrice><ram:ChargeAmount>{cents_text(unit_price)}</ram:ChargeAmount></ram:NetPriceProductTradePrice>"
            "</ram:SpecifiedLineTradeAgreement>"
            "<ram:SpecifiedLineTradeDelivery>"
            f"<ram:BilledQuantity unitCode=\"{xml_text(unit)}\">{xml_text(quantity)}</ram:BilledQuantity>"
            "</ram:SpecifiedLineTradeDelivery>"
            "<ram:SpecifiedLineTradeSettlement>"
            "<ram:ApplicableTradeTax>"
            "<ram:TypeCode>VAT</ram:TypeCode>"
            f"<ram:CategoryCode>{category}</ram:CategoryCode>"
            f"<ram:RateApplicablePercent>{rate_text}</ram:RateApplicablePercent>"
            "</ram:ApplicableTradeTax>"
            "<ram:SpecifiedTradeSettlementLineMonetarySummation>"
            f"<ram:LineTotalAmount>{cents_text(line_net)}</ram:LineTotalAmount>"
            "</ram:SpecifiedTradeSettlementLineMonetarySummation>"
            "</ram:SpecifiedLineTradeSettlement>"
            "</ram:IncludedSupplyChainTradeLineItem>"
        )

    tax_cents = require_int(data, "tax_cents")
    grand_cents = require_int(data, "grand_total_cents")
    due_cents = require_int(data, "due_cents")
    paid_cents = data.get("paid_cents") or 0
    if isinstance(paid_cents, bool) or not isinstance(paid_cents, int) or paid_cents < 0:
        fail("paid_cents must be a whole number of cents, 0 or more")
    if check_totals:
        expected_tax = int((Decimal(net_cents) * Decimal(str(rate)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if tax_cents != expected_tax:
            fail(f"tax_cents must be {expected_tax} (net {net_cents} at {rate_text}%)")
        if grand_cents != net_cents + tax_cents:
            fail(f"grand_total_cents must be {net_cents + tax_cents}")
        if paid_cents > grand_cents:
            fail(f"paid_cents cannot be more than grand_total_cents ({grand_cents})")
        if due_cents != grand_cents - paid_cents:
            fail(f"due_cents must be {grand_cents - paid_cents} (grand total minus paid_cents)")
    currency = require_str(data, "currency")
    issued = date_102(require_str(data, "issue_date"), "issue_date")
    delivery_date = optional_str(data, "delivery_date")
    period_start = optional_str(data, "period_start")
    period_end = optional_str(data, "period_end")
    period_xml = ""
    if period_start or period_end:
        if not (period_start and period_end):
            fail("period_start and period_end go together")
        start = date_102(period_start, "period_start")
        end = date_102(period_end, "period_end")
        if end < start:
            fail("period_end must not be before period_start")
        period_xml = (
            "<ram:BillingSpecifiedPeriod>"
            f"<ram:StartDateTime><udt:DateTimeString format=\"102\">{start}</udt:DateTimeString></ram:StartDateTime>"
            f"<ram:EndDateTime><udt:DateTimeString format=\"102\">{end}</udt:DateTimeString></ram:EndDateTime>"
            "</ram:BillingSpecifiedPeriod>"
        )
    delivery_xml = ""
    # Without a delivery date or a period, the issue date stands in as the delivery date.
    if delivery_date or not period_xml:
        delivery = date_102(delivery_date, "delivery_date") if delivery_date else issued
        delivery_xml = (
            "<ram:ActualDeliverySupplyChainEvent>"
            f"<ram:OccurrenceDateTime><udt:DateTimeString format=\"102\">{delivery}</udt:DateTimeString></ram:OccurrenceDateTime>"
            "</ram:ActualDeliverySupplyChainEvent>"
        )
    due_date = optional_str(data, "due_date")
    payment_terms = optional_str(data, "payment_terms")
    terms_xml = ""
    if due_date or payment_terms:
        terms_xml = (
            "<ram:SpecifiedTradePaymentTerms>"
            + (f"<ram:Description>{xml_text(payment_terms)}</ram:Description>" if payment_terms else "")
            + (f"<ram:DueDateDateTime><udt:DateTimeString format=\"102\">{date_102(due_date, 'due_date')}</udt:DateTimeString></ram:DueDateDateTime>" if due_date else "")
            + "</ram:SpecifiedTradePaymentTerms>"
        )
    ship_to_xml = ""
    if category == "K":
        ship_to = optional_str(data, "deliver_to_country") or buyer_country
        ship_to_xml = f"<ram:ShipToTradeParty><ram:PostalTradeAddress><ram:CountryID>{xml_text(ship_to)}</ram:CountryID></ram:PostalTradeAddress></ram:ShipToTradeParty>"
    header_exemption = f"<ram:ExemptionReason>{xml_text(exemption)}</ram:ExemptionReason>" if exemption else ""
    if profile not in {"facturx", "xrechnung"}:
        fail("profile must be facturx or xrechnung")
    guideline = GUIDELINE
    buyer_reference_xml = ""
    seller_contact_xml = ""
    seller_electronic_xml = ""
    buyer_electronic_xml = ""
    payment_means_xml = ""
    if profile == "xrechnung":
        guideline = XRECHNUNG_GUIDELINE
        buyer_reference_xml = f"<ram:BuyerReference>{xml_text(require_str(data, 'buyer_reference'))}</ram:BuyerReference>"
        seller_contact_xml = (
            "<ram:DefinedTradeContact>"
            f"<ram:PersonName>{xml_text(require_str(data, 'seller_contact_name'))}</ram:PersonName>"
            "<ram:TelephoneUniversalCommunication>"
            f"<ram:CompleteNumber>{xml_text(require_str(data, 'seller_phone'))}</ram:CompleteNumber>"
            "</ram:TelephoneUniversalCommunication>"
            "<ram:EmailURIUniversalCommunication>"
            f"<ram:URIID>{xml_text(require_str(data, 'seller_email'))}</ram:URIID>"
            "</ram:EmailURIUniversalCommunication>"
            "</ram:DefinedTradeContact>"
        )
        seller_electronic_xml = (
            "<ram:URIUniversalCommunication>"
            f"<ram:URIID schemeID=\"EM\">{xml_text(require_str(data, 'seller_electronic_address'))}</ram:URIID>"
            "</ram:URIUniversalCommunication>"
        )
        buyer_electronic_xml = (
            "<ram:URIUniversalCommunication>"
            f"<ram:URIID schemeID=\"EM\">{xml_text(require_str(data, 'buyer_electronic_address'))}</ram:URIID>"
            "</ram:URIUniversalCommunication>"
        )
        payment_means_xml = (
            "<ram:SpecifiedTradeSettlementPaymentMeans>"
            "<ram:TypeCode>58</ram:TypeCode>"
            "<ram:PayeePartyCreditorFinancialAccount>"
            f"<ram:IBANID>{xml_text(require_str(data, 'iban'))}</ram:IBANID>"
            "</ram:PayeePartyCreditorFinancialAccount>"
            "<ram:PayeeSpecifiedCreditorFinancialInstitution>"
            f"<ram:BICID>{xml_text(require_str(data, 'bic'))}</ram:BICID>"
            "</ram:PayeeSpecifiedCreditorFinancialInstitution>"
            "</ram:SpecifiedTradeSettlementPaymentMeans>"
        )

    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<rsm:CrossIndustryInvoice"
        " xmlns:rsm=\"urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100\""
        " xmlns:ram=\"urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100\""
        " xmlns:udt=\"urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100\">"
        "<rsm:ExchangedDocumentContext>"
        "<ram:GuidelineSpecifiedDocumentContextParameter>"
        f"<ram:ID>{guideline}</ram:ID>"
        "</ram:GuidelineSpecifiedDocumentContextParameter>"
        "</rsm:ExchangedDocumentContext>"
        "<rsm:ExchangedDocument>"
        f"<ram:ID>{xml_text(require_str(data, 'invoice_number'))}</ram:ID>"
        "<ram:TypeCode>380</ram:TypeCode>"
        f"<ram:IssueDateTime><udt:DateTimeString format=\"102\">{issued}</udt:DateTimeString></ram:IssueDateTime>"
        "</rsm:ExchangedDocument>"
        "<rsm:SupplyChainTradeTransaction>"
        + "".join(line_xml)
        + "<ram:ApplicableHeaderTradeAgreement>"
        f"{buyer_reference_xml}"
        "<ram:SellerTradeParty>"
        # BR-CO-26: without a VAT id the seller needs an identifier, so the tax number doubles as one.
        + (f"<ram:ID>{xml_text(seller_tax_number)}</ram:ID>" if not seller_vat else "")
        + f"<ram:Name>{xml_text(require_str(data, 'seller_name'))}</ram:Name>"
        f"{seller_contact_xml}"
        "<ram:PostalTradeAddress>"
        f"<ram:PostcodeCode>{xml_text(require_str(data, 'seller_postcode'))}</ram:PostcodeCode>"
        f"<ram:LineOne>{xml_text(require_str(data, 'seller_street'))}</ram:LineOne>"
        f"<ram:CityName>{xml_text(require_str(data, 'seller_city'))}</ram:CityName>"
        f"<ram:CountryID>{seller_country}</ram:CountryID>"
        "</ram:PostalTradeAddress>"
        f"{seller_electronic_xml}"
        f"{tax_registrations(seller_vat, seller_tax_number)}"
        "</ram:SellerTradeParty>"
        "<ram:BuyerTradeParty>"
        f"<ram:Name>{xml_text(require_str(data, 'buyer_name'))}</ram:Name>"
        "<ram:PostalTradeAddress>"
        f"<ram:PostcodeCode>{xml_text(require_str(data, 'buyer_postcode'))}</ram:PostcodeCode>"
        f"<ram:LineOne>{xml_text(require_str(data, 'buyer_street'))}</ram:LineOne>"
        f"<ram:CityName>{xml_text(require_str(data, 'buyer_city'))}</ram:CityName>"
        f"<ram:CountryID>{xml_text(buyer_country)}</ram:CountryID>"
        "</ram:PostalTradeAddress>"
        f"{buyer_electronic_xml}"
        f"{tax_registrations(buyer_vat, None)}"
        "</ram:BuyerTradeParty>"
        "</ram:ApplicableHeaderTradeAgreement>"
        "<ram:ApplicableHeaderTradeDelivery>"
        f"{ship_to_xml}"
        f"{delivery_xml}"
        "</ram:ApplicableHeaderTradeDelivery>"
        "<ram:ApplicableHeaderTradeSettlement>"
        f"<ram:InvoiceCurrencyCode>{xml_text(currency)}</ram:InvoiceCurrencyCode>"
        f"{payment_means_xml}"
        "<ram:ApplicableTradeTax>"
        f"<ram:CalculatedAmount>{cents_text(tax_cents)}</ram:CalculatedAmount>"
        "<ram:TypeCode>VAT</ram:TypeCode>"
        f"{header_exemption}"
        f"<ram:BasisAmount>{cents_text(net_cents)}</ram:BasisAmount>"
        f"<ram:CategoryCode>{category}</ram:CategoryCode>"
        f"<ram:RateApplicablePercent>{rate_text}</ram:RateApplicablePercent>"
        "</ram:ApplicableTradeTax>"
        f"{period_xml}"
        f"{terms_xml}"
        "<ram:SpecifiedTradeSettlementHeaderMonetarySummation>"
        f"<ram:LineTotalAmount>{cents_text(net_cents)}</ram:LineTotalAmount>"
        f"<ram:TaxBasisTotalAmount>{cents_text(net_cents)}</ram:TaxBasisTotalAmount>"
        f"<ram:TaxTotalAmount currencyID=\"{xml_text(currency)}\">{cents_text(tax_cents)}</ram:TaxTotalAmount>"
        f"<ram:GrandTotalAmount>{cents_text(grand_cents)}</ram:GrandTotalAmount>"
        f"{f'<ram:TotalPrepaidAmount>{cents_text(paid_cents)}</ram:TotalPrepaidAmount>' if paid_cents else ''}"
        f"<ram:DuePayableAmount>{cents_text(due_cents)}</ram:DuePayableAmount>"
        "</ram:SpecifiedTradeSettlementHeaderMonetarySummation>"
        "</ram:ApplicableHeaderTradeSettlement>"
        "</rsm:SupplyChainTradeTransaction>"
        "</rsm:CrossIndustryInvoice>"
    )


def xmp_packet() -> bytes:
    xml = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
   <fx:DocumentType>INVOICE</fx:DocumentType>
   <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
   <fx:Version>1.0</fx:Version>
   <fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""
    return xml.encode("utf-8")


def write_pdf(source: Path, xml: str, output: Path) -> None:
    try:
        reader = PdfReader(source)
    except Exception as error:  # pypdf raises several types for broken files
        fail(f"could not read invoice pdf: {error}")
    if reader.is_encrypted:
        fail("the invoice pdf is password protected")
    if ATTACHMENT in reader.attachments:
        fail("the invoice pdf already has a factur-x.xml")
    writer = PdfWriter(clone_from=reader)

    embedded = writer.add_attachment(ATTACHMENT, xml.encode("utf-8"))
    embedded.pdf_object[NameObject("/AFRelationship")] = NameObject("/Alternative")
    writer.root_object[NameObject("/AF")] = ArrayObject([embedded.pdf_object.indirect_reference])

    # ponytail: replaces any XMP the source PDF had; enough for Factur-X readers, not a PDF/A-3 conversion.
    metadata = DecodedStreamObject()
    metadata.set_data(xmp_packet())
    metadata.update({
        NameObject("/Type"): NameObject("/Metadata"),
        NameObject("/Subtype"): NameObject("/XML"),
    })
    writer.root_object[NameObject("/Metadata")] = writer._add_object(metadata)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        writer.write(handle)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[2].lower().endswith(".xml"):
        source = Path(sys.argv[1])
        output = Path(sys.argv[2])
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            fail(f"could not read invoice json: {error}")
        if not isinstance(data, dict):
            fail("invoice json must be an object")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(build_xml(data, profile="xrechnung"), encoding="utf-8")
        return
    if len(sys.argv) != 4:
        fail("usage: python3 generate.py invoice.json invoice.pdf e-invoice.pdf\n       python3 generate.py invoice.json xrechnung.xml")
    source = Path(sys.argv[1])
    visual = Path(sys.argv[2])
    output = Path(sys.argv[3])
    if visual.resolve() == output.resolve():
        fail("write the e-invoice to a new file, not over invoice.pdf")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read invoice json: {error}")
    if not isinstance(data, dict):
        fail("invoice json must be an object")
    write_pdf(visual, build_xml(data), output)


if __name__ == "__main__":
    main()
