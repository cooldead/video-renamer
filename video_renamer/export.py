"""Copying a finished folder to the NAS with rsync, and checking the copy.

Nothing here touches Qt.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

RSYNC = "rsync"

DEFAULT_DESTINATIONS = ["/mnt/media/Movies", "/mnt/media/TVshows"]


def rsync_args(source: Path, destination_parent: Path) -> list[str]:
    """Copy the source folder into destination_parent (as destination_parent/<name>).

    -rt instead of -a: the NAS shares are CIFS mounts, where owners and
    permissions are fixed by the mount. --partial lets a cancelled copy resume.
    """
    return ["-rt", "--partial", "--modify-window=2", "--info=progress2", "--no-inc-recursive",
            str(source), str(destination_parent) + "/"]


@dataclass
class RsyncProgress:
    bytes_done: int
    percent: int
    speed: str
    eta: str


_PROGRESS_RE = re.compile(r"^\s*([\d,.]+)([KMGT]?)\s+(\d+)%\s+(\S+)\s+(\d+:\d{2}:\d{2})")


def parse_rsync_progress(line: str) -> RsyncProgress | None:
    """Parse one --info=progress2 line, e.g.
    "  1,234,567,890  45%  110.21MB/s    0:00:10 (xfr#1, to-chk=3/5)"."""
    match = _PROGRESS_RE.match(line)
    if not match:
        return None
    number, unit, percent, speed, eta = match.groups()
    scale = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[unit]
    return RsyncProgress(int(float(number.replace(",", "")) * scale), int(percent), speed, eta)


def folder_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def verify_copy(source: Path, target: Path) -> list[str]:
    """Problems with target as a copy of source (missing files, size
    differences). An empty list means every file arrived complete."""
    problems = []
    for root, _dirs, files in os.walk(source):
        for name in files:
            local = Path(root) / name
            relative = local.relative_to(source)
            remote = target / relative
            try:
                local_size = local.stat().st_size
            except OSError as error:
                problems.append(f"{relative}: cannot read local file ({error})")
                continue
            try:
                remote_size = remote.stat().st_size
            except OSError:
                problems.append(f"{relative}: missing on the NAS")
                continue
            if remote_size != local_size:
                problems.append(f"{relative}: {remote_size} of {local_size} bytes on the NAS")
    return problems


def free_space(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None
