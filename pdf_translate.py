"""Traducción de PDF conservando el diseño original.

Estrategia: se extrae cada bloque de texto con su posición, tamaño, color y
estilo; se traduce el bloque completo (así el traductor ve la frase entera); se
borra el texto original solo dentro de la caja del bloque (respetando imágenes y
gráficos) y se inserta la traducción en esa misma zona, encogiendo la fuente o
ampliando la caja hacia abajo si la traducción no cabe.
"""
from __future__ import annotations

import asyncio
import html
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

try:  # PyMuPDF >= 1.24 usa el paquete moderno "pymupdf"
    import pymupdf as fitz
except ImportError:  # pragma: no cover - versiones antiguas
    import fitz  # type: ignore

from errors import JobCancelledError, ScannedPdfError

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], Awaitable[None]]
CancelFn = Callable[[], bool]

ALIGN_CSS = {0: "left", 1: "center", 2: "right"}

# Escalas de fuente que se prueban si la traducción no cabe en la caja
# original (1.0 = no reducir). Si ninguna funciona se permite cualquier
# reducción como último recurso.
SCALE_STEPS = (1.0, 0.85, 0.7, 0.55, 0.42, 0.3)

# Si un PDF traduce menos caracteres que este umbral se considera escaneo.
MIN_TEXT_CHARS = 40

# Cada cuántas páginas se guarda un PDF parcial para poder reanudar.
CHECKPOINT_EVERY = 20

_SERIF_HINTS = (
    "times", "georgia", "garamond", "serif", "minion", "bookerly", "charter",
    "cambria", "palatino", "roman", "baskerville", "caslon", "century",
    "constantia", "didot", "freight", "libertine", "garamond",
)
_MONO_HINTS = ("mono", "courier", "consol", "menlo", "code", "typewriter", "fixed")

_METADATA_KEYS = {
    "title", "author", "subject", "keywords", "creator", "producer",
    "creationDate", "modDate", "trapped",
}


@dataclass
class TextBlock:
    """Un bloque de texto de la página con su estilo dominante."""

    rect: "fitz.Rect"
    text: str
    size: float = 11.0
    color: int = 0
    bold: bool = False
    italic: bool = False
    serif: bool = False
    mono: bool = False
    align: int = 0
    line_height: float = 1.2
    lines: list["fitz.Rect"] = field(default_factory=list)


# --------------------------------------------------------------------------- extracción


def _is_horizontal(line: dict) -> bool:
    direction = line.get("dir", (1.0, 0.0))
    return abs(direction[0] - 1.0) < 0.01 and abs(direction[1]) < 0.01


def _join_lines(parts: list[str]) -> str:
    """Une las líneas de un bloque en un solo texto (con des-guionado básico)."""
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("-") and part[:1].islower():
            out = out[:-1] + part
        else:
            out = f"{out} {part}"
    return out


def _weighted_ratio(spans: list[dict], weights: list[int], flag: int) -> float:
    total = float(sum(weights))
    if total <= 0:
        return 0.0
    hit = sum(w for w, span in zip(weights, spans) if int(span.get("flags", 0)) & flag)
    return hit / total


def _dominant_value(spans: list[dict], weights: list[int], key: str, transform=None):
    counts: dict = {}
    for weight, span in zip(weights, spans):
        value = span.get(key, "")
        if transform is not None:
            value = transform(value)
        counts[value] = counts.get(value, 0) + weight
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def extract_blocks(page: "fitz.Page") -> list[TextBlock]:
    """Extrae los bloques de texto traducibles de una página."""
    try:
        data = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT | fitz.TEXT_DEHYPHENATE)
    except Exception:
        data = page.get_text("dict")

    blocks: list[TextBlock] = []
    for raw in data.get("blocks", []):
        if raw.get("type") != 0:
            continue
        lines = [line for line in (raw.get("lines") or []) if _is_horizontal(line)]
        if not lines:
            continue
        text = _join_lines(
            ["".join(span.get("text", "") for span in line.get("spans", [])) for line in lines]
        )
        if not text.strip():
            continue
        spans = [span for line in lines for span in line.get("spans", []) if span.get("text", "").strip()]
        if not spans:
            continue

        weights = [max(1, len(span.get("text", ""))) for span in spans]
        sizes = [float(span.get("size", 0.0)) for span in spans]
        size = statistics.median(sizes) if sizes else 11.0
        if size <= 0:
            size = 11.0

        color = _dominant_value(spans, weights, "color", transform=lambda c: int(c or 0) & 0xFFFFFF) or 0
        font_name = _dominant_value(spans, weights, "font", transform=lambda f: str(f or "").lower()) or ""
        serif_ratio = _weighted_ratio(spans, weights, fitz.TEXT_FONT_SERIFED)
        mono_ratio = _weighted_ratio(spans, weights, fitz.TEXT_FONT_MONOSPACED)

        heights = [
            float(line["bbox"][3]) - float(line["bbox"][1])
            for line in lines
            if float(line["bbox"][3]) > float(line["bbox"][1])
        ]
        line_height = 1.2
        if heights:
            line_height = min(2.0, max(1.0, statistics.median(heights) / size))

        blocks.append(
            TextBlock(
                rect=fitz.Rect(raw["bbox"]),
                text=text,
                size=float(size),
                color=int(color),
                bold=_weighted_ratio(spans, weights, fitz.TEXT_FONT_BOLD) >= 0.6,
                italic=_weighted_ratio(spans, weights, fitz.TEXT_FONT_ITALIC) >= 0.45,
                serif=serif_ratio >= 0.5 or any(hint in font_name for hint in _SERIF_HINTS),
                mono=mono_ratio >= 0.5 or any(hint in font_name for hint in _MONO_HINTS),
                line_height=line_height,
                lines=[fitz.Rect(line["bbox"]) for line in lines],
            )
        )

    _assign_align(blocks)
    return blocks


def _assign_align(blocks: list[TextBlock]) -> None:
    """Adivina la alineación de cada bloque comparándolo con el resto de la página."""
    if not blocks:
        return
    left_margin = min(block.rect.x0 for block in blocks)
    right_margin = max(block.rect.x1 for block in blocks)
    center_x = (left_margin + right_margin) / 2.0

    for block in blocks:
        lines = block.lines
        if len(lines) == 1:
            line = lines[0]
            line_center = (line.x0 + line.x1) / 2.0
            if abs(line_center - center_x) <= 10 and (line.x0 - left_margin) > 14:
                block.align = 1
            else:
                block.align = 0
            continue
        lefts = [line.x0 for line in lines]
        rights = [line.x1 for line in lines]
        centers = [(line.x0 + line.x1) / 2.0 for line in lines]
        if max(lefts) - min(lefts) <= 3:
            block.align = 0
        elif max(rights) - min(rights) <= 3:
            block.align = 2
        elif max(centers) - min(centers) <= 4 and abs(sum(centers) / len(centers) - center_x) <= 24:
            block.align = 1
        else:
            block.align = 0


# --------------------------------------------------------------------------- redacción y escritura


def _expand_rect(rect: "fitz.Rect", others: list["fitz.Rect"], page_rect: "fitz.Rect") -> "fitz.Rect":
    """Amplía la caja hacia abajo si hay hueco libre (hasta el siguiente bloque)."""
    limit = page_rect.y1 - 4.0
    for other in others:
        overlaps_x = other.x1 > rect.x0 + 2 and other.x0 < rect.x1 - 2
        if overlaps_x and other.y0 >= rect.y1 - 1.0:
            limit = min(limit, other.y0 - 1.0)
    extra_cap = min((rect.y1 - rect.y0) * 0.6, 46.0)
    bottom = min(limit, rect.y1 + extra_cap)
    if bottom > rect.y1 + 1.0:
        return fitz.Rect(rect.x0, rect.y0, rect.x1, bottom)
    return rect


def _block_css(block: TextBlock) -> str:
    family = "monospace" if block.mono else ("serif" if block.serif else "sans-serif")
    return (
        "p{margin:0;padding:0;"
        f"font-family:{family};"
        f"font-size:{block.size:.2f}px;"
        f"color:#{block.color & 0xFFFFFF:06x};"
        f"text-align:{ALIGN_CSS.get(block.align, 'left')};"
        f"line-height:{block.line_height:.2f};"
        "}"
    )


def _block_html(block: TextBlock, translated: str) -> str:
    inner = html.escape(translated, quote=False).replace("\n", "<br/>")
    if block.bold and block.italic:
        inner = f"<b><i>{inner}</i></b>"
    elif block.bold:
        inner = f"<b>{inner}</b>"
    elif block.italic:
        inner = f"<i>{inner}</i>"
    return f"<p>{inner}</p>"


def _insert_block(page: "fitz.Page", rect: "fitz.Rect", block: TextBlock, translated: str) -> float | None:
    """Inserta el texto traducido; devuelve la escala usada o None si falló."""
    body = _block_html(block, translated)
    css = _block_css(block)
    for scale_low in SCALE_STEPS:
        try:
            result = page.insert_htmlbox(rect, body, css=css, scale_low=scale_low)
        except Exception as err:  # noqa: BLE001 - PyMuPDF es quisquilloso con algunos estilos
            logger.debug("insert_htmlbox falló (%s); se prueba otra escala", err)
            continue
        spare, scale = result if isinstance(result, tuple) else (result, 1.0)
        if spare >= 0:
            return float(scale)
    # Último recurso: permitir cualquier reducción para no perder el texto.
    try:
        result = page.insert_htmlbox(rect, body, css=css, scale_low=0)
        spare, scale = result if isinstance(result, tuple) else (result, 1.0)
        if spare >= 0:
            return float(scale)
    except Exception as err:  # noqa: BLE001
        logger.warning("No se pudo escribir un bloque traducido: %s", err)
    return None


def _apply_page(page: "fitz.Page", blocks: list[TextBlock], translations: list[str]) -> dict:
    """Borra el texto original de la página y escribe las traducciones."""
    rects = [fitz.Rect(block.rect) for block in blocks]
    for rect in rects:
        page.add_redact_annot(rect, fill=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )

    page_rect = page.rect
    stats = {"shrunk": 0, "failed": 0}
    for index, block in enumerate(blocks):
        translated = translations[index] if index < len(translations) else ""
        if not translated or not translated.strip():
            continue
        others = [rects[j] for j in range(len(rects)) if j != index]
        rect = _expand_rect(rects[index], others, page_rect)
        scale = _insert_block(page, rect, block, translated)
        if scale is None:
            stats["failed"] += 1
        elif scale < 0.55:
            stats["shrunk"] += 1
    return stats


# --------------------------------------------------------------------------- reanudación


def _partial_name(index: int) -> str:
    return f"parcial_{index + 1:05d}.pdf"


def _open_checkpoint(store, key: str, target_lang: str):
    """Abre el PDF parcial de una ejecución anterior, si es utilizable."""
    if store is None or not key:
        return None, 0, {}, 0
    meta = store.load_meta(key)
    if meta.get("kind") != "pdf" or meta.get("lang") != target_lang:
        store.clear_job(key)
        return None, 0, {}, 0
    name = meta.get("partial")
    pages_done = int(meta.get("pages_done", 0) or 0)
    chars = int(meta.get("chars", 0) or 0)
    if not name or pages_done <= 0:
        return None, 0, meta, chars
    path = store.job_dir(key) / str(name)
    if not path.exists():
        return None, 0, meta, chars
    try:
        doc = fitz.open(str(path))
    except Exception:  # noqa: BLE001 - un parcial corrupto se descarta
        logger.warning("El PDF parcial no se pudo abrir; se empieza de cero", exc_info=True)
        return None, 0, meta, chars
    total = int(meta.get("total", 0) or 0)
    if total and doc.page_count != total:
        doc.close()
        return None, 0, meta, chars
    pages_done = min(pages_done, doc.page_count)
    logger.info("Reanudando PDF: %d/%d páginas ya traducidas", pages_done, doc.page_count)
    return doc, pages_done, meta, chars


# --------------------------------------------------------------------------- flujo principal


def _update_metadata(doc: "fitz.Document", target_lang: str) -> None:
    metadata = doc.metadata or {}
    clean = {key: str(value or "") for key, value in metadata.items() if key in _METADATA_KEYS}
    title = clean.get("title", "").strip()
    if title:
        clean["title"] = f"{title} ({target_lang.upper()})"
    clean["producer"] = "Bot traductor - Google web"
    try:
        doc.set_metadata(clean)
    except Exception:  # noqa: BLE001 - los metadatos no son críticos
        logger.debug("No se pudieron actualizar los metadatos", exc_info=True)


async def translate_pdf(
    src_path: Path,
    dst_path: Path,
    translator,
    *,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
    store=None,
    key: str | None = None,
) -> dict:
    """Traduce un PDF completo conservando el diseño. Devuelve estadísticas.

    Si se pasan ``store`` y ``key`` se guarda un PDF parcial cada
    ``CHECKPOINT_EVERY`` páginas y se reanuda automáticamente si una ejecución
    anterior se quedó a medias.
    """
    loop = asyncio.get_running_loop()
    stats = {"pages": 0, "with_text": 0, "empty_pages": 0, "chars": 0, "shrunk": 0, "failed": 0, "resumed": 0}

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf") as pool:

        async def run(fn, *args):
            return await loop.run_in_executor(pool, fn, *args)

        doc, start_page, _meta, previous_chars = await run(_open_checkpoint, store, key or "", translator.target_lang)
        reopened = doc is not None
        if not reopened:
            doc = await run(fitz.open, str(src_path))
        stats["chars"] = previous_chars
        stats["resumed"] = start_page

        previous_partial: Path | None = None
        if store is not None and key:
            meta = store.load_meta(key)
            name = meta.get("partial")
            if reopened and name:
                previous_partial = store.job_dir(key) / str(name)

        async def checkpoint(index: int) -> None:
            """Guarda un PDF parcial (solo si hay dónde reanudar)."""
            nonlocal previous_partial
            if store is None or not key:
                return
            path = store.job_dir(key) / _partial_name(index)
            try:
                await run(lambda: doc.save(str(path), garbage=0, deflate=False, clean=False))
            except Exception as err:  # noqa: BLE001 - sin punto de control se puede seguir
                logger.warning("No se pudo guardar el PDF parcial: %s", err)
                return
            store.save_meta(
                key,
                kind="pdf",
                lang=translator.target_lang,
                total=doc.page_count,
                pages_done=index + 1,
                chars=stats["chars"],
                partial=path.name,
            )
            if previous_partial is not None and previous_partial != path:
                try:  # en Linux se puede borrar aunque el documento siga abierto
                    previous_partial.unlink(missing_ok=True)
                except OSError:
                    pass  # en Windows fallará; se limpia al terminar la tarea
            previous_partial = path

        try:
            total = doc.page_count
            stats["pages"] = total
            if start_page and progress:
                await progress(start_page / max(1, total), f"página {start_page}/{total} (reanudado)")

            for index in range(start_page, total):
                if cancel and cancel():
                    raise JobCancelledError()
                page = await run(doc.load_page, index)
                blocks = await run(extract_blocks, page)
                if not blocks:
                    stats["empty_pages"] += 1
                    if progress:
                        await progress((index + 1) / total, f"página {index + 1}/{total} (sin texto)")
                    if (index + 1) % CHECKPOINT_EVERY == 0:
                        await checkpoint(index)
                    continue

                texts = [block.text for block in blocks]
                if translator.detected_lang is None:
                    sample = " ".join(texts)[:600]
                    if sample.strip():
                        try:
                            await translator.detect(sample)
                        except Exception as err:  # noqa: BLE001 - la detección es opcional
                            logger.debug("Detección de idioma falló: %s", err)

                translations = await translator.translate_many(texts)
                if cancel and cancel():
                    raise JobCancelledError()

                page_stats = await run(_apply_page, page, blocks, translations)
                stats["with_text"] += 1
                stats["chars"] += sum(len(t) for t in translations)
                stats["shrunk"] += page_stats.get("shrunk", 0)
                stats["failed"] += page_stats.get("failed", 0)
                if progress:
                    await progress((index + 1) / total, f"página {index + 1}/{total}")
                if (index + 1) % CHECKPOINT_EVERY == 0:
                    await checkpoint(index)

            if stats["chars"] < MIN_TEXT_CHARS:
                raise ScannedPdfError(
                    "El PDF no contiene texto extraíble (parece un escaneo). "
                    "Este bot no hace OCR."
                )
            if cancel and cancel():
                raise JobCancelledError()

            await run(_update_metadata, doc, translator.target_lang)
            await run(lambda: doc.save(str(dst_path), garbage=3, deflate=True))
        finally:
            await run(doc.close)

    return stats
