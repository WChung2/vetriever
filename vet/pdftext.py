"""PDF text with its symbols repaired.

Some journals' PDFs draw symbols with fonts whose character codes say nothing about what they
show: the text layer has "o" where the page shows "<", "9" for "×", "l" for "µ" and "/C6" for
"±". Nothing later can see this, because the quote check compares a value with this same text.

SYMBOLS says what each code of such a font really draws. It comes from the fonts themselves:
the outline of every glyph in the stored PDFs' symbol fonts was drawn and looked at, each
distinct shape once. Before reading, a font gets that map in memory (the ToUnicode map a
well-made PDF carries), so only the symbols change, never the words around them, and the file
stays as it is. It works on fonts and not on the text because "/C3" is a symbol in one font and
plain words in another ("IgG/C3"). A font in SYMBOLS is corrected even if it brings its own map:
some of those maps are wrong too ("five" for a glyph that draws "=").

A code nobody has checked comes out as "�", never as a wrong letter, and `warning` flags the
paper. It also flags what no map can repair: a font that looks like a symbol font but isn't in
SYMBOLS, and text that comes out as control characters.
"""
import io
import logging
import re

from pypdf import PdfReader
from pypdf._cmap import _parse_to_unicode
from pypdf._codecs import adobe_glyphs, charset_encoding
from pypdf.errors import PyPdfError
from pypdf.generic import DecodedStreamObject, NameObject

UNKNOWN = "�"
GREEK = dict(zip("abcdefghijklmnopqrstuvwxABCDEFGHIJKLMNOPQRSTUVWX",
                 "αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ")) | {
    ".": "ϱ", "/": "ϕ", "0": "ϑ", "1": "ς"}
PI = {"H9251": "α", "H9253": "γ", "H9262": "μ", "H9267": "ρ", "H9273": "χ", "H11001": "+",
      "H11002": "−", "H11003": "×", "H11005": "=", "H11006": "±", "H11021": "<", "H11022": ">",
      "H11034": "°", "H11503": "×", "H17015": "©", "H23041": "®"}
# Math Pi fonts name their glyphs after the digit they replace
MATH_PI = {"zero": "″", "one": "+", "two": "−", "three": "×", "four": "÷", "five": "=", "six": "±",
           "seven": "∓", "eight": "°", "nine": "′", "comma": "<", "period": ">", "semicolon": "~",
           "hyphen": "‴"}
# Adobe's Symbol font has no Latin letters, so its "m" can only be μ. pypdf misses it in a subset
# ("ABCDEF+Symbol"), and a mistake here turns µg into mg.
ADOBE_SYMBOL = {chr(code): symbol for code, symbol in enumerate(charset_encoding["/Symbol"])
                if code > 32 and symbol.strip("\x00")}
# font name (a pattern) → {what the font says: what the page shows}. One character is a code: a
# code with no glyph name of its own in such a font is a stand-in, and one not listed comes out
# as UNKNOWN. A longer key is a glyph name. An empty table: the font was checked and reads right.
SYMBOLS = {
    "AdvEls-ent2": {"w": "†"},
    "AdvEls-ent3": {"r": "©", "s": "®"},
    "AdvEls-ent4": {"o": "<"},
    "AdvEls-ent5": {"1": "°", "Z": "≥"},
    "AdvGreek_B": {"a": "α", "l": "μ"},
    "AdvMacms": {"C3": "*"},
    "AdvP0004": {"B": "“"},
    "AdvP0005": {"&": "•", "^": "”"},
    "AdvP3D3256": {"3": "×", "6": "±"},
    "AdvP44E6F4": {"u": "°"},
    "AdvMPi-One": MATH_PI,
    "AdvP4C4E51": {":": ".", "<": "<", ">": ">"},
    "AdvP4C4E59": {"C18": "`", "C19": "´", "Euro": "¨", "asciicircum": "ˆ", "asciitilde": "˜",
                   "equal": "=", "=": "=", "+": "+"},
    "AdvP4C4E5E": {"C19": "´"},
    "AdvP4C4E74": {f"C{n}": symbol for n, symbol in enumerate("−·×*÷◇±∓⊕⊖⊗⊘⊙©°•≍≡⊆⊇≤≥⪯⪰~≈⊂⊃≪≫≺≻")} | {
        "Thorn": ")", "eth": "(", "h": "⟨", "i": "⟩", "onequarter": "=", "thorn": "+", "z": "‡",
        "zero": "′"},
    "AdvP1491|AdvP1854|AdvP497E[2-5]|AdvPSA35[B-F]": {},     # ordinary letters
    "AdvP697C": {"m": "μ"},
    "AdvP7CAC": {"E": "©"},
    "AdvP7DA6": MATH_PI,
    "AdvPS586B": MATH_PI | {"a": "α", "b": "β", "g": "γ", "m": "μ"},
    "AdvPED0017": {},                                        # ornaments only
    "AdvPS7CFD": {"y": "†", "z": "‡"},
    "AdvPS7DA6": {"dollar": "≥", "numbersign": "≤"},
    "AdvPS7DED": MATH_PI,
    "AdvPSMP1": {"C212": "‘", "C213": "’"},
    "AdvPSMP1[0-3]": GREEK,
    "AdvPSMPi6": {"j": "■"},
    "AdvPSMSAM10": {"6": "≤"},
    "AdvPSSym": {"C176": "°", "C181": "∝", "C210": "®", "C211": "©", "C212": "™", "C213": "∏",
                 "C214": "√", "C215": "·", "C216": "¬", "C226": "®", "C228": "™", "C229": "∑"},
    "AdvPSZap": {"H": "★"},
    "AdvPi1": {"4": ">", "a": "α", "b": "β", "f": "φ", "g": "γ", "m": "μ", "w": "χ"},
    "AdvPi3": {"#": "©", "&": "■"},
    "AdvSPSASORT": {"C223": "¸"},
    "AdvTir_symb": {"9": "×"},
    "CMSY[0-9]+": {"circlecopyrt": "©"},
    "LucidaSansUnicode22": {"2264": "≤", "2265": "≥"},
    "MathPackTen": {"p10": "φ"},
    "MathematicalPi-(One|Four)": PI,
    "Symbol": ADOBE_SYMBOL | {"g42": "≥", "g57": "◊"},
    "SymbolBS": {"bullet": "∞", "c": "χ", "equal": "=", "m": "μ", "plus": "+", "yen": "×"},
    "SymbolBold": {"Adieresis": "≤"},
    "Universal-GreekwithMathPi": PI | {",": "<", "1": "+", "2": "−", "5": "=", "8": "°", "m": "μ"},
    "Universal-NewswithCommPi": PI | {"q": "©", "t": "®"},
    "WPGreekCentury": {"c34": "α"},
    "01Bunmei-jyuui": {"h": "—"},
}
# private-use characters that fonts' own maps point to: Minion's digits (1 is apart from the rest)
# and Word's Symbol font. The rest of the private-use area is left alone (icon fonts).
PRIVATE = str.maketrans({0xF639: "0", 0xF6DC: "1", 0xF63A: "2", 0xF63B: "3", 0xF63C: "4",
                         0xF63D: "5", 0xF63E: "6", 0xF63F: "7", 0xF640: "8", 0xF641: "9",
                         0xF03E: ">", 0xF06D: "μ", 0xF0A3: "≤", 0xF0B0: "°", 0xF0B3: "≥",
                         0xF0B4: "×"})
KNOWN = [(re.compile(rf"(?:{name})(?![0-9A-Za-z])"), table) for name, table in SYMBOLS.items()]
# names that suggest a symbol font: one missing from SYMBOLS is flagged, not trusted
SYMBOL_FONT = re.compile(r"AdvEls-ent|AdvGreek|AdvMPi|AdvMacms|AdvP[0-9A-F]{4,6}(?![0-9A-Za-z])|"
                         r"AdvPS[0-9A-F]{4}(?![0-9A-Za-z])|AdvPSMP|AdvPSMSAM|AdvPSSym|AdvPSZap|"
                         r"AdvPi\d|AdvTir_symb|CMSY|MathPack|MathematicalPi|Symbol|Universal-")
CODE_NAME = re.compile(r"[A-Za-z]{0,3}\d+")       # a glyph called by number, not by what it is
CONTROL = re.compile(r"[\x01-\x08\x0e-\x1b]")     # a font with no way to its letters

logging.getLogger("pypdf").setLevel(logging.ERROR)     # font warnings on older PDFs


def read(data: bytes) -> tuple[str, int]:
    """The text of a PDF, with its symbols repaired, and its number of pages."""
    pdf = PdfReader(io.BytesIO(data))
    for font in _fonts(pdf):
        fixes = _fixes(font)
        if fixes:
            font[NameObject("/ToUnicode")] = _map(font, fixes)
    text = "\n\n".join(page.extract_text() or "" for page in pdf.pages)
    return text.translate(PRIVATE), len(pdf.pages)


def warning(data: bytes, text: str) -> str:
    """Why symbols may be wrong or missing in this text; empty when they can be trusted."""
    fonts = set()
    try:
        for font in _fonts(PdfReader(io.BytesIO(data))):
            name = _name(font)
            if SYMBOL_FONT.search(name) and _table(name) is None:
                fonts.add(name)
    except (PyPdfError, AttributeError, TypeError):
        fonts.add("(fonts unreadable)")
    unread = text.count(UNKNOWN) + len(CONTROL.findall(text))
    why = ([f"{unread} character{'s' if unread > 1 else ''} could not be read"] if unread else []
           ) + ([f"unchecked fonts {', '.join(sorted(fonts))}"] if fonts else [])
    return "symbols such as <, >, = may be wrong in the text: " + "; ".join(why) if why else ""


def _object(x):
    return x.get_object() if hasattr(x, "get_object") else x


def _fonts(pdf: PdfReader) -> list:
    """Every font the pages can use, also inside form objects (a table or figure can be one)."""
    found = {}

    def walk(resources, depth=0):
        resources = _object(resources) or {}
        for ref in (_object(resources.get("/Font")) or {}).values():
            font = _object(ref)
            found[id(font)] = font
        for ref in (_object(resources.get("/XObject")) or {}).values():
            form = _object(ref)
            if form.get("/Subtype") == "/Form" and depth < 4:
                walk(form.get("/Resources"), depth + 1)

    for page in pdf.pages:
        walk(page.get("/Resources"))
    return list(found.values())


def _name(font) -> str:
    return re.sub(r"^/?(?:[A-Z]{6}\+)?", "", str(font.get("/BaseFont", "")))


def _table(name: str) -> dict | None:
    return next((table for pattern, table in KNOWN if pattern.search(name)), None)


def _differences(font) -> dict[int, str]:
    """code → glyph name, from the font's own encoding."""
    encoding, out, code = _object(font.get("/Encoding")), {}, 0
    if isinstance(encoding, dict):
        for item in _object(encoding.get("/Differences")) or []:
            item = _object(item)
            if isinstance(item, int):
                code = item
            else:
                out[code] = str(item).removeprefix("/")
                code += 1
    return out


def _fixes(font) -> dict[int, str]:
    """The codes of this font that pypdf would read wrong, and what they really draw."""
    if font.get("/Subtype") == "/Type0":    # two-byte codes: no glyph names to go by
        return {}
    table, names = _table(_name(font)) or {}, _differences(font)
    stand_ins = any(len(key) == 1 for key in table)
    mapped = set(_parse_to_unicode(font)[1]) if "/ToUnicode" in font else set()
    fixes = {}
    for code in range(256):
        glyph = names.get(code, "")
        if code == 32:                      # the space stays what it is in every font
            continue
        if glyph in table:
            fixes[code] = table[glyph]
        elif stand_ins and not glyph and code not in mapped:
            fixes[code] = table.get(chr(code), UNKNOWN)
        elif code not in mapped and CODE_NAME.fullmatch(glyph) and f"/{glyph}" not in adobe_glyphs:
            fixes[code] = UNKNOWN
    return fixes


def _map(font, fixes: dict[int, str]) -> DecodedStreamObject:
    """The font's ToUnicode map, if it has one, with the fixes after it so they win; in memory."""
    body = "".join(f"<{code:02X}> <{ord(symbol):04X}>\n" for code, symbol in fixes.items())
    own = _object(font["/ToUnicode"]).get_data() if "/ToUnicode" in font else b""
    stream = DecodedStreamObject()
    stream.set_data(own + f"\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
                          f"{len(fixes)} beginbfchar\n{body}endbfchar\n".encode())
    return stream
