#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2md.py - Convert PDFs (research papers and general documents) to Markdown.

Features
  * Reading order that handles single- and two-column layouts
  * Headings (from the PDF outline, font sizes, or bold lone lines)
  * Bold / italic / superscript, bullet + numbered lists, code blocks
  * Tables -> Markdown tables (ruled tables and "booktabs"-style tables)
  * Figures: raster images AND vector charts are rendered to PNG/JPG files and
    linked with ![caption](assets/...). Text inside vector charts (axis labels
    etc.) is removed from the prose so it doesn't pollute the output.
  * Running headers/footers and page numbers are stripped
  * Paragraphs broken by columns/pages are re-joined; end-of-line hyphenation fixed
  * Scanned pages: OCR via Tesseract (optional), else the page is saved as an image

Install
  pip install pymupdf
  (optional OCR: install Tesseract, e.g. `apt install tesseract-ocr`)

Usage
  python pdf2md.py paper.pdf -o out/
  python pdf2md.py papers_dir/ -o out/ --dpi 200 --page-markers
  python pdf2md.py scan.pdf -o out/ --ocr force --ocr-lang auto
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from typing import Callable, Optional
from urllib.parse import quote

try:
    import pymupdf
except ImportError:  # older PyMuPDF
    try:
        import fitz as pymupdf
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")

Rect = pymupdf.Rect
# Plain-text extraction flags, with ligatures (fi, fl, ...) split into letters.
TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES

LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
                           "\xa0": " ", "\xad": "", "\u200b": ""})
MONO_RE = re.compile(r"mono|courier|consolas|menlo|typewriter|cmtt", re.I)
BOLD_RE = re.compile(r"bold|black|heavy|semibold|demi", re.I)
ITAL_RE = re.compile(r"italic|oblique", re.I)
BULLET_RE = re.compile(r"^([•●▪◦‣∙·]|[-–—*])\s+")
ROMAN_NUMERAL = r"[ivxlcdm]+"
NUM_RE = re.compile(
    rf"^(\d{{1,3}}[.)]|(?i:{ROMAN_NUMERAL})[.)]|\((?:\d{{1,3}}|(?i:{ROMAN_NUMERAL})|[^\W_])\))\s+",
    re.UNICODE,
)
ORDERED_MARKER_RE = re.compile(
    rf"(?:\d{{1,3}}[.)]|(?i:{ROMAN_NUMERAL})[.)]|\((?:\d{{1,3}}|(?i:{ROMAN_NUMERAL}))\))",
    re.UNICODE,
)
CAPTION_RE = re.compile(r"^[\*_\s]*(fig(ure)?|table|algorithm|listing)\b\.?\s*\d*", re.I)
PAGENUM_RE = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.I)
TERMINAL = ".!?:;\"”'…！？。｡؟؛։॥"


# --------------------------------------------------------------------------- helpers
@dataclass
class Item:
    kind: str            # h | p | li | code | table | fig | meta
    rect: "Rect"
    text: str = ""
    page: int = 0


def clean(s: str) -> str:
    # Keep every script intact while composing combining characters consistently.
    # NFC also avoids visually identical text being written with inconsistent bytes.
    return unicodedata.normalize("NFC", s).translate(LIGATURES)


def norm(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def line_text(line) -> str:
    return "".join(s["text"] for s in line["spans"])


def is_bold(s):  return bool(s["flags"] & 16) or bool(BOLD_RE.search(s["font"]))
def is_ital(s):  return bool(s["flags"] & 2) or bool(ITAL_RE.search(s["font"]))
def is_mono(s):  return bool(s["flags"] & 8) or bool(MONO_RE.search(s["font"]))


def overlap_frac(a: Rect, b: Rect) -> float:
    """Fraction of a's area that lies inside b."""
    area = a.get_area()
    if area <= 0:
        return 0.0
    inter = a & b
    return 0.0 if inter.is_empty else inter.get_area() / area


def touches(a: Rect, b: Rect, pad: float) -> bool:
    return (a.x0 - pad <= b.x1 and b.x0 - pad <= a.x1 and
            a.y0 - pad <= b.y1 and b.y0 - pad <= a.y1)


def merge_rects(tagged, pad):
    """Merge touching rects. tagged = [(Rect, is_raster)]. Returns same shape."""
    items = [[Rect(r), t] for r, t in tagged]
    changed = True
    while changed:
        changed, out = False, []
        while items:
            r, t = items.pop()
            i = 0
            while i < len(items):
                r2, t2 = items[i]
                if touches(r, r2, pad):
                    r = Rect(min(r.x0, r2.x0), min(r.y0, r2.y0), max(r.x1, r2.x1), max(r.y1, r2.y1))
                    t = t or t2
                    items.pop(i)
                    changed = True
                else:
                    i += 1
            out.append([r, t])
        items = out
    return [(r, t) for r, t in items]


def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s).strip("_") or "document"
def ordered_marker_number(marker: str) -> int | None:
    """Return the numeric value of an Arabic or Roman ordered-list marker."""
    if marker.startswith("(") and marker.endswith(")"):
        value = marker[1:-1]
    elif marker[-1:] in ".)":
        value = marker[:-1]
    else:
        return None
    if value.isdecimal():
        return int(value)
    if not re.fullmatch(ROMAN_NUMERAL, value, re.IGNORECASE):
        return None
    numerals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, previous = 0, 0
    for char in reversed(value.upper()):
        current = numerals[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def roman_marker_style(marker: str) -> str | None:
    """Return whether a marker is Roman, preserving its source case."""
    if marker.startswith("(") and marker.endswith(")"):
        value = marker[1:-1]
    elif marker[-1:] in ".)":
        value = marker[:-1]
    else:
        return None
    if not re.fullmatch(ROMAN_NUMERAL, value, re.IGNORECASE):
        return None
    return "I" if value[:1].isupper() else "i"



def installed_ocr_languages() -> list[str]:
    """Return installed Tesseract language packs suitable for text recognition."""
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines()
            if re.fullmatch(r"[\w./-]+", line.strip())
            and line.strip() not in {"osd", "equ"}]
# --------------------------------------------------------------------------- span -> markdown
def block_spans(lines):
    """Flatten lines to spans; join lines with a space, or de-hyphenate."""
    out = []
    for i, l in enumerate(lines):
        spans = [dict(s) for s in l["spans"] if s["text"]]
        if not spans:
            continue
        last = spans[-1]
        if i + 1 < len(lines):
            nxt = clean(line_text(lines[i + 1])).lstrip()
            cur = last["text"].rstrip()
            if len(cur) > 1 and cur.endswith("-") and cur[-2].isalpha() and nxt[:1].islower():
                last["text"] = cur[:-1]
            else:
                last["text"] = cur + " "
        out.append(spans)
    return [s for spans in out for s in spans]


def strip_leading(spans, n):
    """Remove leading whitespace plus n characters (a list marker) from spans."""
    out, first = [], True
    for s in spans:
        t = s["text"]
        if first:
            t = t.lstrip()
            if not t:
                continue
            first = False
        if n > 0:
            cut = min(n, len(t))
            t, n = t[cut:], n - cut
        if t:
            out.append({**s, "text": t})
    return out


def spans_to_md(spans) -> str:
    merged = []
    for s in spans:
        key = (is_bold(s), is_ital(s), bool(s["flags"] & 1))
        t = clean(s["text"])
        if merged and merged[-1][0] == key:
            merged[-1][1] += t
        else:
            merged.append([key, t])
    out = []
    for (bold, ital, sup), t in merged:
        core = t.strip()
        if not core:
            out.append(" ")
            continue
        lead = " " if t[:1].isspace() else ""
        trail = " " if t[-1:].isspace() else ""
        core = core.replace("*", r"\*").replace("_", r"\_")
        if sup:
            core = f"<sup>{core}</sup>"
        if bold and ital:
            core = f"***{core}***"
        elif bold:
            core = f"**{core}**"
        elif ital:
            core = f"*{core}*"
        out.append(lead + core + trail)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def table_to_md(rows, strict=False):
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows if r]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return None
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    keep = [j for j in range(ncol) if any(r[j] for r in rows)]
    rows = [[r[j] for j in keep] for r in rows]
    ncol = len(keep)
    if ncol < 2:
        return None
    cells = [c for r in rows for c in r]
    if sum(1 for c in cells if c) / len(cells) < 0.4:
        return None  # mostly empty -> probably a chart/grid, not a table
    nonempty = [c for c in cells if c]
    avg = sum(len(c) for c in nonempty) / len(nonempty)
    if strict and (ncol < 3 or len(rows) < 3 or avg > 50):
        return None
    esc = lambda c: clean(c).replace("|", r"\|")
    lines = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
             "| " + " | ".join("---" for _ in rows[0]) + " |"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(lines)


# --------------------------------------------------------------------------- reading order
def order_items(items, page_rect):
    """Sort items into reading order; detects a two-column layout via a free central gutter."""
    pw = page_rect.width
    mid = page_rect.x0 + pw / 2
    tol = 0.03 * pw
    flat = sorted(items, key=lambda it: (round(it.rect.y0 / 3), it.rect.x0))
    full = [it for it in items if it.rect.x0 < mid - tol and it.rect.x1 > mid + tol]
    fid = {id(i) for i in full}
    rest = [it for it in items if id(it) not in fid]
    left = [it for it in rest if (it.rect.x0 + it.rect.x1) / 2 < mid]
    right = [it for it in rest if (it.rect.x0 + it.rect.x1) / 2 >= mid]
    height = lambda L: sum(i.rect.height for i in L)
    total = height(items) or 1
    if not (height(left) > 0.2 * total and height(right) > 0.2 * total
            and height(full) < 0.35 * total and len(left) >= 2 and len(right) >= 2):
        return flat

    lset, rset = {id(i) for i in left}, {id(i) for i in right}
    full.sort(key=lambda it: it.rect.y0)
    out, placed = [], set()
    for f in full + [None]:
        limit = f.rect.y0 - 1 if f else float("inf")
        for col in (lset, rset):
            band = sorted((it for it in items if id(it) in col and id(it) not in placed
                           and it.rect.y0 < limit), key=lambda it: it.rect.y0)
            out += band
            placed |= {id(it) for it in band}
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- converter
class PasswordRequired(RuntimeError):
    """Raised when a PDF needs a password before it can be converted."""


class Cancelled(RuntimeError):
    """Raised when a caller cancels an in-progress conversion."""


class Converter:
    def __init__(self, args, on_event: Optional[Callable[[str], None]] = None,
                 cancel: Optional[Callable[[], bool]] = None):
        self.a = args
        self._ocr_warned = False
        self._ocr_page = False
        self._on_event = on_event or (lambda _message: None)
        self._cancel = cancel or (lambda: False)

    def _event(self, message: str) -> None:
        self._on_event(message)
    # ---- document-level pre-pass -------------------------------------------------
    def _doc_stats(self, doc):
        sizes, mono, total = Counter(), 0, 0
        for page in doc:
            for b in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
                if b.get("type") != 0:
                    continue
                for l in b["lines"]:
                    for s in l["spans"]:
                        n = len(s["text"].strip())
                        if n:
                            sizes[round(s["size"] * 2) / 2] += n
                            total += n
                            mono += n if is_mono(s) else 0
        self.body = max(sizes, key=sizes.get) if sizes else 10.0
        cands = {round(s) for s, n in sizes.items()
                 if s >= self.body * 1.12 and s >= self.body + 1 and n <= 0.2 * total}
        self.heading_sizes = sorted(cands, reverse=True)
        self.doc_mono = total > 0 and mono / total > 0.5

    def _find_running(self, doc):
        n = len(doc)
        self.running = set()
        if n < 4 or self.a.keep_headers:
            return
        counts = Counter()
        for page in doc:
            pr, seen = page.rect, set()
            for b in page.get_text("blocks"):
                if b[6] != 0:
                    continue
                if b[3] < pr.y0 + 0.08 * pr.height or b[1] > pr.y1 - 0.08 * pr.height:
                    seen.add(self._running_key(b[4]))
            counts.update(seen)
        self.running = {k for k, c in counts.items() if c >= max(3, int(0.4 * n))}

    @staticmethod
    def _running_key(text):
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.strip().lower()))

    def _is_running(self, b, pr):
        if self.a.keep_headers:
            return False
        r = Rect(b["bbox"])
        if not (r.y1 < pr.y0 + 0.08 * pr.height or r.y0 > pr.y1 - 0.08 * pr.height):
            return False
        txt = " ".join(line_text(l) for l in b["lines"])
        return bool(PAGENUM_RE.match(txt)) or self._running_key(txt) in self.running

    # ---- OCR ---------------------------------------------------------------------
    def _ocr_language(self) -> str:
        requested = getattr(self.a, "ocr_lang", "auto").strip()
        if requested.casefold() != "auto":
            return requested
        if not hasattr(self, "_auto_ocr_language"):
            languages = installed_ocr_languages()
            if languages:
                self._auto_ocr_language = "+".join(languages)
                self._event(f"OCR auto: using installed language packs ({self._auto_ocr_language})")
            else:
                # PyMuPDF's OCR backend may still be available without the CLI.
                self._auto_ocr_language = "eng"
                self._event("Warning: could not list Tesseract language packs; OCR is falling back to eng")
        return self._auto_ocr_language

    def _textpage(self, page):
        mode = self.a.ocr
        if mode == "off":
            return None, False
        few = len(page.get_text().strip()) < 20
        if mode == "force" or (few and page.get_images()):
            try:
                return page.get_textpage_ocr(language=self._ocr_language(), dpi=300, full=True), True
            except Exception as e:  # tesseract missing, etc.
                if not self._ocr_warned:
                    self._event(f"Warning: OCR unavailable ({e}); scanned pages will be saved as images")
                    self._ocr_warned = True
        return None, False

    # ---- tables ------------------------------------------------------------------
    @staticmethod
    def _table_rows(t, words):
        """Cell text from word boxes (more reliable than Table.extract() with kerned text)."""
        try:
            rows = []
            for row in t.rows:
                cells = []
                for c in row.cells:
                    if c is None:
                        cells.append("")
                        continue
                    cr = Rect(c)
                    ws = [w for w in words if cr.x0 <= (w[0] + w[2]) / 2 <= cr.x1
                          and cr.y0 <= (w[1] + w[3]) / 2 <= cr.y1]
                    ws.sort(key=lambda w: (round(w[1] / 3), w[0]))
                    cells.append(" ".join(w[4] for w in ws))
                rows.append(cells)
            return rows
        except Exception:
            return t.extract()

    def _tables(self, page):
        found = []
        words = page.get_text("words")

        def collect(strict, **kw):
            try:
                tf = page.find_tables(**kw)
            except Exception:
                return
            for t in tf.tables:
                r = Rect(t.bbox)
                if any(overlap_frac(r, f[0]) > 0.5 or overlap_frac(f[0], r) > 0.5 for f in found):
                    continue
                md = table_to_md(self._table_rows(t, words), strict=strict)
                if md:
                    found.append((r, md))

        collect(False)
        if self.a.tables == "auto":  # booktabs-style: horizontal rules, columns by alignment
            collect(True, vertical_strategy="text", horizontal_strategy="lines")
        return found

    # ---- figures -----------------------------------------------------------------
    def _figures(self, page, binfo, table_rects):
        pr = page.rect
        cands = []
        try:
            cands += [(Rect(i["bbox"]), True) for i in page.get_image_info()]
        except Exception:
            pass
        try:
            cands += [(Rect(r), False) for r in page.cluster_drawings()]
        except Exception:
            pass  # PyMuPDF too old for cluster_drawings
        cands = [(r & pr, t) for r, t in cands]
        cands = [(r, t) for r, t in cands if not r.is_empty]

        figs = []
        for r, is_raster in merge_rects(cands, self.a.merge_gap):
            if r.width < self.a.min_fig or r.height < self.a.min_fig:
                continue
            if r.get_area() > 0.85 * pr.get_area():
                continue  # page background / border
            if any(overlap_frac(r, t) > 0.5 for t in table_rects):
                continue
            chars = sum(len(it.text) for br, its in binfo if overlap_frac(br, r) > 0.6 for it in its)
            if chars > (800 if is_raster else 300):
                continue  # a text box / callout, not a figure
            if not is_raster:  # absorb axis labels, legends, panel titles near a vector chart
                for _ in range(3):
                    grown = Rect(r.x0 - 14, r.y0 - 14, r.x1 + 14, r.y1 + 14)
                    for br, its in binfo:
                        if (its and all(i.kind == "p" and len(i.text) <= 40
                                        and not CAPTION_RE.match(i.text) for i in its)
                                and not br.is_empty and touches(grown, br, 0)):
                            r = Rect(min(r.x0, br.x0), min(r.y0, br.y0), max(r.x1, br.x1), max(r.y1, br.y1))
            figs.append(r & pr)
        return figs

    def _save_clip(self, page, rect, pno, k):
        self.assets.mkdir(parents=True, exist_ok=True)
        clip = Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2) & page.rect
        pix = page.get_pixmap(clip=clip, dpi=self.a.dpi, alpha=False)
        name = f"p{pno:03d}_fig{k}.{self.a.image_format}"
        if self.a.image_format == "jpg":
            pix.save(str(self.assets / name), jpg_quality=85)
        else:
            pix.save(str(self.assets / name))
        return name

    # ---- text blocks -------------------------------------------------------------
    @staticmethod
    def _split_block(b):
        """MuPDF sometimes glues a heading or caption to the next paragraph; split at style changes."""
        lines = [l for l in b["lines"] if line_text(l).strip()]
        if len(lines) < 2:
            return [b]

        def info(l):
            sp = [s for s in l["spans"] if s["text"].strip()]
            w = Counter()
            for s in sp:
                w[round(s["size"] * 2) / 2] += len(s["text"].strip())
            return max(w, key=w.get), all(is_bold(s) for s in sp), clean(line_text(l)).strip()

        segs = [[lines[0]]]
        for prev, cur in zip(lines, lines[1:]):
            (ps, pb, pt), (cs, cb, ct) = info(prev), info(cur)
            caption_start = re.match(r"^(fig(ure)?\.?|table)\s*\d+\s*[.:]", ct, re.I)
            if (abs(ps - cs) >= 1.0 or (pb != cb and min(len(pt), len(ct)) <= 120)
                    or (caption_start and pt[-1:] in TERMINAL)):
                segs.append([cur])
            else:
                segs[-1].append(cur)
        if len(segs) == 1:
            return [b]
        out = []
        for seg in segs:
            r = Rect(seg[0]["bbox"])
            for l in seg[1:]:
                r |= Rect(l["bbox"])
            out.append({**b, "lines": seg, "bbox": tuple(r)})
        return out

    def _block_items(self, b, pno):
        lines = [l for l in b["lines"] if line_text(l).strip()]
        if not lines or any(abs(l["dir"][0]) < 0.9 for l in lines):
            return []  # empty or rotated (e.g. arXiv side stamp, y-axis labels)
        rect = Rect(b["bbox"])
        spans = [s for l in lines for s in l["spans"] if s["text"].strip()]
        weights = Counter()
        for s in spans:
            weights[round(s["size"] * 2) / 2] += len(s["text"].strip())
        size = max(weights, key=weights.get)
        plain = re.sub(r"\s+", " ", clean(" ".join(line_text(l).strip() for l in lines))).strip()

        # code
        if not self.doc_mono and not self._ocr_page and all(is_mono(s) for s in spans):
            if len(lines) >= 2:
                x0 = min(l["bbox"][0] for l in lines)
                cw = max(size * 0.6, 1)
                code = "\n".join(" " * int((l["bbox"][0] - x0) / cw + 0.5) + clean(line_text(l)).strip()
                                 for l in lines)
                return [Item("code", rect, code, pno)]
            return [Item("p", rect, f"`{plain}`", pno)]

        # headings
        lvl = self.toc.get((pno, norm(plain)))
        caption = CAPTION_RE.match(plain)
        if lvl is None and not caption and len(lines) <= 3 and len(plain) <= 200:
            if round(size) in self.heading_sizes:
                lvl = min(self.heading_sizes.index(round(size)) + 1, 4)
            elif (all(is_bold(s) for s in spans) and len(lines) <= 2 and len(plain) <= 120
                  and re.search(r"[^\W\d_]", plain, re.UNICODE)
                  and not plain.endswith(tuple(TERMINAL))
                  and not BULLET_RE.match(plain)):
                lvl = max(2, min(len(self.heading_sizes) + 1, 4))
        if lvl:
            return [Item("h", rect, "#" * min(lvl, 6) + " " + plain, pno)]

        # lists / paragraphs
        groups, in_num = [], False
        for i, l in enumerate(lines):
            t = clean(line_text(l)).strip()
            m_b, m_n = BULLET_RE.match(t), NUM_RE.match(t)
            roman_marker = bool(m_n and roman_marker_style(m_n.group(1)))
            # Roman markers may follow introductory prose in the same PDF block.
            m = m_b or (m_n if (i == 0 or in_num or roman_marker) else None)
            if m:
                groups.append({"prefix": m.group(1), "lines": [l], "plen": m.end()})
                in_num = bool(m_n and not m_b)
            elif groups:
                groups[-1]["lines"].append(l)
            else:
                groups.append({"prefix": None, "lines": [l], "plen": 0})

        items = []
        for g in groups:
            sp = block_spans(g["lines"])
            if g["prefix"]:
                sp = strip_leading(sp, g["plen"])
            text = spans_to_md(sp)
            if not text:
                continue
            kind = "p"
            if g["prefix"]:
                kind, p = "li", g["prefix"]
                number = ordered_marker_number(p) if ORDERED_MARKER_RE.fullmatch(p) else None
                if roman_marker_style(p):
                    # Keep Roman markers exactly as they appear in the source PDF.
                    text = f"{p} {text}"
                elif number is not None:
                    text = f"{number}. {text}"
                elif p.startswith("("):
                    text = f"- {p} {text}"
                else:
                    text = f"- {text}"
            bb = g["lines"][0]["bbox"]
            r = Rect(bb)
            for l in g["lines"][1:]:
                r |= Rect(l["bbox"])
            items.append(Item(kind, r, text, pno))
        return items

    # ---- one page ----------------------------------------------------------------
    def _page(self, page, pno):
        a, pr = self.a, page.rect
        tp, ocr_used = self._textpage(page)
        self._ocr_page = ocr_used  # OCR text uses a fake monospace font; don't treat it as code
        d = page.get_text("dict", flags=TEXT_FLAGS, textpage=tp)
        blocks = [b for b in d["blocks"] if b.get("type") == 0 and b.get("lines")]
        nchars = sum(len(s["text"].strip()) for b in blocks for l in b["lines"] for s in l["spans"])

        if nchars < 10:  # scanned page w/o OCR, or a full-page image
            if a.images and (page.get_images() or page.get_drawings()):
                self._event(f"Warning: page {pno}: no text layer -> saved as image")
                return [Item("fig", pr, self._save_clip(page, pr, pno, 1), pno)]
            return []

        blocks = [sb for b in blocks for sb in self._split_block(b)]
        blocks = [b for b in blocks if not self._is_running(b, pr)]
        binfo = [(Rect(b["bbox"]), self._block_items(b, pno)) for b in blocks]

        tables = self._tables(page) if (a.tables != "off" and not ocr_used) else []
        table_rects = [r for r, _ in tables]
        figs = self._figures(page, binfo, table_rects) if (a.images and not ocr_used) else []

        items = [Item("table", r, md, pno) for r, md in tables]
        items += [Item("fig", r, self._save_clip(page, r, pno, k), pno) for k, r in enumerate(figs, 1)]
        for br, its in binfo:
            if any(overlap_frac(br, r) > 0.6 for r in table_rects + figs):
                continue
            items += its
        items = order_items(items, pr)
        if a.page_markers:
            items.insert(0, Item("meta", pr, f"<!-- page {pno} -->", pno))
        return items

    # ---- assemble ----------------------------------------------------------------
    @staticmethod
    def _joinable(prev, cur):
        t = prev.text.rstrip()
        if not t or t[-1] in TERMINAL:
            return False
        first = re.sub(r"^[\*_\\]+", "", cur.text)[:1]
        return first.islower()

    @staticmethod
    def _join(a, b):
        a = a.rstrip()
        if re.search(r"[A-Za-z]-$", a) and b[:1].islower():
            return a[:-1] + b
        return a + " " + b

    def _render(self, items):
        merged, floats = [], ("fig", "table", "meta")
        for it in items:
            if it.kind == "p":
                j = len(merged) - 1
                while j >= 0 and merged[j].kind in floats:
                    j -= 1
                if j >= 0 and merged[j].kind in ("p", "li") and self._joinable(merged[j], it):
                    merged[j].text = self._join(merged[j].text, it.text)
                    continue
            merged.append(it)

        parts = []
        for i, it in enumerate(merged):
            if it.kind == "fig":
                nxt = merged[i + 1] if i + 1 < len(merged) else None
                alt = f"Figure on page {it.page}"
                if nxt and nxt.kind == "p" and CAPTION_RE.match(nxt.text):
                    alt = re.sub(r"[\[\]\*\\]", "", nxt.text)[:120]
                text = f"![{alt}]({quote(self.assets.name)}/{quote(it.text)})"
            elif it.kind == "code":
                text = "```\n" + it.text + "\n```"
            else:
                text = it.text
            parts.append((it.kind, text))
        out = ""
        for i, (kind, text) in enumerate(parts):
            if i:
                out += "\n" if (kind == "li" and parts[i - 1][0] == "li") else "\n\n"
            out += text
        return out + "\n"
    # ---- public ------------------------------------------------------------------
    def convert(self, pdf: Path, out_dir: Path, password: Optional[str] = None) -> Path:
        doc = pymupdf.open(pdf)
        if doc.needs_pass and not (password and doc.authenticate(password)):
            doc.close()
            raise PasswordRequired("PDF is password protected")
        stem = safe_name(pdf.stem)
        out_dir.mkdir(parents=True, exist_ok=True)
        self.assets = out_dir / f"{stem}_assets"
        try:
            self._doc_stats(doc)
            self._find_running(doc)
            self.toc = {(p, norm(t)): l for l, t, p in doc.get_toc(simple=True)}
            items = []
            for pno, page in enumerate(doc, 1):
                if self._cancel():
                    raise Cancelled("Conversion cancelled")
                items += self._page(page, pno)
            if self._cancel():
                raise Cancelled("Conversion cancelled")
            out = out_dir / f"{stem}.md"
            out.write_text(self._render(items), encoding="utf-8")
            nfig = sum(1 for i in items if i.kind == "fig")
            ntab = sum(1 for i in items if i.kind == "table")
            self._event(f"{len(doc)} pages -> {out}  ({nfig} figures, {ntab} tables)")
            return out
        except Exception:
            if self.assets.exists():
                shutil.rmtree(self.assets, ignore_errors=True)
            raise
        finally:
            doc.close()

# --------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Convert PDFs to Markdown (with figures, tables, OCR).")
    ap.add_argument("inputs", nargs="+", help="PDF files and/or directories")
    ap.add_argument("-o", "--out", default="markdown_output", help="output directory")
    ap.add_argument("-r", "--recursive", action="store_true", help="search directories recursively")
    ap.add_argument("--dpi", type=int, default=150, help="resolution for saved figures (default 150)")
    ap.add_argument("--image-format", choices=["png", "jpg"], default="png")
    ap.add_argument("--no-images", dest="images", action="store_false", help="don't extract figures")
    ap.add_argument("--tables", choices=["auto", "lines", "off"], default="auto",
                    help="auto: ruled + booktabs-style detection; lines: ruled only; off")
    ap.add_argument("--ocr", choices=["auto", "off", "force"], default="auto",
                    help="OCR scanned pages with Tesseract (default auto)")
    ap.add_argument("--ocr-lang", default="eng", help="Tesseract language(s), e.g. eng+deu")
    ap.add_argument("--password", help="password for an encrypted PDF")
    ap.add_argument("--page-markers", action="store_true", help="insert <!-- page N --> comments")
    ap.add_argument("--keep-headers", action="store_true", help="keep running headers/footers")
    ap.add_argument("--merge-gap", type=float, default=8, help="merge figure pieces closer than N pt")
    ap.add_argument("--min-fig", type=float, default=36, help="ignore figures smaller than N pt")
    args = ap.parse_args()

    pdfs = []
    for inp in args.inputs:
        p = Path(inp)
        if p.is_dir():
            pdfs += sorted(p.rglob("*.pdf") if args.recursive else p.glob("*.pdf"))
        elif p.is_file():
            pdfs.append(p)
        else:
            print(f"! not found: {inp}", file=sys.stderr)
    if not pdfs:
        sys.exit("No PDFs found.")

    out_dir, failed = Path(args.out), 0
    for pdf in pdfs:
        print(f"Converting {pdf} ...")
        try:
            Converter(args, on_event=lambda message: print(f"  {message}")).convert(pdf, out_dir, password=args.password)
        except PasswordRequired:
            failed += 1
            print("  ! password required (use --password)", file=sys.stderr)
        except Exception as e:
            failed += 1
            print(f"  ! failed: {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
