"""Rip Disc window: pick titles and tracks like in MakeMKV, rip them with
makemkvcon, then drop the unticked tracks with mkvmerge."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from . import makemkv, ripstats
from . import settings as app_settings
from .makemkv import Title

TRACK_ICONS = {"video": "video-x-generic", "audio": "audio-x-generic", "subtitle": "text-x-generic"}
TRACK_KIND_NAMES = {"video": "Video", "audio": "Audio", "subtitle": "Subtitles", "other": "Other"}
ID_ROLE = Qt.ItemDataRole.UserRole + 1  # (title id, track id or None)


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


class RipDialog(QDialog):
    """Non-modal: a rip takes a long time and the renamer stays usable.
    One window per drive; several can rip at the same time."""

    ripFinished = Signal(object)  # the output folder

    def __init__(self, settings: QSettings, parent=None, others=lambda: []):
        """others() returns the other open Rip windows, so two windows never
        use the same drive or output folder."""
        super().__init__(parent)
        self.setWindowTitle("Rip Disc")
        self._others = others
        self.resize(900, 720)
        self.settings = settings
        self.disc: makemkv.Disc | None = None
        self.drives: list[makemkv.Drive] = []
        self._process: QProcess | None = None
        self._phase = "idle"              # idle / drives / scan / rip / strip
        self._buffer = ""
        self._lines: list[str] = []
        self._queue: list[tuple[Title, list[int]]] = []
        self._current: tuple[Title, list[int]] | None = None
        self._strip_temp: Path | None = None
        self._output: Path | None = None
        self._done_titles = 0
        self._total_titles = 0
        self._failures: list[str] = []
        self._scanned_min_length = 120
        self._total_bytes = 0
        self._done_bytes = 0
        # Statistics and the report shown when the rip is done.
        self._meter = ripstats.TransferMeter()
        self._phase_started = 0.0
        self._strip_percent = 0
        self._report: dict = {}
        self._reports: list[dict] = []
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._update_stats)

        self.drive_combo = QComboBox()
        self.refresh_button = QPushButton(QIcon.fromTheme("view-refresh"), "")
        self.refresh_button.setToolTip("Look for drives and discs again")
        self.refresh_button.clicked.connect(self.list_drives)
        self.min_length = QSpinBox()
        self.min_length.setRange(0, 7200)
        self.min_length.setSuffix(" s")
        self.min_length.setValue(app_settings.get(settings, "rip_min_length"))
        self.min_length.setToolTip("Titles shorter than this are not listed (MakeMKV's minimum title length)")
        self.scan_button = QPushButton(QIcon.fromTheme("media-optical"), "Scan Disc")
        self.scan_button.clicked.connect(self.scan)

        top = QHBoxLayout()
        top.addWidget(QLabel("Drive"))
        top.addWidget(self.drive_combo, 1)
        top.addWidget(self.refresh_button)
        top.addSpacing(12)
        top.addWidget(QLabel("Min. title length"))
        top.addWidget(self.min_length)
        top.addWidget(self.scan_button)

        self.disc_label = QLabel("Looking for drives…")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Title / track", "Length", "Chapters", "Size"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._update_summary)

        self.output_edit = QLineEdit()
        self.output_edit.textChanged.connect(self._update_summary)
        browse = QPushButton(QIcon.fromTheme("document-open-folder"), "Browse…")
        browse.clicked.connect(self._browse)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Save to"))
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse)

        self.summary = QLabel()
        self.status = QLabel()
        self.stats_label = QLabel()
        self.stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Overall bar: all ticked titles together, weighted by size, so it only
        # reaches 100% when the last one is done. Second bar: the current title.
        self.title_progress = QProgressBar()
        self.title_progress.setRange(0, 1000)
        self.step_progress = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(110)

        self.rip_button = QPushButton(QIcon.fromTheme("media-record"), "Rip")
        self.rip_button.setDefault(True)
        self.rip_button.clicked.connect(self.start_rip)
        self.stop_button = QPushButton(QIcon.fromTheme("process-stop"), "Stop")
        self.stop_button.clicked.connect(self.stop)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.summary, 1)
        buttons.addWidget(self.rip_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.disc_label)
        layout.addWidget(self.tree, 1)
        layout.addLayout(output_row)
        layout.addWidget(self.status)
        layout.addWidget(self.stats_label)
        layout.addWidget(self.title_progress)
        layout.addWidget(self.step_progress)
        layout.addWidget(self.log)
        layout.addLayout(buttons)

        self._set_busy(False)
        self.list_drives()

    # --- running makemkvcon/mkvmerge --------------------------------------

    def _run(self, phase: str, program: str, args: list[str]) -> None:
        self._phase = phase
        self._buffer = ""
        self._lines = []
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._process_finished)
        process.errorOccurred.connect(self._process_error)
        self._process = process
        self._set_busy(True)
        self.step_progress.setFormat({"scan": "Reading disc: %p%", "rip": "Preparing (MakeMKV analysing the disc): %p%",
                                      "strip": "Removing unticked tracks: %p%"}.get(phase, "%p%"))
        if phase in ("rip", "strip"):
            self._phase_started = time.monotonic()
            self._meter = ripstats.TransferMeter()
            self._strip_percent = 0
            self._stats_timer.start()
        process.start(program, args)

    def _read_output(self) -> None:
        if self._process is None:
            return
        self._buffer += bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._buffer = self._buffer.replace("\r", "\n").split("\n")
        for line in lines:
            if line:
                self._lines.append(line)
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if self._phase == "strip":
            if line.startswith("#GUI#progress"):
                self._strip_percent = int(line.split()[-1].rstrip("%") or 0)
                self.step_progress.setValue(self._strip_percent)
            elif line.startswith(("#GUI#warning", "#GUI#error")):
                self.log.appendPlainText(line.split(" ", 1)[-1])
            return
        progress = makemkv.parse_progress(line)
        if progress is None:
            return
        # While copying, the bar follows the bytes written (see _update_stats);
        # MakeMKV's own counter restarts for every preparation step.
        if progress.fraction is not None and not (self._phase == "rip" and "write_started" in self._report):
            self.step_progress.setValue(int(progress.fraction * 1000))
        if progress.task:
            self.status.setText(progress.task)
        if progress.step and self._phase == "rip":
            self.status.setText(f"{self._title_text()}: {progress.step}")
        if progress.message:
            self.log.appendPlainText(progress.message)
            if self._phase == "rip" and "turned out to be empty" in progress.message:
                self._report["empty_forced"] = self._report.get("empty_forced", 0) + 1

    def _process_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            program = self._process.program() if self._process else "program"
            self.log.appendPlainText(f"Could not start {program}.")
            QMessageBox.critical(self, "Rip Disc", f"Could not start {program}. Is it installed?")
            self._phase = "idle"
            self._set_busy(False)

    def _process_finished(self, exit_code: int, exit_status) -> None:
        if self._buffer:
            self._lines.append(self._buffer)
            self._handle_line(self._buffer)
            self._buffer = ""
        crashed = exit_status != QProcess.ExitStatus.NormalExit
        self._stats_timer.stop()
        phase, self._phase = self._phase, "idle"
        self._process = None
        if phase == "drives":
            self._drives_listed()
        elif phase == "scan":
            self._scanned(exit_code, crashed)
        elif phase == "rip":
            self._title_ripped(exit_code, crashed)
        elif phase == "strip":
            self._title_stripped(exit_code, crashed)
        if self._phase == "idle":
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.drive_combo, self.refresh_button, self.min_length, self.scan_button,
                       self.tree, self.output_edit):
            widget.setEnabled(not busy)
        self.rip_button.setEnabled(not busy and self.disc is not None)
        self.stop_button.setEnabled(busy)
        self.step_progress.setRange(0, 1000)
        # Progress bars only while something runs; the title bar only while ripping.
        ripping = busy and self._phase in ("rip", "strip")
        self.step_progress.setVisible(busy and self._phase != "drives")
        self.title_progress.setVisible(ripping or (self._phase in ("rip", "strip") and busy))
        if not busy:
            self.step_progress.setValue(0)

    @property
    def _makemkvcon(self) -> str:
        return app_settings.get(self.settings, "makemkvcon_path") or makemkv.MAKEMKVCON

    @property
    def _mkvmerge(self) -> str:
        return app_settings.get(self.settings, "mkvmerge_path") or makemkv.MKVMERGE

    # --- sharing drives with other Rip windows --------------------------------

    @property
    def device(self) -> str | None:
        index = self.drive_combo.currentIndex()
        return self.drives[index].device if 0 <= index < len(self.drives) else None

    def claims_drive(self) -> bool:
        """This window is using its drive: scanning, ripping, or holding a scanned disc."""
        return self.device is not None and (self._phase in ("scan", "rip", "strip") or self.disc is not None)

    def claimed_output(self) -> Path | None:
        if self.disc is None or not self.output_edit.text().strip():
            return None
        return Path(self.output_edit.text()).expanduser()

    def _devices_in_use(self) -> set[str]:
        return {other.device for other in self._others() if other is not self and other.claims_drive()}

    def _outputs_in_use(self) -> set[Path]:
        return {other.claimed_output() for other in self._others() if other is not self} - {None}

    # --- drives and scanning -----------------------------------------------

    def list_drives(self) -> None:
        self.disc_label.setText("Looking for drives…")
        self._run("drives", self._makemkvcon, makemkv.drives_args())

    def _drives_listed(self) -> None:
        self.drives = makemkv.parse_drives(self._lines)
        self.drive_combo.clear()
        for drive in self.drives:
            self.drive_combo.addItem(QIcon.fromTheme("drive-optical"), drive.label())
        in_use = self._devices_in_use()
        with_disc = [i for i, d in enumerate(self.drives) if d.has_disc]
        free = [i for i in with_disc if self.drives[i].device not in in_use]
        if not self.drives:
            self.disc_label.setText("No optical drive found.")
        elif not with_disc:
            self.disc_label.setText("No disc in the drive. Insert one and press the refresh button.")
        elif not free:
            self.drive_combo.setCurrentIndex(with_disc[0])
            self.disc_label.setText("Every drive with a disc is already used by another Rip window. "
                                    "Insert a disc in another drive and press the refresh button.")
        else:
            self.drive_combo.setCurrentIndex(free[0])
            self.scan()

    def scan(self) -> None:
        if not self.drives:
            return
        drive = self.drives[self.drive_combo.currentIndex()]
        if drive.device in self._devices_in_use():
            QMessageBox.information(self, "Rip Disc", f"{drive.device} is already used by another Rip window.")
            return
        self.setWindowTitle(f"Rip Disc — {drive.device}")
        self.disc = None
        self.tree.clear()
        self.disc_label.setText(f"Reading {drive.disc_label or 'disc'}… (this can take a minute)")
        self.status.setText("Scanning disc")
        self._run("scan", self._makemkvcon, makemkv.info_args(drive.index, self.min_length.value()))

    def _scanned(self, exit_code: int, crashed: bool) -> None:
        disc = makemkv.parse_info(self._lines)
        if crashed or not disc.titles:
            self.disc_label.setText("Could not read the disc (see the log below).")
            self.status.setText("")
            return
        self.disc = disc
        self._scanned_min_length = self.min_length.value()
        self.disc_label.setText(f"<b>{disc.name}</b> · {disc.kind} · {len(disc.titles)} titles")
        self.setWindowTitle(f"Rip Disc — {disc.name} ({self.device})")
        self.status.setText("Tick the titles and tracks to rip.")
        favorite = (makemkv.parse_languages(app_settings.get(self.settings, "makemkv_language"))[0]
                    or [makemkv.preferred_language()])
        pretick = app_settings.get(self.settings, "pretick")
        self.tree.blockSignals(True)
        for title in disc.titles:
            item = QTreeWidgetItem([f"Title {title.id} · {title.source}", title.duration,
                                    str(title.chapters or ""), title.size_text])
            item.setIcon(0, QIcon.fromTheme("media-optical"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)  # like MakeMKV: every title ticked
            item.setData(0, ID_ROLE, (title.id, None))
            for track in title.tracks:
                child = QTreeWidgetItem([f"{TRACK_KIND_NAMES[track.kind]}: {track.label()}"])
                child.setIcon(0, QIcon.fromTheme(TRACK_ICONS.get(track.kind, "unknown")))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                selected = makemkv.default_selected(track, favorite, pretick)
                child.setCheckState(0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
                child.setData(0, ID_ROLE, (title.id, track.id))
                tooltip = track.attributes.get(makemkv.A_DESCRIPTION, "")
                if track.is_forced_only:
                    tooltip += ("\n\nOnly the forced parts of this subtitle track. MakeMKV only writes it if the "
                                "disc really has forced subtitles; otherwise it is removed as empty.")
                child.setToolTip(0, tooltip)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            parent_index = self.tree.indexFromItem(item)
            for row in range(item.childCount()):
                self.tree.setFirstColumnSpanned(row, parent_index, True)  # full track names
        self.tree.blockSignals(False)
        base = Path(app_settings.get(self.settings, "rip_folder")).expanduser()
        name = makemkv.safe_folder_name(disc.name)
        output, number = base / name, 2
        taken = self._outputs_in_use()
        while output in taken:  # e.g. two discs of a set with the same name
            output, number = base / f"{name} ({number})", number + 1
        self.output_edit.setText(str(output))
        self._update_summary()

    # --- selection ------------------------------------------------------------

    def selection(self) -> list[tuple[Title, list[int]]]:
        """Ticked titles with their ticked track ids."""
        chosen = []
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            title_id = item.data(0, ID_ROLE)[0]
            tracks = [item.child(i).data(0, ID_ROLE)[1] for i in range(item.childCount())
                      if item.child(i).checkState(0) == Qt.CheckState.Checked]
            chosen.append((next(t for t in self.disc.titles if t.id == title_id), tracks))
        return chosen

    def _update_summary(self, *_args) -> None:
        if self.disc is None:
            self.summary.setText("")
            return
        chosen = self.selection()
        size = sum(title.size_bytes for title, _ in chosen)
        output = Path(self.output_edit.text()).expanduser()
        free = shutil.disk_usage(self._existing_parent(output)).free if self.output_edit.text() else 0
        text = f"{len(chosen)} title{'s' if len(chosen) != 1 else ''} · about {human_bytes(size)}"
        text += f" · {human_bytes(free)} free"
        if size > free:
            text = f"<span style='color:#d33'>{text} (not enough space)</span>"
        self.summary.setText(text)
        self.rip_button.setEnabled(self._phase == "idle" and bool(chosen))

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        while not path.exists() and path != path.parent:
            path = path.parent
        return path

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Rip into…", str(self._existing_parent(Path(self.output_edit.text()).expanduser())))
        if folder and self.disc is not None:
            self.output_edit.setText(str(Path(folder) / makemkv.safe_folder_name(self.disc.name)))

    # --- statistics ---------------------------------------------------------------

    def _update_stats(self) -> None:
        """Live figures like MakeMKV's: time, transfer rate, drive speed, amount."""
        if self._current is None:
            return
        title = self._current[0]
        elapsed = time.monotonic() - self._phase_started
        path = self._output / title.output_file
        if self._phase == "rip":
            try:
                written = path.stat().st_size
            except OSError:
                written = 0
            if not written:
                # MakeMKV prepares (BD+ processing, analysis) before writing anything.
                self.stats_label.setText(f"Elapsed {ripstats.format_duration(elapsed)}  ·  "
                                         "preparing: MakeMKV is analysing the disc before copying")
                return
            if "write_started" not in self._report:
                self._report["write_started"] = time.monotonic()
                self.step_progress.setFormat("This title: %p%")
                self.step_progress.setValue(0)
            self._meter.add(time.monotonic(), written)
            fraction = min(1.0, written / title.size_bytes) if title.size_bytes else 0.0
            self._set_overall(fraction)
            self.step_progress.setValue(max(self.step_progress.value(), int(fraction * 1000)))
            rate = self._meter.rate()
            parts = [f"Elapsed {ripstats.format_duration(elapsed)}"]
            remaining = self._meter.remaining(title.size_bytes)
            if remaining is not None and written:
                parts.append(f"about {ripstats.format_duration(remaining)} left")
            if rate:
                multiple = ripstats.speed_multiple(rate, self.disc.kind if self.disc else "")
                parts.append(f"{ripstats.format_rate(rate)} ({multiple:.1f}×)")
            parts.append(f"{human_bytes(written)} of about {human_bytes(title.size_bytes)} written")
            self.stats_label.setText("  ·  ".join(parts))
        elif self._phase == "strip":
            self._set_overall(1.0)
            size = self._report.get("ripped_size", 0)
            parts = [f"Removing tracks: elapsed {ripstats.format_duration(elapsed)}"]
            if self._strip_percent and elapsed > 1:
                rate = size * self._strip_percent / 100 / elapsed
                left = elapsed * (100 - self._strip_percent) / self._strip_percent
                parts += [f"about {ripstats.format_duration(left)} left", ripstats.format_rate(rate)]
            self.stats_label.setText("  ·  ".join(parts))

    def _set_overall(self, current_fraction: float) -> None:
        """Overall progress: finished titles plus the done part of the current one, by size."""
        current = self._current[0].size_bytes if self._current else 0
        done = self._done_bytes + max(0.0, min(1.0, current_fraction)) * current
        fraction = done / self._total_bytes if self._total_bytes else 0.0
        number = min(self._done_titles + 1, self._total_titles)
        self.title_progress.setFormat(f"All titles: {fraction:.0%}  ·  title {number} of {self._total_titles}")
        self.title_progress.setValue(int(fraction * 1000))

    def _disc_speed_unit(self) -> str:
        return "DVD speed" if self.disc and "dvd" in self.disc.kind.lower() else "Blu-ray speed"

    def _report_text(self, report: dict) -> str:
        title = report["title"]
        lines = [f"Title {title.id} ({title.duration}) → {report.get('file', title.output_file)}"
                 + (f"  ·  {human_bytes(report['size'])}" if report.get("size") else "")]
        if report.get("rip_seconds"):
            rate = report.get("rip_rate", 0.0)
            multiple = ripstats.speed_multiple(rate, self.disc.kind if self.disc else "")
            lines.append(f"    Ripped in {ripstats.format_duration(report['rip_seconds'])}: "
                         f"preparing {ripstats.format_duration(report.get('prepare_seconds', 0))}, "
                         f"copying {ripstats.format_duration(report.get('write_seconds', 0))} "
                         f"at {ripstats.format_rate(rate)} ({multiple:.1f}× {self._disc_speed_unit()})")
        if report.get("removed"):
            seconds = report.get("strip_seconds", 0)
            lines.append(f"    Removed {report['removed']} unticked track(s)"
                         + (f" in {ripstats.format_duration(seconds)}" if seconds >= 1 else ""))
        if report.get("kept"):
            lines.append(f"    {report['kept']} track(s) in the file")
        if report.get("unavailable"):
            lines.append(f"    Not on the disc: {len(report['unavailable'])} ticked \"forced only\" subtitle track(s) "
                         f"({', '.join(sorted(set(report['unavailable'])))}): this disc has no forced subtitles, "
                         "so MakeMKV removed them as empty")
        if report.get("problem"):
            lines.append(f"    Problem: {report['problem']}")
        return "\n".join(lines)

    # --- ripping ----------------------------------------------------------------

    def start_rip(self) -> None:
        chosen = self.selection()
        if not chosen:
            return
        empty = [title.id for title, tracks in chosen if not tracks]
        if empty:
            QMessageBox.warning(self, "Rip Disc", f"Title {empty[0]} has no tracks ticked.")
            return
        needs_strip = any(len(tracks) < len(title.tracks) for title, tracks in chosen)
        if needs_strip and shutil.which(self._mkvmerge) is None:
            QMessageBox.warning(self, "Rip Disc",
                                "Unticked tracks are removed after ripping with mkvmerge, which is not installed.\n\n"
                                "Install it with:  sudo pacman -S mkvtoolnix-cli\n"
                                "(or set its location in Settings → MakeMKV)\n\n"
                                "or tick all tracks of the titles you rip.")
            return
        output = Path(self.output_edit.text()).expanduser()
        if not self.output_edit.text().strip():
            return
        if self.device in self._devices_in_use():
            QMessageBox.warning(self, "Rip Disc", f"{self.device} is already used by another Rip window.")
            return
        if output in self._outputs_in_use():
            QMessageBox.warning(self, "Rip Disc", f"Another Rip window is saving into {output}. Choose another folder.")
            return
        existing = [t.output_file for t, _ in chosen if (output / t.output_file).exists()]
        if existing:
            QMessageBox.warning(self, "Rip Disc", f"{existing[0]} already exists in {output}.")
            return
        size = sum(title.size_bytes for title, _ in chosen)
        if size > shutil.disk_usage(self._existing_parent(output)).free:
            QMessageBox.warning(self, "Rip Disc", f"Not enough free space for about {human_bytes(size)}.")
            return
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.critical(self, "Rip Disc", f"Could not create {output}:\n{error}")
            return
        self._output = output
        self._queue = list(chosen)
        self._total_titles = len(chosen)
        self._done_titles = 0
        self._failures = []
        self._reports = []
        self._total_bytes = sum(max(title.size_bytes, 1) for title, _ in chosen)
        self._done_bytes = 0
        self._set_overall(0.0)
        self.log.appendPlainText(f"Ripping {self._total_titles} title(s) to {output}")
        self._next_title()

    def _title_text(self) -> str:
        return f"Title {self._current[0].id}" if self._current else ""

    def _next_title(self) -> None:
        if not self._queue:
            self._finished()
            return
        self._current = self._queue.pop(0)
        title = self._current[0]
        self._report = {"title": title, "file": title.output_file}
        self.stats_label.setText("")
        self.step_progress.setValue(0)
        self._set_overall(0.0)
        self.status.setText(f"Ripping title {title.id} ({title.duration}, {title.size_text})")
        # CacheLocation is already app specific (~/.cache/video-renamer/...).
        cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        profile = makemkv.write_profile(Path(cache) if cache else Path.home() / ".cache" / "video-renamer")
        drive = self.drives[self.drive_combo.currentIndex()]
        self._run("rip", self._makemkvcon,
                  makemkv.rip_args(drive.index, title.id, self._output, self._scanned_min_length, profile,
                                   app_settings.get(self.settings, "makemkv_cache_mb")))

    def _title_ripped(self, exit_code: int, crashed: bool) -> None:
        title, tracks = self._current
        path = self._output / title.output_file
        self._report["rip_seconds"] = time.monotonic() - self._phase_started
        if crashed or exit_code != 0 or not path.exists():
            self._failures.append(f"Title {title.id}: MakeMKV failed (exit code {exit_code})")
            self._report["problem"] = f"MakeMKV failed (exit code {exit_code}); see the log"
            self.log.appendPlainText(self._failures[-1])
            self._title_done()
            return
        size = path.stat().st_size
        now = time.monotonic()
        write_started = self._report.get("write_started", self._phase_started)
        writing = max(0.001, now - write_started)
        self._report.update(size=size, ripped_size=size, prepare_seconds=write_started - self._phase_started,
                            write_seconds=writing, rip_rate=size / writing)
        if len(tracks) == len(title.tracks) and shutil.which(self._mkvmerge) is None:
            self._title_done()
            return
        self._strip(title, tracks, path)  # also checks that every ticked track is in the file

    def _strip(self, title: Title, tracks: list[int], path: Path) -> None:
        """Drop the unticked tracks from the ripped file with mkvmerge."""
        try:
            info = subprocess.run([self._mkvmerge, "-J", str(path)], capture_output=True, text=True, timeout=120)
            file_tracks = json.loads(info.stdout).get("tracks", [])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self._failures.append(f"Title {title.id}: could not read the ripped file ({error}); kept all tracks")
            self._report["problem"] = "could not check the ripped file; all tracks kept"
            self.log.appendPlainText(self._failures[-1])
            self._title_done()
            return
        mapping = makemkv.match_tracks(title.tracks, file_tracks)
        by_id = {t.id: t for t in title.tracks}
        # MakeMKV only writes a "forced only" track when the disc has forced
        # subtitles, so a ticked one may legitimately be absent.
        skipped = [t for t in tracks if t not in mapping and by_id[t].is_forced_only]
        self._report["unavailable"] = [f"{by_id[t].attributes.get(makemkv.A_LANG_NAME, '') or 'no language'}"
                                       for t in skipped]
        if skipped:
            self.log.appendPlainText(f"Title {title.id}: {len(skipped)} \"forced only\" subtitle track(s) not written "
                                     "(the disc has no forced subtitles for them).")
        tracks = [t for t in tracks if t not in skipped]
        missing = [t for t in tracks if t not in mapping]
        if missing:
            names = ", ".join(by_id[t].label() for t in missing[:5])
            self.log.appendPlainText(
                f"Title {title.id}: {len(missing)} ticked track(s) were not in the ripped file ({names}); "
                "kept all tracks so nothing is lost.")
            self._failures.append(f"Title {title.id}: some ticked tracks missing, all tracks kept")
            self._report.update(problem=f"ticked track(s) missing from the file ({names}); all tracks kept",
                                kept=len(file_tracks))
            self._title_done()
            return
        keep = [mapping[t] for t in tracks]
        self._report.update(kept=len(keep), removed=len(file_tracks) - len(keep))
        if len(keep) == len(file_tracks):
            self._title_done()
            return
        self._strip_temp = path.with_name(f".vr-strip-{path.name}")
        self.status.setText(f"Title {title.id}: removing {len(file_tracks) - len(keep)} unticked track(s)")
        self.step_progress.setValue(0)
        self.step_progress.setRange(0, 100)
        self._run("strip", self._mkvmerge,
                  ["--gui-mode", *makemkv.mkvmerge_keep_args(path, self._strip_temp, keep, file_tracks)])
        self.step_progress.setRange(0, 100)

    def _title_stripped(self, exit_code: int, crashed: bool) -> None:
        title = self._current[0]
        temp, self._strip_temp = self._strip_temp, None
        # mkvmerge: 0 = ok, 1 = ok with warnings, 2 = error
        self._report["strip_seconds"] = time.monotonic() - self._phase_started
        if not crashed and exit_code in (0, 1) and temp is not None and temp.exists():
            os.replace(temp, self._output / title.output_file)
            self._report["size"] = (self._output / title.output_file).stat().st_size
        else:
            if temp is not None and temp.exists():
                temp.unlink()
            self._failures.append(f"Title {title.id}: removing tracks failed; kept all tracks")
            self._report.update(problem="removing the unticked tracks failed; all tracks kept", removed=0)
            self.log.appendPlainText(self._failures[-1])
        self._title_done()

    def _title_done(self) -> None:
        if self._current is not None:
            self._done_bytes += max(self._current[0].size_bytes, 1)
        if self._report:
            self._reports.append(self._report)
            self._report = {}
        self._done_titles += 1
        self._current = None
        self.step_progress.setRange(0, 1000)
        self._next_title()

    def _finished(self) -> None:
        self.title_progress.setFormat(f"All titles: 100%  ·  {self._total_titles} of {self._total_titles} done")
        self.title_progress.setValue(1000)
        self.step_progress.setValue(self.step_progress.maximum())
        ok = self._total_titles - len([f for f in self._failures if "MakeMKV failed" in f])
        self.status.setText(f"Done: {ok} of {self._total_titles} title(s) ripped to {self._output}")
        total_time = sum(r.get("rip_seconds", 0) + r.get("strip_seconds", 0) for r in self._reports)
        total_size = sum(r.get("size", 0) for r in self._reports)
        self.stats_label.setText(f"Total {ripstats.format_duration(total_time)}  ·  {human_bytes(total_size)}")
        report = "\n\n".join(self._report_text(r) for r in self._reports)
        self.log.appendPlainText("\n=== Rip report ===\n" + report)
        box = QMessageBox(QMessageBox.Icon.Warning if self._failures else QMessageBox.Icon.Information,
                          "Rip Disc", self.status.text(), QMessageBox.StandardButton.Ok, self)
        box.setInformativeText(f"Total time {ripstats.format_duration(total_time)}, {human_bytes(total_size)}.")
        box.setDetailedText(report)
        box.setModal(False)
        box.show()
        if ok:
            self.ripFinished.emit(self._output)

    def stop(self) -> None:
        if self._process is None:
            return
        if QMessageBox.question(self, "Rip Disc", "Stop ripping? The unfinished file is deleted.") \
                != QMessageBox.StandardButton.Yes:
            return
        self._abort()

    def _abort(self) -> None:
        phase = self._phase
        self._stats_timer.stop()
        self._queue = []
        process, self._process = self._process, None
        self._phase = "idle"
        if process is not None:
            process.finished.disconnect()
            process.kill()
            process.waitForFinished(5000)
        if phase == "rip" and self._current is not None:
            partial = self._output / self._current[0].output_file
            if partial.exists():
                partial.unlink()
        if self._strip_temp is not None and self._strip_temp.exists():
            self._strip_temp.unlink()
        self._strip_temp = None
        self._current = None
        self.status.setText("Stopped.")
        self.log.appendPlainText("Stopped by user.")
        self._set_busy(False)

    def closeEvent(self, event) -> None:
        if self._process is not None and self._phase in ("rip", "strip"):
            if QMessageBox.question(self, "Rip Disc", "A rip is running. Stop it and close?") \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._abort()
        elif self._process is not None:
            self._abort()
        super().closeEvent(event)
