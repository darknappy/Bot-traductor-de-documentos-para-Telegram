"""Divisor de novelas en secciones.

Detecta los capítulos de un documento y lo parte en varios archivos
independientes, **sin traducir**, para poder traducirlos por secciones: si una
falla, se cancela o el servidor se reinicia, solo hay que reenviar la sección
pendiente en lugar de la novela completa.

* **EPUB**: los capítulos son los documentos del *spine*. Cada sección es un
  EPUB válido con el índice (NCX y navegación) recortado a sus capítulos, los
  estilos e imágenes copiados tal cual y las referencias ajustadas.
* **TXT/MD**: los capítulos se detectan por sus encabezados («Capítulo 12»,
  «Chapter 12», «第12章», «12화»...).
* **PDF**: se usa el índice (marcadores) del documento si lo tiene; si no, se
  parte por páginas.
"""
from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html import escape as _html_escape
from html import unescape as _html_unescape
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import fitz
from lxml import etree

import config
from epub_translate import check_drm, find_opf
from errors import JobCancelledError, SplitError, UnsupportedFormatError

logger = logging.getLogger(__name__)

CancelFn = Callable[[], bool]

_XHTML_TYPES = {"application/xhtml+xml", "text/html"}
_XHTML_SUFFIXES = (".xhtml", ".html", ".htm")
_XML_PARSER = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, recover=True)
_HTML_PARSER = etree.HTMLParser()
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
_LABEL_CHAPTERS = ("cap.", "caps.")
_LABEL_PAGES = ("pág.", "págs.")

_UNSAFE_NAME_RE = re.compile(r"[^\w\s.,()\[\]-]", re.UNICODE)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HEADING_RE = re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.I | re.S)
_MD_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")


# --------------------------------------------------------------------------- comandos


@dataclass(frozen=True)
class SplitSpec:
    """Cómo dividir: ``units`` = N por sección · ``parts`` = N secciones."""

    mode: str = "units"
    value: int | None = None


_SPLIT_COMMANDS = ("dividir", "partir", "seccionar", "separar", "split")
_PARTS_WORDS = ("parte", "partes", "part", "parts", "seccion", "secciones", "sections")


def parse_split_command(text: str) -> SplitSpec | None:
    """Reconoce «/dividir [N] [partes]» en un comando o en el pie de foto.

    Devuelve ``None`` si el texto no pide dividir; si no, la especificación
    (con ``value = None`` cuando no se indicó un número).
    """
    tokens = (text or "").strip().split()
    if not tokens:
        return None
    if tokens[0].lstrip("/!").lower() not in _SPLIT_COMMANDS:
        return None
    spec = SplitSpec()
    for token in tokens[1:]:
        low = token.lower().strip(".,:;")
        if low.isdigit() and spec.value is None:
            spec = SplitSpec(mode=spec.mode, value=int(low))
        elif low in _PARTS_WORDS:
            spec = SplitSpec(mode="parts", value=spec.value)
    return spec


def describe_spec(spec: SplitSpec | None) -> str:
    """Texto en español con lo que hará la división (para los mensajes)."""
    if not spec or spec.value is None:
        return (
            f"secciones automáticas (~{config.SPLIT_CHAPTERS_PER_PART} capítulos cada una; "
            f"en PDF, ~{config.SPLIT_PAGES_PER_PART} páginas)"
        )
    if spec.mode == "parts":
        return f"en {spec.value} secciones"
    return f"{spec.value} capítulos por sección (en PDF, páginas)"


# --------------------------------------------------------------------------- resultado


@dataclass
class SplitPart:
    """Una sección ya escrita en disco."""

    path: Path
    name: str
    label: str
    index: int
    count: int


@dataclass
class SplitReport:
    """Resumen de la división de un documento."""

    kind: str
    unit_noun: str
    total_units: int
    units_per_part: int
    parts: list[SplitPart] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- utilidades


def _norm(path: str) -> str:
    path = posixpath.normpath(path.replace("\\", "/"))
    while path.startswith("./"):
        path = path[2:]
    return path


def _zip_path(base_dir: str, href: str) -> str:
    target = unquote(href.split("#", 1)[0].split("?", 1)[0])
    return _norm(posixpath.join(base_dir, target)) if base_dir else _norm(target)


def _relative(path: str, directory: str) -> str:
    if not directory:
        return path
    try:
        return posixpath.relpath(path, directory)
    except ValueError:  # pragma: no cover - rutas de unidades distintas
        return path


def _local_name(element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _safe_name(name: str, max_len: int = 120) -> str:
    name = _UNSAFE_NAME_RE.sub("_", name or "documento").strip(" ._") or "documento"
    return name[:max_len].rstrip() if len(name) > max_len else name


def _part_name(stem: str, index: int, count: int, suffix: str) -> str:
    """Nombre de una parte: solo indica qué parte es.

    El rango de capítulos («caps. 1-50») no va en el nombre porque la numeración
    detectada no siempre coincide con la real; queda en la leyenda del mensaje.
    """
    width = max(2, len(str(count)))
    base = _safe_name(stem, 90)
    name = f"{base} - Parte {index:0{width}d} de {count:0{width}d}{suffix}"
    if len(name) > 150:
        keep = 150 - len(suffix)
        name = name[:keep].rstrip(" .-_()") + suffix
    return name


def _title_number(title: str | None) -> int | None:
    if not title:
        return None
    match = re.search(r"\d{1,5}", title)
    return int(match.group(0)) if match else None


def _range_label(
    titles: list[str] | None, first: int, last: int, nouns: tuple[str, str]
) -> str:
    """Etiqueta legible de un rango de unidades («caps. 97-144», «pág. 3»)."""
    singular, plural = nouns
    low = high = None
    if titles:
        low = _title_number(titles[first])
        if low is not None:
            for index in range(last, first - 1, -1):
                high = _title_number(titles[index])
                if high is not None:
                    break
    if low is None or high is None:
        low, high = first + 1, last + 1
    return f"{singular} {low}" if low == high else f"{plural} {low}-{high}"


def _resolve_parts(
    total: int, spec: SplitSpec | None, default_units: int, noun: str, display: str
) -> tuple[int, int]:
    """Traduce la especificación del usuario a (unidades por sección, secciones)."""
    max_parts = max(2, config.SPLIT_MAX_PARTS)
    if spec and spec.mode == "parts" and spec.value:
        target = min(max(2, spec.value), max_parts)
        units = -(-total // target)
    else:
        units = spec.value if (spec and spec.value) else default_units
        units = max(1, min(units, 10000))
    parts = -(-total // units)
    if parts < 2:
        suggested = max(1, total // 3)
        raise SplitError(
            f"«{display}»: tiene {total} {noun} y con {units} por sección solo saldría 1. "
            f"Usa un número menor, por ejemplo /dividir {suggested}."
        )
    if parts > max_parts:
        suggested = -(-total // max_parts)
        raise SplitError(
            f"«{display}»: saldrían {parts} secciones y el máximo es {max_parts}. "
            f"Usa más {noun} por sección, por ejemplo /dividir {suggested}."
        )
    return units, parts


def _parse_xml_or_none(raw: bytes):
    try:
        root = etree.fromstring(raw, _XML_PARSER)
    except (etree.XMLSyntaxError, ValueError):
        return None
    return root


def _decode_text(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig", "replace")
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", "replace")


def _doc_title(raw: bytes) -> str | None:
    """Título de un documento XHTML (para los capítulos sin entrada de índice)."""
    text = raw[:20000].decode("utf-8", "ignore")
    for pattern in (_TITLE_RE, _HEADING_RE):
        match = pattern.search(text)
        if match:
            title = _html_unescape(_TAG_RE.sub("", match.group(1))).strip()
            if title:
                return title[:120]
    return None


# --------------------------------------------------------------------------- entrada


def split_document(
    src_path: Path,
    file_name: str,
    out_dir: Path,
    spec: SplitSpec | None = None,
    cancel: CancelFn | None = None,
) -> SplitReport:
    """Divide el documento en secciones sin traducir.

    Lanza :class:`SplitError` si no se detectan capítulos separables y
    :class:`UnsupportedFormatError` si el formato no se puede dividir.
    """
    suffix = Path(file_name).suffix.lower()
    stem = _safe_name(Path(file_name).stem)
    if suffix == ".epub":
        return _split_epub(src_path, stem, out_dir, spec, cancel, file_name)
    if suffix in (".txt", ".md"):
        return _split_text(src_path, stem, out_dir, suffix, spec, cancel, file_name)
    if suffix == ".pdf":
        return _split_pdf(src_path, stem, out_dir, spec, cancel, file_name)
    raise UnsupportedFormatError(f"No puedo dividir archivos {suffix or 'desconocidos'}.")


# --------------------------------------------------------------------------- EPUB


def _is_content(path: str, media_type: str) -> bool:
    return media_type in _XHTML_TYPES or path.lower().endswith(_XHTML_SUFFIXES)


def _epub_titles(zin, items: dict, names: set[str], nav_id, ncx_id, chapter_ids) -> tuple[dict, int]:
    """Títulos de capítulo desde el índice del EPUB (NCX y/o navegación)."""
    titles: dict[str, str] = {}
    ncx_counts: dict[str, int] = {}
    nav_counts: dict[str, int] = {}

    def add(counter: dict[str, int], path: str, label: str | None) -> None:
        counter[path] = counter.get(path, 0) + 1
        label = (label or "").strip()
        if label:
            titles.setdefault(path, label[:120])

    if ncx_id:
        ncx_path = items[ncx_id]["path"]
        root = _parse_xml_or_none(zin.read(ncx_path)) if ncx_path in names else None
        if root is not None:
            directory = posixpath.dirname(ncx_path)
            for point in root.iter():
                if _local_name(point) != "navpoint":
                    continue
                if any(_local_name(anc) == "navpoint" for anc in point.iterancestors()):
                    continue  # solo el nivel superior: los subíndices no son capítulos
                src = label = None
                for child in point.iter():
                    lname = _local_name(child)
                    if lname == "content" and src is None:
                        src = child.get("src")
                    elif lname == "text" and label is None and child.text:
                        label = child.text
                if src:
                    add(ncx_counts, _zip_path(directory, src), label)

    if nav_id:
        nav_path = items[nav_id]["path"]
        root = None
        if nav_path in names:
            raw = zin.read(nav_path)
            root = _parse_xml_or_none(raw)
            if root is None:
                try:
                    root = etree.fromstring(raw, _HTML_PARSER)
                except etree.XMLSyntaxError:  # pragma: no cover
                    root = None
        if root is not None:
            directory = posixpath.dirname(nav_path)
            navs = [el for el in root.iter() if _local_name(el) == "nav"]
            toc_navs = [
                el
                for el in navs
                if "toc" in ((el.get("{http://www.idpf.org/2007/ops}type") or el.get("type") or ""))
            ]
            for nav in (toc_navs or navs):
                for link in nav.iter():
                    if _local_name(link) != "a":
                        continue
                    href = link.get("href") or ""
                    if not href or href.startswith(("#", "http:", "https:", "mailto:")):
                        continue
                    depth = sum(1 for anc in link.iterancestors() if _local_name(anc) in ("ol", "ul"))
                    if depth != 1:
                        continue  # los subapartados no son capítulos
                    add(nav_counts, _zip_path(directory, href), "".join(link.itertext()))

    entries = 0
    for path in set(ncx_counts) | set(nav_counts):
        entries += max(ncx_counts.get(path, 0), nav_counts.get(path, 0))

    for index, chapter_id in enumerate(chapter_ids):
        path = items[chapter_id]["path"]
        if path not in titles:
            raw = zin.read(path) if path in names else b""
            titles[path] = _doc_title(raw) or f"Sección {index + 1}"
    return titles, entries


def _book_title(root, fallback: str) -> str:
    for element in root.iter():
        if _local_name(element) == "title":
            if any(_local_name(anc) == "metadata" for anc in element.iterancestors()):
                text = (element.text or "").strip()
                if text:
                    return text
    return fallback


def _book_uid(root) -> str | None:
    for element in root.iter():
        if _local_name(element) == "identifier" and (element.text or "").strip():
            return element.text.strip()
    return None


def _ncx_uid(zin, ncx_path: str | None, names: set[str]) -> str | None:
    if not ncx_path or ncx_path not in names:
        return None
    root = _parse_xml_or_none(zin.read(ncx_path))
    if root is None:
        return None
    for meta in root.iter():
        if _local_name(meta) == "meta" and (meta.get("name") or "").strip().lower() == "dtb:uid":
            return (meta.get("content") or "").strip() or None
    return None


def _drop(element) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _rewrite_opf(
    opf_raw: bytes, removed_ids: set[str], removed_paths: set[str], opf_dir: str, part_title: str
) -> bytes:
    """OPF de la sección: fuera los capítulos que no van en ella, título ajustado."""
    root = _parse_xml_or_none(opf_raw)
    if root is None:
        return opf_raw
    for element in list(root.iter()):
        lname = _local_name(element)
        if lname == "item":
            if element.get("id") in removed_ids:
                _drop(element)
        elif lname == "itemref":
            if element.get("idref") in removed_ids:
                _drop(element)
        elif lname == "reference":
            href = element.get("href")
            if href and _norm(_zip_path(opf_dir, href)) in removed_paths:
                _drop(element)
        elif lname == "title":
            if any(_local_name(anc) == "metadata" for anc in element.iterancestors()):
                element.text = part_title
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _build_ncx(
    ncx_path: str, uid: str, part_title: str, toc_entries: list[tuple[str, str]]
) -> bytes:
    directory = posixpath.dirname(ncx_path)
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<ncx xmlns="{_NCX_NS}" version="2005-1">',
        "<head>",
    ]
    if uid:
        lines.append(f'<meta name="dtb:uid" content="{_html_escape(uid, quote=True)}"/>')
    lines += [
        '<meta name="dtb:depth" content="1"/>',
        '<meta name="dtb:totalPageCount" content="0"/>',
        '<meta name="dtb:maxPageNumber" content="0"/>',
        "</head>",
        f"<docTitle><text>{_html_escape(part_title)}</text></docTitle>",
        "<navMap>",
    ]
    for number, (label, path) in enumerate(toc_entries, 1):
        src = _html_escape(_relative(path, directory), quote=True)
        lines.append(
            f'<navPoint id="np-{number}" playOrder="{number}">'
            f"<navLabel><text>{_html_escape(label)}</text></navLabel>"
            f'<content src="{src}"/></navPoint>'
        )
    lines += ["</navMap>", "</ncx>"]
    return "\n".join(lines).encode("utf-8")


def _build_nav(nav_path: str, book_title: str, toc_entries: list[tuple[str, str]]) -> bytes:
    directory = posixpath.dirname(nav_path)
    rows = "\n".join(
        f'    <li><a href="{_html_escape(_relative(path, directory), quote=True)}">'
        f"{_html_escape(label)}</a></li>"
        for label, path in toc_entries
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        f"<head><title>{_html_escape(book_title)}</title><meta charset=\"utf-8\"/></head>\n"
        "<body>\n"
        '<nav epub:type="toc" id="toc">\n'
        f"  <h1>{_html_escape(book_title)}</h1>\n"
        "  <ol>\n"
        f"{rows}\n"
        "  </ol>\n"
        "</nav>\n"
        "</body>\n"
        "</html>\n"
    ).encode("utf-8")


_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _write_member(zout: zipfile.ZipFile, name: str, data: bytes) -> None:
    """Escribe un miembro del zip con fecha fija: la división queda reproducible.

    Así, partir dos veces el mismo libro produce secciones idénticas byte a byte
    y la caché de traducciones puede reutilizarlas (importante en el modo
    «dividir y traducir»).
    """
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zout.writestr(info, data)


def _split_epub(
    src_path: Path, stem: str, out_dir: Path, spec: SplitSpec | None, cancel, display: str
) -> SplitReport:
    notes: list[str] = []
    with zipfile.ZipFile(src_path) as zin:
        check_drm(zin)
        opf_name = find_opf(zin)
        if not opf_name:
            raise UnsupportedFormatError("El EPUB no contiene un archivo OPF válido.")
        names = {name for name in zin.namelist() if not name.endswith("/")}
        opf_dir = posixpath.dirname(opf_name)
        opf_raw = zin.read(opf_name)
        root = _parse_xml_or_none(opf_raw)
        if root is None:
            raise UnsupportedFormatError("No se pudo leer la estructura del EPUB (OPF ilegible).")

        items: dict[str, dict] = {}
        for element in root.iter():
            if _local_name(element) != "item":
                continue
            item_id = element.get("id")
            href = element.get("href")
            if not item_id or not href:
                continue
            items[item_id] = {
                "path": _zip_path(opf_dir, href),
                "media_type": (element.get("media_type") or "").strip().lower(),
                "properties": (element.get("properties") or "").strip(),
            }
        spine = next((el for el in root.iter() if _local_name(el) == "spine"), None)
        if spine is None:
            raise UnsupportedFormatError("El EPUB no tiene spine de lectura.")

        nav_id = next((i for i, item in items.items() if "nav" in item["properties"].split()), None)
        nav_path = items[nav_id]["path"] if nav_id else None
        ncx_id = spine.get("toc")
        if ncx_id not in items:
            ncx_id = next(
                (i for i, item in items.items() if item["media_type"].endswith("dtbncx+xml")), None
            )
        ncx_path = items[ncx_id]["path"] if ncx_id else None

        chapter_ids: list[str] = []
        relaxed_ids: list[str] = []
        for itemref in spine:
            if _local_name(itemref) != "itemref":
                continue
            idref = itemref.get("idref")
            item = items.get(idref) if idref else None
            if not item or idref == nav_id:
                continue
            if not _is_content(item["path"], item["media_type"]):
                continue
            if item["path"] not in names:
                continue
            relaxed_ids.append(idref)
            if (itemref.get("linear") or "").strip().lower() != "no":
                chapter_ids.append(idref)
        if len(chapter_ids) < 2:
            # Algunos libros marcan todo el spine como linear="no".
            chapter_ids = relaxed_ids
        if len(chapter_ids) < 2:
            raise SplitError(
                f"«{display}»: no tiene capítulos separables por archivo (todo el texto parece "
                "estar en un solo documento). Envíalo directamente para traducirlo entero."
            )

        titles, entry_total = _epub_titles(zin, items, names, nav_id, ncx_id, chapter_ids)
        total = len(chapter_ids)
        units, parts_count = _resolve_parts(
            total, spec, config.SPLIT_CHAPTERS_PER_PART, "capítulos", display
        )
        if entry_total >= total * 1.5:
            notes.append(
                "el índice agrupa varios capítulos en el mismo archivo; las secciones se hicieron por archivo"
            )
        book_title = _book_title(root, stem)
        ncx_uid = _ncx_uid(zin, ncx_path, names) or _book_uid(root) or ""
        titles_ordered = [titles[items[cid]["path"]] for cid in chapter_ids]

        report = SplitReport(
            kind="epub", unit_noun="capítulos", total_units=total, units_per_part=units, notes=notes
        )
        for part_index in range(parts_count):
            if cancel and cancel():
                raise JobCancelledError()
            first = part_index * units
            last = min(first + units, total) - 1
            included = set(chapter_ids[first:last + 1])
            removed_ids = {cid for cid in chapter_ids if cid not in included}
            removed_paths = {_norm(items[cid]["path"]) for cid in removed_ids}
            part_label = _range_label(titles_ordered, first, last, _LABEL_CHAPTERS)
            part_title = f"{book_title} - Parte {part_index + 1} de {parts_count}"
            new_opf = _rewrite_opf(opf_raw, removed_ids, removed_paths, opf_dir, part_title)
            toc_entries = [
                (titles_ordered[i], items[chapter_ids[i]]["path"]) for i in range(first, last + 1)
            ]
            new_ncx = _build_ncx(ncx_path, ncx_uid, part_title, toc_entries) if ncx_path else None
            new_nav = _build_nav(nav_path, book_title, toc_entries) if nav_path else None
            name = _part_name(stem, part_index + 1, parts_count, ".epub")
            out_path = out_dir / name
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
                info = zipfile.ZipInfo("mimetype", date_time=_FIXED_ZIP_TIME)
                zout.writestr(info, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
                for member in zin.namelist():
                    if member.endswith("/") or member == "mimetype":
                        continue
                    if _norm(member) in removed_paths:
                        continue
                    if member == opf_name:
                        _write_member(zout, member, new_opf)
                    elif nav_path and member == nav_path and new_nav is not None:
                        _write_member(zout, member, new_nav)
                    elif ncx_path and member == ncx_path and new_ncx is not None:
                        _write_member(zout, member, new_ncx)
                    else:
                        _write_member(zout, member, zin.read(member))
            report.parts.append(
                SplitPart(
                    path=out_path,
                    name=name,
                    label=part_label,
                    index=part_index + 1,
                    count=parts_count,
                )
            )
    return report


# --------------------------------------------------------------------------- TXT / MD


_CN_NUMBER = r"(?:\d{1,5}|[一二三四五六七八九十百千万零〇两]{1,6})"
_CHAPTER_PATTERNS = (
    re.compile(r"^(?:cap[ií]tulo|capitulo|cap\.?)\s*(?:\d{1,4}|[ivxlcdm]{1,8})(?![a-z0-9])", re.I),
    re.compile(r"^(?:chapter|ch\.?)\s*(?:\d{1,4}|[ivxlcdm]{1,8})(?![a-z0-9])", re.I),
    re.compile(
        r"^(?:chapitre|kapitel|capitolo|hoofdstuk|rozdzia[lł]|b[oö]l[uü]m|bab|chương|chuong|глава)"
        r"\s*\d{1,4}(?![a-z0-9])",
        re.I,
    ),
    re.compile(r"^第\s*" + _CN_NUMBER + r"\s*[章話话回节節卷]"),
    re.compile(r"^(?:제\s*)?\d{1,5}\s*[화장회권]"),
    re.compile(
        r"^(?:pr[oó]logo|prologo|ep[ií]logo|epilogo|interludio|interlude|prologue|epilogue|"
        r"序章|終章|终章|番外|後書き|あとがき)(?:\s*[:.\-—–].{0,60})?$",
        re.I,
    ),
)


def _match_chapter_title(line: str, allow_markdown: bool) -> str | None:
    for pattern in _CHAPTER_PATTERNS:
        if pattern.match(line):
            return line
    if allow_markdown:
        match = _MD_HEADING_RE.match(line)
        if match:
            return match.group(1).strip()
    return None


def _detect_text_chapters(
    lines: list[str], allow_markdown: bool
) -> tuple[list[tuple[int, str]], list[str]]:
    """Encuentra las líneas que abren capítulo: ``(índice de línea, título)``."""
    notes: list[str] = []
    raw_entries: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        text = line.strip()
        if not text or len(text) > 120:
            continue
        title = _match_chapter_title(text, allow_markdown)
        if title:
            raw_entries.append((index, title))
    if not raw_entries:
        return [], notes

    # Los bloques de encabezados en líneas seguidas (índice del principio) se
    # ignoran; basta con que haya una línea en blanco para no fusionarlos.
    kept: list[tuple[int, str]] = []
    group: list[tuple[int, str]] = []
    for entry in raw_entries:
        if group and entry[0] - group[-1][0] < 2:
            group.append(entry)
        else:
            if len(group) >= 3:
                notes.append("se ignoró un bloque de posibles encabezados seguidos (índice)")
            else:
                kept.extend(group)
            group = [entry]
    if len(group) >= 3:
        notes.append("se ignoró un bloque de posibles encabezados seguidos (índice)")
    else:
        kept.extend(group)

    numbers = [n for _index, title in kept if (n := _title_number(title)) is not None]
    if len(numbers) >= 4:
        ascending = sum(1 for a, b in zip(numbers, numbers[1:]) if b >= a)
        if ascending / (len(numbers) - 1) < 0.7:
            notes.append("el orden de los capítulos no fue siempre creciente; revisa las secciones")
    return kept, notes


def _split_text(
    src_path: Path, stem: str, out_dir: Path, suffix: str, spec: SplitSpec | None, cancel, display: str
) -> SplitReport:
    text = _decode_text(src_path.read_bytes())
    lines = text.splitlines(keepends=True)
    candidates, notes = _detect_text_chapters(lines, allow_markdown=(suffix == ".md"))
    if len(candidates) < 2:
        raise SplitError(
            f"«{display}»: no detecté capítulos (se necesitan al menos 2). Los encabezados deben "
            "ir en su propia línea, del tipo «Capítulo 12», «Chapter 12», «第12章» o «12화»."
        )
    total = len(candidates)
    units, parts_count = _resolve_parts(
        total, spec, config.SPLIT_CHAPTERS_PER_PART, "capítulos", display
    )
    boundaries = [index for index, _title in candidates]
    titles = [title for _index, title in candidates]

    report = SplitReport(
        kind="text", unit_noun="capítulos", total_units=total, units_per_part=units, notes=notes
    )
    for part_index in range(parts_count):
        if cancel and cancel():
            raise JobCancelledError()
        first = part_index * units
        last = min(first + units, total) - 1
        start = 0 if first == 0 else boundaries[first]  # el prólogo abre la primera sección
        end = boundaries[last + 1] if last + 1 < total else len(lines)
        label = _range_label(titles, first, last, _LABEL_CHAPTERS)
        name = _part_name(stem, part_index + 1, parts_count, suffix)
        out_path = out_dir / name
        # Se escribe en binario para conservar los finales de línea originales.
        out_path.write_bytes("".join(lines[start:end]).encode("utf-8"))
        report.parts.append(
            SplitPart(
                path=out_path, name=name, label=label, index=part_index + 1, count=parts_count
            )
        )
    return report


# --------------------------------------------------------------------------- PDF


_PDF_META_KEYS = (
    "title",
    "author",
    "subject",
    "keywords",
    "creator",
    "producer",
    "creationDate",
    "modDate",
)


def _split_pdf(
    src_path: Path, stem: str, out_dir: Path, spec: SplitSpec | None, cancel, display: str
) -> SplitReport:
    notes: list[str] = []
    document = fitz.open(str(src_path))
    try:
        page_count = document.page_count
        if page_count < 2:
            raise SplitError(f"«{display}»: el PDF solo tiene una página.")
        toc = document.get_toc() or []
        starts = sorted(
            {
                int(entry[2])
                for entry in toc
                if len(entry) >= 3 and entry[0] == 1 and isinstance(entry[2], int) and entry[2] > 0
            }
        )
        starts = [page for page in starts if 1 <= page <= page_count]
        if len(starts) >= 2:
            unit_noun, default_units = "capítulos", config.SPLIT_CHAPTERS_PER_PART
            ranges: list[tuple[int, int]] = []
            for position, start in enumerate(starts):
                first_page = 0 if position == 0 else start - 1
                last_page = (starts[position + 1] - 2) if position + 1 < len(starts) else page_count - 1
                ranges.append((first_page, last_page))
            label_nouns = _LABEL_CHAPTERS
        else:
            if toc:
                notes.append("el PDF no tiene un índice de capítulos utilizable; se dividió por páginas")
            unit_noun, default_units = "páginas", config.SPLIT_PAGES_PER_PART
            ranges = [(page, page) for page in range(page_count)]
            label_nouns = _LABEL_PAGES

        total = len(ranges)
        units, parts_count = _resolve_parts(total, spec, default_units, unit_noun, display)
        book_title = (document.metadata or {}).get("title") or stem

        report = SplitReport(
            kind="pdf", unit_noun=unit_noun, total_units=total, units_per_part=units, notes=notes
        )
        for part_index in range(parts_count):
            if cancel and cancel():
                raise JobCancelledError()
            first_unit = part_index * units
            last_unit = min(first_unit + units, total) - 1
            start_page = ranges[first_unit][0]
            end_page = ranges[last_unit][1]
            part = fitz.open()
            try:
                part.insert_pdf(document, from_page=start_page, to_page=end_page)
                metadata = {
                    key: value
                    for key, value in (document.metadata or {}).items()
                    if key in _PDF_META_KEYS and isinstance(value, str)
                }
                metadata["title"] = f"{book_title} - Parte {part_index + 1} de {parts_count}"
                part.set_metadata(metadata)
                if toc:
                    part_toc = []
                    for entry in toc:
                        if len(entry) < 3 or not isinstance(entry[2], int):
                            continue
                        page = entry[2] - 1
                        if start_page <= page <= end_page:
                            part_toc.append([1, str(entry[1]), page - start_page + 1])
                    if part_toc:
                        try:
                            part.set_toc(part_toc)
                        except Exception:  # noqa: BLE001 - el índice es opcional
                            logger.debug("No se pudo escribir el índice de la sección", exc_info=True)
                label = _range_label(None, first_unit, last_unit, label_nouns)
                name = _part_name(stem, part_index + 1, parts_count, ".pdf")
                out_path = out_dir / name
                part.save(str(out_path))
            finally:
                part.close()
            report.parts.append(
                SplitPart(
                    path=out_path, name=name, label=label, index=part_index + 1, count=parts_count
                )
            )
    finally:
        document.close()
    return report
