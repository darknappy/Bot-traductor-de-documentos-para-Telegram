"""Bot de Telegram que traduce documentos (PDF, EPUB, TXT, MD) con Google y Microsoft.

Incluye un menú fijo de botones, un divisor de novelas (/dividir) para trabajar por
secciones en libros enormes y el modo «dividir y traducir» (secciones + cada una
traducida por separado).

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
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
import languages
import users
from errors import JobCancelledError, ScannedPdfError, SplitError, UnsupportedFormatError
from health import start_health_server
from pipeline import (
    DocumentTask,
    format_duration,
    get_store,
    process_job,
    process_split_job,
    safe_filename,
)
from splitter import SplitPart, SplitSpec, describe_spec, parse_split_command
from translator import TranslationError, create_translator

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
MENU_STATUS = "📊 Estado"
MENU_CANCEL = "🚫 Cancelar"
MENU_HELP = "❓ Ayuda"
MENU_LABELS = (
    MENU_TRANSLATE,
    MENU_SPLIT,
    MENU_BOTH,
    MENU_LANG,
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
    snapshot: dict = {
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "queue": 0,
        "active": [],
        "cache_mb": 0.0,
        "cache_files": 0,
        "translator": config.TRANSLATOR_BACKEND,
    }
    try:
        app = _bot_data_holder.get("app")
        data = getattr(app, "bot_data", None) or {}
        queue = data.get(_QUEUE_KEY)
        snapshot["queue"] = queue.qsize() if queue is not None else 0
        for task in (data.get(_ACTIVE_KEY) or {}).values():
            snapshot["active"].append(
                {
                    "file": task.file_name,
                    "user": task.user_id,
                    "lang": task.lang,
                    "mode": task.mode,
                    "elapsed": round(time.monotonic() - task.started_at, 1) if task.started_at else 0,
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
                KeyboardButton(MENU_STATUS),
                KeyboardButton(MENU_CANCEL, api_kwargs=_style("danger")),
            ],
            [KeyboardButton(MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Envía un archivo o toca un botón…",
    )


def _mode_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Desactivar modo", callback_data="mode:off", api_kwargs=_style("danger"))]]
    )


def _file_action_keyboard(token: str) -> InlineKeyboardMarkup:
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
                    "🗑️ Descartar", callback_data=f"fx:{token}:x", api_kwargs=_style("danger")
                )
            ],
        ]
    )


def _status_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Actualizar", callback_data="status:refresh", api_kwargs=_style("primary"))]]
    )


# --------------------------------------------------------------------------- mensajes


def help_text(lang: str) -> str:
    return (
        "🌍 <b>Bot traductor de documentos</b>\n"
        "Traduzco <b>PDF · EPUB · TXT · MD</b> y mensajes sueltos con los traductores\n"
        "gratuitos de Google y Microsoft.\n\n"
        "<blockquote>🌐 <b>Traducir</b> — conserva el diseño del PDF y los capítulos, "
        "estilos y enlaces del EPUB.\n"
        "✂️ <b>Dividir</b> — parte una novela larga en secciones <i>sin traducir</i> "
        "(ideal para +100 capítulos).\n"
        "🔀 <b>Dividir y traducir</b> — te manda las secciones y después traduce cada "
        "una por separado.</blockquote>\n\n"
        f"🌐 Idioma de destino: <b>{html.escape(languages.language_name(lang))}</b> · "
        "cámbialo con ⚙️ <b>Idioma</b>\n\n"
        "<blockquote expandable>💡 <b>Trucos y comandos</b>\n"
        "• Toca un botón del menú y envíame el archivo, o mándalo tal cual: te preguntaré "
        "qué hacer con él.\n"
        "• <code>/dividir 25</code> → 25 capítulos por sección · "
        "<code>/dividir 8 partes</code> → 8 secciones (en PDF se cuentan páginas).\n"
        "• También sirve la leyenda del archivo: <code>/traducir</code>, "
        "<code>/dividir 25</code> o <code>/ambos</code>.\n"
        f"• Tamaño máximo por archivo: {config.MAX_INPUT_MB} MB (límite de Telegram).\n"
        "• Los libros enormes guardan el progreso: reenvía el mismo archivo y continúo "
        "donde iba.\n"
        "• Comandos: /lang · /dividir · /status · /cancel · /help</blockquote>"
    )


def status_text() -> str:
    """Resumen del estado del bot (se usa en /status)."""
    snapshot = _status_snapshot()
    active = snapshot.get("active") or []
    lines = [
        "📊 <b>Estado del bot</b>",
        "<blockquote>"
        f"🟢 Activo desde hace {format_duration(snapshot.get('uptime_seconds', 0))}\n"
        f"⚙️ Motor: <code>{html.escape(str(config.TRANSLATOR_BACKEND))}</code> · "
        f"{config.TRANSLATE_CONCURRENCY} traducciones en paralelo\n"
        f"📥 En cola: {snapshot.get('queue', 0)} · 🧵 En proceso: {len(active)}\n"
        f"💾 Resultados guardados: {snapshot.get('cache_files', 0)} archivo(s) · "
        f"{snapshot.get('cache_mb', 0):.0f} MB"
        "</blockquote>",
    ]
    for task in active:
        emoji = _MODE_EMOJI.get(str(task.get("mode", "translate")), "🔄")
        lines.append(
            f"{emoji} <code>{html.escape(str(task.get('file', '?')))}</code> · "
            f"{format_duration(task.get('elapsed', 0))}"
        )
    if not active:
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
        lines.append(f"🌐 → {html.escape(languages.language_name(task.lang))}")
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


def _task_header(file_name: str, size: int, mode: str, lang: str) -> str:
    if mode == "split":
        action = "✂️ Se partirá en secciones sin traducir"
    elif mode == "both":
        action = "🔀 Se dividirá y se traducirá cada parte"
    else:
        action = f"🌐 → {html.escape(languages.language_name(lang))}"
    return (
        f"📥 <b>{html.escape(file_name)}</b>\n"
        f"<blockquote>📦 {size / 1048576:.1f} MB · {_kind_label(file_name)}\n{action}</blockquote>"
    )


def _file_question_text(file_name: str, size: int, lang: str) -> str:
    return (
        f"📄 <b>{html.escape(file_name)}</b>\n"
        f"<blockquote>📦 {size / 1048576:.1f} MB · {_kind_label(file_name)} · "
        f"🌐 → {html.escape(languages.language_name(lang))}</blockquote>\n"
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


def _mode_confirmation(mode: str, lang: str, spec: SplitSpec | None) -> str:
    if mode == "split":
        return (
            "✂️ <b>Modo división activado</b>\n"
            f"El próximo archivo se partirá en secciones <b>sin traducir</b> "
            f"({html.escape(describe_spec(spec))}).\n"
            "<blockquote>💡 Ajusta el tamaño con <code>/dividir 25</code> (capítulos por "
            "parte) o <code>/dividir 8 partes</code>.</blockquote>"
        )
    if mode == "both":
        return (
            "🔀 <b>Modo «dividir y traducir» activado</b>\n"
            "Te enviaré primero todas las secciones <b>sin traducir</b> y después iré "
            "traduciendo cada una.\n"
            "<blockquote>🧩 Cada sección se traduce por separado: si una falla, las demás "
            "quedan hechas.</blockquote>"
        )
    return (
        "🌐 <b>Modo traducción activado</b>\n"
        f"El próximo archivo se traducirá a <b>{html.escape(languages.language_name(lang))}</b> "
        "conservando su formato.\n"
        "<blockquote>💡 Si prefieres decidir después, desactiva el modo: al enviarme un "
        "archivo te preguntaré qué hacer.</blockquote>"
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


async def _safe_edit(bot, task: DocumentTask, text: str) -> None:
    try:
        await bot.edit_message_text(
            chat_id=task.chat_id,
            message_id=task.status_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass


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
    for user_id in [uid for uid, entry in store.items() if entry.get("created", 0) < cutoff]:
        store.pop(user_id, None)


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
        await update.message.reply_text(
            f"✅ Idioma de destino: <b>{html.escape(languages.language_name(code))}</b> "
            f"(<code>{html.escape(code)}</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    current = users.get_lang(user.id)
    await _send_html(update.message.reply_text, _lang_menu_text(current), _lang_menu_markup(current))


async def on_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = languages.normalize((query.data or "").split(":", 1)[-1]) or "es"
    users.set_lang(query.from_user.id, code)
    await query.answer(f"Idioma: {languages.language_name(code)}")
    try:
        await _send_html(query.edit_message_text, _lang_menu_text(code), _lang_menu_markup(code))
    except TelegramError:
        pass


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    active = context.application.bot_data.get(_ACTIVE_KEY, {})
    pending = context.application.bot_data.get(_PENDING_KEY, {})
    task = active.get(user.id) or pending.get(user.id)
    if task is None:
        await update.message.reply_text("No tienes ninguna tarea en curso.")
        return
    task.cancel_event.set()
    await update.message.reply_text("🚫 Cancelando… espera unos segundos.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_html(update.message.reply_text, status_text(), _status_markup())


async def cmd_dividir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dividir: el próximo documento se parte en secciones sin traducir."""
    args = context.args or []
    spec = parse_split_command("/dividir " + " ".join(args))
    context.user_data["file_action"] = {"mode": "split", "spec": spec}
    text = (
        "✂️ <b>Divisor de novelas</b>\n"
        f"El próximo archivo se partirá en secciones <b>sin traducir</b> "
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
    """/traducir: el próximo documento se traduce directamente."""
    context.user_data["file_action"] = {"mode": "translate", "spec": None}
    lang = users.get_lang(update.effective_user.id)
    await _send_html(update.message.reply_text, _mode_confirmation("translate", lang, None), _mode_markup())


async def cmd_ambos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ambos: el próximo documento se divide y se traduce por secciones."""
    args = context.args or []
    spec = parse_split_command("/dividir " + " ".join(args)) if args else None
    context.user_data["file_action"] = {"mode": "both", "spec": spec}
    lang = users.get_lang(update.effective_user.id)
    await _send_html(update.message.reply_text, _mode_confirmation("both", lang, spec), _mode_markup())


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
        MENU_BOTH: "both",
    }.get(label)
    if mode is not None:
        context.user_data["file_action"] = {"mode": mode, "spec": None}
        await _send_html(message.reply_text, _mode_confirmation(mode, lang, None), _mode_markup())
        return
    if label == MENU_LANG:
        await _send_html(message.reply_text, _lang_menu_text(lang), _lang_menu_markup(lang))
        return
    if label == MENU_STATUS:
        await _send_html(message.reply_text, status_text(), _status_markup())
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
        await _send_html(query.edit_message_text, status_text(), _status_markup())
    except TelegramError:
        pass


# --------------------------------------------------------------------------- documentos


async def _ask_file_action(
    message, context: ContextTypes.DEFAULT_TYPE, document, file_name: str, size: int
) -> None:
    """Pregunta qué hacer con el archivo (sin descargarlo todavía)."""
    user = message.from_user
    lang = users.get_lang(user.id)
    token = uuid.uuid4().hex[:8]
    store: dict | None = context.application.bot_data.get(_PENDING_FILES_KEY)
    if store is None:
        store = context.application.bot_data[_PENDING_FILES_KEY] = {}
    _prune_pending_files(store)
    previous = store.get(user.id)
    if previous is not None and previous.get("question_id"):
        await _edit_status(
            context,
            previous.get("chat_id", message.chat_id),
            previous["question_id"],
            "⏭️ Sustituido por un archivo más reciente.",
        )
    question = await _send_html(
        message.reply_text,
        _file_question_text(file_name, size, lang),
        _file_action_keyboard(token),
    )
    store[user.id] = {
        "token": token,
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
        context.user_data.pop("file_action", None)
    else:
        chosen = context.user_data.pop("file_action", None)
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
    """Botones 🌐 ✂️ 🔀 🗑️ del mensaje «¿qué hago con este archivo?»."""
    query = update.callback_query
    user = query.from_user
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "fx":
        return
    token, action = parts[1], parts[2]
    store: dict = context.application.bot_data.get(_PENDING_FILES_KEY) or {}
    entry = store.get(user.id)
    if entry is None or entry.get("token") != token:
        await query.answer("Esa selección ya no está activa: reenvía el archivo.", show_alert=True)
        return

    if action == "x":
        store.pop(user.id, None)
        await query.answer("Archivo descartado")
        await _edit_status(context, entry["chat_id"], entry["question_id"], "🗑️ Archivo descartado.")
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
    store.pop(user.id, None)
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
    """Descarga el documento y lo deja en la cola de trabajo."""
    app = context.application
    active: dict = app.bot_data.get(_ACTIVE_KEY, {})
    pending: dict = app.bot_data.get(_PENDING_KEY, {})
    lock: asyncio.Lock = app.bot_data.get(_LOCK_KEY) or asyncio.Lock()

    task_id = uuid.uuid4().hex[:12]
    workdir = config.WORK_DIR / task_id
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / file_name

    lang = users.get_lang(user_id)
    task = DocumentTask(
        task_id=task_id,
        user_id=user_id,
        chat_id=chat_id,
        status_message_id=status_message_id or 0,
        src_message_id=src_message_id,
        src_path=dest,
        file_name=file_name,
        lang=lang,
        mode=mode,
        split_spec=spec if mode in {"split", "both"} else None,
    )

    # Reserva al usuario antes de descargar: evita procesar dos archivos a la vez
    # si los envía seguidos.
    async with lock:
        if user_id in active or user_id in pending:
            shutil.rmtree(workdir, ignore_errors=True)
            await _tell(
                context,
                chat_id,
                status_message_id,
                reply_source,
                "⏳ Ya tienes una tarea en curso. Espera a que termine o usa /cancel.",
            )
            return
        pending[user_id] = task

    header = _task_header(file_name, size, mode, lang)
    if status_message_id is None:
        status = await _send_html(reply_source.reply_text, header + "\n\n⬇️ Descargando…")
        task.status_message_id = status_message_id = status.message_id
    else:
        task.status_message_id = status_message_id
        await _edit_status(context, chat_id, status_message_id, header + "\n\n⬇️ Descargando…")

    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(dest))
    except TelegramError as err:
        logger.warning("Descarga fallida: %s", err)
        pending.pop(user_id, None)
        shutil.rmtree(workdir, ignore_errors=True)
        await _edit_status(
            context,
            chat_id,
            status_message_id,
            "❌ No se pudo descargar el archivo. Recuerda que los bots de Telegram solo "
            f"pueden bajar archivos de hasta {config.MAX_INPUT_MB} MB.",
        )
        return
    except Exception:  # noqa: BLE001
        logger.exception("Error inesperado descargando el archivo")
        pending.pop(user_id, None)
        shutil.rmtree(workdir, ignore_errors=True)
        await _edit_status(context, chat_id, status_message_id, "❌ Error al descargar el archivo.")
        return

    queue: asyncio.Queue = app.bot_data[_QUEUE_KEY]
    queue.put_nowait(task)
    await _edit_status(
        context,
        chat_id,
        status_message_id,
        header + f"\n\n🕒 En cola · {queue.qsize()} tarea(s) por delante.",
    )


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
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        async with create_translator(lang, concurrency=2) as translator:
            translated = await translator.translate(text)
    except TranslationError as err:
        await message.reply_text(
            f"❌ Fallaron los traductores gratuitos (Google y Microsoft): {html.escape(str(err))}\n"
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

    await reporter.update(f"📤 Enviando {total} parte(s) sin traducir…", force=True)
    for part in outcome.parts:
        await _send_file(bot, task, part.path, part.name, _part_caption(part, "todo"))
        await asyncio.sleep(0.4)  # ritmo suave para los límites de Telegram

    for index, part in enumerate(outcome.parts, 1):
        if task.cancelled():
            raise JobCancelledError()
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
            mode="translate",
        )
        sub.cancel_event = task.cancel_event

        async def part_report(fraction: float, note: str, _index=index, _total=total) -> None:
            overall = (_index - 1 + min(max(fraction, 0.0), 1.0)) / _total
            await report(overall, f"parte {_index} de {_total} · {note}")

        result = await process_job(sub, part_report)
        await _send_file(bot, task, result.out_path, result.out_name, _part_caption(part, "done"))
        await asyncio.sleep(0.4)

    await reporter.update(
        f"🎉 <b>{html.escape(task.file_name)}</b>: {total} partes traducidas.\n"
        "📚 Tienes también las secciones originales sin tocar.",
        force=True,
    )


async def _worker(app: Application) -> None:
    queue: asyncio.Queue = app.bot_data[_QUEUE_KEY]
    active: dict = app.bot_data[_ACTIVE_KEY]
    pending: dict = app.bot_data[_PENDING_KEY]
    bot = app.bot

    while True:
        task = await queue.get()
        try:
            pending.pop(task.user_id, None)
            if task.cancelled():
                await _safe_edit(bot, task, "🚫 Tarea cancelada antes de empezar.")
                continue

            active[task.user_id] = task
            task.started_at = time.monotonic()
            reporter = ProgressReporter(bot, task.chat_id, task.status_message_id)
            await reporter.update(
                _progress_text(task, 0.0, "analizando el documento…", 0.0), force=True
            )

            async def report(fraction: float, note: str) -> None:
                await reporter.update(
                    _progress_text(task, fraction, note, time.monotonic() - task.started_at)
                )

            if task.mode == "split":
                outcome = await process_split_job(task, report)
                await reporter.update(f"📤 Enviando {len(outcome.parts)} parte(s)…", force=True)
                for part in outcome.parts:
                    await _send_file(bot, task, part.path, part.name, _part_caption(part))
                    await asyncio.sleep(0.4)  # ritmo suave para los límites de Telegram
                await reporter.update(outcome.summary, force=True)
            elif task.mode == "both":
                await _run_split_and_translate(bot, task, reporter, report)
            else:
                result = await process_job(task, report)

                out_mb = result.out_path.stat().st_size / 1048576
                if out_mb > config.MAX_OUTPUT_MB:
                    await _safe_edit(
                        bot,
                        task,
                        f"❌ El resultado pesa {out_mb:.1f} MB y Telegram no permite enviarlo "
                        f"(máximo {config.MAX_OUTPUT_MB} MB). Prueba con un documento más pequeño.",
                    )
                    continue

                await reporter.update(f"📤 Enviando «{html.escape(result.out_name)}»…", force=True)
                await _send_file(bot, task, result.out_path, result.out_name, result.caption)
                await reporter.update("✅ <b>Listo.</b> ¡Que lo disfrutes!", force=True)
        except JobCancelledError:
            await _safe_edit(
                bot,
                task,
                {
                    "split": "🚫 División cancelada.",
                    "both": "🚫 División y traducción canceladas.",
                }.get(task.mode, "🚫 Traducción cancelada."),
            )
        except TelegramError as err:
            logger.error("Fallo de Telegram al enviar %s: %s", task.task_id, err)
            if task.mode == "both":
                await _safe_edit(
                    bot,
                    task,
                    "⚠️ Hubo un fallo al enviar alguna parte. Reenvíame el mismo archivo: "
                    "las partes ya traducidas se recuperan de la caché al instante.",
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
                    "Vuelve a enviarme el mismo documento y te lo mandaré al instante desde la caché.",
                )
        except ScannedPdfError as err:
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except SplitError as err:
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except UnsupportedFormatError as err:
            await _safe_edit(bot, task, f"❌ {html.escape(str(err))}")
        except TranslationError as err:
            await _safe_edit(
                bot,
                task,
                f"❌ Fallaron los traductores gratuitos (Google y Microsoft): {html.escape(str(err))}\n"
                "Puede ser un bloqueo temporal por exceso de peticiones; vuelve a intentarlo más tarde.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Fallo procesando la tarea %s", task.task_id)
            await _safe_edit(bot, task, "⚠️ Ocurrió un error inesperado al procesar el documento.")
        finally:
            active.pop(task.user_id, None)
            pending.pop(task.user_id, None)
            shutil.rmtree(config.WORK_DIR / task.task_id, ignore_errors=True)
            queue.task_done()


# --------------------------------------------------------------------------- arranque


async def _post_init(app: Application) -> None:
    _bot_data_holder["app"] = app
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Menú principal y cómo funciona"),
            BotCommand("lang", "Cambiar el idioma de destino"),
            BotCommand("traducir", "Traducir el próximo archivo"),
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
            "traductores gratuitos de Google y Microsoft, conservando el diseño y los capítulos.\n\n"
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
    app.bot_data[_PENDING_FILES_KEY] = {}
    app.bot_data[_LOCK_KEY] = asyncio.Lock()
    try:
        await asyncio.to_thread(_cleanup_startup)
    except OSError:
        logger.warning("La limpieza inicial falló", exc_info=True)
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("traducir", cmd_traducir, filters=no_document))
    app.add_handler(CommandHandler("dividir", cmd_dividir, filters=no_document))
    app.add_handler(CommandHandler("ambos", cmd_ambos, filters=no_document))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_lang_button, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_file_action, pattern=r"^fx:"))
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
    logger.info("Iniciando el bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
