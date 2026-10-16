"""Bot de Telegram que traduce documentos (PDF, EPUB, TXT, MD) con Google y Microsoft.

Incluye un divisor de novelas (/dividir) para trabajar por secciones en libros
enormes.

Uso:
    1. Copia .env.example a .env y pega el token de @BotFather.
    2. python bot.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import shutil
import time
import uuid
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import RetryAfter, TelegramError
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
from splitter import describe_spec, parse_split_command
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
# Referencia global a la aplicación (solo para el estado de /status y Render).
_bot_data_holder: dict = {}


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


# --------------------------------------------------------------------------- mensajes


def help_text(lang: str) -> str:
    return (
        "🌍 <b>Bot traductor de documentos</b>\n\n"
        "Envíame un archivo y te lo devuelvo traducido con los traductores gratuitos\n"
        "de Google y Microsoft:\n"
        "• <b>PDF</b> — conserva el diseño original (imágenes, tablas, colores)\n"
        "• <b>EPUB</b> — conserva capítulos, estilos, enlaces e imágenes\n"
        "• <b>TXT / MD</b> — texto plano\n\n"
        "También puedes escribirme un texto suelto y te lo traduzco al momento.\n\n"
        f"Idioma de destino actual: <b>{html.escape(languages.language_name(lang))}</b>\n\n"
        "Comandos:\n"
        "/lang – cambiar el idioma de destino\n"
        "/dividir – partir una novela larga en secciones\n"
        "/status – estado del bot y tareas en curso\n"
        "/cancel – cancelar la tarea en curso\n"
        "/help – mostrar esta ayuda\n\n"
        f"⚠️ Los bots de Telegram solo pueden descargar archivos de hasta "
        f"{config.MAX_INPUT_MB} MB.\n"
        "📚 <b>Novelas largas</b>: usa /dividir para trocear el libro en secciones "
        "(~50 capítulos) y traduce cada una por separado. Así, si una falla o el bot "
        "se reinicia, solo reenvías esa sección. Si traduces el libro entero, el bot "
        "guarda el progreso capítulo a capítulo y lo reanuda donde iba."
    )


def status_text() -> str:
    """Resumen del estado del bot (se usa en /status)."""
    snapshot = _status_snapshot()
    active = snapshot.get("active") or []
    lines = [
        "🤖 <b>Estado del bot</b>",
        f"🕒 En funcionamiento desde hace {format_duration(snapshot.get('uptime_seconds', 0))}",
        f"🧵 Motor: <code>{html.escape(str(config.TRANSLATOR_BACKEND))}</code> · "
        f"{config.TRANSLATE_CONCURRENCY} peticiones simultáneas",
        f"📥 En cola: {snapshot.get('queue', 0)} · en proceso: {len(active)}",
        f"💾 Resultados guardados: {snapshot.get('cache_mb', 0):.0f} MB "
        f"({snapshot.get('cache_files', 0)} archivo(s))",
    ]
    for task in active:
        lines.append(
            f"• <code>{html.escape(str(task.get('file', '?')))}</code> "
            f"({format_duration(task.get('elapsed', 0))})"
        )
    return "\n".join(lines)


def _progress_text(task: DocumentTask, fraction: float, note: str, elapsed: float) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * 10))
    bar = "▓" * filled + "░" * (10 - filled)
    header = "✂️ <b>Dividiendo…</b>" if task.mode == "split" else "🔄 <b>Traduciendo…</b>"
    lines = [header, f"📄 {html.escape(task.file_name)}"]
    if task.mode != "split":
        lines.append(f"🌐 → {html.escape(languages.language_name(task.lang))}")
    lines.append(f"{bar} {int(round(fraction * 100))}% · {html.escape(note)}")
    lines.append(f"⏱️ {format_duration(elapsed)}")
    return "\n".join(lines)


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


# --------------------------------------------------------------------------- comandos


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = users.get_lang(update.effective_user.id)
    await update.message.reply_text(help_text(lang), parse_mode=ParseMode.HTML)


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
    keyboard = [
        [
            InlineKeyboardButton(
                ("✅ " if code == current else "") + languages.LANGUAGES[code],
                callback_data=f"lang:{code}",
            )
            for code in row
        ]
        for row in languages.KEYBOARD_ROWS
    ]
    await update.message.reply_text(
        "Elige el idioma de destino (o usa <code>/lang código</code> para cualquier otro):",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = languages.normalize((query.data or "").split(":", 1)[-1]) or "es"
    users.set_lang(query.from_user.id, code)
    await query.answer(f"Idioma: {languages.language_name(code)}")
    try:
        await query.edit_message_text(
            f"✅ Idioma de destino: <b>{html.escape(languages.language_name(code))}</b> "
            f"(<code>{html.escape(code)}</code>).",
            parse_mode=ParseMode.HTML,
        )
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
    await update.message.reply_text(status_text(), parse_mode=ParseMode.HTML)


async def cmd_dividir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dividir: el próximo documento se parte en secciones sin traducir."""
    args = context.args or []
    spec = parse_split_command("/dividir " + " ".join(args))
    context.user_data["split_mode"] = spec
    await update.message.reply_text(
        "✂️ <b>Divisor de novelas</b>\n"
        f"El próximo archivo que envíes se partirá <b>sin traducir</b> "
        f"({html.escape(describe_spec(spec))}).\n\n"
        "• <code>/dividir</code> → tamaño automático\n"
        "• <code>/dividir 25</code> → 25 capítulos por sección (en PDF, páginas)\n"
        "• <code>/dividir 8 partes</code> → 8 secciones\n\n"
        "También puedes enviar el archivo con la leyenda «<code>/dividir 25</code>» "
        "en el pie de foto. Después traduce cada sección por separado: si una falla, "
        "solo tendrás que reenviar esa.",
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- documentos


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

    app = context.application
    active: dict = app.bot_data[_ACTIVE_KEY]
    pending: dict = app.bot_data[_PENDING_KEY]

    # Modo división: con la leyenda «/dividir …» o activado antes con /dividir.
    spec = parse_split_command(message.caption or "")
    if spec is None:
        spec = context.user_data.pop("split_mode", None)
    else:
        context.user_data.pop("split_mode", None)

    lang = users.get_lang(user.id)
    task_id = uuid.uuid4().hex[:12]
    workdir = config.WORK_DIR / task_id
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / file_name
    task = DocumentTask(
        task_id=task_id,
        user_id=user.id,
        chat_id=message.chat_id,
        status_message_id=0,
        src_message_id=message.message_id,
        src_path=dest,
        file_name=file_name,
        lang=lang,
        mode="split" if spec else "translate",
        split_spec=spec,
    )

    # Reserva al usuario antes de descargar: evita procesar dos archivos a la vez
    # si los envía seguidos.
    lock: asyncio.Lock = app.bot_data[_LOCK_KEY]
    async with lock:
        if user.id in active or user.id in pending:
            shutil.rmtree(workdir, ignore_errors=True)
            await message.reply_text(
                "⏳ Ya tienes una tarea en curso. Espera a que termine o usa /cancel."
            )
            return
        pending[user.id] = task

    status = await message.reply_text(f"📥 Descargando «{file_name}»…")
    task.status_message_id = status.message_id
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(custom_path=str(dest))
    except TelegramError as err:
        logger.warning("Descarga fallida: %s", err)
        pending.pop(user.id, None)
        shutil.rmtree(workdir, ignore_errors=True)
        await status.edit_text(
            "❌ No se pudo descargar el archivo. Recuerda que los bots de Telegram solo "
            f"pueden bajar archivos de hasta {config.MAX_INPUT_MB} MB."
        )
        return
    except Exception:  # noqa: BLE001
        logger.exception("Error inesperado descargando el archivo")
        pending.pop(user.id, None)
        shutil.rmtree(workdir, ignore_errors=True)
        await status.edit_text("❌ Error al descargar el archivo.")
        return

    queue: asyncio.Queue = app.bot_data[_QUEUE_KEY]
    queue.put_nowait(task)

    if spec:
        detail = "✂️ Modo división: se partirá en secciones sin traducir.\n"
    else:
        detail = f"🌐 → {html.escape(languages.language_name(lang))}\n"
    await status.edit_text(
        f"📥 Recibido «{html.escape(file_name)}» ({size / 1048576:.1f} MB)\n"
        f"{detail}"
        f"🕒 En cola… hay {queue.qsize()} tarea(s) por delante.",
        parse_mode=ParseMode.HTML,
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
        await update.message.reply_text(
            "Solo sé traducir documentos (PDF, EPUB, TXT, MD) y mensajes de texto. 🙂"
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
                await reporter.update(f"📤 Enviando {len(outcome.parts)} sección(es)…", force=True)
                for part in outcome.parts:
                    await _send_file(bot, task, part.out_path, part.out_name, part.caption)
                    await asyncio.sleep(0.4)  # ritmo suave para los límites de Telegram
                await reporter.update(outcome.summary, force=True)
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
                await reporter.update("✅ Listo.", force=True)
        except JobCancelledError:
            await _safe_edit(
                bot,
                task,
                "🚫 División cancelada." if task.mode == "split" else "🚫 Traducción cancelada.",
            )
        except TelegramError as err:
            logger.error("Fallo de Telegram al enviar %s: %s", task.task_id, err)
            if task.mode == "split":
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
            BotCommand("start", "Cómo funciona"),
            BotCommand("lang", "Cambiar el idioma de destino"),
            BotCommand("dividir", "Partir una novela larga en secciones"),
            BotCommand("status", "Estado del bot y tareas en curso"),
            BotCommand("cancel", "Cancelar la tarea en curso"),
            BotCommand("help", "Ayuda"),
        ]
    )
    app.bot_data[_QUEUE_KEY] = asyncio.Queue()
    app.bot_data[_ACTIVE_KEY] = {}
    app.bot_data[_PENDING_KEY] = {}
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("dividir", cmd_dividir, filters=~filters.Document.ALL))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_lang_button, pattern=r"^lang:"))
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
