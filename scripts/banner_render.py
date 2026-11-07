#!/usr/bin/env python3
"""
SSH Banner Designer - shared banner renderer (original code, stdlib only).

The Dropbear pre-auth banner (the "server message" SSH apps show while
connecting) is a plain file (/etc/vps-manager/banner.txt), but its content
may carry ANSI color codes, unicode box drawing and emoji - clients like
HTTP Custom / DarkTunnel / NPV Tunnel render all of that. This module
turns a structured design into that file's content:

    design = {
        "title":  "My Brand",
        "style":  "box" | "bigtitle" | "lines" | "plain",
        "scheme": "gold" | "cyan" | "green" | "magenta" | "red" | "blue" | "white",
        "lines":  [{"text": "...", "color": "red"}, ...],   # max 6
    }

Used by the web panel (web/panel.py imports it, live preview included) and
by install.sh / fix-stack.sh through the CLI at the bottom:

    banner_render.py write-default --config <config.json> --out <banner.txt>
    banner_render.py render --design <design.json> --out <banner.txt>

Python 3.6+ compatible (the suite supports Ubuntu 18.04).
"""
import json
import os
import re
import sys
import unicodedata

ESC = "\x1b"
RESET = ESC + "[0m"
MAX_INNER = 40          # widest framed content line, in visible columns
MAX_LINES = 6
TITLE_MAX = 80
LINE_MAX = 100

# name -> (ANSI SGR parameter string, CSS color for the panel preview)
COLORS = {
    "red":     ("1;91", "#ff6b6b"),
    "green":   ("1;92", "#51e07a"),
    "yellow":  ("1;93", "#ffd94d"),
    "gold":    ("1;33", "#ffb52e"),
    "blue":    ("1;94", "#5aa2ff"),
    "cyan":    ("1;96", "#4dd7e8"),
    "magenta": ("1;95", "#e87ee8"),
    "white":   ("1;97", "#f2f5f7"),
    "orange":  ("38;5;208", "#ff8c1a"),
}

# name -> (frame color name, title color name)
SCHEMES = {
    "gold":    ("gold", "yellow"),
    "cyan":    ("cyan", "cyan"),
    "green":   ("green", "green"),
    "magenta": ("magenta", "magenta"),
    "red":     ("red", "red"),
    "blue":    ("blue", "blue"),
    "white":   ("white", "white"),
}

STYLES = ("box", "bigtitle", "lines", "plain")

# Colors handed out when legacy plain text is migrated into a design.
MIGRATION_COLORS = ("cyan", "red", "green", "yellow", "magenta", "white")

# ---------------------------------------------------------------------------
# Visible width: ANSI-free text measured the way a terminal shows it.
# ---------------------------------------------------------------------------

def char_width(ch):
    """Column width of one character: emoji / wide chars count 2,
    combining marks and variation selectors count 0, the rest 1."""
    if unicodedata.combining(ch):
        return 0
    cp = ord(ch)
    if cp in (0xFE0E, 0xFE0F, 0x200D):      # variation selectors, ZWJ
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if cp >= 0x1F000:                        # pictographs, emoticons, flags...
        return 2
    if 0x2600 <= cp <= 0x27BF:               # misc symbols + dingbats (⚡ ★ ♥)
        return 2
    if 0x2B00 <= cp <= 0x2BFF:               # ⭐ and friends
        return 2
    return 1


def vis_width(text):
    return sum(char_width(c) for c in text)


def truncate_vis(text, maxw):
    """Cut text to at most maxw visible columns (never splits a character)."""
    out = []
    w = 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > maxw:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def pad_vis(text, width):
    """Pad on the right with spaces until the visible width matches."""
    return text + " " * max(0, width - vis_width(text))


_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


def strip_ansi(text):
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Big block-letter font (3 rows). Glyphs are 1-4 columns wide and use only
# the full block character, so they render the same everywhere.
# ---------------------------------------------------------------------------

_FONT = {
    "A": [" █ ", "███", "█ █"], "B": ["██ ", "███", "██ "],
    "C": [" ██", "█  ", " ██"], "D": ["██ ", "█ █", "██ "],
    "E": ["███", "██ ", "███"], "F": ["███", "██ ", "█  "],
    "G": [" ██", "█ █", " ██"], "H": ["█ █", "███", "█ █"],
    "I": ["███", " █ ", "███"], "J": ["  █", "  █", "██ "],
    "K": ["█ █", "██ ", "█ █"], "L": ["█  ", "█  ", "███"],
    "M": ["█  █", "████", "█  █"], "N": ["█  █", "██ █", "█ ██"],
    "O": ["███", "█ █", "███"], "P": ["███", "███", "█  "],
    "Q": ["███", "█ █", " ██"], "R": ["███", "███", "█ █"],
    "S": [" ██", " █ ", "██ "], "T": ["███", " █ ", " █ "],
    "U": ["█ █", "█ █", "███"], "V": ["█ █", "█ █", " █ "],
    "W": ["█  █", "█  █", "████"], "X": ["█ █", " █ ", "█ █"],
    "Y": ["█ █", " █ ", " █ "], "Z": ["███", " █ ", "███"],
    "0": ["███", "█ █", "███"], "1": [" █ ", " █ ", " █ "],
    "2": ["███", " ██", "██ "], "3": ["███", " ██", "███"],
    "4": ["█ █", "███", "  █"], "5": ["███", "██ ", " ██"],
    "6": ["█  ", "███", "███"], "7": ["███", "  █", "  █"],
    "8": ["███", "███", "███"], "9": ["███", "███", "  █"],
    " ": ["  ", "  ", "  "], "!": ["█", "█", " "],
    ".": [" ", " ", "█"], ",": [" ", " ", "█"],
    "-": ["   ", "███", "   "], "_": ["   ", "   ", "███"],
    "+": [" █ ", "███", " █ "], "*": ["█ █", " █ ", "█ █"],
    "/": ["  █", " █ ", "█  "], "\\": ["█  ", " █ ", "  █"],
    ":": ["█", " ", "█"], ";": ["█", " ", "█"],
    "'": ["█", " ", " "], '"': ["█ █", "   ", "   "],
    "?": ["███", " ██", " █ "], "(": [" ██", "█  ", " ██"],
    ")": ["██ ", "  █", "██ "], "#": ["█ █", "███", "█ █"],
    "%": ["██ ", " █ ", " ██"], "=": ["███", "   ", "███"],
}
_FONT_ALIAS = {"⚡": "*", "★": "*", "♥": "*"}


def big_text(text):
    """Render text as 3 rows of block letters. Returns a list of 3 strings,
    or None when any character has no glyph (caller falls back to bold caps)."""
    glyphs = []
    for ch in str(text).upper():
        ch = _FONT_ALIAS.get(ch, ch)
        g = _FONT.get(ch)
        if g is None:
            return None
        glyphs.append(g)
    if not glyphs:
        return None
    return [" ".join(g[r] for g in glyphs) for r in range(3)]


# ---------------------------------------------------------------------------
# Design model
# ---------------------------------------------------------------------------

def _clean_text(value):
    """One safe banner line: no newlines, no control chars (a raw ESC in
    user text would let it inject its own color codes), trimmed."""
    s = str(value or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = "".join(ch for ch in s if ord(ch) >= 32 and ord(ch) != 127)
    return s.strip()


def normalize_design(raw):
    """Coerce anything into a valid design dict (never raises)."""
    if not isinstance(raw, dict):
        raw = {}
    title = _clean_text(raw.get("title", ""))[:TITLE_MAX]
    style = str(raw.get("style", "box") or "box").lower()
    if style not in STYLES:
        style = "box"
    scheme = str(raw.get("scheme", "gold") or "gold").lower()
    if scheme not in SCHEMES:
        scheme = "gold"
    lines = []
    raw_lines = raw.get("lines")
    if isinstance(raw_lines, list):
        for item in raw_lines[:MAX_LINES]:
            if isinstance(item, dict):
                text = _clean_text(item.get("text", ""))[:LINE_MAX]
                color = str(item.get("color", "white") or "white").lower()
            else:
                text = _clean_text(item)[:LINE_MAX]
                color = "white"
            if color not in COLORS:
                color = "white"
            lines.append({"text": text, "color": color})
    return {"title": title, "style": style, "scheme": scheme, "lines": lines}


def design_from_plain(text, style="box"):
    """Migrate legacy plain banner text into a design: first line becomes
    the title, the rest become colored lines."""
    rows = [ln.strip() for ln in str(text or "").replace("\r", "").split("\n")]
    rows = [ln for ln in rows if ln]
    if not rows:
        rows = ["DDK VPS Manager"]
    lines = [{"text": t, "color": MIGRATION_COLORS[i % len(MIGRATION_COLORS)]}
             for i, t in enumerate(rows[1:1 + MAX_LINES])]
    return normalize_design({"title": rows[0], "style": style,
                             "scheme": "gold", "lines": lines})


def default_design(title=None):
    """The styled design fresh installs start with (gold box, like the
    reference panels): bold colored title, one cyan info line, one red
    rule line."""
    t = _clean_text(title or "") or "DDK VPS Manager"
    return normalize_design({
        "title": t,
        "style": "box",
        "scheme": "gold",
        "lines": [
            {"text": "Premium SSH & Xray Tunnel", "color": "cyan"},
            {"text": "🚫 No Torrent / No Abuse", "color": "red"},
        ],
    })


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _wrap(color_name, text):
    return "%s[%sm%s%s" % (ESC, COLORS[color_name][0], text, RESET)


def _content_rows(design):
    """(text, color_name) rows for the framed styles, before framing."""
    frame_color, title_color = SCHEMES[design["scheme"]]
    rows = []
    title = design["title"]
    if design["style"] == "bigtitle" and title:
        big = big_text(title)
        if big and max(vis_width(r) for r in big) <= MAX_INNER:
            rows.extend((r, title_color) for r in big)
        else:
            # Too wide for block letters (or unsupported characters):
            # bold caps title row instead - never a broken frame.
            rows.append(("⚡ " + title.upper() + " ⚡", title_color))
    elif title:
        rows.append(("⚡ " + title + " ⚡", title_color))
    for ln in design["lines"]:
        if ln["text"]:
            rows.append((ln["text"], ln["color"]))
    return rows


def _frame(rows, scheme):
    frame_color = SCHEMES[scheme][0]
    inner = 0
    for text, _c in rows:
        inner = max(inner, vis_width(text))
    inner = min(inner, MAX_INNER)
    top = _wrap(frame_color, "╔" + "═" * (inner + 2) + "╗")
    bottom = _wrap(frame_color, "╚" + "═" * (inner + 2) + "╝")
    edge = _wrap(frame_color, "║")
    out = [top]
    for text, color in rows:
        cell = _wrap(color, pad_vis(truncate_vis(text, inner), inner))
        out.append(edge + " " + cell + " " + edge)
    out.append(bottom)
    return "\n".join(out)


def render(design):
    """Design -> banner file content (str, no trailing newline)."""
    design = normalize_design(design)
    style = design["style"]
    if style == "plain":
        parts = []
        if design["title"]:
            parts.append(design["title"])
        parts.extend(ln["text"] for ln in design["lines"] if ln["text"])
        return "\n".join(parts)
    if style == "lines":
        _fc, title_color = SCHEMES[design["scheme"]]
        parts = []
        if design["title"]:
            parts.append(_wrap(title_color, "⚡ " + design["title"] + " ⚡"))
        for ln in design["lines"]:
            if ln["text"]:
                parts.append(_wrap(ln["color"], truncate_vis(ln["text"], 60)))
        return "\n".join(parts)
    rows = _content_rows(design)
    if not rows:
        return ""
    return _frame(rows, design["scheme"])


# ---------------------------------------------------------------------------
# ANSI -> HTML, so the panel preview shows exactly what clients get.
# ---------------------------------------------------------------------------

_STD_CSS = {30: "#7a8699", 31: "#e5484d", 32: "#46a758", 33: "#ffb52e",
            34: "#3e8ef7", 35: "#c05fc0", 36: "#2ea8b8", 37: "#d7dde5",
            90: "#98a4b3", 91: "#ff6b6b", 92: "#51e07a", 93: "#ffd94d",
            94: "#5aa2ff", 95: "#e87ee8", 96: "#4dd7e8", 97: "#f2f5f7"}
_256_CSS = {208: "#ff8c1a"}


def _esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def to_html(rendered):
    """Convert rendered banner text (with ANSI codes) to an HTML fragment
    of colored spans, safe to drop into a <pre>."""
    out = []
    color = None
    bold = False

    def seg(text):
        if not text:
            return
        e = _esc(text)
        if color or bold:
            style = ""
            if color:
                style += "color:" + color + ";"
            if bold:
                style += "font-weight:bold;"
            out.append("<span style=\"%s\">%s</span>" % (style, e))
        else:
            out.append(e)

    pos = 0
    for m in _ANSI_RE.finditer(rendered):
        seg(rendered[pos:m.start()])
        codes = [c for c in m.group(1).split(";") if c != ""]
        i = 0
        while i < len(codes):
            try:
                n = int(codes[i])
            except ValueError:
                i += 1
                continue
            if n == 0:
                color, bold = None, False
            elif n == 1:
                bold = True
            elif n == 22:
                bold = False
            elif n == 39:
                color = None
            elif n in _STD_CSS:
                color = _STD_CSS[n]
            elif n == 38:
                # 38;5;N 256-color form
                try:
                    if int(codes[i + 1]) == 5:
                        color = _256_CSS.get(int(codes[i + 2]), color)
                        i += 2
                except (ValueError, IndexError):
                    pass
            i += 1
        pos = m.end()
    seg(rendered[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# CLI (install.sh / fix-stack.sh)
# ---------------------------------------------------------------------------

def _arg(argv, name, default=""):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _write_out(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def cmd_write_default(argv):
    """Render the default (or the already-stored) design to the banner file
    and store the design in the panel config, so panel and file agree."""
    cfg_path = _arg(argv, "--config")
    out_path = _arg(argv, "--out")
    if not out_path:
        print("write-default: --out is required", file=sys.stderr)
        return 2
    cfg = {}
    if cfg_path and os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    stored = cfg.get("banner_design")
    if isinstance(stored, dict):
        design = normalize_design(stored)
    else:
        title = cfg.get("banner") or ""
        if str(title).strip() == "Welcome to VPS Manager Suite":
            title = ""  # the pre-rebrand stock default, not a custom title
        design = default_design(title)
    cfg["banner_design"] = design
    if design["title"]:
        cfg["banner"] = design["title"]
    if cfg_path:
        d = os.path.dirname(cfg_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    _write_out(out_path, render(design))
    print("Banner design '%s' (%s/%s) written to %s"
          % (design["title"] or "-", design["style"], design["scheme"], out_path))
    return 0


def cmd_render(argv):
    design_path = _arg(argv, "--design")
    out_path = _arg(argv, "--out")
    if not design_path or not out_path:
        print("render: --design and --out are required", file=sys.stderr)
        return 2
    with open(design_path, encoding="utf-8") as f:
        design = normalize_design(json.load(f))
    _write_out(out_path, render(design))
    print("Rendered banner (%s/%s) to %s" % (design["style"], design["scheme"], out_path))
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "write-default":
        return cmd_write_default(argv[2:])
    if argv[1] == "render":
        return cmd_render(argv[2:])
    print("unknown command: %s" % argv[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
