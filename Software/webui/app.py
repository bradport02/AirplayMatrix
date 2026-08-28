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
import time
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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
    return {"csrf_token": g.get("csrf_token", "")}


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
    return render_template(
        "dashboard.html",
        airplay_name=sp["airplay_name"],
        tv_timeout=sp["tv_timeout"],
        hostname=current_hostname(),
        ip_addresses=ip_addresses(),
        wifi=wifi_status(),
        services={name: service_status(unit) for unit, name in RESTARTABLE.items()},
        led=led_status(),
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
    return render_template(
        "matrix.html",
        matrix=_config["matrix"],
        color_presets=MATRIX_COLOR_PRESETS,
        color_is_custom=_config["matrix"]["color"] not in preset_hexes,
    )


@app.route("/settings/matrix/mode", methods=["POST"])
def set_matrix_mode():
    mode = request.form.get("mode", "")
    if mode not in ("album", "fixed_color"):
        flash("Unknown display mode.", "error")
        return redirect(url_for("matrix_page"))
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
