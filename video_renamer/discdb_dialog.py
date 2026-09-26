"""Match with TheDiscDB: compare the videos in a folder with the discs in
TheDiscDB by length, suggest names, and let the user approve them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from . import discdb, renamer
from .discdb import DbDisc, DbTitle, LocalFile
from .file_model import scan_tree
from .renamer import RenameOp

COL_USE, COL_FILE, COL_LENGTH, COL_MATCH, COL_NEW = range(5)
REQUEST_TIMEOUT_MS = 30000


def probe_seconds(path: Path) -> float | None:
    """A video's length in seconds (mkvmerge, or ffprobe as a fallback)."""
    try:
        if shutil.which("mkvmerge"):
            info = json.loads(subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True,
                                             text=True, timeout=60).stdout)
            duration = info.get("container", {}).get("properties", {}).get("duration")
            if duration:
                return duration / 1e9
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def format_length(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


class DiscDbDialog(QDialog):
    def __init__(self, folder: Path, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.setWindowTitle(f"Match with TheDiscDB — {folder.name}")
        self.resize(1150, 620)
        self._network = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._ranked: list[tuple[DbDisc, dict[Path, DbTitle]]] = []

        self.search_edit = QLineEdit(discdb.search_text(folder.name))
        self.search_edit.returnPressed.connect(self.search)
        self.search_button = QPushButton(QIcon.fromTheme("edit-find"), "Search TheDiscDB")
        self.search_button.clicked.connect(self.search)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search"))
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_button)

        self.disc_combo = QComboBox()
        self.disc_combo.currentIndexChanged.connect(self._show_disc)
        disc_row = QHBoxLayout()
        disc_row.addWidget(QLabel("Disc"))
        disc_row.addWidget(self.disc_combo, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "File", "Length", "TheDiscDB match", "New name (extension is kept)"])
        header = self.table.horizontalHeader()
        for column, mode in ((COL_USE, QHeaderView.ResizeMode.ResizeToContents),
                             (COL_FILE, QHeaderView.ResizeMode.Stretch),
                             (COL_LENGTH, QHeaderView.ResizeMode.ResizeToContents),
                             (COL_MATCH, QHeaderView.ResizeMode.Stretch),
                             (COL_NEW, QHeaderView.ResizeMode.Stretch)):
            header.setSectionResizeMode(column, mode)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.itemChanged.connect(self._validate)

        self.rename_folder = QCheckBox("Rename the folder to")
        self.folder_name = QLineEdit()
        self.rename_folder.toggled.connect(self.folder_name.setEnabled)
        self.rename_folder.toggled.connect(self._validate)
        self.folder_name.textChanged.connect(self._validate)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.rename_folder)
        folder_row.addWidget(self.folder_name, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Rename")
        self.apply_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        credit = QLabel('Data from <a href="https://thediscdb.com">TheDiscDB</a>')
        credit.setOpenExternalLinks(True)
        bottom = QHBoxLayout()
        bottom.addWidget(credit, 1)
        bottom.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Folder: <b>{folder}</b>"))
        layout.addLayout(search_row)
        layout.addLayout(disc_row)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        layout.addLayout(folder_row)
        layout.addLayout(bottom)

        self.files = self._load_files()
        self._fill_table({})
        if self.files:
            self.search()
        else:
            self.status.setText("There are no video files in this folder.")

    # --- local files ---------------------------------------------------------

    def _load_files(self) -> list[LocalFile]:
        self.status.setText("Reading video lengths…")
        _folders, entries = scan_tree(self.folder)
        files = []
        for entry in entries:
            seconds = probe_seconds(entry.path)
            if seconds is not None:
                files.append(LocalFile(entry.path, seconds))
        return files

    # --- TheDiscDB -------------------------------------------------------------

    def search(self) -> None:
        text = self.search_edit.text().strip()
        if not text or self._reply is not None:
            return
        request = QNetworkRequest(QUrl(discdb.ENDPOINT))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(b"User-Agent", b"video-renamer")
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        body = json.dumps({"query": discdb.SEARCH_QUERY, "variables": {"text": text}}).encode()
        self.status.setText(f"Searching TheDiscDB for “{text}”…")
        self.search_button.setEnabled(False)
        self._reply = self._network.post(request, QByteArray(body))
        self._reply.finished.connect(self._search_finished)

    def _search_finished(self) -> None:
        reply, self._reply = self._reply, None
        self.search_button.setEnabled(True)
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.status.setText(f"<span style='color:#d33'>Could not reach TheDiscDB: {reply.errorString()}</span>")
            return
        try:
            response = json.loads(bytes(reply.readAll()).decode("utf-8"))
        except ValueError:
            self.status.setText("<span style='color:#d33'>TheDiscDB sent an unreadable answer.</span>")
            return
        if response.get("errors"):
            self.status.setText(f"<span style='color:#d33'>TheDiscDB error: {response['errors'][0].get('message')}</span>")
            return
        discs = discdb.parse_search(response)
        self._ranked = discdb.rank_discs(self.files, discs)
        self.disc_combo.blockSignals(True)
        self.disc_combo.clear()
        for disc, matches in self._ranked:
            self.disc_combo.addItem(f"{len(matches)}/{len(self.files)} files match · {disc.label()}")
        self.disc_combo.blockSignals(False)
        if not self._ranked:
            self.status.setText("Nothing found. Try a different search (e.g. just the main title).")
            self._fill_table({})
            return
        self.disc_combo.setCurrentIndex(0)
        self._show_disc(0)

    def _show_disc(self, index: int) -> None:
        if not 0 <= index < len(self._ranked):
            return
        disc, matches = self._ranked[index]
        self.status.setText(
            f"{len(matches)} of {len(self.files)} file(s) match a title on this disc by length (±{discdb.MATCH_TOLERANCE:.0f} s). "
            "Tick the renames to do and edit names if needed; nothing changes until you press Rename.")
        self._fill_table(matches, discdb.suggest_names(matches, disc))
        new_folder = discdb.safe_name(disc.base_name)
        self.folder_name.setText(new_folder)
        self.rename_folder.setChecked(bool(matches) and new_folder != self.folder.name)
        self.folder_name.setEnabled(self.rename_folder.isChecked())

    # --- table -------------------------------------------------------------------

    def _fill_table(self, matches: dict[Path, DbTitle], names: dict[Path, str] | None = None) -> None:
        names = names or {}
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.files))
        for row, file in enumerate(sorted(self.files, key=lambda f: str(f.path))):
            relative = file.path.relative_to(self.folder)
            match = matches.get(file.path)
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use.setCheckState(Qt.CheckState.Checked if match else Qt.CheckState.Unchecked)
            use.setData(Qt.ItemDataRole.UserRole, str(file.path))
            current = QTableWidgetItem(str(relative))
            length = QTableWidgetItem(format_length(file.seconds))
            found = QTableWidgetItem(match.label() if match else "no match")
            if not match:
                found.setForeground(QBrush(self.palette().placeholderText().color()))
            new = QTableWidgetItem(names.get(file.path, str(relative.with_suffix(""))))
            for item in (current, length, found):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, COL_USE, use)
            self.table.setItem(row, COL_FILE, current)
            self.table.setItem(row, COL_LENGTH, length)
            self.table.setItem(row, COL_MATCH, found)
            self.table.setItem(row, COL_NEW, new)
        self.table.blockSignals(False)
        self._validate()

    def ops(self) -> tuple[list[RenameOp], dict[int, str]]:
        """Renames for the ticked rows, and per-row problems."""
        ops, problems = [], {}
        for row in range(self.table.rowCount()):
            if self.table.item(row, COL_USE).checkState() != Qt.CheckState.Checked:
                continue
            source = Path(self.table.item(row, COL_USE).data(Qt.ItemDataRole.UserRole))
            try:
                # New names are relative to the scanned folder.
                target = renamer.target_for(self.folder / "_", self.table.item(row, COL_NEW).text(), source.suffix)
            except renamer.RenameError as error:
                problems[row] = str(error)
                continue
            if target != source:
                ops.append(RenameOp(source, target))
        rows = {Path(self.table.item(r, COL_USE).data(Qt.ItemDataRole.UserRole)): r for r in range(self.table.rowCount())}
        for source, message in renamer.plan_renames(ops).items():
            problems[rows[source]] = message
        return ops, problems

    def new_folder_name(self) -> str | None:
        name = self.folder_name.text().strip()
        return name if self.rename_folder.isChecked() and name and name != self.folder.name else None

    def _validate(self, *_args) -> None:
        ops, problems = self.ops()
        red = QBrush(QColor("#d33"))
        normal = QBrush(self.palette().text().color())
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_NEW)
            if item is None:
                continue
            item.setForeground(red if row in problems else normal)
            item.setToolTip(problems.get(row, ""))
        folder_problem = None
        if self.new_folder_name():
            folder_problem = renamer.validate_name(self.new_folder_name())
            if not folder_problem and self.folder.with_name(self.new_folder_name()).exists():
                folder_problem = "a folder with that name already exists"
        self.folder_name.setToolTip(folder_problem or "")
        self.folder_name.setStyleSheet("color: #d33" if folder_problem else "")
        self.apply_button.setEnabled(not problems and not folder_problem and bool(ops or self.new_folder_name()))
