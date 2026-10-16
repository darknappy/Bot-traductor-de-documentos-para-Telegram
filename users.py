"""Preferencias por usuario (idioma destino y motor) persistidas en users.json."""
from __future__ import annotations

import json
import logging
import os
import threading

import config

logger = logging.getLogger(__name__)

#: Motor por defecto: Google primero y relevo automático si se bloquea.
DEFAULT_ENGINE = "auto"

_lock = threading.Lock()
_cache: dict[str, dict[str, str]] | None = None


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


def get_lang(user_id: int) -> str:
    """Idioma destino preferido del usuario (o el valor por defecto)."""
    with _lock:
        return _load().get(str(user_id), {}).get("lang") or config.DEFAULT_TARGET_LANG


def set_lang(user_id: int, lang: str) -> None:
    """Guarda el idioma destino preferido del usuario."""
    with _lock:
        data = _load()
        entry = _entry(data, user_id)
        if entry.get("lang") != lang:
            entry["lang"] = lang
            _save(data)


def get_engine(user_id: int) -> str:
    """Motor de traducción preferido del usuario (``auto`` por defecto)."""
    with _lock:
        return _load().get(str(user_id), {}).get("engine") or DEFAULT_ENGINE


def set_engine(user_id: int, engine: str) -> None:
    """Guarda el motor preferido (``auto`` se guarda como valor por defecto)."""
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
