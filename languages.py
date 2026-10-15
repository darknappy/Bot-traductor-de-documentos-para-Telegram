"""Catálogo de idiomas soportados (código de Google -> nombre en español)."""
from __future__ import annotations

import re

LANGUAGES: dict[str, str] = {
    "es": "Español",
    "en": "Inglés",
    "pt": "Portugués",
    "fr": "Francés",
    "de": "Alemán",
    "it": "Italiano",
    "nl": "Neerlandés",
    "ru": "Ruso",
    "uk": "Ucraniano",
    "pl": "Polaco",
    "cs": "Checo",
    "ro": "Rumano",
    "sv": "Sueco",
    "da": "Danés",
    "no": "Noruego",
    "fi": "Finés",
    "el": "Griego",
    "tr": "Turco",
    "ar": "Árabe",
    "he": "Hebreo",
    "hi": "Hindi",
    "th": "Tailandés",
    "vi": "Vietnamita",
    "id": "Indonesio",
    "ja": "Japonés",
    "ko": "Coreano",
    "zh": "Chino",
    "ca": "Catalán",
    "gl": "Gallego",
    "eu": "Euskera",
    "la": "Latín",
}

# Botones del teclado de /lang (códigos del catálogo).
KEYBOARD_ROWS: list[list[str]] = [
    ["es", "en", "pt"],
    ["fr", "de", "it"],
    ["ru", "uk", "pl"],
    ["zh", "ja", "ko"],
    ["ar", "he", "tr"],
    ["nl", "sv", "el"],
]


def language_name(code: str | None) -> str:
    """Nombre legible de un código de idioma."""
    if not code:
        return "desconocido"
    code = code.strip().lower()
    if code in LANGUAGES:
        return LANGUAGES[code]
    base = code.split("-")[0]
    return LANGUAGES.get(base, code.upper())


def normalize(code: str) -> str | None:
    """Normaliza un código introducido por el usuario.

    Acepta mayúsculas, espacios y variantes regionales ("es-ES" -> "es").
    Devuelve None si el formato no es válido.
    """
    code = (code or "").strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,4})?", code):
        return None
    base = code.split("-")[0]
    if base in LANGUAGES:
        return base
    if code in LANGUAGES:
        return code
    return code  # se deja pasar: Google acepta muchos códigos fuera del catálogo
