"""Export window: copy a finished folder to the NAS with rsync, check the
copy, and optionally delete the local folder afterwards."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
    QPushButton, QVBoxLayout,
)

from . import export
from . import settings as app_settings
from .rip_dialog import human_bytes

MAX_RECENT = 8


class ExportDialog(QDialog):
    """Non-modal: copying 50+ GB takes a while."""

    exported = Signal(object, bool)  # (local folder, whether it was deleted)

    def __init__(self, source: Path, settings: QSettings, release_folder, parent=None):
        """release_folder(path) is called before the local folder is deleted
        (so the window can stop playing a file from it)."""
        super().__init__(parent)
        self.setWindowTitle(f"Export “{source.name}”")
        self.resize(640, 300)
        self.source = source
        self.settings = settings
        self.release_folder = release_folder
        self._process: QProcess | None = None
        self._buffer = ""
        self._size = export.folder_size(source)

        self.destination = QComboBox()
        self.destination.setEditable(True)
        for path in dict.fromkeys(app_settings.get(settings, "export_destinations")):
            self.destination.addItem(QIcon.fromTheme("folder-network"), path)
        self.destination.currentTextChanged.connect(self._update_target)
        browse = QPushButton(QIcon.fromTheme("document-open-folder"), "Browse…")
        browse.clicked.connect(self._browse)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(browse)

        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        self.delete_after = QCheckBox("Delete the local folder after the copy has been checked")
        self.delete_after.setChecked(app_settings.get(settings, "export_delete_after"))

        form = QFormLayout()
        form.addRow("Folder", QLabel(f"{source}  ({human_bytes(self._size)})"))
        form.addRow("Copy into", destination_row)
        form.addRow("", self.target_label)
        form.addRow("", self.delete_after)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.export_button = QPushButton(QIcon.fromTheme("document-export"), "Export")
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self.start)
        self.stop_button = QPushButton(QIcon.fromTheme("process-stop"), "Stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addLayout(buttons)
        self._update_target()

    def _parent_folder(self) -> Path:
        return Path(self.destination.currentText().strip()).expanduser()

    def target(self) -> Path:
        return self._parent_folder() / self.source.name

    def _problem(self) -> str | None:
        parent = self._parent_folder()
        if not self.destination.currentText().strip():
            return "Choose where to copy the folder."
        if not parent.is_dir():
            return f"{parent} does not exist (is the NAS mounted?)."
        if self.target() == self.source or self.target().is_relative_to(self.source):
            return "The destination is inside the folder itself."
        free = export.free_space(parent)
        if free is not None and free < self._size:
            return f"Not enough space: {human_bytes(free)} free, {human_bytes(self._size)} needed."
        return None

    def _update_target(self, *_args) -> None:
        problem = self._problem()
        if problem:
            self.target_label.setText(f"<span style='color:#d33'>{problem}</span>")
        else:
            free = export.free_space(self._parent_folder())
            text = f"→ <b>{self.target()}</b>" + (f"  ({human_bytes(free)} free)" if free is not None else "")
            if self.target().exists():
                text += "<br><span style='color:#c80'>This folder already exists on the NAS: files are merged, identical ones skipped.</span>"
            self.target_label.setText(text)
        self.export_button.setEnabled(problem is None and self._process is None)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Copy into…", str(self._parent_folder()))
        if folder:
            self.destination.setCurrentText(folder)

    # --- copying -----------------------------------------------------------

    def start(self) -> None:
        problem = self._problem()
        if problem:
            QMessageBox.warning(self, self.windowTitle(), problem)
            return
        destination = str(self._parent_folder())
        # The destination used moves to the top, so it's the default next time.
        recent = [destination] + [d for d in app_settings.get(self.settings, "export_destinations") if d != destination]
        app_settings.put(self.settings, "export_destinations", recent[:MAX_RECENT])
        self._buffer = ""
        self.progress.setValue(0)
        self.status.setText(f"Copying to {self.target()}…")
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        self._process = process
        self._set_busy(True)
        process.start(export.RSYNC, export.rsync_args(self.source, self._parent_folder()))

    def _set_busy(self, busy: bool) -> None:
        self.destination.setEnabled(not busy)
        self.delete_after.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.export_button.setEnabled(not busy and self._problem() is None)

    def _read_output(self) -> None:
        if self._process is None:
            return
        self._buffer += bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._buffer = self._buffer.replace("\r", "\n").split("\n")
        for line in lines:
            progress = export.parse_rsync_progress(line)
            if progress:
                self.progress.setValue(progress.percent)
                self.status.setText(f"Copying… {human_bytes(progress.bytes_done)} of {human_bytes(self._size)}"
                                    f" · {progress.speed} · {progress.eta} left")
            elif line.strip():
                self._last_message = line.strip()

    def _error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._process = None
            self._set_busy(False)
            QMessageBox.critical(self, self.windowTitle(), "Could not start rsync. Is it installed?")

    def _finished(self, exit_code: int, exit_status) -> None:
        self._process = None
        self._set_busy(False)
        if exit_status != QProcess.ExitStatus.NormalExit or exit_code != 0:
            message = getattr(self, "_last_message", "")
            self.status.setText(f"<span style='color:#d33'>Copy failed (rsync exit code {exit_code}). {message}</span>")
            return
        self.status.setText("Checking the copy…")
        self.progress.setValue(100)
        problems = export.verify_copy(self.source, self.target())
        if problems:
            self.status.setText("<span style='color:#d33'>The copy is incomplete. The local folder was kept.</span>")
            QMessageBox.warning(self, self.windowTitle(), "The copy on the NAS is incomplete:\n\n" + "\n".join(problems[:15]))
            self.exported.emit(self.source, False)
            return
        if not self.delete_after.isChecked():
            self.status.setText(f"Copied and checked: {self.target()}")
            self.exported.emit(self.source, False)
            return
        self.release_folder(self.source)
        try:
            shutil.rmtree(self.source)
        except OSError as error:
            self.status.setText(f"Copied and checked, but the local folder could not be deleted: {error}")
            self.exported.emit(self.source, False)
            return
        self.status.setText(f"Copied and checked: {self.target()}. The local folder was deleted.")
        self.exported.emit(self.source, True)

    def stop(self) -> None:
        if self._process is not None:
            self._process.finished.disconnect()
            self._process.kill()
            self._process.waitForFinished(5000)
            self._process = None
            self._set_busy(False)
            self.status.setText("Stopped. Starting the export again continues where it left off.")

    def closeEvent(self, event) -> None:
        if self._process is not None:
            if QMessageBox.question(self, self.windowTitle(), "The copy is still running. Stop it and close?") \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop()
        super().closeEvent(event)
