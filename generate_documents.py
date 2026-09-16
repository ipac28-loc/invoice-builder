#!/usr/bin/env python3
"""Generate English invoice and payment-certificate PDFs from the Indico API."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import ssl
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


TRUE_VALUES = {"1", "true", "yes", "y", "requested", "on"}
PAID_VALUES = {"paid", "complete", "completed", "successful", "succeeded"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--registration-id", type=int, help="Generate documents for one registration only")
    parser.add_argument("--list-forms", action="store_true", help="List registration forms and exit")
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    for key in ("issuer", "event", "indico", "columns"):
        if key not in config:
            raise ValueError(f"Missing top-level config key: {key}")
    return config


class IndicoAPIError(RuntimeError):
    """An error returned by or encountered while contacting Indico."""


class IndicoClient:
    def __init__(self, config: dict[str, Any]):
        api = config["indico"]
        self.base_url = api["base_url"].rstrip("/") + "/"
        self.event_id = int(api["event_id"])
        self.form_id = int(api.get("registration_form_id") or 0)
        self.timeout = float(api.get("timeout_seconds", 30))
        self.workers = max(1, min(int(api.get("parallel_requests", 8)), 20))
        token_env = api.get("token_env", "INDICO_API_TOKEN")
        self.token = os.environ.get(token_env, "").strip()
        if not self.token:
            raise ValueError(f"API token is missing. Set the {token_env} environment variable.")
        self.ssl_context = ssl.create_default_context()
        if not api.get("verify_tls", True):
            raise ValueError("TLS verification may not be disabled; install the correct CA certificate instead.")

    def get(self, path: str) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "indico-invoice-pdf-tool/2.0",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                content_type = response.headers.get_content_type()
                body = response.read()
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise IndicoAPIError(f"Indico returned HTTP {exc.code} for {url}{suffix}") from exc
        except URLError as exc:
            raise IndicoAPIError(f"Could not connect to Indico at {url}: {exc.reason}") from exc
        if content_type != "application/json":
            raise IndicoAPIError(f"Expected JSON from {url}, received {content_type}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise IndicoAPIError(f"Indico returned invalid JSON from {url}") from exc

    def forms(self) -> list[dict[str, Any]]:
        result = self.get(f"api/checkin/event/{self.event_id}/forms/")
        if not isinstance(result, list):
            raise IndicoAPIError("Unexpected response while retrieving registration forms")
        return result

    def registration_ids(self) -> list[int]:
        if not self.form_id:
            raise ValueError("indico.registration_form_id must be set in config.json")
        result = self.get(f"api/checkin/event/{self.event_id}/forms/{self.form_id}/registrations/")
        if not isinstance(result, list):
            raise IndicoAPIError("Unexpected response while retrieving registrations")
        return [int(item["id"]) for item in result]

    def registration(self, registration_id: int) -> dict[str, Any]:
        if not self.form_id:
            raise ValueError("indico.registration_form_id must be set in config.json")
        result = self.get(
            f"api/checkin/event/{self.event_id}/forms/{self.form_id}/registrations/"
            f"{quote(str(registration_id))}"
        )
        if not isinstance(result, dict):
            raise IndicoAPIError(f"Unexpected response for registration {registration_id}")
        return result

    def registrations(self, registration_id: int | None = None) -> list[dict[str, Any]]:
        ids = [registration_id] if registration_id is not None else self.registration_ids()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            return list(executor.map(self.registration, ids))


def display_date(value: Any, *, default_today: bool = False) -> str:
    if value in (None, ""):
        return date.today().strftime("%d %B %Y") if default_today else ""
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%d %B %Y")


def field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(field_value(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return field_value(value["text"])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def flatten_registration_fields(registration: dict[str, Any]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    for section in registration.get("registration_data") or []:
        for field in section.get("fields") or []:
            title = str(field.get("title") or "").strip()
            if title:
                flattened[title] = field_value(field.get("data"))
    return flattened


def registration_to_row(registration: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    fields_by_title = flatten_registration_fields(registration)
    api_fields = config.get("api_fields", {})

    def custom(logical_name: str) -> str:
        title = api_fields.get(logical_name, "")
        return fields_by_title.get(title, "") if title else ""

    logical_values = {
        "registration_id": str(registration.get("id", "")),
        "participant_name": field_value(registration.get("full_name")),
        "affiliation": custom("affiliation"),
        "billing_name": custom("billing_name") or field_value(registration.get("full_name")),
        "billing_address": custom("billing_address"),
        "description": custom("description") or config["event"].get("default_description", "Registration fee"),
        "amount": field_value(registration.get("price")),
        "tax_amount": custom("tax_amount") or "0",
        "currency": field_value(registration.get("currency")) or config.get("default_currency", "JPY"),
        "invoice_date": display_date(custom("invoice_date"), default_today=True),
        "invoice_requested": custom("invoice_requested"),
        "payment_status": "Paid" if registration.get("is_paid") else "Unpaid",
        "payment_date": display_date(registration.get("payment_date")),
        "payment_reference": custom("payment_reference"),
    }
    return {
        config["columns"][logical_name]: value
        for logical_name, value in logical_values.items()
        if config["columns"].get(logical_name)
    }


def register_font(config: dict[str, Any]) -> tuple[str, str]:
    font = config.get("font", {})
    regular = font.get("regular", "")
    bold = font.get("bold", "")
    candidates = [
        (regular, bold),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/google-noto-sans-fonts/NotoSans-Regular.ttf", "/usr/share/fonts/google-noto-sans-fonts/NotoSans-Bold.ttf"),
    ]
    regular, bold = next(((r, b) for r, b in candidates if r and Path(r).is_file()), ("", ""))
    if regular:
        pdfmetrics.registerFont(TTFont("InvoiceFont", regular))
        if bold and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("InvoiceFont-Bold", bold))
            return "InvoiceFont", "InvoiceFont-Bold"
        return "InvoiceFont", "InvoiceFont"
    return "Helvetica", "Helvetica-Bold"


def cell(row: dict[str, str], config: dict[str, Any], logical_name: str) -> str:
    column_name = config["columns"].get(logical_name, "")
    return (row.get(column_name, "") if column_name else "").strip()


def is_true(value: str, default: bool = True) -> bool:
    return default if value == "" else value.casefold() in TRUE_VALUES


def is_paid(value: str) -> bool:
    return value.casefold() in PAID_VALUES


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return value.strip("_.") or "registration"


def as_money(value: str) -> Decimal:
    cleaned = re.sub(r"[^0-9.-]", "", value)
    try:
        return Decimal(cleaned or "0")
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary value: {value!r}") from exc


def money(amount: Decimal, currency: str) -> str:
    digits = 0 if currency.upper() == "JPY" else 2
    return f"{currency.upper()} {amount:,.{digits}f}"


def esc(value: Any) -> str:
    text = str(value or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName=bold, fontSize=24, leading=28, textColor=colors.HexColor("#19324D"), alignment=TA_RIGHT, spaceAfter=10, tracking=0),
        "brand": ParagraphStyle("brand", parent=base["Normal"], fontName=bold, fontSize=12, leading=15, textColor=colors.HexColor("#19324D")),
        "body": ParagraphStyle("body", parent=base["Normal"], fontName=regular, fontSize=9.5, leading=14, textColor=colors.HexColor("#25313C")),
        "small": ParagraphStyle("small", parent=base["Normal"], fontName=regular, fontSize=8, leading=11, textColor=colors.HexColor("#536170")),
        "label": ParagraphStyle("label", parent=base["Normal"], fontName=bold, fontSize=8, leading=10, textColor=colors.HexColor("#5D6B78"), spaceAfter=2),
        "section": ParagraphStyle("section", parent=base["Heading2"], fontName=bold, fontSize=9, leading=12, textColor=colors.HexColor("#19324D"), spaceAfter=5),
        "right": ParagraphStyle("right", parent=base["Normal"], fontName=regular, fontSize=9, leading=13, alignment=TA_RIGHT),
        "center": ParagraphStyle("center", parent=base["Normal"], fontName=regular, fontSize=10, leading=15, alignment=TA_CENTER),
        "cert_title": ParagraphStyle("cert_title", parent=base["Title"], fontName=bold, fontSize=23, leading=29, alignment=TA_CENTER, textColor=colors.HexColor("#19324D"), spaceAfter=8),
        "cert_lead": ParagraphStyle("cert_lead", parent=base["Normal"], fontName=regular, fontSize=11, leading=18, alignment=TA_CENTER, textColor=colors.HexColor("#25313C")),
    }


def document(path: Path, title: str, config: dict[str, Any]) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        str(path), pagesize=A4,
        rightMargin=20 * mm, leftMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title,
        author=config["issuer"]["name"],
        subject=config["event"]["name"],
    )


def footer(canvas, doc, config: dict[str, Any], regular: str) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D8DEE5"))
    canvas.line(20 * mm, 14 * mm, A4[0] - 20 * mm, 14 * mm)
    canvas.setFont(regular, 7)
    canvas.setFillColor(colors.HexColor("#6D7883"))
    canvas.drawString(20 * mm, 9 * mm, config["issuer"]["name"])
    canvas.drawRightString(A4[0] - 20 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def issuer_block(config: dict[str, Any], style: dict[str, ParagraphStyle]) -> Paragraph:
    issuer = config["issuer"]
    parts = [f"<b>{esc(issuer['name'])}</b>", esc(issuer.get("address", "")), esc(issuer.get("email", ""))]
    if issuer.get("tax_registration_number"):
        parts.append(f"Tax registration no.: {esc(issuer['tax_registration_number'])}")
    return Paragraph("<br/>".join(p for p in parts if p), style["body"])


def header(title: str, config: dict[str, Any], style: dict[str, ParagraphStyle]) -> Table:
    event = config["event"]
    left = Paragraph(f"<b>{esc(event['short_name'])}</b><br/><font size='8'>{esc(event['dates'])}<br/>{esc(event['venue'])}</font>", style["brand"])
    right = Paragraph(esc(title), style["title"])
    table = Table([[left, right]], colWidths=[85 * mm, 85 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return table


def metadata_table(items: list[tuple[str, str]], style: dict[str, ParagraphStyle]) -> Table:
    rows = [[Paragraph(esc(label), style["label"]), Paragraph(esc(value), style["right"])] for label, value in items]
    table = Table(rows, colWidths=[31 * mm, 55 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#E3E7EB")),
    ]))
    return table


def make_invoice(row: dict[str, str], config: dict[str, Any], out: Path, regular: str, bold: str) -> None:
    s = styles(regular, bold)
    reg_id = cell(row, config, "registration_id")
    invoice_number = config["numbering"]["invoice"].format(registration_id=reg_id)
    currency = cell(row, config, "currency") or config.get("default_currency", "JPY")
    amount = as_money(cell(row, config, "amount"))
    tax = as_money(cell(row, config, "tax_amount"))
    total = amount + tax
    bill_name = cell(row, config, "billing_name") or cell(row, config, "participant_name")
    bill_address = cell(row, config, "billing_address")
    participant = cell(row, config, "participant_name")
    affiliation = cell(row, config, "affiliation")
    description = cell(row, config, "description") or config["event"].get("default_description", "Registration fee")

    story = [header("INVOICE", config, s), HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#19324D")), Spacer(1, 7 * mm)]
    bill = Paragraph(f"<b>{esc(bill_name)}</b><br/>{esc(bill_address)}", s["body"])
    meta = metadata_table([
        ("Invoice number", invoice_number),
        ("Invoice date", cell(row, config, "invoice_date")),
        ("Registration ID", reg_id),
    ], s)
    top = Table([[Paragraph("BILL TO", s["section"]), ""], [bill, meta]], colWidths=[84 * mm, 86 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("SPAN", (0, 0), (0, 0)), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story.extend([top, Spacer(1, 11 * mm)])

    line_data = [
        [Paragraph("DESCRIPTION", s["label"]), Paragraph("AMOUNT", s["label"])],
        [Paragraph(f"<b>{esc(description)}</b><br/><font size='8'>Participant: {esc(participant)}<br/>Affiliation: {esc(affiliation)}</font>", s["body"]), Paragraph(money(amount, currency), s["right"])],
    ]
    lines = Table(line_data, colWidths=[130 * mm, 40 * mm])
    lines.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF3F7")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CAD2DA")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#DCE2E8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([lines, Spacer(1, 5 * mm)])

    totals_rows = []
    if tax:
        totals_rows.extend([["Subtotal", money(amount, currency)], [config.get("tax_label", "Tax"), money(tax, currency)]])
    totals_rows.append(["TOTAL", money(total, currency)])
    totals = Table([[Paragraph(esc(a), s["label"]), Paragraph(esc(b), s["right"])] for a, b in totals_rows], colWidths=[31 * mm, 44 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#19324D")), ("TOPPADDING", (0, -1), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.extend([totals, Spacer(1, 13 * mm), Paragraph("ISSUED BY", s["section"]), issuer_block(config, s)])
    if config.get("invoice_note"):
        story.extend([Spacer(1, 8 * mm), Paragraph(esc(config["invoice_note"]), s["small"])])
    doc = document(out, f"Invoice {invoice_number}", config)
    doc.build(story, onFirstPage=lambda c, d: footer(c, d, config, regular), onLaterPages=lambda c, d: footer(c, d, config, regular))


def make_certificate(row: dict[str, str], config: dict[str, Any], out: Path, regular: str, bold: str) -> None:
    s = styles(regular, bold)
    reg_id = cell(row, config, "registration_id")
    invoice_number = config["numbering"]["invoice"].format(registration_id=reg_id)
    certificate_number = config["numbering"]["certificate"].format(registration_id=reg_id)
    currency = cell(row, config, "currency") or config.get("default_currency", "JPY")
    amount = as_money(cell(row, config, "amount")) + as_money(cell(row, config, "tax_amount"))
    participant = cell(row, config, "participant_name")
    affiliation = cell(row, config, "affiliation")
    description = cell(row, config, "description") or config["event"].get("default_description", "Registration fee")
    payment_date = cell(row, config, "payment_date")
    reference = cell(row, config, "payment_reference")

    story = [header("", config, s), Spacer(1, 12 * mm), Paragraph("PAYMENT CERTIFICATE", s["cert_title"]), HRFlowable(width="48%", thickness=1.2, color=colors.HexColor("#19324D")), Spacer(1, 10 * mm)]
    lead = f"This is to certify that the following payment was received in full for {esc(config['event']['name'])}."
    story.extend([Paragraph(lead, s["cert_lead"]), Spacer(1, 12 * mm)])
    details = [
        ("Certificate number", certificate_number),
        ("Related invoice", invoice_number),
        ("Participant", participant),
        ("Affiliation", affiliation),
        ("Description", description),
        ("Amount received", money(amount, currency)),
        ("Payment date", payment_date),
    ]
    if reference:
        details.append(("Payment reference", reference))
    rows = [[Paragraph(esc(label), s["label"]), Paragraph(esc(value), s["body"])] for label, value in details]
    table = Table(rows, colWidths=[48 * mm, 106 * mm], hAlign="CENTER")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF3F7")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CAD2DA")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#DCE2E8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([KeepTogether(table), Spacer(1, 16 * mm), Paragraph("Issued by", s["section"]), issuer_block(config, s)])
    if config.get("certificate_note"):
        story.extend([Spacer(1, 8 * mm), Paragraph(esc(config["certificate_note"]), s["small"])])
    doc = document(out, f"Payment Certificate {certificate_number}", config)
    doc.build(story, onFirstPage=lambda c, d: footer(c, d, config, regular), onLaterPages=lambda c, d: footer(c, d, config, regular))


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    client = IndicoClient(config)
    if args.list_forms:
        for form in client.forms():
            print(f"{form['id']}\t{form['title']}\tregistrations={form.get('registration_count', 0)}")
        return 0

    regular, bold = register_font(config)
    invoice_dir = args.output_dir / "invoices"
    certificate_dir = args.output_dir / "payment_certificates"
    invoice_dir.mkdir(parents=True, exist_ok=True)
    certificate_dir.mkdir(parents=True, exist_ok=True)
    counts = {"invoice": 0, "certificate": 0, "skipped": 0}

    excluded_states = {
        str(value).casefold()
        for value in config.get("excluded_registration_states", ["rejected", "withdrawn"])
    }
    registrations = client.registrations(args.registration_id)
    for registration in registrations:
        if str(registration.get("state", "")).casefold() in excluded_states:
            counts["skipped"] += 1
            continue
        row = registration_to_row(registration, config)
        reg_id = cell(row, config, "registration_id")
        if not reg_id:
            raise ValueError("Indico returned a registration without an ID")
        base = safe_name(reg_id)
        requested = is_true(
            cell(row, config, "invoice_requested"),
            config.get("generate_invoice_by_default", True),
        )
        if requested:
            make_invoice(row, config, invoice_dir / f"Invoice_{base}.pdf", regular, bold)
            counts["invoice"] += 1
        status = cell(row, config, "payment_status")
        if is_paid(status):
            make_certificate(
                row,
                config,
                certificate_dir / f"Payment_Certificate_{base}.pdf",
                regular,
                bold,
            )
            counts["certificate"] += 1
        if not requested and not is_paid(status):
            counts["skipped"] += 1

    print(f"Generated {counts['invoice']} invoice(s), {counts['certificate']} payment certificate(s); skipped {counts['skipped']} row(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, IndicoAPIError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
