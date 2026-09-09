"""
Qt glue between Software/matrix/eq_meter.py and MatrixController.

Owns an EqCapture instance and a QTimer, both started/stopped in lockstep
with display_settings.py's eq_meter_enabled (via SettingsController's
eqMeterEnabledChanged signal) -- there's no reason to run arecord, or tick
the FFT, while artwork mode is showing. All the actual audio-capture/
band-level/rendering logic lives in eq_meter.py (Qt-free, same as
encoder.py and matrix/link.py); this class is just the timer loop and the
on/off wiring.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer

from matrix.eq_meter import EqCapture, render_bars

from .matrix_controller import MatrixController
from .settings_controller import SettingsController

LOG = logging.getLogger(__name__)

# ~12fps. eq_meter.py's docstring covers why one FFT this small is cheap
# even on the Zero WH's single core; this rate is chosen for how it looks
# (fast enough to read as live movement, slow enough not to spam the
# serial link with frames the eye can't tell apart anyway) rather than
# being pushed by any actual performance ceiling.
TICK_MS = 80


class EqController(QObject):
    def __init__(
        self,
        settings: SettingsController,
        matrix: MatrixController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._matrix = matrix
        self._capture: Optional[EqCapture] = None

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

        settings.eqMeterEnabledChanged.connect(self._sync_running)
        self._sync_running()

    def _sync_running(self) -> None:
        if self._settings.eqMeterEnabled:
            if self._capture is None:
                LOG.info("EQ meter enabled -- starting audio capture")
                self._capture = EqCapture()
                self._capture.start()
            self._timer.start()
        else:
            self._timer.stop()
            if self._capture is not None:
                LOG.info("EQ meter disabled -- stopping audio capture")
                self._capture.stop()
                self._capture = None

    def _tick(self) -> None:
        if self._capture is None:
            return
        self._matrix.push_eq_frame(render_bars(self._capture.bands()))

    def shutdown(self) -> None:
        """Called from AppController.shutdown() so an app quit doesn't leave
        arecord running as an orphan subprocess."""
        self._timer.stop()
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
