"""Traducción de EPUB conservando capítulos, estilos, imágenes y enlaces.

El EPUB se trata como un ZIP: se traducen los documentos XHTML/HTML y el índice
NCX, y se vuelve a empaquetar el libro copiando el resto de archivos byte a byte
(imágenes, CSS, fuentes, OPF...). Así el resultado conserva exactamente la
estructura original; lo único que cambia además del texto es el idioma
(``dc:language``) y el título con sufijo en el OPF.

Dentro de cada documento, los bloques de texto simples se traducen como una
unidad (mejor calidad) y los que contienen enlaces u otros elementos se traducen
nodo a nodo para no perder la estructura.
"""
from __future__ import annotations

import logging
import re
import shutil
import zipfile
from html.entities import html5 as _HTML5_ENTITIES
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString
from lxml import etree

from errors import JobCancelledError, UnsupportedFormatError

try:  # subclases de NavigableString que no deben traducirse
    from bs4 import CData, Declaration, Doctype, ProcessingInstruction

    _NON_TEXT_TYPES: tuple = (CData, Declaration, Doctype, ProcessingInstruction)
except ImportError:  # pragma: no cover
    _NON_TEXT_TYPES = ()

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], Awaitable[None]]
CancelFn = Callable[[], bool]

# Elementos cuyo contenido textual se traduce como una unidad.
_BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "td", "th",
    "blockquote", "figcaption", "caption", "div", "title", "legend", "summary",
    "text",  # etiquetas de texto del índice NCX
}

# Etiquetas en línea "seguras": si un bloque solo tiene estas, se puede
# reemplazar todo su contenido por la traducción sin perder nada relevante.
_SAFE_INLINE = {
    "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i", "kbd",
    "mark", "q", "rp", "rt", "ruby", "s", "samp", "small", "span", "strong",
    "sub", "sup", "time", "u", "var",
}

# No se traduce nada dentro de estos elementos.
_SKIP_TAGS = {"pre", "script", "style", "svg", "math", "textarea"}

# Archivos del EPUB que contienen texto traducible.
_CONTENT_SUFFIXES = {".xhtml", ".html", ".htm", ".ncx"}

# Paquetes de cifrado típicos de DRM (el de fuentes es legítimo y no bloquea).
_DRM_HINT = "http://www.w3.org/2001/04/xmlenc#"

# Tareas por lote antes de informar progreso (evita ráfagas gigantes).
_BATCH = 48

_KEEP_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_PREFIX_RE = re.compile(r"\s*(<\?xml[^>]*\?>)?\s*(<!DOCTYPE(?:[^>\[]|\[[^\]]*\])*>)?", re.S)

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_DC_NS = "http://purl.org/dc/elements/1.1/"


# --------------------------------------------------------------------------- documentos XHTML/NCX


def _decode(content: bytes) -> str:
    """Decodifica el contenido de un documento del EPUB."""
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig", "replace")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16", "replace")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    match = re.search(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", content[:4096], re.I)
    if match:
        encoding = match.group(1).decode("ascii", "ignore")
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return content.decode("cp1252", "replace")


def _replace_named_entities(text: str) -> str:
    """Convierte entidades HTML (&nbsp;, &mdash;...) en caracteres.

    El parser XML falla con entidades HTML no definidas en XHTML, así que se
    sustituyen antes de analizar el documento.
    """

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name in _KEEP_ENTITIES:
            return match.group(0)
        value = _HTML5_ENTITIES.get(name + ";")
        return value if value else match.group(0)

    return _ENTITY_RE.sub(_replace, text)


def _split_prefix(text: str) -> tuple[str, str, str]:
    """Separa la declaración XML y el DOCTYPE del resto del documento."""
    match = _PREFIX_RE.match(text)
    if not match:
        return "", "", text
    return match.group(1) or "", match.group(2) or "", text[match.end():]


def _compose(soup: BeautifulSoup, doctype: str) -> str:
    """Vuelve a montar el documento (el serializador ya añade la declaración XML)."""
    body = str(soup)
    if not doctype or "<!DOCTYPE" in body[:300]:
        return body
    # El DOCTYPE va después de la declaración XML, nunca antes.
    if body.startswith("<?xml"):
        end = body.find("?>")
        if end != -1:
            return f"{body[:end + 2]}\n{doctype}{body[end + 2:]}"
    return f"{doctype}\n{body}"


def _parse_html(body: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(body, "xml")
    except Exception:  # noqa: BLE001 - si lxml falla se usa el parser estándar
        logger.warning("El parser XML falló; se usa html.parser", exc_info=True)
        return BeautifulSoup(body, "html.parser")


def _ensure_utf8_meta(soup: BeautifulSoup) -> None:
    """Garantiza que la declaración de charset del documento sea UTF-8."""
    head = soup.find("head")
    if head is None:
        return
    meta = head.find("meta", attrs={"charset": True})
    if meta is not None:
        meta["charset"] = "utf-8"
        return
    meta = head.find(
        "meta",
        attrs={"http-equiv": lambda value: bool(value) and value.lower() == "content-type"},
    )
    if meta is not None:
        meta["content"] = "text/html; charset=utf-8"
        return
    new_meta = soup.new_tag("meta")
    new_meta["charset"] = "utf-8"
    head.insert(0, new_meta)


def _is_text_node(node) -> bool:
    return isinstance(node, NavigableString) and not isinstance(node, _NON_TEXT_TYPES)


def _plan_tasks(soup: BeautifulSoup) -> list[tuple[str, object, str]]:
    """Decide qué textos traducir y cómo volver a colocarlos.

    Devuelve tareas ``(tipo, destino, texto)`` donde tipo es ``"whole"`` (se
    reemplaza todo el contenido del elemento) o ``"node"`` (se reemplaza solo
    ese nodo de texto).
    """
    tasks: list[tuple[str, object, str]] = []
    visited: set[int] = set()

    def in_skipped(el) -> bool:
        parent = el
        while parent is not None:
            if getattr(parent, "name", None) in _SKIP_TAGS:
                return True
            parent = parent.parent
        return False

    for element in soup.find_all(_BLOCK_TAGS):
        if in_skipped(element):
            continue

        block_children = element.find_all(_BLOCK_TAGS)
        if block_children:
            # Los hijos de bloque se tratan por su cuenta; aquí solo se
            # recogen los textos que quedan fuera de ellos.
            block_ids = {id(child) for child in block_children}
            for node in element.find_all(string=True):
                if id(node) in visited or not _is_text_node(node) or not str(node).strip():
                    continue
                ancestor = node.parent
                inside_block = False
                while ancestor is not None and ancestor is not element:
                    if id(ancestor) in block_ids:
                        inside_block = True
                        break
                    ancestor = ancestor.parent
                if inside_block or in_skipped(node):
                    continue
                tasks.append(("node", node, str(node)))
                visited.add(id(node))
            continue

        child_tags = {child.name for child in element.find_all(True)}
        if child_tags <= _SAFE_INLINE:
            text = element.get_text()
            if text.strip():
                tasks.append(("whole", element, text))
                for node in element.find_all(string=True):
                    visited.add(id(node))
        else:
            for node in element.find_all(string=True):
                if id(node) in visited or not _is_text_node(node) or not str(node).strip():
                    continue
                if in_skipped(node):
                    continue
                tasks.append(("node", node, str(node)))
                visited.add(id(node))

    # Red de seguridad: cualquier texto no planificado (etiquetas raras).
    for node in soup.find_all(string=True):
        if id(node) in visited or not _is_text_node(node) or not str(node).strip():
            continue
        if in_skipped(node):
            continue
        tasks.append(("node", node, str(node)))

    return tasks


async def _translate_batched(translator, texts: list[str]) -> list[str]:
    results: list[str] = []
    for start in range(0, len(texts), _BATCH):
        results.extend(await translator.translate_many(texts[start:start + _BATCH]))
    return results


async def _render_translated(content: bytes, translator) -> tuple[bytes, int]:
    """Devuelve el documento con los textos traducidos y el número de fragmentos."""
    text = _decode(content)
    _xml_decl, doctype, body = _split_prefix(text)
    soup = _parse_html(_replace_named_entities(body))
    _ensure_utf8_meta(soup)

    tasks = _plan_tasks(soup)
    if not tasks:
        return content, 0

    results = await _translate_batched(translator, [task[2] for task in tasks])
    for (kind, target, _original), translated in zip(tasks, results):
        if not translated or not translated.strip():
            continue
        if kind == "whole":
            target.clear()
            target.append(NavigableString(translated))
        else:
            target.replace_with(NavigableString(translated))

    return _compose(soup, doctype).encode("utf-8"), len(tasks)


# --------------------------------------------------------------------------- paquete EPUB


def _find_opf(zin: zipfile.ZipFile) -> str | None:
    """Localiza el archivo OPF usando META-INF/container.xml."""
    try:
        container = etree.fromstring(zin.read("META-INF/container.xml"))
        for rootfile in container.iter(f"{{{_CONTAINER_NS}}}rootfile"):
            full_path = rootfile.get("full-path")
            if full_path and full_path in zin.namelist():
                return full_path
    except (KeyError, etree.XMLSyntaxError):
        pass
    for name in zin.namelist():
        if name.lower().endswith(".opf"):
            return name
    return None


def _check_drm(zin: zipfile.ZipFile) -> None:
    """Bloquea EPUB con DRM (las fuentes ofuscadas no cuentan)."""
    try:
        raw = zin.read("META-INF/encryption.xml")
    except KeyError:
        return
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return
    protected = []
    for reference in root.iter(f"{{{_DRM_HINT}}}CipherReference"):
        uri = unquote(reference.get("URI") or "")
        if Path(uri).suffix.lower() in _CONTENT_SUFFIXES | {".opf", ".txt"}:
            protected.append(uri)
    if protected:
        raise UnsupportedFormatError(
            "El EPUB está protegido con DRM, no se puede traducir."
        )


def _update_opf(raw: bytes, target_lang: str) -> bytes:
    """Añade el idioma destino y un sufijo al título en los metadatos del libro."""
    try:
        parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, recover=True)
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError:
        logger.debug("No se pudo analizar el OPF; se deja tal cual", exc_info=True)
        return raw
    if root is None:
        return raw
    titles = root.findall(f".//{{{_DC_NS}}}title")
    if titles:
        current = (titles[0].text or "").strip()
        titles[0].text = f"{current} ({target_lang.upper()})" if current else target_lang.upper()
    for language in root.findall(f".//{{{_DC_NS}}}language"):
        language.text = target_lang
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


# --------------------------------------------------------------------------- reanudación


def _load_progress(store, key: str, total: int, target_lang: str) -> tuple[set[int], int]:
    """Recupera los documentos ya traducidos de una ejecución anterior."""
    if store is None or not key:
        return set(), 0
    meta = store.load_meta(key)
    valid = (
        meta.get("kind") == "epub"
        and meta.get("total") == total
        and meta.get("lang") == target_lang
    )
    if not valid:
        if meta:
            logger.info("Progreso anterior incompatible; se empieza de cero")
        store.clear_job(key)
        store.save_meta(key, kind="epub", total=total, lang=target_lang, done=[], units=0)
        return set(), 0

    # Solo se reutilizan los documentos cuyo contenido traducido sigue en disco.
    done = {
        int(index)
        for index in meta.get("done", [])
        if isinstance(index, int) and store.blob_path(key, index).exists()
    }
    units = int(meta.get("units", 0) or 0)
    if done:
        logger.info("Reanudando EPUB: %d/%d documentos ya traducidos", len(done), total)
    return done, units


# --------------------------------------------------------------------------- flujo principal


async def translate_epub(
    src_path: Path,
    dst_path: Path,
    translator,
    *,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
    store=None,
    key: str | None = None,
) -> dict:
    """Traduce un EPUB completo. Devuelve estadísticas.

    Si se pasan ``store`` y ``key``, el progreso se guarda en disco documento a
    documento y se reanuda automáticamente si una ejecución anterior se quedó a
    medias (útil para libros enormes en servidores que se reinician).
    """
    stats = {"documents": 0, "translated": 0, "units": 0, "resumed": 0}

    with zipfile.ZipFile(src_path, "r") as zin:
        _check_drm(zin)
        opf_name = _find_opf(zin)
        if opf_name is None:
            raise UnsupportedFormatError("El EPUB no contiene un archivo OPF válido.")

        names = [name for name in zin.namelist() if not name.endswith("/")]
        content_names = [
            name
            for name in names
            if not name.startswith("META-INF/") and Path(name).suffix.lower() in _CONTENT_SUFFIXES
        ]
        stats["documents"] = len(content_names)
        content_index = {name: index for index, name in enumerate(content_names)}
        total = max(1, len(content_names))

        done, units_total = _load_progress(store, key, len(content_names), translator.target_lang)
        stats["resumed"] = len(done)
        # Sin almacén en disco el resultado de cada documento vive en memoria.
        replaced: dict[str, bytes] = {}

        for index, name in enumerate(content_names):
            if cancel and cancel():
                raise JobCancelledError()
            if index in done:
                if progress:
                    await progress(
                        (index + 1) / total,
                        f"documento {index + 1}/{len(content_names)} (ya traducido)",
                    )
                continue
            try:
                raw = zin.read(name)
            except KeyError:  # pragma: no cover - entradas raras del zip
                continue
            new_content, units = await _render_translated(raw, translator)
            if units:
                if store is not None and key:
                    store.write_blob(key, index, new_content)
                else:
                    replaced[name] = new_content
                done.add(index)
                units_total += units
                if store is not None and key:
                    store.save_meta(key, done=sorted(done), units=units_total)
            if progress:
                await progress(
                    (index + 1) / total,
                    f"documento {index + 1}/{len(content_names)}",
                )

        stats["translated"] = len(done)
        stats["units"] = units_total

        if stats["units"] == 0:
            raise ValueError("No se encontró texto traducible en el EPUB.")
        if cancel and cancel():
            raise JobCancelledError()

        new_opf = _update_opf(zin.read(opf_name), translator.target_lang)

        def _translated_bytes(name: str) -> bytes | None:
            if store is not None and key:
                index = content_index.get(name)
                return store.read_blob(key, index) if index is not None else None
            return replaced.get(name)

        with zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as zout:
            # El mimetype debe ir el primero y sin comprimir (norma EPUB).
            info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
            zout.writestr(info, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)

            for name in names:
                if name == "mimetype":
                    continue
                if name == opf_name:
                    zout.writestr(name, new_opf, compress_type=zipfile.ZIP_DEFLATED)
                    continue
                new_content = _translated_bytes(name)
                if new_content is not None:
                    zout.writestr(name, new_content, compress_type=zipfile.ZIP_DEFLATED)
                else:
                    with zin.open(name) as source, zout.open(name, "w", force_zip64=True) as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)

    return stats
