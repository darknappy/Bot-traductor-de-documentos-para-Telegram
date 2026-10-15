"""Traducción con Google Translate (endpoint web gratuito y API oficial).

**Backend web** (por defecto, gratis y sin API key): usa la misma llamada que
hace el traductor de Google en el navegador (``client=gtx`` sobre
translate.googleapis.com). El servicio es público, pero limita la velocidad si
se abusa de él, por eso:

* las peticiones pasan por un semáforo (concurrencia configurable);
* cada fallo extiende un "tiempo de espera global" que todas las tareas
  respetan (freno automático) y se reintenta con espera exponencial;
* los textos cortos se agrupan en **lotes** separados por saltos de línea para
  reducir el número de peticiones (clave con novelas de +1000 capítulos).

**Backend API** (``TRANSLATOR_BACKEND=api``): usa la API oficial de Google
Cloud Translation con ``GOOGLE_API_KEY``; requiere facturación pero soporta
muchísimo más volumen sin bloqueos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

import httpx

logger = logging.getLogger(__name__)

ENDPOINT = "https://translate.googleapis.com/translate_a/single"

# El endpoint web admite bastantes caracteres, pero se es conservador para
# reducir errores y truncamientos.
MAX_CHUNK_CHARS = 4000
# Tamaño máximo de un lote (varios párrafos separados por \n).
MAX_BATCH_CHARS = 3600
MAX_ATTEMPTS = 5
MAX_COOLDOWN = 90.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…。！？;:])\s+")


class TranslationError(RuntimeError):
    """Fallo irrecuperable al hablar con el traductor de Google."""


def split_text(text: str, max_len: int = MAX_CHUNK_CHARS) -> list[str]:
    """Divide el texto en trozos de <= max_len cortando preferentemente por frases."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(text):
        if not sentence:
            continue
        pieces = _hard_split(sentence, max_len) if len(sentence) > max_len else [sentence]
        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + len(piece) + 1 <= max_len:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, max_len: int) -> list[str]:
    """Corta un texto sin espacios útiles en trozos de <= max_len."""
    parts: list[str] = []
    while len(text) > max_len:
        cut = text.rfind(" ", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _split_edges(text: str) -> tuple[str, str, str]:
    """Separa los espacios iniciales y finales del texto útil."""
    lead_len = len(text) - len(text.lstrip())
    core = text.strip()
    trail_len = len(text) - len(text.rstrip())
    return text[:lead_len], core, (text[len(text) - trail_len:] if trail_len else "")


def _reattach(original: str, core: str) -> str:
    lead, _core, trail = _split_edges(original)
    return f"{lead}{core}{trail}"


class BaseTranslator:
    """Maquinaria común: cliente HTTP, semáforo, freno global y reintentos."""

    name = "base"

    def __init__(
        self,
        target_lang: str,
        source_lang: str = "auto",
        concurrency: int = 6,
        timeout: float = 60.0,
        *,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        max_batch_chars: int = MAX_BATCH_CHARS,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay_scale: float = 1.0,
    ) -> None:
        self.target_lang = target_lang
        self.source_lang = source_lang
        self.concurrency = max(1, int(concurrency))
        self.timeout = timeout
        self.max_chunk_chars = max_chunk_chars
        self.max_batch_chars = max_batch_chars
        self.retry_delay_scale = max(0.0, float(retry_delay_scale))
        self.detected_lang: str | None = None
        self.requests_done = 0
        self.chars_sent = 0
        self._transport = transport
        self._sem: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None
        # Freno global: si Google limita, todas las tareas esperan.
        self._cooldown_until = 0.0
        self._fail_streak = 0

    # ------------------------------------------------------------------ ciclo de vida

    async def __aenter__(self) -> "BaseTranslator":
        self._sem = asyncio.Semaphore(self.concurrency)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "*/*",
            },
            follow_redirects=True,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._sem = None

    # ------------------------------------------------------------------ API pública

    async def translate(self, text: str) -> str:
        """Traduce un texto (puede ser largo: se divide automáticamente)."""
        if not text or not text.strip() or not _has_letters(text):
            return text

        lead, core, trail = _split_edges(text)
        pieces = split_text(core, self.max_chunk_chars)
        translated_pieces: list[str] = []
        for piece in pieces:
            translated, detected = await self._request(piece)
            if detected and self.detected_lang is None:
                self.detected_lang = detected
            translated_pieces.append(translated.strip())

        if not translated_pieces:
            return text
        return lead + " ".join(translated_pieces) + trail

    async def translate_many(self, texts: list[str]) -> list[str]:
        """Traduce muchos textos en paralelo, agrupando los cortos en lotes.

        Devuelve una lista con el mismo orden y tamaño que ``texts``. Los textos
        sin letras (números, viñetas, separadores) se devuelven tal cual.
        """
        if not texts:
            return []

        results: list[str | None] = [None] * len(texts)
        singles: list[int] = []
        batchable: list[int] = []

        for index, text in enumerate(texts):
            _lead, core, _trail = _split_edges(text)
            if not core or not _has_letters(core):
                results[index] = text  # nada que traducir
            elif "\n" in core:
                singles.append(index)  # multilínea: no se puede agrupar con \n
            else:
                batchable.append(index)

        groups = self._plan_batches(texts, batchable) if batchable else []
        jobs = [self._translate_group([texts[i] for i in group]) for group in groups]
        jobs += [self._translate_single(texts[i]) for i in singles]
        pairs = await asyncio.gather(*jobs)
        for indexes, values in zip(groups + [[i] for i in singles], pairs):
            for index, value in zip(indexes, values):
                results[index] = value

        return [value if value is not None else texts[i] for i, value in enumerate(results)]

    async def detect(self, text: str) -> str | None:
        """Detecta el idioma de un fragmento (una sola petición)."""
        sample = text.strip()[:400]
        if not sample:
            return None
        _, detected = await self._request(sample)
        if detected:
            self.detected_lang = detected
        return detected

    # ------------------------------------------------------------------ lotes

    def _plan_batches(self, texts: list[str], indexes: list[int]) -> list[list[int]]:
        """Agrupa índices originales en lotes que quepan en una sola petición."""
        groups: list[list[int]] = []
        current: list[int] = []
        length = 0
        for index in indexes:
            size = len(texts[index].strip())
            extra = size + (1 if current else 0)
            if current and length + extra > self.max_batch_chars:
                groups.append(current)
                current = []
                length = 0
                extra = size
            current.append(index)
            length += extra
        if current:
            groups.append(current)
        return groups

    async def _translate_single(self, text: str) -> list[str]:
        return [await self.translate(text)]

    async def _translate_group(self, texts: list[str]) -> list[str]:
        """Traduce un lote uniendo los textos con saltos de línea.

        Si Google no devuelve exactamente el mismo número de líneas se traduce
        cada texto por separado (más lento pero seguro).
        """
        if len(texts) == 1:
            return [await self.translate(texts[0])]
        joined = "\n".join(text.strip() for text in texts)
        translated, detected = await self._request(joined)
        if detected and self.detected_lang is None:
            self.detected_lang = detected
        parts = translated.split("\n")
        if len(parts) == len(texts) and all(part.strip() for part in parts):
            return [_reattach(original, part.strip()) for original, part in zip(texts, parts)]

        logger.debug(
            "El lote no se pudo repartir (%d líneas para %d textos); se traduce por separado",
            len(parts), len(texts),
        )
        return list(await asyncio.gather(*(self.translate(text) for text in texts)))

    async def translate_segments(self, texts: list[str]) -> list[str]:
        """Traduce una lista de textos como peticiones independientes por lotes."""
        return await self.translate_many(texts)

    # ------------------------------------------------------------------ freno global

    async def _wait_cooldown(self) -> None:
        """Espera si el freno global está activo (bloqueo temporal de Google)."""
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 5.0))

    def _register_failure(self, err: Exception) -> None:
        self._fail_streak += 1
        delay = min(2.0 ** min(self._fail_streak + 1, 7), MAX_COOLDOWN)
        delay *= (0.75 + random.random() * 0.5) * self.retry_delay_scale
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
        logger.warning(
            "Traducción falló (%s). Pausa global de %.0f s (fallos seguidos: %d)",
            err, delay, self._fail_streak,
        )

    # ------------------------------------------------------------------ petición

    async def _request(self, text: str) -> tuple[str, str | None]:
        """Una petición al traductor con semáforo, freno global y reintentos."""
        if self._client is None or self._sem is None:
            raise RuntimeError(f"El traductor debe usarse como contexto: 'async with {type(self).__name__}(...)'")
        last_err: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._wait_cooldown()
            try:
                async with self._sem:
                    response = await self._send(self._client, text)
                self.requests_done += 1
                self.chars_sent += len(text)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise TranslationError(f"HTTP {response.status_code}")
                response.raise_for_status()
                result = self._parse(response)
                self._fail_streak = 0
                return result
            except (httpx.HTTPError, TranslationError) as err:
                last_err = err
                self._register_failure(err)
                if attempt < MAX_ATTEMPTS:
                    delay = (min(2.0 ** attempt, 20.0) + random.uniform(0.0, 1.0)) * self.retry_delay_scale
                    logger.info("Reintento %d/%d de traducción en %.1f s", attempt, MAX_ATTEMPTS, delay)
                    if delay:
                        await asyncio.sleep(delay)
        raise TranslationError(f"No se pudo traducir tras {MAX_ATTEMPTS} intentos: {last_err}")

    async def _send(self, client: httpx.AsyncClient, text: str) -> httpx.Response:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, response: httpx.Response) -> tuple[str, str | None]:  # pragma: no cover
        raise NotImplementedError


class GoogleWebTranslator(BaseTranslator):
    """Cliente del traductor web de Google (gratis, sin API key)."""

    name = "google-web"

    async def _send(self, client: httpx.AsyncClient, text: str) -> httpx.Response:
        payload = {
            "client": "gtx",
            "sl": self.source_lang,
            "tl": self.target_lang,
            "dt": "t",
            "ie": "UTF-8",
            "oe": "UTF-8",
            "q": text,
        }
        return await client.post(ENDPOINT, data=payload)

    def _parse(self, response: httpx.Response) -> tuple[str, str | None]:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise TranslationError("respuesta no JSON (posible bloqueo temporal de Google)")
        try:
            data = response.json()
        except json.JSONDecodeError as err:
            raise TranslationError("JSON inválido del traductor") from err
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            raise TranslationError("respuesta inesperada del traductor")
        translated = "".join(
            segment[0]
            for segment in data[0]
            if isinstance(segment, list) and segment and isinstance(segment[0], str)
        )
        detected = data[2] if len(data) > 2 and isinstance(data[2], str) else None
        return translated, detected


def create_translator(target_lang: str, *, concurrency: int | None = None) -> BaseTranslator:
    """Crea el traductor configurado (web gratuito o API oficial de Google)."""
    import config  # import local para evitar dependencias circulares

    workers = max(1, int(concurrency or config.TRANSLATE_CONCURRENCY))
    backend = (config.TRANSLATOR_BACKEND or "web").strip().lower()
    if backend in ("api", "gcp", "google-api", "official"):
        from translator_api import GoogleApiTranslator

        if not config.GOOGLE_API_KEY:
            logger.warning(
                "TRANSLATOR_BACKEND=%s pero falta GOOGLE_API_KEY; se usa el endpoint web",
                backend,
            )
            return GoogleWebTranslator(target_lang, concurrency=workers)
        return GoogleApiTranslator(target_lang, api_key=config.GOOGLE_API_KEY, concurrency=workers)
    return GoogleWebTranslator(target_lang, concurrency=workers)
