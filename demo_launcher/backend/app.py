import os
import shlex
import subprocess
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parents[1]
UI_DIR = BASE_DIR / "ui"

import json
import socket

DEMO_SOCK = os.environ.get(
    "HOVERSCAN_DEMO_SOCK",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hoverscan_demo.sock")
)

CAMERA_DEMO_CMD = "/home/team3/alpr_demo/run_demo.sh"

VIDEO_FILE = os.environ.get(
    "HOVERSCAN_VIDEO_FILE",
    "/home/team3/alpr_demo/demo_vid.mp4"
)

VIDEO_PLAYER_CMD = os.environ.get(
    "HOVERSCAN_VIDEO_PLAYER_CMD",
    f"mpv --fs --no-border --ontop --keep-open=no --really-quiet {shlex.quote(VIDEO_FILE)}"
)

EXIT_KIOSK_CMD = os.environ.get(
    "HOVERSCAN_EXIT_KIOSK_CMD",
    "pkill -f \"--class=HoverscanKiosk\""
)

START_KIOSK_CMD = os.environ.get(
    "HOVERSCAN_START_KIOSK_CMD",
    "/home/pi/hoverscan_launcher/scripts/start_kiosk.sh http://127.0.0.1:8080"
)

REBOOT_CMD = os.environ.get("HOVERSCAN_REBOOT_CMD", "sudo systemctl reboot")
POWEROFF_CMD = os.environ.get("HOVERSCAN_POWEROFF_CMD", "sudo systemctl poweroff")

HOST = os.environ.get("HOVERSCAN_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOVERSCAN_PORT", "8080"))

app = Flask(__name__, static_folder=None)

def _spawn(cmd: str) -> None:
    subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

@app.get("/")
def index():
    return send_from_directory(UI_DIR, "index.html")

@app.get("/<path:path>")
def static_files(path: str):
    return send_from_directory(UI_DIR, path)

@app.get("/api/health")
def health():
    return jsonify({"ok": True})

@app.post("/api/action")
def action():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if name == "camera_demo":
        _spawn(CAMERA_DEMO_CMD)
        return jsonify({"ok": True, "started": "camera_demo"})

    if name == "video_demo":
        _spawn(VIDEO_PLAYER_CMD)
        return jsonify({"ok": True, "started": "video_demo"})

    if name == "return_desktop":
        _spawn(EXIT_KIOSK_CMD)
        return jsonify({"ok": True, "started": "return_desktop"})

    if name == "reboot":
        _spawn(REBOOT_CMD)
        return jsonify({"ok": True, "started": "reboot"})

    if name == "poweroff":
        _spawn(POWEROFF_CMD)
        return jsonify({"ok": True, "started": "poweroff"})

    return jsonify({"ok": False, "error": "unknown action"}), 400

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
