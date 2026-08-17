"""Deleting a file the way the desktop means it.

The Downloaded page is a view of a real folder, so removing a row has to
remove the file — anything else is a list that lies about what is on disk.
It goes to the Trash rather than being unlinked: a mis-clicked delete of a
stem that took four minutes to separate should cost a trip to the Trash, not
the separation.

`unlink` is the fallback and is never taken silently — the caller is told
which one happened so it can ask first.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


class TrashUnavailable(RuntimeError):
    """No Trash folder on this system, or it cannot be written to."""


@dataclass(frozen=True)
class Removal:
    """What happened to one file."""

    source: Path
    #: Where it went, or None when it was unlinked outright.
    destination: Path | None

    @property
    def recoverable(self) -> bool:
        return self.destination is not None


def trash_dir(home: Path | None = None) -> Path:
    """The desktop's Trash for this user.

    macOS is the only platform NEYTA ships on; the XDG path is here so the
    Linux CI box does not have to special-case the tests.
    """
    home = Path(home or Path.home())
    if sys.platform == "darwin":
        return home / ".Trash"
    return home / ".local" / "share" / "Trash" / "files"


def _free_name(directory: Path, name: str) -> Path:
    """`name`, or "name 2", "name 3"… — the Finder's own collision rule.

    Two downloads of the same track deleted a week apart must not have the
    second one silently overwrite the first one in the Trash.
    """
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(2, 1000):
        candidate = directory / f"{stem} {n}{suffix}"
        if not candidate.exists():
            return candidate
    raise TrashUnavailable(f"no free name for {name} in {directory}")


def move_to_trash(path: Path | str, *, trash: Path | None = None) -> Removal:
    """Move one file to the Trash. Raises TrashUnavailable if there is none.

    A cross-volume move falls back to a copy-and-delete, which is what the
    Finder does for an external drive too.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    directory = Path(trash) if trash is not None else trash_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TrashUnavailable(f"no Trash at {directory}: {exc}") from exc

    target = _free_name(directory, path.name)
    try:
        path.rename(target)
    except OSError:
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:  # different volume, no permission, …
            raise TrashUnavailable(f"could not trash {path}: {exc}") from exc
    return Removal(source=path, destination=target)


def delete(path: Path | str) -> Removal:
    """Delete outright. Only for when the Trash is not available and the
    caller has asked the user whether they mean it."""
    path = Path(path)
    path.unlink()
    return Removal(source=path, destination=None)
