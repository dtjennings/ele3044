#!/usr/bin/env bash
set -euo pipefail

cd /home/team3/alpr_demo

LOG_DIR="/home/team3/alpr_demo/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/demo_$(date +%Y%m%d_%H%M%S).log"

# Log everything (stdout+stderr) to file and show in terminal
exec > >(tee -a "$LOG_FILE") 2>&1

echo "===== DEMO START $(date) ====="
echo "Logging to: $LOG_FILE"
echo "PWD: $(pwd)"

# Ensure GUI apps can open windows when launched via systemd
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Run
 /home/team3/venv/bin/python pi_alpr_demo.py \
  --source picamera \
  --ncnn-model models/yolo11n_ncnn_model \
  --main-w 1280 --main-h 720 \
  --lores-w 640 --lores-h 360 \
  --cam-fps 30 \
  --infer-every 2 --stable-hits 2 \
  --transport tcp --tcp-host 192.168.50.1 --tcp-port 5005 \
  --tx-json --tx-metrics --tx-crop \
  --ocr-min-conf 0.2 \
  --fullscreen   

rc=$?
echo "===== DEMO END $(date) exit_code=$rc ====="
echo "Press Enter to close..."
read -r
exit "$rc"
