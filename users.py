"""Preferencias por usuario (idioma destino y motor) persistidas en users.json."""
from __future__ import annotations

import json
import logging
import os
import threading

import config
from supabase_store import get_backend

logger = logging.getLogger(__name__)

#: Motor por defecto: Google primero y relevo automático si se bloquea.
DEFAULT_ENGINE = "auto"

_lock = threading.Lock()
_cache: dict[str, dict[str, str]] | None = None
_remote_disabled = False


def _normalize(raw: dict) -> dict[str, dict[str, str]]:
    """Acepta el formato antiguo (``{id: "es"}``) y el nuevo (``{id: {...}}``)."""
    data: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            entry = {"lang": str(value.get("lang") or config.DEFAULT_TARGET_LANG)}
            engine = str(value.get("engine") or DEFAULT_ENGINE)
            if engine != DEFAULT_ENGINE:
                entry["engine"] = engine
            data[str(key)] = entry
        else:
            data[str(key)] = {"lang": str(value)}
    return data


def _load() -> dict[str, dict[str, str]]:
    global _cache
    if _cache is not None:
        return _cache
    data: dict[str, dict[str, str]] = {}
    try:
        if config.USERS_FILE.exists():
            raw = json.loads(config.USERS_FILE.read_text("utf-8"))
            if isinstance(raw, dict):
                data = _normalize(raw)
    except Exception:
        logger.warning("No se pudo leer %s; se empieza de cero", config.USERS_FILE, exc_info=True)
    _cache = data
    return data


def _save(data: dict[str, dict[str, str]]) -> None:
    try:
        tmp = config.USERS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, config.USERS_FILE)
    except Exception:
        logger.warning("No se pudieron guardar las preferencias", exc_info=True)


def _entry(data: dict[str, dict[str, str]], user_id: int) -> dict[str, str]:
    key = str(user_id)
    entry = data.get(key)
    if entry is None:
        entry = data[key] = {"lang": config.DEFAULT_TARGET_LANG}
    return entry


def _remote_upsert(user_id: int, entry: dict[str, str]) -> None:
    """Guarda la preferencia en Supabase sin bloquear el bucle de Telegram."""
    global _remote_disabled
    if _remote_disabled:
        return
    backend = get_backend()
    if backend is None:
        return

    def send() -> None:
        global _remote_disabled
        try:
            ok = backend.upsert_user(
                user_id,
                entry.get("lang") or config.DEFAULT_TARGET_LANG,
                entry.get("engine") or DEFAULT_ENGINE,
            )
        except Exception:  # noqa: BLE001 - Supabase nunca debe romper el bot
            logger.debug("No se pudo guardar la preferencia en Supabase", exc_info=True)
            return
        if not ok:
            _remote_disabled = True

    # El cliente HTTP es síncrono y los handlers de Telegram son async: una
    # petición lenta no debe congelar el botón de idioma o de motor.
    threading.Thread(target=send, name="supabase-user", daemon=True).start()


def preload() -> None:
    """Carga las preferencias de Supabase al arrancar (si está configurado).

    Si la tabla está vacía, sube las preferencias locales para no perderlas.
    Si Supabase falla, se sigue usando ``users.json`` sin interrumpir el bot.
    """
    global _cache, _remote_disabled
    backend = get_backend()
    if backend is None:
        return
    rows = backend.fetch_users()
    if not rows:
        if backend.db_available:
            with _lock:
                data = _load()
            for key, entry in data.items():
                try:
                    user_id = int(key)
                except ValueError:
                    continue
                _remote_upsert(user_id, entry)
        return

    with _lock:
        data = dict(_load())
        for row in rows:
            try:
                user_id = int(row.get("user_id"))
            except (TypeError, ValueError):
                continue
            entry = {"lang": str(row.get("lang") or config.DEFAULT_TARGET_LANG)}
            engine = str(row.get("engine") or DEFAULT_ENGINE)
            if engine != DEFAULT_ENGINE:
                entry["engine"] = engine
            data[str(user_id)] = entry
        _cache = data
    logger.info("Preferencias cargadas desde Supabase: %d usuario(s)", len(rows))


def remote_enabled() -> bool:
    return not _remote_disabled and get_backend() is not None


def get_lang(user_id: int) -> str:
    """Idioma destino preferido del usuario (o el valor por defecto)."""
    with _lock:
        return _load().get(str(user_id), {}).get("lang") or config.DEFAULT_TARGET_LANG


def set_lang(user_id: int, lang: str) -> None:
    """Guarda el idioma destino preferido del usuario."""
    changed = False
    with _lock:
        data = _load()
        entry = _entry(data, user_id)
        if entry.get("lang") != lang:
            entry["lang"] = lang
            _save(data)
            changed = True
    if changed:
        _remote_upsert(user_id, entry)


def get_engine(user_id: int) -> str:
    """Motor de traducción preferido del usuario (``auto`` por defecto)."""
    with _lock:
        return _load().get(str(user_id), {}).get("engine") or DEFAULT_ENGINE


def set_engine(user_id: int, engine: str) -> None:
    """Guarda el motor preferido (``auto`` se guarda como valor por defecto)."""
    changed = False
    with _lock:
        data = _load()
        entry = _entry(data, user_id)
        wanted = engine if engine != DEFAULT_ENGINE else None
        if entry.get("engine") != wanted:
            if wanted is None:
                entry.pop("engine", None)
            else:
                entry["engine"] = wanted
            _save(data)
            changed = True
    if changed:
        _remote_upsert(user_id, entry)
