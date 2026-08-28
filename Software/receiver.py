"""
AirPlay receiver process supervisor.

Host abstraction: on the Pi, shairport-sync is expected to be managed
externally (systemd), co-hosted with this app, so there is nothing for the
app to start. On Windows there is no such service, so the app takes on that
responsibility and starts/stops shairport-sync + sps_bridge.py inside WSL2
itself -- this is what makes "discoverable on launch" possible on Windows
without a separate manual step.

WslProcessSupervisor spawns raw processes: no systemd unit exists inside WSL
for either program yet (that's the separate provisioning step). Killing the
Windows-side wsl.exe launcher does not reliably kill what it started inside
the WSL VM, so stop() reaches back in with `wsl.exe ... pkill` rather than
relying on terminating the launcher process.
"""

from __future__ import annotations

import abc
import logging
import subprocess
from typing import Optional, Sequence

LOG = logging.getLogger(__name__)

DEFAULT_DISTRO = "Ubuntu"

# shairport-sync is built from source into /usr/local/bin (on $PATH) and
# reads /etc/shairport-sync.conf (sysconfdir was set at build time), so no
# flags are needed here. sps_bridge.py is deployed to a fixed path inside
# the WSL filesystem rather than relying on wsl.exe's directory mapping --
# redeploy it there by hand if this file changes, until a proper install
# step exists.
DEFAULT_SHAIRPORT_CMD = ["shairport-sync"]
DEFAULT_BRIDGE_CMD = ["python3", "/opt/airplay-desk/sps_bridge.py"]

STOP_GRACE_SECONDS = 3.0


class ReceiverSupervisor(abc.ABC):
    """Starts and stops the AirPlay receiver stack for this host."""

    @abc.abstractmethod
    def start(self) -> None:
        """Bring the receiver stack up. Must not block indefinitely."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Tear the receiver stack down. Safe to call if never started."""

    @property
    @abc.abstractmethod
    def is_running(self) -> bool:
        """Best-effort liveness check, cheap enough to poll from a UI timer."""


class NullSupervisor(ReceiverSupervisor):
    """Pi target: the receiver is managed externally, so there is nothing
    for the app to do here."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    @property
    def is_running(self) -> bool:
        return True


class _WslProcess:
    """One command run inside a WSL distro via wsl.exe."""

    def __init__(self, distro: str, cmd: Sequence[str]) -> None:
        self._distro = distro
        self._cmd = list(cmd)
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        LOG.info("starting in WSL (%s): %s", self._distro, self._cmd)
        self._proc = subprocess.Popen(
            ["wsl.exe", "-d", self._distro, "--", *self._cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        # wsl.exe is a launcher: terminating it on the Windows side does not
        # reliably kill the process it started inside the VM. Reach in and
        # kill it by command-line match instead.
        marker = " ".join(self._cmd)
        try:
            subprocess.run(
                ["wsl.exe", "-d", self._distro, "--", "pkill", "-f", marker],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=STOP_GRACE_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("pkill -f %r failed: %s", marker, exc)
        try:
            self._proc.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class WslProcessSupervisor(ReceiverSupervisor):
    """Windows target: spawns shairport-sync and sps_bridge.py as raw
    processes inside a WSL2 distro."""

    def __init__(
        self,
        distro: str = DEFAULT_DISTRO,
        shairport_cmd: Sequence[str] = DEFAULT_SHAIRPORT_CMD,
        bridge_cmd: Sequence[str] = DEFAULT_BRIDGE_CMD,
    ) -> None:
        self._shairport = _WslProcess(distro, shairport_cmd)
        self._bridge = _WslProcess(distro, bridge_cmd)

    def start(self) -> None:
        self._shairport.start()
        self._bridge.start()

    def stop(self) -> None:
        # Stop the bridge first so it doesn't log a burst of read errors as
        # shairport-sync's metadata pipe disappears out from under it.
        self._bridge.stop()
        self._shairport.stop()

    @property
    def is_running(self) -> bool:
        return self._shairport.is_running and self._bridge.is_running
