"""Backend con la API oficial de Google Cloud Translation (v2, con API key).

Se activa con ``TRANSLATOR_BACKEND=api`` y ``GOOGLE_API_KEY``. Requiere
facturación en Google Cloud, pero aguanta mucho más volumen sin bloqueos, por
lo que es la opción recomendada para traducir novelas enormes de una sola vez.

Ventaja extra: la API traduce **varios segmentos por petición** de forma nativa
(no hace falta la técnica de unir párrafos con saltos de línea).
"""
from __future__ import annotations

import asyncio
import html
import logging

import httpx

from translator import BaseTranslator, TranslationError, _has_letters, _split_edges, split_text

logger = logging.getLogger(__name__)

API_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

# Límites conservadores de la API v2 (varios "q" por petición).
MAX_BATCH_ITEMS = 64
MAX_BATCH_CHARS = 4500


class GoogleApiTranslator(BaseTranslator):
    """Cliente de la API oficial de Google Cloud Translation (v2)."""

    name = "google-api"

    def __init__(self, target_lang: str, api_key: str, **kwargs) -> None:
        kwargs.setdefault("concurrency", 8)
        kwargs.setdefault("max_batch_chars", MAX_BATCH_CHARS)
        super().__init__(target_lang, **kwargs)
        self.api_key = api_key

    # ------------------------------------------------------------------ peticiones

    async def _send(self, client: httpx.AsyncClient, text: str) -> tuple[httpx.Response, str]:  # pragma: no cover
        # No se usa: la API envía varios segmentos por petición (_request_batch).
        return await self._post(client, [text]), "api"

    async def _post(self, client: httpx.AsyncClient, texts: list[str]) -> httpx.Response:
        body: dict = {"q": texts, "target": self.target_lang, "format": "text"}
        if self.source_lang and self.source_lang not in ("auto", ""):
            body["source"] = self.source_lang
        return await client.post(API_ENDPOINT, params={"key": self.api_key}, json=body)

    def _parse(self, response: httpx.Response, _kind: str = "api") -> tuple[str, str | None]:  # pragma: no cover
        # No se usa directamente; ver _request_batch.
        values, detected = _parse_api_response(response)
        return values[0], detected

    async def _request_batch(self, texts: list[str]) -> tuple[list[str], str | None]:
        """Traduce varios segmentos en una petición (con freno global y reintentos)."""
        from translator import MAX_ATTEMPTS

        if self._client is None or self._sem is None:
            raise RuntimeError("El traductor debe usarse como contexto: 'async with GoogleApiTranslator(...)'")
        last_err: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._wait_cooldown()
            response: httpx.Response | None = None
            try:
                async with self._sem:
                    response = await self._post(self._client, texts)
                self.requests_done += 1
                self.chars_sent += sum(len(text) for text in texts)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise TranslationError(f"HTTP {response.status_code}")
                response.raise_for_status()
                values, detected = _parse_api_response(response)
                if len(values) != len(texts):
                    raise TranslationError("La API devolvió un número de traducciones inesperado")
                self._fail_streak = 0
                return values, detected
            except (httpx.HTTPError, TranslationError) as err:
                last_err = err
                self._on_retryable(response, err)
                self._register_failure(err)
                if attempt < MAX_ATTEMPTS:
                    delay = min(2.0 ** attempt, 20.0) * self.retry_delay_scale
                    logger.info("Reintento %d/%d de la API en %.1f s", attempt, MAX_ATTEMPTS, delay)
                    if delay:
                        await asyncio.sleep(delay)
        raise TranslationError(f"No se pudo traducir tras {MAX_ATTEMPTS} intentos: {last_err}")

    async def _request(self, text: str) -> tuple[str, str | None]:
        values, detected = await self._request_batch([text])
        return values[0], detected

    # ------------------------------------------------------------------ API pública

    async def translate(self, text: str) -> str:
        if not text or not text.strip() or not _has_letters(text):
            return text
        lead, core, trail = _split_edges(text)
        pieces = split_text(core, self.max_chunk_chars)
        indexes = list(range(len(pieces)))
        values: list[str] = []
        for batch in self._batch_indexes(pieces, indexes):
            translated, detected = await self._request_batch([pieces[i] for i in batch])
            if detected and self.detected_lang is None:
                self.detected_lang = detected
            values.extend(value.strip() for value in translated)
        if not values:
            return text
        return lead + " ".join(values) + trail

    async def translate_many(self, texts: list[str]) -> list[str]:
        """Traduce muchos textos usando varios segmentos por petición."""
        if not texts:
            return []
        results: list[str | None] = [None] * len(texts)
        pending = [index for index, text in enumerate(texts) if text.strip() and _has_letters(text)]
        pending_set = set(pending)
        for index, text in enumerate(texts):
            if index not in pending_set:
                results[index] = text

        async def run(batch: list[int]):
            values, detected = await self._request_batch([texts[i] for i in batch])
            if detected and self.detected_lang is None:
                self.detected_lang = detected
            return batch, values

        batches = self._batch_indexes(texts, pending)
        for batch, values in await asyncio.gather(*(run(batch) for batch in batches)):
            for index, value in zip(batch, values):
                results[index] = value

        return [value if value is not None else texts[i] for i, value in enumerate(results)]

    # ------------------------------------------------------------------ lotes

    def _batch_indexes(self, texts: list[str], indexes: list[int]) -> list[list[int]]:
        """Agrupa índices originales respetando el número de segmentos y caracteres."""
        batches: list[list[int]] = []
        current: list[int] = []
        length = 0
        for index in indexes:
            size = len(texts[index].strip())
            if current and (len(current) >= MAX_BATCH_ITEMS or length + size > self.max_batch_chars):
                batches.append(current)
                current = []
                length = 0
            current.append(index)
            length += size
        if current:
            batches.append(current)
        return batches


def _parse_api_response(response: httpx.Response) -> tuple[list[str], str | None]:
    """Extrae las traducciones de la respuesta de la API v2."""
    try:
        data = response.json()
    except ValueError as err:
        raise TranslationError("JSON inválido de la API de Google") from err
    payload = data.get("data") if isinstance(data, dict) else None
    translations = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(translations, list):
        raise TranslationError("Respuesta inesperada de la API de Google")
    values: list[str] = []
    detected: str | None = None
    for item in translations:
        if not isinstance(item, dict):
            raise TranslationError("Respuesta inesperada de la API de Google")
        # La API v2 devuelve el texto escapado en HTML.
        values.append(html.unescape(str(item.get("translatedText", ""))))
        if detected is None and isinstance(item.get("detectedSourceLanguage"), str):
            detected = item["detectedSourceLanguage"]
    return values, detected
