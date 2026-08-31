"""
Root controller, exposed to QML as the single `app` context property.

Wires TrackController -> LyricsController -> MatrixController together and
owns the ReceiverSupervisor lifecycle: started at construction, stopped from
`shutdown()` (connected to QGuiApplication.aboutToQuit in main.py) so the
WSL-side processes don't outlive the window.
"""

from __future__ import annotations

import logging
import platform
from typing import Callable

from PySide6.QtCore import Property, QObject, QTimer, Slot, Signal

import airplay_name
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
    deviceNameChanged = Signal()

    def __init__(
        self,
        source_factory: Callable[[], MetadataSource] | None = None,
        supervisor: ReceiverSupervisor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._supervisor = supervisor if supervisor is not None else _default_supervisor()
        self._receiver_running = False
        self._device_name_mtime = airplay_name.mtime()
        self._device_name = airplay_name.read()

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

        # Piggybacks on this same 1s tick rather than a timer of its own --
        # a rename is rare and this is just a stat() until it happens (see
        # airplay_name.mtime()), so it's not worth a second QTimer.
        name_mtime = airplay_name.mtime()
        if name_mtime != self._device_name_mtime:
            self._device_name_mtime = name_mtime
            name = airplay_name.read()
            if name != self._device_name:
                self._device_name = name
                self.deviceNameChanged.emit()

    def _get_receiver_running(self) -> bool:
        return self._receiver_running

    def _get_device_name(self) -> str:
        return self._device_name

    def _get_track(self) -> TrackController:
        return self._track

    def _get_lyrics(self) -> LyricsController:
        return self._lyrics

    def _get_matrix(self) -> MatrixController:
        return self._matrix

    def _get_settings(self) -> SettingsController:
        return self._settings

    receiverRunning = Property(bool, _get_receiver_running, notify=receiverRunningChanged)
    deviceName = Property(str, _get_device_name, notify=deviceNameChanged)
    track = Property(QObject, _get_track, constant=True)
    lyrics = Property(QObject, _get_lyrics, constant=True)
    matrix = Property(QObject, _get_matrix, constant=True)
    settings = Property(QObject, _get_settings, constant=True)

    @Slot()
    def shutdown(self) -> None:
        LOG.info("shutting down receiver supervisor")
        self._matrix.clear()
        self._supervisor.stop()
