"""Bot de Telegram que traduce documentos (PDF, EPUB, TXT, MD) con Google, Microsoft y DeepL.

Incluye un menú fijo de botones, un divisor de novelas (/dividir) para trabajar por
secciones en libros enormes, el modo «dividir y traducir» (secciones + cada una
traducida por separado) y la elección de motor de traducción por usuario.

Uso:
    1. Copia .env.example a .env y pega el token de @BotFather.
    2. python bot.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
import languages
import users
from errors import (
    InputUnavailableError,
    JobCancelledError,
    ScannedPdfError,
    SplitError,
    UnsupportedFormatError,
)
from health import start_health_server
from pipeline import (
    DocumentTask,
    format_duration,
    get_store,
    process_job,
    process_split_job,
    safe_filename,
)
from checkpoints import file_digest
from splitter import SplitPart, SplitSpec, describe_spec, parse_split_command
from supabase_store import content_type_for, get_backend
from translator import (
    TranslationError,
    active_engine,
    create_translator,
    deepl_supported,
    engine_label,
    normalize_engine,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("bot")

MAX_TEXT_MESSAGE = 3500
STARTED_AT = time.time()
_QUEUE_KEY = "queue"
_ACTIVE_KEY = "active"
_PENDING_KEY = "pending"
_TASKS_KEY = "tasks"
_WORKERS_KEY = "workers"
_LOCK_KEY = "lock"
_PENDING_FILES_KEY = "pending_files"
# Referencia global a la aplicación (solo para el estado de /status y Render).
_bot_data_holder: dict = {}

# --- Menú fijo (teclado bajo el campo de texto) ---------------------------------
MENU_TRANSLATE = "🌐 Traducir"
MENU_SPLIT = "✂️ Dividir"
MENU_BOTH = "🔀 Dividir y traducir"
MENU_LANG = "⚙️ Idioma"
MENU_ENGINE = "🧠 Motor"
MENU_STATUS = "📊 Estado"
MENU_CANCEL = "🚫 Cancelar"
MENU_HELP = "❓ Ayuda"
MENU_LABELS = (
    MENU_TRANSLATE,
    MENU_SPLIT,
    MENU_BOTH,
    MENU_LANG,
    MENU_ENGINE,
    MENU_STATUS,
    MENU_CANCEL,
    MENU_HELP,
)

_MODE_EMOJI = {"translate": "🔄", "split": "✂️", "both": "🔀"}
_MODE_HEADERS = {
    "translate": "🔄 <b>Traduciendo…</b>",
    "split": "✂️ <b>Dividiendo en secciones…</b>",
    "both": "🔀 <b>Dividiendo y traduciendo…</b>",
}
_MODE_NAMES = {"translate": "traducción", "split": "división", "both": "dividir y traducir"}
_ACTION_MODES = {"t": "translate", "d": "split", "b": "both"}
# Tamaños de sección que ofrece el selector de «dividir y traducir»: nada automático.
_CHAPTER_CHOICES = (25, 50, 75, 100)
_CAPTION_MODES = {
    "traducir": "translate",
    "translate": "translate",
    "ambos": "both",
    "both": "both",
}
_MENTION_RE = re.compile(r"^([/!][A-Za-z]+)@[A-Za-z0-9_]+")

# Bot API 9.4+: los botones pueden llevar color («primary», «success», «danger»).
# Si el servidor no los aceptara, se desactiva solo y el mensaje se reenvía sin color.
_BUTTON_STYLES = True


# --------------------------------------------------------------------------- estado


def _status_snapshot() -> dict:
    """Estado del bot para /status y para el servidor de salud de Render."""
    backend = get_backend()
    supabase_ready = bool(backend is not None and backend.ready)
    snapshot: dict = {
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "queue": 0,
        "active": [],
        "queued": [],
        "cache_mb": 0.0,
        "cache_files": 0,
        "translator": config.TRANSLATOR_BACKEND,
        "supabase": supabase_ready,
        "supabase_configured": backend is not None,
    }
    try:
        app = _bot_data_holder.get("app")
        data = getattr(app, "bot_data", None) or {}
        queue = data.get(_QUEUE_KEY)
        snapshot["queue"] = queue.qsize() if queue is not None else 0
        active_tasks = list((data.get(_ACTIVE_KEY) or {}).values())
        for task in active_tasks:
            snapshot["active"].append(
                {
                    "file": task.file_name,
                    "user": task.user_id,
                    "lang": task.lang,
                    "mode": task.mode,
                    "elapsed": round(time.monotonic() - task.started_at, 1) if task.started_at else 0,
                    "progress": round(min(1.0, max(0.0, task.last_fraction)) * 100),
                    "note": (task.last_note or "")[:80],
                    "idle": round(time.monotonic() - task.last_progress_at, 1)
                    if task.last_progress_at
                    else None,
                }
            )
        active_ids = {task.task_id for task in active_tasks}
        for task in (data.get(_TASKS_KEY) or {}).values():
            if task.task_id in active_ids or task.cancelled():
                continue
            snapshot["queued"].append(
                {
                    "file": task.file_name,
                    "user": task.user_id,
                    "lang": task.lang,
                    "mode": task.mode,
                }
            )
    except Exception:  # noqa: BLE001 - el estado nunca debe romper nada
        logger.debug("No se pudo componer el estado", exc_info=True)
    try:
        files = [path for path in config.CACHE_DIR.glob("*") if path.is_file()]
        snapshot["cache_files"] = len(files)
        snapshot["cache_mb"] = round(sum(path.stat().st_size for path in files) / 1048576, 1)
    except OSError:
        pass
    return snapshot


# --------------------------------------------------------------------------- botones


def _style(color: str) -> dict:
    """api_kwargs para colorear un botón (Bot API 9.4+)."""
    return {"style": color} if _BUTTON_STYLES else {}


def _plain_markup(markup):
    """Copia sin colores de un teclado (para reintentar si Telegram los rechaza)."""
    if isinstance(markup, InlineKeyboardMarkup):
        rows = []
        for row in markup.inline_keyboard:
            buttons = []
            for button in row:
                if button.url:
                    buttons.append(InlineKeyboardButton(button.text, url=button.url))
                elif button.callback_data is not None:
                    buttons.append(InlineKeyboardButton(button.text, callback_data=button.callback_data))
                else:
                    buttons.append(InlineKeyboardButton(button.text, callback_data="noop"))
            rows.append(buttons)
        return InlineKeyboardMarkup(rows)
    if isinstance(markup, ReplyKeyboardMarkup):
        rows = [[KeyboardButton(button.text) for button in row] for row in markup.keyboard]
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=markup.resize_keyboard,
            one_time_keyboard=markup.one_time_keyboard,
            is_persistent=markup.is_persistent,
            input_field_placeholder=markup.input_field_placeholder,
        )
    return markup


async def _send_html(send, text: str, markup=None):
    """Envía (o edita) un mensaje HTML; si Telegram rechaza los colores, reintenta sin ellos."""
    global _BUTTON_STYLES
    try:
        return await send(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as err:
        if not _BUTTON_STYLES or "style" not in str(err).lower():
            raise
        _BUTTON_STYLES = False
        logger.warning("Telegram rechazó los estilos de botón; se usarán normales (%s)", err)
        return await send(text, parse_mode=ParseMode.HTML, reply_markup=_plain_markup(markup))


def main_keyboard() -> ReplyKeyboardMarkup:
    """Menú fijo bajo el campo de texto: siempre a la vista."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(MENU_TRANSLATE, api_kwargs=_style("primary")),
                KeyboardButton(MENU_SPLIT, api_kwargs=_style("success")),
            ],
            [KeyboardButton(MENU_BOTH, api_kwargs=_style("primary"))],
            [
                KeyboardButton(MENU_LANG),
                KeyboardButton(MENU_ENGINE),
                KeyboardButton(MENU_STATUS),
            ],
            [
                KeyboardButton(MENU_CANCEL, api_kwargs=_style("danger")),
                KeyboardButton(MENU_HELP),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Envía un archivo o toca un botón…",
    )


def _mode_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Desactivar modo", callback_data="mode:off", api_kwargs=_style("danger"))]]
    )


def _file_action_keyboard(token: str, engine: str = "auto") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌐 Traducir", callback_data=f"fx:{token}:t", api_kwargs=_style("primary")),
                InlineKeyboardButton("✂️ Dividir", callback_data=f"fx:{token}:d", api_kwargs=_style("success")),
            ],
            [
                InlineKeyboardButton(
                    "🔀 Dividir y traducir cada parte",
                    callback_data=f"fx:{token}:b",
                    api_kwargs=_style("primary"),
                )
            ],
            [
                InlineKeyboardButton(
                    f"🧠 Motor: {engine_label(engine)}",
                    callback_data=f"fx:{token}:e",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Descartar", callback_data=f"fx:{token}:x", api_kwargs=_style("danger")
                )
            ],
        ]
    )


def _status_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Actualizar", callback_data="status:refresh", api_kwargs=_style("primary"))]]
    )


def _chapters_markup(token: str | None = None) -> InlineKeyboardMarkup:
    """Selector de capítulos por sección para «dividir y traducir».

    Con ``token`` (archivo ya recibido) los botones empiezan por ``fbc:`` y
    llevan «Volver»; sin él (menú o /ambos) empiezan por ``mbc:``.
    """
    prefix = f"fbc:{token}" if token else "mbc"
    rows = [
        [
            InlineKeyboardButton(
                f"{count} capítulos",
                callback_data=f"{prefix}:{count}",
                api_kwargs=_style("primary"),
            )
            for count in _CHAPTER_CHOICES[start : start + 2]
        ]
        for start in (0, 2)
    ]
    if token:
        rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"fx:{token}:v")])
    return InlineKeyboardMarkup(rows)


def _chapters_text(file_name: str | None = None) -> str:
    lead = f"📄 <b>{html.escape(file_name)}</b>\n" if file_name else ""
    return (
        lead
        + "📐 <b>¿Cuántos capítulos por sección?</b>\n"
        "<blockquote>🔀 «Dividir y traducir» usará ese tamaño para cada parte "
        "(en PDF se cuentan páginas).\n"
        "• Con menos capítulos, cada sección es más pequeña y más fácil de reenviar "
        "si algo falla.</blockquote>"
    )


# --- Motor de traducción (selector por usuario) ---------------------------------

# Opciones del selector: código, nombre del botón y explicación corta.
_ENGINE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("auto", "🔄 Automático", "Google primero; si se bloquea, continúa Microsoft o DeepL"),
    ("google", "🔵 Solo Google", "El más rápido y estable; sin relevo"),
    ("microsoft", "🟣 Solo Microsoft", "El relevo clásico cuando Google falla"),
    ("deepl", "🟢 Solo DeepL", "La mejor calidad; traduce más despacio"),
)
_ENGINE_SHORT = {code: label for code, label, _desc in _ENGINE_CHOICES}


def _engine_markup(current: str, *, token: str | None = None, lang: str | None = None) -> InlineKeyboardMarkup:
    """Botones del selector de motor (en el menú o para un archivo pendiente).

    Con ``token`` los botones empiezan por ``fe:`` y llevan «Volver» a la
    pregunta del archivo; sin él (menú o /motor) empiezan por ``eng:``.
    """
    code = normalize_engine(current) or "auto"
    prefix = f"fe:{token}" if token else "eng"
    rows = []
    for choice, label, _desc in _ENGINE_CHOICES:
        if choice == "deepl" and lang is not None and not deepl_supported(lang):
            continue  # DeepL no soporta ese idioma: mejor no ofrecerlo
        mark = "✅ " if choice == code else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{label}",
                    callback_data=f"{prefix}:{choice}",
                    api_kwargs=_style("success" if choice == code else "primary"),
                )
            ]
        )
    if token:
        rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"fx:{token}:v")])
    return InlineKeyboardMarkup(rows)


def _engine_menu_text(current: str, lang: str) -> str:
    code = normalize_engine(current) or "auto"
    lines = [
        "🧠 <b>Motor de traducción</b>",
        f"<blockquote>Ahora mismo: <b>{html.escape(engine_label(code))}</b> · "
        f"🌐 → {html.escape(languages.language_name(lang))}</blockquote>",
    ]
    for choice, label, desc in _ENGINE_CHOICES:
        if choice == "deepl" and not deepl_supported(lang):
            continue
        mark = "✅" if choice == code else "▪️"
        lines.append(f"{mark} {label} — {html.escape(desc)}")
    if not deepl_supported(lang):
        lines.append("ℹ️ DeepL no traduce a este idioma, así que no aparece.")
    lines.append(
        "<blockquote>💡 Afecta a tus próximas traducciones; las tareas en curso no "
        "cambian. También vale <code>/motor google</code>, <code>/motor deepl</code>…</blockquote>"
    )
    return "\n".join(lines)


def _fix_engine_for_lang(user_id: int, lang: str) -> str | None:
    """Si el motor elegido no traduce al idioma nuevo, vuelve a Automático.

    Devuelve una nota para el mensaje, o ``None`` si no hubo que tocar nada.
    """
    if users.get_engine(user_id) == "deepl" and not deepl_supported(lang):
        users.set_engine(user_id, "auto")
        return "motor a Automático (DeepL no traduce a ese idioma)"
    return None


# --------------------------------------------------------------------------- mensajes


def help_text(lang: str) -> str:
    return (
        "🌍 <b>Bot traductor de documentos</b>\n"
        "Traduzco <b>PDF · EPUB · TXT · MD</b> y mensajes sueltos con los traductores\n"
        "gratuitos de <b>Google</b>, <b>Microsoft</b> y <b>DeepL</b>.\n\n"
        "<blockquote>🌐 <b>Traducir</b> — conserva el diseño del PDF y los capítulos, "
        "estilos y enlaces del EPUB.\n"
        "✂️ <b>Dividir</b> — parte una novela larga en secciones <i>sin traducir</i> "
        "(ideal para +100 capítulos).\n"
        "🔀 <b>Dividir y traducir</b> — elige 25/50/75/100 capítulos por sección: te "
        "manda las secciones y después traduce cada una.\n"
        "🧠 <b>Motor</b> — decide quién traduce: automático (Google primero y relevo), "
        "solo Google, solo Microsoft o solo DeepL.</blockquote>\n\n"
        f"🌐 Idioma de destino: <b>{html.escape(languages.language_name(lang))}</b> · "
        "cámbialo con ⚙️ <b>Idioma</b>\n\n"
        "<blockquote expandable>💡 <b>Trucos y comandos</b>\n"
        "• Toca un botón del menú y envíame uno o varios archivos seguidos: se procesan "
        "en cola, de uno en uno. El modo sigue activo hasta que lo desactives.\n"
        "• Si no eliges modo, te preguntaré qué hacer con cada archivo.\n"
        "• <code>/dividir 25</code> → 25 capítulos por sección · "
        "<code>/dividir 8 partes</code> → 8 secciones (en PDF se cuentan páginas).\n"
        "• También sirve la leyenda del archivo: <code>/traducir</code>, "
        "<code>/dividir 25</code> o <code>/ambos</code>.\n"
        f"• Tamaño máximo por archivo: {config.MAX_INPUT_MB} MB (límite de Telegram).\n"
        f"• Puedes encolar hasta {config.QUEUE_MAX_FILES} archivos y {config.QUEUE_MAX_MB} MB "
        "en total; se procesan por orden.\n"
        "• Con Supabase configurado, si el servidor se reinicia la cola y el progreso se "
        "recuperan solos: no hace falta reenviar nada.\n"
        "• Mientras traduzco verás con qué motor trabajo (Google, Microsoft o DeepL).\n"
        "• Comandos: /lang · /motor · /dividir · /status · /cancel · /help</blockquote>"
    )


def status_text(preference: str | None = None) -> str:
    """Resumen del estado del bot (se usa en /status)."""
    snapshot = _status_snapshot()
    active = snapshot.get("active") or []
    queued = snapshot.get("queued") or []
    engine = active_engine()
    engine_line = f"\n🛰️ Traduciendo con <b>{html.escape(engine[1])}</b>" if engine else ""
    pref_name = engine_label(preference) if preference else "Automático (por defecto)"
    if snapshot.get("supabase"):
        cloud = "☁️ Supabase activo"
    elif snapshot.get("supabase_configured"):
        cloud = "⚠️ Supabase con problemas · modo local"
    else:
        cloud = "💾 Solo disco local"
    lines = [
        "📊 <b>Estado del bot</b>",
        "<blockquote>"
        f"🟢 Activo desde hace {format_duration(snapshot.get('uptime_seconds', 0))}\n"
        f"⚙️ Servidor: <code>{html.escape(str(config.TRANSLATOR_BACKEND))}</code> · "
        f"{config.TRANSLATE_CONCURRENCY} traducciones en paralelo\n"
        f"🧠 Tu motor: <b>{html.escape(pref_name)}</b> · {cloud}\n"
        f"📥 En cola: {snapshot.get('queue', 0)} · 🧵 En proceso: {len(active)}"
        f"{engine_line}\n"
        f"💾 Resultados guardados: {snapshot.get('cache_files', 0)} archivo(s) · "
        f"{snapshot.get('cache_mb', 0):.0f} MB"
        "</blockquote>",
    ]
    for task in active:
        emoji = _MODE_EMOJI.get(str(task.get("mode", "translate")), "🔄")
        detail = ""
        note = str(task.get("note") or "").strip()
        if note:
            detail = f" · {int(task.get('progress', 0))}% · {html.escape(note)}"
        idle = task.get("idle")
        if isinstance(idle, (int, float)) and idle >= _HEARTBEAT_SECONDS:
            detail += f" · última señal hace {format_duration(idle)}"
        lines.append(
            f"{emoji} <code>{html.escape(str(task.get('file', '?')))}</code> · "
            f"{format_duration(task.get('elapsed', 0))}{detail}"
        )
    for task in queued[:5]:
        emoji = _MODE_EMOJI.get(str(task.get("mode", "translate")), "🔄")
        lines.append(f"🕒 <code>{html.escape(str(task.get('file', '?')))}</code> · en espera {emoji}")
    if len(queued) > 5:
        lines.append(f"… y {len(queued) - 5} archivo(s) más en cola.")
    if not active and not queued:
        lines.append("💤 No hay tareas en curso ahora mismo.")
    return "\n".join(lines)


def _eta_text(fraction: float, elapsed: float) -> str | None:
    """Estimación del tiempo que queda (solo cuando hay datos suficientes)."""
    if fraction < 0.08 or elapsed < 20:
        return None
    remaining = elapsed * (1.0 - fraction) / fraction
    if remaining < 30:
        return None
    return format_duration(remaining)


def _progress_text(task: DocumentTask, fraction: float, note: str, elapsed: float) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * 10))
    bar = "▓" * filled + "░" * (10 - filled)
    header = _MODE_HEADERS.get(task.mode, _MODE_HEADERS["translate"])
    lines = [header, f"📄 <code>{html.escape(task.file_name)}</code>"]
    if task.mode != "split":
        lang_line = f"🌐 → {html.escape(languages.language_name(task.lang))}"
        engine = active_engine()
        if engine:
            lang_line += f" · ⚙️ <b>{html.escape(engine[1])}</b>"
        lines.append(lang_line)
    lines.append(f"{bar} <b>{int(round(fraction * 100))}%</b> · {html.escape(note)}")
    time_line = f"⏱️ {format_duration(elapsed)}"
    eta = _eta_text(fraction, elapsed)
    if eta:
        time_line += f" · ⏳ quedan ~{eta}"
    lines.append(time_line)
    return "\n".join(lines)


def _kind_label(file_name: str) -> str:
    return {
        ".pdf": "📕 PDF",
        ".epub": "📗 EPUB",
        ".txt": "📄 TXT",
        ".md": "📝 Markdown",
    }.get(Path(file_name).suffix.lower(), "📁 Archivo")


def _task_header(file_name: str, size: int, mode: str, lang: str, engine: str = "auto") -> str:
    if mode == "split":
        action = "✂️ Se partirá en secciones sin traducir"
    elif mode == "both":
        action = (
            "🔀 Se dividirá y se traducirá cada parte · "
            f"🧠 {html.escape(engine_label(engine))}"
        )
    else:
        action = (
            f"🌐 → {html.escape(languages.language_name(lang))} · "
            f"🧠 {html.escape(engine_label(engine))}"
        )
    return (
        f"📥 <b>{html.escape(file_name)}</b>\n"
        f"<blockquote>📦 {size / 1048576:.1f} MB · {_kind_label(file_name)}\n{action}</blockquote>"
    )


def _file_question_text(file_name: str, size: int, lang: str, engine: str = "auto") -> str:
    return (
        f"📄 <b>{html.escape(file_name)}</b>\n"
        f"<blockquote>📦 {size / 1048576:.1f} MB · {_kind_label(file_name)} · "
        f"🌐 → {html.escape(languages.language_name(lang))} · "
        f"🧠 {html.escape(engine_label(engine))}</blockquote>\n"
        "❓ <b>¿Qué quieres que haga con este archivo?</b>\n"
        "<i>No haré nada hasta que elijas.</i>"
    )


def _part_caption(part: SplitPart, status: str = "split") -> str:
    """Pie de foto de una sección (sin traducir, en el modo «ambos» o traducida)."""
    base = f"✂️ <b>Parte {part.index} de {part.count}</b> · {html.escape(part.label)}"
    if status == "todo":
        return f"{base}\n📚 Sin traducir"
    if status == "done":
        return f"{base}\n✅ Traducida"
    return base


def _mode_confirmation(mode: str, lang: str, spec: SplitSpec | None, engine: str | None = None) -> str:
    if mode == "split":
        return (
            "✂️ <b>Modo división activado</b>\n"
            "Los archivos que envíes se partirán en secciones <b>sin traducir</b> "
            f"({html.escape(describe_spec(spec))}).\n"
            "<blockquote>💡 Ajusta el tamaño con <code>/dividir 25</code> (capítulos por "
            "parte) o <code>/dividir 8 partes</code>.\n"
            "🚫 El modo sigue activo hasta que pulses «Desactivar modo».</blockquote>"
        )
    if mode == "both":
        return (
            "🔀 <b>Modo «dividir y traducir» activado</b>\n"
            f"Tamaño: {html.escape(describe_spec(spec))}.\n"
            "Te enviaré primero todas las secciones <b>sin traducir</b> y después iré "
            "traduciendo cada una.\n"
            "<blockquote>🧩 Cada sección se traduce por separado: si una falla, las demás "
            "quedan hechas.\n"
            "🚫 El modo sigue activo hasta que pulses «Desactivar modo».</blockquote>"
        )
    motor = f"🧠 Motor: <b>{html.escape(engine_label(engine))}</b>\n" if engine else ""
    return (
        "🌐 <b>Modo traducción activado</b>\n"
        "Los archivos que envíes se traducirán a "
        f"<b>{html.escape(languages.language_name(lang))}</b> conservando su formato.\n"
        f"<blockquote>{motor}"
        "🚫 El modo sigue activo hasta que pulses «Desactivar modo». Si lo desactivas, "
        "al enviarme un archivo te preguntaré qué hacer.</blockquote>"
    )


def _lang_menu_text(current: str) -> str:
    return (
        "🌐 <b>Idioma de destino</b>\n"
        f"<blockquote>Ahora mismo: <b>{html.escape(languages.language_name(current))}</b>\n"
        "Toca otro para cambiarlo o usa <code>/lang código</code> para cualquier otro "
        "(p. ej. <code>/lang pt-BR</code>).</blockquote>"
    )


def _lang_menu_markup(current: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                ("✅ " if code == current else languages.flag(code)) + languages.LANGUAGES[code],
                callback_data=f"lang:{code}",
                api_kwargs=_style("success" if code == current else "primary"),
            )
            for code in row
        ]
        for row in languages.KEYBOARD_ROWS
    ]
    return InlineKeyboardMarkup(keyboard)


def parse_caption_action(caption: str) -> tuple[str, SplitSpec | None] | None:
    """Interpreta el pie de foto: «/dividir [N]», «/traducir» o «/ambos [N]»."""
    text = _MENTION_RE.sub(r"\1", (caption or "").strip())
    if not text:
        return None
    spec = parse_split_command(text)
    if spec is not None:
        return ("split", spec)
    first = text.split()[0].lstrip("/!").lower()
    mode = _CAPTION_MODES.get(first)
    if mode is None:
        return None
    tokens = text.split()
    spec = None
    if mode == "both" and len(tokens) > 1:
        spec = parse_split_command("/dividir " + " ".join(tokens[1:]))
    return (mode, spec)


class ProgressReporter:
    """Edita el mensaje de estado sin superar los límites de Telegram."""

    def __init__(self, bot, chat_id: int, message_id: int, min_interval: float = 3.0) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.min_interval = min_interval
        self._last_text: str | None = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def update(self, text: str, *, force: bool = False) -> None:
        async with self._lock:
            now = time.monotonic()
            if text == self._last_text:
                return
            if not force and now - self._last_edit < self.min_interval:
                return
            self._last_text = text
            self._last_edit = now
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except RetryAfter as err:
            delay = err.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
            await asyncio.sleep(min(delay, 10.0))
        except TelegramError:
            pass


async def _safe_edit(bot, task: DocumentTask, text: str) -> bool:
    try:
        await bot.edit_message_text(
            chat_id=task.chat_id,
            message_id=task.status_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return True
    except TelegramError:
        return False


# Avisos de acceso denegado: como máximo uno por usuario y hora para no
# convertir el bot en un amplificador de spam si alguien lo encuentra.
_DENIED_NOTICE: dict[int, float] = {}
_DENIED_NOTICE_SECONDS = 3600.0


def is_authorized(user) -> bool:
    """True si el usuario puede usar el bot.

    Sin ``AUTHORIZED_USER_IDS`` el bot mantiene el comportamiento abierto de
    siempre (útil en local); con la variable definida solo pasan esos IDs.
    """
    if not config.AUTHORIZED_USER_IDS:
        return True
    return user is not None and user.id in config.AUTHORIZED_USER_IDS


def _should_notify_denied(user_id: int) -> bool:
    now = time.monotonic()
    last = _DENIED_NOTICE.get(user_id)
    if last is not None and now - last < _DENIED_NOTICE_SECONDS:
        return False
    if len(_DENIED_NOTICE) > 500:
        oldest = min(_DENIED_NOTICE, key=_DENIED_NOTICE.get)
        _DENIED_NOTICE.pop(oldest, None)
    _DENIED_NOTICE[user_id] = now
    return True


async def _authorization_gate(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bloquea cualquier update de un usuario que no esté en la lista blanca.

    Se registra en el grupo -1: se ejecuta antes que los handlers normales y
    ``ApplicationHandlerStop`` impide que el update siga su curso.
    """
    user = update.effective_user
    if is_authorized(user):
        return

    if user is None:
        logger.warning("Acceso denegado: update sin usuario (%s)", type(update).__name__)
    else:
        logger.warning(
            "Acceso denegado a %s (@%s), id=%s",
            user.full_name or "sin nombre",
            user.username or "sin usuario",
            user.id,
        )

    query = update.callback_query
    if query is not None:
        try:
            await query.answer("No estás autorizado para usar este bot.", show_alert=True)
        except TelegramError:
            pass

    message = update.effective_message
    chat = update.effective_chat
    if (
        user is not None
        and message is not None
        and chat is not None
        and chat.type == ChatType.PRIVATE
        and _should_notify_denied(user.id)
    ):
        try:
            await message.reply_text(
                "🔒 <b>Bot privado</b>\n"
                f"Tu ID de Telegram es <code>{user.id}</code>.\n"
                "Pide al administrador que lo añada a "
                "<code>AUTHORIZED_USER_IDS</code>.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    raise ApplicationHandlerStop


async def _edit_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int | None, text: str) -> None:
    if not message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML
        )
    except TelegramError:
        pass


async def _tell(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, status_message_id: int | None, reply_source, text: str
) -> None:
    if status_message_id:
        await _edit_status(context, chat_id, status_message_id, text)
    elif reply_source is not None:
        await _send_html(reply_source.reply_text, text)


def _prune_pending_files(store: dict) -> None:
    cutoff = time.time() - 2 * 3600
    for token, entry in list(store.items()):
        if not isinstance(entry, dict) or entry.get("created", 0) < cutoff:
            store.pop(token, None)


def _pending_count(store: dict, user_id: int) -> int:
    return sum(1 for entry in store.values() if entry.get("user_id") == user_id)


# --------------------------------------------------------------------------- comandos


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = users.get_lang(update.effective_user.id)
    await _send_html(update.message.reply_text, help_text(lang), main_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = context.args or []
    if args:
        code = languages.normalize(args[0])
        if not code:
            await update.message.reply_text(
                "❌ Código no válido. Ejemplos: <code>/lang es</code>, <code>/lang en</code>, "
                "<code>/lang ja</code>, <code>/lang pt-BR</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        users.set_lang(user.id, code)
        note = _fix_engine_for_lang(user.id, code)
        await update.message.reply_text(
            f"✅ Idioma de destino: <b>{html.escape(languages.language_name(code))}</b> "
            f"(<code>{html.escape(code)}</code>)."
            + (f"\n🔄 Cambié el {note}." if note else ""),
            parse_mode=ParseMode.HTML,
        )
        return

    current = users.get_lang(user.id)
    await _send_html(update.message.reply_text, _lang_menu_text(current), _lang_menu_markup(current))


async def cmd_motor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/motor: elige el motor de traducción (auto, google, microsoft, deepl)."""
    user = update.effective_user
    lang = users.get_lang(user.id)
    args = context.args or []
    if args:
        code = normalize_engine(args[0])
        if code is None:
            await update.message.reply_text(
                "❌ Motor no válido. Opciones: <code>/motor auto</code>, "
                "<code>/motor google</code>, <code>/motor microsoft</code> o "
                "<code>/motor deepl</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if code == "deepl" and not deepl_supported(lang):
            await update.message.reply_text(
                f"❌ DeepL no traduce a <b>{html.escape(languages.language_name(lang))}</b>. "
                "Cambia de idioma con /lang o elige otro motor.",
                parse_mode=ParseMode.HTML,
            )
            return
        users.set_engine(user.id, code)
        extra = (
            " (Google primero; si se bloquea, continúa Microsoft o DeepL)"
            if code == "auto"
            else ""
        )
        await update.message.reply_text(
            f"✅ Motor de traducción: <b>{html.escape(engine_label(code))}</b>{extra}.",
            parse_mode=ParseMode.HTML,
        )
        return

    current = users.get_engine(user.id)
    await _send_html(
        update.message.reply_text,
        _engine_menu_text(current, lang),
        _engine_markup(current, lang=lang),
    )


async def on_engine_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones del selector de motor (🧠 Motor o /motor)."""
    query = update.callback_query
    user = query.from_user
    code = normalize_engine((query.data or "").split(":", 1)[-1])
    if code is None:
        await query.answer()
        return
    lang = users.get_lang(user.id)
    if code == "deepl" and not deepl_supported(lang):
        await query.answer(
            f"DeepL no traduce a {languages.language_name(lang)}.", show_alert=True
        )
        return
    users.set_engine(user.id, code)
    await query.answer(f"Motor: {engine_label(code)}")
    try:
        await _send_html(
            query.edit_message_text,
            _engine_menu_text(code, lang),
            _engine_markup(code, lang=lang),
        )
    except TelegramError:
        pass


async def on_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = languages.normalize((query.data or "").split(":", 1)[-1]) or "es"
    users.set_lang(query.from_user.id, code)
    note = _fix_engine_for_lang(query.from_user.id, code)
    await query.answer(f"Idioma: {languages.language_name(code)}" + (f" · {note}" if note else ""))
    try:
        await _send_html(query.edit_message_text, _lang_menu_text(code), _lang_menu_markup(code))
    except TelegramError:
        pass


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    tasks = context.application.bot_data.get(_TASKS_KEY) or {}
    mine = [task for task in tasks.values() if task.user_id == user.id]
    if not mine:
        await update.message.reply_text("No tienes ninguna tarea en curso.")
        return
    for task in mine:
        task.cancel_event.set()
        await _db_call("update_job", task.task_id, status="cancelled")
    if len(mine) == 1:
        text = "🚫 Cancelando la tarea… espera unos segundos."
    else:
        text = f"🚫 Cancelando {len(mine)} tareas (la activa y las que están en cola)…"
    await update.message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = users.get_engine(update.effective_user.id)
    await _send_html(update.message.reply_text, status_text(engine), _status_markup())


async def cmd_dividir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dividir: los documentos que envíes se parten en secciones sin traducir."""
    args = context.args or []
    spec = parse_split_command("/dividir " + " ".join(args))
    context.user_data["file_action"] = {"mode": "split", "spec": spec}
    text = (
        "✂️ <b>Divisor de novelas</b>\n"
        f"Los archivos que envíes se partirán en secciones <b>sin traducir</b> "
        f"({html.escape(describe_spec(spec))}).\n"
        "<blockquote>⚙️ Personaliza el tamaño:\n"
        "• <code>/dividir</code> → automático (~50 capítulos)\n"
        "• <code>/dividir 25</code> → 25 capítulos por parte (en PDF, páginas)\n"
        "• <code>/dividir 8 partes</code> → 8 secciones exactas</blockquote>"
        "📌 También vale escribirlo en el pie de foto del archivo.\n"
        "🌐 Después, envía cada sección como un archivo normal para traducirla."
    )
    await _send_html(update.message.reply_text, text, _mode_markup())


async def cmd_traducir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/traducir: los documentos que envíes se traducen directamente."""
    context.user_data["file_action"] = {"mode": "translate", "spec": None}
    user = update.effective_user
    lang = users.get_lang(user.id)
    await _send_html(
        update.message.reply_text,
        _mode_confirmation("translate", lang, None, users.get_engine(user.id)),
        _mode_markup(),
    )


async def cmd_ambos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ambos: los documentos que envíes se dividen y se traducen por secciones.

    Sin número, el tamaño de las secciones se elige con botones (25/50/75/100).
    """
    args = context.args or []
    if not args:
        await _send_html(update.message.reply_text, _chapters_text(), _chapters_markup())
        return
    spec = parse_split_command("/dividir " + " ".join(args))
    context.user_data["file_action"] = {"mode": "both", "spec": spec}
    lang = users.get_lang(update.effective_user.id)
    await _send_html(
        update.message.reply_text,
        _mode_confirmation("both", lang, spec, users.get_engine(update.effective_user.id)),
        _mode_markup(),
    )


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones del menú fijo (llegan como mensajes de texto)."""
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    label = (message.text or "").strip()
    lang = users.get_lang(user.id)

    mode = {
        MENU_TRANSLATE: "translate",
        MENU_SPLIT: "split",
    }.get(label)
    if mode is not None:
        context.user_data["file_action"] = {"mode": mode, "spec": None}
        await _send_html(
            message.reply_text,
            _mode_confirmation(mode, lang, None, users.get_engine(user.id)),
            _mode_markup(),
        )
        return
    if label == MENU_BOTH:
        # Nada de tamaño automático: se elige con botones.
        await _send_html(message.reply_text, _chapters_text(), _chapters_markup())
        return
    if label == MENU_LANG:
        await _send_html(message.reply_text, _lang_menu_text(lang), _lang_menu_markup(lang))
        return
    if label == MENU_ENGINE:
        current = users.get_engine(user.id)
        await _send_html(message.reply_text, _engine_menu_text(current, lang), _engine_markup(current, lang=lang))
        return
    if label == MENU_STATUS:
        await _send_html(message.reply_text, status_text(users.get_engine(user.id)), _status_markup())
        return
    if label == MENU_CANCEL:
        await cmd_cancel(update, context)
        return
    if label == MENU_HELP:
        await _send_html(message.reply_text, help_text(lang), main_keyboard())


async def on_mode_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.pop("file_action", None)
    await query.answer("Modo desactivado")
    try:
        await _send_html(
            query.edit_message_text,
            "🚫 <b>Modo desactivado.</b>\nEnvíame un archivo y te preguntaré qué hacer con él.",
        )
    except TelegramError:
        pass


async def on_status_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Estado actualizado")
    try:
        await _send_html(
            query.edit_message_text,
            status_text(users.get_engine(query.from_user.id)),
            _status_markup(),
        )
    except TelegramError:
        pass


# --------------------------------------------------------------------------- documentos


async def _ask_file_action(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    document,
    file_name: str,
    size: int,
    *,
    both_count: bool = False,
) -> None:
    """Pregunta qué hacer con el archivo (sin descargarlo todavía).

    Con ``both_count=True`` muestra directamente el selector de capítulos por
    sección (para «dividir y traducir»).
    """
    user = message.from_user
    lang = users.get_lang(user.id)
    token = uuid.uuid4().hex[:8]
    store: dict | None = context.application.bot_data.get(_PENDING_FILES_KEY)
    if store is None:
        store = context.application.bot_data[_PENDING_FILES_KEY] = {}
    _prune_pending_files(store)
    if _pending_count(store, user.id) >= config.PENDING_MAX_FILES:
        await message.reply_text(
            f"⏳ Tienes {config.PENDING_MAX_FILES} archivos esperando una acción. "
            "Responde o descarta alguno antes de enviar más."
        )
        return
    if both_count:
        text, markup = _chapters_text(file_name), _chapters_markup(token)
    else:
        engine = users.get_engine(user.id)
        text, markup = _file_question_text(file_name, size, lang, engine), _file_action_keyboard(token, engine)
    question = await _send_html(message.reply_text, text, markup)
    store[token] = {
        "token": token,
        "user_id": user.id,
        "chat_id": message.chat_id,
        "file_id": document.file_id,
        "file_name": file_name,
        "size": size,
        "message_id": message.message_id,
        "question_id": question.message_id,
        "created": time.time(),
    }


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    document = message.document if message else None
    user = update.effective_user
    if message is None or document is None or user is None:
        return

    file_name = safe_filename(document.file_name or f"documento_{document.file_unique_id}")
    suffix = Path(file_name).suffix.lower()
    if suffix not in config.SUPPORTED_EXTENSIONS:
        await message.reply_text(
            "❌ Formato no soportado. Envíame un <b>PDF</b>, <b>EPUB</b>, <b>TXT</b> o <b>MD</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    size = document.file_size or 0
    if size > config.MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(
            f"❌ El archivo pesa {size / 1048576:.1f} MB y supera el límite de "
            f"{config.MAX_INPUT_MB} MB que Telegram permite descargar a los bots."
        )
        return

    # Prioridad: lo escrito en el pie de foto > el modo elegido en el menú > preguntar.
    from_caption = parse_caption_action(message.caption or "")
    if from_caption is not None:
        mode, spec = from_caption
        # La acción del pie de foto vale solo para este archivo: el modo
        # elegido en el menú sigue activo para los siguientes.
    else:
        # El modo elegido en el menú se mantiene para todos los archivos que
        # envíes seguidos (varios volúmenes); se desactiva con «🚫 Desactivar».
        chosen = context.user_data.get("file_action")
        if isinstance(chosen, dict) and chosen.get("mode") in _MODE_NAMES:
            mode = str(chosen["mode"])
            spec = chosen.get("spec") if mode in {"split", "both"} else None
            if not isinstance(spec, SplitSpec):
                spec = None
        else:
            mode, spec = None, None

    if mode is None:
        await _ask_file_action(message, context, document, file_name, size)
        return
    if mode == "both" and spec is None:
        # «Dividir y traducir» sin tamaño: nada automático, se elige con botones.
        await _ask_file_action(message, context, document, file_name, size, both_count=True)
        return

    await _start_task(
        context,
        user_id=user.id,
        chat_id=message.chat_id,
        file_id=document.file_id,
        file_name=file_name,
        size=size,
        src_message_id=message.message_id,
        mode=mode,
        spec=spec,
        status_message_id=None,
        reply_source=message,
    )


async def on_file_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones 🌐 ✂️ 🔀 🧠 🗑️ del mensaje «¿qué hago con este archivo?»."""
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "fx":
        return
    token, action = parts[1], parts[2]
    store: dict = context.application.bot_data.get(_PENDING_FILES_KEY) or {}
    entry = store.get(token)
    if entry is None or entry.get("user_id") != user.id:
        await query.answer("Esa selección ya no está activa: reenvía el archivo.", show_alert=True)
        return

    if action == "x":
        store.pop(token, None)
        await query.answer("Archivo descartado")
        await _edit_status(context, entry["chat_id"], entry["question_id"], "🗑️ Archivo descartado.")
        return

    if action == "v":  # volver a la pregunta inicial
        await query.answer()
        lang = users.get_lang(user.id)
        engine = users.get_engine(user.id)
        try:
            await _send_html(
                query.edit_message_text,
                _file_question_text(entry["file_name"], entry["size"], lang, engine),
                _file_action_keyboard(token, engine),
            )
        except TelegramError:
            pass
        return

    if action == "e":  # elegir motor sin salir de la pregunta del archivo
        await query.answer("Elige el motor de traducción")
        lang = users.get_lang(user.id)
        engine = users.get_engine(user.id)
        try:
            await _send_html(
                query.edit_message_text,
                _engine_menu_text(engine, lang),
                _engine_markup(engine, token=token, lang=lang),
            )
        except TelegramError:
            pass
        return

    if action == "b":  # «dividir y traducir»: primero el tamaño de las secciones
        await query.answer("Elige cuántos capítulos por sección")
        try:
            await _send_html(
                query.edit_message_text,
                _chapters_text(entry["file_name"]),
                _chapters_markup(token),
            )
        except TelegramError:
            pass
        return

    mode = _ACTION_MODES.get(action)
    if mode is None:
        await query.answer()
        return

    await query.answer(
        {
            "translate": "Preparando la traducción…",
            "split": "Preparando la división…",
            "both": "Dividiendo y traduciendo…",
        }[mode]
    )
    store.pop(token, None)
    context.application.create_task(
        _start_task(
            context,
            user_id=user.id,
            chat_id=entry["chat_id"],
            file_id=entry["file_id"],
            file_name=entry["file_name"],
            size=entry["size"],
            src_message_id=entry["message_id"],
            mode=mode,
            spec=None,
            status_message_id=entry["question_id"],
            reply_source=None,
        )
    )


async def on_file_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Motor elegido desde la pregunta de un archivo pendiente (🧠 Motor)."""
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "fe":
        return
    token = parts[1]
    code = normalize_engine(parts[2])
    store: dict = context.application.bot_data.get(_PENDING_FILES_KEY) or {}
    entry = store.get(token)
    if entry is None or entry.get("user_id") != user.id:
        await query.answer("Esa selección ya no está activa: reenvía el archivo.", show_alert=True)
        return
    if code is None:
        await query.answer()
        return
    lang = users.get_lang(user.id)
    if code == "deepl" and not deepl_supported(lang):
        await query.answer(f"DeepL no traduce a {languages.language_name(lang)}.", show_alert=True)
        return
    users.set_engine(user.id, code)
    await query.answer(f"Motor: {engine_label(code)}")
    try:
        await _send_html(
            query.edit_message_text,
            _file_question_text(entry["file_name"], entry["size"], lang, code),
            _file_action_keyboard(token, code),
        )
    except TelegramError:
        pass


async def on_file_both_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elige capítulos por sección para un archivo ya recibido (selector 🔀)."""
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "fbc" or not parts[2].isdigit():
        return
    token, count = parts[1], int(parts[2])
    if count not in _CHAPTER_CHOICES:
        await query.answer()
        return
    store: dict = context.application.bot_data.get(_PENDING_FILES_KEY) or {}
    entry = store.get(token)
    if entry is None or entry.get("user_id") != user.id:
        await query.answer("Esa selección ya no está activa: reenvía el archivo.", show_alert=True)
        return
    await query.answer(f"Secciones de {count} capítulos")
    store.pop(token, None)
    context.application.create_task(
        _start_task(
            context,
            user_id=user.id,
            chat_id=entry["chat_id"],
            file_id=entry["file_id"],
            file_name=entry["file_name"],
            size=entry["size"],
            src_message_id=entry["message_id"],
            mode="both",
            spec=SplitSpec("units", count),
            status_message_id=entry["question_id"],
            reply_source=None,
        )
    )


async def on_menu_both_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elige capítulos por sección desde el menú o /ambos (sin archivo todavía)."""
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 2 or parts[0] != "mbc" or not parts[1].isdigit():
        return
    count = int(parts[1])
    if count not in _CHAPTER_CHOICES:
        await query.answer()
        return
    spec = SplitSpec("units", count)
    context.user_data["file_action"] = {"mode": "both", "spec": spec}
    await query.answer(f"Secciones de {count} capítulos")
    try:
        await _send_html(
            query.edit_message_text,
            _mode_confirmation(
                "both",
                users.get_lang(query.from_user.id),
                spec,
                users.get_engine(query.from_user.id),
            ),
            _mode_markup(),
        )
    except TelegramError:
        pass


async def _db_call(method: str, *args, **kwargs):
    """Llama a Supabase sin bloquear el bucle de Telegram; nunca lanza."""
    backend = get_backend()
    if backend is None:
        return None
    function = getattr(backend, method, None)
    if function is None:
        return None
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except Exception:  # noqa: BLE001 - Supabase es opcional
        logger.debug("Fallo llamando a Supabase.%s", method, exc_info=True)
        return None


def _task_row(task: DocumentTask) -> dict:
    return {
        "id": task.task_id,
        "user_id": task.user_id,
        "chat_id": task.chat_id,
        "status_message_id": task.status_message_id or None,
        "src_message_id": task.src_message_id,
        "file_id": task.file_id,
        "file_name": task.file_name,
        "size_bytes": task.size,
        "lang": task.lang,
        "engine": task.engine,
        "mode": task.mode,
        "split_mode": task.split_spec.mode if task.split_spec else None,
        "split_value": task.split_spec.value if task.split_spec else None,
        "status": "queued",
    }


def _queue_usage(app: Application, user_id: int) -> tuple[int, int]:
    tasks = app.bot_data.get(_TASKS_KEY) or {}
    mine = [task for task in tasks.values() if task.user_id == user_id]
    return len(mine), sum(int(task.size or 0) for task in mine)


async def _start_task(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    chat_id: int,
    file_id: str,
    file_name: str,
    size: int,
    src_message_id: int | None,
    mode: str,
    spec: SplitSpec | None,
    status_message_id: int | None,
    reply_source=None,
) -> None:
    """Crea la tarea y la deja en la cola (la descarga la hace el worker)."""
    app = context.application
    queue: asyncio.Queue = app.bot_data[_QUEUE_KEY]
    tasks: dict = app.bot_data.setdefault(_TASKS_KEY, {})
    pending: dict = app.bot_data.setdefault(_PENDING_KEY, {})

    count, total_bytes = _queue_usage(app, user_id)
    if count >= config.QUEUE_MAX_FILES:
        await _tell(
            context,
            chat_id,
            status_message_id,
            reply_source,
            f"❌ Ya tienes {count} archivo(s) en cola (máximo {config.QUEUE_MAX_FILES}). "
            "Espera a que termine alguno o usa /cancel.",
        )
        return
    if total_bytes + size > config.QUEUE_MAX_MB * 1024 * 1024:
        used = total_bytes / 1048576
        await _tell(
            context,
            chat_id,
            status_message_id,
            reply_source,
            f"❌ La cola llegaría a {used + size / 1048576:.1f} MB y el máximo es "
            f"{config.QUEUE_MAX_MB} MB. Espera a que termine una tarea.",
        )
        return

    task_id = uuid.uuid4().hex[:12]
    workdir = config.WORK_DIR / task_id
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / file_name

    lang = users.get_lang(user_id)
    engine = users.get_engine(user_id)
    task = DocumentTask(
        task_id=task_id,
        user_id=user_id,
        chat_id=chat_id,
        status_message_id=status_message_id or 0,
        src_message_id=src_message_id,
        src_path=dest,
        file_name=file_name,
        lang=lang,
        engine=engine,
        mode=mode,
        split_spec=spec if mode in {"split", "both"} else None,
        size=size,
        file_id=file_id,
    )

    # Reservar el hueco antes de cualquier espera: si llegan cinco archivos a
    # la vez, los cinco se tienen en cuenta para los límites de la cola.
    tasks[task_id] = task
    pending[task_id] = task
    try:
        header = _task_header(file_name, size, mode, lang, engine)
        status_text_line = header + "\n\n🕒 En cola…"
        if status_message_id:
            task.status_message_id = status_message_id
            await _edit_status(context, chat_id, status_message_id, status_text_line)
        else:
            if reply_source is not None:
                status = await _send_html(reply_source.reply_text, status_text_line)
            else:
                status = await _send_html(
                    lambda text, **kwargs: context.bot.send_message(chat_id=chat_id, text=text, **kwargs),
                    status_text_line,
                )
            task.status_message_id = status.message_id

        # Persistir primero: si el proceso muere justo después, la tarea se
        # recupera al arrancar. Si Supabase no está configurado, no hace nada.
        await _db_call("create_job", _task_row(task))
        queue.put_nowait(task)
        await _edit_status(
            context,
            chat_id,
            task.status_message_id,
            header + f"\n\n🕒 En cola · {max(0, queue.qsize() - 1)} tarea(s) por delante.",
        )
    except Exception:
        tasks.pop(task_id, None)
        pending.pop(task_id, None)
        shutil.rmtree(workdir, ignore_errors=True)
        raise


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    text = (message.text or "") if message else ""
    if not text.strip():
        return
    if len(text) > MAX_TEXT_MESSAGE:
        await message.reply_text(
            f"❌ El texto tiene {len(text)} caracteres; el máximo es {MAX_TEXT_MESSAGE}. "
            "Guárdalo en un .txt y envíamelo como documento."
        )
        return
    lang = users.get_lang(update.effective_user.id)
    engine = users.get_engine(update.effective_user.id)
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        async with create_translator(lang, concurrency=2, engine=engine) as translator:
            translated = await translator.translate(text)
    except TranslationError as err:
        await message.reply_text(
            f"❌ Fallaron los traductores gratuitos (Google, Microsoft y DeepL): {html.escape(str(err))}\n"
            "Puede ser un bloqueo temporal por exceso de peticiones; inténtalo de nuevo en unos minutos."
        )
        return
    for start in range(0, max(1, len(translated)), 3800):
        await message.reply_text(translated[start:start + 3800])


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _send_html(
            update.message.reply_text,
            "🙂 De momento solo traduzco <b>PDF · EPUB · TXT · MD</b> y mensajes de texto.\n"
            "Si me mandas una foto con texto, pásala antes a PDF y la traduciré encantado.",
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Error no controlado", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo de nuevo.")
        except TelegramError:
            pass


# --------------------------------------------------------------------------- cola de trabajo


async def _send_file(bot, task: DocumentTask, path: Path, name: str, caption: str) -> None:
    """Envía un archivo por Telegram con reintentos (los libros tardan horas)."""
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            await bot.send_chat_action(chat_id=task.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            with open(path, "rb") as handle:
                await bot.send_document(
                    chat_id=task.chat_id,
                    document=handle,
                    filename=name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=task.src_message_id,
                    read_timeout=600,
                    write_timeout=600,
                )
            return
        except RetryAfter as err:
            delay = err.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
            logger.warning("Telegram pidió esperar %.0f s antes de enviar", delay)
            await asyncio.sleep(min(delay, 60.0))
            last_err = err
        except BadRequest as err:
            last_err = err
            if task.src_message_id and "reply" in str(err).lower():
                # El mensaje original ya no existe; se envía sin responder a él.
                task.src_message_id = None
                logger.info("El mensaje original de %s ya no existe; se envía sin reply", task.task_id)
                continue
            logger.warning("No se pudo enviar el resultado (intento %d/3): %s", attempt, err)
            if attempt < 3:
                await asyncio.sleep(3.0 * attempt)
        except TelegramError as err:
            last_err = err
            logger.warning("No se pudo enviar el resultado (intento %d/3): %s", attempt, err)
            if attempt < 3:
                await asyncio.sleep(3.0 * attempt)
    raise TelegramError(f"No se pudo enviar el archivo: {last_err}")


async def _run_split_and_translate(bot, task: DocumentTask, reporter: ProgressReporter, report) -> None:
    """Modo «🔀»: envía las secciones sin traducir y después traduce cada una."""
    outcome = await process_split_job(task, report)
    total = len(outcome.parts)

    task.phase = "sending"
    await reporter.update(f"📤 Enviando {total} parte(s) sin traducir…", force=True)
    for part in outcome.parts:
        await _send_file(bot, task, part.path, part.name, _part_caption(part, "todo"))
        await asyncio.sleep(0.4)  # ritmo suave para los límites de Telegram

    for index, part in enumerate(outcome.parts, 1):
        if task.cancelled():
            raise JobCancelledError()
        task.phase = "processing"
        await report((index - 1) / total, f"parte {index} de {total} · empezando")

        sub = DocumentTask(
            task_id=f"{task.task_id}-p{index:02d}",
            user_id=task.user_id,
            chat_id=task.chat_id,
            status_message_id=task.status_message_id,
            src_message_id=task.src_message_id,
            src_path=part.path,
            file_name=part.name,
            lang=task.lang,
            engine=task.engine,
            mode="translate",
        )
        sub.cancel_event = task.cancel_event

        async def part_report(fraction: float, note: str, _index=index, _total=total) -> None:
            overall = (_index - 1 + min(max(fraction, 0.0), 1.0)) / _total
            await report(overall, f"parte {_index} de {_total} · {note}")

        result = await process_job(sub, part_report)
        task.phase = "sending"
        await _send_file(bot, task, result.out_path, result.out_name, _part_caption(part, "done"))
        await asyncio.sleep(0.4)

    await reporter.update(
        f"🎉 <b>{html.escape(task.file_name)}</b>: {total} partes traducidas.\n"
        "📚 Tienes también las secciones originales sin tocar.",
        force=True,
    )


async def _ensure_status_message(bot, task: DocumentTask, text: str) -> None:
    """Garantiza que la tarea tenga un mensaje de estado editable."""
    if task.status_message_id:
        if await _safe_edit(bot, task, text):
            return
        # El mensaje original se borró o ya no existe: se crea uno nuevo.
        task.status_message_id = 0
    try:
        message = await _send_html(
            lambda body, **kwargs: bot.send_message(chat_id=task.chat_id, text=body, **kwargs),
            text,
        )
        task.status_message_id = message.message_id
        await _db_call("update_job", task.task_id, status_message_id=message.message_id)
    except TelegramError:
        logger.debug("No se pudo crear el mensaje de estado de %s", task.task_id, exc_info=True)


async def _ensure_task_input(app: Application, task: DocumentTask) -> None:
    """Recupera el original desde Supabase o Telegram y lo deja en disco.

    La primera vez que se procesa una tarea también se sube a Supabase para
    que un reinicio posterior no dependa del file_id de Telegram.
    """
    dest = task.src_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    backend = get_backend()

    if dest.is_file() and dest.stat().st_size > 0:
        if backend is not None and not task.input_key:
            key = f"inputs/{task.task_id}/{task.file_name}"
            ok = await asyncio.to_thread(
                backend.upload_path, key, dest, content_type=content_type_for(task.file_name)
            )
            if ok:
                task.input_key = key
                await _db_call("update_job", task.task_id, input_key=key)
        return

    if task.input_key and backend is not None:
        data = await asyncio.to_thread(backend.download, task.input_key)
        if data:
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return

    if not task.file_id:
        raise InputUnavailableError(
            "No encuentro el archivo original. Reenvíalo para poder continuar."
        )

    tg_file = await app.bot.get_file(task.file_id)
    await tg_file.download_to_drive(custom_path=str(dest))
    if backend is not None:
        key = f"inputs/{task.task_id}/{task.file_name}"
        ok = await asyncio.to_thread(
            backend.upload_path, key, dest, content_type=content_type_for(task.file_name)
        )
        if ok:
            task.input_key = key
            await _db_call("update_job", task.task_id, input_key=key)


async def _cleanup_remote_input(task: DocumentTask) -> None:
    backend = get_backend()
    if backend is None:
        return
    try:
        await asyncio.to_thread(backend.delete_prefix, f"inputs/{task.task_id}")
    except Exception:  # noqa: BLE001 - limpieza best effort
        logger.debug("No se pudo borrar el original remoto de %s", task.task_id, exc_info=True)


async def _cleanup_task_checkpoints(task: DocumentTask) -> None:
    """Borra los checkpoints remotos de una tarea cancelada (no se reanudará)."""
    if not task.src_path.is_file():
        return
    try:
        store = get_store()
        digest = await asyncio.to_thread(file_digest, task.src_path)
        key = store.key_for(digest, task.lang, task.user_id, task.engine)
        await asyncio.to_thread(store.clear_job, key)
    except Exception:  # noqa: BLE001 - limpieza best effort
        logger.debug("No se pudieron borrar los checkpoints de %s", task.task_id, exc_info=True)


_HEARTBEAT_SECONDS = 45.0
_HEARTBEAT_POLL = 15.0


async def _heartbeat_loop(task: DocumentTask, reporter: ProgressReporter) -> None:
    """Mantiene visible que la tarea sigue viva aunque un paso tarde.

    Algunas peticiones a Google o las subidas de checkpoints pueden tardar
    minutos sin llamar a ``progress``. Sin este latido el mensaje parecía
    congelado aunque el proceso siguiera trabajando.
    """
    while True:
        # Se comprueba más a menudo de lo que se edita para que el primer
        # latido llegue como máximo ~60 s después del último avance.
        await asyncio.sleep(_HEARTBEAT_POLL)
        if task.cancelled():
            return
        if task.phase != "processing":
            continue
        now = time.monotonic()
        last = task.last_progress_at or task.started_at or now
        idle = now - last
        if idle < _HEARTBEAT_SECONDS:
            continue
        note = (task.last_note or "trabajando").strip() or "trabajando"
        elapsed = now - (task.started_at or now)
        await reporter.update(
            _progress_text(task, task.last_fraction, f"{note} · sigo trabajando", elapsed),
            force=True,
        )
        # El latido también cuenta como señal de vida: así no se repite cada
        # 15 s, sino una vez cada ~45 s mientras no haya avances reales.
        task.last_progress_at = now
        task.last_db_update = now
        await _db_call(
            "update_job",
            task.task_id,
            progress=round(min(1.0, max(0.0, task.last_fraction)), 4),
            progress_note=f"{note} · sin avances hace {format_duration(idle)}"[:300],
        )
        logger.info("Latido de %s: %s (%.0f s sin avances)", task.task_id, note, idle)


async def _worker(app: Application) -> None:
    queue: asyncio.Queue = app.bot_data[_QUEUE_KEY]
    active: dict = app.bot_data[_ACTIVE_KEY]
    pending: dict = app.bot_data[_PENDING_KEY]
    tasks: dict = app.bot_data.setdefault(_TASKS_KEY, {})
    bot = app.bot

    while True:
        task = await queue.get()
        result = None
        heartbeat: asyncio.Task | None = None
        try:
            pending.pop(task.task_id, None)
            if task.cancelled():
                await _safe_edit(bot, task, "🚫 Tarea cancelada antes de empezar.")
                await _db_call("update_job", task.task_id, status="cancelled")
                continue

            active[task.task_id] = task
            await _ensure_status_message(bot, task, "🔄 <b>Preparando tarea…</b>")
            task.started_at = time.monotonic()
            reporter = ProgressReporter(bot, task.chat_id, task.status_message_id)
            task.phase = "processing"
            task.last_progress_at = task.started_at
            task.last_fraction = 0.0
            task.last_note = "descargando el archivo"
            heartbeat = asyncio.create_task(_heartbeat_loop(task, reporter))

            await _db_call(
                "update_job",
                task.task_id,
                status="downloading",
                progress=0.0,
                progress_note="descargando el archivo",
            )
            await reporter.update(
                _progress_text(task, 0.0, "descargando el archivo…", 0.0), force=True
            )
            try:
                await _ensure_task_input(app, task)
            except (TelegramError, OSError) as err:
                logger.warning("No se pudo recuperar el original de %s: %s", task.task_id, err)
                await _db_call(
                    "update_job",
                    task.task_id,
                    status="failed",
                    error=f"no se pudo descargar el original: {err}"[:300],
                )
                await _safe_edit(
                    bot,
                    task,
                    "❌ No pude recuperar el archivo original. Reenvíalo para continuar; "
                    "el resto de la cola seguirá avanzando.",
                )
                continue

            await _db_call(
                "update_job",
                task.task_id,
                status="running",
                progress=0.0,
                progress_note="analizando el documento",
            )
            task.last_progress_at = time.monotonic()
            task.last_note = "analizando el documento"
            await reporter.update(
                _progress_text(task, 0.0, "analizando el documento…", 0.0), force=True
            )

            async def report(fraction: float, note: str) -> None:
                now = time.monotonic()
                task.last_progress_at = now
                task.last_fraction = fraction
                task.last_note = note
                await reporter.update(
                    _progress_text(task, fraction, note, now - task.started_at)
                )
                if now - task.last_db_update >= 10.0:
                    task.last_db_update = now
                    await _db_call(
                        "update_job",
                        task.task_id,
                        status="running",
                        progress=round(min(1.0, max(0.0, fraction)), 4),
                        progress_note=note[:300],
                    )

            if task.mode == "split":
                outcome = await process_split_job(task, report)
                task.phase = "sending"
                await reporter.update(f"📤 Enviando {len(outcome.parts)} parte(s)…", force=True)
                for part in outcome.parts:
                    await _send_file(bot, task, part.path, part.name, _part_caption(part))
                    await asyncio.sleep(0.4)  # ritmo suave para los límites de Telegram
                await _db_call("update_job", task.task_id, status="sent", progress=1.0)
                await _cleanup_remote_input(task)
                await reporter.update(outcome.summary, force=True)
            elif task.mode == "both":
                await _run_split_and_translate(bot, task, reporter, report)
                await _db_call("update_job", task.task_id, status="sent", progress=1.0)
                await _cleanup_remote_input(task)
            else:
                result = await process_job(task, report)

                task.phase = "sending"
                out_mb = result.out_path.stat().st_size / 1048576
                if out_mb > config.MAX_OUTPUT_MB:
                    await _safe_edit(
                        bot,
                        task,
                        f"❌ El resultado pesa {out_mb:.1f} MB y Telegram no permite enviarlo "
                        f"(máximo {config.MAX_OUTPUT_MB} MB). Prueba con un documento más pequeño.",
                    )
                    await _db_call(
                        "update_job",
                        task.task_id,
                        status="failed",
                        progress=1.0,
                        error="resultado demasiado grande para Telegram",
                    )
                    continue

                await _db_call(
                    "update_job",
                    task.task_id,
                    status="ready",
                    progress=1.0,
                    output_name=result.out_name,
                    output_key=f"cache/{Path(result.out_path).name}",
                    progress_note="resultado listo; enviando",
                )
                await reporter.update(f"📤 Enviando «{html.escape(result.out_name)}»…", force=True)
                await _send_file(bot, task, result.out_path, result.out_name, result.caption)
                await _db_call("update_job", task.task_id, status="sent", progress=1.0)
                await _cleanup_remote_input(task)
                await reporter.update("✅ <b>Listo.</b> ¡Que lo disfrutes!", force=True)
        except JobCancelledError:
            await _db_call("update_job", task.task_id, status="cancelled")
            await _cleanup_remote_input(task)
            await _cleanup_task_checkpoints(task)
            await _safe_edit(
                bot,
                task,
                {
                    "split": "🚫 División cancelada.",
                    "both": "🚫 División y traducción canceladas.",
                }.get(task.mode, "🚫 Traducción cancelada."),
            )
        except InputUnavailableError as err:
            await _db_call("update_job", task.task_id, status="failed", error=str(err))
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except TelegramError as err:
            logger.error("Fallo de Telegram al enviar %s: %s", task.task_id, err)
            if result is not None:
                await _db_call(
                    "update_job",
                    task.task_id,
                    status="ready",
                    output_name=result.out_name,
                    output_key=f"cache/{Path(result.out_path).name}",
                    error=str(err)[:300],
                )
            if task.mode == "both":
                await _safe_edit(
                    bot,
                    task,
                    "⚠️ Hubo un fallo al enviar alguna parte. Al reiniciar, el bot reanuda "
                    "desde la caché; las partes ya traducidas no se repiten.",
                )
            elif task.mode == "split":
                await _safe_edit(
                    bot,
                    task,
                    "⚠️ La división terminó pero Telegram no aceptó enviar todas las secciones. "
                    "Vuelve a enviarme el archivo con /dividir y te las mandaré de nuevo.",
                )
            else:
                await _safe_edit(
                    bot,
                    task,
                    "⚠️ La traducción terminó pero Telegram no aceptó el archivo. "
                    "El resultado queda guardado: al reiniciar te lo enviaré sin traducir de nuevo.",
                )
        except ScannedPdfError as err:
            await _db_call("update_job", task.task_id, status="failed", error=str(err))
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except SplitError as err:
            await _db_call("update_job", task.task_id, status="failed", error=str(err))
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except UnsupportedFormatError as err:
            await _db_call("update_job", task.task_id, status="failed", error=str(err))
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except TranslationError as err:
            await _db_call("update_job", task.task_id, status="failed", error=str(err)[:300])
            await _safe_edit(
                bot,
                task,
                f"❌ Fallaron los traductores gratuitos (Google, Microsoft y DeepL): {html.escape(str(err))}\n"
                "Puede ser un bloqueo temporal por exceso de peticiones; vuelve a intentarlo más tarde.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Fallo procesando la tarea %s", task.task_id)
            await _db_call("update_job", task.task_id, status="failed", error="error inesperado")
            await _safe_edit(bot, task, "⚠️ Ocurrió un error inesperado al procesar el documento.")
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - un latido nunca debe tumbar al worker
                    logger.warning("Falló el latido de %s", task.task_id, exc_info=True)
            active.pop(task.task_id, None)
            pending.pop(task.task_id, None)
            tasks.pop(task.task_id, None)
            shutil.rmtree(config.WORK_DIR / task.task_id, ignore_errors=True)
            queue.task_done()


# --------------------------------------------------------------------------- arranque


async def _restore_pending_jobs(app: Application) -> None:
    """Recupera de Supabase las tareas que quedaron a medias o en cola.

    Se ejecuta antes de arrancar los workers. El original se descarga del
    Storage (o de Telegram usando el ``file_id``) y los checkpoints se restauran
    de forma perezosa al empezar a traducir.
    """
    backend = get_backend()
    if backend is None:
        return
    try:
        rows = await asyncio.to_thread(backend.fetch_jobs)
    except Exception:  # noqa: BLE001 - Supabase es opcional
        logger.debug("No se pudieron recuperar tareas de Supabase", exc_info=True)
        return
    if not rows:
        return

    queue: asyncio.Queue = app.bot_data[_QUEUE_KEY]
    tasks: dict = app.bot_data[_TASKS_KEY]
    pending: dict = app.bot_data[_PENDING_KEY]
    restored = 0
    for row in rows:
        try:
            task_id = str(row.get("id") or "").strip()
            chat_id = int(row.get("chat_id"))
            user_id = int(row.get("user_id"))
            file_name = safe_filename(str(row.get("file_name") or "documento")) or "documento"
            if not task_id or task_id in tasks:
                continue
            mode = str(row.get("mode") or "translate")
            if mode not in _MODE_NAMES:
                mode = "translate"
            spec = None
            if mode in {"split", "both"}:
                try:
                    split_value = int(row.get("split_value") or 0)
                except (TypeError, ValueError):
                    split_value = 0
                spec = SplitSpec(mode=str(row.get("split_mode") or "units"), value=split_value)
            workdir = config.WORK_DIR / task_id
            workdir.mkdir(parents=True, exist_ok=True)
            task = DocumentTask(
                task_id=task_id,
                user_id=user_id,
                chat_id=chat_id,
                status_message_id=int(row.get("status_message_id") or 0),
                src_message_id=None,
                src_path=workdir / file_name,
                file_name=file_name,
                lang=str(row.get("lang") or config.DEFAULT_TARGET_LANG),
                engine=str(row.get("engine") or "auto"),
                mode=mode,
                split_spec=spec,
                size=int(row.get("size_bytes") or 0),
                file_id=row.get("file_id"),
                input_key=row.get("input_key"),
            )
        except (TypeError, ValueError, OSError):
            logger.warning("Fila de trabajo inválida en Supabase: %s", row, exc_info=True)
            continue

        tasks[task_id] = task
        pending[task_id] = task
        queue.put_nowait(task)
        restored += 1
        await _db_call(
            "update_job",
            task_id,
            status="queued",
            progress_note="recuperada tras un reinicio",
        )
        await _ensure_status_message(
            app.bot,
            task,
            f"♻️ <b>Recuperando tarea…</b>\n\n{_task_header(task.file_name, task.size, task.mode, task.lang, task.engine)}",
        )

    if restored:
        logger.info("Tareas recuperadas desde Supabase: %d", restored)
        logger.info("Se reanudarán en orden; no hace falta reenviar los archivos.")


async def _post_init(app: Application) -> None:
    _bot_data_holder["app"] = app
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Menú principal y cómo funciona"),
            BotCommand("lang", "Cambiar el idioma de destino"),
            BotCommand("motor", "Elegir el motor de traducción"),
            BotCommand("traducir", "Traducir los archivos que envíes"),
            BotCommand("dividir", "Partir una novela larga en secciones"),
            BotCommand("ambos", "Dividir y traducir cada parte"),
            BotCommand("status", "Estado del bot y tareas en curso"),
            BotCommand("cancel", "Cancelar la tarea en curso"),
            BotCommand("help", "Ayuda y trucos"),
        ]
    )
    try:
        await app.bot.set_my_short_description(
            "Traduce PDF, EPUB, TXT y MD, divide novelas largas en secciones y te avisa del progreso."
        )
        await app.bot.set_my_description(
            "Envíame un documento (PDF, EPUB, TXT o MD) y te lo devuelvo traducido con los "
            "traductores gratuitos de Google, Microsoft y DeepL, conservando el diseño y los capítulos.\n\n"
            "✂️ Con «Dividir» troceo novelas de +100 capítulos en secciones para traducirlas por "
            "separado y reanudar donde se quedó.\n"
            "🔀 Con «Dividir y traducir» te envío todas las secciones y después cada una traducida.\n\n"
            "También traduzco mensajes de texto. Toca un botón del menú para empezar."
        )
    except TelegramError:
        logger.debug("No se pudo actualizar la descripción del bot", exc_info=True)
    app.bot_data[_QUEUE_KEY] = asyncio.Queue()
    app.bot_data[_ACTIVE_KEY] = {}
    app.bot_data[_PENDING_KEY] = {}
    app.bot_data[_TASKS_KEY] = {}
    app.bot_data[_PENDING_FILES_KEY] = {}
    app.bot_data[_LOCK_KEY] = asyncio.Lock()

    backend = get_backend()
    if backend is not None:
        try:
            await asyncio.to_thread(backend.ensure_bucket)
        except Exception:  # noqa: BLE001 - Supabase es opcional
            logger.debug("No se pudo comprobar el bucket de Supabase", exc_info=True)
    try:
        await asyncio.to_thread(users.preload)
    except Exception:  # noqa: BLE001
        logger.debug("No se pudieron precargar las preferencias", exc_info=True)
    try:
        await asyncio.to_thread(_cleanup_startup)
    except OSError:
        logger.warning("La limpieza inicial falló", exc_info=True)

    # Primero se recuperan las tareas pendientes; después los workers empiezan
    # a vaciar la cola en el mismo orden en que se enviaron los archivos.
    await _restore_pending_jobs(app)

    workers = [app.create_task(_worker(app)) for _ in range(config.MAX_CONCURRENT_JOBS)]
    app.bot_data[_WORKERS_KEY] = workers
    logger.info(
        "Bot listo. Tareas simultáneas: %d · concurrencia de traducción: %d · motor: %s",
        config.MAX_CONCURRENT_JOBS,
        config.TRANSLATE_CONCURRENCY,
        config.TRANSLATOR_BACKEND,
    )


async def _post_shutdown(app: Application) -> None:
    for worker in app.bot_data.get(_WORKERS_KEY, []):
        worker.cancel()
    backend = get_backend()
    if backend is not None:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            logger.debug("No se pudo cerrar el cliente de Supabase", exc_info=True)
    logger.info("Bot detenido.")


def _cleanup_startup() -> None:
    """Borra restos de tareas anteriores y resultados caducados de la caché."""
    removed = 0
    if config.WORK_DIR.exists():
        for child in config.WORK_DIR.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                pass
    if removed:
        logger.info("Limpieza inicial: %d carpeta(s) temporales eliminadas", removed)
    get_store().prune(max_mb=config.CACHE_MAX_MB, ttl_days=config.CACHE_TTL_DAYS)


def build_app() -> Application:
    """Construye la aplicación de Telegram con todos los handlers."""
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=60.0,
        write_timeout=180.0,
        pool_timeout=15.0,
    )

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    no_document = ~filters.Document.ALL  # deja pasar los comandos escritos en el pie de foto
    # Grupo -1: la lista blanca se comprueba antes que cualquier otro handler.
    app.add_handler(TypeHandler(Update, _authorization_gate), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("motor", cmd_motor, filters=no_document))
    app.add_handler(CommandHandler("traducir", cmd_traducir, filters=no_document))
    app.add_handler(CommandHandler("dividir", cmd_dividir, filters=no_document))
    app.add_handler(CommandHandler("ambos", cmd_ambos, filters=no_document))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_lang_button, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_engine_button, pattern=r"^eng:"))
    app.add_handler(CallbackQueryHandler(on_file_action, pattern=r"^fx:"))
    app.add_handler(CallbackQueryHandler(on_file_engine, pattern=r"^fe:"))
    app.add_handler(CallbackQueryHandler(on_file_both_count, pattern=r"^fbc:"))
    app.add_handler(CallbackQueryHandler(on_menu_both_count, pattern=r"^mbc:"))
    app.add_handler(CallbackQueryHandler(on_mode_off, pattern=r"^mode:off$"))
    app.add_handler(CallbackQueryHandler(on_status_refresh, pattern=r"^status:refresh$"))
    app.add_handler(MessageHandler(filters.Text(list(MENU_LABELS)), on_menu_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO | filters.Sticker.ALL,
            on_other,
        )
    )
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "Falta TELEGRAM_BOT_TOKEN. Copia .env.example a .env y pega el token de @BotFather."
        )
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    if config.HEALTH_PORT:
        # Render (y otras plataformas) necesitan un puerto HTTP abierto.
        start_health_server(config.HEALTH_PORT, status_provider=_status_snapshot)
    app = build_app()
    if config.AUTHORIZED_USER_IDS:
        logger.info(
            "Acceso restringido a %d usuario(s) de la lista blanca.",
            len(config.AUTHORIZED_USER_IDS),
        )
    else:
        logger.warning(
            "AUTHORIZED_USER_IDS está vacío: cualquier usuario puede usar el bot. "
            "Define tu ID numérico para cerrarlo."
        )
    logger.info("Iniciando el bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
