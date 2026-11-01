# hacks4pdf

Small, self-contained command-line tools for working with PDF files.

Each tool is a single Python file that stands on its own: it declares the
packages it needs in its own header, so one tool's dependencies can never
collide with another's. Adding a tool is adding one file.

## Running a tool

Install [uv](https://docs.astral.sh/uv/) once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
```

Then run any tool in this repository directly:

```bash
uv run redact_cc.py ~/bills --auto --keep-row acme
```

`uv` reads the dependency block at the top of the file, builds an isolated
environment for that tool, caches it, and runs the script. There is no virtual
environment to create, activate, or remember, and no repo-level install step.
The first run of a tool downloads its packages; later runs start immediately.

## Tools

| Tool | What it does |
| --- | --- |
| [`redact_cc.py`](redact_cc.py) | Permanently erases credit-card numbers, transaction rows and whole pages from PDF statements. |
| [`stamp_signature.py`](stamp_signature.py) | Stamps a signature image onto PDF pages, placed by the surrounding text rather than fixed coordinates. |

Every tool documents itself:

```bash
uv run redact_cc.py --help
```

## How a tool declares its dependencies

The header of each script is a [PEP 723](https://peps.python.org/pep-0723/)
metadata block — a comment Python ignores and `uv` reads:

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pymupdf>=1.24",
# ]
# ///
"""What this tool does."""
```

This is what keeps the tools independent. A script needing `pikepdf` and one
needing an old `pypdf` can live side by side without either being downgraded to
suit the other, because neither shares an environment with the other.

## Adding a new tool

1. Write a single `.py` file at the root of the repository.
2. Give it the metadata block above, listing only what it imports, at the
   narrowest version range that does the job.
3. Give it the GPL v3 header used by the other tools, with the year and
   copyright holder filled in.
4. Give it a `--help` via `argparse`, and a module docstring that says what it
   does and what it cannot do.
5. Say in that docstring whether AI assistance was used, and keep the AI use
   section below accurate — including for a tool written without it.
6. Add a row to the table above.

To generate the block rather than typing it:

```bash
uv add --script newtool.py pikepdf
```

## Running everything in one shared environment instead

The metadata block is only a comment, so the tools remain ordinary Python
scripts. If you would rather keep one environment for the whole repository —
on a machine without `uv`, or inside an existing workflow — use the union of
the tools' requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 redact_cc.py --help
```

Both routes work at the same time and neither excludes the other. The per-tool
blocks stay authoritative; `requirements.txt` is a convenience that has to be
updated by hand when a tool gains a dependency.

## AI use

The tools here were written with AI assistance, in conversation with Claude
(Anthropic). Stated plainly so the claim is useful rather than decorative:

- **What the model did.** For `redact_cc.py`: the implementation, the synthetic
  test statements, and this README. It also chose most of the design details —
  how rows are grouped, how continuation lines inherit their parent's fate, the
  dependency pin. For `stamp_signature.py`: the author wrote the first working
  version, with AI assistance, in an earlier conversation; the model then
  reworked it to this repository's conventions, added batch processing and the
  rotated-text handling, and fixed the defects named below.
- **What the author did.** Set the requirements, directed every change, decided
  what the tools should and should not do, and accepted or rejected the result
  at each step.
- **How it was checked.** Against synthetic PDFs generated for the purpose,
  never against a real statement or signed document committed to this
  repository. Defects surfaced that way and were fixed before the relevant
  commit. In `redact_cc.py`: a Luhn checksum false positive that erased rows it
  was meant to keep, and a currency continuation line that outlived the
  transaction it belonged to on tightly spaced tables. In
  `stamp_signature.py`: an output file written even when nothing matched, so a
  listing showed a `-signed.pdf` that carried no signature; and sideways text
  placing the signature perpendicular to the name. The second of those is worth
  a note — the first attempted fix keyed on the page's `/Rotate` value, which
  testing showed to be the wrong signal, since the failing page reported no
  rotation at all and the sideways-ness lived in the text.
- **Where it is recorded.** Commits carry a `Co-Authored-By` trailer naming the
  model, so `git log` shows which changes it had a hand in.

AI involvement is not a warranty in either direction, and neither is the
testing above. Both tools do something consequential to a document, so check
what they produce before it leaves your hands. `redact_cc.py` re-reads every
file it writes and reports anything it can still extract — that line is worth
reading rather than skipping. `stamp_signature.py` has `--dry-run`, which
reports every placement it would make without writing anything; a signature in
the wrong place is as much of a problem as a redaction that did not take, and
the dry run is how you find out first.

## Licence

These tools are released under the GNU General Public License v3 or later; see
[LICENSE](LICENSE). Each file carries the licence header and an
`SPDX-License-Identifier` line.

The libraries a tool depends on keep their own licences, which are not always
the same. Both tools here use PyMuPDF, which is dual-licensed under the **AGPL
v3** or an Artifex commercial licence. The AGPL is compatible with the GPL, so
the combination is fine to distribute, but the AGPL's network clause travels
with PyMuPDF: anyone who puts this tool behind a web service owes its users the
corresponding source. That costs nothing for a command-line tool run locally.
Worth keeping in mind when adding a tool — a dependency's licence can be
stricter than the repository's own.
