"""Deterministic, local-only extraction helpers for bill intake."""
from django.conf import settings
import pytesseract
pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from zipfile import BadZipFile

from docx import Document
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError


class LocalExtractionError(Exception):
    """Expected local document-extraction failure safe to show to staff."""


class OCRUnavailable(LocalExtractionError):
    """Raised when the optional local Tesseract executable is unavailable."""


def extract_document_text(file_obj, filename):
    """Extract document text without sending the document to any external service."""
    suffix = Path(filename).suffix.lower()
    file_obj.seek(0)
    if suffix == ".pdf":
        try:
            return "\n".join(page.extract_text() or "" for page in PdfReader(file_obj).pages)
        except (PdfReadError, OSError, ValueError, EOFError) as error:
            raise LocalExtractionError("The PDF could not be read locally.") from error
    if suffix == ".docx":
        try:
            return "\n".join(paragraph.text for paragraph in Document(file_obj).paragraphs)
        except (BadZipFile, OSError, ValueError, KeyError) as error:
            raise LocalExtractionError("The DOCX file could not be read locally.") from error
    if suffix in {".png", ".jpg", ".jpeg"}:
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)
            from pytesseract import TesseractNotFoundError
        except ImportError as error:
            raise OCRUnavailable(
                "Image extraction requires the local Tesseract program to be installed."
            ) from error
        try:
            with Image.open(file_obj) as image:
                return pytesseract.image_to_string(image)
        except TesseractNotFoundError as error:
            raise OCRUnavailable(
                "Image extraction requires the local Tesseract program to be installed."
            ) from error
        except (OSError, UnidentifiedImageError) as error:
            raise LocalExtractionError("The image could not be read locally.") from error
    raise LocalExtractionError("This file type is not supported for local extraction.")


def _money(value):
    cleaned = value.replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        return str(Decimal(cleaned).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def _labelled_money(text, labels):
    for label in labels:
        match = re.search(
            rf"(?im)^\s*{label}\s*[:\-]?\s*\$?\s*(\(?[\d,]+(?:\.\d{{2}})?\)?)",
            text,
        )
        if match:
            amount = _money(match.group(1))
            if amount is not None:
                return amount
    return ""


def _labelled_date(text, labels):
    for label in labels:
        match = re.search(
            rf"(?im)^\s*{label}\s*[:#-]?\s*([A-Za-z]{{3,9}}\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}|\d{{4}}-\d{{1,2}}-\d{{1,2}})",
            text,
        )
        if not match:
            continue
        value = match.group(1).replace(",", "")
        for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(value, pattern).date().isoformat()
            except ValueError:
                pass
    return ""


def _line_items(text):
    items = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.search(r"(?i)\b(subtotal|tax|total|amount due|balance due)\b", line):
            continue
        match = re.match(
            r"^(?P<description>.+?)\s{2,}(?P<quantity>\d+(?:\.\d+)?)\s+\$?(?P<price>[\d,]+(?:\.\d{2})?)(?:\s+\$?[\d,]+(?:\.\d{2})?)?$",
            line,
        )
        if not match:
            continue
        price = _money(match.group("price"))
        if price is not None:
            items.append(
                {
                    "description": match.group("description")[:255],
                    "quantity": match.group("quantity"),
                    "unit_price": price,
                    "taxable": False,
                }
            )
    return items[:20]


def parse_invoice_text(text):
    """Return conservative bill candidates from local text; values always require review."""
    vendor_match = re.search(r"(?im)^\s*(?:vendor|from|supplier)\s*:\s*(.+?)\s*$", text)
    number_match = re.search(
        r"(?im)^\s*(?:invoice|bill)\s*(?:number|no\.?|#)?\s*[:#-]\s*([A-Za-z0-9][A-Za-z0-9./_-]{0,99})",
        text,
    )
    total = _labelled_money(text, (r"grand\s+total", r"amount\s+due", r"balance\s+due", r"total"))
    return {
        "vendor_name": vendor_match.group(1).strip()[:255] if vendor_match else "",
        "bill_number": number_match.group(1) if number_match else "",
        "invoice_date": _labelled_date(text, (r"invoice\s+date", r"bill\s+date", r"date")),
        "due_date": _labelled_date(text, (r"due\s+date", r"payment\s+due")),
        "subtotal": _labelled_money(text, (r"subtotal",)),
        "tax": _labelled_money(text, (r"sales\s+tax", r"tax")),
        "total": total,
        "line_items": _line_items(text),
    }


def normalize_vendor_name(value):
    return re.sub(r"[^a-z0-9]+", "", value.casefold())
