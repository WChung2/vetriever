"""PDF symbols: real PDFs built here, read back through pypdf. No publisher's file is used."""
import io
import logging
import re

import pytest
from pypdf import PdfReader

from vet import fetch as f
from vet import pdftext as p

logging.getLogger("pypdf").setLevel(logging.CRITICAL)


def make(fonts: dict[int, str], content: str, more: dict[int, bytes] | None = None,
         xobjects: str = "") -> bytes:
    """A one-page PDF. fonts: object number → the font's entries, used as /F<number> on the page."""
    stream = content.encode("latin-1")
    used = " ".join(f"/F{n} {n} 0 R" for n in fonts)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                f"/Resources << /Font << {used} >> {xobjects} >> >>").encode(),
            4: b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"}
    objs |= {n: f"<< /Type /Font /Subtype /Type1 {entries} >>".encode()
             for n, entries in fonts.items()} | (more or {})
    out, offsets = b"%PDF-1.4\n", {}
    for k in sorted(objs):
        offsets[k] = len(out)
        out += b"%d 0 obj\n" % k + objs[k] + b"\nendobj\n"
    start, size = len(out), max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    out += b"".join(b"%010d 00000 n \n" % offsets[k] if k in offsets else b"0000000000 00000 f \n"
                    for k in range(1, size))
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, start)


def stream(data: bytes) -> bytes:
    return b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def font_map(*pairs: tuple[int, int]) -> bytes:
    body = "".join(f"<{a:02X}> <{b:04X}>\n" for a, b in pairs)
    return stream((f"/CIDInit /ProcSet findresource begin\nbegincmap\n1 begincodespacerange\n"
                   f"<00> <FF>\nendcodespacerange\n{len(pairs)} beginbfchar\n{body}endbfchar\n"
                   f"endcmap\n").encode())


PLAIN = "/BaseFont /Helvetica /Encoding /WinAnsiEncoding"


def symbol_font(name: str, encoding: str = "/Encoding /WinAnsiEncoding") -> str:
    return f"/BaseFont /ABCDEF+{name} {encoding}"


def beside_words(code: str, font: int = 11) -> str:
    """A symbol between words, in another font, as on a real page."""
    return (f"BT /F10 9 Tf 40 760 Td (before) Tj /F{font} 9 Tf ( {code} ) Tj "
            f"/F10 9 Tf (after) Tj ET")


def read(fonts: dict[int, str], content: str, **kw) -> str:
    return p.read(make({10: PLAIN} | fonts, content, **kw))[0]


@pytest.mark.parametrize("font, code, shown", [
    ("AdvPi1", "4", ">"), ("AdvPi1", "m", "μ"), ("AdvEls-ent4", "o", "<"),
    ("AdvEls-ent5", "Z", "≥"), ("AdvTir_symb", "9", "×"), ("AdvPSMP13", "l", "μ"),
    ("AdvPSMP10", "a", "α"), ("AdvP4C4E51", ":", "."), ("AdvP4C4E59", "+", "+")])
def test_a_stand_in_letter_becomes_what_the_page_shows(font, code, shown):
    assert f"before {shown} after" in read({11: symbol_font(font)}, beside_words(code))


@pytest.mark.parametrize("code, glyph, shown", [
    (7, "C6", "±"), (3, "C2", "×"), (21, "C20", "≤"), (22, "C21", "≥"), (1, "C0", "−"),
    (254, "thorn", "+"), (188, "onequarter", "="), (240, "eth", "(")])
def test_a_glyph_name_becomes_what_the_page_shows(code, glyph, shown):
    fonts = {11: symbol_font("AdvP4C4E74", f"/Encoding << /Differences [{code} /{glyph}] >>")}
    assert f"before {shown} after" in read(fonts, beside_words(f"\\{code:03o}"))


def test_a_math_pi_font_is_corrected_even_when_its_own_map_is_wrong():
    fonts = {11: symbol_font("AdvMPi-One", "/Encoding << /Differences [53 /five] >> "
                                           "/ToUnicode 20 0 R")}
    text = read(fonts, beside_words("5"), more={20: font_map((53, 0x35))})
    assert "before = after" in text and "5" not in text


def test_adobe_symbol_has_no_latin_letters_so_its_m_is_mu():
    """The error that matters most: µg read as mg is a dose a thousand times too high."""
    assert "before μ after" in read({11: symbol_font("Symbol", "")}, beside_words("m"))


def test_a_map_the_font_brings_is_kept_for_codes_it_covers():
    fonts = {11: symbol_font("Symbol", "/Encoding << /Differences [2 /alpha] >> /ToUnicode 20 0 R")}
    text = read(fonts, beside_words("!"), more={20: font_map((33, 0x2265))})
    assert "before ≥ after" in text


def test_an_ordinary_font_keeps_its_letters_and_words_that_look_like_codes():
    words = "P o .05 and 4 and 9 and 5 mg IgG/C3 /C6 /H11021"
    content = f"BT /F10 9 Tf 40 760 Td ({words}) Tj ET"
    assert read({}, content).strip() == words


def test_only_the_symbols_change_nothing_else_in_the_text():
    data = make({10: PLAIN, 11: symbol_font("AdvPi1")},
                "BT /F10 9 Tf 11 TL 40 760 Td (first line of words) Tj T* (P ) Tj /F11 9 Tf (4) Tj "
                "/F10 9 Tf ( .05 at 10 ) Tj /F11 9 Tf (m) Tj /F10 9 Tf (g/kg) Tj T* (last) Tj ET")
    before = PdfReader(io.BytesIO(data)).pages[0].extract_text()
    after = p.read(data)[0]
    assert len(before) == len(after) and before != after
    assert [(a, b) for a, b in zip(before, after) if a != b] == [("4", ">"), ("m", "μ")]


def test_a_code_nobody_checked_is_never_a_wrong_letter_and_the_paper_is_flagged():
    data = make({10: PLAIN, 11: symbol_font("AdvPi1")}, beside_words("q"))
    text = p.read(data)[0]
    assert f"before {p.UNKNOWN} after" in text and " q " not in text
    assert "1 character could not be read" in p.warning(data, text)


def test_a_glyph_called_by_number_in_any_font_is_unknown_not_a_code_in_the_text():
    data = make({10: PLAIN, 11: symbol_font("SomeFont", "/Encoding << /Differences [1 /C99] >>")},
                beside_words("\\001"))
    text = p.read(data)[0]
    assert "/C99" not in text and p.UNKNOWN in text and p.warning(data, text)


def test_a_symbol_font_nobody_checked_is_flagged_by_name():
    data = make({10: PLAIN, 11: symbol_font("AdvPi7")}, beside_words("4"))
    assert "AdvPi7" in p.warning(data, p.read(data)[0])


def test_a_repaired_paper_is_not_flagged():
    data = make({10: PLAIN, 11: symbol_font("AdvPi1")}, beside_words("4"))
    assert p.warning(data, p.read(data)[0]) == ""


def test_text_that_comes_out_as_control_characters_is_flagged():
    data = make({10: PLAIN}, "BT /F10 9 Tf 40 760 Td (words) Tj ET")
    assert "3 characters could not be read" in p.warning(data, "words \x01\x02\x03")


def test_a_font_inside_a_form_object_is_repaired_too():
    drawing = b"BT /F11 9 Tf 40 700 Td (4) Tj ET"
    form = (b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << "
            b"/F11 11 0 R >> >> /Length %d >>\nstream\n" % len(drawing)) + drawing + b"\nendstream"
    font = f"<< /Type /Font /Subtype /Type1 {symbol_font('AdvPi1')} >>".encode()
    data = make({10: PLAIN}, "BT /F10 9 Tf 40 760 Td (words) Tj ET /Fm1 Do",
                more={11: font, 12: form}, xobjects="/XObject << /Fm1 12 0 R >>")
    assert ">" in p.read(data)[0]


def test_private_use_characters_become_digits_and_word_symbols():
    fonts = {11: "/BaseFont /ABCDEF+MinionPro-Bold /Encoding /WinAnsiEncoding /ToUnicode 20 0 R"}
    content = "BT /F11 9 Tf 40 760 Td (ABC) Tj ET"
    text = read(fonts, content, more={20: font_map((65, 0xF6DC), (66, 0xF63A), (67, 0xF06D))})
    assert "12μ" in text


def test_every_entry_of_the_table_is_one_symbol_and_none_is_the_unknown_mark():
    assert len(p.GREEK) == 52
    for _, table in p.KNOWN:
        for key, symbol in table.items():
            assert len(symbol) == 1 and symbol != p.UNKNOWN and key


def test_a_font_name_finds_its_own_table_and_no_other():
    assert p._table("AdvPSMP13") is p.GREEK and p._table("AdvPSMP1") is not p.GREEK
    assert p._table("GlqwvwAdvPi3") == {"#": "©", "&": "■"}       # a maker's scrambled prefix
    assert p._table("AdvPi7") is None and p._table("Helvetica") is None
    assert p._table("SymbolBS") != p._table("Symbol")


# ---- reading the stored PDFs again ----------------------------------------------------

def stored(folder, source_id="DOI-1", old="P 4 .05\n", font="AdvPi1", code="4"):
    (folder / "docs").mkdir(exist_ok=True)
    data = make({10: PLAIN, 11: symbol_font(font)},
                f"BT /F10 9 Tf 40 760 Td (P ) Tj /F11 9 Tf ({code}) Tj /F10 9 Tf ( .05) Tj ET")
    (folder / "docs" / f"{source_id}.pdf").write_bytes(data)
    (folder / "docs" / f"{source_id}.txt").write_text(old, "utf-8")
    return f.Paper(source_id=source_id, route="wiley", file_name=f"docs/{source_id}.pdf",
                   text_file=f"docs/{source_id}.txt", text_warning="old warning", words="1")


def put(folder, *papers):
    con = f._connect(folder / "vetriever.db", folder)
    with con:
        f._insert(con, papers)
    con.close()


def rows(folder):
    con = f._connect(folder / "vetriever.db", folder)
    try:
        return {r.source_id: r for r in f._select(con)}
    finally:
        con.close()


def test_reread_repairs_the_text_keeps_the_old_one_and_updates_the_warning(tmp_path):
    put(tmp_path, stored(tmp_path))
    done = f.reread(tmp_path, say=lambda _: None)
    assert done == {"read": 1, "changed": 1, "flagged": 0, "skipped": 0}
    assert re.search(r"P > \.05", (tmp_path / "docs" / "DOI-1.txt").read_text("utf-8"))
    assert (tmp_path / "text_before" / "DOI-1.txt").read_text("utf-8") == "P 4 .05\n"
    assert rows(tmp_path)["DOI-1"].text_warning == ""
    assert "text_warning" in (tmp_path / "docs" / "index.csv").read_text("utf-8")


def test_a_second_reread_changes_nothing_and_never_replaces_the_first_copy(tmp_path):
    put(tmp_path, stored(tmp_path))
    f.reread(tmp_path, say=lambda _: None)
    assert f.reread(tmp_path, say=lambda _: None)["changed"] == 0
    (tmp_path / "docs" / "DOI-1.txt").write_text("a later reader's text\n", "utf-8")
    assert f.reread(tmp_path, say=lambda _: None)["changed"] == 1
    assert (tmp_path / "text_before" / "DOI-1.txt").read_text("utf-8") == "P 4 .05\n"


def test_reread_flags_what_it_cannot_read(tmp_path):
    put(tmp_path, stored(tmp_path, code="q"))
    assert f.reread(tmp_path, say=lambda _: None)["flagged"] == 1
    assert "could not be read" in rows(tmp_path)["DOI-1"].text_warning


def test_reread_skips_a_missing_file_says_so_and_keeps_its_text(tmp_path):
    keep = stored(tmp_path, "DOI-2")
    (tmp_path / "docs" / "DOI-2.pdf").unlink()
    (tmp_path / "docs" / "DOI-3.txt").write_text("from XML\n", "utf-8")
    xml = f.Paper(source_id="DOI-3", route="pmc", file_name="docs/DOI-3.xml",
                  text_file="docs/DOI-3.txt")       # its XML file is gone too
    put(tmp_path, keep, xml)
    said = []
    done = f.reread(tmp_path, say=said.append)
    assert done["skipped"] == 2 and any("DOI-2" in x for x in said) and any("DOI-3" in x for x in said)
    assert (tmp_path / "docs" / "DOI-2.txt").read_text("utf-8") == "P 4 .05\n"
    assert (tmp_path / "docs" / "DOI-3.txt").read_text("utf-8") == "from XML\n"
    assert rows(tmp_path)["DOI-2"].text_warning == "old warning"
