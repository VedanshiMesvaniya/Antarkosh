"""Stage 4 — OCR (online vision model).

Scanned pages and standalone images are transcribed by the vision model (Gemini
first, then the configured fallbacks — see config/providers.yaml).
The transcription is then confidence-checked (garbage ratio, word ratio) and the
result is recorded on the page.

Only called on pages where Stage 3 left text empty (structure == SCANNED
or ocr_method == "pending_ocr"). Native-text pages are never re-OCR'd —
that would be a quality regression.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from src.core.confidence import check_ocr_confidence
from src.core.provider_client import ProviderRouter
from src.models.schemas import PageContent, PageStructure, ParsedDocument

logger = logging.getLogger(__name__)


async def run_ocr(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Run OCR on all pages that need it (structure == SCANNED or pending_ocr).

    Mutates the document's pages in-place, filling in text and OCR metadata.
    Native-text pages are skipped entirely.
    """
    pages_needing_ocr = [
        (i, p) for i, p in enumerate(document.pages)
        if p.structure == PageStructure.SCANNED and p.ocr_method == "pending_ocr"
    ]

    if not pages_needing_ocr:
        logger.info("No pages need OCR — skipping Stage 4")
        return document

    logger.info("Running OCR on %d pages", len(pages_needing_ocr))

    for idx, page in pages_needing_ocr:
        # Get page image
        image_data = _get_page_image(document.file_path, page.page_number)
        if image_data is None:
            document.warnings.append(f"Page {page.page_number}: could not extract image for OCR")
            continue

        # Run the tiered OCR chain
        text, confidence, method = await _ocr_chain(image_data, router)

        document.pages[idx].text = text
        document.pages[idx].ocr_confidence = confidence
        document.pages[idx].ocr_method = method

        logger.info(
            "Page %d OCR: method=%s, confidence=%.2f, chars=%d",
            page.page_number,
            method,
            confidence,
            len(text),
        )

    return document


def _get_page_image(file_path: str, page_number: int) -> bytes | None:
    """Extract a page as a PNG image from a PDF, or read an image file directly."""
    path = Path(file_path)

    # Standalone image file
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"):
        return path.read_bytes()

    # PDF page → PNG
    try:
        doc = fitz.open(file_path)
        page = doc[page_number - 1]
        # Render at 300 DPI for good OCR quality
        pix = page.get_pixmap(dpi=300)
        image_data = pix.tobytes("png")
        doc.close()
        return image_data
    except Exception as e:
        logger.error("Failed to extract page %d image from '%s': %s", page_number, file_path, e)
        return None


async def _ocr_chain(
    image_data: bytes,
    router: ProviderRouter,
) -> tuple[str, float, str]:
    """Transcribe a page image with the vision model.

    Returns (text, confidence_score, method_used). An empty transcription gets
    confidence 0.0, so downstream stages see the page as unreadable.
    """
    text = await _vision_llm_ocr(image_data, router)
    report = check_ocr_confidence(text)
    return text, report.confidence_score, "vision_llm"


async def _vision_llm_ocr(
    image_data: bytes,
    router: ProviderRouter,
) -> str:
    """Use a vision LLM to transcribe text from an image.

    Prompted to also self-report confidence on ambiguous words —
    something classical OCR engines can't do.
    """
    prompt = """Transcribe ALL text visible in this image as accurately as possible.

Rules:
- Preserve the original layout and reading order
- Use Markdown formatting (headings with #, tables with | pipes, lists with -)
- For tables, preserve column alignment and all cell values
- If any word is ambiguous or unclear, put it in [brackets] with a ? suffix, like [unclear?]
- Preserve mathematical notation using LaTeX when applicable
- Do NOT add any text that isn't visible in the image
- Do NOT summarize or paraphrase — transcribe verbatim

Output the transcribed text:"""

    try:
        return await router.vision(
            "ocr_vision",
            image_data,
            prompt,
            mime_type="image/png",
            max_tokens=8192,
        )
    except Exception as e:
        logger.error("Vision-LLM OCR failed: %s", e)
        return ""
