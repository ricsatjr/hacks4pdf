#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     # Held below 2.0 on purpose: 2.x pulls in numpy, networkx and
#     # onnxruntime (~105 MB) for features this tool never uses, where 1.x is
#     # a single 25 MB package. Tested on both - drop the bound if you want
#     # 2.x anyway.
#     "pymupdf>=1.24,<2",
# ]
# ///
#
# Copyright (C) 2026 Ricarido M. Saturay Jr.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
"""Permanently remove credit-card numbers and transaction rows from PDF bills.

This tool locates sensitive text in a PDF's text layer and *destroys* it using
PyMuPDF redaction annotations. Unlike drawing a black rectangle, applying a
redaction deletes the underlying glyphs, so the content can no longer be
recovered by copy-paste, `pdftotext`, or a text-extraction library.

Two kinds of redaction are available, and they combine freely:

1. **Card numbers** - a known number (``--number``) or any Luhn-valid,
   card-length digit run the script finds for itself (``--auto``).
2. **Statement rows** - every transaction line in the table is erased except
   the ones you name with ``--keep-row``. A row's currency continuation line
   (a ``USD 12.34`` printed under a foreign charge) follows its parent row, so
   a kept row keeps its continuation and a redacted row loses its own.
3. **Whole pages** - ``--drop-empty-pages`` deletes any page left with no
   surviving transaction, so a six-page statement can come out as one page.
   ``--keep-page`` is the looser alternative, matching anywhere on the page.

When one merchant appears several times, a ``--keep-date`` list narrows the
``--keep-row`` it follows, and that term only::

    --keep-row acme --keep-date 03/14/26,04/18/26 --keep-row globex

keeps two Acme charges and every Globex charge.

Typical use
-----------
    # Erase the card number only
    python3 redact_cc.py ~/bills -n "4111 1111 1111 1111"

    # Erase the card number AND every transaction except two merchants
    python3 redact_cc.py ~/bills --auto --also-masked \\
        --keep-row acme --keep-row globex \\
        --redact-line 'PREVIOUS BALANCE|SUBTOTAL'

    # ...and throw away any page left with no surviving transaction
    python3 redact_cc.py ~/bills --auto --also-masked \\
        --keep-row acme --keep-row globex --drop-empty-pages

    # Two specific Acme charges, but every Globex one
    python3 redact_cc.py ~/bills --auto \\
        --keep-row acme --keep-date 03/14/26,04/18/26 \\
        --keep-row globex \\
        --drop-empty-pages

    # Preview first, without writing anything
    python3 redact_cc.py ~/bills --auto --keep-row acme --dry-run -v

Requirements
------------
    uv run redact_cc.py ...        # dependencies come from the header above

or, without uv::

    pip install 'pymupdf<2'

Limitations
-----------
* Only the *text layer* is searched. A scanned/photographed statement has no
  text layer; the script reports that and you will need OCR first
  (e.g. ``ocrmypdf in.pdf out.pdf``) or a manual rectangle.
* A match is redacted at word granularity. If the PDF glues a number to
  neighbouring characters inside one word, those characters go too. This errs
  on the side of over-redaction, which is the safe direction.
* A card number split across two *rows* is not matched.
* Row mode treats a line starting with a date as a transaction. A statement
  that omits the date column needs ``--row-anchor`` to describe its rows.

Part of hacks4pdf. Licensed under the GNU GPL v3 or later; see the header
above and the LICENSE file. Note that PyMuPDF, which this tool depends on, is
itself dual-licensed under the AGPL v3 or an Artifex commercial licence.

Written with AI assistance (Claude, Anthropic) to the author's requirements
and under the author's review, and tested against synthetic statements rather
than real ones. See the AI use section of the README. Check what this tool
produces before you send it to anybody.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:  # PyMuPDF ships under both names; 'pymupdf' is the modern one
    import pymupdf as fitz
except ImportError:  # pragma: no cover - PyMuPDF older than 1.24
    try:
        import fitz  # type: ignore
    except ImportError:
        sys.exit("PyMuPDF is not installed. Run:  pip install pymupdf")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Characters a statement may use to mask digits (asterisk, letter x, bullets).
MASK_CHARS = "*xX#•·●○"

#: Characters kept when a text row is "compacted" before matching.
KEEP_CHARS = set("0123456789") | set(MASK_CHARS)

#: Characters that may sit *inside* a printed card number without breaking it.
BENIGN_CHARS = set(" -–—")

#: Placed between digits that cannot belong to the same printed number, so a
#: date column and an amount column never merge into one card-length run.
SEPARATOR = "|"

#: A horizontal gap wider than this many row-heights is a column boundary.
COLUMN_GAP_FACTOR = 0.8

#: Grey used for the border drawn by ``--fill outline``.
OUTLINE_COLOR = (0.45, 0.45, 0.45)

#: Thickness in points of that border.
OUTLINE_WIDTH = 0.4

#: A run of digits long enough to contain a card number (Amex 15 .. Maestro 19).
CARD_DIGIT_RE = re.compile(r"\d{13,}")

#: Plausible card-number lengths, longest first so the widest match wins.
CARD_LENGTHS = (19, 18, 17, 16, 15, 14, 13)

#: A partly masked card display, e.g. ``************3456`` or ``411111****1111``.
MASKED_CARD_RE = re.compile(r"[\d*]{12,19}")

#: Default anchor for a transaction row: a leading date like 03/07/26.
DEFAULT_ROW_ANCHOR = r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"

#: A date anywhere in a row, in either ``03/14/26`` or ``2026-03-14`` form.
DATE_TOKEN_RE = re.compile(r"\b(\d{1,4})[/-](\d{1,2})[/-](\d{1,4})\b")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def digits_only(text: str) -> str:
    """Return *text* stripped of everything except decimal digits."""
    return "".join(char for char in text if char.isdigit())


def alnum_only(text: str) -> str:
    """Return *text* folded to lowercase letters and digits.

    Used when verifying a written file, so that spacing changes between the
    source and the output cannot hide a row that survived redaction.
    """
    return "".join(char for char in text.lower() if char.isalnum())


def luhn_ok(number: str) -> bool:
    """Return True if *number* (digits only) passes the Luhn checksum.

    Used to keep ``--auto`` from redacting long reference numbers, account IDs
    and dates that merely look card-shaped.
    """
    total, parity = 0, len(number) % 2
    for index, char in enumerate(number):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def parse_date(text: str, order: str = "mdy") -> tuple[int, int, int] | None:
    """Return the first date in *text* as a ``(year, month, day)`` triple.

    A statement row opens with its transaction date, so the first date found is
    the one that identifies the row. Two-digit years are read as 2000-2099, and
    a four-digit leading field is taken as an ISO date whatever *order* says.

    Args:
        text: The text to search, such as a row or a ``--keep-date`` value.
        order: ``"mdy"`` for 03/14/26 as 26 September, ``"dmy"`` for 9 June.

    Returns:
        A ``(year, month, day)`` triple, or None if no usable date is present.
    """
    match = DATE_TOKEN_RE.search(text)
    if not match:
        return None

    first, second, third = (int(part) for part in match.groups())
    if len(match.group(1)) == 4:        # 2026-03-14
        year, month, day = first, second, third
    elif order == "dmy":
        day, month, year = first, second, third
    else:
        month, day, year = first, second, third

    if year < 100:
        year += 2000
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return (year, month, day)


def luhn_windows(compact: str) -> list[tuple[int, int]]:
    """Find every card-length, Luhn-valid window inside *compact*.

    A statement may print a card number immediately beside a reference number,
    producing one long digit run. Each window of a plausible card length is
    therefore tested, longest first, rather than only the run as a whole.

    Args:
        compact: A digit/mask string from :func:`compact_row`.

    Returns:
        A list of ``(start, end)`` index pairs into *compact*.
    """
    found: list[tuple[int, int]] = []
    for match in CARD_DIGIT_RE.finditer(compact):
        run, base, offset = match.group(), match.start(), 0
        while offset < len(run):
            for length in CARD_LENGTHS:
                window = run[offset:offset + length]
                if len(window) == length and luhn_ok(window):
                    found.append((base + offset, base + offset + length))
                    offset += length
                    break
            else:
                offset += 1
    return found


# ---------------------------------------------------------------------------
# Row geometry
# ---------------------------------------------------------------------------

def group_rows(words: list, tolerance: float = 0.6) -> list[list]:
    """Group a page's words into visual rows, top to bottom.

    Rows are built from vertical position rather than from PyMuPDF's block and
    line numbering, because a table column is often its own text block: the
    date, description and amount of one statement line can carry three
    different line numbers while sitting on the same baseline.

    Args:
        words: Word tuples from ``page.get_text("words")``.
        tolerance: Fraction of a word's height that two words' vertical
            centres may differ by and still count as the same row.

    Returns:
        A list of rows, each a list of word tuples ordered left to right.
    """
    if not words:
        return []

    ordered = sorted(words, key=lambda word: ((word[1] + word[3]) / 2, word[0]))
    rows: list[list] = [[ordered[0]]]

    for word in ordered[1:]:
        current = rows[-1]
        centre = (word[1] + word[3]) / 2
        last_centre = sum((w[1] + w[3]) / 2 for w in current) / len(current)
        height = max(word[3] - word[1], 1.0)
        if abs(centre - last_centre) <= tolerance * height:
            current.append(word)
        else:
            rows.append([word])

    for row in rows:
        row.sort(key=lambda word: word[0])
    return rows


def row_rect(row: list):
    """Return the bounding rectangle of every word in *row*."""
    rect = fitz.Rect(row[0][:4])
    for word in row[1:]:
        rect |= fitz.Rect(word[:4])
    return rect


def row_text(row: list) -> str:
    """Return the plain text of *row*, words joined by single spaces."""
    return " ".join(word[4] for word in row)


def compact_row(row: list) -> tuple[str, list[int]]:
    """Collapse a row into a string matchable against card numbers.

    Characters other than digits and mask characters are dropped, and every
    mask character is normalised to ``*``, so that ``4111 1111-1111 1111`` and
    ``4111111111111111`` match the same target.

    Digits that cannot plausibly belong to one printed number are kept apart by
    a :data:`SEPARATOR`, because blindly concatenating a row's digits invents
    card-length runs that never appeared on the page: two date columns and an
    amount can join into eighteen digits that pass the Luhn check and cost an
    innocent row its contents. A separator is inserted where words sit on
    opposite sides of a column gap, and around any word holding a character a
    card number never contains - a slash, comma or full stop.

    Args:
        row: Word tuples for one row, ordered left to right.

    Returns:
        A ``(compact_text, owner)`` pair, where ``owner[i]`` is the index into
        *row* of the word that contributed ``compact_text[i]``, or ``-1`` for a
        separator.
    """
    buffer: list[str] = []
    owner: list[int] = []
    previous_x1: float | None = None

    def separate() -> None:
        """Append a separator unless one is already in place."""
        if buffer and buffer[-1] != SEPARATOR:
            buffer.append(SEPARATOR)
            owner.append(-1)

    for word_index, word in enumerate(row):
        text = word[4]
        height = max(word[3] - word[1], 1.0)
        breaks = any(char not in KEEP_CHARS and char not in BENIGN_CHARS
                     for char in text)
        far = (previous_x1 is not None
               and word[0] - previous_x1 > COLUMN_GAP_FACTOR * height)

        if breaks or far:
            separate()
        for char in text:
            if char in KEEP_CHARS:
                buffer.append("*" if char in MASK_CHARS else char)
                owner.append(word_index)
        if breaks:
            separate()
        previous_x1 = word[2]

    return "".join(buffer), owner


# ---------------------------------------------------------------------------
# Deciding what to destroy
# ---------------------------------------------------------------------------

def find_spans(compact: str, targets: list[str], last4s: list[str],
               auto: bool, also_masked: bool) -> list[tuple[int, int]]:
    """Locate every stretch of *compact* that holds a card number.

    Args:
        compact: Output of :func:`compact_row`.
        targets: Literal digit strings supplied by the user.
        last4s: Known last-four groups, used to anchor masked matches.
        auto: Whether to Luhn-detect unknown card numbers.
        also_masked: Whether to redact partly masked card displays too.

    Returns:
        A list of ``(start, end)`` index pairs into *compact*.
    """
    spans: list[tuple[int, int]] = []

    # 1. Explicit targets the user gave us.
    for target in targets:
        start = 0
        while True:
            hit = compact.find(target, start)
            if hit < 0:
                break
            spans.append((hit, hit + len(target)))
            start = hit + 1

    # 2. Luhn-valid digit runs we discovered ourselves.
    if auto:
        spans.extend(luhn_windows(compact))

    # 3. Partly masked displays such as ``************3456``.
    if also_masked:
        for match in MASKED_CARD_RE.finditer(compact):
            text = match.group()
            if "*" not in text:
                continue
            if last4s and not any(text.endswith(four) for four in last4s):
                continue
            spans.append(match.span())

    return spans


def spans_to_rects(row: list, owner: list[int],
                   spans: list[tuple[int, int]], pad: float) -> list:
    """Convert character spans into padded rectangles on the page.

    Args:
        row: The row's word tuples.
        owner: Character-to-word map from :func:`compact_row`.
        spans: Character spans from :func:`find_spans`.
        pad: Points added on every side so descenders are fully covered.

    Returns:
        A list of ``fitz.Rect`` objects, one per span.
    """
    rects = []
    for start, end in spans:
        indices = sorted({index for index in owner[start:end] if index >= 0})
        if not indices:
            continue
        rect = fitz.Rect(row[indices[0]][:4])
        for index in indices[1:]:
            rect |= fitz.Rect(row[index][:4])
        rects.append(rect + (-pad, -pad, pad, pad))
    return rects


def classify_rows(rows: list[list], options: argparse.Namespace) -> list[bool]:
    """Decide, for each row, whether the whole row must be destroyed.

    The rules, in order:

    * A row matching the row anchor is a transaction. It survives if it
      satisfies at least one keep rule - a ``--keep-row`` term together with
      whatever ``--keep-date`` values followed it. With no rules at all -
      plain ``--redact-rows`` - every transaction goes.
    * A row that is not a transaction but sits tightly beneath one is a
      continuation (the ``USD 12.34`` under a foreign-currency charge) and
      inherits its parent's fate.
    * Any other row - column headings, section titles, the bank's name - is
      left alone unless it matches one of the ``--redact-line`` patterns.

    Args:
        rows: Output of :func:`group_rows`, ordered top to bottom.
        options: Parsed command-line options, supplying the keep terms, keep
            dates, forced-drop patterns, row anchor and continuation gap.

    Returns:
        A list of booleans parallel to *rows*; True means destroy the row.
    """
    keep_rules = options.keep_rules
    drop_patterns = options.drop_patterns
    anchor = options.anchor_re
    gap = options.row_gap

    decisions: list[bool] = []
    in_table = False
    previous_decision = False
    previous_centre: float | None = None

    for row in rows:
        text = row_text(row)
        rect = row_rect(row)
        height = max(rect.height, 1.0)
        centre = (rect.y0 + rect.y1) / 2

        if anchor.search(text):
            decision = not (keep_rules
                            and row_survives(text, keep_rules,
                                             options.date_order))
            in_table = True
        elif (in_table and previous_centre is not None
                and 0 < centre - previous_centre <= (1 + gap) * height):
            # Distance is measured centre to centre rather than between the
            # boxes' edges. On a tightly set statement the boxes of adjacent
            # rows overlap, making an edge-to-edge gap negative, and a
            # continuation line would then be mistaken for a fresh row and
            # left standing after its parent row was erased.
            decision = previous_decision
        else:
            decision = False

        if any(pattern.search(text) for pattern in drop_patterns):
            decision = True

        decisions.append(decision)
        previous_decision = decision
        previous_centre = centre

    return decisions


def clip_clear_of(rect, protected: list, margin: float):
    """Trim *rect* vertically so it keeps *margin* clear of surviving rows.

    A row's bounding box reaches up to the ascenders of its tallest letter and
    down past the descenders, so the boxes of two consecutive rows can touch or
    overlap. Left alone, the box erasing one row then bites into the top of the
    row below it - exactly the row that was meant to be preserved.

    Args:
        rect: The redaction rectangle to trim.
        protected: Bounding rectangles of rows that must stay legible.
        margin: Points of clearance to leave between them.

    Returns:
        A trimmed ``fitz.Rect``, or None if nothing meaningful is left of it.
    """
    result = fitz.Rect(rect)
    centre = (result.y0 + result.y1) / 2

    for other in protected:
        if result.x1 <= other.x0 or result.x0 >= other.x1:
            continue  # no horizontal overlap, so no conflict
        other_centre = (other.y0 + other.y1) / 2
        if other_centre > centre:
            result.y1 = min(result.y1, other.y0 - margin)
        elif other_centre < centre:
            result.y0 = max(result.y0, other.y1 + margin)

    return result if result.height > 0.5 and result.width > 0.5 else None


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def pages_to_keep(document, terms: list[str]) -> list[int]:
    """Return the indices of pages whose text contains one of *terms*.

    The test runs against each page's original text, before any redaction, so
    a page is judged on what the bill actually said.

    Args:
        document: An open PyMuPDF document.
        terms: Lowercase substrings; a page matching any one of them is kept.

    Returns:
        Page indices to keep, in order.
    """
    keep = []
    for page in document:
        lowered = page.get_text().lower()
        if any(term in lowered for term in terms):
            keep.append(page.number)
    return keep


def analyse_page(page, options: argparse.Namespace) -> tuple[list, list[bool]]:
    """Group one page into rows and decide the fate of each.

    Args:
        page: The page to inspect.
        options: Parsed command-line options.

    Returns:
        A ``(rows, decisions)`` pair, as produced by :func:`group_rows` and
        :func:`classify_rows`.
    """
    rows = group_rows(page.get_text("words"))
    if options.row_mode:
        return rows, classify_rows(rows, options)
    return rows, [False] * len(rows)


def surviving_rows(rows: list, decisions: list[bool],
                   anchor) -> list[str]:
    """Return the text of each transaction row left standing on a page.

    Args:
        rows: Output of :func:`group_rows`.
        decisions: Output of :func:`classify_rows`.
        anchor: Compiled regex identifying a transaction row.

    Returns:
        The text of every transaction row that was not destroyed.
    """
    return [row_text(row) for row, destroyed in zip(rows, decisions)
            if not destroyed and anchor.search(row_text(row))]


def apply_redactions(page) -> None:
    """Burn the page's redaction annotations in, leaving images intact.

    Older PyMuPDF releases do not accept the ``images`` keyword, so the call
    degrades gracefully.
    """
    kwargs = {}
    if hasattr(fitz, "PDF_REDACT_IMAGE_NONE"):
        kwargs["images"] = fitz.PDF_REDACT_IMAGE_NONE
    try:
        page.apply_redactions(**kwargs)
    except TypeError:  # pragma: no cover - older PyMuPDF
        page.apply_redactions()


def redact_pdf(path: Path, out_path: Path, targets: list[str],
               last4s: list[str], options: argparse.Namespace) -> dict:
    """Redact one PDF and write the result to *out_path*.

    Args:
        path: Source PDF.
        out_path: Destination PDF (not written when ``--dry-run`` is set).
        targets: Literal digit strings to destroy.
        last4s: Known last-four groups.
        options: Parsed command-line options.

    Returns:
        A report dict with keys ``hits``, ``rows``, ``kept``,
        ``pages_with_hits``, ``pages``, ``pages_dropped``, ``image_only_pages``,
        ``no_pages_left`` and ``leftover``.
    """
    document = fitz.open(path)
    report = {
        "hits": 0,
        "rows": 0,
        "kept": [],
        "pages_with_hits": 0,
        "pages": document.page_count,
        "pages_dropped": 0,
        "image_only_pages": 0,
        "no_pages_left": False,
        "leftover": [],
    }
    killed_rows: list[str] = []

    # Decide which pages survive before touching anything, so that dropped
    # pages are never processed and never counted. Both filters may apply: a
    # page must satisfy every one that is in force.
    keep_pages: set[int] | None = None

    if options.page_terms:
        keep_pages = set(pages_to_keep(document, options.page_terms))

    if options.drop_empty_pages:
        with_rows = {page.number for page in document
                     if surviving_rows(*analyse_page(page, options),
                                       options.anchor_re)}
        keep_pages = with_rows if keep_pages is None \
            else keep_pages & with_rows

    if keep_pages is not None:
        report["pages_dropped"] = document.page_count - len(keep_pages)
        if not keep_pages:
            report["no_pages_left"] = True
            document.close()
            return report

    for page in document:
        if keep_pages is not None and page.number not in keep_pages:
            continue
        if not page.get_text("words") and page.get_images():
            report["image_only_pages"] += 1

        rows, decisions = analyse_page(page, options)

        page_rects = []
        row_rects = []
        survivor_rects = []
        for row, destroy_row in zip(rows, decisions):
            if destroy_row:
                row_rects.append(row_rect(row))
                killed_rows.append(row_text(row))
                report["rows"] += 1
                continue
            if row_text(row).strip():
                survivor_rects.append(row_rect(row))

            if options.row_mode and options.anchor_re.search(row_text(row)):
                report["kept"].append(row_text(row))

            compact, owner = compact_row(row)
            if not compact:
                continue
            spans = find_spans(compact, targets, last4s,
                               options.auto, options.also_masked)
            rects = spans_to_rects(row, owner, spans, options.pad)
            page_rects.extend(rects)
            report["hits"] += len(rects)

        # Give every erased row the same width. Boxes cut to each row's own
        # text would advertise how long each description was. Widening comes
        # before trimming, because a wider box may newly overlap a kept row.
        if row_rects:
            if options.row_box == "uniform":
                left = min(rect.x0 for rect in row_rects)
                right = max(rect.x1 for rect in row_rects)
                row_rects = [fitz.Rect(left, rect.y0, right, rect.y1)
                             for rect in row_rects]
            for rect in row_rects:
                # Pad sideways only: a row box already spans the full height
                # of its text, and padding it vertically is what makes it
                # encroach on its neighbours.
                widened = rect + (-options.pad, 0, options.pad, 0)
                trimmed = clip_clear_of(widened, survivor_rects,
                                        options.row_margin)
                if trimmed is not None:
                    page_rects.append(trimmed)

        if page_rects:
            report["pages_with_hits"] += 1
            if options.verbose:
                print(f"    page {page.number + 1}: "
                      f"{len(page_rects)} area(s) redacted")
            if not options.dry_run:
                for rect in page_rects:
                    page.add_redact_annot(rect, fill=options.fill_rgb)
                apply_redactions(page)
                # Drawn after the redactions are burnt in, so the outline
                # survives rather than being swallowed by them.
                if options.outline:
                    for rect in page_rects:
                        page.draw_rect(rect, color=OUTLINE_COLOR,
                                       width=OUTLINE_WIDTH)

    if options.dry_run:
        document.close()
        return report

    if keep_pages is not None:
        document.select(sorted(keep_pages))

    if options.scrub_metadata:
        document.set_metadata({})
        try:
            document.del_xml_metadata()
        except Exception:  # pragma: no cover - older PyMuPDF
            pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(out_path, garbage=4, deflate=True, clean=True)
    document.close()

    report["leftover"] = verify(out_path, targets, killed_rows, options.auto)
    return report


def verify(path: Path, targets: list[str], killed_rows: list[str],
           auto: bool) -> list[str]:
    """Re-open a written PDF and confirm the sensitive content is really gone.

    Three checks run against the output: the literal card numbers, any
    Luhn-valid card-length run (when ``--auto`` was used), and the text of
    every row that was supposed to be destroyed. The first two re-use the same
    row grouping and gap-aware compaction as the redaction pass, so the check
    sees the page exactly as the matcher did.

    Args:
        path: The redacted PDF.
        targets: Literal digit strings that must no longer be extractable.
        killed_rows: Text of rows that were redacted.
        auto: Whether to re-check for Luhn-valid card-length runs.

    Returns:
        A list of descriptions of what survived (empty is good).
    """
    document = fitz.open(path)
    leftover: list[str] = []
    text_parts: list[str] = []

    for page in document:
        text_parts.append(page.get_text())
        for row in group_rows(page.get_text("words")):
            compact, _ = compact_row(row)
            leftover.extend(target for target in targets
                            if target and target in compact)
            if auto:
                leftover.extend(compact[start:end]
                                for start, end in luhn_windows(compact))

    document.close()
    folded = alnum_only("".join(text_parts))
    for row in killed_rows:
        needle = alnum_only(row)
        if len(needle) >= 12 and needle in folded:
            leftover.append(f"row {row[:40]!r}")

    return sorted(set(leftover))


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

class OrderedKeepAction(argparse.Action):
    """Record ``--keep-row`` and ``--keep-date`` in the order they were typed.

    ``argparse``'s ``append`` collects each option into its own list, which
    loses the relation between them. Each ``--keep-date`` belongs to the
    ``--keep-row`` it follows, so both options are also appended to a single
    ordered sequence that :func:`build_keep_rules` can walk.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        sequence = getattr(namespace, "keep_sequence", None)
        if sequence is None:
            sequence = []
            setattr(namespace, "keep_sequence", sequence)
        sequence.append((self.dest, values))
        # Rebuild rather than mutate, so the parser's default list is never
        # shared between runs.
        current = list(getattr(namespace, self.dest, None) or [])
        setattr(namespace, self.dest, current + [values])


def build_keep_rules(sequence: list[tuple[str, str]],
                     order: str) -> tuple[list[dict], str | None]:
    """Turn the typed sequence of keep options into a list of rules.

    Each ``--keep-row`` opens a rule, and every ``--keep-date`` that follows it
    narrows that rule alone. A row survives if it satisfies *any* rule, so::

        --keep-row acme --keep-date 03/14/26 --keep-row globex

    keeps the Acme charge of 26 September and every Globex charge,
    whatever its date.

    Args:
        sequence: ``(dest, value)`` pairs in command-line order.
        order: Date order, ``"mdy"`` or ``"dmy"``.

    Returns:
        A ``(rules, error)`` pair. Each rule is a dict with ``term`` (a
        lowercase substring, or None to match any row) and ``dates`` (a set of
        ``(year, month, day)`` triples, empty to match any date). *error* is a
        message when a date could not be read, otherwise None.
    """
    rules: list[dict] = []

    for dest, value in sequence:
        if dest == "keep_row":
            rules.append({"term": value.lower(), "dates": set()})
            continue

        parsed = set()
        for piece in value.split(","):
            piece = piece.strip()
            if not piece:
                continue
            date = parse_date(piece, order)
            if date is None:
                return [], (f"cannot read the date {piece!r}. Use a form like "
                            f"03/14/26 or 2026-03-14.")
            parsed.add(date)

        if not rules:
            # Dates given before any --keep-row stand on their own: keep any
            # transaction falling on them, whatever its description.
            rules.append({"term": None, "dates": set()})
        rules[-1]["dates"] |= parsed

    return rules, None


def row_survives(text: str, rules: list[dict], order: str) -> bool:
    """Return True if a transaction row satisfies at least one keep rule.

    Args:
        text: The row's text.
        rules: Output of :func:`build_keep_rules`.
        order: Date order, ``"mdy"`` or ``"dmy"``.

    Returns:
        True if the row should be left standing.
    """
    lowered = text.lower()
    date = None

    for rule in rules:
        if rule["term"] is not None and rule["term"] not in lowered:
            continue
        if rule["dates"]:
            if date is None:
                date = parse_date(text, order) or False
            if date is False or date not in rule["dates"]:
                continue
        return True

    return False


def collect_pdfs(inputs: list[str], recursive: bool) -> list[Path]:
    """Expand files and folders on the command line into a list of PDFs."""
    pattern = "**/*.pdf" if recursive else "*.pdf"
    found: list[Path] = []
    for item in inputs:
        path = Path(item).expanduser()
        if path.is_dir():
            found.extend(sorted(p for p in path.glob(pattern) if p.is_file()))
        elif path.is_file():
            found.append(path)
        else:
            print(f"warning: no such file or folder: {path}", file=sys.stderr)
    return found


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        description="Permanently erase card numbers and transaction rows "
                    "from PDF bills.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python3 redact_cc.py ~/bills --auto --also-masked \\\n"
               "      --keep-row acme --keep-row globex \\\n"
               "      --redact-line 'PREVIOUS BALANCE|SUBTOTAL'",
    )

    parser.add_argument("inputs", nargs="+",
                        help="PDF files and/or folders containing PDFs")

    numbers = parser.add_argument_group("card numbers")
    numbers.add_argument("-n", "--number", action="append", default=[],
                         help="card number to erase; spaces and dashes are "
                              "ignored. Repeat for several cards.")
    numbers.add_argument("--extra", action="append", default=[],
                         help="any other digit string to erase (e.g. account "
                              "number). Repeat as needed.")
    numbers.add_argument("--auto", action="store_true",
                         help="also detect unknown 13-19 digit, Luhn-valid "
                              "numbers automatically")
    numbers.add_argument("--also-masked", action="store_true",
                         help="also erase partly masked displays such as "
                              "'************3456'")

    rows = parser.add_argument_group("transaction rows")
    rows.add_argument("--keep-row", action=OrderedKeepAction, default=[],
                      metavar="TEXT",
                      help="erase every transaction row EXCEPT those whose "
                           "text contains TEXT (case-insensitive). Repeat for "
                           "several merchants. Implies --redact-rows.")
    rows.add_argument("--redact-rows", action="store_true",
                      help="erase every transaction row, keeping only those "
                           "named by --keep-row")
    rows.add_argument("--keep-date", action=OrderedKeepAction, default=[],
                      metavar="DATES",
                      help="narrow the PRECEDING --keep-row to these "
                           "transaction dates (e.g. 03/14/26, or 2026-03-14; "
                           "comma-separate or repeat). It applies to that one "
                           "term only, so a later --keep-row is unrestricted "
                           "until given dates of its own. Before any "
                           "--keep-row it stands alone, keeping any "
                           "transaction on those dates.")
    rows.add_argument("--date-order", choices=["mdy", "dmy"], default="mdy",
                      help="how to read an ambiguous date like 03/07/26: "
                           "'mdy' is 7 September, 'dmy' is 9 July "
                           "(default: %(default)s)")
    rows.add_argument("--redact-line", action="append", default=[],
                      metavar="REGEX",
                      help="also erase any row matching REGEX, whether or not "
                           "it is a transaction (e.g. 'PREVIOUS BALANCE')")
    rows.add_argument("--row-anchor", default=DEFAULT_ROW_ANCHOR,
                      metavar="REGEX",
                      help="regex identifying the start of a transaction row "
                           "(default: a leading date such as 03/07/26)")
    rows.add_argument("--row-gap", type=float, default=0.9, metavar="N",
                      help="a row sitting within (1 + N) row-heights below a "
                           "transaction is treated as its continuation; raise "
                           "it for loosely spaced bills (default: %(default)s)")
    rows.add_argument("--row-box", choices=["uniform", "tight"],
                      default="uniform",
                      help="'uniform' gives every erased row the same width so "
                           "box widths reveal nothing; 'tight' fits each row "
                           "(default: %(default)s)")

    pages = parser.add_argument_group("pages")
    pages.add_argument("--drop-empty-pages", action="store_true",
                       help="delete every page on which no transaction row "
                            "survived. Judges a page by the rows actually "
                            "kept, so a merchant named in a summary line "
                            "cannot rescue a page with no matching "
                            "transaction. Needs --keep-row or --keep-date.")
    pages.add_argument("--keep-page", action="append", default=[],
                       metavar="TEXT",
                       help="delete every page whose text does NOT contain "
                            "TEXT anywhere (case-insensitive). Repeat for "
                            "several terms. Looser than --drop-empty-pages; "
                            "both may be given, and a page must then satisfy "
                            "both.")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--out-dir", default=None,
                        help="output folder (default: 'redacted' beside each "
                             "input file)")
    output.add_argument("--suffix", default="-redacted",
                        help="suffix added to output filenames "
                             "(default: %(default)s)")
    output.add_argument("--fill",
                        choices=["white", "outline", "black", "none"],
                        default="white",
                        help="what the erased area looks like: 'white' leaves "
                             "blank paper, 'outline' adds a thin grey border "
                             "so the gap is visibly a redaction, 'black' is "
                             "the ink-heavy classic (default: %(default)s)")
    output.add_argument("--pad", type=float, default=1.0,
                        help="points of padding at each side of a match "
                             "(default: %(default)s)")
    output.add_argument("--row-margin", type=float, default=1.5, metavar="N",
                        help="points of clearance left between an erased row "
                             "and a surviving one (default: %(default)s)")
    output.add_argument("--scrub-metadata", action="store_true",
                        help="also wipe document metadata from the output")
    output.add_argument("-r", "--recursive", action="store_true",
                        help="search folders recursively")
    output.add_argument("--dry-run", action="store_true",
                        help="report matches without writing any file")
    output.add_argument("-v", "--verbose", action="store_true",
                        help="print per-page counts and the rows kept")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a shell exit status."""
    options = build_parser().parse_args(argv)

    # Normalise every card target down to bare digits.
    targets = [digits_only(value) for value in options.number + options.extra]
    targets = [value for value in targets if value]
    last4s = [value[-4:] for value in targets if len(value) >= 4]

    # Row-mode settings.
    options.drop_patterns = [re.compile(pattern, re.IGNORECASE)
                             for pattern in options.redact_line]
    options.anchor_re = re.compile(options.row_anchor)

    # Pair each --keep-date with the --keep-row it followed.
    options.keep_rules, error = build_keep_rules(
        getattr(options, "keep_sequence", []), options.date_order)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    options.row_mode = bool(options.redact_rows or options.keep_rules
                            or options.redact_line)
    options.page_terms = [term.lower() for term in options.keep_page]

    if options.drop_empty_pages and not options.keep_rules:
        print("error: --drop-empty-pages needs --keep-row or --keep-date, "
              "otherwise no row survives anywhere.", file=sys.stderr)
        return 2

    if not targets and not options.auto and not options.also_masked \
            and not options.row_mode and not options.page_terms:
        print("error: nothing to redact. Give --number, --auto, or "
              "--keep-row/--redact-rows.", file=sys.stderr)
        return 2

    options.fill_rgb = {
        "white": (1, 1, 1),
        "outline": (1, 1, 1),
        "black": (0, 0, 0),
        "none": None,
    }[options.fill]
    options.outline = options.fill == "outline"

    pdfs = collect_pdfs(options.inputs, options.recursive)
    if not pdfs:
        print("error: no PDF files found.", file=sys.stderr)
        return 1

    total_hits, total_rows, total_dropped = 0, 0, 0
    failures, untouched = 0, []

    for pdf in pdfs:
        print(f"[{pdf.name}]")
        out_dir = Path(options.out_dir).expanduser() if options.out_dir \
            else pdf.parent / "redacted"
        out_path = out_dir / f"{pdf.stem}{options.suffix}.pdf"

        try:
            report = redact_pdf(pdf, out_path, targets, last4s, options)
        except Exception as error:  # keep the batch going
            print(f"    FAILED: {error}", file=sys.stderr)
            failures += 1
            continue

        total_hits += report["hits"]
        total_rows += report["rows"]
        total_dropped += report["pages_dropped"]

        if report["no_pages_left"]:
            print("    no page survives the page filters - nothing would be "
                  "left, so this file was skipped")
            untouched.append(pdf.name)
            continue

        if report["pages_dropped"]:
            print(f"    {report['pages_dropped']} of {report['pages']} "
                  f"page(s) dropped; {report['pages'] - report['pages_dropped']}"
                  f" kept")

        if report["hits"] == 0 and report["rows"] == 0 \
                and report["pages_dropped"] == 0:
            untouched.append(pdf.name)
            if report["image_only_pages"]:
                print(f"    no text layer on {report['image_only_pages']} "
                      f"page(s) - this looks scanned; run OCR first "
                      f"(ocrmypdf) and try again")
            else:
                print("    no match found - check the number and the "
                      "--keep-row terms against the bill")
            continue

        print(f"    {report['hits']} card match(es), "
              f"{report['rows']} row(s) erased, on "
              f"{report['pages_with_hits']}/{report['pages']} page(s)")

        if options.row_mode:
            print(f"    {len(report['kept'])} transaction row(s) kept")
            if options.verbose:
                for row in report["kept"]:
                    print(f"        kept: {row}")

        if options.dry_run:
            print("    dry run - nothing written")
            continue

        if report["leftover"]:
            print("    WARNING: still extractable after redaction: "
                  f"{', '.join(report['leftover'])}", file=sys.stderr)
            failures += 1
        else:
            print(f"    verified clean -> {out_path}")

    print(f"\n{len(pdfs)} file(s), {total_hits} card redaction(s), "
          f"{total_rows} row(s) erased, {total_dropped} page(s) dropped.")
    if untouched:
        print(f"No matches in: {', '.join(untouched)}")
    if failures:
        print(f"{failures} file(s) need attention.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
