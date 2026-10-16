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

    def __init__(self, jobs_root: Path, cache_root: Path) -> None:
        self.jobs_root = Path(jobs_root)
        self.cache_root = Path(cache_root)
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
        return meta

    # ------------------------------------------------------------------ fragmentos traducidos

    def blob_path(self, key: str, index: int) -> Path:
        return self.job_dir(key) / f"frag_{index:05d}.bin"

    def write_blob(self, key: str, index: int, data: bytes) -> None:
        path = self.blob_path(key, index)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def read_blob(self, key: str, index: int) -> bytes | None:
        try:
            return self.blob_path(key, index).read_bytes()
        except FileNotFoundError:
            return None

    def clear_job(self, key: str) -> None:
        shutil.rmtree(self.jobs_root / key, ignore_errors=True)

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
        return meta

    # ------------------------------------------------------------------ caché

    def cached_output(self, key: str) -> Path | None:
        """Ruta del resultado ya traducido, si existe y está marcado como listo."""
        meta = self.load_meta(key)
        if meta.get("status") != "done":
            return None
        name = meta.get("output")
        if not name:
            return None
        path = self.cache_root / name
        return path if path.exists() else None

    def store_output(self, key: str, source: Path, out_name: str) -> Path:
        """Mueve el resultado a la caché con un nombre estable."""
        target = self.cache_root / f"{key}{Path(out_name).suffix.lower()}"
        try:
            shutil.move(str(source), str(target))
        except Exception:  # noqa: BLE001 - si no se puede mover, se copia
            shutil.copyfile(source, target)
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

        if removed:
            logger.info("Caché depurada: %d archivos, %.1f MB liberados", removed, freed / 1048576)
        return {"removed": removed, "freed_bytes": freed, "cached": len(keep)}
