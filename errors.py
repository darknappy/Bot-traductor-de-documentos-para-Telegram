"""Excepciones compartidas del bot."""
from __future__ import annotations


class JobCancelledError(RuntimeError):
    """El usuario canceló la tarea."""


class UnsupportedFormatError(RuntimeError):
    """El archivo no es de un tipo soportado."""


class ScannedPdfError(RuntimeError):
    """El PDF no contiene texto extraíble (probablemente es un escaneo)."""
