"""Batch rename dialog with a live before -> after preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from . import renamer
from .renamer import RenameOp

TOKEN_HELP = (
    "<b>{n}</b> counter &nbsp; <b>{n:03}</b> counter with 3 digits &nbsp; "
    "<b>{name}</b> current name &nbsp; <b>{date}</b> modified date &nbsp; <b>{parent}</b> folder name"
    "<br><b>/</b> puts files in subfolders, e.g. <b>Season 1/S01E{n}</b>"
)


class BatchRenameDialog(QDialog):
    def __init__(self, entries: list[tuple[Path, float]], parent=None):
        """entries: (path, mtime) pairs in the order they should be numbered."""
        super().__init__(parent)
        self.setWindowTitle(f"Batch Rename — {len(entries)} files")
        self.resize(800, 520)
        self._entries = entries
        self._ops: list[RenameOp] = []

        self.pattern = QLineEdit("{name}")
        self.start = QSpinBox()
        self.start.setRange(0, 999_999)
        self.start.setValue(1)
        self.pad = QSpinBox()
        self.pad.setRange(1, 10)
        self.pad.setValue(2)
        self.pad.setToolTip("Minimum digits for {n}")
        self.find = QLineEdit()
        self.find.setPlaceholderText("Text to find (optional)")
        self.replace = QLineEdit()
        self.replace.setPlaceholderText("Replace with")
        self.regex = QCheckBox("Regular expression")
        self.case = QCheckBox("Match case")
        self.case.setChecked(True)

        counter_row = QHBoxLayout()
        counter_row.addWidget(QLabel("Start at"))
        counter_row.addWidget(self.start)
        counter_row.addSpacing(16)
        counter_row.addWidget(QLabel("Digits"))
        counter_row.addWidget(self.pad)
        counter_row.addStretch(1)

        options_row = QHBoxLayout()
        options_row.addWidget(self.regex)
        options_row.addWidget(self.case)
        options_row.addStretch(1)

        help_label = QLabel(TOKEN_HELP)
        help_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Pattern", self.pattern)
        form.addRow("", help_label)
        form.addRow("Counter", counter_row)
        form.addRow("Find", self.find)
        form.addRow("Replace", self.replace)
        form.addRow("", options_row)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Current name", "New name"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        self.summary = QLabel()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Rename")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)

        for edit in (self.pattern, self.find, self.replace):
            edit.textChanged.connect(self._refresh)
        for spin in (self.start, self.pad):
            spin.valueChanged.connect(self._refresh)
        for box in (self.regex, self.case):
            box.toggled.connect(self._refresh)

        self.pattern.setFocus()
        self.pattern.selectAll()
        self._refresh()

    def ops(self) -> list[RenameOp]:
        return self._ops

    def _build(self) -> tuple[list[RenameOp], dict[Path, str], str | None]:
        ops = []
        path_errors: dict[Path, str] = {}
        for i, (path, mtime) in enumerate(self._entries):
            stem = renamer.expand_pattern(
                self.pattern.text(),
                stem=path.stem,
                parent=path.parent.name,
                mtime=mtime,
                number=self.start.value() + i,
                pad=self.pad.value(),
            )
            try:
                stem = renamer.find_replace(
                    stem, self.find.text(), self.replace.text(),
                    regex=self.regex.isChecked(), case_sensitive=self.case.isChecked(),
                )
            except ValueError as error:
                return [], {}, str(error)
            try:
                ops.append(RenameOp(path, renamer.target_for(path, stem, path.suffix)))
            except renamer.RenameError as error:
                ops.append(RenameOp(path, path))  # shown unchanged, marked as an error
                path_errors[path] = str(error)
        errors = renamer.plan_renames(ops)
        errors.update(path_errors)
        return ops, errors, None

    @staticmethod
    def _display(op: RenameOp) -> str:
        """New name relative to the file's current folder, so moves show as "Folder/name"."""
        try:
            return str(op.dst.relative_to(op.src.parent))
        except ValueError:
            return str(op.dst)

    def _refresh(self) -> None:
        ops, errors, global_error = self._build()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if global_error:
            self.summary.setText(f"<span style='color:#d33'>{global_error}</span>")
            ok_button.setEnabled(False)
            return

        error_brush = QBrush(QColor("#d33"))
        self.table.setRowCount(len(ops))
        changed = 0
        for row, op in enumerate(ops):
            old_item = QTableWidgetItem(op.src.name)
            new_item = QTableWidgetItem(self._display(op))
            old_item.setToolTip(str(op.src))
            if op.src != op.dst and op.src not in errors:
                changed += 1
            else:
                new_item.setForeground(QBrush(self.palette().placeholderText().color()))
            message = errors.get(op.src)
            if message:
                new_item.setForeground(error_brush)
                new_item.setToolTip(message)
            self.table.setItem(row, 0, old_item)
            self.table.setItem(row, 1, new_item)

        self._ops = [op for op in ops if op.src != op.dst]
        parts = [f"{len(ops)} files", f"{changed} to rename"]
        if errors:
            parts.append(f"<span style='color:#d33'>{len(errors)} problem(s) — hover the red names</span>")
        self.summary.setText(", ".join(parts))
        ok_button.setEnabled(changed > 0 and not errors)
