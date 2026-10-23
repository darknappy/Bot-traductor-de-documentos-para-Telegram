"""Progreso persistente por documento y caché de resultados.

En plataformas con disco efímero (Render, por ejemplo) estos archivos permiten:

* **Reanudar** una traducción larga si el proceso se reinicia a mitad: al
  reenviar el mismo archivo, el bot continúa por donde iba en lugar de
  empezar de cero.
* **Reenviar al instante** un documento que ya se tradujo antes (caché).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DIGEST_CHUNK = 1024 * 1024


def file_digest(path: Path) -> str:
    """SHA-256 del contenido del archivo (identifica el documento)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_DIGEST_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class JobStore:
    """Carpetas de trabajo por documento, metadatos y caché de resultados."""

    def __init__(self, jobs_root: Path, cache_root: Path, remote: Any | None = None) -> None:
        self.jobs_root = Path(jobs_root)
        self.cache_root = Path(cache_root)
        self.remote = remote
        self._restored: set[str] = set()
        self._remote_output_ok: set[str] = set()
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ claves

    @staticmethod
    def key_for(
        digest: str,
        lang: str,
        user_id: int | None = None,
        engine: str | None = None,
    ) -> str:
        """Clave estable para un documento + idioma (+ usuario + motor elegido).

        El motor forma parte de la clave: si el usuario cambia de motor, la
        traducción se rehace con el nuevo en lugar de servir la anterior.
        """
        key = f"{digest[:24]}-{lang}"
        if user_id is not None:
            key += f"-{user_id}"
        if engine and engine != "auto":
            key += f"-{engine}"
        return key

    def job_dir(self, key: str) -> Path:
        directory = self.jobs_root / key
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # ------------------------------------------------------------------ Supabase

    def restore_job(self, key: str) -> None:
        """Trae a disco los checkpoints de un trabajo guardados en Supabase.

        Se llama una sola vez por clave y proceso. Si el archivo ya está local
        (por ejemplo, porque no hubo reinicio) no se vuelve a descargar.
        """
        if self.remote is None or key in self._restored:
            return
        self._restored.add(key)
        try:
            entries = self.remote.list(f"checkpoints/{key}")
        except Exception:  # noqa: BLE001 - Supabase nunca debe romper la traducción
            logger.debug("No se pudo listar los checkpoints de %s", key, exc_info=True)
            return
        downloaded = 0
        for entry in entries or []:
            name = str(entry.get("name") or "").strip("/")
            if not name or "/" in name:
                continue
            local = self.job_dir(key) / name
            if local.exists():
                continue
            try:
                data = self.remote.download(f"checkpoints/{key}/{name}")
            except Exception:  # noqa: BLE001
                logger.debug("No se pudo descargar el checkpoint %s/%s", key, name, exc_info=True)
                continue
            if data is None:
                continue
            tmp = local.with_suffix(local.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(local)
            downloaded += 1
        if downloaded:
            logger.info("Checkpoints restaurados desde Supabase para %s: %d archivo(s)", key, downloaded)

    def upload_checkpoint_file(self, key: str, name: str) -> bool:
        """Sube un archivo concreto de la carpeta de trabajo a Supabase."""
        if self.remote is None:
            return False
        path = self.job_dir(key) / name
        if not path.is_file():
            return False
        try:
            return bool(self.remote.upload_path(f"checkpoints/{key}/{name}", path))
        except Exception:  # noqa: BLE001 - best effort
            logger.debug("No se pudo subir el checkpoint %s/%s", key, name, exc_info=True)
            return False

    def _prune_remote_checkpoints(self, key: str, keep: set[str]) -> None:
        """Borra de Supabase los checkpoints que ya no se usan (p. ej. PDF viejos)."""
        if self.remote is None:
            return
        try:
            entries = self.remote.list(f"checkpoints/{key}")
        except Exception:  # noqa: BLE001
            return
        for entry in entries or []:
            name = str(entry.get("name") or "").strip("/")
            if not name or name in keep:
                continue
            try:
                self.remote.delete(f"checkpoints/{key}/{name}")
            except Exception:  # noqa: BLE001
                logger.debug("No se pudo borrar el checkpoint remoto %s/%s", key, name, exc_info=True)

    # ------------------------------------------------------------------ metadatos

    def load_meta(self, key: str) -> dict:
        path = self.jobs_root / key / "meta.json"
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001
            logger.warning("Metadatos ilegibles en %s; se ignoran", path, exc_info=True)
            return {}

    def save_meta(self, key: str, **fields) -> dict:
        directory = self.job_dir(key)
        meta = self.load_meta(key)
        meta.update(fields)
        meta["updated"] = time.time()
        tmp = directory / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
        tmp.replace(directory / "meta.json")

        # En PDF, el parcial se guarda antes de actualizar los metadatos. Se
        # sube primero el PDF y después el meta que lo referencia.
        partial = str(meta.get("partial") or "")
        if partial:
            self.upload_checkpoint_file(key, partial)
            self.upload_checkpoint_file(key, "meta.json")
            self._prune_remote_checkpoints(key, {partial, "meta.json"})
        else:
            self.upload_checkpoint_file(key, "meta.json")
        return meta

    # ------------------------------------------------------------------ fragmentos traducidos

    def blob_path(self, key: str, index: int) -> Path:
        return self.job_dir(key) / f"frag_{index:05d}.bin"

    def write_blob(self, key: str, index: int, data: bytes) -> None:
        path = self.blob_path(key, index)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        self.upload_checkpoint_file(key, path.name)

    def read_blob(self, key: str, index: int) -> bytes | None:
        try:
            return self.blob_path(key, index).read_bytes()
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------------ progreso de texto

    @staticmethod
    def _read_jsonl(path: Path) -> dict[int, str]:
        done: dict[int, str] = {}
        if not path.is_file():
            return done
        try:
            for line in path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    done[int(item["i"])] = str(item["t"])
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            logger.warning("No se pudo leer %s", path, exc_info=True)
        return done

    def append_text_records(self, key: str, records: list[dict]) -> None:
        """Añade un lote traducido al progreso local y a un chunk en Supabase.

        Se guarda un archivo pequeño por lote en vez de subir el JSONL completo
        en cada iteración: así el progreso de una novela de miles de párrafos
        no genera cientos de megas de tráfico.
        """
        if not records:
            return
        directory = self.job_dir(key)
        line_path = directory / "lineas.jsonl"
        with open(line_path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        first = min(int(record["i"]) for record in records)
        chunk = directory / f"texto_{first:06d}.jsonl"
        chunk.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            "utf-8",
        )
        self.upload_checkpoint_file(key, chunk.name)

    def read_text_progress(self, key: str) -> dict[int, str]:
        """Une el JSONL local con los chunks restaurados desde Supabase."""
        directory = self.jobs_root / key
        done = self._read_jsonl(directory / "lineas.jsonl")
        for path in sorted(directory.glob("texto_*.jsonl")):
            done.update(self._read_jsonl(path))
        return done

    def clear_job(self, key: str) -> None:
        shutil.rmtree(self.jobs_root / key, ignore_errors=True)
        self._restored.discard(key)
        if self.remote is not None:
            try:
                self.remote.delete_prefix(f"checkpoints/{key}")
            except Exception:  # noqa: BLE001
                logger.debug("No se pudieron borrar los checkpoints remotos de %s", key, exc_info=True)

    def finish_job(self, key: str, output_name: str, **extra) -> dict:
        """Marca la tarea como terminada y libera los archivos intermedios."""
        meta = self.save_meta(key, status="done", output=output_name, **extra)
        directory = self.jobs_root / key
        if directory.exists():
            for child in directory.iterdir():
                if child.name == "meta.json":
                    continue
                try:
                    child.unlink() if child.is_file() else shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    pass
        if self.remote is not None:
            try:
                # El marcador permite recuperar la caché aunque se haya
                # perdido el disco y ya no existan los checkpoints. Solo se
                # sube si el resultado también llegó a Storage; si no, se
                # conservan los checkpoints para poder reanudar.
                if key in self._remote_output_ok:
                    marked = self.remote.upload_bytes(
                        f"cache/{key}.json",
                        json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                        content_type="application/json",
                    )
                    if marked:
                        self.remote.delete_prefix(f"checkpoints/{key}")
                else:
                    logger.warning(
                        "El resultado de %s no está en Supabase; se conservan los checkpoints",
                        key,
                    )
            except Exception:  # noqa: BLE001
                logger.debug("No se pudieron borrar los checkpoints remotos de %s", key, exc_info=True)
        self._remote_output_ok.discard(key)
        self._restored.discard(key)
        return meta

    # ------------------------------------------------------------------ caché

    def cached_output(self, key: str) -> Path | None:
        """Ruta del resultado ya traducido, si existe y está marcado como listo.

        Si el disco se perdió pero el resultado está en Supabase Storage, se
        descarga de nuevo para poder reenviarlo sin volver a traducir.
        """
        meta = self.load_meta(key)
        if meta.get("status") != "done" and self.remote is not None:
            # Tras un reinicio puede que solo quede el marcador de la caché.
            try:
                marker = self.remote.download(f"cache/{key}.json")
            except Exception:  # noqa: BLE001
                marker = None
            if marker:
                try:
                    data = json.loads(marker.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    data = None
                if isinstance(data, dict):
                    meta = data
                    directory = self.job_dir(key)
                    (directory / "meta.json").write_text(
                        json.dumps(meta, ensure_ascii=False), "utf-8"
                    )
        if meta.get("status") != "done":
            return None
        name = meta.get("output")
        if not name:
            return None
        path = self.cache_root / name
        if path.exists():
            return path
        if self.remote is not None:
            try:
                data = self.remote.download(f"cache/{name}")
            except Exception:  # noqa: BLE001
                logger.debug("No se pudo recuperar la caché remota de %s", name, exc_info=True)
                data = None
            if data is not None:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
                logger.info("Resultado recuperado desde Supabase: %s", name)
                return path
        return None

    def store_output(self, key: str, source: Path, out_name: str) -> Path:
        """Mueve el resultado a la caché con un nombre estable y lo sube a Supabase."""
        target = self.cache_root / f"{key}{Path(out_name).suffix.lower()}"
        try:
            shutil.move(str(source), str(target))
        except Exception:  # noqa: BLE001 - si no se puede mover, se copia
            shutil.copyfile(source, target)
        if self.remote is not None:
            try:
                uploaded = self.remote.upload_path(f"cache/{target.name}", target)
            except Exception:  # noqa: BLE001 - la caché local sigue funcionando
                uploaded = False
                logger.debug("No se pudo subir la caché %s", target.name, exc_info=True)
            if uploaded:
                self._remote_output_ok.add(key)
            else:
                self._remote_output_ok.discard(key)
        return target

    # ------------------------------------------------------------------ limpieza

    def prune(self, *, max_mb: int = 512, ttl_days: float = 7.0) -> dict:
        """Elimina resultados antiguos o cuando la caché ocupa demasiado."""
        now = time.time()
        removed = 0
        freed = 0
        entries: list[tuple[float, int, Path]] = []
        for path in self.cache_root.iterdir():
            if not path.is_file():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            entries.append((info.st_mtime, info.st_size, path))

        # 1) por antigüedad
        ttl_seconds = max(0.0, ttl_days) * 86400
        keep: list[tuple[float, int, Path]] = []
        for mtime, size, path in entries:
            if ttl_seconds and now - mtime > ttl_seconds:
                key = path.name.split(".", 1)[0]
                path.unlink(missing_ok=True)
                self.clear_job(key)
                removed += 1
                freed += size
            else:
                keep.append((mtime, size, path))

        # 2) por tamaño máximo (se eliminan los más antiguos primero)
        total = sum(size for _mtime, size, _path in keep)
        limit = max_mb * 1024 * 1024
        for mtime, size, path in sorted(keep, key=lambda item: item[0]):
            if total <= limit:
                break
            key = path.name.split(".", 1)[0]
            path.unlink(missing_ok=True)
            self.clear_job(key)
            total -= size
            removed += 1
            freed += size

        remote_report: dict = {}
        if self.remote is not None:
            try:
                remote_report = self.remote.prune_cache(ttl_days=ttl_days, max_mb=max_mb)
            except Exception:  # noqa: BLE001 - la limpieza remota es opcional
                logger.debug("No se pudo limpiar la caché de Supabase", exc_info=True)

        if removed:
            logger.info("Caché depurada: %d archivos, %.1f MB liberados", removed, freed / 1048576)
        return {
            "removed": removed,
            "freed_bytes": freed,
            "cached": len(keep),
            "remote": remote_report,
        }
