"""Main window: file list on the left, player and rename box on the right."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QEvent, QFile, QFileSystemWatcher, QModelIndex, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDrag, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter, QTreeView, QVBoxLayout, QWidget,
)

from . import renamer
from .batch_dialog import BatchRenameDialog
from .discdb_dialog import DiscDbDialog
from .export_dialog import ExportDialog
from .file_model import COL_NAME, IS_DIR_ROLE, PATH_ROLE, VideoTreeModel, VideoTreeProxy, is_video
from .player import SEEK_STEP_MS, PlayerWidget
from .renamer import RenameOp
from .rip_dialog import RipDialog, human_bytes
from . import settings as app_settings
from .settings_dialog import SettingsDialog
from .export import folder_size

APP_NAME = "Video Renamer"


class _NameEditKeys(QObject):
    """Enter renames, Ctrl+Enter renames and moves to the next file."""

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.window.rename_current(next_after=bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
            return True
        return False


class FileTree(QTreeView):
    """The file tree, with drag and drop to move files and folders on disk.

    Drops are handled here instead of by the model: a model move would only
    shuffle rows, while this moves the real files (through the window, so
    undo, the player and the tree refresh all work as for other moves).
    """

    moveRequested = Signal(list, object)  # (paths, target folder)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAutoExpandDelay(700)  # hovering a folder while dragging opens it
        self.root: Path | None = None

    def dragged_paths(self) -> list[Path]:
        """Selected items, without those inside another selected folder."""
        paths = [index.data(PATH_ROLE) for index in self.selectionModel().selectedRows(COL_NAME)]
        return [p for p in paths if not any(p != q and p.is_relative_to(q) for q in paths)]

    def drop_target(self, position) -> Path | None:
        """Folder a drop at position goes into: the folder under the cursor,
        the folder of the file under it, or the open folder for empty space."""
        index = self.indexAt(position)
        if not index.isValid():
            return self.root
        path = index.siblingAtColumn(COL_NAME).data(PATH_ROLE)
        return path if index.siblingAtColumn(COL_NAME).data(IS_DIR_ROLE) else path.parent

    @staticmethod
    def can_move(paths: list[Path], target: Path | None) -> bool:
        if target is None or not paths:
            return False
        if any(target == p or target.is_relative_to(p) for p in paths):
            return False  # a folder into itself
        return any(p.parent != target for p in paths)

    def startDrag(self, supported_actions) -> None:
        # Not QAbstractItemView's version: after a move it deletes the rows
        # itself, but here the tree is rebuilt from disk once the move is done.
        indexes = self.selectionModel().selectedRows(COL_NAME)
        if not indexes:
            return
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData(indexes))
        count = len(self.dragged_paths())
        drag.setPixmap(QIcon.fromTheme("folder-move" if count > 1 else "video-x-generic").pixmap(32, 32))
        drag.exec(Qt.DropAction.MoveAction)

    def _accept(self, event) -> bool:
        if event.source() is not self:
            return False
        return self.can_move(self.dragged_paths(), self.drop_target(event.position().toPoint()))

    def dragEnterEvent(self, event) -> None:
        if event.source() is self:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()  # files dragged in from outside are handled by the window

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)  # auto-scroll, auto-expand, drop indicator
        if self._accept(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if not self._accept(event):
            event.ignore()
            return
        target = self.drop_target(event.position().toPoint())
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.moveRequested.emit(self.dragged_paths(), target)


class MainWindow(QMainWindow):
    def __init__(self, folder: Path | None = None):
        super().__init__()
        self.settings = QSettings("video-renamer", "video-renamer")
        # Each entry: the renames applied and the folders created by them.
        # Each undo entry is a list of steps (renames applied, folders created);
        # steps are undone last-first, one batch each, because a later step
        # may depend on an earlier one (e.g. files renamed, then their folder).
        self.undo_stack: list[list[tuple[list[RenameOp], list[Path]]]] = []
        self._rip_dialogs: list[RipDialog] = []

        self.model = VideoTreeModel(self)
        self.model.renameRequested.connect(self._on_inline_rename, Qt.ConnectionType.QueuedConnection)
        self.proxy = VideoTreeProxy(self)
        self.proxy.setSourceModel(self.model)
        self._rebuilding = False
        self._shown_path: Path | None = None

        # Rebuild the tree when something changes on disk (also changes made
        # outside the app). Bursts of changes are collapsed into one refresh.
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _path: self._refresh_timer.start())
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self._on_disk_changed)

        self._build_ui()
        self._build_actions()
        self._restore_settings()

        if folder is None:
            if app_settings.get(self.settings, "start_folder_mode") == "fixed":
                folder = Path(app_settings.get(self.settings, "start_folder")).expanduser()
            else:
                last = self.settings.value("last_folder", "", str)
                folder = Path(last) if last else None
        if folder is not None and folder.is_dir():
            self.load_folder(folder)
        else:
            self._update_title()

    # --- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by name…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)

        self.table = FileTree()
        self.table.moveRequested.connect(self.move_paths, Qt.ConnectionType.QueuedConnection)
        self.table.setModel(self.proxy)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._setup_header()
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.selectionModel().currentRowChanged.connect(self._on_current_changed)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.filter_edit)
        left_layout.addWidget(self.table, 1)

        self.player = PlayerWidget()
        self.player.errorOccurred.connect(lambda text: self.statusBar().showMessage(f"Playback error: {text}", 8000))

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("New name (use / to put it in a subfolder)")
        self.name_edit.setEnabled(False)
        self.name_edit.installEventFilter(_NameEditKeys(self))
        self.ext_label = QLabel()
        self.rename_button = QPushButton("Rename")
        self.rename_button.setToolTip("Rename (Enter)")
        self.rename_button.clicked.connect(lambda: self.rename_current())
        self.rename_next_button = QPushButton("Rename && Next")
        self.rename_next_button.setToolTip("Rename and go to the next file (Ctrl+Enter)")
        self.rename_next_button.clicked.connect(lambda: self.rename_current(next_after=True))
        for button in (self.rename_button, self.rename_next_button):
            button.setEnabled(False)

        rename_row = QHBoxLayout()
        rename_row.addWidget(self.name_edit, 1)
        rename_row.addWidget(self.ext_label)
        rename_row.addWidget(self.rename_button)
        rename_row.addWidget(self.rename_next_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.player, 1)
        right_layout.addLayout(rename_row)

        self.splitter = QSplitter()
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.setCentralWidget(self.splitter)

        self.folder_label = QLabel()
        self.statusBar().addPermanentWidget(self.folder_label)
        self.setAcceptDrops(True)
        self.resize(1300, 760)

    def _action(self, text, slot, shortcut=None, icon=None, checkable=False) -> QAction:
        action = QAction(text, self)
        if icon:
            action.setIcon(QIcon.fromTheme(icon))
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        action.triggered.connect(slot)
        self.addAction(action)  # keep shortcuts working even with the menu bar hidden
        return action

    def _build_actions(self) -> None:
        self.open_act = self._action("&Open Folder…", self.choose_folder, QKeySequence.StandardKey.Open, "document-open-folder")
        self.refresh_act = self._action("&Refresh", self.refresh, QKeySequence.StandardKey.Refresh, "view-refresh")
        quit_act = self._action("&Quit", self.close, QKeySequence.StandardKey.Quit, "application-exit")
        self.rip_act = self._action("&Rip Disc…", self.rip_disc, "Ctrl+D", "media-optical")
        self.export_act = self._action("&Export to NAS…", self.export_folder, "Ctrl+E", "document-export")

        self.rename_act = self._action("&Rename in List", self.edit_in_list, "F2", "edit-rename")
        self.batch_act = self._action("&Batch Rename…", self.batch_rename, "Ctrl+B", "edit-rename")
        self.new_folder_act = self._action("&New Folder…", self.new_folder, "Ctrl+Shift+N", "folder-new")
        self.move_act = self._action("&Move to Folder…", self.move_to_folder, "Ctrl+M", "folder-move")
        self.discdb_act = self._action("Match with &TheDiscDB…", self.match_discdb, "Ctrl+T", "edit-find")
        self.delete_act = self._action("&Delete…", self.delete_selected, QKeySequence.StandardKey.Delete, "edit-delete")
        self.settings_act = self._action("&Configure Video Renamer…", self.open_settings,
                                         QKeySequence.StandardKey.Preferences, "configure")
        self.undo_act = self._action("&Undo Rename", self.undo, QKeySequence.StandardKey.Undo, "edit-undo")
        self.undo_act.setEnabled(False)
        focus_name = self._action("Edit &Name", self._focus_name_edit, "Ctrl+L")

        play_act = self._action("&Play/Pause", self.player.toggle_play, "Space", "media-playback-start")
        back_act = self._action("Seek &Back 5s", lambda: self.player.seek(-SEEK_STEP_MS), "Left", "media-seek-backward")
        fwd_act = self._action("Seek &Forward 5s", lambda: self.player.seek(SEEK_STEP_MS), "Right", "media-seek-forward")
        prev_chapter = self._action("Pre&vious Chapter", lambda: self.player.step_chapter(-1), "Ctrl+Left", "media-skip-backward")
        next_chapter = self._action("&Next Chapter", lambda: self.player.step_chapter(1), "Ctrl+Right", "media-skip-forward")

        file_menu = self.menuBar().addMenu("&File")
        for action in (self.open_act, self.refresh_act):
            file_menu.addAction(action)
        file_menu.addSeparator()
        for action in (self.rip_act, self.export_act):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(quit_act)

        edit_menu = self.menuBar().addMenu("&Edit")
        for action in (self.rename_act, focus_name, self.batch_act, self.discdb_act):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        for action in (self.new_folder_act, self.move_act):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.undo_act)

        edit_menu.addSeparator()
        edit_menu.addAction(self.delete_act)

        play_menu = self.menuBar().addMenu("&Playback")
        for action in (play_act, back_act, fwd_act, prev_chapter, next_chapter):
            play_menu.addAction(action)

        settings_menu = self.menuBar().addMenu("&Settings")
        settings_menu.addAction(self.settings_act)

        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("main_toolbar")
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for action in (self.rip_act, self.open_act, self.refresh_act, self.batch_act, self.discdb_act,
                       self.new_folder_act, self.move_act, self.undo_act, self.export_act):
            toolbar.addAction(action)

    # --- settings --------------------------------------------------------

    def _restore_settings(self) -> None:
        s = self.settings
        if s.contains("geometry"):
            self.restoreGeometry(s.value("geometry"))
        if s.contains("window_state"):
            self.restoreState(s.value("window_state"))
        if s.contains("splitter"):
            self.splitter.restoreState(s.value("splitter"))
        else:
            self.splitter.setSizes([520, 780])
        self.player.volume.setValue(s.value("volume", 80, int))
        self.player.mute_button.setChecked(s.value("muted", False, bool))

    def closeEvent(self, event) -> None:
        # A running rip/export asks before it is stopped; keep the window if refused.
        for dialog in self.findChildren(QDialog):
            if dialog.isVisible() and not dialog.close():
                event.ignore()
                return
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("window_state", self.saveState())
        s.setValue("splitter", self.splitter.saveState())
        s.setValue("volume", self.player.volume.value())
        s.setValue("muted", self.player.mute_button.isChecked())
        self.player.unload()
        self.player.shutdown()
        super().closeEvent(event)

    # --- folder loading --------------------------------------------------

    def choose_folder(self) -> None:
        start = str(self.model.root) if self.model.root else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Open Folder", start)
        if folder:
            self.load_folder(Path(folder))

    def refresh(self) -> None:
        self._rebuild()

    def load_folder(self, folder: Path) -> None:
        self.player.unload()
        self.model.reset()
        self.model.root = folder
        self.table.root = folder
        self._shown_path = None
        self._rebuild(expand_top=True)
        self.settings.setValue("last_folder", str(folder))
        self._update_title()
        count = len(self.model.entries)
        self.statusBar().showMessage(f"{count} video{'s' if count != 1 else ''} found", 5000)
        files = self._files_in_order()
        if files:
            self.select_path(files[0])

    def _rebuild(self, keep: Path | None = None, mapping: dict[Path, Path] | None = None,
                 load_player: bool = True, expand_top: bool = False) -> None:
        """Rescan the open folder and rebuild the tree, keeping expanded
        folders, selection, current item and scroll position. mapping
        translates old paths to new ones after a rename/move."""
        root = self.model.root
        if root is None:
            return
        mapping = mapping or {}
        moved = lambda path: renamer.translate(path, mapping)
        expanded = {moved(p) for p in self._expanded_paths()}
        selected = [moved(p) for p in self._selected_paths()]
        current = keep if keep is not None else (moved(self.current_path()) if self.current_path() else None)
        scroll = self.table.verticalScrollBar().value()

        self._rebuilding = True
        # Setting the current item auto-scrolls to it, which would reopen
        # collapsed folders; select_path(reveal=...) decides that instead.
        self.table.setAutoScroll(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        # Detach the sort/filter proxy while rebuilding so it resorts once, not per row.
        self.proxy.setSourceModel(None)
        try:
            self.model.load(root)
        except OSError as error:
            self.model.reset()
            QMessageBox.warning(self, APP_NAME, f"Could not read {root}:\n{error}")
        finally:
            self.proxy.setSourceModel(self.model)
            self._setup_header()
            QApplication.restoreOverrideCursor()
        try:
            if expand_top:
                self.table.expandToDepth(0)
            for path in expanded:
                index = self.proxy.mapFromSource(self.model.index_for(path))
                if index.isValid():
                    self.table.expand(index)
            if current is not None:
                # Only open folders when something was renamed/moved; a plain
                # refresh must not undo folders the user collapsed.
                self.select_path(current, reveal=bool(mapping) or keep is not None)
            selection = self.table.selectionModel()
            for path in selected:
                index = self.proxy.mapFromSource(self.model.index_for(path))
                if index.isValid():
                    selection.select(index, selection.SelectionFlag.Select | selection.SelectionFlag.Rows)
            if not mapping and keep is None:
                self.table.verticalScrollBar().setValue(scroll)
        finally:
            self._rebuilding = False
            self.table.setAutoScroll(True)
        self._watch_folders()
        self._current_changed(load_player)

    def _setup_header(self) -> None:
        # Re-applied after each rebuild: detaching the proxy drops the header's
        # per-column settings.
        header = self.table.header()
        header.setStretchLastSection(False)
        if header.count() > COL_NAME:
            header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
            for column in range(COL_NAME + 1, header.count()):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    def _watch_folders(self) -> None:
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        if self.model.root is not None:
            self.watcher.addPaths([str(self.model.root)] + [str(p) for p in self.model.folders])

    def _on_disk_changed(self) -> None:
        # Don't pull the tree out from under an open editor or dialog.
        if self.table.state() == QAbstractItemView.State.EditingState or QApplication.activeModalWidget():
            self._refresh_timer.start()
            return
        self._rebuild()

    def _on_filter_changed(self, text: str) -> None:
        self.proxy.set_filter_text(text)
        if text.strip():
            self.table.expandAll()

    def _update_title(self) -> None:
        root = self.model.root
        self.setWindowTitle(f"{root.name or root} — {APP_NAME}" if root else APP_NAME)
        self.folder_label.setText(str(root) if root else "")

    def dragEnterEvent(self, event) -> None:
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_dir():
                self.load_folder(path)
            elif path.is_file() and is_video(path.name):
                if self.model.root is None or not path.is_relative_to(self.model.root):
                    self.load_folder(path.parent)
                self.select_path(path)
            else:
                continue
            event.acceptProposedAction()
            return

    # --- selection -------------------------------------------------------

    def current_path(self) -> Path | None:
        """The current item in the tree: a file or a folder."""
        index = self.table.currentIndex()
        return index.siblingAtColumn(COL_NAME).data(PATH_ROLE) if index.isValid() else None

    def current_file(self) -> Path | None:
        path = self.current_path()
        return path if path in self.model.entries else None

    def select_path(self, path: Path, reveal: bool = True) -> bool:
        """Make path the current item. reveal expands its folders and scrolls to it."""
        index = self.proxy.mapFromSource(self.model.index_for(path))
        if not index.isValid():
            return False  # unknown, or hidden by the filter
        if reveal:
            parent = index.parent()
            while parent.isValid():
                self.table.expand(parent)
                parent = parent.parent()
        self.table.setCurrentIndex(index)
        if reveal:
            self.table.scrollTo(index)
        return True

    def _walk(self, parent: QModelIndex = QModelIndex()):
        """Name-column indexes of the visible (filtered, sorted) tree, depth first."""
        for row in range(self.proxy.rowCount(parent)):
            index = self.proxy.index(row, COL_NAME, parent)
            yield index
            yield from self._walk(index)

    def _files_in_order(self) -> list[Path]:
        return [index.data(PATH_ROLE) for index in self._walk() if not index.data(IS_DIR_ROLE)]

    def _expanded_paths(self) -> list[Path]:
        return [index.data(PATH_ROLE) for index in self._walk()
                if index.data(IS_DIR_ROLE) and self.table.isExpanded(index)]

    def _selected_paths(self) -> list[Path]:
        return [index.data(PATH_ROLE) for index in self.table.selectionModel().selectedRows(COL_NAME)]

    def _neighbor_path(self, step: int) -> Path | None:
        """The next/previous item of the same kind (file or folder) in tree
        order, also inside collapsed folders."""
        current = self.current_path()
        is_dir = current in self.model.folders
        items = [index.data(PATH_ROLE) for index in self._walk() if bool(index.data(IS_DIR_ROLE)) == is_dir]
        if current not in items:
            return None
        position = items.index(current) + step
        return items[position] if 0 <= position < len(items) else None

    def _on_current_changed(self, _current, _previous) -> None:
        if not self._rebuilding:
            self._current_changed()

    def _current_changed(self, load_player: bool = True) -> None:
        path = self.current_path()
        if path != self._shown_path:
            self._shown_path = path
            self._update_name_box(path)
        # Selecting a folder keeps the current video in the player.
        file = self.current_file()
        if load_player and file is not None and file != self.player.path:
            self.player.load(file)

    def _suffix(self, path: Path) -> str:
        """The part of the name that renaming keeps: a file's extension, nothing for folders."""
        return "" if path in self.model.folders else path.suffix

    def _update_name_box(self, path: Path | None) -> None:
        enabled = path is not None
        is_dir = path in self.model.folders
        self.name_edit.setText(self.model.editable_name(path, is_dir) if path else "")
        self.ext_label.setText(self._suffix(path) if path else "")
        self.ext_label.setVisible(bool(self.ext_label.text()))
        self.name_edit.setPlaceholderText(
            "New folder name (use / to move it into a subfolder)" if is_dir
            else "New name (use / to put it in a subfolder)"
        )
        for widget in (self.name_edit, self.rename_button, self.rename_next_button):
            widget.setEnabled(enabled)
        if enabled and self.name_edit.hasFocus():
            self.name_edit.selectAll()

    def _focus_name_edit(self) -> None:
        if self.name_edit.isEnabled():
            self.name_edit.setFocus()
            self.name_edit.selectAll()

    # --- renaming --------------------------------------------------------

    def rename_current(self, next_after: bool = False) -> None:
        path = self.current_path()
        if path is None:
            return
        try:
            new_path = renamer.target_for(path, self.name_edit.text(), self._suffix(path))
        except renamer.RenameError as error:
            QMessageBox.warning(self, "Cannot Rename", f"{path.name}: {error}")
            return
        next_path = self._neighbor_path(1) if next_after else None
        if not self.do_renames([RenameOp(path, new_path)]):
            return
        if next_path is not None:
            self.select_path(next_path)
        if next_after:
            self._focus_name_edit()

    def _on_double_clicked(self, index) -> None:
        # Double-click on a folder opens/closes it (built in); on a file it renames.
        if not index.data(IS_DIR_ROLE):
            self.edit_in_list()

    def _show_context_menu(self, position) -> None:
        index = self.table.indexAt(position)
        on_item = index.isValid()
        files_selected = bool(self._selected_entries())
        menu = QMenu(self)
        rename = menu.addAction(QIcon.fromTheme("edit-rename"), "Rename", self.edit_in_list)
        rename.setShortcut(self.rename_act.shortcut())
        rename.setEnabled(on_item)
        batch = menu.addAction(QIcon.fromTheme("edit-rename"), "Batch Rename…", self.batch_rename)
        batch.setShortcut(self.batch_act.shortcut())
        batch.setEnabled(files_selected)
        move = menu.addAction(QIcon.fromTheme("folder-move"), "Move to Folder…", self.move_to_folder)
        move.setShortcut(self.move_act.shortcut())
        move.setEnabled(files_selected)
        menu.addSeparator()
        new_folder = menu.addAction(QIcon.fromTheme("folder-new"), "New Folder…", self.new_folder)
        new_folder.setShortcut(self.new_folder_act.shortcut())
        new_folder.setEnabled(self.model.root is not None)
        discdb_item = menu.addAction(QIcon.fromTheme("edit-find"), "Match with TheDiscDB…", self.match_discdb)
        discdb_item.setShortcut(self.discdb_act.shortcut())
        discdb_item.setEnabled(self._export_source() is not None)
        export_item = menu.addAction(QIcon.fromTheme("document-export"), "Export to NAS…", self.export_folder)
        export_item.setShortcut(self.export_act.shortcut())
        export_item.setEnabled(self._export_source() is not None)
        menu.addSeparator()
        delete = menu.addAction(QIcon.fromTheme("edit-delete"), "Delete…", self.delete_selected)
        delete.setShortcut(self.delete_act.shortcut())
        delete.setEnabled(on_item)
        menu.addSeparator()
        undo = menu.addAction(QIcon.fromTheme("edit-undo"), "Undo", self.undo)
        undo.setShortcut(self.undo_act.shortcut())
        undo.setEnabled(self.undo_act.isEnabled())
        menu.exec(self.table.viewport().mapToGlobal(position))

    def edit_in_list(self) -> None:
        index = self.table.currentIndex()
        if index.isValid():
            self.table.edit(index.siblingAtColumn(COL_NAME))

    def _on_inline_rename(self, path: Path, text: str) -> None:
        try:
            new_path = renamer.target_for(path, text, self._suffix(path))
        except renamer.RenameError as error:
            QMessageBox.warning(self, "Cannot Rename", f"{path.name}: {error}")
            return
        self.do_renames([RenameOp(path, new_path)])

    def _selected_entries(self):
        """Selected files (folders ignored), in tree order."""
        selected = set(self._selected_paths())
        return [self.model.entries[path] for path in self._files_in_order() if path in selected]

    def batch_rename(self) -> None:
        selected = self._selected_entries()
        if not selected:
            QMessageBox.information(self, APP_NAME, "Select the files to rename first (Ctrl+A selects all).")
            return
        entries = [(entry.path, entry.mtime) for entry in selected]
        dialog = BatchRenameDialog(entries, self)
        if dialog.exec() == BatchRenameDialog.DialogCode.Accepted:
            self.do_renames(dialog.ops())

    def new_folder(self) -> None:
        root = self.model.root
        if root is None:
            return
        text, ok = QInputDialog.getText(
            self, "New Folder", f"Folder name, inside {root.name or root}\n(use / for nested folders):",
        )
        if not ok or not text.strip():
            return
        try:
            folder = renamer.folder_for(root, text)
            folder.mkdir(parents=True)
        except FileExistsError:
            QMessageBox.warning(self, "New Folder", f"'{text.strip()}' already exists.")
            return
        except (renamer.RenameError, OSError) as error:
            QMessageBox.warning(self, "New Folder", f"Could not create the folder:\n{error}")
            return
        self._rebuild()
        self.select_path(folder)
        self.statusBar().showMessage(f"Created folder {folder.relative_to(root)}", 5000)

    def move_to_folder(self) -> None:
        selected = self._selected_entries()
        if not selected:
            QMessageBox.information(self, APP_NAME, "Select the files to move first.")
            return
        count = len(selected)
        start = self.model.root or selected[0].path.parent
        folder = QFileDialog.getExistingDirectory(self, f"Move {count} file{'s' if count != 1 else ''} to…", str(start))
        if folder:
            self.do_renames([RenameOp(entry.path, Path(folder) / entry.path.name) for entry in selected])

    # --- disc ripping and NAS export -------------------------------------

    def rip_disc(self) -> None:
        """Open a Rip window. Each drive gets its own, so discs in several
        drives can be ripped at the same time; an unused window is reused."""
        self._rip_dialogs = [d for d in self._rip_dialogs if d.isVisible()]
        idle = next((d for d in self._rip_dialogs if not d.claims_drive()), None)
        if idle is not None:
            idle.raise_()
            idle.activateWindow()
            idle.list_drives()
            return
        dialog = RipDialog(self.settings, self, others=lambda: list(self._rip_dialogs))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.ripFinished.connect(self._on_rip_finished)
        dialog.destroyed.connect(lambda _obj=None, d=dialog: self._rip_dialogs.remove(d) if d in self._rip_dialogs else None)
        self._rip_dialogs.append(dialog)
        dialog.show()

    def _busy_here(self) -> bool:
        """The user is watching something or typing a new name."""
        playing = self.player.path is not None and not self.player.mpv.pause
        return playing or self.name_edit.hasFocus() or self.table.state() == QAbstractItemView.State.EditingState

    def _on_rip_finished(self, folder: Path) -> None:
        """Show the freshly ripped folder in the tree, ready for renaming. If
        the user is busy (another rip may finish mid-rename), only refresh."""
        root = self.model.root
        inside = root is not None and folder.is_relative_to(root)
        if inside and self._busy_here():
            self._rebuild()
            self.statusBar().showMessage(f"Rip finished: {folder.name} is in the tree", 10000)
            return
        if inside:
            self._rebuild()
        else:
            self.load_folder(folder.parent)
        self.select_path(folder)
        files = [p for p in self._files_in_order() if p.is_relative_to(folder)]
        if files:
            self.select_path(files[0])
        self.statusBar().showMessage(f"Ripped into {folder}", 8000)

    def match_discdb(self) -> None:
        """Compare a folder's videos with TheDiscDB and rename what the user approves."""
        folder = self._export_source()
        if folder is None:
            start = str(self.model.root or Path.home())
            chosen = QFileDialog.getExistingDirectory(self, "Folder to match with TheDiscDB", start)
            if not chosen:
                return
            folder = Path(chosen)
            if self.model.root is None or not folder.is_relative_to(self.model.root):
                self.load_folder(folder.parent)
        dialog = DiscDbDialog(folder, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ops, _problems = dialog.ops()
        self.apply_folder_plan(folder, ops, dialog.new_folder_name())

    def apply_folder_plan(self, folder: Path, ops: list[RenameOp], new_folder_name: str | None) -> None:
        """Rename files inside folder, then the folder itself, as one undo step."""
        steps = 0
        if ops:
            if not self.do_renames(ops):
                return
            steps += 1
        if new_folder_name:
            if not self.do_renames([RenameOp(folder, folder.with_name(new_folder_name))]):
                return
            steps += 1
        if steps == 2:  # merge into one undo entry (undo reverses in order)
            second, first = self.undo_stack.pop(), self.undo_stack.pop()
            self.undo_stack.append(first + second)
        count = len(ops) + (1 if new_folder_name else 0)
        self.statusBar().showMessage(f"TheDiscDB: renamed {count} item(s). Ctrl+Z undoes it all.", 8000)

    def _export_source(self) -> Path | None:
        """The folder Export works on: the selected folder."""
        path = self.current_path()
        return path if path in self.model.folders else None

    def export_folder(self) -> None:
        source = self._export_source()
        if source is None:
            QMessageBox.information(self, APP_NAME, "Select the folder to export in the tree first.")
            return
        dialog = ExportDialog(source, self.settings, self._release_folder, self)
        dialog.exported.connect(self._on_exported)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _release_folder(self, folder: Path) -> None:
        """Stop using files in folder before it is deleted."""
        if self.player.path is not None and self.player.path.is_relative_to(folder):
            self.player.unload()

    def _on_exported(self, folder: Path, deleted: bool) -> None:
        if deleted:
            self._rebuild()
            self.statusBar().showMessage(f"Exported and removed {folder.name}", 8000)
        else:
            self.statusBar().showMessage(f"Export of {folder.name} finished", 8000)

    # --- settings and deleting ---------------------------------------------

    def open_settings(self) -> None:
        SettingsDialog(self.settings, self).exec()

    def delete_selected(self) -> None:
        """Delete the selected files/folders (to the Trash unless set otherwise),
        after asking if "Ask before deleting" is on."""
        paths = self.table.dragged_paths() or ([self.current_path()] if self.current_path() else [])
        paths = [p for p in paths if p is not None and p.exists()]
        if not paths:
            return
        to_trash = app_settings.get(self.settings, "delete_to_trash")
        if app_settings.get(self.settings, "confirm_delete") and not self._confirm_delete(paths, to_trash):
            return
        for path in paths:
            self._release_folder(path)  # also releases a file that is itself being deleted
        failed = []
        for path in paths:
            try:
                if to_trash:
                    if not QFile.moveToTrash(str(path)):
                        raise OSError("could not move it to the Trash")
                elif path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as error:
                failed.append(f"{path.name}: {error}")
        self._rebuild()
        done = len(paths) - len(failed)
        verb = "Moved to the Trash" if to_trash else "Deleted"
        self.statusBar().showMessage(f"{verb}: {done} item{'s' if done != 1 else ''}", 6000)
        if failed:
            QMessageBox.warning(self, APP_NAME, "Some items could not be deleted:\n\n" + "\n".join(failed[:15]))

    def _confirm_delete(self, paths: list[Path], to_trash: bool) -> bool:
        size = sum(folder_size(p) if p.is_dir() else p.stat().st_size for p in paths)
        folders = sum(p.is_dir() for p in paths)
        files = len(paths) - folders
        what = ", ".join(part for part in (f"{files} file{'s' if files != 1 else ''}" if files else "",
                                            f"{folders} folder{'s' if folders != 1 else ''} with everything in "
                                            f"{'them' if folders != 1 else 'it'}"
                                            if folders else "") if part)
        names = "\n".join(f"  • {p.name}" for p in paths[:10]) + ("\n  …" if len(paths) > 10 else "")
        box = QMessageBox(QMessageBox.Icon.Warning, "Delete",
                          f"{'Move' if to_trash else 'Permanently delete'} {what} ({human_bytes(size)})"
                          f"{' to the Trash' if to_trash else ''}?", parent=self)
        box.setInformativeText(names + ("" if to_trash else "\n\nThis cannot be undone."))
        yes = box.addButton("Move to Trash" if to_trash else "Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dont_ask = QCheckBox("Don't ask again (can be turned back on in Settings)")
        box.setCheckBox(dont_ask)
        box.exec()
        if box.clickedButton() is not yes:
            return False
        if dont_ask.isChecked():
            app_settings.put(self.settings, "confirm_delete", False)
        return True

    def move_paths(self, paths: list[Path], target: Path) -> None:
        """Move files/folders into target (used by drag and drop)."""
        ops = [RenameOp(path, target / path.name) for path in paths if path.parent != target]
        if ops and self.do_renames(ops):
            count = len(ops)
            self.statusBar().showMessage(
                f"Moved {count} item{'s' if count != 1 else ''} to {target.name or target}", 5000)

    def undo(self) -> None:
        if not self.undo_stack:
            return
        steps = self.undo_stack[-1]
        while steps:
            applied, created = steps[-1]
            if not self.do_renames(renamer.undo_ops(applied), record_undo=False):
                break  # the steps undone so far stay undone; the rest can be retried
            renamer.remove_empty_dirs(created)
            steps.pop()
        if not steps:
            self.undo_stack.pop()
            self.statusBar().showMessage("Undone", 5000)
        self.undo_act.setEnabled(bool(self.undo_stack))

    def do_renames(self, ops: list[RenameOp], record_undo: bool = True) -> bool:
        ops = [op for op in ops if op.src != op.dst]
        if not ops:
            return True
        errors = renamer.plan_renames(ops)
        if errors:
            lines = [f"{src.name}: {message}" for src, message in list(errors.items())[:20]]
            if len(errors) > 20:
                lines.append(f"…and {len(errors) - 20} more")
            QMessageBox.warning(self, "Cannot Rename", "\n".join(lines))
            return False

        # Release the file in the player before renaming it, then pick up
        # playback again at the same position under the new name.
        resume = None
        playing = self.player.path
        if playing is not None and any(playing == op.src or playing.is_relative_to(op.src) for op in ops):
            resume = self.player.unload()  # the file itself or a folder it's in is renamed
        created: list[Path] = []
        try:
            applied = renamer.apply_renames(ops, created)
        except renamer.RenameError as error:
            QMessageBox.critical(self, "Rename Failed", f"Nothing was renamed.\n\n{error}")
            if resume:
                self.player.load(*resume)
            return False

        mapping = {op.src: op.dst for op in applied}
        self._rebuild(mapping=mapping, load_player=False)
        current = self.current_file()
        resumed = renamer.translate(resume[0], mapping) if resume else None
        if resumed in self.model.entries and current in (None, resumed):
            self.player.load(resumed, *resume[1:])  # same position, tracks and play state
        elif current is not None and current != self.player.path:
            self.player.load(current)
        if record_undo:
            self.undo_stack.append([(applied, created)])
        self.undo_act.setEnabled(bool(self.undo_stack))
        self._shown_path = self.current_path()
        self._update_name_box(self._shown_path)
        if len(applied) == 1:
            op = applied[0]
            where = f" in {op.dst.parent.name}" if op.dst.parent != op.src.parent else ""
            self.statusBar().showMessage(f"Renamed to {op.dst.name}{where}", 5000)
        else:
            self.statusBar().showMessage(f"Renamed/moved {len(applied)} files", 5000)
        return True
