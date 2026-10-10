"""Stages 7 & 8 — Chart/Graph Analysis and Image Understanding.

Combined into one module because they share the same vision-LLM infrastructure.
Separated by prompt and routing — charts get chart-specific prompts and
model preferences, general images get broader understanding prompts.

Stage 7: Gemini Flash primary → fallback providers → optional cross-check
Stage 8: Gemini Flash primary → Nemotron-Nano-VL fallback
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from src.core.config import PROJECT_ROOT, settings
from src.core.provider_client import ProviderRouter
from src.models.schemas import FigureData, PageContent, ParsedDocument

logger = logging.getLogger(__name__)


# Timeouts and concurrency come from settings (core/config.py), sized for online
# vision APIs: short per-image limit, a few images in flight at once.


async def analyze_visuals(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Extract and analyze charts, graphs, and images from document pages.

    Runs on pages that have embedded images detected by PyMuPDF.
    Enforces a per-image timeout (30s) and an overall stage timeout (120s)
    so that provider 503s / hangs can never block the ingestion pipeline.
    """
    import asyncio

    try:
        return await asyncio.wait_for(
            _analyze_visuals_impl(document, router),
            timeout=settings.vision_stage_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Visual analysis timed out after %.0fs for '%s' — continuing without figures",
            settings.vision_stage_timeout_seconds, document.file_path,
        )
        return document


async def _analyze_visuals_impl(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Internal implementation of analyze_visuals (wrapped by timeout above)."""
    if not document.file_path.lower().endswith(".pdf"):
        # For non-PDFs, visual analysis happens during Stage 3 parsing
        # (e.g., PPTX slides rendered to images)
        return document

    try:
        doc = fitz.open(document.file_path)
    except Exception as e:
        logger.warning("Could not open document for visual analysis: %s", e)
        return document

    for idx, page_content in enumerate(document.pages):
        try:
            page = doc[page_content.page_number - 1]
            images = page.get_images(full=True)
        except Exception:
            continue

        if not images:
            continue

        import asyncio

        semaphore = asyncio.Semaphore(settings.vision_concurrency)
        _VISION_TIMEOUT = settings.vision_timeout_seconds  # per image — prevents hung 503s from blocking

        async def process_figure(fig_idx: int, img_info: tuple) -> FigureData | None:
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                if base_image is None:
                    return None

                image_data = base_image["image"]
                mime_type = f"image/{base_image.get('ext', 'png')}"

                # Skip tiny images (likely icons/bullets, not figures)
                width = base_image.get("width", 0)
                height = base_image.get("height", 0)
                if width < 100 or height < 100:
                    return None

                # Determine if this looks like a chart/graph or a general image
                is_chart = _likely_chart(width, height)

                async with semaphore:
                    try:
                        if is_chart:
                            description = await asyncio.wait_for(
                                _analyze_chart(image_data, mime_type, router),
                                timeout=_VISION_TIMEOUT,
                            )
                            task_used = "chart_analysis"
                        else:
                            description = await asyncio.wait_for(
                                _analyze_image(image_data, mime_type, router),
                                timeout=_VISION_TIMEOUT,
                            )
                            task_used = "image_understanding"
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Vision timeout (>%.0fs) for xref %d on page %d — skipping",
                            _VISION_TIMEOUT, xref, page_content.page_number,
                        )
                        return None

                # Both prompts ask for "VISIBLE TEXT / MEANING" sections. If a model
                # ignores the format the whole answer is kept as the description.
                ocr_text, description = _split_image_analysis(description)

                image_path = ""
                if description or ocr_text:
                    image_path = _save_figure_image(
                        document.file_path,
                        page_content.page_number,
                        fig_idx,
                        image_data,
                        base_image.get("ext", "png"),
                    )

                return FigureData(
                    page_number=page_content.page_number,
                    figure_index=fig_idx,
                    description=description,
                    ocr_text=ocr_text,
                    image_path=image_path,
                    confidence=0.85 if (description or ocr_text) else 0.0,
                    extraction_method=task_used,
                )

            except Exception as e:
                logger.debug("Could not extract image xref %d: %s", xref, e)
                return None

        tasks = [process_figure(fig_idx, img_info) for fig_idx, img_info in enumerate(images)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        figures = [
            r for r in results
            if r is not None and not isinstance(r, BaseException)
        ]

        if figures:
            document.pages[idx].figures = figures

    doc.close()
    return document


def _likely_chart(width: int, height: int) -> bool:
    """Rough heuristic: charts tend to be wider/taller than photos.

    This is intentionally simple — the vision-LLM will handle the real
    classification. We're just choosing which prompt to use.
    """
    aspect = width / height if height > 0 else 1.0
    # Charts are often wider than tall, and of moderate size
    return 0.5 < aspect < 3.0 and width > 200 and height > 200


async def _analyze_chart(
    image_data: bytes,
    mime_type: str,
    router: ProviderRouter,
) -> str:
    """Analyze a chart/graph image — extract data, axes, trends."""
    prompt = """Analyze this chart, graph or diagram in detail.

Answer in exactly this format, with these two labels:

VISIBLE TEXT:
Every word and number of text that appears in the image, verbatim, one line per
text element (title, axis labels, legend, node or box labels, annotations).
Write "none" if the image contains no text.

MEANING:
Extract and describe:
1. Type (bar, line, pie, scatter, flowchart, architecture diagram, etc.)
2. Title and axis labels, or what the boxes and arrows represent
3. All data points or values visible (be precise with numbers)
4. Key trends, patterns, relationships or comparisons shown
5. Any legends or annotations

Be precise with numbers — if a bar shows 42.3%, report 42.3%, not "about 40%".
If you cannot read a value clearly, say so rather than guessing."""

    try:
        return await router.vision(
            "chart_analysis",
            image_data,
            prompt,
            mime_type=mime_type,
            max_tokens=2048,
        )
    except Exception as e:
        logger.warning("Chart analysis failed: %s", e)
        return ""


async def _analyze_image(
    image_data: bytes,
    mime_type: str,
    router: ProviderRouter,
) -> str:
    """Analyze a general image — describe content, context, relevance."""
    prompt = """Describe this image in the context of a document.

Answer in exactly this format, with these two labels:

VISIBLE TEXT:
Every word of text that appears in the image, verbatim, one line per text element.
Write "none" if the image contains no text.

MEANING:
What the image shows (objects, people, scenes, diagrams), what kind of image it is
(diagram, photo, logo, screenshot), and any important details that would help
someone understand the document without seeing this image.

Be factual and concise. Do not speculate beyond what is visible."""

    try:
        return await router.vision(
            "image_understanding",
            image_data,
            prompt,
            mime_type=mime_type,
            max_tokens=1024,
        )
    except Exception as e:
        logger.warning("Image analysis failed: %s", e)
        return ""


_NO_TEXT_MARKERS = frozenset({"", "none", "n/a", "na", "-", "no text", "no visible text", "none."})
_LABEL_VISIBLE = re.compile(r"(?im)^[\W_]*VISIBLE\s+TEXT[\W_]*?:[\W_]*")
_LABEL_MEANING = re.compile(r"(?im)^[\W_]*MEANING[\W_]*?:[\W_]*")


def _split_image_analysis(raw: str) -> tuple[str, str]:
    """Split a vision answer into (ocr_text, meaning).

    Tolerant by design: if the model ignored the requested format, the whole
    answer is kept as the meaning and ocr_text stays empty — nothing is lost.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    visible = _LABEL_VISIBLE.search(raw)
    meaning = _LABEL_MEANING.search(raw)
    if not meaning:
        return "", raw
    meaning_text = raw[meaning.end():].strip()
    ocr_text = ""
    if visible and visible.end() <= meaning.start():
        ocr_text = raw[visible.end():meaning.start()].strip()
    elif visible is None:
        # Only a MEANING label: anything before it is preamble, drop nothing important.
        pass
    if ocr_text.strip().lower() in _NO_TEXT_MARKERS:
        ocr_text = ""
    return ocr_text, meaning_text or raw


def _save_figure_image(
    file_path: str, page_number: int, figure_index: int, image_data: bytes, ext: str
) -> str:
    """Save an extracted figure under data/processed/figures and return its path.

    The returned path is relative to the project root when possible. Best-effort:
    any failure returns "" and never blocks ingestion.
    """
    if not settings.save_figure_images:
        return ""
    try:
        safe_ext = re.sub(r"[^a-z0-9]", "", str(ext).lower())[:5] or "png"
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(file_path).stem)[:40] or "doc"
        digest = hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:10]
        folder = settings.processed_dir / "figures" / f"{stem}_{digest}"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"p{page_number:04d}_f{figure_index:02d}.{safe_ext}"
        target.write_bytes(image_data)
        try:
            return target.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return target.resolve().as_posix()
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not save figure image: %s", e)
        return ""
