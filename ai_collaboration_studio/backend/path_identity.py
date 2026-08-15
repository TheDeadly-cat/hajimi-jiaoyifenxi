from __future__ import annotations

import os
import stat
from pathlib import Path


def first_reparse_component(path: str | Path) -> Path | None:
    """Return the first existing symlink/reparse component in a raw path.

    The check intentionally walks the path before calling ``resolve()``. A
    regular file below a junction (or a symlinked parent) is still an alias,
    even though resolving the final file would make it look ordinary. Missing
    trailing components are allowed; every existing ancestor is checked.
    """

    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    anchor = Path(requested.anchor) if requested.anchor else Path.cwd()
    current = anchor
    anchor_parts = anchor.parts
    for part in requested.parts[len(anchor_parts) :]:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if not os.path.lexists(os.fspath(current)):
            break
        try:
            metadata = current.lstat()
        except OSError:
            # The caller will perform its own identity/hash read and fail
            # closed if the component disappears or is unreadable.
            break
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(
            reparse_flag and attributes & reparse_flag
        ):
            return current
    return None
