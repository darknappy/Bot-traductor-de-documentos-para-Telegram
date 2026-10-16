"""Traducción con servicios gratuitos (Google, Microsoft y DeepL) y API oficial.

**Backend web** (por defecto, gratis y sin API key): usa las mismas llamadas
que hacen los traductores en el navegador.

* **Google (método nuevo)** (``translate-pa.googleapis.com/v1/translate`` y
  ``/v1/translateHtml``, el mismo que usa la web actual de Google Translate y
  el plugin de Calibre «Ebook Translator») es el motor principal: aguanta IPs
  de datacenter sin devolver 429 y admite **lotes enteros de textos en una
  sola petición**. Si falla, se vuelve temporalmente a los endpoints clásicos
  (``translate_a/single`` y variantes) hasta que se recupera.
* **Microsoft** (``edge.microsoft.com/translate/translatetext``, el traductor
  de Edge/Bing) es un proveedor **independiente**: no comparte cuota con Google,
  así que sigue traduciendo aunque Google devuelva 429 desde la IP del servidor.
  En modo ``auto`` (el de por defecto) es el relevo cuando Google se bloquea.
* **DeepL** gratuito (``oneshot-free.www.deepl.com``) es el motor de **más
  calidad**, pero el más delicado: va de petición en petición con pausa (como
  su web) y, si se abusa, puede devolver el texto sin traducir; el bot detecta
  esa respuesta «eco» y la trata como fallo para que el relevo siga.
* Cada fallo extiende un "tiempo de espera global" que todas las tareas
  respetan (freno automático) y se reintenta con espera exponencial; si el
  servicio manda la cabecera ``Retry-After`` se respeta.
* Los textos se agrupan en **lotes** para reducir el número de peticiones
  (clave con novelas de +1000 capítulos).

El **motor inicial** se elige por usuario (``users.get_engine``): ``auto``
(Google primero y relevo automático), ``google``, ``microsoft`` o ``deepl``.
Con ``TRANSLATOR_BACKEND`` se fija el valor por defecto del servidor.

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
import uuid
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

# Endpoints públicos equivalentes del traductor web. Si uno empieza a limitar
# (HTTP 429) se rota al siguiente. El formato de respuesta cambia entre los
# endpoints "single" (client=gtx) y "legacy" (client=dict-chrome-ex, via GET).
WEB_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("https://translate.googleapis.com/translate_a/single", "single"),
    ("https://clients5.google.com/translate_a/single", "single"),
    ("https://translate.google.com/translate_a/single", "single"),
    ("https://clients5.google.com/translate_a/t", "legacy"),
    ("https://translate.google.com/translate_a/t", "legacy"),
)
# Nombre clásico del primer endpoint (por compatibilidad).
ENDPOINT = WEB_ENDPOINTS[0][0]

# Los endpoints "legacy" viajan por GET (el texto va en la URL, ~3 KB máximo).
LEGACY_MAX_CHARS = 2500

# Traductor gratuito de Microsoft (el de Edge/Bing): sin API key y con límites
# independientes de los de Google, así que sirve de relevo cuando Google limita
# la IP (muy habitual desde servidores como Render).
MICROSOFT_ENDPOINT = "https://edge.microsoft.com/translate/translatetext"

# --- Google, método «nuevo» (el de la web actual y del plugin de Calibre) -------
# Es un servicio distinto de translate_a/*: devuelve 200 de forma estable
# (incluso desde IPs de datacenter) y translateHtml acepta la lista completa de
# textos en una sola petición. Estas claves son públicas (viajan en el navegador).
GOOGLE_NEW_ENDPOINT = "https://translate-pa.googleapis.com/v1/translate"
GOOGLE_NEW_BATCH_ENDPOINT = "https://translate-pa.googleapis.com/v1/translateHtml"
GOOGLE_NEW_API_KEY = "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA"
GOOGLE_NEW_BATCH_API_KEY = "AIzaSyATBXajvzQLTDHEQbcpq0Ihe0vWDHmO520"
GOOGLE_NEW_BATCH_HEADERS = {
    "Content-Type": "application/json+protobuf",
    "X-Goog-Api-Key": GOOGLE_NEW_BATCH_API_KEY,
}
# El texto de /v1/translate viaja en la URL (GET): se reserva para textos cortos.
GOOGLE_NEW_GET_MAX_CHARS = 1500
# Tope de caracteres por lote del método nuevo (probado con holgura: 100 textos).
GOOGLE_NEW_MAX_BATCH_CHARS = 12000
# Cuando el método nuevo falla se vuelve al clásico; se reintenta con espera
# creciente (hasta 30 min) por si el servicio cambia.
GOOGLE_NEW_RETRY_SECONDS = 120.0
GOOGLE_NEW_MAX_DOWN = 1800.0

# --- DeepL gratuito (web, sin API key) ------------------------------------------
DEEPL_ENDPOINT = "https://oneshot-free.www.deepl.com/v1/storefront/translate"
DEEPL_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.deepl.com",
    "Referer": "https://www.deepl.com/",
}
# DeepL va de petición en petición con pausa (como su web); abusar provoca que
# responda con el texto original sin traducir.
DEEPL_MIN_INTERVAL = 1.1
DEEPL_MAX_INTERVAL = 10.0
DEEPL_MAX_BATCH_CHARS = 3000
# Códigos propios de DeepL para los idiomas del catálogo (en minúscula, como
# en su web). Con un código que no reconoce, DeepL **devuelve el texto original
# sin dar ningún error**: por eso solo se ofrecen los idiomas de esta tabla y
# además se detecta la respuesta «eco» para tratarla como fallo.
DEEPL_LANGS: dict[str, str] = {
    "es": "es-ES",
    "en": "en",
    "pt": "pt",
    "fr": "fr",
    "de": "de",
    "it": "it",
    "nl": "nl",
    "ru": "ru",
    "uk": "uk",
    "pl": "pl",
    "cs": "cs",
    "ro": "ro",
    "sv": "sv",
    "da": "da",
    "no": "nb",
    "fi": "fi",
    "el": "el",
    "tr": "tr",
    "ar": "ar",
    "he": "he",
    "hi": "hi",
    "th": "th",
    "vi": "vi",
    "id": "id",
    "ja": "ja",
    "ko": "ko",
    "zh": "zh-Hans",
}


def _deepl_code(target_lang: str | None) -> str | None:
    """Código de DeepL para un idioma del bot (o ``None`` si no lo soporta)."""
    code = (target_lang or "").strip().lower().replace("_", "-")
    if code in DEEPL_LANGS:
        return DEEPL_LANGS[code]
    return DEEPL_LANGS.get(code.split("-")[0])


def deepl_supported(target_lang: str | None) -> bool:
    """¿DeepL gratuito traduce hacia este idioma? (solo los de la tabla)."""
    return _deepl_code(target_lang) is not None


# Relevo automático entre proveedores (TRANSLATOR_BACKEND=auto).
AUTO_BLOCK_SECONDS = 120.0      # lo que se aparta un proveedor al ceder el turno
AUTO_MAX_BLOCK_SECONDS = 600.0  # tope del aparte (después se vuelve a probar)
AUTO_ABORT_STREAK = 4           # fallos seguidos que hacen ceder el turno
AUTO_ABORT_COOLDOWN = 20.0      # o un freno global mayor que estos segundos

# --- Indicador del motor que traduce ahora mismo (progreso y /status) ----------
ENGINE_LABELS: dict[str, str] = {
    "google-web": "Google",
    "microsoft-web": "Microsoft",
    "deepl-web": "DeepL",
    "google-api": "Google Cloud (API)",
}
_engine_state: dict = {"name": None, "label": "", "at": 0.0}


def record_engine(name: str) -> None:
    """Apunta qué motor acaba de traducir bien (Google, Microsoft…)."""
    _engine_state["name"] = name
    _engine_state["label"] = ENGINE_LABELS.get(name, name)
    _engine_state["at"] = time.monotonic()


def active_engine(max_age: float = 300.0) -> tuple[str, str] | None:
    """Motor en uso ahora mismo, o ``None`` si lleva un rato sin traducir."""
    if not _engine_state["name"] or time.monotonic() - _engine_state["at"] > max_age:
        return None
    return str(_engine_state["name"]), str(_engine_state["label"])

# El endpoint web admite bastantes caracteres, pero se es conservador para
# reducir errores y truncamientos.
MAX_CHUNK_CHARS = 4000
# Tamaño máximo de un lote (varios párrafos separados por \n).
MAX_BATCH_CHARS = 3600
MAX_ATTEMPTS = 8
MAX_COOLDOWN = 300.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Ritmo por defecto del endpoint web: segundos mínimos entre peticiones y tope
# cuando Google empieza a limitar. Desde IP de datacenter (Render, VPS...) los
# bursts de peticiones se castigan mucho antes, así que se empieza despacio.
DEFAULT_MIN_INTERVAL = 0.6
DEFAULT_MAX_INTERVAL = 8.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…。！？;:])\s+")


def _retry_after_seconds(response: httpx.Response | None) -> float:
    """Segundos que Google pide esperar (cabecera Retry-After), si la envía."""
    if response is None:
        return 0.0
    value = (response.headers.get("retry-after") or "").strip()
    if not value:
        return 0.0
    try:
        return min(max(float(value), 0.0), 300.0)
    except ValueError:
        return 0.0  # las fechas HTTP se ignoran


class TranslationError(RuntimeError):
    """Fallo irrecuperable al hablar con el traductor."""


class ProviderBlockedError(TranslationError):
    """El proveedor se está bloqueando (429 insistente): cede el turno al relevo."""


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
    #: ¿El backend acepta una lista de textos en una sola petición? Los motores
    #: con lote nativo (Google nuevo, DeepL) también aceptan textos multilínea.
    supports_native_batch = False

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
        min_interval: float = 0.0,
        max_interval: float = 0.0,
        max_attempts: int = MAX_ATTEMPTS,
        max_cooldown: float = MAX_COOLDOWN,
    ) -> None:
        self.target_lang = target_lang
        self.source_lang = source_lang
        self.concurrency = max(1, int(concurrency))
        self.timeout = timeout
        self.max_chunk_chars = max_chunk_chars
        self.max_batch_chars = max_batch_chars
        self.retry_delay_scale = max(0.0, float(retry_delay_scale))
        self.min_interval = max(0.0, float(min_interval))
        self.max_interval = max(self.min_interval, float(max_interval))
        self.max_attempts = max(1, int(max_attempts))
        self.max_cooldown = max(1.0, float(max_cooldown))
        self.detected_lang: str | None = None
        self.requests_done = 0
        self.chars_sent = 0
        self._transport = transport
        self._sem: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None
        # Freno global: si el proveedor limita, todas las tareas esperan.
        self._cooldown_until = 0.0
        self._fail_streak = 0
        # Ritmo adaptativo (0 = sin espaciado; solo lo usan los backends web).
        self._interval = self.min_interval
        self._pace_next = 0.0
        self._pace_lock: asyncio.Lock | None = None
        # Gancho del relevo automático (AutoTranslator): si devuelve True se
        # abandona el intento y el proveedor de relevo toma el control.
        self.abort_check: Callable[[], bool] | None = None

    # ------------------------------------------------------------------ ciclo de vida

    async def __aenter__(self) -> "BaseTranslator":
        self._sem = asyncio.Semaphore(self.concurrency)
        self._pace_lock = asyncio.Lock()
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
        self._pace_lock = None

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
            elif "\n" in core and not self.supports_native_batch:
                singles.append(index)  # multilínea: el lote clásico no puede separarlo
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

    def _batch_limit(self) -> int:
        """Caracteres máximos por lote (los motores con lote nativo lo amplían)."""
        return self.max_batch_chars

    def _plan_batches(self, texts: list[str], indexes: list[int]) -> list[list[int]]:
        """Agrupa índices originales en lotes que quepan en una sola petición."""
        groups: list[list[int]] = []
        current: list[int] = []
        length = 0
        limit = self._batch_limit()
        for index in indexes:
            size = len(texts[index].strip())
            extra = size + (1 if current else 0)
            if current and length + extra > limit:
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

    def cooldown_remaining(self) -> float:
        """Segundos que le quedan al freno global (0 si no hay freno activo)."""
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def fail_streak(self) -> int:
        """Fallos seguidos desde el último éxito (el relevo mira este dato)."""
        return self._fail_streak

    async def _wait_cooldown(self) -> None:
        """Espera si el freno global está activo (bloqueo temporal de Google)."""
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 5.0))

    async def _pace(self) -> None:
        """Espacia el inicio de las peticiones para no disparar el límite."""
        lock = self._pace_lock
        if lock is None or self._interval <= 0:
            return
        async with lock:
            now = time.monotonic()
            wait = self._pace_next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._pace_next = now + self._interval

    def _slow_down(self) -> None:
        """Baja el ritmo cuando Google empieza a limitar."""
        if self.max_interval <= 0:
            return
        new_interval = min(self.max_interval, max(self._interval * 2.0, 1.0))
        if new_interval > self._interval + 0.001:
            self._interval = new_interval
            logger.warning(
                "Google está limitando: se espacian las peticiones a %.1f s", new_interval
            )

    def _speed_up(self) -> None:
        """Recupera el ritmo poco a poco mientras las peticiones van bien."""
        if self.max_interval > 0 and self._interval > self.min_interval:
            self._interval = max(self.min_interval, self._interval * 0.98)

    def _on_retryable(self, response: httpx.Response | None, err: Exception) -> None:
        """Gancho al fallar una petición reintentable (los 429 bajan el ritmo)."""
        if response is not None and response.status_code == 429:
            self._slow_down()
            wait = _retry_after_seconds(response)
            if wait > 0:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait)
                logger.info("Google pidió esperar %.0f s antes de reintentar", wait)
        elif response is None and isinstance(err, httpx.HTTPError):
            # Corte de red: mejor no insistir al mismo ritmo.
            self._slow_down()

    def _register_failure(self, err: Exception) -> None:
        # Solo el primer fallo de cada "oleada" sube el contador: los fallos
        # simultáneos de otras tareas no lo inflan sin motivo.
        if time.monotonic() >= self._cooldown_until:
            self._fail_streak += 1
        delay = min(2.0 ** min(self._fail_streak + 1, 9), self.max_cooldown)
        delay *= (0.75 + random.random() * 0.5) * self.retry_delay_scale
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
        logger.warning(
            "Traducción falló (%s). Pausa global de %.0f s (fallos seguidos: %d)",
            err, delay, self._fail_streak,
        )

    # ------------------------------------------------------------------ petición

    async def _request(self, text: str) -> tuple[str, str | None]:
        """Una petición al traductor con semáforo, freno global y reintentos."""
        translations, detected = await self._request_payload(("text", text))
        return translations[0], detected

    async def _request_payload(self, payload: tuple[str, object]) -> tuple[list[str], str | None]:
        """Una petición (texto suelto o lote entero) con freno global y reintentos."""
        if self._client is None or self._sem is None:
            raise RuntimeError(f"El traductor debe usarse como contexto: 'async with {type(self).__name__}(...)'")
        last_err: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            # Con un relevo sano disponible, un bloqueo insistente cede el turno
            # ya (incluso antes de esperar el freno global) en lugar de castigar
            # la tarea con esperas de minutos.
            if self.abort_check is not None and self.abort_check():
                raise ProviderBlockedError(
                    f"{self.name} se está bloqueando; pasa el turno al traductor de relevo"
                )
            await self._wait_cooldown()
            await self._pace()
            response: httpx.Response | None = None
            try:
                async with self._sem:
                    response, kind = await self._send_payload(self._client, payload)
                self.requests_done += 1
                self.chars_sent += len(self._payload_text(payload))
                if response.status_code in RETRY_STATUSES:
                    raise TranslationError(f"HTTP {response.status_code}")
                response.raise_for_status()
                translations, detected = self._parse_payload(response, kind)
                self._fail_streak = 0
                self._speed_up()
                record_engine(self.name)
                return translations, detected
            except (httpx.HTTPError, TranslationError) as err:
                last_err = err
                # Primero el freno global (decide según la oleada de fallos) y
                # después se ajusta el ritmo con lo que haya pedido el proveedor.
                self._register_failure(err)
                self._on_retryable(response, err)
                if attempt < self.max_attempts:
                    delay = (min(2.0 ** attempt, 20.0) + random.uniform(0.0, 1.0)) * self.retry_delay_scale
                    logger.info("Reintento %d/%d de traducción en %.1f s", attempt, self.max_attempts, delay)
                    if delay:
                        await asyncio.sleep(delay)
        raise TranslationError(f"No se pudo traducir tras {self.max_attempts} intentos: {last_err}")

    def _payload_text(self, payload: tuple[str, object]) -> str:
        """Texto del payload (para contar los caracteres enviados)."""
        _kind, data = payload
        if isinstance(data, str):
            return data
        if isinstance(data, (list, tuple)):
            return "".join(str(item) for item in data)
        return str(data)

    async def _send_payload(self, client: httpx.AsyncClient, payload: tuple[str, object]) -> tuple[httpx.Response, str]:
        """Envía el payload (por defecto: un texto suelto, vía ``_send``)."""
        _kind, data = payload
        return await self._send(client, str(data))

    def _parse_payload(self, response: httpx.Response, kind: str) -> tuple[list[str], str | None]:
        """Extrae las traducciones del payload (por defecto: un solo texto)."""
        translated, detected = self._parse(response, kind)
        return [translated], detected

    def reset_backoff(self) -> None:
        """Olvida el freno global y los fallos seguidos al recuperar la prioridad."""
        self._cooldown_until = 0.0
        self._fail_streak = 0

    async def _send(self, client: httpx.AsyncClient, text: str) -> tuple[httpx.Response, str]:  # pragma: no cover
        """Envía una petición y devuelve la respuesta junto al formato usado."""
        raise NotImplementedError

    def _parse(self, response: httpx.Response, kind: str) -> tuple[str, str | None]:  # pragma: no cover
        """Extrae ``(texto traducido, idioma detectado)`` de la respuesta."""
        raise NotImplementedError


class GoogleWebTranslator(BaseTranslator):
    """Cliente del traductor web de Google (gratis, sin API key).

    Usa primero el **método nuevo** (``translate-pa``, el de la web actual):
    aguanta IPs de datacenter y traduce lotes enteros en una petición. Si
    falla, vuelve temporalmente a los endpoints clásicos (``translate_a/*``)
    rotando entre ellos cuando limitan, y se reintenta el método nuevo con
    espera creciente.
    """

    name = "google-web"
    supports_native_batch = True

    def __init__(self, target_lang: str, *, use_new: bool = True, **kwargs) -> None:
        kwargs.setdefault("min_interval", DEFAULT_MIN_INTERVAL)
        kwargs.setdefault("max_interval", DEFAULT_MAX_INTERVAL)
        kwargs.setdefault("max_batch_chars", GOOGLE_NEW_MAX_BATCH_CHARS)
        super().__init__(target_lang, **kwargs)
        self._endpoint_index = 0
        self._use_new = use_new
        self._new_fails = 0
        self._new_down_until = 0.0

    # ------------------------------------------------------------------ método nuevo

    def _new_available(self) -> bool:
        return self._use_new and time.monotonic() >= self._new_down_until

    def _new_failed(self, reason: object) -> None:
        """El método nuevo falló: se usa el clásico un rato (espera creciente)."""
        self._new_fails += 1
        wait = min(GOOGLE_NEW_RETRY_SECONDS * (2.0 ** (self._new_fails - 1)), GOOGLE_NEW_MAX_DOWN)
        self._new_down_until = time.monotonic() + wait
        logger.warning(
            "El método nuevo de Google falló (%s); se usa el clásico %.0f s", reason, wait
        )

    def _new_ok(self) -> None:
        self._new_fails = 0
        self._new_down_until = 0.0

    def _batch_limit(self) -> int:
        # El lote grande solo vale con el método nuevo (el clásico une con \n).
        if self._new_available():
            return self.max_batch_chars
        return min(self.max_batch_chars, MAX_BATCH_CHARS)

    async def _send_payload(self, client: httpx.AsyncClient, payload: tuple[str, object]) -> tuple[httpx.Response, str]:
        kind, data = payload
        if self._new_available() and not (kind == "text" and len(str(data)) > GOOGLE_NEW_GET_MAX_CHARS):
            fmt = ""
            try:
                if kind == "batch":
                    fmt = "new-batch"
                    response = await client.post(
                        GOOGLE_NEW_BATCH_ENDPOINT,
                        content=json.dumps([[list(data), self.source_lang, self.target_lang], "wt_lib"]),
                        headers=GOOGLE_NEW_BATCH_HEADERS,
                    )
                else:
                    fmt = "new-single"
                    response = await client.get(
                        GOOGLE_NEW_ENDPOINT,
                        params={
                            "params.client": "gtx",
                            "query.source_language": self.source_lang,
                            "query.target_language": self.target_lang,
                            "query.display_language": "en-US",
                            "data_types": "TRANSLATION",
                            "key": GOOGLE_NEW_API_KEY,
                            "query.text": str(data),
                        },
                    )
            except httpx.HTTPError as err:
                self._new_failed(err)
            else:
                if response.status_code < 400:
                    return response, fmt
                self._new_failed(f"HTTP {response.status_code}")
        if kind == "batch":
            # Respaldo clásico: los textos viajan unidos por \n y luego se
            # reparten por líneas (lo hace ``_translate_group``).
            merged = "\n".join(str(item).strip() for item in data)
            return await self._send(client, merged)
        return await super()._send_payload(client, payload)

    def _parse_payload(self, response: httpx.Response, kind: str) -> tuple[list[str], str | None]:
        if kind.startswith("new-"):
            try:
                translations, detected = self._parse_new(response, kind)
            except TranslationError as err:
                self._new_failed(f"respuesta inválida: {err}")
                raise
            self._new_ok()
            return translations, detected
        return super()._parse_payload(response, kind)

    def _parse_new(self, response: httpx.Response, kind: str) -> tuple[list[str], str | None]:
        try:
            data = response.json()
        except json.JSONDecodeError as err:
            raise TranslationError("respuesta no JSON del método nuevo de Google") from err
        if kind == "new-batch":
            # Formato: [["traducción 1", "traducción 2", …], ["en", …]]
            if not isinstance(data, list) or not data or not isinstance(data[0], list):
                raise TranslationError("respuesta inesperada del método nuevo de Google")
            translations = [item if isinstance(item, str) else "" for item in data[0]]
            detected = None
            if len(data) > 1 and isinstance(data[1], list) and data[1] and isinstance(data[1][0], str):
                detected = data[1][0] or None
            return translations, detected
        # Formato: {"translation": "…", "sourceLanguage": "en"}
        if not isinstance(data, dict) or not isinstance(data.get("translation"), str):
            raise TranslationError("respuesta inesperada del método nuevo de Google")
        detected = data.get("sourceLanguage")
        return [data["translation"]], (detected if isinstance(detected, str) else None)

    async def _translate_group(self, texts: list[str]) -> list[str]:
        """El método nuevo traduce la lista completa en una sola petición."""
        if len(texts) == 1:
            return [await self.translate(texts[0])]
        if self._new_available():
            cores = [text.strip() for text in texts]
            try:
                translations, detected = await self._request_payload(("batch", cores))
            except TranslationError as err:
                logger.debug("Lote nativo de Google falló (%s); se usa el clásico", err)
            else:
                if detected and self.detected_lang is None:
                    self.detected_lang = detected
                if len(translations) == len(texts) and all(part.strip() for part in translations):
                    return [
                        _reattach(original, part.strip())
                        for original, part in zip(texts, translations)
                    ]
                if len(translations) == 1:
                    # Respaldo clásico: los textos viajaron unidos por \n; si
                    # vuelven exactamente en ese número de líneas, se reparten.
                    parts = translations[0].split("\n")
                    if len(parts) == len(texts) and all(part.strip() for part in parts):
                        return [
                            _reattach(original, part.strip())
                            for original, part in zip(texts, parts)
                        ]
                self._new_failed(f"lote incompleto ({len(translations)} de {len(texts)})")
        return await super()._translate_group(texts)

    # ------------------------------------------------------------------ endpoints

    def _pick_endpoint(self, text: str) -> tuple[str, str]:
        """Elige el endpoint actual, saltando los GET si el texto es demasiado largo."""
        length = len(text)
        for offset in range(len(WEB_ENDPOINTS)):
            index = (self._endpoint_index + offset) % len(WEB_ENDPOINTS)
            url, kind = WEB_ENDPOINTS[index]
            if kind == "legacy" and length > LEGACY_MAX_CHARS:
                continue  # el texto viaja en la URL: no admite lotes grandes
            return url, kind
        return WEB_ENDPOINTS[0]

    def _rotate_endpoint(self, status: int | None) -> None:
        self._endpoint_index = (self._endpoint_index + 1) % len(WEB_ENDPOINTS)
        logger.info(
            "Cambiando de endpoint de Google (%s) → %s",
            f"HTTP {status}" if status else "error de red",
            WEB_ENDPOINTS[self._endpoint_index][0],
        )

    def _on_retryable(self, response: httpx.Response | None, err: Exception) -> None:
        super()._on_retryable(response, err)
        status = response.status_code if response is not None else None
        if status in RETRY_STATUSES or isinstance(err, httpx.HTTPError):
            self._rotate_endpoint(status)

    # ------------------------------------------------------------------ peticiones

    async def _send(self, client: httpx.AsyncClient, text: str) -> tuple[httpx.Response, str]:
        url, kind = self._pick_endpoint(text)
        if kind == "legacy":
            params = {
                "client": "dict-chrome-ex",
                "sl": self.source_lang,
                "tl": self.target_lang,
                "q": text,
            }
            return await client.get(url, params=params), kind
        payload = {
            "client": "gtx",
            "sl": self.source_lang,
            "tl": self.target_lang,
            "dt": "t",
            "ie": "UTF-8",
            "oe": "UTF-8",
            "q": text,
        }
        return await client.post(url, data=payload), kind

    def _parse(self, response: httpx.Response, kind: str) -> tuple[str, str | None]:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise TranslationError("respuesta no JSON (posible bloqueo temporal de Google)")
        try:
            data = response.json()
        except json.JSONDecodeError as err:
            raise TranslationError("JSON inválido del traductor") from err
        if not isinstance(data, list) or not data:
            raise TranslationError("respuesta inesperada del traductor")
        if kind == "legacy":
            # Formato compacto: [["texto traducido", "idioma original"]]
            first = data[0]
            if isinstance(first, list) and first and isinstance(first[0], str):
                detected = first[1] if len(first) > 1 and isinstance(first[1], str) else None
                return first[0], detected
            raise TranslationError("respuesta inesperada del traductor")
        if not isinstance(data[0], list):
            raise TranslationError("respuesta inesperada del traductor")
        translated = "".join(
            segment[0]
            for segment in data[0]
            if isinstance(segment, list) and segment and isinstance(segment[0], str)
        )
        detected = data[2] if len(data) > 2 and isinstance(data[2], str) else None
        return translated, detected


class MicrosoftWebTranslator(BaseTranslator):
    """Cliente del traductor gratuito de Microsoft Edge/Bing (sin API key).

    Es un proveedor **independiente** del de Google: sus límites no tienen nada
    que ver, así que sigue respondiendo aunque Google bloquee la IP del
    servidor. En modo ``auto`` actúa de relevo.
    """

    name = "microsoft-web"

    def __init__(self, target_lang: str, **kwargs) -> None:
        kwargs.setdefault("min_interval", DEFAULT_MIN_INTERVAL)
        kwargs.setdefault("max_interval", DEFAULT_MAX_INTERVAL)
        super().__init__(target_lang, **kwargs)

    async def _send(self, client: httpx.AsyncClient, text: str) -> tuple[httpx.Response, str]:
        params = {"isEnterpriseClient": "false", "to": self.target_lang}
        if self.source_lang and self.source_lang != "auto":
            params["from"] = self.source_lang
        # El cuerpo es una lista JSON con el texto (así lo llama el navegador).
        return await client.post(MICROSOFT_ENDPOINT, params=params, json=[text]), "ms"

    def _parse(self, response: httpx.Response, kind: str) -> tuple[str, str | None]:
        # Ojo: este endpoint sirve JSON con Content-Type "text/plain", así que
        # no se puede exigir "application/json"; se valida el cuerpo directamente.
        try:
            data = response.json()
        except json.JSONDecodeError as err:
            raise TranslationError("respuesta no JSON del traductor de Microsoft (posible bloqueo temporal)") from err
        item = data[0] if isinstance(data, list) and data else None
        if not isinstance(item, dict):
            raise TranslationError("respuesta inesperada del traductor de Microsoft")
        detected = None
        info = item.get("detectedLanguage")
        if isinstance(info, dict) and isinstance(info.get("language"), str):
            detected = info["language"]
        translations = item.get("translations")
        if isinstance(translations, list) and translations and isinstance(translations[0], dict):
            translated = translations[0].get("text")
            if isinstance(translated, str):
                return translated, detected
        raise TranslationError("respuesta inesperada del traductor de Microsoft")


class DeepLWebTranslator(BaseTranslator):
    """Cliente del traductor DeepL gratuito (web, sin API key).

    Es el motor de **más calidad** pero el más delicado: DeepL va de petición
    en petición con pausa (como su web) y, si se abusa, responde con el texto
    original **sin dar error**. Solo se ofrece para los idiomas de
    ``DEEPL_LANGS``; además, si todo un lote vuelve idéntico al original se
    trata como fallo para que el relevo siga traduciendo.
    """

    name = "deepl-web"
    supports_native_batch = True

    def __init__(self, target_lang: str, **kwargs) -> None:
        code = _deepl_code(target_lang)
        if code is None:
            raise TranslationError(f"DeepL no traduce a «{target_lang}»")
        self.deepl_target = code
        kwargs.setdefault("min_interval", DEEPL_MIN_INTERVAL)
        kwargs.setdefault("max_interval", DEEPL_MAX_INTERVAL)
        kwargs.setdefault("max_batch_chars", DEEPL_MAX_BATCH_CHARS)
        kwargs.setdefault("concurrency", 1)  # una petición a la vez, como su web
        super().__init__(target_lang, **kwargs)

    # ------------------------------------------------------------------ peticiones

    def _body(self, texts: list[str]) -> dict:
        """Cuerpo que manda la web de DeepL (una petición por lote)."""
        return {
            "app_information": {
                "app_build": "Chrome",
                "app_version": "any",
                "instance_id": str(uuid.uuid4()),
                "os": "Windows",
                "os_version": "any",
            },
            "language_model": "next-gen",
            "source_lang": self.source_lang or "auto",
            "text": list(texts),
            "usage_type": "Translate",
            "target_lang": self.deepl_target,
        }

    async def _send_payload(self, client: httpx.AsyncClient, payload: tuple[str, object]) -> tuple[httpx.Response, str]:
        kind, data = payload
        if kind == "batch":
            body = json.dumps(self._body(list(data)), separators=(",", ":"))
            return await client.post(DEEPL_ENDPOINT, content=body, headers=DEEPL_HEADERS), "deepl-batch"
        return await super()._send_payload(client, payload)

    def _parse_payload(self, response: httpx.Response, kind: str) -> tuple[list[str], str | None]:
        if kind == "deepl-batch":
            items = self._items(response)
            detected = next((lang for _text, lang in items if lang), None)
            return [text for text, _lang in items], detected
        return super()._parse_payload(response, kind)

    async def _send(self, client: httpx.AsyncClient, text: str) -> tuple[httpx.Response, str]:
        body = json.dumps(self._body([text]), separators=(",", ":"))
        return await client.post(DEEPL_ENDPOINT, content=body, headers=DEEPL_HEADERS), "deepl"

    def _items(self, response: httpx.Response) -> list[tuple[str, str | None]]:
        try:
            data = response.json()
        except json.JSONDecodeError as err:
            raise TranslationError("respuesta no JSON de DeepL (posible bloqueo temporal)") from err
        translations = data.get("translations") if isinstance(data, dict) else None
        if not isinstance(translations, list) or not translations:
            raise TranslationError("respuesta inesperada de DeepL")
        items: list[tuple[str, str | None]] = []
        for item in translations:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise TranslationError("respuesta inesperada de DeepL")
            detected = item.get("detected_source_language")
            items.append((item["text"], detected if isinstance(detected, str) else None))
        return items

    def _parse(self, response: httpx.Response, kind: str) -> tuple[str, str | None]:
        return self._items(response)[0]

    # ------------------------------------------------------------------ guardas

    @staticmethod
    def _echoes(originals: list[str], translations: list[str]) -> bool:
        """¿Todo un lote volvió igual? (DeepL lo hace al limitar o fallar)."""
        if not any(len(text) >= 40 for text in originals):
            return False  # frases cortas o nombres pueden coincidir sin más
        return all(
            original.strip() == translated.strip()
            for original, translated in zip(originals, translations)
        )

    async def _translate_group(self, texts: list[str]) -> list[str]:
        if len(texts) == 1:
            return [await self.translate(texts[0])]
        cores = [text.strip() for text in texts]
        translations, detected = await self._request_payload(("batch", cores))
        if detected and self.detected_lang is None:
            self.detected_lang = detected
        if len(translations) != len(texts):
            raise TranslationError("DeepL devolvió un lote incompleto")
        if self._echoes(cores, translations):
            raise TranslationError("DeepL devolvió el lote sin traducir (posible límite temporal)")
        return [
            _reattach(original, part.strip())
            for original, part in zip(texts, translations)
        ]

    async def translate(self, text: str) -> str:
        result = await super().translate(text)
        if len(text.strip()) >= 40 and result.strip() == text.strip():
            raise TranslationError("DeepL devolvió el texto sin traducir (posible límite temporal)")
        return result


class AutoTranslator:
    """Traduce con un proveedor y, si se bloquea, continúa con el relevo.

    El primer traductor de la lista es el **motor principal** (Google en el modo
    de por defecto) y mantiene la prioridad incluso con frenos pequeños: solo
    cede el turno cuando el bloqueo es serio (varios 429 seguidos o un freno
    global largo) y hay otro proveedor sano. Entonces se aparta unos minutos
    (con castigo creciente) y el relevo (Microsoft, DeepL) continúa; pasado ese
    tiempo vuelve a tener una oportunidad con las pilas frescas.
    Es el modo ``TRANSLATOR_BACKEND=auto`` (el de por defecto).
    """

    name = "auto"

    def __init__(
        self,
        translators: list[BaseTranslator],
        *,
        block_seconds: float = AUTO_BLOCK_SECONDS,
        max_block_seconds: float = AUTO_MAX_BLOCK_SECONDS,
    ) -> None:
        if not translators:
            raise ValueError("AutoTranslator necesita al menos un traductor")
        self._translators = list(translators)
        self._blocked_until = [0.0] * len(self._translators)
        self._block_penalty = [block_seconds] * len(self._translators)
        self.max_block_seconds = max_block_seconds
        self.target_lang = self._translators[0].target_lang

    # ------------------------------------------------------------------ estado

    @property
    def detected_lang(self) -> str | None:
        for translator in self._translators:
            if translator.detected_lang:
                return translator.detected_lang
        return None

    def _healthy(self, index: int) -> bool:
        return time.monotonic() >= self._blocked_until[index]

    def _stand_down(self, index: int, reason: object) -> None:
        """Aparta temporalmente a un proveedor y da el turno al relevo."""
        translator = self._translators[index]
        delay = self._block_penalty[index]
        self._block_penalty[index] = min(delay * 2.0, self.max_block_seconds)
        self._blocked_until[index] = max(self._blocked_until[index], time.monotonic() + delay)
        logger.warning(
            "Traductor %s apartado %.0f s (%s); el relevo continúa la traducción",
            translator.name, delay, reason,
        )

    def _stand_up(self, index: int) -> None:
        self._blocked_until[index] = 0.0
        self._block_penalty[index] = AUTO_BLOCK_SECONDS

    def _should_abort(self, index: int) -> bool:
        """¿Merece la pena seguir esperando a este proveedor teniendo relevo?"""
        if not any(
            other != index and self._healthy(other)
            for other in range(len(self._translators))
        ):
            return False
        translator = self._translators[index]
        return (
            translator.fail_streak >= AUTO_ABORT_STREAK
            or translator.cooldown_remaining() > AUTO_ABORT_COOLDOWN
        )

    def _order(self) -> list[int]:
        """Proveedores por preferencia: el principal primero mientras esté sano.

        Un freno pequeño **no** le quita la prioridad: se le da el turno igual y
        es ``_should_abort`` quien decide ceder cuando el bloqueo es serio. Así
        Google sigue siendo el motor por defecto y Microsoft/DeepL solo actúan
        de relevo.
        """
        now = time.monotonic()
        return sorted(
            range(len(self._translators)),
            key=lambda index: (self._blocked_until[index] > now, index),
        )

    # ------------------------------------------------------------------ API pública

    async def _dispatch(self, method: str, argument):
        errors: list[str] = []
        for index in self._order():
            # Si terminó su aparte, vuelve a tener una oportunidad con las pilas
            # frescas (sin arrastrar el contador de fallos de la oleada anterior).
            if 0.0 < self._blocked_until[index] <= time.monotonic():
                self._blocked_until[index] = 0.0
                self._translators[index].reset_backoff()
            translator = self._translators[index]
            try:
                result = await getattr(translator, method)(argument)
            except TranslationError as err:
                errors.append(f"{translator.name}: {err}")
                self._stand_down(index, err)
                continue
            self._stand_up(index)
            return result
        raise TranslationError(
            "Ningún traductor gratuito pudo completar la traducción ("
            + " · ".join(errors) + ")"
        )

    async def translate(self, text: str) -> str:
        return await self._dispatch("translate", text)

    async def translate_many(self, texts: list[str]) -> list[str]:
        return await self._dispatch("translate_many", texts)

    async def translate_segments(self, texts: list[str]) -> list[str]:
        return await self._dispatch("translate_many", texts)

    async def detect(self, text: str) -> str | None:
        return await self._dispatch("detect", text)

    # ------------------------------------------------------------------ ciclo de vida

    async def __aenter__(self) -> "AutoTranslator":
        for index, translator in enumerate(self._translators):
            await translator.__aenter__()
            translator.abort_check = (lambda i=index: self._should_abort(i))
        return self

    async def __aexit__(self, *exc) -> None:
        for translator in self._translators:
            translator.abort_check = None
            await translator.__aexit__(*exc)


# Motores que puede elegir el usuario (users.get_engine) y sus alias.
ENGINE_NAMES: dict[str, str] = {
    "auto": "Automático",
    "google": "Google",
    "microsoft": "Microsoft",
    "deepl": "DeepL",
    "api": "Google Cloud (API)",
}

_ENGINE_ALIASES: dict[str, str] = {
    "": "auto",
    "auto": "auto",
    "automatic": "auto",
    "fallback": "auto",
    "google": "google",
    "web": "google",
    "free": "google",
    "google-web": "google",
    "microsoft": "microsoft",
    "ms": "microsoft",
    "edge": "microsoft",
    "bing": "microsoft",
    "microsoft-web": "microsoft",
    "deepl": "deepl",
    "deepl-web": "deepl",
    "api": "api",
    "gcp": "api",
    "google-api": "api",
    "official": "api",
}


def normalize_engine(name: str | None) -> str | None:
    """Convierte el nombre de un motor en su código (o ``None`` si no existe)."""
    if name is None:
        return None
    return _ENGINE_ALIASES.get(str(name).strip().lower())


def engine_label(name: str | None) -> str:
    """Nombre legible de un motor («Automático», «Google», «Microsoft», «DeepL»)."""
    code = normalize_engine(name) or "auto"
    return ENGINE_NAMES.get(code, code)


def create_translator(
    target_lang: str,
    *,
    concurrency: int | None = None,
    engine: str | None = None,
):
    """Crea el traductor para el idioma y el motor elegidos.

    * ``auto`` (por defecto): Google primero y, si se bloquea, Microsoft y
      DeepL de relevo.
    * ``google``/``web``: solo el método web gratuito de Google.
    * ``microsoft``/``edge``: solo el traductor gratuito de Microsoft.
    * ``deepl``: solo DeepL gratuito (mejor calidad, más pausado).
    * ``api``: API oficial de Google Cloud (requiere ``GOOGLE_API_KEY``).

    ``engine`` es la preferencia por usuario (``users.get_engine``); con ``None``
    se usa ``TRANSLATOR_BACKEND``, el valor por defecto del servidor.
    """
    import config  # import local para evitar dependencias circulares

    workers = max(1, int(concurrency or config.TRANSLATE_CONCURRENCY))
    raw = engine if engine is not None else config.TRANSLATOR_BACKEND
    backend = normalize_engine(raw)
    if backend is None:
        logger.warning("Motor «%s» desconocido; se usa el modo automático", raw)
        backend = "auto"
    web_kwargs = {
        "concurrency": workers,
        "min_interval": config.WEB_MIN_INTERVAL,
        "max_interval": config.WEB_MAX_INTERVAL,
    }

    if backend == "api":
        from translator_api import GoogleApiTranslator

        if not config.GOOGLE_API_KEY:
            logger.warning(
                "Motor «api» pero falta GOOGLE_API_KEY; se usan los servicios gratuitos"
            )
        else:
            return GoogleApiTranslator(target_lang, api_key=config.GOOGLE_API_KEY, concurrency=workers)

    if backend == "microsoft":
        return MicrosoftWebTranslator(target_lang, **web_kwargs)

    if backend == "google":
        return GoogleWebTranslator(target_lang, **web_kwargs)

    if backend == "deepl":
        if deepl_supported(target_lang):
            # DeepL va de una en una con pausa propia: no se le pasa la
            # concurrencia general ni el ritmo de Google.
            return DeepLWebTranslator(target_lang)
        logger.warning("DeepL no traduce a «%s»; se usa el modo automático", target_lang)

    # Modo "auto": Google primero y Microsoft/DeepL de relevo. Con relevo
    # disponible ninguno necesita tanta paciencia: cuando uno se atasca, cede
    # el turno en segundos en lugar de castigar la tarea con esperas de minutos.
    google = GoogleWebTranslator(target_lang, **web_kwargs)
    google.max_attempts = 5
    google.max_cooldown = 45.0
    microsoft = MicrosoftWebTranslator(target_lang, **web_kwargs)
    microsoft.max_attempts = 8
    microsoft.max_cooldown = 120.0
    chain: list[BaseTranslator] = [google, microsoft]
    if deepl_supported(target_lang):
        chain.append(DeepLWebTranslator(target_lang))
    return AutoTranslator(chain)
