"""Persistencia opcional en Supabase (Postgres + Storage).

El bot funciona igual sin Supabase. Si defines ``SUPABASE_URL`` y
``SUPABASE_SERVICE_KEY``, los trabajos, las preferencias y los checkpoints se
guardan también en Supabase para sobrevivir a reinicios o redeploys de Render.

Se usa la API REST de Supabase directamente con ``httpx``: no hace falta añadir
el SDK oficial ni más dependencias. Todas las operaciones son "best effort": si
Supabase falla o no está configurado, el bot sigue trabajando con el disco local.
"""
from __future__ import annotations

import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

import config

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_type_for(name: str) -> str:
    """Content-Type razonable para los archivos que se guardan en Storage."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".epub":
        return "application/epub+zip"
    if suffix in {".txt", ".md", ".json", ".jsonl"}:
        return "text/plain; charset=utf-8"
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _parse_time(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class SupabaseBackend:
    """Cliente mínimo para la base de datos y el Storage de Supabase."""

    def __init__(
        self,
        url: str,
        key: str,
        bucket: str,
        *,
        jobs_table: str = "bot_jobs",
        users_table: str = "bot_users",
        timeout: float = 12.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.bucket = bucket
        self.jobs_table = jobs_table
        self.users_table = users_table
        self.timeout = timeout
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            transport=transport,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "User-Agent": "bot-traductor-supabase/1.0",
            },
        )
        self.db_available = True
        self.storage_available = True
        # Si falta una tabla concreta (por ejemplo, bot_users), la otra puede
        # seguir funcionando. No se desactiva toda la base de datos por un 404.
        self._disabled_tables: set[str] = set()
        self._db_failures = 0
        self._warned: set[str] = set()

    @classmethod
    def from_config(cls, *, transport: httpx.BaseTransport | None = None) -> "SupabaseBackend | None":
        if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
            return None
        return cls(
            config.SUPABASE_URL,
            config.SUPABASE_SERVICE_KEY,
            config.SUPABASE_BUCKET,
            jobs_table=config.SUPABASE_JOBS_TABLE,
            users_table=config.SUPABASE_USERS_TABLE,
            timeout=config.SUPABASE_TIMEOUT,
            transport=transport,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    @property
    def ready(self) -> bool:
        """True si la base de datos y Storage están operativos y sin tablas caídas."""
        return bool(
            self.db_available
            and self.storage_available
            and not self._disabled_tables
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ utilidades

    def _warn_once(self, tag: str, message: str, *args: Any) -> None:
        if tag in self._warned:
            return
        self._warned.add(tag)
        logger.warning(message, *args)

    @property
    def _base_headers(self) -> dict[str, str]:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}"}

    def _db_headers(self, *, prefer: str | None = None) -> dict[str, str]:
        headers = {**self._base_headers, "Content-Type": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _db(self, method: str, table: str, *, params: dict | None = None, json: Any = None,
            prefer: str | None = None) -> httpx.Response | None:
        if not self.db_available or table in self._disabled_tables:
            return None
        url = f"{self.url}/rest/v1/{table}"
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._db_headers(prefer=prefer),
                # Las operaciones de estado son pequeñas: si Supabase no
                # responde, no bloqueamos la cola de traducción 12 s por cada
                # actualización. Storage conserva el timeout completo.
                timeout=min(5.0, self.timeout),
            )
        except httpx.HTTPError as err:
            # Un fallo aislado puede ser un timeout puntual; no conviene
            # desactivar Supabase para toda la sesión. Tras dos fallos
            # seguidos sí se pasa a modo local para no frenar cada avance.
            self._db_failures += 1
            if self._db_failures >= 2:
                self.db_available = False
                self._warn_once(
                    "db-net",
                    "No se pudo contactar con Supabase (base de datos): %s. "
                    "Se sigue en modo local hasta reiniciar.",
                    err,
                )
            else:
                logger.warning(
                    "Fallo puntual al contactar con Supabase: %s. Se reintentará.",
                    err,
                )
            return None
        if response.status_code in (200, 201, 204):
            self._db_failures = 0
            return response
        if response.status_code == 404:
            self._disabled_tables.add(table)
            self._warn_once(
                f"db-missing-{table}",
                "Supabase no encuentra la tabla «%s». Ejecuta supabase_schema.sql en el SQL Editor. "
                "El bot seguirá en modo local para esa tabla.",
                table,
            )
            if {self.jobs_table, self.users_table} <= self._disabled_tables:
                self.db_available = False
        elif response.status_code in (401, 403):
            self.db_available = False
            self._warn_once(
                "db-auth",
                "Supabase rechazó la clave (HTTP %s). Usa la clave service_role, no la anon. "
                "Se sigue en modo local.",
                response.status_code,
            )
        elif response.status_code == 409:
            # Una fila duplicada no es un fallo grave (por ejemplo, al reanudar).
            logger.debug("Supabase: conflicto al insertar en %s", table)
            return response
        else:
            self._warn_once(
                "db-error",
                "Supabase devolvió HTTP %s al usar %s: %s",
                response.status_code,
                table,
                response.text[:300],
            )
        return None

    # ------------------------------------------------------------------ usuarios

    def upsert_user(self, user_id: int, lang: str, engine: str) -> bool:
        response = self._db(
            "POST",
            self.users_table,
            params={"on_conflict": "user_id"},
            json={
                "user_id": int(user_id),
                "lang": lang,
                "engine": engine,
                "updated_at": _now(),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return response is not None

    def fetch_users(self) -> list[dict]:
        response = self._db(
            "GET",
            self.users_table,
            params={"select": "user_id,lang,engine", "order": "user_id.asc"},
        )
        if response is None:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ trabajos

    def create_job(self, job: dict) -> bool:
        payload = dict(job)
        payload.setdefault("created_at", _now())
        payload["updated_at"] = _now()
        response = self._db(
            "POST",
            self.jobs_table,
            json=payload,
            prefer="return=minimal",
        )
        return response is not None

    def update_job(self, task_id: str, **fields: Any) -> bool:
        fields["updated_at"] = _now()
        response = self._db(
            "PATCH",
            self.jobs_table,
            params={"id": f"eq.{task_id}"},
            json=fields,
            prefer="return=minimal",
        )
        return response is not None

    def fetch_jobs(self, statuses: tuple[str, ...] = ("queued", "running", "downloading", "ready", "sending")) -> list[dict]:
        if not statuses:
            return []
        response = self._db(
            "GET",
            self.jobs_table,
            params={
                "select": "*",
                "status": f"in.({','.join(statuses)})",
                "order": "created_at.asc",
            },
        )
        if response is None:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def delete_job(self, task_id: str) -> bool:
        response = self._db(
            "DELETE",
            self.jobs_table,
            params={"id": f"eq.{task_id}"},
            prefer="return=minimal",
        )
        return response is not None

    # ------------------------------------------------------------------ Storage

    def _storage_headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = dict(self._base_headers)
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _object_url(self, path: str) -> str:
        safe_path = quote(path.lstrip("/"), safe="/")
        return f"{self.url}/storage/v1/object/{quote(self.bucket, safe='')}/{safe_path}"

    def ensure_bucket(self) -> bool:
        """Comprueba el bucket y lo crea (privado) si no existe."""
        if not self.storage_available:
            return False
        bucket_url = f"{self.url}/storage/v1/bucket/{quote(self.bucket, safe='')}"
        try:
            response = self._client.get(bucket_url, headers=self._base_headers)
        except httpx.HTTPError as err:
            self.storage_available = False
            self._warn_once("storage-net", "No se pudo contactar con Supabase Storage: %s", err)
            return False
        if response.status_code == 200:
            return True
        if response.status_code in (401, 403):
            self.storage_available = False
            self._warn_once(
                "storage-auth",
                "Supabase Storage rechazó la clave. Usa la clave service_role.",
            )
            return False
        if response.status_code != 404:
            self.storage_available = False
            self._warn_once(
                "storage-check",
                "Supabase Storage devolvió HTTP %s al comprobar el bucket: %s",
                response.status_code,
                response.text[:300],
            )
            return False
        try:
            created = self._client.post(
                f"{self.url}/storage/v1/bucket",
                json={"id": self.bucket, "name": self.bucket, "public": False},
                headers={**self._base_headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as err:
            self.storage_available = False
            self._warn_once("storage-create-net", "No se pudo crear el bucket de Supabase: %s", err)
            return False
        if created.status_code in (200, 201):
            logger.info("Bucket de Supabase creado: %s", self.bucket)
            return True
        if created.status_code == 409:
            return True
        self.storage_available = False
        self._warn_once(
            "storage-create",
            "No se pudo crear el bucket «%s» (HTTP %s). Créalo a mano en Supabase → Storage.",
            self.bucket,
            created.status_code,
        )
        return False

    def upload_path(self, remote_path: str, local_path: Path, *, content_type: str | None = None) -> bool:
        try:
            data = Path(local_path).read_bytes()
        except OSError:
            logger.warning("No se pudo leer %s para subirlo a Supabase", local_path, exc_info=True)
            return False
        return self.upload_bytes(remote_path, data, content_type=content_type or content_type_for(str(local_path)))

    def upload_bytes(self, remote_path: str, data: bytes, *, content_type: str = "application/octet-stream") -> bool:
        if not self.storage_available:
            return False
        headers = self._storage_headers(content_type=content_type)
        headers["x-upsert"] = "true"
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                response = self._client.post(self._object_url(remote_path), content=data, headers=headers)
            except httpx.HTTPError as err:
                self._warn_once("storage-upload-net", "No se pudo subir %s a Supabase: %s", remote_path, err)
                if attempt == 0:
                    time.sleep(0.6)
                    continue
                return False
            if response.status_code in (200, 201):
                return True
            if response.status_code >= 500 and attempt == 0:
                time.sleep(0.6)
                continue
            break
        if response is None:
            return False
        if response.status_code in (401, 403):
            self._warn_once("storage-upload-auth", "Supabase Storage rechazó la subida (clave service_role).")
        else:
            self._warn_once(
                "storage-upload",
                "Supabase Storage devolvió HTTP %s al subir %s: %s",
                response.status_code,
                remote_path,
                response.text[:300],
            )
        return False

    def download(self, remote_path: str) -> bytes | None:
        if not self.storage_available:
            return None
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                response = self._client.get(self._object_url(remote_path), headers=self._base_headers)
            except httpx.HTTPError as err:
                self._warn_once("storage-download-net", "No se pudo descargar %s de Supabase: %s", remote_path, err)
                if attempt == 0:
                    time.sleep(0.6)
                    continue
                return None
            if response.status_code == 200:
                return response.content
            if response.status_code == 404:
                return None
            if response.status_code >= 500 and attempt == 0:
                time.sleep(0.6)
                continue
            break
        if response is None:
            return None
        self._warn_once(
            "storage-download",
            "Supabase Storage devolvió HTTP %s al descargar %s",
            response.status_code,
            remote_path,
        )
        return None

    def list(self, prefix: str) -> list[dict]:
        """Lista los archivos de una carpeta (no recursivo)."""
        if not self.storage_available:
            return []
        clean = prefix.strip("/")
        url = f"{self.url}/storage/v1/object/list/{quote(self.bucket, safe='')}"
        results: list[dict] = []
        offset = 0
        limit = 1000
        while True:
            body = {
                "prefix": f"{clean}/" if clean else "",
                "limit": limit,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            }
            try:
                response = self._client.post(url, json=body, headers={**self._base_headers, "Content-Type": "application/json"})
            except httpx.HTTPError as err:
                self._warn_once("storage-list-net", "No se pudo listar %s en Supabase: %s", clean, err)
                return results
            if response.status_code != 200:
                self._warn_once(
                    "storage-list",
                    "Supabase Storage devolvió HTTP %s al listar %s",
                    response.status_code,
                    clean,
                )
                return results
            try:
                page = response.json()
            except ValueError:
                return results
            if not isinstance(page, list) or not page:
                break
            results.extend(item for item in page if isinstance(item, dict))
            if len(page) < limit:
                break
            offset += limit
        return results

    def delete(self, remote_path: str) -> bool:
        if not self.storage_available:
            return False
        try:
            response = self._client.delete(self._object_url(remote_path), headers=self._base_headers)
        except httpx.HTTPError as err:
            self._warn_once("storage-delete-net", "No se pudo borrar %s de Supabase: %s", remote_path, err)
            return False
        if response.status_code in (200, 204, 404):
            return True
        self._warn_once(
            "storage-delete",
            "Supabase Storage devolvió HTTP %s al borrar %s",
            response.status_code,
            remote_path,
        )
        return False

    def delete_prefix(self, prefix: str) -> int:
        clean = prefix.strip("/")
        removed = 0
        for entry in self.list(clean):
            name = str(entry.get("name") or "").strip("/")
            if not name:
                continue
            if self.delete(f"{clean}/{name}"):
                removed += 1
        return removed

    def prune_cache(self, *, ttl_days: float = 7.0, max_mb: int = 512) -> dict:
        """Limpia la caché remota por antigüedad y tamaño (best effort)."""
        # Cada resultado tiene dos archivos: ``cache/{key}.ext`` y el marcador
        # ``cache/{key}.json``. Se agrupan para no borrar uno y dejar huérfano
        # el otro (el marcador es lo que permite recuperar la caché tras un
        # reinicio).
        groups: dict[str, dict] = {}
        for item in self.list("cache"):
            name = str(item.get("name") or "").strip("/")
            if not name:
                continue
            metadata = item.get("metadata") or {}
            size = int(metadata.get("size") or item.get("size") or 0)
            when = _parse_time(item.get("updated_at") or item.get("created_at"))
            is_marker = name.endswith(".json")
            key = name[:-5] if is_marker else Path(name).stem
            group = groups.setdefault(key, {"files": [], "size": 0, "when": 0.0})
            group["files"].append(f"cache/{name}")
            group["size"] += size
            if is_marker:
                group["when"] = when
            elif not group["when"]:
                group["when"] = when

        now = time.time()
        ttl_seconds = max(0.0, ttl_days) * 86400
        removed = 0
        freed = 0
        keep: list[tuple[float, int, list[str]]] = []
        for group in groups.values():
            when = group["when"] or now
            if ttl_seconds and now - when > ttl_seconds:
                for path in group["files"]:
                    if self.delete(path):
                        removed += 1
                        freed += group["size"] // max(1, len(group["files"]))
            else:
                keep.append((when, group["size"], group["files"]))

        total = sum(size for _when, size, _files in keep)
        limit = max_mb * 1024 * 1024
        for _when, size, files in sorted(keep, key=lambda item: item[0]):
            if total <= limit:
                break
            deleted_any = False
            for path in files:
                if self.delete(path):
                    removed += 1
                    deleted_any = True
            if deleted_any:
                freed += size
                total -= size
        if removed:
            logger.info("Caché de Supabase depurada: %d archivo(s), %.1f MB", removed, freed / 1048576)
        return {"removed": removed, "freed_bytes": freed}


_BACKEND: SupabaseBackend | None = None
_BACKEND_INIT = False


def get_backend() -> SupabaseBackend | None:
    """Devuelve el backend configurado (o ``None`` si Supabase no está activo)."""
    global _BACKEND, _BACKEND_INIT
    if not _BACKEND_INIT:
        _BACKEND_INIT = True
        _BACKEND = SupabaseBackend.from_config()
        if _BACKEND is not None:
            logger.info("Supabase configurado: %s (bucket %s)", config.SUPABASE_URL, config.SUPABASE_BUCKET)
    return _BACKEND


def reset_backend(backend: SupabaseBackend | None = None, *, initialized: bool = True) -> None:
    """Reinicia el singleton (útil en pruebas o al cambiar de configuración)."""
    global _BACKEND, _BACKEND_INIT
    if _BACKEND is not None and _BACKEND is not backend:
        try:
            _BACKEND.close()
        except Exception:  # noqa: BLE001
            pass
    _BACKEND = backend
    _BACKEND_INIT = initialized
