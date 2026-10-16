"""Pruebas automáticas del bot (no necesitan token de Telegram).

Uso:
    python tests/selftest.py            # con un traductor simulado
    python tests/selftest.py --real     # además, una prueba real con Google

Comprueba que la traducción de PDF y EPUB conserva estructura, estilos,
imágenes y enlaces, que el texto original se reemplaza de verdad, que los lotes
de traducción funcionan, que las peticiones bloqueadas se reintentan, que las
traducciones largas se reanudan donde se quedaron y que el divisor de novelas
detecta capítulos y genera secciones válidas y sin pérdidas.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from lxml import etree

import languages
import pdf_translate
from checkpoints import JobStore, file_digest
from epub_translate import translate_epub
from errors import JobCancelledError, ScannedPdfError, SplitError, UnsupportedFormatError
from health import start_health_server
from pdf_translate import translate_pdf
from pipeline import DocumentTask, format_duration, safe_filename
from splitter import SplitSpec, parse_split_command, split_document
from translator import (
    AutoTranslator,
    GoogleWebTranslator,
    MicrosoftWebTranslator,
    TranslationError,
    split_text,
)
from translator_api import GoogleApiTranslator

TESTS_DIR = Path(__file__).resolve().parent
WORK = TESTS_DIR / "_work"

XML_PARSER = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, recover=False)


class MockTranslator:
    """Traductor simulado: escribe [ES] y pasa el texto a MAYÚSCULAS.

    Así se comprueba que el texto original se sustituye (no se duplica) y que
    el ajuste de cajas funciona con textos más largos.
    """

    def __init__(self, target_lang: str = "es", **_kwargs) -> None:
        self.target_lang = target_lang
        self.detected_lang = None
        self.calls = 0
        self.chars = 0

    async def __aenter__(self) -> "MockTranslator":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def translate(self, text: str) -> str:
        self.calls += 1
        self.chars += len(text)
        if not text.strip():
            return text
        if self.detected_lang is None:
            self.detected_lang = "en"
        lead_len = len(text) - len(text.lstrip())
        trail_len = len(text) - len(text.rstrip())
        lead = text[:lead_len]
        trail = text[len(text) - trail_len:] if trail_len else ""
        return f"{lead}[ES] {text.strip().upper()}{trail}"

    async def translate_many(self, texts: list[str]) -> list[str]:
        return [await self.translate(text) for text in texts]

    async def detect(self, text: str) -> str | None:
        self.detected_lang = "en"
        return "en"


class FlakyTranslator(MockTranslator):
    """Traductor que falla a propósito en la llamada número ``fail_at``."""

    def __init__(self, target_lang: str = "es", *, fail_at: int | None = None) -> None:
        super().__init__(target_lang)
        self.fail_at = fail_at
        self.batch_calls = 0

    async def translate_many(self, texts: list[str]) -> list[str]:
        self.batch_calls += 1
        if self.fail_at is not None and self.batch_calls >= self.fail_at:
            raise TranslationError("fallo simulado")
        return await super().translate_many(texts)


# --------------------------------------------------------------------------- creación de muestras


def _png_bytes() -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 30, 30))
    pix.set_rect(pix.irect, (90, 150, 90))
    return pix.tobytes("png")


def make_sample_pdf(path: Path, pages: int = 2) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, 520, 200),
        "Chapter One. This is the original English text of the sample document. " * 4,
        fontsize=11,
    )
    page.insert_textbox(fitz.Rect(72, 230, 520, 260), "A Bold Heading", fontsize=16, fontname="hebo")
    page.draw_rect(fitz.Rect(60, 280, 535, 430), fill=(0.9, 0.95, 1.0))
    page.insert_textbox(
        fitz.Rect(72, 292, 520, 420),
        "Text over a coloured background that must keep its background. " * 5,
        fontsize=11,
    )
    image_path = WORK / "sample_image.png"
    image_path.write_bytes(_png_bytes())
    page.insert_image(fitz.Rect(440, 20, 500, 80), filename=str(image_path))
    for index in range(1, max(2, pages)):
        extra = doc.new_page()
        extra.insert_textbox(
            fitz.Rect(72, 72, 520, 400),
            f"Text of page {index + 1} for the translation test. " * 8,
            fontsize=12,
        )
    doc.save(str(path))
    doc.close()


CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">sample-123</dc:identifier>
    <dc:title>Sample Book</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>Tester</dc:creator>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover" href="cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="nav"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="sample-123"/></head>
  <docTitle><text>Sample Book</text></docTitle>
  <navMap>
    <navPoint id="np-1" playOrder="1">
      <navLabel><text>Chapter One</text></navLabel><content src="ch1.xhtml"/>
    </navPoint>
    <navPoint id="np-2" playOrder="2">
      <navLabel><text>Chapter Two</text></navLabel><content src="ch2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><title>Índice</title><meta charset="utf-8"/></head>
<body>
<nav epub:type="toc" id="toc"><h1>Contents</h1>
<ol>
  <li><a href="ch1.xhtml">Chapter One</a></li>
  <li><a href="ch2.xhtml">Chapter Two</a></li>
</ol>
</nav>
</body>
</html>
"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><title>Chapter 1</title><meta charset="utf-8"/>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body epub:type="bodymatter">
<h1>Chapter One</h1>
<p>Hello <i>world</i>, this is the first paragraph with a
<a href="ch2.xhtml">link to chapter two</a> and a non-breaking space&nbsp;here.</p>
<p>Second paragraph of the sample chapter.</p>
<p><img src="cover.png" alt="cover"/> Text after an inline image.</p>
</body>
</html>
"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title>Chapter 2</title><meta charset="utf-8"/></head>
<body><h2>Chapter Two</h2><p>More text to translate in chapter two.</p></body>
</html>
"""

STYLE = b"body { font-family: serif; }"


def make_sample_epub(path: Path, drm: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as zout:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zout.writestr(info, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zout.writestr("META-INF/container.xml", CONTAINER_XML)
        if drm:
            zout.writestr(
                "META-INF/encryption.xml",
                '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
                '<CipherData><CipherReference URI="OEBPS/ch1.xhtml"/></CipherData>'
                "</EncryptedData></encryption>",
            )
        zout.writestr("OEBPS/content.opf", OPF)
        zout.writestr("OEBPS/toc.ncx", NCX)
        zout.writestr("OEBPS/nav.xhtml", NAV)
        zout.writestr("OEBPS/ch1.xhtml", CH1)
        zout.writestr("OEBPS/ch2.xhtml", CH2)
        zout.writestr("OEBPS/style.css", STYLE)
        zout.writestr("OEBPS/cover.png", _png_bytes())


CHAPTER_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ko">
<head><title>{title}</title><meta charset="utf-8"/>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><h1>{title}</h1><p>Texto original del capítulo {number}.</p>
<p>Otro párrafo con <i>cursiva</i> y un enlace interno al capítulo 1.</p></body>
</html>
"""


def make_chapter_epub(path: Path, chapters: int = 12) -> None:
    """EPUB con muchos capítulos, uno por archivo (como las novelas web)."""
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    spine = ['<itemref idref="nav"/>']
    links = []
    points = []
    for number in range(1, chapters + 1):
        manifest.append(
            f'<item id="ch{number}" href="ch{number}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="ch{number}"/>')
        links.append(f'    <li><a href="ch{number}.xhtml">Capítulo {number}</a></li>')
        points.append(
            f'    <navPoint id="np-{number}" playOrder="{number}">'
            f"<navLabel><text>Capítulo {number}</text></navLabel>"
            f'<content src="ch{number}.xhtml"/></navPoint>'
        )
    manifest += [
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="cover" href="cover.png" media-type="image/png"/>',
    ]
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="id">cap-{chapters}</dc:identifier>\n'
        "    <dc:title>Novela de prueba</dc:title>\n"
        "    <dc:language>ko</dc:language>\n"
        "  </metadata>\n"
        "  <manifest>\n    "
        + "\n    ".join(manifest)
        + "\n  </manifest>\n"
        '  <spine toc="ncx">\n    '
        + "\n    ".join(spine)
        + "\n  </spine>\n"
        "</package>\n"
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="cap-{chapters}"/></head>\n'
        "<docTitle><text>Novela de prueba</text></docTitle>\n"
        "<navMap>\n" + "\n".join(points) + "\n</navMap>\n</ncx>\n"
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ko">\n'
        '<head><title>Índice</title><meta charset="utf-8"/></head>\n'
        "<body>\n"
        '<nav epub:type="toc" id="toc"><h1>Índice</h1>\n'
        "<ol>\n" + "\n".join(links) + "\n</ol>\n"
        "</nav>\n</body>\n</html>\n"
    )
    with zipfile.ZipFile(path, "w") as zout:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zout.writestr(info, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zout.writestr("META-INF/container.xml", CONTAINER_XML)
        zout.writestr("OEBPS/content.opf", opf)
        zout.writestr("OEBPS/toc.ncx", ncx)
        zout.writestr("OEBPS/nav.xhtml", nav)
        for number in range(1, chapters + 1):
            zout.writestr(
                f"OEBPS/ch{number}.xhtml",
                CHAPTER_XHTML.format(title=f"Capítulo {number}", number=number),
            )
        zout.writestr("OEBPS/style.css", STYLE)
        zout.writestr("OEBPS/cover.png", _png_bytes())


def make_novel_txt(path: Path, chapters: int = 12) -> str:
    """TXT de novela con índice al principio y encabezados «Capítulo N»."""
    lines = ["Novela de prueba", ""]
    for number in range(1, chapters + 1):
        lines.append(f"Capítulo {number}")  # índice (bloque seguido: debe ignorarse)
    lines.append("")
    for number in range(1, chapters + 1):
        lines.append(f"Capítulo {number}: El comienzo")
        lines.append("")
        for paragraph in range(4):
            lines.append(f"Texto del capítulo {number}, párrafo {paragraph}.")
        lines.append("")
    text = "\n".join(lines)
    path.write_text(text, "utf-8")
    return text


# --------------------------------------------------------------------------- pruebas


async def test_pdf_mock() -> None:
    src = WORK / "sample.pdf"
    out = WORK / "sample_es.pdf"
    make_sample_pdf(src)
    translator = MockTranslator("es")
    stats = await translate_pdf(src, out, translator)

    assert stats["pages"] == 2, stats
    assert stats["chars"] > 100, stats

    doc = fitz.open(str(out))
    try:
        assert doc.page_count == 2
        text1 = doc[0].get_text()
        text2 = doc[1].get_text()
        assert "[ES]" in text1, "no se insertó la traducción en la página 1"
        assert "[ES]" in text2, "no se insertó la traducción en la página 2"
        assert "CHAPTER ONE" in text1, "el texto no se pasó a mayúsculas (mock)"
        assert "Chapter One" not in text1, "el texto original no se eliminó"
        assert len(doc[0].get_images(full=True)) == 1, "se perdió la imagen de la página 1"
    finally:
        doc.close()
    print("OK · PDF (mock):", stats)


async def test_scanned_pdf() -> None:
    src = WORK / "scanned.pdf"
    out = WORK / "scanned_es.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(100, 100, 300, 300), fill=(0.2, 0.2, 0.2))
    doc.save(str(src))
    doc.close()
    try:
        await translate_pdf(src, out, MockTranslator())
    except ScannedPdfError:
        print("OK · PDF sin texto detectado como escaneo")
        return
    raise AssertionError("un PDF sin texto debería lanzar ScannedPdfError")


async def test_pdf_cancel() -> None:
    src = WORK / "sample.pdf"
    out = WORK / "sample_cancel.pdf"
    try:
        await translate_pdf(src, out, MockTranslator(), cancel=lambda: True)
    except JobCancelledError:
        print("OK · cancelación de PDF")
        return
    raise AssertionError("la cancelación debería lanzar JobCancelledError")


async def test_epub_mock() -> None:
    src = WORK / "sample.epub"
    out = WORK / "sample_es.epub"
    make_sample_epub(src)
    translator = MockTranslator("es")
    stats = await translate_epub(src, out, translator)
    assert stats["units"] > 3, stats

    with zipfile.ZipFile(out) as zin:
        names = zin.namelist()
        assert names[0] == "mimetype", names[:3]
        assert zin.getinfo("mimetype").compress_type == zipfile.ZIP_STORED

        # el resto de archivos se conserva byte a byte
        assert zin.read("OEBPS/style.css") == STYLE
        assert zin.read("OEBPS/cover.png") == _png_bytes()
        assert zin.read("OEBPS/ch2.xhtml") != CH2.encode("utf-8")

        ch1 = zin.read("OEBPS/ch1.xhtml").decode("utf-8")
        assert "[ES]" in ch1
        assert "CHAPTER ONE" in ch1, ch1[:300]
        assert "Chapter One" not in ch1, "el texto original sigue ahí"
        assert 'href="ch2.xhtml"' in ch1, "se perdió el enlace"
        assert "\xa0" in ch1, "&nbsp; debería conservarse como carácter"
        assert "&nbsp;" not in ch1
        assert "<i>" in ch1 or "<i " in ch1, "se perdió la cursiva"
        assert 'src="cover.png"' in ch1, "se perdió la imagen del capítulo"
        assert 'xmlns="http://www.w3.org/1999/xhtml"' in ch1
        assert 'epub:type="bodymatter"' in ch1

        nav = zin.read("OEBPS/nav.xhtml").decode("utf-8")
        assert "[ES]" in nav, "el índice de navegación no se tradujo"
        ncx = zin.read("OEBPS/toc.ncx").decode("utf-8")
        assert "[ES]" in ncx, "el índice NCX no se tradujo"
        assert "<dc:title>Sample Book (ES)</dc:title>" in zin.read("OEBPS/content.opf").decode("utf-8")
        assert "<dc:language>es</dc:language>" in zin.read("OEBPS/content.opf").decode("utf-8")

        for name in names:
            if name.endswith((".xhtml", ".html", ".ncx", ".opf")):
                etree.fromstring(zin.read(name), XML_PARSER)  # debe ser XML bien formado

    print("OK · EPUB (mock):", stats)


async def test_epub_drm() -> None:
    src = WORK / "drm.epub"
    out = WORK / "drm_es.epub"
    make_sample_epub(src, drm=True)
    try:
        await translate_epub(src, out, MockTranslator())
    except UnsupportedFormatError as err:
        assert "DRM" in str(err)
        print("OK · EPUB con DRM detectado")
        return
    raise AssertionError("un EPUB con DRM debería lanzar UnsupportedFormatError")


# --------------------------------------------------------------------------- reanudación


async def test_epub_resume() -> None:
    """Un EPUB interrumpido continúa por el capítulo donde iba."""
    src = WORK / "sample.epub"
    out1 = WORK / "resume1.epub"
    out2 = WORK / "resume2.epub"
    make_sample_epub(src)
    store = JobStore(WORK / "store_epub" / "jobs", WORK / "store_epub" / "cache")
    key = store.key_for(file_digest(src), "es", 7)

    broken = FlakyTranslator("es", fail_at=3)
    try:
        await translate_epub(src, out1, broken, store=store, key=key)
    except TranslationError:
        pass
    else:
        raise AssertionError("se esperaba el fallo simulado")
    meta = store.load_meta(key)
    assert meta.get("done") == [0, 1], meta
    assert store.blob_path(key, 0).exists()
    assert not store.blob_path(key, 2).exists()
    assert not out1.exists(), "no debería generarse el EPUB con la traducción incompleta"

    healthy = FlakyTranslator("es")
    stats = await translate_epub(src, out2, healthy, store=store, key=key)
    assert stats["resumed"] == 2, stats
    assert healthy.batch_calls == 2, healthy.batch_calls
    assert stats["translated"] == 4, stats

    with zipfile.ZipFile(out2) as zin:
        ch1 = zin.read("OEBPS/ch1.xhtml").decode("utf-8")
        assert "CHAPTER ONE" in ch1 and "[ES]" in ch1
    print("OK · reanudación de EPUB:", stats)


async def test_pdf_resume() -> None:
    """Un PDF interrumpido continúa desde la última página guardada."""
    src = WORK / "big.pdf"
    out1 = WORK / "big_es.pdf"
    out2 = WORK / "big_es2.pdf"
    make_sample_pdf(src, pages=3)
    store = JobStore(WORK / "store_pdf" / "jobs", WORK / "store_pdf" / "cache")
    key = store.key_for(file_digest(src), "es", 8)

    original_every = pdf_translate.CHECKPOINT_EVERY
    pdf_translate.CHECKPOINT_EVERY = 1  # guardar en cada página para probar la reanudación
    try:
        broken = FlakyTranslator("es", fail_at=3)
        try:
            await translate_pdf(src, out1, broken, store=store, key=key)
        except TranslationError:
            pass
        else:
            raise AssertionError("se esperaba el fallo simulado")
        meta = store.load_meta(key)
        assert meta.get("pages_done") == 2, meta
        assert meta.get("partial"), meta

        healthy = FlakyTranslator("es")
        stats = await translate_pdf(src, out2, healthy, store=store, key=key)
    finally:
        pdf_translate.CHECKPOINT_EVERY = original_every

    assert stats["resumed"] == 2, stats
    assert healthy.batch_calls == 1, healthy.batch_calls  # solo la página pendiente
    doc = fitz.open(str(out2))
    try:
        assert doc.page_count == 3
        assert all("[ES]" in doc[index].get_text() for index in range(3))
    finally:
        doc.close()
    print("OK · reanudación de PDF:", stats)


async def test_txt_resume() -> None:
    """Un TXT interrumpido continúa por el párrafo donde iba."""
    import pipeline

    src = WORK / "novela.txt"
    lines = [f"Line number {index} of the sample novel with some words." for index in range(100)]
    src.write_text("\n".join(lines), "utf-8")
    store = JobStore(WORK / "store_txt" / "jobs", WORK / "store_txt" / "cache")
    key = store.key_for(file_digest(src), "es", 9)

    def make_task() -> DocumentTask:
        return DocumentTask(
            task_id="txt_resume",
            user_id=9,
            chat_id=9,
            status_message_id=1,
            src_message_id=None,
            src_path=src,
            file_name="novela.txt",
            lang="es",
        )

    async def noop(_fraction: float, _note: str) -> None:
        return None

    broken = FlakyTranslator("es", fail_at=3)
    try:
        await pipeline._translate_text_file(make_task(), broken, noop, store, key)
    except TranslationError:
        pass
    else:
        raise AssertionError("se esperaba el fallo simulado")

    healthy = FlakyTranslator("es")
    stats = await pipeline._translate_text_file(make_task(), healthy, noop, store, key)
    assert stats["resumed"] == 64, stats
    assert stats["lines"] == 100, stats

    translated = src.with_name("novela [ES].txt").read_text("utf-8")
    assert translated.count("[ES]") == 100, translated.count("[ES]")
    assert "Line number 99" not in translated
    print("OK · reanudación de TXT:", stats)


# --------------------------------------------------------------------------- divisor de novelas


def test_split_command() -> None:
    assert parse_split_command("/dividir") == SplitSpec("units", None)
    assert parse_split_command("/dividir 25") == SplitSpec("units", 25)
    assert parse_split_command("dividir 8 partes") == SplitSpec("parts", 8)
    assert parse_split_command("/partir 60 secciones") == SplitSpec("parts", 60)
    assert parse_split_command("/split") == SplitSpec("units", None)
    assert parse_split_command("/traducir 25") is None
    assert parse_split_command("Hola mundo") is None
    assert parse_split_command("") is None
    # el tamaño del pie de foto no importa: solo el primer número y «partes»
    assert parse_split_command("/dividir 3 partes por favor") == SplitSpec("parts", 3)
    print("OK · comandos de /dividir")


def test_split_text() -> None:
    src = WORK / "novela_split.txt"
    out_dir = WORK / "split_txt"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = make_novel_txt(src, chapters=130)

    report = split_document(src, "novela_split.txt", out_dir, None)  # automático (50)
    assert report.kind == "text" and report.total_units == 130, report
    assert len(report.parts) == 3, [p.label for p in report.parts]
    assert [p.label for p in report.parts] == ["caps. 1-50", "caps. 51-100", "caps. 101-130"]
    assert report.parts[0].name.startswith("novela_split - Parte 01 de 03")

    first = report.parts[0].path.read_text("utf-8")
    assert first.startswith("Novela de prueba"), "el prólogo debería abrir la primera sección"
    assert "Capítulo 50: El comienzo" in first
    assert "Capítulo 51: El comienzo" not in first
    third = report.parts[2].path.read_text("utf-8")
    assert third.startswith("Capítulo 101: El comienzo")

    joined = "".join(part.path.read_text("utf-8") for part in report.parts)
    assert joined == text, "la división pierde o duplica texto"

    # «en 2 partes» reparte los capítulos a la mitad
    report2 = split_document(src, "novela_split.txt", out_dir, SplitSpec("parts", 2))
    assert len(report2.parts) == 2 and report2.units_per_part == 65, report2.units_per_part
    assert report2.parts[1].label == "caps. 66-130"

    # un libro sin capítulos detectables no se puede dividir
    plain = WORK / "sin_capitulos.txt"
    plain.write_text("Solo un párrafo\nsin ninguna estructura", "utf-8")
    try:
        split_document(plain, "sin_capitulos.txt", out_dir, None)
    except SplitError as err:
        assert "capítulos" in str(err)
    else:
        raise AssertionError("un TXT sin capítulos debería lanzar SplitError")

    # pedir más capítulos por sección de los que hay tampoco vale
    try:
        split_document(src, "novela_split.txt", out_dir, SplitSpec("units", 500))
    except SplitError as err:
        assert "solo saldría 1" in str(err)
    else:
        raise AssertionError("una sección única debería lanzar SplitError")
    print("OK · división de TXT:", [p.label for p in report.parts])


async def test_split_epub() -> None:
    src = WORK / "novela_split.epub"
    out_dir = WORK / "split_epub"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    make_chapter_epub(src, chapters=12)

    report = split_document(src, "novela_split.epub", out_dir, SplitSpec("units", 5))
    assert report.kind == "epub" and report.total_units == 12, report
    assert [p.label for p in report.parts] == ["caps. 1-5", "caps. 6-10", "caps. 11-12"]

    with zipfile.ZipFile(report.parts[0].path) as zin:
        names = zin.namelist()
        assert names[0] == "mimetype", names[:2]
        assert zin.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert "OEBPS/ch1.xhtml" in names and "OEBPS/ch5.xhtml" in names
        assert "OEBPS/ch6.xhtml" not in names, "la sección incluye capítulos que no le tocan"
        assert "OEBPS/style.css" in names and "OEBPS/cover.png" in names
        opf = zin.read("OEBPS/content.opf").decode("utf-8")
        assert 'id="ch1"' in opf and 'id="ch6"' not in opf, opf
        assert "Parte 1 de 3" in opf, opf
        ncx = zin.read("OEBPS/toc.ncx").decode("utf-8")
        assert ncx.count("<navPoint") == 5, ncx
        nav = zin.read("OEBPS/nav.xhtml").decode("utf-8")
        assert nav.count("<li>") == 5, nav
        for name in names:
            if name.endswith((".xhtml", ".ncx", ".opf")):
                etree.fromstring(zin.read(name), XML_PARSER)

    with zipfile.ZipFile(report.parts[2].path) as zin:
        names = set(zin.namelist())
        assert {"OEBPS/ch11.xhtml", "OEBPS/ch12.xhtml"} <= names
        assert "OEBPS/ch10.xhtml" not in names

    # cada sección es un EPUB válido que se puede traducir por separado
    out = WORK / "novela_split_parte1_es.epub"
    stats = await translate_epub(report.parts[0].path, out, MockTranslator("es"))
    assert stats["units"] > 0, stats
    with zipfile.ZipFile(out) as zin:
        opf = zin.read("OEBPS/content.opf").decode("utf-8")
        assert "Parte 1 de 3" in opf and "(ES)" in opf
        chapter = zin.read("OEBPS/ch3.xhtml").decode("utf-8")
        assert "[ES]" in chapter

    # «en N partes» reparte a partes iguales
    report2 = split_document(src, "novela_split.epub", out_dir, SplitSpec("parts", 2))
    assert len(report2.parts) == 2 and report2.units_per_part == 6, report2.units_per_part
    recovered = []
    for part in report2.parts:
        with zipfile.ZipFile(part.path) as zin:
            recovered.extend(
                name for name in zin.namelist() if name.startswith("OEBPS/ch") and name.endswith(".xhtml")
            )
    assert len(recovered) == 12 and len(set(recovered)) == 12, recovered
    print("OK · división de EPUB:", [p.label for p in report.parts])


async def test_split_pdf() -> None:
    src = WORK / "libro_split.pdf"
    out_dir = WORK / "split_pdf"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    make_sample_pdf(src, pages=6)

    report = split_document(src, "libro_split.pdf", out_dir, SplitSpec("units", 2))
    assert report.unit_noun == "páginas" and report.total_units == 6, report
    assert [p.label for p in report.parts] == ["págs. 1-2", "págs. 3-4", "págs. 5-6"]
    for part in report.parts:
        with fitz.open(str(part.path)) as doc:
            assert doc.page_count == 2, (part.name, doc.page_count)

    # con índice (marcadores) se divide por capítulos
    bookmarked = WORK / "libro_con_indice.pdf"
    doc = fitz.open()
    for _ in range(6):
        doc.new_page()
    doc.set_toc([[1, "Capítulo 1", 1], [1, "Capítulo 2", 3], [1, "Capítulo 3", 5]])
    doc.save(str(bookmarked))
    doc.close()

    report2 = split_document(bookmarked, "libro_con_indice.pdf", out_dir, SplitSpec("units", 1))
    assert report2.unit_noun == "capítulos" and report2.total_units == 3, report2
    assert [p.label for p in report2.parts] == ["cap. 1", "cap. 2", "cap. 3"]
    sizes = []
    for part in report2.parts:
        with fitz.open(str(part.path)) as piece:
            sizes.append(piece.page_count)
    assert sizes == [2, 2, 2], sizes
    print("OK · división de PDF:", [p.label for p in report2.parts])


# --------------------------------------------------------------------------- traductor


def _google_handler(calls: list[str], *, merged: bool = False, fails: int = 0):
    """Simula el endpoint web de Google (agrupa líneas y puede fallar o mezclarlas)."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode("utf-8"))
        text = query.get("q", [""])[0]
        calls.append(text)
        if len(calls) <= fails:
            return httpx.Response(429, text="too many requests")
        lines = [line for line in text.split("\n") if line.strip()]
        if merged:
            segments = [[f"[ES] {' '.join(lines).upper()}", text, None, None, 10]]
        else:
            segments = [
                [f"[ES] {line.upper()}" + ("\n" if index < len(lines) - 1 else ""), line, None, None, 10]
                for index, line in enumerate(lines)
            ]
        return httpx.Response(200, json=[segments, None, "en"], headers={"content-type": "application/json"})

    return handler


async def test_web_batches() -> None:
    """Varios párrafos cortos deben ir en una sola petición."""
    calls: list[str] = []
    transport = httpx.MockTransport(_google_handler(calls))
    texts = [f"Paragraph number {index} with enough words to translate." for index in range(10)]
    async with GoogleWebTranslator("es", concurrency=2, transport=transport) as translator:
        results = await translator.translate_many(texts)

    assert len(calls) == 1, f"se esperaba 1 petición, hubo {len(calls)}"
    assert all(result.startswith("[ES] ") for result in results), results
    assert "PARAGRAPH NUMBER 0" in results[0], results[0]
    assert translator.detected_lang == "en"

    # Muchos textos: varios lotes, todos correctos
    calls.clear()
    many = [f"Sentence {index} of the book that we are translating today." for index in range(200)]
    async with GoogleWebTranslator("es", concurrency=4, transport=httpx.MockTransport(_google_handler(calls))) as translator:
        results = await translator.translate_many(many)
    assert 1 < len(calls) < 40, f"demasiadas peticiones: {len(calls)}"
    assert results == [f"[ES] {text.upper()}" for text in many]

    # Textos sin letras no se envían
    calls.clear()
    async with GoogleWebTranslator("es", concurrency=2, transport=httpx.MockTransport(_google_handler(calls))) as translator:
        results = await translator.translate_many(["123 — 456", "!!! ??? ...", "Real text here."])
    assert results[0] == "123 — 456" and results[1] == "!!! ??? ..."
    assert results[2] == "[ES] REAL TEXT HERE."
    assert len(calls) == 1
    print("OK · lotes del traductor web:", len(calls), "peticiones en el último caso")


async def test_web_fallback_and_cooldown() -> None:
    """Si Google mezcla las líneas se traduce párrafo a párrafo; los 429 se reintentan."""
    calls: list[str] = []
    transport = httpx.MockTransport(_google_handler(calls, merged=True))
    texts = ["First paragraph.", "Second paragraph.", "Third paragraph."]
    async with GoogleWebTranslator("es", concurrency=2, transport=transport) as translator:
        results = await translator.translate_many(texts)
    assert len(calls) == 4, f"1 lote fallido + 3 individuales, hubo {len(calls)}"
    assert results == ["[ES] FIRST PARAGRAPH.", "[ES] SECOND PARAGRAPH.", "[ES] THIRD PARAGRAPH."], results

    calls.clear()
    transport = httpx.MockTransport(_google_handler(calls, fails=2))
    async with GoogleWebTranslator("es", concurrency=1, transport=transport) as translator:
        translator.retry_delay_scale = 0.0  # no dormir en las pruebas
        result = await translator.translate("Hello world.")
        assert translator._cooldown_until > 0, "el freno global no se activó"
        assert translator._fail_streak == 0, "el contador de fallos no se reinició"
    assert result == "[ES] HELLO WORLD.", result
    assert len(calls) == 3, calls

    # Tras agotar los intentos debe lanzar TranslationError
    calls.clear()
    transport = httpx.MockTransport(_google_handler(calls, fails=99))
    try:
        async with GoogleWebTranslator("es", concurrency=1, transport=transport) as translator:
            translator.retry_delay_scale = 0.0
            await translator.translate("Never translated.")
    except TranslationError:
        pass
    else:
        raise AssertionError("debería fallar tras agotar los reintentos")
    print("OK · mezcla de líneas y freno por bloqueo temporal")


def _multi_host_handler(calls: list[str], blocked: frozenset = frozenset(), fail_first: int = 0):
    """Simula los endpoints de Google: bloquea hosts o los primeros intentos."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= fail_first or request.url.host in blocked:
            return httpx.Response(
                429, text="too many requests", headers={"retry-after": "0.2", "content-type": "text/plain"}
            )
        if request.url.path.endswith("/translate_a/t"):  # formato "legacy"
            text = request.url.params.get("q", "")
            translated = "\n".join(
                f"[ES] {line.upper()}" for line in text.split("\n") if line.strip()
            )
            return httpx.Response(
                200,
                json=[[translated, "en"]],
                headers={"content-type": "application/json"},
            )
        text = parse_qs(request.content.decode("utf-8")).get("q", [""])[0]
        lines = [line for line in text.split("\n") if line.strip()]
        segments = [
            [f"[ES] {line.upper()}" + ("\n" if index < len(lines) - 1 else ""), line, None, None, 10]
            for index, line in enumerate(lines)
        ]
        return httpx.Response(
            200, json=[segments, None, "en"], headers={"content-type": "application/json"}
        )

    return handler


async def test_web_endpoint_failover() -> None:
    """Si un endpoint limita (429), se rota a otro host equivalente."""
    calls: list[str] = []
    transport = httpx.MockTransport(_multi_host_handler(calls, blocked=frozenset({"translate.googleapis.com"})))
    async with GoogleWebTranslator(
        "es", concurrency=2, transport=transport, min_interval=0.0, max_interval=0.0
    ) as translator:
        translator.retry_delay_scale = 0.0
        results = await translator.translate_many(["First paragraph.", "Second paragraph."])
    assert results == ["[ES] FIRST PARAGRAPH.", "[ES] SECOND PARAGRAPH."], results
    assert calls[0].startswith("https://translate.googleapis.com/"), calls
    assert any("clients5.google.com" in url for url in calls), calls
    assert translator._endpoint_index == 1, translator._endpoint_index

    # El formato "legacy" (GET + dict-chrome-ex) también se interpreta bien
    calls.clear()
    transport = httpx.MockTransport(_multi_host_handler(calls))
    async with GoogleWebTranslator(
        "es", concurrency=1, transport=transport, min_interval=0.0, max_interval=0.0
    ) as translator:
        translator._endpoint_index = 3
        results = await translator.translate_many(["Hello my friend", "Good night"])
    assert all("/translate_a/t" in url and "dict-chrome-ex" in url for url in calls), calls
    assert results == ["[ES] HELLO MY FRIEND", "[ES] GOOD NIGHT"], results
    assert translator.detected_lang == "en", translator.detected_lang
    print("OK · rotación de endpoints y formato legacy")


async def test_web_rhythm() -> None:
    """Los 429 bajan el ritmo (hasta el tope) y se respeta Retry-After."""
    calls: list[str] = []
    transport = httpx.MockTransport(_multi_host_handler(calls, fail_first=3))
    started = time.monotonic()
    async with GoogleWebTranslator(
        "es", concurrency=1, transport=transport, min_interval=0.05, max_interval=0.5
    ) as translator:
        translator.retry_delay_scale = 0.0
        result = await translator.translate("Some text here.")
    elapsed = time.monotonic() - started
    assert result == "[ES] SOME TEXT HERE.", result
    assert translator._interval > 0.4, translator._interval  # se frenó hasta el tope
    assert elapsed >= 0.15, elapsed  # esperó lo que pidió Google (Retry-After)
    print(f"OK · ritmo adaptativo (frenó a {translator._interval:.2f} s y respetó Retry-After)")


def _microsoft_handler(calls: list[str]):
    """Simula el traductor gratuito de Microsoft (edge.microsoft.com)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "edge.microsoft.com" in request.url.host, str(request.url)
        assert request.url.params.get("to") == "es", str(request.url)
        body = json.loads(request.content.decode("utf-8"))
        text = body[0]
        calls.append(text)
        lines = [line for line in text.split("\n") if line.strip()]
        translated = "\n".join(f"[ES] {line.upper()}" for line in lines)
        payload = [
            {
                "detectedLanguage": {"language": "en", "score": 1.0},
                "translations": [{"text": translated, "to": "es"}],
            }
        ]
        # El endpoint real sirve el JSON con "text/plain" (comprobado en vivo).
        return httpx.Response(200, json=payload, headers={"content-type": "text/plain; charset=utf-8"})

    return handler


async def test_microsoft_translator() -> None:
    """El traductor gratuito de Microsoft (Edge/Bing) traduce lotes y detecta idioma."""
    calls: list[str] = []
    transport = httpx.MockTransport(_microsoft_handler(calls))
    async with MicrosoftWebTranslator("es", concurrency=2, transport=transport) as translator:
        results = await translator.translate_many(
            ["First paragraph.", "Second paragraph.", "Third paragraph."]
        )
    assert results == ["[ES] FIRST PARAGRAPH.", "[ES] SECOND PARAGRAPH.", "[ES] THIRD PARAGRAPH."], results
    assert len(calls) == 1, f"los tres textos deberían ir en un lote: {calls}"
    assert translator.detected_lang == "en", translator.detected_lang
    print("OK · traductor gratuito de Microsoft (lotes y detección de idioma)")


async def test_auto_failover() -> None:
    """Si Google encadena 429, Microsoft toma el relevo automáticamente."""
    google_calls: list[str] = []

    def google_handler(request: httpx.Request) -> httpx.Response:
        google_calls.append(str(request.url))
        return httpx.Response(429, text="too many requests", headers={"content-type": "text/plain"})

    ms_calls: list[str] = []
    google = GoogleWebTranslator(
        "es", concurrency=1, transport=httpx.MockTransport(google_handler),
        min_interval=0.0, max_interval=0.0,
    )
    google.retry_delay_scale = 0.0
    google.max_attempts = 4
    microsoft = MicrosoftWebTranslator(
        "es", concurrency=1, transport=httpx.MockTransport(_microsoft_handler(ms_calls)),
        min_interval=0.0, max_interval=0.0,
    )
    microsoft.retry_delay_scale = 0.0

    auto = AutoTranslator([google, microsoft])
    async with auto:
        results = await auto.translate_many(["First paragraph.", "Second paragraph."])
        assert results == ["[ES] FIRST PARAGRAPH.", "[ES] SECOND PARAGRAPH."], results
        assert google_calls, "Google debería intentarse primero"
        assert ms_calls, "Microsoft debería haber traducido el lote"
        assert not auto._healthy(0), "Google debería quedar apartado unos minutos"
        assert auto.detected_lang == "en", auto.detected_lang

        # Mientras Google está apartado, el relevo sigue trabajando solo.
        used = len(google_calls)
        results2 = await auto.translate_many(["Third paragraph."])
        assert results2 == ["[ES] THIRD PARAGRAPH."], results2
        assert len(google_calls) == used, "no debería molestar a Google mientras está apartado"

    # Si fallan los dos proveedores, el error explica qué pasó con cada uno
    broken = MicrosoftWebTranslator(
        "es", concurrency=1,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="unavailable", headers={"content-type": "text/plain"})
        ),
        min_interval=0.0, max_interval=0.0,
    )
    broken.retry_delay_scale = 0.0
    broken.max_attempts = 2
    google2 = GoogleWebTranslator(
        "es", concurrency=1, transport=httpx.MockTransport(google_handler),
        min_interval=0.0, max_interval=0.0,
    )
    google2.retry_delay_scale = 0.0
    google2.max_attempts = 2
    try:
        async with AutoTranslator([google2, broken]) as auto2:
            await auto2.translate("Never translated.")
    except TranslationError as err:
        assert "Ningún traductor" in str(err), err
    else:
        raise AssertionError("con los dos proveedores caídos debería lanzar TranslationError")
    print("OK · relevo automático Google → Microsoft (y error claro si fallan ambos)")


async def test_epub_progress_units() -> None:
    """El progreso del EPUB también avanza dentro de un documento."""
    from epub_translate import _translate_batched

    seen: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        seen.append((done, total))

    texts = [f"Paragraph {index} of the chapter with some words." for index in range(100)]
    results = await _translate_batched(MockTranslator(), texts, on_progress)
    assert len(results) == 100, len(results)
    assert results[0] == "[ES] PARAGRAPH 0 OF THE CHAPTER WITH SOME WORDS.", results[0]
    assert seen and seen[-1] == (100, 100), seen
    assert len(seen) >= 3, seen  # lotes de 48 + 48 + 4
    print("OK · progreso por fragmentos dentro del documento:", seen)


async def test_api_translator() -> None:
    """El backend de la API oficial envía varios segmentos por petición."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append({"url": str(request.url), "body": body})
        translations = [
            {"translatedText": f"[ES] {text.upper()} &amp; co", "detectedSourceLanguage": "en"}
            for text in body["q"]
        ]
        return httpx.Response(200, json={"data": {"translations": translations}})

    transport = httpx.MockTransport(handler)
    texts = [f"Segment {index} to translate." for index in range(10)]
    async with GoogleApiTranslator("es", api_key="TEST-KEY", concurrency=2, transport=transport) as translator:
        results = await translator.translate_many(texts)

    assert len(requests) == 1, f"se esperaba 1 petición, hubo {len(requests)}"
    assert "key=TEST-KEY" in requests[0]["url"], requests[0]["url"]
    assert requests[0]["body"]["target"] == "es"
    assert requests[0]["body"]["q"] == texts
    assert results[0] == "[ES] SEGMENT 0 TO TRANSLATE. & co", results[0]
    assert translator.detected_lang == "en"

    # Textos largos se dividen en trozos y se agrupan en la misma petición
    requests.clear()
    async with GoogleApiTranslator(
        "es", api_key="TEST-KEY", concurrency=2, transport=transport, max_chunk_chars=200
    ) as translator:
        result = await translator.translate("Sentence. " * 60)
    assert requests, "no se envió ninguna petición"
    assert all(len(chunk) <= 200 for chunk in requests[0]["body"]["q"]), requests[0]["body"]["q"]
    assert result.count("[ES]") == 3, result[:150]

    # Con un límite de lote pequeño cada trozo viaja por separado
    requests.clear()
    async with GoogleApiTranslator(
        "es", api_key="TEST-KEY", concurrency=2, transport=transport,
        max_chunk_chars=200, max_batch_chars=250,
    ) as translator:
        await translator.translate("Sentence. " * 60)
    assert len(requests) >= 3, requests
    print("OK · backend de la API oficial (con transporte simulado)")


# --------------------------------------------------------------------------- almacén y salud


async def test_job_store() -> None:
    """Metadatos, caché de resultados y limpieza automática."""
    root = WORK / "store_unit"
    store = JobStore(root / "jobs", root / "cache")
    source = WORK / "sample.epub"
    key = store.key_for(file_digest(source), "es", 42)
    assert key == store.key_for(file_digest(source), "es", 42)
    assert key != store.key_for(file_digest(source), "en", 42)

    store.save_meta(key, kind="epub", done=[0, 1], units=7)
    store.write_blob(key, 1, b"texto traducido")
    assert store.read_blob(key, 1) == b"texto traducido"
    assert store.read_blob(key, 9) is None
    assert store.cached_output(key) is None

    output = WORK / "salida.epub"
    output.write_bytes(b"contenido final")
    cached = store.store_output(key, output, "salida.epub")
    store.finish_job(key, cached.name)
    assert not output.exists(), "el resultado debería moverse a la caché"
    assert store.cached_output(key) == cached
    assert not store.blob_path(key, 1).exists(), "los fragmentos deberían liberarse"

    # Caducidad: un resultado viejo se borra al limpiar
    old = time.time() - 10 * 86400
    os.utime(cached, (old, old))
    report = store.prune(max_mb=512, ttl_days=7)
    assert report["removed"] == 1, report
    assert store.cached_output(key) is None
    print("OK · almacén de progreso y caché:", report)


async def test_health_server() -> None:
    """El servidor de salud responde a Render (y solo con lo previsto)."""
    server = start_health_server(0, status_provider=lambda: {"queue": 3})
    port = server.server_address[1]
    try:
        async with httpx.AsyncClient() as client:
            health = await client.get(f"http://127.0.0.1:{port}/health")
            status = await client.get(f"http://127.0.0.1:{port}/status")
            missing = await client.get(f"http://127.0.0.1:{port}/otra-cosa")
        assert health.status_code == 200, health.text
        payload = health.json()
        assert payload["ok"] is True and payload["queue"] == 3, payload
        assert "uptime_seconds" in payload, payload
        assert status.status_code == 200
        assert missing.status_code == 404
    finally:
        server.shutdown()
        server.server_close()
    print("OK · servidor de salud para Render")


# --------------------------------------------------------------------------- utilidades y aplicación


def test_utilidades() -> None:
    assert split_text("hola") == ["hola"]
    trozos = split_text("Frase uno. " * 900)
    assert all(len(trozo) <= 4000 for trozo in trozos), max(len(t) for t in trozos)
    assert len(trozos) > 1

    assert languages.normalize("ES") == "es"
    assert languages.normalize("pt-BR") == "pt"
    assert languages.normalize("chino!") is None
    assert languages.language_name("ja") == "Japonés"

    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("") == "documento"
    assert safe_filename("libro: malo?.pdf").endswith(".pdf")

    assert format_duration(9) == "9 s"
    assert format_duration(65) == "1 min 5 s"
    assert format_duration(3700) == "1 h 1 min"
    print("OK · utilidades")


def test_build_app() -> None:
    """La aplicación de Telegram debe construirse y registrar todos los handlers."""
    import config
    import bot as bot_module

    original = config.TELEGRAM_BOT_TOKEN
    config.TELEGRAM_BOT_TOKEN = "123456:FAKE-TOKEN-FOR-TESTING"
    try:
        app = bot_module.build_app()
    finally:
        config.TELEGRAM_BOT_TOKEN = original
    from telegram.ext import CommandHandler, MessageHandler

    handlers = app.handlers[0]
    commands = {
        command
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {"start", "help", "lang", "dividir", "status", "cancel"} <= commands, commands
    assert any(isinstance(handler, MessageHandler) for handler in handlers)
    assert app.error_handlers, "falta el manejador de errores"
    print("OK · aplicación de Telegram construida con", len(handlers), "handlers")


async def test_worker_integration() -> None:
    """Prueba la cola de trabajo completa con un bot falso (sin red)."""
    import config
    import bot as bot_module
    import pipeline

    class FakeBot:
        def __init__(self) -> None:
            self.edits: list[dict] = []
            self.documents: list[dict] = []
            self.content = b""

        async def edit_message_text(self, **kwargs) -> None:
            self.edits.append(kwargs)

        async def send_chat_action(self, **_kwargs) -> None:
            return None

        async def send_document(self, **kwargs) -> None:
            self.content = kwargs["document"].read()
            self.documents.append(kwargs)

    class FakeApp:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.bot_data: dict = {}

    app = FakeApp()
    app.bot_data[bot_module._QUEUE_KEY] = asyncio.Queue()
    app.bot_data[bot_module._ACTIVE_KEY] = {}
    app.bot_data[bot_module._PENDING_KEY] = {}

    original_factory = pipeline.create_translator
    original_jobs, original_cache = config.JOBS_DIR, config.CACHE_DIR
    store_root = WORK / "store_worker"
    config.JOBS_DIR = store_root / "jobs"
    config.CACHE_DIR = store_root / "cache"
    pipeline._STORE = None
    created: list[MockTranslator] = []

    def factory(lang: str, **_kwargs) -> MockTranslator:
        translator = MockTranslator(lang)
        created.append(translator)
        return translator

    pipeline.create_translator = factory
    worker = asyncio.create_task(bot_module._worker(app))
    try:
        task_id = "integracion01"
        workdir = config.WORK_DIR / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        source = workdir / "texto.txt"
        source.write_text("Hello world. This is a text file.\nSecond line.", "utf-8")
        payload = source.read_bytes()

        def make_task(task_id: str, user_id: int, message_id: int, path: Path) -> DocumentTask:
            return DocumentTask(
                task_id=task_id,
                user_id=user_id,
                chat_id=user_id,
                status_message_id=message_id,
                src_message_id=None,
                src_path=path,
                file_name="texto.txt",
                lang="es",
            )

        task = make_task(task_id, 99, 5, source)
        app.bot_data[bot_module._PENDING_KEY][99] = task
        queue = app.bot_data[bot_module._QUEUE_KEY]
        queue.put_nowait(task)
        await asyncio.wait_for(queue.join(), timeout=60)

        assert app.bot.documents, "no se envió ningún documento"
        sent = app.bot.documents[0]
        assert sent["filename"] == "texto [ES].txt", sent["filename"]
        assert "[ES]" in app.bot.content.decode("utf-8")
        assert "✅" in sent["caption"]
        assert any("Traduciendo" in edit.get("text", "") for edit in app.bot.edits)
        assert not workdir.exists(), "no se limpió el directorio temporal"
        assert not app.bot_data[bot_module._ACTIVE_KEY], "la tarea quedó activa"
        assert not app.bot_data[bot_module._PENDING_KEY], "la tarea quedó pendiente"
        calls_after_first = created[0].calls

        # Reenviar el mismo archivo: se responde desde la caché sin traducir de nuevo
        second_id = "integracion01b"
        workdir2 = config.WORK_DIR / second_id
        workdir2.mkdir(parents=True, exist_ok=True)
        source2 = workdir2 / "texto.txt"
        source2.write_bytes(payload)
        again = make_task(second_id, 99, 6, source2)
        app.bot_data[bot_module._PENDING_KEY][99] = again
        queue.put_nowait(again)
        await asyncio.wait_for(queue.join(), timeout=60)
        assert len(app.bot.documents) == 2, app.bot.documents
        assert "caché" in app.bot.documents[1]["caption"], app.bot.documents[1]["caption"]
        assert created[0].calls == calls_after_first, "no debería volver a traducir"

        # Tarea cancelada antes de empezar
        cancelled = make_task("integracion02", 98, 7, source)
        cancelled.cancel_event.set()
        queue.put_nowait(cancelled)
        await asyncio.wait_for(queue.join(), timeout=30)
        assert any("cancelada" in edit.get("text", "") for edit in app.bot.edits)

        # Modo división: parte el archivo en secciones sin traducir
        split_id = "integracion03"
        workdir3 = config.WORK_DIR / split_id
        workdir3.mkdir(parents=True, exist_ok=True)
        novel_src = workdir3 / "novela.txt"
        make_novel_txt(novel_src, chapters=12)
        split_task = DocumentTask(
            task_id=split_id,
            user_id=97,
            chat_id=97,
            status_message_id=8,
            src_message_id=None,
            src_path=novel_src,
            file_name="novela.txt",
            lang="es",
            mode="split",
            split_spec=SplitSpec("units", 5),
        )
        documents_before = len(app.bot.documents)
        app.bot_data[bot_module._PENDING_KEY][97] = split_task
        queue.put_nowait(split_task)
        await asyncio.wait_for(queue.join(), timeout=60)
        new_documents = app.bot.documents[documents_before:]
        assert len(new_documents) == 3, new_documents
        assert all("Parte" in doc["filename"] for doc in new_documents), new_documents
        assert all("✂️" in doc["caption"] for doc in new_documents), new_documents
        assert any(
            "dividido en <b>3 partes</b>" in edit.get("text", "") for edit in app.bot.edits
        ), "falta el resumen de la división"
        assert not workdir3.exists(), "no se limpió el directorio temporal de la división"
    finally:
        worker.cancel()
        pipeline.create_translator = original_factory
        config.JOBS_DIR, config.CACHE_DIR = original_jobs, original_cache
        pipeline._STORE = None
        shutil.rmtree(config.WORK_DIR / "integracion01", ignore_errors=True)
        shutil.rmtree(config.WORK_DIR / "integracion01b", ignore_errors=True)
        shutil.rmtree(config.WORK_DIR / "integracion03", ignore_errors=True)
    print("OK · cola de trabajo, envío, caché y división (bot falso)")


async def test_real_google() -> None:
    out_pdf = WORK / "real_es.pdf"
    out_epub = WORK / "real_es.epub"
    translator = GoogleWebTranslator("es", concurrency=3)
    async with translator:
        stats = await translate_pdf(WORK / "sample.pdf", out_pdf, translator)
    print("OK · PDF real con Google:", stats, "· idioma detectado:", translator.detected_lang)
    async with GoogleWebTranslator("es", concurrency=3) as translator2:
        stats = await translate_epub(WORK / "sample.epub", out_epub, translator2)
    print("OK · EPUB real con Google:", stats)

    doc = fitz.open(str(out_pdf))
    try:
        text = doc[0].get_text()
        assert "Chapter" not in text, text[:200]
        doc[0].get_pixmap(dpi=110).save(str(WORK / "real_pagina1.png"))
    finally:
        doc.close()
    with zipfile.ZipFile(out_epub) as zin:
        ch1 = zin.read("OEBPS/ch1.xhtml").decode("utf-8")
        assert "Hola" in ch1 or "hola" in ch1, ch1[:300]
    print("PNG de verificación:", WORK / "real_pagina1.png")


async def main() -> None:
    real = "--real" in sys.argv
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    make_sample_pdf(WORK / "sample.pdf")
    make_sample_epub(WORK / "sample.epub")
    try:
        test_utilidades()
        test_build_app()
        await test_health_server()
        await test_job_store()
        await test_web_batches()
        await test_web_fallback_and_cooldown()
        await test_web_endpoint_failover()
        await test_web_rhythm()
        await test_microsoft_translator()
        await test_auto_failover()
        await test_api_translator()
        await test_pdf_mock()
        await test_scanned_pdf()
        await test_pdf_cancel()
        await test_epub_mock()
        await test_epub_progress_units()
        await test_epub_drm()
        await test_epub_resume()
        await test_pdf_resume()
        await test_txt_resume()
        test_split_command()
        test_split_text()
        await test_split_epub()
        await test_split_pdf()
        await test_worker_integration()
        if real:
            await test_real_google()
    finally:
        if not real:
            shutil.rmtree(WORK, ignore_errors=True)
    print("\nTodas las pruebas han pasado ✅")


if __name__ == "__main__":
    asyncio.run(main())
