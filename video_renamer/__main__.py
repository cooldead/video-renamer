import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .main_window import APP_ID, APP_NAME, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("video-renamer")
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon.fromTheme("video-x-generic")))  # generic when run from the source folder

    folder = None
    args = [a for a in app.arguments()[1:] if not a.startswith("-")]
    if args:
        path = Path(args[0]).expanduser().resolve()
        folder = path if path.is_dir() else path.parent

    window = MainWindow(folder)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
