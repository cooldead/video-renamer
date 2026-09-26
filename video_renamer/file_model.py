"""Folder scanning and the tree model behind the file list."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv",
})

COL_NAME, COL_SIZE, COL_MODIFIED = range(3)
COLUMNS = ("Name", "Size", "Modified")

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
IS_DIR_ROLE = Qt.ItemDataRole.UserRole + 2
SORT_ROLE = Qt.ItemDataRole.UserRole + 3


@dataclass
class VideoEntry:
    path: Path
    size: int
    mtime: float


def is_video(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS


def scan_tree(root: Path) -> tuple[list[tuple[Path, float]], list[VideoEntry]]:
    """All non-hidden subfolders (with mtime) and video files below root.
    Folders come parent-first."""
    folders: list[tuple[Path, float]] = []
    files: list[VideoEntry] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        base = Path(current)
        for name in dirs:
            try:
                folders.append((base / name, (base / name).stat().st_mtime))
            except OSError:
                continue
        for name in names:
            if name.startswith(".") or not is_video(name):
                continue
            path = base / name
            try:
                info = path.stat()
            except OSError:
                continue
            if path.is_file():
                files.append(VideoEntry(path, info.st_size, info.st_mtime))
    return folders, files


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _format_time(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


class VideoTreeModel(QStandardItemModel):
    # Emitted when the user edits a file/folder name in the tree: (path, new text).
    # The window does the actual rename so it can handle the player first.
    renameRequested = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root: Path | None = None
        self.entries: dict[Path, VideoEntry] = {}
        self.folders: set[Path] = set()
        self._items: dict[Path, QStandardItem] = {}
        self._folder_icon = QIcon.fromTheme("folder")
        self._video_icon = QIcon.fromTheme("video-x-generic")
        self.setHorizontalHeaderLabels(COLUMNS)

    def load(self, root: Path) -> None:
        folders, files = scan_tree(root)
        self.reset()
        self.root = root
        self.entries = {entry.path: entry for entry in files}
        self.folders = {path for path, _ in folders}
        for path, mtime in folders:
            self._append(path.parent, self._folder_row(path, mtime))
        for entry in files:
            self._append(entry.path.parent, self._file_row(entry))

    def reset(self) -> None:
        self.removeRows(0, self.rowCount())  # keeps the header/columns
        self.root = None
        self.entries = {}
        self.folders = set()
        self._items = {}

    def index_for(self, path: Path) -> QModelIndex:
        item = self._items.get(path)
        return item.index() if item is not None else QModelIndex()

    @staticmethod
    def editable_name(path: Path, is_dir: bool) -> str:
        """What the user edits: a folder's full name, a file's name without extension."""
        return path.name if is_dir else path.stem

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.EditRole and index.column() == COL_NAME:
            path = super().data(index, PATH_ROLE)
            if path is not None:
                return self.editable_name(path, bool(super().data(index, IS_DIR_ROLE)))
        return super().data(index, role)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.EditRole and index.column() == COL_NAME:
            path = super().data(index, PATH_ROLE)
            is_dir = bool(super().data(index, IS_DIR_ROLE))
            if path is not None and str(value) != self.editable_name(path, is_dir):
                self.renameRequested.emit(path, str(value))
            return False
        return super().setData(index, value, role)

    def _append(self, parent: Path, row: list[QStandardItem]) -> None:
        parent_item = self.invisibleRootItem() if parent == self.root else self._items.get(parent)
        if parent_item is None:
            return
        parent_item.appendRow(row)
        self._items[row[0].data(PATH_ROLE)] = row[0]

    def _folder_row(self, path: Path, mtime: float) -> list[QStandardItem]:
        name = self._item(path.name, path.name.casefold(), path, True)
        name.setIcon(self._folder_icon)
        name.setToolTip(str(path))
        name.setEditable(True)
        return [name, self._item("", 0, path, True), self._item(_format_time(mtime), mtime, path, True)]

    def _file_row(self, entry: VideoEntry) -> list[QStandardItem]:
        path = entry.path
        name = self._item(path.name, path.name.casefold(), path, False)
        name.setIcon(self._video_icon)
        name.setToolTip(str(path))
        name.setEditable(True)
        size = self._item(human_size(entry.size), entry.size, path, False)
        size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return [name, size, self._item(_format_time(entry.mtime), entry.mtime, path, False)]

    @staticmethod
    def _item(text: str, sort_key, path: Path, is_dir: bool) -> QStandardItem:
        item = QStandardItem(text)
        item.setEditable(False)
        item.setData(sort_key, SORT_ROLE)
        item.setData(path, PATH_ROLE)
        item.setData(is_dir, IS_DIR_ROLE)
        return item


class VideoTreeProxy(QSortFilterProxyModel):
    """Folders before files; the name filter matches files and keeps the
    folders that contain a match."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self.setSortRole(SORT_ROLE)
        self.setRecursiveFilteringEnabled(True)

    def set_filter_text(self, text: str) -> None:
        self.beginFilterChange()
        self._text = text.strip().casefold()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filter_text(self) -> str:
        return self._text

    def filterAcceptsRow(self, source_row, source_parent):
        if not self._text:
            return True
        index = self.sourceModel().index(source_row, COL_NAME, source_parent)
        if index.data(IS_DIR_ROLE):
            return False  # shown only when something inside matches
        return self._text in index.data(Qt.ItemDataRole.DisplayRole).casefold()

    def lessThan(self, left, right):
        left_dir, right_dir = bool(left.data(IS_DIR_ROLE)), bool(right.data(IS_DIR_ROLE))
        if left_dir != right_dir:
            # Keep folders on top in both sort directions.
            return left_dir if self.sortOrder() == Qt.SortOrder.AscendingOrder else right_dir
        return left.data(SORT_ROLE) < right.data(SORT_ROLE)
