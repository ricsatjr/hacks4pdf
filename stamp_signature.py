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
"""Stamp a signature image onto PDF pages, positioned by the text around it.

This tool finds a reference string on each page - usually the signer's printed
name - and draws a signature image above it. Placement is derived from the
page's own text rather than from fixed coordinates, so a signature block that
sits at a different height on every page needs no per-page tuning, and a page
with no match is left alone.

How a placement is decided:

1. **The lower anchor** - every match for ``--text-anchor-below`` is located;
   ``--occurrence`` decides whether to use the first, the last, or all of them.
2. **The upper anchor** - optional. For each lower anchor the script looks for
   a ``--text-anchor-above`` match (such as ``Prepared by:``) whose bottom edge
   lies above it and within ``--max-gap``. Candidates are scored by vertical
   distance plus a quarter of the horizontal centre offset, so an anchor in the
   same column beats an equally close one in another. With no upper anchor the
   signature rests directly on the lower one.
3. **Size** - ``--size`` is a percentage of whatever ``--size-of`` names: the
   gap between the anchors, the width of the lower anchor's text, or the page
   width. The image's own aspect ratio supplies the other dimension. An
   absolute ``--width`` overrides all of that.
4. **Position** - ``--valign`` places it within the gap, ``--align`` and
   ``--align-to`` set the horizontal edge, then ``--offset-x`` and
   ``--offset-y`` apply as final nudges.

Rotated text is handled. Where the glyphs themselves are written sideways - a
sheet scanned in landscape, a page assembled by a tool that rotated its
content - the geometry above is computed in a derotated frame and the result
is mapped back, so the signature follows the text rather than the paper. Note
that this is a property of the text, not of the page: a page whose ``/Rotate``
is 0 can still carry sideways text, and a rotated page can carry upright text.

Lengths are PDF points (1 pt = 1/72 inch) unless ``--units px`` is given, in
which case ``--dpi`` converts them. The y axis increases downward, so a
positive ``--offset-y`` moves the signature *down*.

Typical use
-----------
    # Preview placements without writing anything
    python3 stamp_signature.py memo.pdf -s sig.png \\
        -a "Prepared by:" -b "Juan dela Cruz" --dry-run

    # Write it, output beside the input in a 'signed' folder
    python3 stamp_signature.py memo.pdf -s sig.png \\
        -a "Prepared by:" -b "Juan dela Cruz"

    # A whole folder of forms, one fixed signature width throughout
    python3 stamp_signature.py ~/forms -s sig.png \\
        -a "Prepared by:" -b "Juan dela Cruz" --width 140

    # Several signature blocks on one page, centred over each name
    python3 stamp_signature.py minutes.pdf -s sig.png \\
        -a "Noted by:" -b "Juan dela Cruz" --occurrence all --align center

Requirements
------------
    uv run stamp_signature.py ...  # dependencies come from the header above

or, without uv::

    pip install 'pymupdf<2'

Limitations
-----------
* Only the *text layer* is searched. A scanned page has none; the script
  reports that and you will need OCR first (e.g. ``ocrmypdf in.pdf out.pdf``).
* The search is literal. Text broken across two lines, hyphenated, or set with
  unusual ligatures will not match. A shorter distinctive fragment - a surname
  alone - is often the fix.
* Text skewed to an angle that is not a multiple of 90 degrees is placed
  unrotated, with a warning. A crooked scan is the usual cause; deskew it
  first.
* This produces a *visual* stamp, not a cryptographic digital signature. The
  output carries an image, not an identity: anyone can extract and reuse it,
  and nothing in the file proves who added it or detects a later edit. Where
  that matters, sign the stamped file afterwards with a certificate, for
  example using ``pyhanko``.

Part of hacks4pdf. Licensed under the GNU GPL v3 or later; see the header
above and the LICENSE file. Note that PyMuPDF, which this tool depends on, is
itself dual-licensed under the AGPL v3 or an Artifex commercial licence.

Written with AI assistance (Claude, Anthropic) to the author's requirements
and under the author's review, and tested against synthetic documents rather
than real ones. See the AI use section of the README. Check what this tool
produces before you send it to anybody.
"""

from __future__ import annotations

import argparse
import math
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

#: Points per inch, for the --units px conversion.
POINTS_PER_INCH = 72.0

#: How far a measured text angle may sit from a multiple of 90 degrees and
#: still be treated as that multiple. Beyond this the text is genuinely
#: skewed, which the image rotation cannot express.
ANGLE_TOLERANCE = 2.0

#: Weight given to horizontal separation when pairing an upper anchor with a
#: lower one. Below 1.0 so that vertical distance dominates, but high enough
#: that an anchor in another column loses to one in the same column.
COLUMN_PENALTY = 0.25


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def to_points(value: float, units: str, dpi: float) -> float:
    """Convert a length to PDF points.

    Args:
        value: The length to convert.
        units: Either ``"pt"``, returned unchanged, or ``"px"``.
        dpi: Pixels per inch, used only for the ``"px"`` conversion.

    Returns:
        The length in PDF points.
    """
    if units == "pt":
        return value
    return value * POINTS_PER_INCH / dpi


def image_aspect_ratio(path: Path) -> float:
    """Return an image's height divided by its width.

    Args:
        path: Image file to measure.

    Returns:
        The aspect ratio as height / width.

    Raises:
        ValueError: If the image reports zero width.
    """
    pixmap = fitz.Pixmap(str(path))
    if pixmap.width == 0:
        raise ValueError(f"image has zero width: {path}")
    return pixmap.height / pixmap.width


def select_matches(rects: list, occurrence: str) -> list:
    """Narrow one page's matches according to ``--occurrence``.

    Args:
        rects: Matches for the lower anchor, in page order.
        occurrence: One of ``"first"``, ``"last"`` or ``"all"``.

    Returns:
        The subset to place a signature against.
    """
    if not rects:
        return []
    if occurrence == "first":
        return [rects[0]]
    if occurrence == "last":
        return [rects[-1]]
    return rects


# ---------------------------------------------------------------------------
# Text orientation
# ---------------------------------------------------------------------------

def text_angle(page, rect) -> tuple[float, bool]:
    """Measure the writing direction of the text under a rectangle.

    PyMuPDF reports a per-line direction vector, which is what distinguishes
    sideways *glyphs* from a merely rotated *page*. The line overlapping the
    rectangle most is the one that counts, so a rotated stamp elsewhere on an
    otherwise upright page does not mislead the result.

    Args:
        page: The page to inspect.
        rect: Bounding box of a matched anchor.

    Returns:
        A ``(angle, exact)`` pair. *angle* is degrees clockwise from
        horizontal, snapped to a multiple of 90. *exact* is False when the
        text was skewed beyond :data:`ANGLE_TOLERANCE` and the angle had to be
        forced to zero, which the caller should report.
    """
    direction, best_area = None, 0.0

    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            overlap = fitz.Rect(line["bbox"]) & rect
            area = abs(overlap) if not overlap.is_empty else 0.0
            if area > best_area:
                best_area, direction = area, line["dir"]

    if direction is None:
        return 0.0, True

    cosine, sine = direction
    measured = math.degrees(math.atan2(sine, cosine)) % 360.0
    snapped = round(measured / 90.0) * 90.0 % 360.0

    if abs((measured - snapped + 180.0) % 360.0 - 180.0) > ANGLE_TOLERANCE:
        return 0.0, False
    return snapped, True


def flatten_matrix(angle: float) -> tuple["fitz.Matrix", "fitz.Matrix"]:
    """Build the pair of matrices that move in and out of upright text space.

    Args:
        angle: Text angle as returned by :func:`text_angle`.

    Returns:
        A ``(forward, back)`` pair. *forward* maps page coordinates into a
        frame where the text reads left to right, so the placement rules can
        assume upright text; *back* returns a result to page coordinates.
    """
    forward = fitz.Matrix(1, 0, 0, 1, 0, 0).prerotate(-angle)
    back = fitz.Matrix(1, 0, 0, 1, 0, 0).prerotate(angle)
    return forward, back


def flatten(rect, matrix):
    """Map a rectangle through a matrix and restore x0 < x1, y0 < y1.

    Args:
        rect: The rectangle to transform.
        matrix: One of the matrices from :func:`flatten_matrix`.

    Returns:
        The transformed, normalised rectangle.
    """
    moved = fitz.Rect(rect) * matrix
    moved.normalize()
    return moved


# ---------------------------------------------------------------------------
# Anchor pairing
# ---------------------------------------------------------------------------

def nearest_above(below_rect, above_rects: list, max_gap: float):
    """Find the upper anchor sitting directly above a lower one.

    Candidates are upper anchors whose bottom edge lies above the top of the
    lower anchor and within *max_gap*. The winner minimises the vertical
    distance plus :data:`COLUMN_PENALTY` times the horizontal centre offset,
    so a nearby anchor in the same column beats an equally close one in
    another.

    Args:
        below_rect: Bounding box of the matched lower anchor, upright frame.
        above_rects: Every upper anchor match on the page, upright frame.
        max_gap: Largest acceptable vertical distance, in points.

    Returns:
        The paired upper anchor's rectangle, or None if nothing qualifies.
    """
    below_centre = (below_rect.x0 + below_rect.x1) / 2.0
    candidates = []

    for above in above_rects:
        gap = below_rect.y0 - above.y1
        if 0 <= gap <= max_gap:
            above_centre = (above.x0 + above.x1) / 2.0
            score = gap + COLUMN_PENALTY * abs(above_centre - below_centre)
            candidates.append((score, above))

    if not candidates:
        return None
    return min(candidates, key=lambda pair: pair[0])[1]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def signature_size(aspect: float, options: argparse.Namespace,
                   below_rect, page_width: float,
                   gap: float | None) -> tuple[float, float]:
    """Decide the signature's width and height, in points.

    An absolute ``--width`` wins outright. Otherwise ``--size`` is read as a
    percentage of whichever reference ``--size-of`` names, and the image's
    aspect ratio supplies the other dimension. Sizing against the gap needs a
    paired upper anchor; without one it falls back to the lower anchor's
    width, which is the only local measure left.

    Args:
        aspect: Height divided by width of the signature image.
        options: Parsed command-line options.
        below_rect: Bounding box of the lower anchor, upright frame.
        page_width: Page width in the upright frame, in points.
        gap: Vertical space between the anchors, or None when unpaired.

    Returns:
        A ``(width, height)`` pair in points.
    """
    if options.width is not None:
        width = to_points(options.width, options.units, options.dpi)
        return width, width * aspect

    fraction = options.size / 100.0

    if options.size_of == "gap" and gap:
        height = gap * fraction
        return height / aspect, height

    if options.size_of == "page":
        width = page_width * fraction
    else:  # "below", and the fallback when no gap is available
        width = below_rect.width * fraction

    return width, width * aspect


def placement_rect(below_rect, above_rect, sig_width: float,
                   sig_height: float, options: argparse.Namespace):
    """Build the rectangle the signature should occupy, in the upright frame.

    Vertical position comes from ``--valign`` within the gap between the
    anchors, or from the lower anchor alone when there is no upper one.
    Horizontal position comes from ``--align`` against whichever box
    ``--align-to`` names. The two fine-tuning offsets are applied last, to
    whatever the rules produced.

    Args:
        below_rect: Bounding box of the lower anchor, upright frame.
        above_rect: Bounding box of the paired upper anchor, or None.
        sig_width: Signature width in points.
        sig_height: Signature height in points.
        options: Parsed command-line options.

    Returns:
        The target rectangle in the upright frame.
    """
    if above_rect is None or options.valign == "bottom":
        bottom = below_rect.y0
    elif options.valign == "top":
        bottom = above_rect.y1 + sig_height
    else:  # "center"
        centre = (above_rect.y1 + below_rect.y0) / 2.0
        bottom = centre + sig_height / 2.0

    top = bottom - sig_height

    reference = below_rect if (options.align_to == "below"
                               or above_rect is None) else above_rect

    if options.align == "left":
        left = reference.x0
    elif options.align == "center":
        left = (reference.x0 + reference.x1) / 2.0 - sig_width / 2.0
    else:  # "right"
        left = reference.x1 - sig_width

    # Fine tuning. Positive y moves down the page.
    dx = to_points(options.offset_x, options.units, options.dpi)
    dy = to_points(options.offset_y, options.units, options.dpi)

    return fitz.Rect(left + dx, top + dy,
                     left + dx + sig_width, bottom + dy)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def stamp_page(page, aspect: float, options: argparse.Namespace) -> dict:
    """Place every signature due on one page.

    Args:
        page: The page to stamp. Modified in place unless ``--dry-run``.
        aspect: Height divided by width of the signature image.
        options: Parsed command-line options.

    Returns:
        A report dict with keys ``placed`` (int), ``lines`` (descriptions of
        each placement), ``warnings`` (text of each warning raised) and
        ``has_text`` (False when the page carries no text layer at all).
    """
    report = {"placed": 0, "lines": [], "warnings": [],
              "has_text": bool(page.get_text("text").strip())}

    below_hits = select_matches(page.search_for(options.text_anchor_below),
                                options.occurrence)
    if not below_hits:
        return report

    above_hits = (page.search_for(options.text_anchor_above)
                  if options.text_anchor_above else [])
    max_gap = to_points(options.max_gap, options.units, options.dpi)

    for below_hit in below_hits:
        angle, exact = text_angle(page, below_hit)
        if not exact:
            report["warnings"].append(
                "text is skewed off the 90-degree grid; signature placed "
                "unrotated. Deskew the scan first.")
        forward, back = flatten_matrix(angle)

        below_rect = flatten(below_hit, forward)
        above_rects = [flatten(hit, forward) for hit in above_hits]
        page_rect = flatten(page.rect, forward)

        above_rect = (nearest_above(below_rect, above_rects, max_gap)
                      if above_rects else None)
        gap = (below_rect.y0 - above_rect.y1) if above_rect else None

        sig_width, sig_height = signature_size(
            aspect, options, below_rect, page_rect.width, gap)
        flat_rect = placement_rect(below_rect, above_rect,
                                   sig_width, sig_height, options)

        if options.text_anchor_above and above_rect is None:
            report["warnings"].append(
                f"no {options.text_anchor_above!r} found within "
                f"{max_gap:.0f} pt above the name; placed relative to the "
                f"name alone")
        if gap is not None and sig_height > gap:
            report["warnings"].append(
                "signature is taller than the gap and will overlap "
                "neighbouring text; lower --size or use --size-of below")
        if not page_rect.contains(flat_rect):
            report["warnings"].append(
                "signature extends past the page edge")

        where = f"gap {gap:.1f} pt" if gap is not None else "unpaired"
        turned = f", text at {angle:.0f} deg" if angle else ""
        size = f"{flat_rect.width:.1f}x{flat_rect.height:.1f} pt"
        report["lines"].append(f"{where}{turned}; {size}")

        if not options.dry_run:
            page.insert_image(
                flatten(flat_rect, back),
                filename=str(options.signature),
                overlay=True,
                keep_proportion=True,
                rotate=int((360 - angle) % 360),
            )
        report["placed"] += 1

    return report


def stamp_pdf(path: Path, out_path: Path, aspect: float,
              options: argparse.Namespace) -> dict:
    """Stamp one PDF and write the result, unless nothing matched.

    A file with no match is never written. The alternative - saving an
    untouched copy - produces an output that looks signed in a listing but is
    not, which is the more dangerous of the two failures.

    Args:
        path: Source PDF.
        out_path: Destination PDF, not written on ``--dry-run`` or no match.
        aspect: Height divided by width of the signature image.
        options: Parsed command-line options.

    Returns:
        A report dict with keys ``placed``, ``pages``, ``pages_hit``,
        ``image_only_pages``, ``warnings`` and ``written``.
    """
    document = fitz.open(path)
    report = {"placed": 0, "pages": document.page_count, "pages_hit": 0,
              "image_only_pages": 0, "warnings": [], "written": False}

    for number, page in enumerate(document, start=1):
        page_report = stamp_page(page, aspect, options)

        if not page_report["has_text"]:
            report["image_only_pages"] += 1

        if page_report["placed"]:
            report["pages_hit"] += 1
            report["placed"] += page_report["placed"]

        if options.verbose:
            for line in page_report["lines"]:
                print(f"    page {number:>3}: {line}")
            if not page_report["lines"]:
                print(f"    page {number:>3}: no match")

        for warning in page_report["warnings"]:
            report["warnings"].append(f"page {number}: {warning}")

    if report["placed"] and not options.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(out_path), garbage=3, deflate=True)
        report["written"] = True

    document.close()
    return report


def collect_pdfs(inputs: list[str], recursive: bool) -> list[Path]:
    """Expand files and folders on the command line into a list of PDFs.

    Args:
        inputs: Paths as given on the command line.
        recursive: Whether to descend into subfolders.

    Returns:
        Every PDF found, folders expanded and sorted.
    """
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


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        description="Stamp a signature image onto PDF pages, positioned by "
                    "the text around it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python3 stamp_signature.py ~/forms -s sig.png \\\n"
               "      -a 'Prepared by:' -b 'Juan dela Cruz' --dry-run",
    )

    parser.add_argument("inputs", nargs="+",
                        help="PDF files and/or folders containing PDFs")

    anchors = parser.add_argument_group("signature and anchors")
    anchors.add_argument("-s", "--signature", type=Path, required=True,
                         metavar="IMAGE",
                         help="signature image; a PNG with a transparent "
                              "background works best")
    anchors.add_argument("-b", "--text-anchor-below", required=True,
                         metavar="TEXT",
                         help="text the signature sits above, normally the "
                              "signer's printed name")
    anchors.add_argument("-a", "--text-anchor-above", default=None,
                         metavar="TEXT",
                         help="text the signature sits below, e.g. "
                              "'Prepared by:'. Optional; without it the "
                              "signature rests on the name alone.")
    anchors.add_argument("--occurrence", choices=["first", "last", "all"],
                         default="first",
                         help="which match to use when the name appears "
                              "several times on a page "
                              "(default: %(default)s)")

    sizing = parser.add_argument_group("sizing")
    sizing.add_argument("--size", type=float, default=85.0, metavar="PERCENT",
                        help="signature size as a percentage of the "
                             "--size-of reference (default: %(default)s)")
    sizing.add_argument("--size-of", choices=["gap", "below", "page"],
                        default="gap",
                        help="what --size is a percentage of: the height of "
                             "the gap between anchors, the width of the name, "
                             "or the page width (default: %(default)s)")
    sizing.add_argument("--width", type=float, default=None,
                        help="absolute signature width; overrides --size")

    position = parser.add_argument_group("positioning")
    position.add_argument("--align", choices=["left", "center", "right"],
                          default="left",
                          help="horizontal alignment against the --align-to "
                               "box (default: %(default)s)")
    position.add_argument("--align-to", choices=["below", "above"],
                          default="below",
                          help="which anchor --align works from "
                               "(default: %(default)s)")
    position.add_argument("--valign", choices=["bottom", "center", "top"],
                          default="bottom",
                          help="where the signature sits in the gap: resting "
                               "on the name, floating midway, or tucked under "
                               "the upper anchor (default: %(default)s)")
    position.add_argument("--offset-x", type=float, default=0.0, metavar="N",
                          help="horizontal nudge; negative moves left "
                               "(default: %(default)s)")
    position.add_argument("--offset-y", type=float, default=0.0, metavar="N",
                          help="vertical nudge; positive moves DOWN "
                               "(default: %(default)s)")
    position.add_argument("--max-gap", type=float, default=150.0, metavar="N",
                          help="largest anchor separation still treated as a "
                               "pair (default: %(default)s)")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--out-dir", default=None,
                        help="output folder (default: 'signed' beside each "
                             "input file)")
    output.add_argument("--suffix", default="-signed",
                        help="suffix added to output filenames "
                             "(default: %(default)s)")
    output.add_argument("--units", choices=["pt", "px"], default="pt",
                        help="units for --offset-x, --offset-y, --width and "
                             "--max-gap (default: %(default)s)")
    output.add_argument("--dpi", type=float, default=96.0,
                        help="pixels per inch, used only with --units px "
                             "(default: %(default)s)")
    output.add_argument("-r", "--recursive", action="store_true",
                        help="search folders recursively")
    output.add_argument("--dry-run", action="store_true",
                        help="report placements without writing any file")
    output.add_argument("-v", "--verbose", action="store_true",
                        help="print a line per page")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a shell exit status."""
    options = build_parser().parse_args(argv)

    if not options.signature.is_file():
        print(f"error: signature image not found: {options.signature}",
              file=sys.stderr)
        return 2
    if not options.text_anchor_below.strip():
        print("error: --text-anchor-below cannot be blank.", file=sys.stderr)
        return 2
    if options.text_anchor_above is not None \
            and not options.text_anchor_above.strip():
        print("error: --text-anchor-above cannot be blank; omit it instead.",
              file=sys.stderr)
        return 2
    if options.size <= 0:
        print("error: --size must be greater than zero.", file=sys.stderr)
        return 2
    if options.width is not None and options.width <= 0:
        print("error: --width must be greater than zero.", file=sys.stderr)
        return 2
    if options.max_gap < 0:
        print("error: --max-gap cannot be negative.", file=sys.stderr)
        return 2
    if options.dpi <= 0:
        print("error: --dpi must be greater than zero.", file=sys.stderr)
        return 2

    try:
        aspect = image_aspect_ratio(options.signature)
    except Exception as error:
        print(f"error: cannot read the signature image: {error}",
              file=sys.stderr)
        return 2

    pdfs = collect_pdfs(options.inputs, options.recursive)
    if not pdfs:
        print("error: no PDF files found.", file=sys.stderr)
        return 1

    total_placed, failures, unmatched = 0, 0, []

    for pdf in pdfs:
        print(f"[{pdf.name}]")
        out_dir = Path(options.out_dir).expanduser() if options.out_dir \
            else pdf.parent / "signed"
        out_path = out_dir / f"{pdf.stem}{options.suffix}.pdf"

        try:
            report = stamp_pdf(pdf, out_path, aspect, options)
        except Exception as error:  # keep the batch going
            print(f"    FAILED: {error}", file=sys.stderr)
            failures += 1
            continue

        total_placed += report["placed"]

        for warning in report["warnings"]:
            print(f"    WARNING {warning}", file=sys.stderr)

        if report["placed"] == 0:
            unmatched.append(pdf.name)
            if report["image_only_pages"] == report["pages"]:
                print(f"    no text layer on any of {report['pages']} "
                      f"page(s) - this looks scanned; run OCR first "
                      f"(ocrmypdf) and try again")
            else:
                print(f"    no match for {options.text_anchor_below!r} - "
                      f"check spelling, and note that text split across two "
                      f"lines will not match")
            continue

        print(f"    {report['placed']} signature(s) on "
              f"{report['pages_hit']}/{report['pages']} page(s)")

        if options.dry_run:
            print("    dry run - nothing written")
        else:
            print(f"    wrote {out_path}")

    print(f"\n{len(pdfs)} file(s), {total_placed} signature(s) placed.")
    if unmatched:
        print(f"No matches in: {', '.join(unmatched)}")
    if failures:
        print(f"{failures} file(s) need attention.", file=sys.stderr)
        return 1
    if total_placed == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
