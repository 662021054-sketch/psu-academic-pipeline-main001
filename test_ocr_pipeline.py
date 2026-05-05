"""
End-to-end test for ocr_pipeline.py.

Creates a synthetic invoice PNG using Pillow (no external file needed),
runs the full OCR → Gemini pipeline, and validates the output shape.

Usage:
    GEMINI_API_KEY=<key> python test_ocr_pipeline.py
"""

import json
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Synthetic test document
# ---------------------------------------------------------------------------

INVOICE_TEXT_LINES = [
    "INVOICE",
    "",
    "Invoice No : INV-2024-00789",
    "Date       : 15 March 2024",
    "Due Date   : 15 April 2024",
    "",
    "Bill To:",
    "Acme Corporation",
    "123 Business Park, Bangkok 10110",
    "Tel: +66 2 555 0100",
    "",
    "Item              Qty   Unit Price    Amount",
    "----------------------------------------------",
    "Web Development    1    50,000.00    50,000.00",
    "UI/UX Design       1    20,000.00    20,000.00",
    "Server Setup       2     5,000.00    10,000.00",
    "----------------------------------------------",
    "                        Subtotal:    80,000.00",
    "                        VAT 7%:       5,600.00",
    "                        TOTAL:       85,600.00",
    "",
    "Payment: Bank Transfer",
    "Bank: Bangkok Bank  Account: 123-4-56789-0",
]


def _make_invoice_image(path: str, width: int = 800, font_size: int = 20) -> None:
    line_height = font_size + 8
    height = line_height * (len(INVOICE_TEXT_LINES) + 4)
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    y = 20
    for line in INVOICE_TEXT_LINES:
        draw.text((40, y), line, fill=(0, 0, 0), font=font)
        y += line_height

    img.save(path)
    print(f"[test] Synthetic invoice image created: {path}")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {message}")
    print(f"  PASS  {message}")


def validate_structured_output(data: dict) -> None:
    print("\n[test] Validating structured output …")
    _assert(isinstance(data, dict), "result is a dict")
    _assert("document_type" in data, "document_type key present")
    _assert("extracted_fields" in data, "extracted_fields key present")
    _assert("line_items" in data, "line_items key present")
    _assert("raw_amounts" in data, "raw_amounts key present")
    _assert("extraction_notes" in data, "extraction_notes key present")
    _assert(isinstance(data["line_items"], list), "line_items is a list")

    doc_type = data.get("document_type", "").lower()
    _assert("invoice" in doc_type or doc_type in {"receipt", "financial", "billing"},
            f"document_type recognised as invoice-like (got '{doc_type}')")

    fields = data.get("extracted_fields", {})
    _assert(len(fields) > 0, f"at least one extracted field (got {len(fields)})")

    amounts = data.get("raw_amounts", {})
    _assert(len(amounts) > 0, f"at least one raw amount (got {len(amounts)})")

    # Check output files exist
    output_dir = Path("output")
    _assert((output_dir / "raw_text.txt").exists(), "raw_text.txt saved")
    _assert((output_dir / "structured_data.json").exists(), "structured_data.json saved")

    raw_text = (output_dir / "raw_text.txt").read_text(encoding="utf-8")
    _assert(len(raw_text.strip()) > 10, f"raw_text.txt is non-trivial ({len(raw_text)} chars)")

    print("\n[test] All assertions passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        print("Set it and re-run: GEMINI_API_KEY=<key> python test_ocr_pipeline.py")
        raise SystemExit(1)

    # Import pipeline after dependency check
    from ocr_pipeline import process_document

    with tempfile.TemporaryDirectory() as tmpdir:
        invoice_path = str(Path(tmpdir) / "test_invoice.png")
        _make_invoice_image(invoice_path)

        print("\n[test] Running full pipeline …")
        result = process_document(invoice_path, api_key, output_dir="output")

    validate_structured_output(result)

    print("\n[test] Final JSON (first 800 chars):")
    pretty = json.dumps(result, ensure_ascii=False, indent=2)
    print(pretty[:800] + (" …" if len(pretty) > 800 else ""))


if __name__ == "__main__":
    main()
