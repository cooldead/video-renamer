"""Export window: copy a finished folder to the NAS with rsync, check the
copy, and optionally delete the local folder afterwards."""

from __future__ import annotations

import itertools
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
from .rip_dialog import human_bytes, stop_process

MAX_RECENT = 8
_queue_order = itertools.count()  # order in which exports joined the queue


class ExportDialog(QDialog):
    """Non-modal: copying 50+ GB takes a while. Several can be open (one per
    folder); they copy one at a time by default, the others wait in a queue."""

    exported = Signal(object, bool)  # (local folder, whether it was deleted)
    idle = Signal()                  # a copy ended or was stopped: the next queued export may start

    def __init__(self, source: Path, settings: QSettings, release_folder, parent=None, others=lambda: []):
        """release_folder(path) is called before the local folder is deleted
        (so the window can stop playing a file from it). others() returns the
        other open Export windows."""
        super().__init__(parent)
        self._others = others
        self.queued_at: int | None = None  # set while waiting for another export
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

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setVisible(False)
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
        layout.addWidget(self.hint)
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
        self.export_button.setEnabled(problem is None and self._process is None and self.queued_at is None)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Copy into…", str(self._parent_folder()))
        if folder:
            self.destination.setCurrentText(folder)

    # --- several exports -------------------------------------------------------

    def is_running(self) -> bool:
        return self._process is not None

    def _other_windows(self) -> list["ExportDialog"]:
        return [o for o in self._others() if o is not self]

    def set_group_hint(self, open_windows: int) -> None:
        """Shown while several Export windows are open."""
        self.hint.setVisible(open_windows > 1)
        self.hint.setText(f"<i>{open_windows} Export windows are open. Copying one folder at a time is "
                          "recommended: several copies at once share the network and each gets slower. "
                          "Start them all and they will wait in line.</i>")

    def _ask_to_wait(self, running: "ExportDialog") -> str:
        """"wait", "now" or "cancel"."""
        box = QMessageBox(QMessageBox.Icon.Question, self.windowTitle(),
                          f"“{running.source.name}” is still being copied.", parent=self)
        box.setInformativeText("Copying one folder at a time is recommended. Wait, and this export starts "
                               "automatically when the other one is done?")
        wait = box.addButton("Wait until it finishes (recommended)", QMessageBox.ButtonRole.AcceptRole)
        now = box.addButton("Start now anyway", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(wait)
        box.exec()
        return {wait: "wait", now: "now"}.get(box.clickedButton(), "cancel")

    def _enqueue(self) -> None:
        self.queued_at = next(_queue_order)
        ahead = [o for o in self._other_windows() if o.is_running() or
                 (o.queued_at is not None and o.queued_at < self.queued_at)]
        self.status.setText(f"Waiting: starts automatically when {len(ahead)} export"
                            f"{'s' if len(ahead) != 1 else ''} ahead of it {'are' if len(ahead) != 1 else 'is'} done. "
                            "Press Stop to leave the queue.")
        self._set_busy(True)

    # --- copying -----------------------------------------------------------

    def start(self, from_queue: bool = False) -> None:
        problem = self._problem()
        if problem:
            self.queued_at = None
            self._set_busy(False)
            QMessageBox.warning(self, self.windowTitle(), problem)
            self.idle.emit()  # let the next queued export go
            return
        if not from_queue:
            busy = [o for o in self._other_windows() if o.is_running() or o.queued_at is not None]
            if busy:
                running = next((o for o in busy if o.is_running()), busy[0])
                choice = self._ask_to_wait(running)
                if choice == "cancel":
                    return
                if choice == "wait":
                    self._enqueue()
                    return
        self.queued_at = None
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
        """busy = copying or waiting in the queue."""
        self.destination.setEnabled(not busy)
        self.delete_after.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.stop_button.setText("Leave queue" if self.queued_at is not None else "Stop")
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
            self.idle.emit()

    def _finished(self, exit_code: int, exit_status) -> None:
        self._process = None
        self._set_busy(False)
        try:
            self._report_finished(exit_code, exit_status)
        finally:
            self.idle.emit()  # the next queued export may start now

    def _report_finished(self, exit_code: int, exit_status) -> None:
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
        if self.queued_at is not None:
            self.queued_at = None
            self._set_busy(False)
            self.status.setText("Left the queue. Press Export to start it later.")
            return
        if self._process is not None:
            self._process.finished.disconnect()
            stop_process(self._process)
            self._process = None
            self._set_busy(False)
            self.status.setText("Stopped. Starting the export again continues where it left off.")
            self.idle.emit()

    def closeEvent(self, event) -> None:
        if self.queued_at is not None:
            self.stop()  # just leave the queue
        if self._process is not None:
            if QMessageBox.question(self, self.windowTitle(), "The copy is still running. Stop it and close?") \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop()
        super().closeEvent(event)
