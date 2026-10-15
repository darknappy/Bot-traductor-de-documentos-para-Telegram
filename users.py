"""Preferencias por usuario (idioma destino) persistidas en users.json."""
from __future__ import annotations

import json
import logging
import os
import threading

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    data: dict[str, str] = {}
    try:
        if config.USERS_FILE.exists():
            raw = json.loads(config.USERS_FILE.read_text("utf-8"))
            if isinstance(raw, dict):
                data = {str(key): str(value) for key, value in raw.items()}
    except Exception:
        logger.warning("No se pudo leer %s; se empieza de cero", config.USERS_FILE, exc_info=True)
    _cache = data
    return data


def _save(data: dict[str, str]) -> None:
    try:
        tmp = config.USERS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, config.USERS_FILE)
    except Exception:
        logger.warning("No se pudieron guardar las preferencias", exc_info=True)


def get_lang(user_id: int) -> str:
    """Idioma destino preferido del usuario (o el valor por defecto)."""
    with _lock:
        return _load().get(str(user_id), config.DEFAULT_TARGET_LANG)


def set_lang(user_id: int, lang: str) -> None:
    """Guarda el idioma destino preferido del usuario."""
    with _lock:
        data = _load()
        if data.get(str(user_id)) != lang:
            data[str(user_id)] = lang
            _save(data)
