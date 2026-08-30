"""
Entry point for the Pi Zero WH build of the AirPlay Desk Display app.

This is a PySide2/Qt5 port of app/main.py (the Pi 5's PySide6/Qt6 build) --
see docs/install-pi-zero-wh.md for why: PySide6 has no official armv6 wheel,
and Qt6 has no official ARMv6 build to apt-package it from either, so the
Zero WH needs a genuinely different Qt major version, not just a recompile.
The QML in qml/ is likewise a Qt5 port of app/qml/ -- same layout and look,
MultiEffect (Qt6-only) swapped for QtGraphicalEffects (see qml/Background.qml
and qml/AlbumArt.qml for what that changed).

Run as a module from the Software/ directory so the package's relative
imports and the sibling flat modules (metadata.py, encoder.py, ...) both
resolve correctly:

    python3 -m app_qt5.main
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from PySide2.QtCore import QCoreApplication, QTimer, QUrl, Qt
from PySide2.QtGui import QCursor, QGuiApplication
from PySide2.QtQml import QQmlApplicationEngine

from .app_controller import AppController

QML_DIR = Path(__file__).resolve().parent / "qml"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    QCoreApplication.setOrganizationName("AirplayDeskDisplay")
    QCoreApplication.setApplicationName("AirplayDeskDisplay")

    # No QQuickStyle.setStyle() call here, unlike app/main.py: Qt5's built-in
    # Controls 2 styles are Default/Fusion/Material/Universal/Imagine, not
    # Qt6's Basic, and none of this app's QML actually places a QQC2 widget
    # (ApplicationWindow is the only Controls import used, and that's
    # style-invariant) -- so there's nothing for a style choice to affect,
    # and no reason to risk picking one that behaves differently on Qt5.
    app = QGuiApplication(sys.argv)

    # This runs full-screen as a kiosk display on a touch-free box (see
    # airplaymatrix-run-qt5.sh) -- a mouse pointer sitting on screen has
    # nothing to point at and just looks like debug leftovers.
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

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
