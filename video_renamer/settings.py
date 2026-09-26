"""App settings: one place for every setting's key and default."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from . import export

DEFAULTS: dict[str, object] = {
    # General
    "confirm_delete": True,          # ask before deleting files/folders
    "delete_to_trash": True,         # move to the Trash instead of deleting permanently
    # Folders
    "start_folder_mode": "last",     # "last" = reopen the last folder, "fixed" = start_folder
    "start_folder": str(Path.home() / "Videos"),
    "rip_folder": str(Path.home() / "Videos"),
    # MakeMKV
    "makemkvcon_path": "makemkvcon",
    "mkvmerge_path": "mkvmerge",
    "rip_min_length": 120,           # seconds
    "makemkv_cache_mb": 0,           # 0 = MakeMKV's default
    "makemkv_language": "",          # "" = MakeMKV's own preferred language
    "pretick": "makemkv",            # "makemkv" / "all" / "video"
    # Export
    "export_destinations": list(export.DEFAULT_DESTINATIONS),
    "export_delete_after": False,
}


def get(settings: QSettings, key: str):
    default = DEFAULTS[key]
    if isinstance(default, list):
        value = settings.value(key, default)
        if isinstance(value, str):  # QSettings returns a 1-item list as a string
            value = [value]
        return list(value) if value else list(default)
    return settings.value(key, default, type(default))


def put(settings: QSettings, key: str, value) -> None:
    settings.setValue(key, value)
