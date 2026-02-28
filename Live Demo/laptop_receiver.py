#!/usr/bin/env python3
"""
laptop_receiver.py (Windows receiver)

Receives framed TCP packets from the Raspberry Pi demo and:
  - saves received crop JPEGs to the dashboard images folder 
  - prepends plate events to events.csv 
  - prepends system metrics to metrics.csv 

Framing format (from Pi):
  [4-byte big-endian header_len][header_json][for each attachment: 4-byte len + bytes]
"""

import argparse
import csv
import json
import os
import socket
import struct
import time
import hashlib
from dataclasses import dataclass, field
from typing import Dict, Any, List

try:
    import cv2  
    import numpy as np  
except Exception:
    cv2 = None
    np = None

IMAGES_DIR = r"web_app\images"
EVENTS_CSV = r"web_app\data\events.csv"
METRICS_CSV = r"web_app\data\metrics.csv"

EVENTS_HEADER = ["timestamp", "number_plate", "ticket_owned", "confidence", "image_path"]
METRICS_HEADER = ["timestamp", "fps", "latency_ms", "cpu_percent", "temp_c", "ram_mb", "dropped_frames"]

CSV_DELIMITER = ","

def recv_exact(conn: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes or raise ConnectionError."""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket_closed")
        buf += chunk
    return buf


def _safe_makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _ensure_csv_with_header(path: str, header: List[str]) -> None:
    """Create CSV file and header row if missing/empty."""
    parent = os.path.dirname(path)
    if parent:
        _safe_makedirs(parent)

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=CSV_DELIMITER, lineterminator="\n")
            w.writerow(header)


def _append_csv_row(path: str, row: List[Any]) -> None:
    """Insert one CSV row directly below the header (newest-first ordering)."""
    parent = os.path.dirname(path)
    if parent:
        _safe_makedirs(parent)

    existing_rows: List[List[str]] = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "r", newline="", encoding="utf-8") as f:
            existing_rows = list(csv.reader(f, delimiter=CSV_DELIMITER))

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=CSV_DELIMITER, lineterminator="\n")
        if existing_rows:
            header = existing_rows[0]
            data_rows = existing_rows[1:]
            w.writerow(header)
            w.writerow(row)
            w.writerows(data_rows)
        else:
            w.writerow(row)

    os.replace(tmp_path, path)


def _decode_jpeg(jpg_bytes: bytes):
    if cv2 is None or np is None or not jpg_bytes:
        return None
    arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _fmt_ts_for_csv(ts_ms: int) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts_ms / 1000.0))


def _fmt_ts_for_filename(ts_ms: int) -> str:
    return time.strftime("%d%m%Y_%H%M%S", time.localtime(ts_ms / 1000.0))


def _fmt_ts_for_metrics(ts_ms: int) -> str:
    return time.strftime("%d/%m/%Y %H/%M/%S", time.localtime(ts_ms / 1000.0))


def _stable_ticket_owned(plate: str) -> str:
    """
    Ticket ownership isn't present in the incoming JSON.
    For demo purposes we assign a stable TRUE/FALSE per plate so the UI looks consistent.
    """
    h = hashlib.md5(plate.encode("utf-8")).digest()
    return "TRUE" if (h[0] & 1) == 1 else "FALSE"


@dataclass
class PendingWrites:
    events: List[List[Any]] = field(default_factory=list)
    metrics: List[List[Any]] = field(default_factory=list)

    def flush(self) -> None:
        # Flush in FIFO order; if file is locked keep queue.
        if self.events:
            try:
                for row in self.events:
                    _append_csv_row(EVENTS_CSV, row)
                self.events.clear()
            except PermissionError:
                pass

        if self.metrics:
            try:
                for row in self.metrics:
                    _append_csv_row(METRICS_CSV, row)
                self.metrics.clear()
            except PermissionError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--show", action="store_true", help="Debug: show received images (may be disabled automatically)")
    ap.add_argument("--random-ticket", action="store_true", help="Use random TRUE/FALSE instead of stable per-plate")
    args = ap.parse_args()

    _safe_makedirs(IMAGES_DIR)
    _ensure_csv_with_header(EVENTS_CSV, EVENTS_HEADER)
    _ensure_csv_with_header(METRICS_CSV, METRICS_HEADER)

    pending = PendingWrites()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)

    print(f"[RX] Listening on {args.bind}:{args.port}")

    while True:
        conn, addr = srv.accept()
        print(f"[RX] Connected: {addr}")
        conn.settimeout(None)

        try:
            while True:
                # Read framed header
                hdr_len = struct.unpack(">I", recv_exact(conn, 4))[0]
                hdr = json.loads(recv_exact(conn, hdr_len).decode("utf-8"))

                # Read attachments
                attachments: Dict[str, bytes] = {}
                for name in hdr.get("attachments_order", []):
                    blob_len = struct.unpack(">I", recv_exact(conn, 4))[0]
                    attachments[name] = recv_exact(conn, blob_len)

                # Flush any queued writes opportunistically
                pending.flush()

                if hdr.get("type") != "plate_event":
                    continue

                ts_ms = int(hdr.get("ts_ms") or 0)
                plate = (hdr.get("plate") or "").strip()
                det_conf = float(hdr.get("det_conf") or 0.0)
                ocr_conf = float(hdr.get("ocr_conf") or 0.0)
                bbox = hdr.get("bbox_main")

                tstr = time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000.0))
                print(f"[{tstr}] PLATE={plate} det={det_conf:.2f} ocr={ocr_conf:.2f} bbox={bbox}")

                image_filename = ""
                if "crop_jpg" in attachments and plate:
                    image_filename = f"{plate}_{_fmt_ts_for_filename(ts_ms)}.jpg"
                    out_path = os.path.join(IMAGES_DIR, image_filename)
                    try:
                        with open(out_path, "wb") as f:
                            f.write(attachments["crop_jpg"])
                    except PermissionError as e:
                        print(f"[WARN] Could not write image: {e}")
                        image_filename = ""

                if plate:
                    ticket_owned = _stable_ticket_owned(plate)
                    if args.random_ticket:
                        ticket_owned = "TRUE" if (time.time_ns() & 1) else "FALSE"

                    event_row = [
                        _fmt_ts_for_csv(ts_ms),
                        plate,
                        ticket_owned,
                        f"{ocr_conf:.2f}",
                        image_filename,
                    ]
                    try:
                        _append_csv_row(EVENTS_CSV, event_row)
                    except PermissionError:
                        pending.events.append(event_row)

                metrics = hdr.get("metrics") or {}
                rx_ms = int(time.time() * 1000)
                latency_ms = (rx_ms - ts_ms) if ts_ms else ""

                # fps / dropped_frames may not be provided by Pi; keep blank/0 if missing
                fps = hdr.get("fps", "")
                dropped = hdr.get("dropped_frames", 0)

                cpu_percent = metrics.get("cpu_percent", "")
                temp_c = metrics.get("cpu_temp_c", "")
                ram_mb = metrics.get("ram_used_mb", "")

                metrics_row = [
                    _fmt_ts_for_metrics(rx_ms),
                    fps,
                    latency_ms,
                    cpu_percent,
                    temp_c,
                    ram_mb,
                    dropped,
                ]
                try:
                    _append_csv_row(METRICS_CSV, metrics_row)
                except PermissionError:
                    pending.metrics.append(metrics_row)

                if args.show and cv2 is not None and np is not None:
                    try:
                        if "crop_jpg" in attachments:
                            img = _decode_jpeg(attachments["crop_jpg"])
                            if img is not None:
                                cv2.imshow("CROP", img)
                        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                            break
                    except Exception as e:
                        print(f"[WARN] show disabled due to display error: {e}")
                        args.show = False
                        try:
                            cv2.destroyAllWindows()
                        except Exception:
                            pass

        except Exception as e:
            print(f"[RX] Connection ended: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            if args.show and cv2 is not None:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass


if __name__ == "__main__":
    main()

