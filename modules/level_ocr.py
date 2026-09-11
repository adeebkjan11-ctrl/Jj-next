# This file is part of LazyFarmers.
# Copyright (c) 2025-Present Routo
#
# LazyFarmers is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# You should have received a copy of the GNU General Public License
# along with LazyFarmers. If not, see <https://www.gnu.org/licenses/>.

"""Read the level/XP/rank that OwO renders as a *picture* on `owo level`.

OwO no longer prints ``you are level N`` in text - it replies with a components-v2
card whose only content is a rendered 800x240 image (and whose ``attachments`` list
is empty, so the url has to come out of the media_gallery component).

This module downloads nothing itself; it takes the raw image bytes, runs Tesseract
over a short ordered list of pre-processing passes and pulls out:

* ``level``      - the big stylised ``LVL N`` number
* ``xp``         - ``XP:3,579/5,196`` -> (current, needed)
* ``rank``       - ``Rank: #32,485,249``
* ``text``       - everything OCR read, lowercased, so the caller can confirm the
                   card actually carries *our* username before trusting the numbers

Any field that cannot be read is returned as ``None`` rather than guessed.
"""

import io
import os
import re
import shutil

try:
    from PIL import Image, ImageOps, ImageEnhance
    _PIL = True
except Exception:  # pragma: no cover - pillow is in requirements but be safe
    _PIL = False

try:
    import pytesseract
    _TESS = True
except Exception:  # pragma: no cover
    _TESS = False


# ── regexes ─────────────────────────────────────────────────────────────────
# Tesseract garbles the stylised "LVL" label ("vL0", "LVL 9}", "tvLO"), so we
# tolerate spaces / junk between the letters and the number.
LEVEL_RE = re.compile(r'l\s*v\s*l\D{0,8}(\d{1,4})', re.IGNORECASE)
# the thousands separator is read as "," or "." or a space depending on the pass, so
# accept all three and strip them in _best_int rather than losing the field
XP_RE = re.compile(r'(?:xp|exp)\D{0,4}([\d.,\s]{1,15}?)\s*/\s*([\d.,\s]{1,15}?)(?=\s|$|[^\d.,\s])',
                   re.IGNORECASE)
RANK_RE = re.compile(r'rank\D{1,5}#?\s*([\d,.\s]{4,20})', re.IGNORECASE)


# ── where the tesseract binary actually lives ───────────────────────────────
# The Windows installer does *not* put tesseract on PATH, so pytesseract's bare
# `tesseract` call fails on a machine where the engine is perfectly well installed.
# That looked identical to "OCR is not installed" with nothing anywhere to point at
# the real cause, so look in the locations the official installers really use.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\shims\tesseract.exe"),
    r"C:\ProgramData\chocolatey\bin\tesseract.exe",
)
_UNIX_CANDIDATES = (
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
    "/data/data/com.termux/files/usr/bin/tesseract",
)


def _find_binary():
    """Absolute path to a tesseract binary, or None. An explicit env var wins."""
    override = os.environ.get("LAZYFARMERS_TESSERACT") or os.environ.get("TESSERACT_CMD")
    if override and os.path.isfile(override):
        return override
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    for candidate in (_WINDOWS_CANDIDATES if os.name == "nt" else _UNIX_CANDIDATES):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


# pytesseract imports fine without the Tesseract *binary* ever being installed - it
# only shells out to it on the first image. That made an import-only check return True
# on a machine with no OCR engine at all, so every call ground through the whole
# preprocessing loop, swallowed a TesseractNotFoundError on each pass, and returned
# "unreadable" with nothing anywhere saying the engine was simply missing.
_BINARY_STATE = None
_BINARY_PATH = None


def _binary_ok():
    global _BINARY_STATE, _BINARY_PATH
    if _BINARY_STATE is None:
        _BINARY_STATE = False
        if _TESS:
            found = _find_binary()
            if found:
                # point pytesseract at it explicitly; this is what makes a stock
                # Windows install work without the user editing PATH by hand
                pytesseract.pytesseract.tesseract_cmd = found
            try:
                pytesseract.get_tesseract_version()
                _BINARY_STATE = True
                _BINARY_PATH = found or "tesseract"
            except Exception:
                _BINARY_STATE = False
                _BINARY_PATH = None
    return _BINARY_STATE


def _available():
    return _PIL and _TESS and _binary_ok()


def ocr_status():
    """Why OCR is unavailable, or None when it works. For an honest log line."""
    if not _PIL:
        return "Pillow is not installed"
    if not _TESS:
        return "the pytesseract package is not installed"
    if not _binary_ok():
        where = "C:\\Program Files\\Tesseract-OCR" if os.name == "nt" else "/usr/bin"
        return ("the Tesseract OCR engine is not installed or could not be found "
                "(pytesseract is only a wrapper). Install Tesseract itself - the usual "
                f"location is {where} - or set LAZYFARMERS_TESSERACT to the full path "
                "of tesseract" + (".exe" if os.name == "nt" else ""))
    return None


def ocr_engine_path():
    """Path of the tesseract binary in use, or None. Only meaningful after a check."""
    _binary_ok()
    return _BINARY_PATH


# ── pre-processing passes ───────────────────────────────────────────────────
# (scale, contrast, invert, threshold, psm) - best known first.
#
# The order is measured, not guessed. On a real 800x240 owo level card the first entry
# reads the XP pair and the rank in ~0.4 s, and the second is the cheapest combination
# that also resolves the stylised "LVL n" glyph (which no unscaled pass can read at
# all). parse_level_card stops the moment it has all three fields, so the normal cost
# is two passes (~1.5 s) rather than grinding the whole list (~56 s) every time.
_PASSES = (
    (1, 1.0, False, None, 6),
    (3, 2.0, False, 160, 11),
    (1, 1.0, False, None, 11),
    (2, 1.0, False, None, 6),
    (3, 2.0, False, 140, 11),
    (3, 2.0, False, 180, 11),
    (3, 1.0, False, None, 6),
    (3, 3.0, False, 160, 6),
    (4, 2.0, False, 160, 11),
    (3, 2.0, True, 160, 11),
    (4, 3.0, True, 180, 6),
)


def _prep(img, scale, contrast, invert, thr):
    """grayscale -> autocontrast -> contrast boost -> optional invert/threshold."""
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g)
    if contrast != 1.0:
        g = ImageEnhance.Contrast(g).enhance(contrast)
    if invert:
        g = ImageOps.invert(g)
    if thr is not None:
        g = g.point(lambda x, t=thr: 255 if x > t else 0, 'L')
    return g


def _best_int(raw, lo=None, hi=None):
    if raw is None:
        return None
    # OCR reports the thousands separator as "," or "." or a space depending on the
    # pass, and owo never prints a fractional xp value, so all three are just noise
    cleaned = re.sub(r'[,.\s_]', '', str(raw))
    if not cleaned.isdigit():
        return None
    val = int(cleaned)
    if lo is not None and val < lo:
        return None
    if hi is not None and val > hi:
        return None
    return val


def parse_level_card(image_bytes):
    """Read an OwO level card.

    Returns ``{'level', 'xp', 'xp_needed', 'rank', 'text'}``. Every numeric field is
    ``None`` when it could not be read - nothing is ever guessed. ``text`` is the
    accumulated OCR output, lowercased, so the caller can confirm the card carries our
    own username before believing the numbers (owo's card renders the name, and in a
    shared channel two accounts can both have a level request in flight).
    """
    blank = {'level': None, 'xp': None, 'xp_needed': None, 'rank': None, 'text': ''}
    if not _available():
        return blank
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception:
        return blank

    level = xp = xp_needed = rank = None
    seen = set()
    chunks = []

    for scale, contrast, invert, thr, psm in _PASSES:
        try:
            prepped = _prep(img, scale, contrast, invert, thr)
            text = pytesseract.image_to_string(prepped, config=f'--psm {psm}')
        except Exception:
            continue
        if not text or text in seen:
            continue
        seen.add(text)
        chunks.append(text)

        if level is None:
            for match in LEVEL_RE.finditer(text):
                candidate = _best_int(match.group(1), lo=0, hi=9999)
                if candidate is not None:
                    level = candidate
                    break

        if xp is None:
            match = XP_RE.search(text)
            if match:
                current = _best_int(match.group(1), lo=0)
                needed = _best_int(match.group(2), lo=1)
                # a mangled pair reads as e.g. 579/5; refuse it instead of showing a
                # 11580% xp bar on the dashboard
                if current is not None and needed is not None and current <= needed * 50:
                    xp, xp_needed = current, needed

        if rank is None:
            match = RANK_RE.search(text)
            if match:
                rank = _best_int(match.group(1), lo=1)

        # everything worth having - stop rather than running the remaining passes
        if level is not None and xp is not None and rank is not None:
            break

    return {
        'level': level,
        'xp': xp,
        'xp_needed': xp_needed,
        'rank': rank,
        'text': "\n".join(chunks).lower(),
    }
