"""
OCR + LLM document processing pipeline.
Extracts text via pdfplumber/PaddleOCR, structures it with Gemini.
"""

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def _extract_text_pdfplumber(pdf_path: str) -> str:
    import pdfplumber
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def _extract_text_paddleocr(file_path: str) -> str:
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    result = ocr.ocr(file_path, cls=True)
    lines = []
    for page in result:
        if page:
            for line in page:
                text = line[1][0]
                lines.append(text)
    return "\n".join(lines)


def _extract_text_tesseract(file_path: str) -> str:
    import pytesseract
    from PIL import Image
    img = Image.open(file_path)
    return pytesseract.image_to_string(img)


def _pdf_to_images(pdf_path: str, output_dir: str) -> list[str]:
    """Render each PDF page to a PNG for OCR fallback."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError(
            "PyMuPDF (fitz) is required to OCR scanned PDFs. "
            "Install with: pip install pymupdf"
        )
    doc = fitz.open(pdf_path)
    image_paths = []
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(doc):
        mat = fitz.Matrix(2.0, 2.0)  # 2× zoom → ~144 dpi
        pix = page.get_pixmap(matrix=mat)
        img_path = str(Path(output_dir) / f"_page_{i}.png")
        pix.save(img_path)
        image_paths.append(img_path)
    return image_paths


def extract_text(file_path: str, output_dir: str = "output") -> str:
    """
    Auto-detect file type and extract raw text.
    PDF  → pdfplumber first; if empty, render pages → PaddleOCR.
    Image → PaddleOCR directly, Tesseract as fallback.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            text = _extract_text_pdfplumber(file_path)
        except Exception as exc:
            print(f"[warn] pdfplumber failed ({exc}), falling back to PaddleOCR")
            text = ""

        if text.strip():
            print(f"[ocr] pdfplumber extracted {len(text)} chars from {path.name}")
            return text

        # No text layer — render to images then OCR
        print("[ocr] No text layer found, rendering pages for PaddleOCR …")
        tmp_dir = str(Path(output_dir) / "_tmp_pages")
        image_paths = _pdf_to_images(file_path, tmp_dir)
        parts = []
        for img in image_paths:
            try:
                parts.append(_extract_text_paddleocr(img))
            except Exception as exc:
                print(f"[warn] PaddleOCR failed on {img} ({exc}), trying Tesseract")
                try:
                    parts.append(_extract_text_tesseract(img))
                except Exception as exc2:
                    print(f"[warn] Tesseract also failed on {img}: {exc2}")
        # Clean up temp images
        for img in image_paths:
            try:
                os.remove(img)
            except OSError:
                pass
        return "\n".join(parts)

    elif suffix in {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}:
        try:
            text = _extract_text_paddleocr(file_path)
            print(f"[ocr] PaddleOCR extracted {len(text)} chars from {path.name}")
            return text
        except Exception as exc:
            print(f"[warn] PaddleOCR failed ({exc}), trying Tesseract")
            text = _extract_text_tesseract(file_path)
            print(f"[ocr] Tesseract extracted {len(text)} chars from {path.name}")
            return text
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


# ---------------------------------------------------------------------------
# LLM structuring
# ---------------------------------------------------------------------------

GEMINI_PROMPT = """\
You are a document parser. Extract all key information from the following raw \
OCR text and return ONLY a valid JSON object with no markdown, no code fences, \
and no preamble.

The JSON must contain:
- "document_type": string — invoice, receipt, contract, report, form, letter, \
  or other
- "extracted_fields": object — all key-value pairs found (dates, names, IDs, \
  addresses, phone numbers, etc.)
- "line_items": array of objects — each row from any table or itemised list \
  (description, quantity, unit_price, amount); empty array if none
- "raw_amounts": object — every monetary or numeric total found, preserving \
  original formatting (e.g. "1,234.56")
- "extraction_notes": array of strings — flag any values that are unclear, \
  partially legible, or ambiguous

Raw OCR text:
\"\"\"
{raw_text}
\"\"\"
"""


def structure_with_gemini(raw_text: str, api_key: str) -> dict:
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = GEMINI_PROMPT.format(raw_text=raw_text)

    response = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )

    raw_json = response.text.strip()

    # Strip accidental markdown fences just in case
    if raw_json.startswith("```"):
        raw_json = raw_json.split("```")[1]
        if raw_json.startswith("json"):
            raw_json = raw_json[4:]
        raw_json = raw_json.strip()

    try:
        structured = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned invalid JSON: {exc}\n\nRaw response:\n{raw_json}")

    # Attach token usage metadata if available
    try:
        usage = response.usage_metadata
        structured["_token_usage"] = {
            "prompt_tokens": usage.prompt_token_count,
            "response_tokens": usage.candidates_token_count,
            "total_tokens": usage.total_token_count,
        }
    except Exception:
        pass

    return structured


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_document(file_path: str, gemini_api_key: str, output_dir: str = "output") -> dict:
    """
    Full pipeline: OCR → Gemini → structured JSON.

    Parameters
    ----------
    file_path     : path to PDF, JPG, PNG, or TIFF
    gemini_api_key: Gemini API key
    output_dir    : directory for raw_text.txt and structured_data.json

    Returns
    -------
    dict with structured document data
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 1. OCR
    print(f"\n[pipeline] Extracting text from: {file_path}")
    raw_text = extract_text(file_path, output_dir)

    if not raw_text.strip():
        raise RuntimeError("OCR produced no text — check the file and OCR dependencies.")

    raw_text_path = Path(output_dir) / "raw_text.txt"
    raw_text_path.write_text(raw_text, encoding="utf-8")
    print(f"[pipeline] Raw text saved → {raw_text_path}")

    # 2. Gemini structuring
    print("[pipeline] Sending to Gemini for structuring …")
    structured = structure_with_gemini(raw_text, gemini_api_key)

    # 3. Save output
    json_path = Path(output_dir) / "structured_data.json"
    json_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[pipeline] Structured JSON saved → {json_path}")

    # 4. Console summary
    _print_summary(structured)

    return structured


def _print_summary(data: dict) -> None:
    print("\n" + "=" * 60)
    print("DOCUMENT PROCESSING SUMMARY")
    print("=" * 60)
    print(f"  Document type : {data.get('document_type', 'unknown')}")

    fields = data.get("extracted_fields", {})
    print(f"  Fields found  : {len(fields)}")
    for k, v in list(fields.items())[:8]:
        print(f"    {k}: {v}")
    if len(fields) > 8:
        print(f"    … and {len(fields) - 8} more")

    amounts = data.get("raw_amounts", {})
    if amounts:
        print(f"  Amounts       : {amounts}")

    line_items = data.get("line_items", [])
    print(f"  Line items    : {len(line_items)}")

    notes = data.get("extraction_notes", [])
    if notes:
        print(f"  Notes         : {'; '.join(notes[:3])}")

    usage = data.get("_token_usage", {})
    if usage:
        print(f"  Token usage   : {usage.get('total_tokens', '?')} total "
              f"({usage.get('prompt_tokens', '?')} prompt + "
              f"{usage.get('response_tokens', '?')} response)")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ocr_pipeline.py <file_path> [output_dir]")
        print("Set GEMINI_API_KEY environment variable before running.")
        sys.exit(1)

    input_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "output"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    result = process_document(input_path, api_key, out_dir)
    print("\nFull structured JSON:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
