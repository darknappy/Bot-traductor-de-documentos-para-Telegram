"""Excepciones compartidas del bot."""
from __future__ import annotations


class JobCancelledError(RuntimeError):
    """El usuario canceló la tarea."""


class UnsupportedFormatError(RuntimeError):
    """El archivo no es de un tipo soportado."""


class ScannedPdfError(RuntimeError):
    """El PDF no contiene texto extraíble (probablemente es un escaneo)."""


class SplitError(RuntimeError):
    """El documento no se puede dividir en secciones (sin capítulos detectables)."""


class InputUnavailableError(RuntimeError):
    """No se pudo recuperar el archivo original (ni de Supabase ni de Telegram)."""
