"""
Entry point for the AirPlay Desk Display Qt/QML application.

Run as a module from the Claude/ directory so the package's relative
imports and the sibling flat modules (metadata.py, encoder.py, ...) both
resolve correctly:

    python -m app.main
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer, QUrl, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .app_controller import AppController

QML_DIR = Path(__file__).resolve().parent / "qml"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    QCoreApplication.setOrganizationName("AirplayDeskDisplay")
    QCoreApplication.setApplicationName("AirplayDeskDisplay")

    # Basic is the only built-in style that doesn't fight the custom theming
    # in qml/Theme.qml -- Fusion/Material both bring their own strong look.
    QQuickStyle.setStyle("Basic")

    app = QGuiApplication(sys.argv)

    # This runs full-screen as a kiosk display on a touch-free box (see
    # airplaymatrix-run.sh) -- a mouse pointer sitting on screen has nothing
    # to point at and just looks like debug leftovers.
    app.setOverrideCursor(QCursor(Qt.BlankCursor))

    # Qt on Linux doesn't install its own SIGTERM/SIGINT handlers, so left
    # alone the interpreter's default disposition just kills the process --
    # skipping aboutToQuit and controller.shutdown()'s cleanup. Handle both
    # signals by asking the event loop to quit normally instead. Python only
    # runs signal handlers between bytecode instructions, which never happens
    # while Qt's C++ loop is blocked waiting for events, so a no-op timer
    # ticks the interpreter often enough to notice. This is what lets the
    # remote's Shift+X hotkey (see /usr/local/bin/airplaymatrix-quit.sh) shut
    # the app down cleanly rather than killing it outright.
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal_wakeup = QTimer()
    signal_wakeup.start(200)
    signal_wakeup.timeout.connect(lambda: None)

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))

    controller = AppController()
    engine.rootContext().setContextProperty("app", controller)
    app.aboutToQuit.connect(controller.shutdown)

    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
