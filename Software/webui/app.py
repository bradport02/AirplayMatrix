"""
AirplayMatrix settings web UI.

A small, unprivileged Flask app that lets you change the AirPlay device
name, Wi-Fi network, and TV auto-off timeout, plus a few troubleshooting
actions (restart services, restart the kiosk display, reboot/shutdown).
It never touches root-owned files or runs systemctl/nmcli itself --
every privileged action is delegated to
/usr/local/bin/airplaymatrix-privileged.py via a narrowly-scoped sudoers
NOPASSWD rule (see /etc/sudoers.d/airplaymatrix-webui). See that script's
docstring for why that split exists and what it does/doesn't trust.

Run via the airplaymatrix-webui systemd service (see
webui/airplaymatrix-webui.service), not directly.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

# display_settings.py lives one level up (Software/), a sibling of this
# webui/ package rather than inside it -- same reason matrix_daemon.py does
# this: it's shared with the kiosk app (app_qt5/), which also imports it as
# a bare top-level module. WorkingDirectory for this service is webui/, so
# it isn't on sys.path by default.
_PARENT = str(Path(__file__).resolve().parent.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import display_settings  # noqa: E402
import runtime_state  # noqa: E402

SHAIRPORT_CONF = Path("/etc/shairport-sync.conf")
CONFIG_DIR = Path.home() / ".config" / "airplaymatrix-webui"
CONFIG_FILE = CONFIG_DIR / "config.json"
PRIVILEGED = "/usr/local/bin/airplaymatrix-privileged.py"

RESTARTABLE = {
    "shairport-sync": "AirPlay service",
    "airplay-cec-remote": "TV remote passthrough",
}

# Placeholder state for the P3 matrix's future power/brightness/colour/test
# controls (see /matrix routes below). Nothing on the wire supports these
# yet -- Software/matrix/link.py's docstring is explicit that the ESP32
# firmware is a passive image-frame sink with no command opcodes -- so this
# is just persisted intent, ready for whenever firmware support lands.
DEFAULT_MATRIX_SETTINGS = {
    "power": True,
    "mode": "album",  # "album" (show AirPlay artwork) or "fixed_color"
    "brightness": 80,
    "color": "#ffffff",
}

# Named presets for the fixed-colour dropdown. "Custom..." (handled in the
# template/JS) falls back to a raw colour picker for anything not in here.
MATRIX_COLOR_PRESETS = [
    ("#ffffff", "White"),
    ("#ffd9a0", "Warm white"),
    ("#ff0000", "Red"),
    ("#ff8000", "Orange"),
    ("#ffff00", "Yellow"),
    ("#00ff00", "Green"),
    ("#00ffff", "Cyan"),
    ("#0000ff", "Blue"),
    ("#8000ff", "Purple"),
    ("#ff00ff", "Magenta"),
    ("#ff1493", "Pink"),
]

app = Flask(__name__)

# -- bootstrap config (secret key + password) --------------------------------


def _load_or_create_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
        # Merge key-by-key (not just "matrix" missing wholesale) so a config
        # from before a new matrix setting existed picks up its default
        # instead of a KeyError.
        merged = {**DEFAULT_MATRIX_SETTINGS, **cfg.get("matrix", {})}
        if merged != cfg.get("matrix"):
            cfg["matrix"] = merged
            CONFIG_FILE.write_text(json.dumps(cfg))
            CONFIG_FILE.chmod(0o600)
        return cfg
    password = secrets.token_urlsafe(9)
    cfg = {
        "secret_key": secrets.token_hex(32),
        "password_hash": generate_password_hash(password),
        "matrix": dict(DEFAULT_MATRIX_SETTINGS),
    }
    CONFIG_FILE.write_text(json.dumps(cfg))
    CONFIG_FILE.chmod(0o600)
    print(f"[airplaymatrix-webui] generated initial admin password: {password}")
    print(f"[airplaymatrix-webui] change it from the Account page after logging in.")
    return cfg


_config = _load_or_create_config()
app.secret_key = _config["secret_key"]


def _persist_config() -> None:
    CONFIG_FILE.write_text(json.dumps(_config))
    CONFIG_FILE.chmod(0o600)


def _save_password_hash(new_hash: str) -> None:
    _config["password_hash"] = new_hash
    _persist_config()


# -- auth ----------------------------------------------------------------


@app.before_request
def _require_login():
    g.csrf_token = session.get("csrf_token")
    if g.csrf_token is None:
        g.csrf_token = session["csrf_token"] = secrets.token_hex(16)

    if request.endpoint in ("login", "static"):
        return None
    if not session.get("authed"):
        return redirect(url_for("login"))

    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, g.csrf_token):
            flash("Session expired, please try again.", "error")
            return redirect(request.path)
    return None


@app.context_processor
def _inject_csrf():
    # receiver_on drives the top-bar power button, which appears on every
    # page -- so it has to be available to every template, not just the
    # dashboard's own view.
    return {"csrf_token": g.get("csrf_token", ""), "receiver_on": receiver_is_on()}


_login_attempts: dict[str, float] = {}


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        # Simple throttle: at most one attempt every 2 seconds per process.
        last = _login_attempts.get("last", 0.0)
        wait = 2.0 - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _login_attempts["last"] = time.monotonic()

        password = request.form.get("password", "")
        if check_password_hash(_config["password_hash"], password):
            session.clear()
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        flash("Incorrect password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


app.permanent_session_lifetime = 60 * 60 * 24 * 14  # 14 days


# -- helpers ---------------------------------------------------------------


def run_privileged(*args: str, timeout: float = 45.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "-n", PRIVILEGED, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    output = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, output.strip()


def read_shairport_settings() -> dict:
    text = SHAIRPORT_CONF.read_text() if SHAIRPORT_CONF.exists() else ""
    name_m = re.search(r'name\s*=\s*"([^"]*)"', text)
    timeout_m = re.search(r"active_state_timeout\s*=\s*([0-9.]+)", text)
    return {
        "airplay_name": name_m.group(1) if name_m else "(unknown)",
        "tv_timeout": float(timeout_m.group(1)) if timeout_m else None,
    }


def service_status(unit: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# Onboard status LED (Raspberry Pi 5: /sys/class/leds/ACT). World-readable,
# so this side is fine unprivileged -- only writing it needs root, done via
# the "led-set" privileged subcommand. This board's classdev brightness is
# inverted from the usual LED convention (0 = lit, nonzero = off, confirmed
# by hands-on testing), matching LED_BRIGHTNESS_FOR_STATE on the privileged
# side -- keep the two in sync.
LED_DIR = Path("/sys/class/leds/ACT")


def led_status() -> dict:
    try:
        brightness = int((LED_DIR / "brightness").read_text().strip())
    except Exception:
        return {"available": False, "on": False}
    return {"available": True, "on": brightness == 0}


def current_hostname() -> str:
    try:
        return Path("/etc/hostname").read_text().strip()
    except Exception:
        return "unknown"


def ip_addresses() -> list[str]:
    addrs = []
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface = parts[1]
            cidr = parts[3]
            if iface == "lo":
                continue
            addr = ipaddress.ip_interface(cidr).ip
            addrs.append(f"{addr} ({iface})")
    except Exception:
        pass
    return addrs


def wifi_status() -> dict:
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in r.stdout.splitlines():
            f = line.split(":")
            if len(f) >= 4 and f[1] == "wifi":
                return {"device": f[0], "state": f[2], "connection": f[3] or None}
    except Exception:
        pass
    return {"device": None, "state": "unknown", "connection": None}


def wifi_scan() -> list[dict]:
    subprocess.run(
        ["nmcli", "dev", "wifi", "rescan"], capture_output=True, text=True, timeout=10
    )
    networks: list[dict] = []
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        seen = set()
        for line in r.stdout.splitlines():
            f = line.split(":")
            if len(f) < 4 or not f[1] or f[1] in seen:
                continue
            seen.add(f[1])
            networks.append(
                {
                    "in_use": f[0] == "*",
                    "ssid": f[1],
                    "signal": f[2],
                    "security": f[3] or "open",
                }
            )
        networks.sort(key=lambda n: (-n["in_use"], -int(n["signal"] or 0)))
    except Exception:
        pass
    return networks


def saved_wifi_connections() -> list[str]:
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [
            line.split(":")[0]
            for line in r.stdout.splitlines()
            if line.split(":")[-1] == "802-11-wireless"
        ]
    except Exception:
        return []


# -- dashboard ---------------------------------------------------------------


@app.route("/")
def dashboard():
    sp = read_shairport_settings()
    tz = current_timezone()
    return render_template(
        "dashboard.html",
        airplay_name=sp["airplay_name"],
        tv_timeout=sp["tv_timeout"],
        hostname=current_hostname(),
        ip_addresses=ip_addresses(),
        wifi=wifi_status(),
        services={name: service_status(unit) for unit, name in RESTARTABLE.items()},
        led=led_status(),
        display=display_settings.load(),
        np=now_playing(),
        timezone=tz,
        timezones=timezone_choices(tz),
    )


@app.route("/wifi")
def wifi_page():
    return render_template(
        "wifi.html",
        wifi=wifi_status(),
        networks=wifi_scan(),
        saved=saved_wifi_connections(),
    )


# -- settings actions --------------------------------------------------------


@app.route("/settings/airplay-name", methods=["POST"])
def set_airplay_name():
    name = request.form.get("name", "").strip()
    ok, out = run_privileged("set-airplay-name", name)
    flash(f"AirPlay name updated to \"{name}\"." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/settings/tv-timeout", methods=["POST"])
def set_tv_timeout():
    seconds = request.form.get("seconds", "").strip()
    ok, out = run_privileged("set-tv-timeout", seconds)
    flash("TV auto-off timeout updated." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


# (flash label, where it's edited from, what it affects) for each boolean
# display_settings.py toggle this route can flip. show_lyrics/show_details
# are only read by the Zero WH's Qt5 kiosk app (app_qt5/) -- the Pi 5's Qt6
# app doesn't read this file for those two, always showing both.
# eq_meter_enabled is read by MatrixController in *both* builds.
_DISPLAY_TOGGLES = {
    "show_lyrics": ("Lyrics", "dashboard", "the desk display"),
    "show_details": ("Song details", "dashboard", "the desk display"),
    "eq_meter_enabled": ("EQ meter", "matrix_page", "the LED panel (replacing album art)"),
    "progress_dot_enabled": ("Progress bar dot", "dashboard", "the desk display"),
    "sync_on_connect": ("Lyric sync on connect", "dashboard", "the desk display"),
}


@app.route("/settings/display/<key>", methods=["POST"])
def set_display_setting(key: str):
    # No sudo/privileged-script involved for any of these: this process and
    # the kiosk app run as the same unprivileged user, this is just a JSON
    # file under ~/.config.
    if key not in _DISPLAY_TOGGLES:
        flash("Unknown display setting.", "error")
        return redirect(url_for("dashboard"))
    label, redirect_to, affects = _DISPLAY_TOGGLES[key]
    state = request.form.get("state", "") == "on"
    display_settings.set_one(key, state)
    flash(f"{label} {'enabled' if state else 'disabled'} on {affects}.", "ok")
    return redirect(url_for(redirect_to))


@app.route("/settings/transition-mode", methods=["POST"])
def set_transition_mode():
    """Separate from the boolean toggles above -- this one's an enum."""
    mode = request.form.get("mode", "")
    try:
        display_settings.set_transition_mode(mode)
    except ValueError:
        flash("Unknown transition mode.", "error")
        return redirect(url_for("dashboard"))
    label = "cross-fade" if mode == "crossfade" else "fade through background"
    flash(f"Track transitions set to {label} on the desk display.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/settings/crossfade-seconds", methods=["POST"])
def set_crossfade_seconds():
    raw = request.form.get("seconds", "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        flash(f"\"{raw}\" isn't a number.", "error")
        return redirect(url_for("dashboard"))
    settings = display_settings.set_crossfade_seconds(seconds)
    # Report the stored value, not the submitted one -- it's clamped, so
    # those can differ and the user should see which they actually got.
    flash(f"Cross-fade time set to {settings['crossfade_seconds']:.1f}s.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/settings/lyrics-offset", methods=["POST"])
def set_lyrics_offset():
    # Unlike the two toggles above, both kiosk builds read this one -- see
    # display_settings.py's docstring for why it exists (AirPlay 2's output
    # buffering vs. shairport-sync's prgr metadata reporting stream
    # position, not buffered/audible position).
    raw = request.form.get("seconds", "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        flash(f"\"{raw}\" isn't a number.", "error")
        return redirect(url_for("dashboard"))
    settings = display_settings.set_lyrics_offset(seconds)
    flash(f"Lyrics offset set to {settings['lyrics_offset_seconds']:+.2f}s.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/settings/connect-volume", methods=["POST"])
def set_connect_volume():
    # Read by cec/airplay-tv-power.sh (as the shairport-sync user, on every
    # new AirPlay connection), not either kiosk app -- see
    # display_settings.py's docstring.
    raw = request.form.get("percent", "").strip()
    try:
        percent = int(raw)
    except ValueError:
        flash(f"\"{raw}\" isn't a whole number.", "error")
        return redirect(url_for("dashboard"))
    settings = display_settings.set_connect_volume_percent(percent)
    flash(f"AirPlay will connect at {settings['connect_volume_percent']}% volume.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/wifi/connect", methods=["POST"])
def wifi_connect():
    ssid = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")
    ok, out = run_privileged("wifi-connect", ssid, password, timeout=60)
    flash(f"Connected to \"{ssid}\"." if ok else f"Failed to connect: {out}", "ok" if ok else "error")
    return redirect(url_for("wifi_page"))


@app.route("/wifi/forget", methods=["POST"])
def wifi_forget():
    ssid = request.form.get("ssid", "").strip()
    ok, out = run_privileged("wifi-forget", ssid)
    flash(f"Forgot \"{ssid}\"." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("wifi_page"))


@app.route("/system/restart/<service>", methods=["POST"])
def restart_service(service: str):
    if service not in RESTARTABLE:
        flash("Unknown service.", "error")
        return redirect(url_for("dashboard"))
    ok, out = run_privileged("restart-service", service)
    flash(f"Restarted {RESTARTABLE[service]}." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/system/restart-display", methods=["POST"])
def restart_display():
    ok, out = run_privileged("restart-display")
    flash("Restarting the display app." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


def receiver_is_on() -> bool:
    """Whether the AirPlay receiver is currently running -- what the top-bar
    power button reflects and toggles."""
    return service_status("shairport-sync") == "active"


REPO_DIR = Path(__file__).resolve().parent.parent.parent


def current_timezone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        return ""


def timezone_choices(current: str) -> list[tuple[str, list[str]]]:
    """The timezone dropdown's contents: zones grouped by region prefix
    ("Europe/London" under "Europe"), regions and zones alphabetical.

    Read from zoneinfo rather than `timedatectl list-timezones` so building
    the dashboard doesn't wait on another subprocess -- the two agree on
    this system apart from "localtime", which is dropped below. The one
    thing that must stay true is that everything offered here survives
    cmd_set_timezone()'s validation, which *is* timedatectl's list.

    Grouping is purely for the human: ~480 zones in one flat list is a
    miserable scroll, and the region is the first thing anyone narrows by.

    `current` is put back at the top as a group of its own when the system
    no longer knows it -- an /etc/timezone written by an older tzdata (the
    "backward" links like US/Eastern aren't shipped here any more) must
    still render as the selected value and stay replaceable, rather than
    silently showing whatever happens to sort first as if it were live.
    """
    # zoneinfo reports /etc/localtime -- the symlink that names the current
    # zone, not a zone in its own right -- as "localtime". timedatectl has
    # no such name, so offering it would only ever fail on save.
    zones = sorted(zoneinfo.available_timezones() - {"localtime"})

    groups: dict[str, list[str]] = {}
    for zone in zones:
        # UTC/GMT/Factory have no prefix to group by.
        region = zone.split("/", 1)[0] if "/" in zone else "Other"
        groups.setdefault(region, []).append(zone)

    choices = [(region, groups[region]) for region in sorted(groups) if region != "Other"]
    if "Other" in groups:
        choices.append(("Other", groups["Other"]))
    if current and current not in zones:
        choices.insert(0, ("Currently set", [current]))
    return choices


def now_playing() -> dict:
    """What the kiosk app last said it was showing.

    Stale or missing means the app isn't running (or hasn't got that far),
    which is worth showing as exactly that rather than as an error -- see
    runtime_state.py.
    """
    state = runtime_state.read_json(runtime_state.NOW_PLAYING_PATH) or {}
    age = time.time() - state.get("updated_at", 0)
    state["stale"] = age > 120
    return state


@app.route("/diagnostics")
def diagnostics():
    """Everything needed to work out why the display is misbehaving, without
    an SSH session -- which is otherwise the only way to see any of it."""
    return render_template(
        "diagnostics.html",
        kiosk_log=runtime_state.tail(runtime_state.KIOSK_LOG_PATH, 400),
        kiosk_log_path=runtime_state.KIOSK_LOG_PATH,
        shairport_log=_journal("shairport-sync", 120),
        webui_log=_journal("airplaymatrix-webui", 60),
        display=display_settings.load(),
        levels=display_settings.LOG_LEVELS,
        version=_repo_version(),
    )


def _journal(unit: str, lines: int) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(could not read journal: {exc})"
    return (r.stdout or r.stderr or "").strip() or "(nothing logged)"


def _repo_version() -> dict:
    """Short description of the checkout, for the update card."""
    def git(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(REPO_DIR), *args],
                capture_output=True, text=True, timeout=20,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    return {
        "commit": git("log", "-1", "--format=%h"),
        "subject": git("log", "-1", "--format=%s"),
        "date": git("log", "-1", "--format=%cd", "--date=short"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


@app.route("/settings/log-level", methods=["POST"])
def set_log_level():
    level = request.form.get("level", "")
    try:
        display_settings.set_log_level(level)
    except ValueError:
        flash("Unknown log level.", "error")
        return redirect(url_for("diagnostics"))
    flash(
        f"Log level set to {level}. Restart the display app for it to take effect.",
        "ok",
    )
    return redirect(url_for("diagnostics"))


@app.route("/system/update", methods=["POST"])
def update_from_git():
    """Pull new code and report exactly what changed.

    --ff-only on purpose: this box is a deployment target, not somewhere to
    resolve a merge. If it can't fast-forward, something has been edited
    locally and a human should look rather than have a button paper over it.
    """
    before = _repo_version().get("commit", "")
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_DIR), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        flash(f"Update failed: {exc}", "error")
        return redirect(url_for("diagnostics"))

    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        flash(f"Update failed: {detail[-1] if detail else 'git pull returned an error'}", "error")
        return redirect(url_for("diagnostics"))

    after = _repo_version()
    if after.get("commit") == before:
        flash("Already up to date.", "ok")
    else:
        flash(
            f"Updated to {after.get('commit')} \u2014 {after.get('subject')}. "
            "Restart the display app to run the new code; if privileged commands "
            "changed, reinstall the root helper below too.",
            "ok",
        )
    return redirect(url_for("diagnostics"))


@app.route("/system/install-privileged", methods=["POST"])
def install_privileged():
    ok, out = run_privileged("install-privileged")
    flash("Root helper updated." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("diagnostics"))


@app.route("/settings/timezone", methods=["POST"])
def set_timezone():
    zone = request.form.get("zone", "").strip()
    if not zone:
        flash("No timezone given.", "error")
        return redirect(url_for("dashboard"))
    ok, out = run_privileged("set-timezone", zone)
    flash(f"Timezone set to {zone}." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/system/power", methods=["POST"])
def soft_power():
    """Standby toggle: the Pi keeps running (so this page still answers),
    only the AirPlay receiver goes down."""
    state = request.form.get("state", "")
    if state not in ("on", "off"):
        flash("Unknown power state.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    ok, out = run_privileged("soft-power", state)
    if ok and state == "off":
        # Matrix panel goes dark with it. Purely persisted intent for now --
        # the ESP32 firmware has no power opcode yet (see the /matrix page's
        # warning), so this is the "have the function ready" half: when that
        # firmware lands, this is already the right place and the stored
        # state is already correct.
        _config["matrix"]["power"] = False
        _persist_config()

    if ok:
        flash(
            "Receiver in standby -- AirPlay is off, this page stays available to switch it back on."
            if state == "off"
            else "Receiver active -- AirPlay is discoverable again.",
            "ok",
        )
    else:
        flash(f"Failed: {out}", "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/system/restart-webui", methods=["POST"])
def restart_webui():
    # The redirect below is served by the process that's about to be
    # restarted -- the privileged helper schedules the restart a couple of
    # seconds out precisely so this response gets out first. The browser
    # then reloads the dashboard from the *new* process, which is what makes
    # picking up new settings/templates possible without an SSH session.
    ok, out = run_privileged("restart-webui")
    flash(
        "Restarting the web UI -- reload this page in a few seconds."
        if ok
        else f"Failed: {out}",
        "ok" if ok else "error",
    )
    return redirect(url_for("dashboard"))


@app.route("/system/reboot", methods=["POST"])
def reboot():
    ok, out = run_privileged("reboot")
    flash("Rebooting now -- this page will stop responding for a minute or two." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/system/shutdown", methods=["POST"])
def shutdown():
    ok, out = run_privileged("poweroff")
    flash(
        "Shutting down now. This Pi has no remote wake -- physical access "
        "(unplug/replug power, or a smart plug) is the only way to bring it back up."
        if ok else f"Failed: {out}",
        "ok" if ok else "error",
    )
    return redirect(url_for("dashboard"))


@app.route("/system/led", methods=["POST"])
def set_led():
    state = request.form.get("state", "")
    if state not in ("on", "off"):
        flash("Unknown LED state.", "error")
        return redirect(url_for("dashboard"))
    ok, out = run_privileged("led-set", state)
    label = state
    flash(f"Status LED set {label}." if ok else f"Failed: {out}", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/settings/restore-defaults", methods=["POST"])
def restore_defaults():
    # Scoped deliberately: only settings this app owns with a real, known
    # default. Wi-Fi credentials and the AirPlay device name are left
    # alone -- there's no "default" to fall back to for either, and wiping
    # Wi-Fi could cut off remote access to the device entirely.
    _config["matrix"] = dict(DEFAULT_MATRIX_SETTINGS)
    new_password = secrets.token_urlsafe(9)
    _config["password_hash"] = generate_password_hash(new_password)
    _persist_config()
    display_settings.save(dict(display_settings.DEFAULTS))  # type: ignore[arg-type]

    tv_ok, tv_out = run_privileged("set-tv-timeout", "300")

    session.clear()
    message = (
        f"Defaults restored. New admin password: {new_password} "
        "-- save this now, it will not be shown again."
    )
    if not tv_ok:
        message += f" (TV auto-off timeout reset failed: {tv_out})"
    flash(message, "ok")
    return redirect(url_for("login"))


@app.route("/matrix")
def matrix_page():
    preset_hexes = {h for h, _ in MATRIX_COLOR_PRESETS}
    display = display_settings.load()
    return render_template(
        "matrix.html",
        matrix=_config["matrix"],
        # The panel's effective display mode, resolved across both stores --
        # see set_matrix_mode() for why it's split. eq_meter_enabled wins
        # because that's the one the panel actually obeys today.
        matrix_mode="eq_meter" if display["eq_meter_enabled"] else _config["matrix"]["mode"],
        color_presets=MATRIX_COLOR_PRESETS,
        color_is_custom=_config["matrix"]["color"] not in preset_hexes,
        # Unlike matrix/power/brightness/colour above (webui-local, not
        # wired to anything yet -- see MATRIX_COLOR_PRESETS' neighbouring
        # comment), the EQ meter setting lives in display_settings.py's
        # shared config so MatrixController (app/, app_qt5/) can actually
        # read it. Same source the dashboard's desk-display cards use.
        display=display,
    )


@app.route("/settings/matrix/mode", methods=["POST"])
def set_matrix_mode():
    """The panel's one display mode, spanning two different stores.

    "eq_meter" lives in display_settings.py (MatrixController reads it and
    acts on it today); "album"/"fixed_color" live in this app's own config
    and aren't wired to the firmware yet. They're presented as one
    three-way choice because that's what they actually are -- the EQ meter
    takes the whole panel over when it's on, so it can't meaningfully
    coexist with either of the others. Picking album art or fixed colour
    therefore also switches the EQ meter off, which is what makes those
    buttons do the one thing a user would expect them to: give the panel
    back to artwork.
    """
    mode = request.form.get("mode", "")
    if mode not in ("album", "fixed_color", "eq_meter"):
        flash("Unknown display mode.", "error")
        return redirect(url_for("matrix_page"))

    if mode == "eq_meter":
        display_settings.set_one("eq_meter_enabled", True)
        flash("Display mode set to EQ meter on the LED panel.", "ok")
        return redirect(url_for("matrix_page"))

    display_settings.set_one("eq_meter_enabled", False)
    _config["matrix"]["mode"] = mode
    _persist_config()
    label = "album art" if mode == "album" else "fixed colour"
    flash(f"Display mode set to {label} (saved -- not wired to the panel yet).", "ok")
    return redirect(url_for("matrix_page"))


@app.route("/settings/matrix/power", methods=["POST"])
def set_matrix_power():
    state = request.form.get("power", "") == "on"
    _config["matrix"]["power"] = state
    _persist_config()
    flash(f"Power set to {'on' if state else 'off'} (saved -- not wired to the panel yet).", "ok")
    return redirect(url_for("matrix_page"))


@app.route("/settings/matrix/brightness", methods=["POST"])
def set_matrix_brightness():
    raw = request.form.get("brightness", "").strip()
    try:
        value = max(0, min(100, int(raw)))
    except ValueError:
        flash("Brightness must be a whole number.", "error")
        return redirect(url_for("matrix_page"))
    _config["matrix"]["brightness"] = value
    _persist_config()
    flash(f"Brightness set to {value}% (saved -- not wired to the panel yet).", "ok")
    return redirect(url_for("matrix_page"))


@app.route("/settings/matrix/color", methods=["POST"])
def set_matrix_color():
    color = request.form.get("color", "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        flash("Colour must be a hex value like #ff8800.", "error")
        return redirect(url_for("matrix_page"))
    _config["matrix"]["color"] = color.lower()
    _persist_config()
    flash(f"Fixed colour set to {color.lower()} (saved -- not wired to the panel yet).", "ok")
    return redirect(url_for("matrix_page"))


@app.route("/settings/matrix/test", methods=["POST"])
def matrix_display_test():
    flash(
        "Display test isn't wired up yet -- the matrix firmware only accepts image frames "
        "today, with no command channel for this. Coming once that support ships.",
        "error",
    )
    return redirect(url_for("matrix_page"))


@app.route("/account/password", methods=["POST"])
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not check_password_hash(_config["password_hash"], current):
        flash("Current password is incorrect.", "error")
    elif len(new) < 8:
        flash("New password must be at least 8 characters.", "error")
    elif new != confirm:
        flash("New passwords don't match.", "error")
    else:
        _save_password_hash(generate_password_hash(new))
        flash("Password changed.", "ok")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
