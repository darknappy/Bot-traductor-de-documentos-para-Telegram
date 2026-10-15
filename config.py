"""Configuración global del bot: variables de entorno, rutas y límites."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# Token del bot creado con @BotFather (obligatorio).
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Idioma destino por defecto (código de Google: "es", "en", "pt"...).
DEFAULT_TARGET_LANG: str = (os.getenv("DEFAULT_TARGET_LANG", "es") or "es").strip().lower()

# Motor de traducción: "web" (gratis, sin API key) o "api" (Google Cloud
# Translation, requiere GOOGLE_API_KEY y facturación).
TRANSLATOR_BACKEND: str = (os.getenv("TRANSLATOR_BACKEND", "web") or "web").strip().lower()
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "").strip()

# Peticiones simultáneas al traductor.
TRANSLATE_CONCURRENCY: int = max(1, _env_int("TRANSLATE_CONCURRENCY", 6))

# Ritmo del endpoint web gratuito: segundos mínimos entre peticiones y tope
# cuando Google limita (HTTP 429). Desde IP de datacenter conviene ir despacio.
WEB_MIN_INTERVAL: float = max(0.0, _env_float("WEB_MIN_INTERVAL", 0.6))
WEB_MAX_INTERVAL: float = max(WEB_MIN_INTERVAL, _env_float("WEB_MAX_INTERVAL", 8.0))

# Documentos procesados a la vez (cada uno usa su propia concurrencia).
MAX_CONCURRENT_JOBS: int = max(1, _env_int("MAX_CONCURRENT_JOBS", 1))

# Tamaño máximo de entrada en MB. Los bots de Telegram no pueden descargar
# archivos de más de 20 MB con getFile().
MAX_INPUT_MB: int = max(1, _env_int("MAX_INPUT_MB", 20))

# Tamaño máximo del archivo que envía el bot (Telegram permite hasta 50 MB).
MAX_OUTPUT_MB: int = max(1, _env_int("MAX_OUTPUT_MB", 48))

# Carpeta de datos (trabajo, progreso y caché de resultados).
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))

# Carpeta para archivos temporales de trabajo.
WORK_DIR: Path = Path(os.getenv("WORK_DIR", str(DATA_DIR / "workdir")))

# Progreso persistente por documento (para reanudar traducciones largas).
JOBS_DIR: Path = Path(os.getenv("JOBS_DIR", str(DATA_DIR / "jobs")))

# Resultados ya traducidos (para reenviarlos al instante si se repite el envío).
CACHE_DIR: Path = Path(os.getenv("CACHE_DIR", str(DATA_DIR / "cache")))
CACHE_TTL_DAYS: float = max(0.0, _env_float("CACHE_TTL_DAYS", 7.0))
CACHE_MAX_MB: int = max(1, _env_int("CACHE_MAX_MB", 512))

# Archivo JSON con las preferencias de idioma de cada usuario.
USERS_FILE: Path = Path(os.getenv("USERS_FILE", str(BASE_DIR / "users.json")))

# Servidor HTTP de salud (Render lo necesita en los Web Services). Render define
# la variable PORT; también se puede forzar con HEALTH_PORT.
HEALTH_PORT: int | None = _env_optional_int("HEALTH_PORT") or _env_optional_int("PORT")

# Extensiones admitidas.
SUPPORTED_EXTENSIONS = {".pdf", ".epub", ".txt", ".md"}
