#!/usr/bin/env python3
"""
airplaymatrix-privileged.py — the ONLY thing the settings web UI is allowed
to run as root (via a narrowly-scoped sudoers NOPASSWD entry, see
/etc/sudoers.d/airplaymatrix-webui).

Design: the Flask app (webui/app.py) runs unprivileged as the airplaymatrix
user and never touches root-owned files or systemctl/nmcli/hostnamectl
directly. Every privileged action goes through one subcommand here, which
re-validates its own arguments (never trust the caller) with allowlist
regexes and never invokes a shell, so there is no injection surface even
though sudoers has to grant "any arguments" (subcommand arguments, like a
Wi-Fi SSID, can't be enumerated in advance).

Usage: airplaymatrix-privileged.py <subcommand> [args...]
Exit 0 + "OK" line on success; exit 1 + message on stderr on failure.

Deployed to /usr/local/bin/airplaymatrix-privileged.py, owned root:root,
mode 0700. This file is the source of record -- after editing it here,
reinstall with:
  sudo install -o root -g root -m 0700 airplaymatrix-privileged.py /usr/local/bin/airplaymatrix-privileged.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SHAIRPORT_CONF = Path("/etc/shairport-sync.conf")
HOSTS_FILE = Path("/etc/hosts")

# Only these two units may be restarted from the web UI.
RESTARTABLE_SERVICES = {"shairport-sync", "airplay-cec-remote"}

# Onboard status LED (Raspberry Pi 5: /sys/class/leds/ACT). This board's
# classdev brightness is inverted from the usual LED convention -- writing
# 0 lights it, 1 turns it off -- confirmed by hands-on testing, so "on"/
# "off" below are swapped to match physical reality rather than the sysfs
# name.
LED_DIR = Path("/sys/class/leds/ACT")
LED_STATES = {"on", "off"}
LED_BRIGHTNESS_FOR_STATE = {"on": "0", "off": "1"}

NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
SSID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,32}$")  # no control chars, <=32 bytes-ish


def fail(msg: str) -> "NoReturn":  # noqa: F821
    print(msg, file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o644)
    tmp.replace(path)


# -- shairport-sync.conf edits ------------------------------------------------

def _edit_conf(pattern: str, replacement: str) -> None:
    text = SHAIRPORT_CONF.read_text()
    new_text, n = re.subn(pattern, replacement, text, count=1)
    if n == 0:
        fail(f"pattern not found in {SHAIRPORT_CONF}: {pattern!r}")
    atomic_write(SHAIRPORT_CONF, new_text)


def cmd_set_airplay_name(args: argparse.Namespace) -> None:
    name = args.name
    if not NAME_RE.match(name):
        fail("invalid AirPlay name: use 1-64 letters, numbers, spaces, '.', '_', '-'")
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    _edit_conf(r'(name\s*=\s*")[^"]*(")', rf'\g<1>{escaped}\g<2>')
    r = run(["systemctl", "restart", "shairport-sync"])
    if r.returncode != 0:
        fail(f"name updated but restart failed: {r.stderr.strip()}")
    print("OK")


def cmd_set_tv_timeout(args: argparse.Namespace) -> None:
    try:
        seconds = float(args.seconds)
    except ValueError:
        fail("timeout must be a number")
    if not (5.0 <= seconds <= 3600.0):
        fail("timeout must be between 5 and 3600 seconds")
    _edit_conf(
        r"(active_state_timeout\s*=\s*)[0-9.]+(;)",
        rf"\g<1>{seconds:.1f}\g<2>",
    )
    r = run(["systemctl", "restart", "shairport-sync"])
    if r.returncode != 0:
        fail(f"timeout updated but restart failed: {r.stderr.strip()}")
    print("OK")


# -- hostname -----------------------------------------------------------------

def cmd_set_hostname(args: argparse.Namespace) -> None:
    new = args.name
    if not HOSTNAME_RE.match(new) or len(new) > 63:
        fail("invalid hostname: letters, numbers, '-' only, can't start/end with '-'")
    r = run(["hostnamectl", "set-hostname", new])
    if r.returncode != 0:
        fail(f"hostnamectl failed: {r.stderr.strip()}")
    if HOSTS_FILE.exists():
        text = HOSTS_FILE.read_text()
        new_text, n = re.subn(
            r"(?m)^(127\.0\.1\.1\s+)\S+", rf"\g<1>{new}", text, count=1
        )
        if n:
            atomic_write(HOSTS_FILE, new_text)
    run(["systemctl", "restart", "avahi-daemon"])
    print("OK (reboot recommended for the new name to fully take effect)")


# -- Wi-Fi ----------------------------------------------------------------

def cmd_wifi_connect(args: argparse.Namespace) -> None:
    ssid = args.ssid
    password = args.password
    if not SSID_RE.match(ssid):
        fail("invalid SSID")
    if password and not (8 <= len(password) <= 63):
        fail("Wi-Fi password must be 8-63 characters (or blank for an open network)")
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    r = run(cmd, timeout=45.0)
    if r.returncode != 0:
        fail(f"nmcli connect failed: {(r.stderr or r.stdout).strip()}")
    print("OK")


def cmd_wifi_forget(args: argparse.Namespace) -> None:
    ssid = args.ssid
    if not SSID_RE.match(ssid):
        fail("invalid SSID")
    r = run(["nmcli", "connection", "delete", "id", ssid])
    if r.returncode != 0:
        fail(f"nmcli delete failed: {(r.stderr or r.stdout).strip()}")
    print("OK")


# -- service control / power ------------------------------------------------

def cmd_restart_service(args: argparse.Namespace) -> None:
    name = args.name
    if name not in RESTARTABLE_SERVICES:
        fail(f"service not in allowlist: {name}")
    r = run(["systemctl", "restart", name])
    if r.returncode != 0:
        fail(f"restart failed: {r.stderr.strip()}")
    print("OK")


def cmd_restart_display(_args: argparse.Namespace) -> None:
    run(["pkill", "-TERM", "-f", "python3 -m app.main"], timeout=10.0)
    import time

    time.sleep(1.5)
    env = {
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WAYLAND_DISPLAY": "wayland-0",
        "DISPLAY": ":0",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    subprocess.Popen(
        [
            "runuser",
            "-u",
            "airplaymatrix",
            "--",
            "/home/airplaymatrix/.local/bin/airplaymatrix-run.sh",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print("OK")


def cmd_reboot(_args: argparse.Namespace) -> None:
    # Fire-and-forget a delayed reboot via a transient timer so this process
    # (and the Flask request that's still holding the connection open) can
    # return a response before the machine actually goes down.
    r = run(
        [
            "systemd-run",
            "--on-active=2",
            "--unit=airplaymatrix-webui-reboot",
            "systemctl",
            "reboot",
        ],
        timeout=10.0,
    )
    if r.returncode != 0:
        fail(f"failed to schedule reboot: {r.stderr.strip()}")
    print("OK (rebooting in ~2s)")


def cmd_poweroff(_args: argparse.Namespace) -> None:
    # Same delayed-timer trick as cmd_reboot -- let the HTTP response go out
    # before the machine actually powers off. Nothing brings it back
    # remotely: this Pi has no Wake-on-LAN path (Pi 5's onboard NIC doesn't
    # support it, and it's on Wi-Fi here anyway), so this is a one-way door
    # until someone has physical access to the power.
    r = run(
        [
            "systemd-run",
            "--on-active=2",
            "--unit=airplaymatrix-webui-poweroff",
            "systemctl",
            "poweroff",
        ],
        timeout=10.0,
    )
    if r.returncode != 0:
        fail(f"failed to schedule poweroff: {r.stderr.strip()}")
    print("OK (shutting down in ~2s)")


# -- onboard status LED -------------------------------------------------------

def cmd_led_set(args: argparse.Namespace) -> None:
    state = args.state
    if state not in LED_STATES:
        fail(f"led state must be one of: {', '.join(sorted(LED_STATES))}")
    trigger_path = LED_DIR / "trigger"
    brightness_path = LED_DIR / "brightness"
    if not trigger_path.exists():
        fail(f"no onboard LED found at {LED_DIR}")
    # Manual on/off needs the trigger out of the way first, or the kernel's
    # activity blinker just overwrites brightness again a moment later.
    trigger_path.write_text("none")
    brightness_path.write_text(LED_BRIGHTNESS_FOR_STATE[state])
    print("OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("set-airplay-name")
    p.add_argument("name")
    p.set_defaults(func=cmd_set_airplay_name)

    p = sub.add_parser("set-tv-timeout")
    p.add_argument("seconds")
    p.set_defaults(func=cmd_set_tv_timeout)

    p = sub.add_parser("set-hostname")
    p.add_argument("name")
    p.set_defaults(func=cmd_set_hostname)

    p = sub.add_parser("wifi-connect")
    p.add_argument("ssid")
    p.add_argument("password", nargs="?", default="")
    p.set_defaults(func=cmd_wifi_connect)

    p = sub.add_parser("wifi-forget")
    p.add_argument("ssid")
    p.set_defaults(func=cmd_wifi_forget)

    p = sub.add_parser("restart-service")
    p.add_argument("name")
    p.set_defaults(func=cmd_restart_service)

    p = sub.add_parser("restart-display")
    p.set_defaults(func=cmd_restart_display)

    p = sub.add_parser("reboot")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("poweroff")
    p.set_defaults(func=cmd_poweroff)

    p = sub.add_parser("led-set")
    p.add_argument("state")
    p.set_defaults(func=cmd_led_set)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
