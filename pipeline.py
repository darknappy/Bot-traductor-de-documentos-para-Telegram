"""Orquestación de las tareas: elige el traductor según el formato y compone el resultado.

Además de traducir, aquí se gestiona:

* la **caché** de resultados (si alguien reenvía el mismo documento ya traducido
  se lo devuelve al instante);
* el **progreso persistente** por documento para reanudar tareas enormes que se
  hayan quedado a medias (reinicios del servidor, cancelaciones, cortes...).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import config
import languages
from checkpoints import JobStore, file_digest
from epub_translate import translate_epub
from errors import JobCancelledError, UnsupportedFormatError
from pdf_translate import translate_pdf
from splitter import SplitPart, SplitSpec, split_document
from supabase_store import get_backend
from translator import BaseTranslator, create_translator

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], Awaitable[None]]

_UNSAFE_NAME_RE = re.compile(r"[^\w\s.,()\[\]-]", re.UNICODE)

_STORE: JobStore | None = None


@dataclass
class DocumentTask:
    """Un documento recibido pendiente de traducir o dividir en secciones."""

    task_id: str
    user_id: int
    chat_id: int
    status_message_id: int
    src_message_id: int | None
    src_path: Path
    file_name: str
    lang: str
    engine: str = "auto"  # motor elegido por el usuario (auto/google/microsoft/deepl)
    mode: str = "translate"  # "translate" | "split" | "both"
    split_spec: SplitSpec | None = None
    size: int = 0
    file_id: str | None = None
    input_key: str | None = None  # ruta del original en Supabase Storage
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = 0.0
    last_db_update: float = 0.0

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


@dataclass
class JobResult:
    """Resultado listo para enviar por Telegram."""

    out_path: Path
    out_name: str
    caption: str
    key: str | None = None


@dataclass
class SplitOutcome:
    """Resultado de dividir un documento: varias secciones y un resumen."""

    parts: list[SplitPart]
    summary: str


# --------------------------------------------------------------------------- utilidades


def get_store() -> JobStore:
    """Almacén de progreso y caché (singleton, con Supabase si está activo)."""
    global _STORE
    if _STORE is None:
        _STORE = JobStore(config.JOBS_DIR, config.CACHE_DIR, remote=get_backend())
    return _STORE


def safe_filename(name: str, max_len: int = 120) -> str:
    """Nombre de archivo seguro para Windows/Linux/Telegram."""
    name = Path(name or "documento").name
    name = _UNSAFE_NAME_RE.sub("_", name).strip(" ._") or "documento"
    if len(name) > max_len:
        suffix = Path(name).suffix
        name = name[: max_len - len(suffix)] + suffix
    return name


def output_name_for(file_name: str, lang: str) -> str:
    """Nombre del archivo traducido (mantiene la extensión original)."""
    suffix = Path(file_name).suffix.lower()
    return safe_filename(f"{Path(file_name).stem} [{lang.upper()}]{suffix}")


def _decode_text(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig", "replace")
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", "replace")


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def format_number(value: int) -> str:
    return f"{value:,}".replace(",", ".")


# --------------------------------------------------------------------------- traducción de texto plano


def _read_progress_lines(path: Path) -> dict[int, str]:
    """Lee las líneas ya traducidas de un archivo JSONL (tolerante a cortes)."""
    done: dict[int, str] = {}
    if not path.exists():
        return done
    try:
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                index = int(item["i"])
                text = str(item["t"])
            except (ValueError, KeyError, TypeError):
                continue  # línea incompleta por un corte: se ignora
            done[index] = text
    except OSError:
        logger.warning("No se pudo leer el progreso de %s", path, exc_info=True)
    return done


async def _translate_text_file(
    task: DocumentTask,
    translator: BaseTranslator,
    progress: ProgressCallback,
    store: JobStore | None = None,
    key: str | None = None,
) -> dict:
    text = _decode_text(task.src_path.read_bytes())
    lines = text.split("\n")
    targets = [i for i, line in enumerate(lines) if line.strip() and any(c.isalpha() for c in line)]
    total = max(1, len(targets))

    done_map: dict[int, str] = {}
    if store is not None and key:
        meta = await asyncio.to_thread(store.load_meta, key)
        valid = meta.get("kind") == "txt" and meta.get("total") == len(targets) and meta.get("lang") == translator.target_lang
        if valid:
            done_map = await asyncio.to_thread(store.read_text_progress, key)
            if done_map:
                logger.info("Reanudando texto: %d/%d párrafos ya traducidos", len(done_map), len(targets))
        else:
            await asyncio.to_thread(store.clear_job, key)
            await asyncio.to_thread(
                store.save_meta,
                key,
                kind="txt",
                total=len(targets),
                lang=translator.target_lang,
                lines_done=0,
            )

    for index, translated in done_map.items():
        if 0 <= index < len(lines):
            lines[index] = translated

    pending = [index for index in targets if index not in done_map]
    done = len(done_map)
    stats = {"lines": len(targets), "chars": 0, "resumed": len(done_map)}

    batch_size = 32
    for start in range(0, len(pending), batch_size):
        if task.cancelled():
            raise JobCancelledError()
        indexes = pending[start:start + batch_size]
        results = await translator.translate_many([lines[i] for i in indexes])
        for index, result in zip(indexes, results):
            lines[index] = result
        if store is not None and key:
            await asyncio.to_thread(
                store.append_text_records,
                key,
                [{"i": index, "t": result} for index, result in zip(indexes, results)],
            )
            await asyncio.to_thread(store.save_meta, key, lines_done=done + len(indexes))
        done += len(indexes)
        await progress(done / total, f"párrafo {done}/{len(targets)}")

    out_path = task.src_path.with_name(output_name_for(task.file_name, task.lang))
    out_path.write_text("\n".join(lines), "utf-8")
    stats["chars"] = sum(len(line) for line in lines)
    return stats


# --------------------------------------------------------------------------- resúmenes


def _summary_pdf(stats: dict) -> str:
    parts = [
        f"📑 {format_number(stats.get('pages', 0))} páginas",
        f"· {format_number(stats.get('chars', 0))} caracteres traducidos",
    ]
    text = " ".join(parts)
    notes = []
    if stats.get("resumed"):
        notes.append(f"🔁 {format_number(stats['resumed'])} páginas reanudadas")
    if stats.get("empty_pages"):
        notes.append(f"{stats['empty_pages']} sin texto")
    if stats.get("shrunk"):
        notes.append(f"{stats['shrunk']} bloques con fuente reducida para encajar")
    if stats.get("failed"):
        notes.append(f"⚠️ {stats['failed']} bloques no se pudieron colocar")
    if notes:
        text += "\nℹ️ " + " · ".join(notes)
    return text


def _summary_epub(stats: dict) -> str:
    text = (
        f"📚 {format_number(stats.get('translated', 0))} de {format_number(stats.get('documents', 0))} "
        f"documentos traducidos · {format_number(stats.get('units', 0))} fragmentos"
    )
    if stats.get("resumed"):
        text += f"\n🔁 {format_number(stats['resumed'])} documentos reanudados"
    return text


def _summary_text(stats: dict) -> str:
    text = f"📝 {format_number(stats.get('lines', 0))} párrafos · {format_number(stats.get('chars', 0))} caracteres"
    if stats.get("resumed"):
        text += f"\n🔁 {format_number(stats['resumed'])} párrafos reanudados"
    return text


def _build_caption(
    task: DocumentTask,
    out_name: str,
    translator: BaseTranslator,
    summary: str,
    elapsed: float,
) -> str:
    target = languages.language_name(task.lang)
    if translator.detected_lang:
        source = languages.language_name(translator.detected_lang)
        line = f"🌐 {html.escape(source)} → {html.escape(target)}"
    else:
        line = f"🌐 → {html.escape(target)}"
    lines = [
        f"✅ <b>{html.escape(out_name)}</b>",
        line,
        summary,
        f"⏱️ {format_duration(elapsed)}",
    ]
    if translator.detected_lang and translator.detected_lang.split("-")[0] == task.lang.split("-")[0]:
        lines.append("⚠️ El documento parecía estar ya en el idioma de destino.")
    return "\n".join(lines)


def _cached_caption(task: DocumentTask, out_name: str) -> str:
    return "\n".join(
        [
            f"✅ <b>{html.escape(out_name)}</b>",
            f"🌐 → {html.escape(languages.language_name(task.lang))}",
            "⚡ Ya había traducido este mismo documento: te lo envío desde la caché al instante.",
        ]
    )


# --------------------------------------------------------------------------- flujo principal


async def process_job(task: DocumentTask, progress: ProgressCallback) -> JobResult:
    """Traduce el documento de la tarea y devuelve el archivo resultante."""
    suffix = Path(task.file_name).suffix.lower()
    if suffix not in config.SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"Formato no soportado: {suffix or 'desconocido'}")

    store = get_store()
    upload_name = output_name_for(task.file_name, task.lang)

    # Mismo documento + idioma + usuario (+ motor): se reutiliza lo guardado.
    digest = await asyncio.to_thread(file_digest, task.src_path)
    key = store.key_for(digest, task.lang, task.user_id, task.engine)
    # Si el proceso se reinició, los checkpoints pueden estar en Supabase.
    await asyncio.to_thread(store.restore_job, key)
    cached = await asyncio.to_thread(store.cached_output, key)
    if cached is not None:
        logger.info("Resultado en caché para %s (%s)", task.file_name, key)
        return JobResult(
            out_path=cached,
            out_name=upload_name,
            caption=_cached_caption(task, upload_name),
            key=key,
        )

    upload_path = task.src_path.with_name(upload_name)
    started = time.monotonic()
    stats: dict = {}

    translator = create_translator(task.lang, engine=task.engine)
    async with translator:
        if suffix == ".pdf":
            stats = await translate_pdf(
                task.src_path, upload_path, translator,
                progress=progress, cancel=task.cancelled, store=store, key=key,
            )
            summary = _summary_pdf(stats)
        elif suffix == ".epub":
            stats = await translate_epub(
                task.src_path, upload_path, translator,
                progress=progress, cancel=task.cancelled, store=store, key=key,
            )
            summary = _summary_epub(stats)
        else:
            stats = await _translate_text_file(task, translator, progress, store, key)
            summary = _summary_text(stats)

    elapsed = time.monotonic() - started
    caption = _build_caption(task, upload_name, translator, summary, elapsed)

    # Guardar el resultado en la caché y liberar los archivos intermedios.
    try:
        cached_path = await asyncio.to_thread(store.store_output, key, upload_path, upload_name)
        await asyncio.to_thread(
            store.finish_job,
            key,
            cached_path.name,
            kind=suffix.lstrip("."),
            file_name=task.file_name,
            lang=task.lang,
            user_id=task.user_id,
            stats={k: v for k, v in stats.items() if isinstance(v, (int, float, str))},
        )
    except OSError:
        logger.warning("No se pudo guardar el resultado en la caché", exc_info=True)
        cached_path = upload_path

    return JobResult(out_path=cached_path, out_name=upload_name, caption=caption, key=key)


# --------------------------------------------------------------------------- división en secciones


async def process_split_job(task: DocumentTask, progress: ProgressCallback) -> SplitOutcome:
    """Parte el documento en secciones sin traducir (/dividir y modo «ambos»)."""
    await progress(0.05, "detectando capítulos…")
    report = await asyncio.to_thread(
        split_document,
        task.src_path,
        task.file_name,
        task.src_path.parent,
        task.split_spec,
        task.cancelled,
    )
    count = len(report.parts)

    summary_lines = [
        f"✂️ <b>{html.escape(task.file_name)}</b> dividido en <b>{count} partes</b>",
        "<blockquote>"
        f"📚 {format_number(report.total_units)} {report.unit_noun} · "
        f"{format_number(report.units_per_part)} por sección\n"
        "📤 Te he enviado todas las partes <b>sin traducir</b>."
        "</blockquote>",
        "➡️ Envíame cada parte como un archivo normal para traducirla por separado.",
        "💡 Si una falla o el bot se reinicia, solo reenvías esa parte.",
    ]
    for note in report.notes:
        summary_lines.append(f"ℹ️ {html.escape(note)}.")
    logger.info("División de %s: %d secciones de %d %s", task.file_name, count, report.units_per_part, report.unit_noun)
    return SplitOutcome(parts=list(report.parts), summary="\n".join(summary_lines))
