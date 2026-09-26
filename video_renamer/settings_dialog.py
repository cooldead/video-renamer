"""Settings window: deleting, folders, MakeMKV and NAS export."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QRadioButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from . import settings as app_settings

PRETICK_CHOICES = [
    ("makemkv", "Like MakeMKV: video, plus audio/subtitles in the preferred language"),
    ("all", "All tracks"),
    ("video", "Video only"),
]


def tool_version(program: str) -> str | None:
    """First line of a tool's version output, or None if it can't be run."""
    path = shutil.which(program)
    if path is None:
        return None
    try:
        if Path(path).name.startswith("makemkvcon"):
            # --noscan: report the version without scanning the drives (~0.5 s instead of ~12 s)
            out = subprocess.run([path, "-r", "--noscan", "--cache=1", "info", "disc:9999"], capture_output=True,
                                 text=True, timeout=30).stdout
            for line in out.splitlines():
                if line.startswith("MSG:1005,"):
                    return line.split('"')[1].replace(" started", "")
            return "found"
        return subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout.splitlines()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return "found"


class _ToolCheck(QObject):
    """Runs tool_version() for some programs off the GUI thread."""

    done = Signal(object)  # {name: version or None}

    def __init__(self, programs: dict[str, str]):
        super().__init__()
        self.programs = programs

    def run(self) -> None:
        self.done.emit({name: tool_version(program) for name, program in self.programs.items()})


class PathEdit(QWidget):
    """A line edit with a Browse button (for a folder or a program)."""

    def __init__(self, text: str, folder: bool, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.edit = QLineEdit(text)
        browse = QPushButton(QIcon.fromTheme("document-open-folder"), "Browse…")
        browse.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

    def text(self) -> str:
        return self.edit.text().strip()

    def _browse(self) -> None:
        start = str(Path(self.text()).expanduser()) if self.text() else str(Path.home())
        if self.folder:
            chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, "Choose the program", start)
        if chosen:
            self.edit.setText(chosen)


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 480)
        self.settings = settings
        get = lambda key: app_settings.get(settings, key)

        tabs = QTabWidget()

        # --- General
        general = QWidget()
        self.confirm_delete = QCheckBox("Ask before deleting files and folders")
        self.confirm_delete.setChecked(get("confirm_delete"))
        self.to_trash = QRadioButton("Move them to the Trash (can be restored)")
        self.permanent = QRadioButton("Delete them permanently")
        (self.to_trash if get("delete_to_trash") else self.permanent).setChecked(True)
        delete_group = QButtonGroup(self)
        delete_group.addButton(self.to_trash)
        delete_group.addButton(self.permanent)
        form = QFormLayout(general)
        form.addRow("Deleting", self.confirm_delete)
        form.addRow("", self.to_trash)
        form.addRow("", self.permanent)
        tabs.addTab(general, QIcon.fromTheme("configure"), "General")

        # --- Folders
        folders = QWidget()
        self.start_last = QRadioButton("Reopen the last folder")
        self.start_fixed = QRadioButton("Always open this folder:")
        (self.start_fixed if get("start_folder_mode") == "fixed" else self.start_last).setChecked(True)
        start_group = QButtonGroup(self)
        start_group.addButton(self.start_last)
        start_group.addButton(self.start_fixed)
        self.start_folder = PathEdit(get("start_folder"), folder=True)
        self.start_fixed.toggled.connect(self.start_folder.setEnabled)
        self.start_folder.setEnabled(self.start_fixed.isChecked())
        self.rip_folder = PathEdit(get("rip_folder"), folder=True)
        form = QFormLayout(folders)
        form.addRow("At start (input)", self.start_last)
        form.addRow("", self.start_fixed)
        form.addRow("", self.start_folder)
        form.addRow("Rip discs into", self.rip_folder)
        form.addRow("", QLabel("Each disc gets its own folder inside, named after the disc."))
        tabs.addTab(folders, QIcon.fromTheme("folder"), "Folders")

        # --- MakeMKV
        makemkv_tab = QWidget()
        self.makemkvcon = PathEdit(get("makemkvcon_path"), folder=False)
        self.mkvmerge = PathEdit(get("mkvmerge_path"), folder=False)
        self.tool_status = QLabel()
        check = QPushButton(QIcon.fromTheme("view-refresh"), "Check programs")
        check.clicked.connect(self._check_tools)
        self.min_length = QSpinBox()
        self.min_length.setRange(0, 7200)
        self.min_length.setSuffix(" s")
        self.min_length.setValue(get("rip_min_length"))
        self.cache = QSpinBox()
        self.cache.setRange(0, 8192)
        self.cache.setSuffix(" MB")
        self.cache.setSpecialValueText("MakeMKV default")
        self.cache.setValue(get("makemkv_cache_mb"))
        self.language = QLineEdit(get("makemkv_language"))
        self.language.setPlaceholderText("MakeMKV's own setting (e.g. eng)")
        self.language.setMaxLength(3)
        self.pretick = QComboBox()
        for value, text in PRETICK_CHOICES:
            self.pretick.addItem(text, value)
        self.pretick.setCurrentIndex(max(0, self.pretick.findData(get("pretick"))))
        form = QFormLayout(makemkv_tab)
        form.addRow("makemkvcon", self.makemkvcon)
        form.addRow("mkvmerge", self.mkvmerge)
        form.addRow("", check)
        form.addRow("", self.tool_status)
        form.addRow("Minimum title length", self.min_length)
        form.addRow("Read cache", self.cache)
        form.addRow("Preferred language", self.language)
        form.addRow("Tracks ticked at first", self.pretick)
        form.addRow("", QLabel("mkvmerge (package mkvtoolnix-cli) removes the tracks you untick after ripping."))
        tabs.addTab(makemkv_tab, QIcon.fromTheme("media-optical"), "MakeMKV")

        # --- Export
        export_tab = QWidget()
        self.destinations = QListWidget()
        for path in get("export_destinations"):
            self._add_destination(path)
        add = QPushButton(QIcon.fromTheme("list-add"), "Add…")
        add.clicked.connect(self._browse_destination)
        remove = QPushButton(QIcon.fromTheme("list-remove"), "Remove")
        remove.clicked.connect(lambda: self.destinations.takeItem(self.destinations.currentRow()))
        up = QPushButton(QIcon.fromTheme("go-up"), "Make default")
        up.clicked.connect(self._make_default)
        buttons = QVBoxLayout()
        for button in (add, remove, up):
            buttons.addWidget(button)
        buttons.addStretch(1)
        list_row = QHBoxLayout()
        list_row.addWidget(self.destinations, 1)
        list_row.addLayout(buttons)
        self.delete_after = QCheckBox("Delete the local folder after the copy has been checked")
        self.delete_after.setChecked(get("export_delete_after"))
        layout = QVBoxLayout(export_tab)
        layout.addWidget(QLabel("NAS destinations (output). The first one is the default, and the one you "
                                "export to moves to the top. Double-click to edit."))
        layout.addLayout(list_row)
        layout.addWidget(self.delete_after)
        tabs.addTab(export_tab, QIcon.fromTheme("folder-network"), "Export")

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                               | QDialogButtonBox.StandardButton.RestoreDefaults)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._restore_defaults)

        main = QVBoxLayout(self)
        main.addWidget(tabs, 1)
        main.addWidget(box)
        self._check_tools()

    def _add_destination(self, path: str) -> None:
        item = QListWidgetItem(QIcon.fromTheme("folder-network"), path)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.destinations.addItem(item)

    def _browse_destination(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Add a NAS destination", "/mnt")
        if chosen:
            self._add_destination(chosen)

    def _make_default(self) -> None:
        row = self.destinations.currentRow()
        if row > 0:
            self.destinations.insertItem(0, self.destinations.takeItem(row))
            self.destinations.setCurrentRow(0)

    def _check_tools(self) -> None:
        """Check the programs in the background so the window opens at once."""
        if getattr(self, "_check_thread", None) is not None:
            return
        programs = {"makemkvcon": self.makemkvcon.text() or "makemkvcon",
                    "mkvmerge": self.mkvmerge.text() or "mkvmerge"}
        self.tool_status.setText("<br>".join(f"{name}: checking…" for name in programs))
        self._check_thread = QThread(self)
        self._check = _ToolCheck(programs)
        self._check.moveToThread(self._check_thread)
        self._check_thread.started.connect(self._check.run)
        self._check.done.connect(self._show_tools)
        self._check.done.connect(self._check_thread.quit)
        self._check_thread.finished.connect(self._check_finished)
        self._check_thread.start()

    def _check_finished(self) -> None:
        self._check_thread.deleteLater()
        self._check_thread = None

    def _show_tools(self, versions: dict) -> None:
        self.tool_status.setText("<br>".join(
            f"{name}: " + (f"<span style='color:#3a3'>{version}</span>" if version
                           else "<span style='color:#d33'>not found</span>")
            for name, version in versions.items()))

    def done(self, result: int) -> None:
        # Don't close while the check thread still runs (it takes well under a second).
        if getattr(self, "_check_thread", None) is not None:
            self._check_thread.quit()
            self._check_thread.wait(3000)
        super().done(result)

    def _restore_defaults(self) -> None:
        d = app_settings.DEFAULTS
        self.confirm_delete.setChecked(d["confirm_delete"])
        (self.to_trash if d["delete_to_trash"] else self.permanent).setChecked(True)
        self.start_last.setChecked(d["start_folder_mode"] == "last")
        self.start_fixed.setChecked(d["start_folder_mode"] == "fixed")
        self.start_folder.edit.setText(d["start_folder"])
        self.rip_folder.edit.setText(d["rip_folder"])
        self.makemkvcon.edit.setText(d["makemkvcon_path"])
        self.mkvmerge.edit.setText(d["mkvmerge_path"])
        self.min_length.setValue(d["rip_min_length"])
        self.cache.setValue(d["makemkv_cache_mb"])
        self.language.setText(d["makemkv_language"])
        self.pretick.setCurrentIndex(0)
        self.destinations.clear()
        for path in d["export_destinations"]:
            self._add_destination(path)
        self.delete_after.setChecked(d["export_delete_after"])
        self._check_tools()

    def accept(self) -> None:
        put = lambda key, value: app_settings.put(self.settings, key, value)
        put("confirm_delete", self.confirm_delete.isChecked())
        put("delete_to_trash", self.to_trash.isChecked())
        put("start_folder_mode", "fixed" if self.start_fixed.isChecked() else "last")
        put("start_folder", self.start_folder.text())
        put("rip_folder", self.rip_folder.text() or app_settings.DEFAULTS["rip_folder"])
        put("makemkvcon_path", self.makemkvcon.text() or "makemkvcon")
        put("mkvmerge_path", self.mkvmerge.text() or "mkvmerge")
        put("rip_min_length", self.min_length.value())
        put("makemkv_cache_mb", self.cache.value())
        put("makemkv_language", self.language.text().strip().lower())
        put("pretick", self.pretick.currentData())
        destinations = [self.destinations.item(i).text().strip() for i in range(self.destinations.count())]
        put("export_destinations", [d for d in destinations if d] or app_settings.DEFAULTS["export_destinations"])
        put("export_delete_after", self.delete_after.isChecked())
        super().accept()

