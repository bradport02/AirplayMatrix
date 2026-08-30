"""
Root controller, exposed to QML as the single `app` context property.

PySide2 port of app/app_controller.py for the Pi Zero WH's Qt5 build. Wires
SettingsController -> TrackController -> LyricsController -> MatrixController
together and owns the ReceiverSupervisor lifecycle, same as the Qt6 version.
The one addition over that file is `settings`: the live lyrics/song-details
toggles this build exists to make possible (see
Software/display_settings.py's docstring for why).
"""

from __future__ import annotations

import logging
import platform
from typing import Callable

from PySide2.QtCore import Property, QObject, QTimer, Slot, Signal

from metadata import MetadataSource, PipeSource, TcpSource
from receiver import NullSupervisor, ReceiverSupervisor, WslProcessSupervisor

from .lyrics_controller import LyricsController
from .matrix_controller import MatrixController
from .settings_controller import SettingsController
from .track_controller import TrackController

LOG = logging.getLogger(__name__)

RECEIVER_POLL_MS = 1000


def _default_source_factory() -> Callable[[], MetadataSource]:
    # Windows has no native shairport-sync; it runs in WSL2 and sps_bridge.py
    # re-serves its metadata pipe over TCP. The Pi is co-hosted with a native
    # shairport-sync and reads the FIFO directly. Returning the class itself
    # (not an instance) matters: both constructors block until connected, so
    # instantiating here on the GUI thread would hang app startup.
    if platform.system() == "Windows":
        return TcpSource
    return PipeSource


def _default_supervisor() -> ReceiverSupervisor:
    if platform.system() == "Windows":
        return WslProcessSupervisor()
    return NullSupervisor()


class AppController(QObject):
    receiverRunningChanged = Signal()

    def __init__(
        self,
        source_factory: Callable[[], MetadataSource] | None = None,
        supervisor: ReceiverSupervisor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._supervisor = supervisor if supervisor is not None else _default_supervisor()
        self._receiver_running = False

        self._settings = SettingsController(self)
        self._track = TrackController(source_factory or _default_source_factory(), self)
        self._lyrics = LyricsController(self._track, self._settings, self)
        self._matrix = MatrixController(self._track, self)

        self._receiver_timer = QTimer(self)
        self._receiver_timer.setInterval(RECEIVER_POLL_MS)
        self._receiver_timer.timeout.connect(self._poll_receiver)

        self._supervisor.start()
        self._poll_receiver()
        self._receiver_timer.start()

    def _poll_receiver(self) -> None:
        running = self._supervisor.is_running
        if running != self._receiver_running:
            self._receiver_running = running
            self.receiverRunningChanged.emit()

    def _get_receiver_running(self) -> bool:
        return self._receiver_running

    def _get_track(self) -> TrackController:
        return self._track

    def _get_lyrics(self) -> LyricsController:
        return self._lyrics

    def _get_matrix(self) -> MatrixController:
        return self._matrix

    def _get_settings(self) -> SettingsController:
        return self._settings

    receiverRunning = Property(bool, _get_receiver_running, notify=receiverRunningChanged)
    track = Property(QObject, _get_track, constant=True)
    lyrics = Property(QObject, _get_lyrics, constant=True)
    matrix = Property(QObject, _get_matrix, constant=True)
    settings = Property(QObject, _get_settings, constant=True)

    @Slot()
    def shutdown(self) -> None:
        LOG.info("shutting down receiver supervisor")
        self._matrix.clear()
        self._supervisor.stop()
