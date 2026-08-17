"""Output filenames.

Two jobs, both security-relevant in their way:

  * nothing in a track title may escape the output directory, and track titles
    come from strangers on Soulseek and from YouTube uploaders;
  * nothing already on disk is ever overwritten. Collisions get -2, -3, which
    is the rule uvr-local/uvr.py already follows for its own output folders.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

#: Illegal on HFS+/APFS or in Finder, plus the separators that would make a
#: name into a path.
_ILLEGAL = r'/\\:*?"<>|'
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COLLAPSE = re.compile(r"\s+")

#: Windows device names. NEYTA is macOS-only, but output files get dragged
#: into shared folders and cloud drives, so they are neutralised anyway.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MAX_COMPONENT = 120


def sanitise(text: str, *, fallback: str = "untitled") -> str:
    """One filename component, guaranteed to contain no path structure.

    Deliberately not `Path(text).name` — that keeps `..` intact and silently
    drops everything before the last separator, so "Artist/../../x" would
    become "x" and lose the artist. Separators become underscores instead.
    """
    text = unicodedata.normalize("NFC", text)
    # Space, not deletion: a tab or newline in a title is a word boundary, and
    # "a\nb" should read as "a b" rather than "ab".
    text = _CONTROL.sub(" ", text)
    text = "".join("_" if c in _ILLEGAL else c for c in text)
    text = _COLLAPSE.sub(" ", text).strip()

    # A name made only of dots is a directory reference, not a name.
    if set(text) <= {".", " "}:
        text = ""

    text = text.strip(" .")

    if text.split(".")[0].lower() in _RESERVED:
        text = f"_{text}"

    if len(text) > MAX_COMPONENT:
        text = text[:MAX_COMPONENT].rstrip(" .")

    return text or fallback


def output_name(
    *,
    title: str,
    artist: str | None = None,
    stem: str | None = None,
    ext: str,
) -> str:
    """`Artist - Title [stem].ext`, with the optional parts dropped cleanly."""
    parts = []
    if artist and artist.strip():
        parts.append(sanitise(artist, fallback="unknown artist"))
    parts.append(sanitise(title, fallback="untitled"))
    name = " - ".join(parts)

    if stem and stem.strip():
        name = f"{name} [{sanitise(stem, fallback='stem')}]"

    ext = ext.lstrip(".").strip()
    suffix = f".{sanitise(ext, fallback='bin')}" if ext else ""

    # Each component is capped at MAX_COMPONENT, but the join of artist,
    # title and stem can still run past what a filesystem will take. Trim the
    # stub only — never the extension, which is what tells Ableton and Finder
    # what the file is.
    limit = MAX_COMPONENT * 2 - len(suffix)
    if len(name) > limit:
        name = name[:limit].rstrip(" .") or "untitled"
    return name + suffix


def unique_path(directory: Path, filename: str) -> Path:
    """`directory/filename`, or the first free `name-2.ext`, `name-3.ext`, ...

    Never touches anything already on disk, and never returns a path that
    exists at call time.
    """
    directory = Path(directory)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def resolve_output(
    directory: Path,
    *,
    title: str,
    artist: str | None = None,
    stem: str | None = None,
    ext: str,
) -> Path:
    """The full path a finished file should be written to.

    Asserts the result really is inside `directory`, so a sanitisation bug can
    never become a path traversal.
    """
    directory = Path(directory).resolve()
    name = output_name(title=title, artist=artist, stem=stem, ext=ext)
    path = unique_path(directory, name)
    if path.parent.resolve() != directory:
        raise ValueError(f"refusing to write outside {directory}: {path}")
    return path
