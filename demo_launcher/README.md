# Hoverscan Launcher (Pi5 Kiosk Home Screen)

Demo Launcher Screen:
- Camera Demo (runs live demo script)
- Video Demo (plays a fixed MP4)
- Return to Pi Desktop (exits kiosk Chromium)
- Power menu (Reboot / Power off)

**Recommended demo screen:** Portrait 1080×1920 (UI is responsive — it will scale on other resolutions too).

## Install dependencies
```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip chromium-browser mpv
```

## Copy this folder to the Pi
Place the project at:
```bash
/home/pi/hoverscan_launcher
```

## Create venv + install backend
```bash
cd /home/pi/hoverscan_launcher
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Prefilled demo paths
- Camera demo script:
  `/home/team3/alpr_demo/pi_alpr_demo.py`
- Video file:
  `/home/team3/alpr_demo/demo_vid.mp4`

If you ever need to change these, edit:
`systemd/hoverscan-backend.service` (Environment=...) or `backend/app.py`.

## Allow reboot/poweroff without password (for the demo UI)
```bash
sudo nano /etc/sudoers.d/hoverscan-launcher
```
Add EXACTLY:
```
pi ALL=NOPASSWD: /bin/systemctl reboot, /bin/systemctl poweroff
```

## Install + enable auto-launch (user services)
```bash
mkdir -p ~/.config/systemd/user
cp /home/pi/hoverscan_launcher/systemd/hoverscan-*.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now hoverscan-backend.service
systemctl --user enable --now hoverscan-kiosk.service
```

## Desktop shortcut to relaunch kiosk (optional)
```bash
mkdir -p ~/Desktop
cp /home/pi/hoverscan_launcher/scripts/desktop_shortcut.desktop ~/Desktop/"Hoverscan Launcher.desktop"
chmod +x ~/Desktop/"Hoverscan Launcher.desktop"
```
