#!/usr/bin/env python3
"""
pi_alpr_demo.py (Raspberry Pi 5)
ANPR pipeline:
PiCamera2/video/images -> YOLO11n (NCNN) plate detection -> crop -> pre-processing -> OCR -> transmit
"""

import argparse
import os
import queue
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import psutil
import pytesseract
from ultralytics import YOLO

# ----------------------------
# System / OpenCV tuning
# ----------------------------
cv2.setUseOptimized(True)
cv2.setNumThreads(1)  # Avoid contention on Pi

def now_ms() -> int:
    return int(time.time() * 1000)

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def jpeg_encode(img_bgr: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""

# ----------------------------
# Metrics (sample 1 Hz)
# ----------------------------
class MetricsSampler:
    def __init__(self, interval_sec: float = 1.0):
        self.interval_sec = interval_sec
        self._lock = threading.Lock()
        self._m: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        psutil.cpu_percent(interval=None)
        self._t.start()

    def stop(self):
        self._stop.set()
        self._t.join(timeout=1.0)

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._m)

    def _read_cpu_temp_c(self) -> Optional[float]:
        for p in ("/sys/class/thermal/thermal_zone0/temp",):
            try:
                if os.path.exists(p):
                    with open(p, "r") as f:
                        v = float(f.read().strip())
                        return v / 1000.0 if v > 200 else v
            except Exception:
                pass
        return None

    def _read_throttled_hex(self) -> Optional[str]:
        try:
            import subprocess
            out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True).strip()
            if "=" in out:
                return out.split("=")[1]
        except Exception:
            return None
        return None

    def _run(self):
        while not self._stop.is_set():
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                temp = self._read_cpu_temp_c()
                thr = self._read_throttled_hex()
                m = {
                    "ts_ms": now_ms(),
                    "cpu_percent": round(cpu, 1),
                    "ram_percent": round(mem.percent, 1),
                    "ram_used_mb": round(mem.used / (1024 * 1024), 1),
                    "ram_total_mb": round(mem.total / (1024 * 1024), 1),
                    "cpu_temp_c": None if temp is None else round(temp, 1),
                    "throttled": thr,
                }
                with self._lock:
                    self._m = m
            except Exception:
                pass
            self._stop.wait(self.interval_sec)

# ----------------------------
# Transport framing 
# ----------------------------
def pack_msg(header_json: bytes, attachments_order: List[str], attachments: Dict[str, bytes]) -> bytes:
    parts = [struct.pack(">I", len(header_json)), header_json]
    for name in attachments_order:
        blob = attachments.get(name, b"")
        parts.append(struct.pack(">I", len(blob)))
        parts.append(blob)
    return b"".join(parts)

class Transport:
    def send(self, header: Dict[str, Any], attachments: Dict[str, bytes]) -> None:
        raise NotImplementedError

class NullTransport(Transport):
    def send(self, header: Dict[str, Any], attachments: Dict[str, bytes]) -> None:
        return

class TCPTransport(Transport):
    def __init__(self, host: str, port: int, retry_sec: float = 1.0):
        self.host = host
        self.port = port
        self.retry_sec = retry_sec
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def _connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((self.host, self.port))
        s.settimeout(None)
        self._sock = s

    def send(self, header: Dict[str, Any], attachments: Dict[str, bytes]) -> None:
        import json
        header_json = json.dumps(header).encode("utf-8")
        payload = pack_msg(header_json, header.get("attachments_order", []), attachments)

        with self._lock:
            try:
                if self._sock is None:
                    self._connect()
                assert self._sock is not None
                self._sock.sendall(payload)
            except Exception:
                try:
                    if self._sock:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
                time.sleep(self.retry_sec)

class RFCOMMTransport(Transport):
    """
    Linux RFCOMM client: connect to laptop SPP server.
    Requires laptop receiver already set up for RFCOMM channel.
    """
    def __init__(self, bt_addr: str, channel: int, retry_sec: float = 1.0):
        self.bt_addr = bt_addr
        self.channel = channel
        self.retry_sec = retry_sec
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def _connect(self):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(4.0)
        s.connect((self.bt_addr, self.channel))
        s.settimeout(None)
        self._sock = s

    def send(self, header: Dict[str, Any], attachments: Dict[str, bytes]) -> None:
        import json
        header_json = json.dumps(header).encode("utf-8")
        payload = pack_msg(header_json, header.get("attachments_order", []), attachments)

        with self._lock:
            try:
                if self._sock is None:
                    self._connect()
                assert self._sock is not None
                self._sock.sendall(payload)
            except Exception:
                try:
                    if self._sock:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
                time.sleep(self.retry_sec)

# ----------------------------
# Detection / OCR helpers
# ----------------------------
@dataclass
class DetBox:
    x1: int
    y1: int
    x2: int
    y2: int
    conf: float

def iou(a: DetBox, b: DetBox) -> float:
    ax1, ay1, ax2, ay2 = a.x1, a.y1, a.x2, a.y2
    bx1, by1, bx2, by2 = b.x1, b.y1, b.x2, b.y2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(1, (ax2 - ax1)) * max(1, (ay2 - ay1))
    area_b = max(1, (bx2 - bx1)) * max(1, (by2 - by1))
    return inter / float(area_a + area_b - inter + 1e-6)

def clean_text(s: str) -> str:
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s

UK_REGEX = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z]{3}$")

def uk_correct(plate: str) -> Tuple[str, float]:
    """
    Heuristic UK format correction:
    - target: AA11AAA 
    - fix common confusions by position
    Returns (corrected, score_boost)
    """
    p = clean_text(plate)
    if len(p) < 7:
        return p, 0.0
    p = p[:7]

    p_list = list(p)

    # fix digit positions
    for idx in (2, 3):
        if p_list[idx] == "O":
            p_list[idx] = "0"
        if p_list[idx] == "I":
            p_list[idx] = "1"
        if p_list[idx] == "Z":
            p_list[idx] = "2"

    # fix letter positions
    for idx in (0, 1, 4, 5, 6):
        if p_list[idx] == "0":
            p_list[idx] = "O"
        if p_list[idx] == "1":
            p_list[idx] = "I"
        if p_list[idx] == "2":
            p_list[idx] = "Z"

    corrected = "".join(p_list)
    boost = 0.25 if UK_REGEX.match(corrected) else 0.0
    return corrected, boost

class TesseractUKPlateOCR:
    def __init__(self):
        # PSM 7 = single text line; whitelist alnum
        self.cfg = "--oem 1 --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
    
    def preprocess(self, crop_bgr):
        g = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)

        # boost contrast
        g = cv2.equalizeHist(g)

        # denoise
        g = cv2.GaussianBlur(g, (3,3), 0)

        # binary 
        _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # Make letters darker 
        # If plates are black text on bright background, invert will help:
        th = 255 - th

        # Morph to close gaps in characters
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k, iterations=1)

        return th

    def infer(self, crop_bgr: np.ndarray) -> Tuple[str, float]:
        proc = self.preprocess(crop_bgr)

        # Use image_to_data to get confidence-like numbers
        data = pytesseract.image_to_data(proc, config=self.cfg, output_type=pytesseract.Output.DICT)
        text = "".join(data.get("text", [])).strip()
        plate = clean_text(text)

        confs = []
        for c in data.get("conf", []):
            try:
                v = float(c)
                if v >= 0:
                    confs.append(v)
            except Exception:
                pass

        base_conf = (sum(confs) / (len(confs) * 100.0)) if confs else 0.0
        corrected, boost = uk_correct(plate)
        conf = min(0.99, base_conf + boost)
        return corrected, conf

class YOLOPlateDetectorNCNN:
    def __init__(self, model_path: str, imgsz: int, conf: float, iou_th: float, plate_class_id: int):
        self.model = YOLO(model_path)
        self.imgsz = imgsz
        self.conf = conf
        self.iou_th = iou_th
        self.plate_class_id = plate_class_id

        # warm-up
        dummy = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
        _ = self.model.predict(dummy, imgsz=imgsz, conf=conf, iou=iou_th,
                               classes=[plate_class_id], max_det=1, verbose=False)
        
    def infer_one(self, frame_bgr: np.ndarray) -> Optional[DetBox]:
        res = self.model.predict(frame_bgr, imgsz=self.imgsz, conf=self.conf, iou=self.iou_th,
                                 classes=[self.plate_class_id], max_det=1, verbose=False)
        if not res:
            return None
        r0 = res[0]
        if r0.boxes is None or len(r0.boxes) == 0:
            return None

        b = r0.boxes[0]
        xyxy = b.xyxy[0].cpu().numpy() if hasattr(b.xyxy, "cpu") else np.array(b.xyxy[0])
        c = float(b.conf[0].cpu().item()) if hasattr(b.conf[0], "cpu") else float(b.conf[0])
        x1, y1, x2, y2 = map(int, xyxy)
        return DetBox(x1, y1, x2, y2, c)

# ----------------------------
# PiCamera2 source / Video / Images
# ----------------------------
class FrameSource:
    def read(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], int]:
        raise NotImplementedError
    def release(self): pass


class PiCamera2Source(FrameSource):
    def __init__(self, main_size: Tuple[int,int], lores_size: Tuple[int,int], fps: int, camera_index: int = 0):
        from picamera2 import Picamera2

        self.picam2 = Picamera2(camera_index)
        self.main_w, self.main_h = main_size
        self.lo_w, self.lo_h = lores_size

        config = self.picam2.create_video_configuration(
            main={"size": (self.main_w, self.main_h), "format": "BGR888"},
            lores={"size": (self.lo_w, self.lo_h), "format": "BGR888"},
            controls={"FrameRate": fps},
            buffer_count=6,
        )
        self.picam2.configure(config)
        self.picam2.start()
        self._logged_first_frame = False

    def read(self):
        ts = now_ms()
        try:
            request = self.picam2.capture_request()
            try:
                main_bgr = request.make_array("main")
                lo_bgr   = request.make_array("lores")
                
            finally:
                request.release()

            if not self._logged_first_frame:
                print(f"[CAM] first frame main={main_bgr.shape} {main_bgr.dtype}, lores={lo_bgr.shape} {lo_bgr.dtype}")
                self._logged_first_frame = True

            return main_bgr, lo_bgr, ts

        except Exception as e:
            print("Camera read error:", e)
            return None, None, ts

    def release(self):
        try:
            self.picam2.stop()
        except Exception:
            pass


class VideoSource(FrameSource):
    def __init__(self, path: str, lores_size: Tuple[int,int], loop: bool):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open video: {path}")
        self.lo_w, self.lo_h = lores_size
        self.loop = loop

        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            if self.loop:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.cap.read()
            if not ok:
                return None, None, now_ms()
        ts = now_ms()
        lo = cv2.resize(frame, (self.lo_w, self.lo_h), interpolation=cv2.INTER_LINEAR)
        return frame, lo, ts

    def release(self):
        try: self.cap.release()
        except Exception: pass


class ImagesSource(FrameSource):
    def __init__(self, folder: str, lores_size: Tuple[int,int], rate_hz: float, loop: bool):
        exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
        self.files = [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.lower().endswith(exts)]
        if not self.files:
            raise RuntimeError(f"No images in: {folder}")
        self.idx = 0
        self.lo_w, self.lo_h = lores_size
        self.period = 1.0 / max(0.1, rate_hz)
        self.loop = loop
        self._last = time.time()

    def read(self):
        dt = time.time() - self._last
        if dt < self.period:
            time.sleep(self.period - dt)
        self._last = time.time()

        if self.idx >= len(self.files):
            if self.loop:
                self.idx = 0
            else:
                return None, None, now_ms()

        path = self.files[self.idx]
        self.idx += 1
        img = cv2.imread(path)
        if img is None:
            return None, None, now_ms()
        ts = now_ms()
        lo = cv2.resize(img, (self.lo_w, self.lo_h), interpolation=cv2.INTER_LINEAR)
        return img, lo, ts

# ----------------------------
# UI overlay 
# ----------------------------
def draw_hud(frame: np.ndarray, fps: float, metrics: Dict[str, Any], last_plate: str, last_conf: float, age_s: float):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 56), (0, 0, 0), -1)
    left = f"FPS {fps:4.1f} | CPU {metrics.get('cpu_percent','-')}% | RAM {metrics.get('ram_percent','-')}% | T {metrics.get('cpu_temp_c','-')}C"
    right = f"LAST {last_plate or '-'} ({last_conf:.2f})  {age_s:0.1f}s"
    cv2.putText(frame, left, (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2, cv2.LINE_AA)
    (tw, _), _ = cv2.getTextSize(right, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    cv2.putText(frame, right, (max(10, w - tw - 10), 36), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2, cv2.LINE_AA)

def draw_box(frame: np.ndarray, b: DetBox):
    cv2.rectangle(frame, (b.x1, b.y1), (b.x2, b.y2), (0,255,0), 2)
    cv2.putText(frame, f"{b.conf:.2f}", (b.x1, max(20, b.y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2, cv2.LINE_AA)

def draw_inset(frame: np.ndarray, crop: Optional[np.ndarray]):
    if crop is None or crop.size == 0:
        return
    h, w = frame.shape[:2]
    inset_w = int(w * 0.25)
    inset_h = int(inset_w * 0.35)
    thumb = cv2.resize(crop, (inset_w, inset_h), interpolation=cv2.INTER_LINEAR)
    x2, y2 = w - 10, h - 10
    x1, y1 = x2 - inset_w, y2 - inset_h
    cv2.rectangle(frame, (x1-3, y1-3), (x2+3, y2+3), (0,0,0), -1)
    frame[y1:y2, x1:x2] = thumb
    cv2.rectangle(frame, (x1, y1), (x2, y2), (255,255,255), 2)

# ----------------------------
# Event model
# ----------------------------
@dataclass
class PlateEvent:
    ts_ms: int
    det_conf: float
    bbox_main: Tuple[int,int,int,int]
    crop_bgr: np.ndarray
    annot_frame_bgr: Optional[np.ndarray]


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--source", choices=["picamera","video","images"], default="picamera")
    ap.add_argument("--camera-index", type=int, default=0)
    ap.add_argument("--video-path", type=str, default="")
    ap.add_argument("--images-folder", type=str, default="")
    ap.add_argument("--images-rate", type=float, default=10.0)
    ap.add_argument("--loop", action="store_true")

    ap.add_argument("--main-w", type=int, default=960)
    ap.add_argument("--main-h", type=int, default=540)
    ap.add_argument("--lores-w", type=int, default=640)
    ap.add_argument("--lores-h", type=int, default=360)
    ap.add_argument("--cam-fps", type=int, default=30)

    ap.add_argument("--ncnn-model", type=str, required=True)
    ap.add_argument("--plate-class-id", type=int, default=0)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--det-conf", type=float, default=0.25)
    ap.add_argument("--det-iou", type=float, default=0.45)

    # Smoothness knobs
    ap.add_argument("--infer-every", type=int, default=2, help="Run YOLO every N frames (demo default: 2)")
    ap.add_argument("--stable-hits", type=int, default=2, help="Require N consistent detections before OCR event")
    ap.add_argument("--stable-iou", type=float, default=0.5, help="IoU threshold for consistent detections")
    ap.add_argument("--event-min-conf", type=float, default=0.45, help="Min det conf to trigger OCR")
    ap.add_argument("--crop-pad", type=float, default=0.08, help="Pad bbox by fraction (helps OCR)")

    # OCR
    ap.add_argument("--ocr-min-conf", type=float, default=0.55)
    ap.add_argument("--ocr-min-len", type=int, default=7)

    # TX 
    ap.add_argument("--transport", choices=["none","tcp","rfcomm"], default="none")
    ap.add_argument("--tcp-host", type=str, default="192.168.0.2")
    ap.add_argument("--tcp-port", type=int, default=5005)
    ap.add_argument("--bt-addr", type=str, default="")
    ap.add_argument("--bt-channel", type=int, default=1)

    # TX toggles
    ap.add_argument("--tx-json", action="store_true")
    ap.add_argument("--tx-metrics", action="store_true")
    ap.add_argument("--tx-crop", action="store_true")
    ap.add_argument("--tx-frame", action="store_true")
    ap.add_argument("--jpeg-quality", type=int, default=80)

    # UI
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--no-display", action="store_true")

    args = ap.parse_args()

    # Source
    if args.source == "picamera":
        src = PiCamera2Source(
            main_size=(args.main_w, args.main_h),
            lores_size=(args.lores_w, args.lores_h),
            fps=args.cam_fps,
            camera_index=args.camera_index,
        )
    elif args.source == "video":
        if not args.video_path:
            raise SystemExit("--video-path required")
        src = VideoSource(args.video_path, (args.lores_w, args.lores_h), args.loop)
    else:
        if not args.images_folder:
            raise SystemExit("--images-folder required")
        src = ImagesSource(args.images_folder, (args.lores_w, args.lores_h), args.images_rate, args.loop)

    detector = YOLOPlateDetectorNCNN(args.ncnn_model, args.imgsz, args.det_conf, args.det_iou, args.plate_class_id)    
    ocr = TesseractUKPlateOCR()
    metrics = MetricsSampler(1.0)
    metrics.start()

    # Transport
    if args.transport == "none":
        transport: Transport = NullTransport()
    elif args.transport == "tcp":
        transport = TCPTransport(args.tcp_host, args.tcp_port)
        try:
            transport.send(
                {"type": "hello", "ts_ms": now_ms(), "msg": "pi demo started", "attachments_order": []},
                {}
            )
            print("Sent hello to laptop")
        except Exception as e:
            print("Hello send failed:", e)
    else:
        if not args.bt_addr:
            raise SystemExit("--bt-addr required for rfcomm")
        transport = RFCOMMTransport(args.bt_addr, args.bt_channel)

    # Workers
    stop = threading.Event()
    plate_q: "queue.Queue[PlateEvent]" = queue.Queue(maxsize=100)
    tx_q: "queue.Queue[Tuple[Dict[str,Any], Dict[str,bytes]]]" = queue.Queue(maxsize=50)

    last_plate = ""
    last_plate_conf = 0.0
    last_plate_ts = 0
    last_crop_for_inset: Optional[np.ndarray] = None

    # “stable detection” state
    stable_count = 0
    prev_box_main: Optional[DetBox] = None
    last_event_time = 0.0

    def ocr_worker():
        nonlocal last_plate, last_plate_conf, last_plate_ts, last_crop_for_inset
        import json
        while not stop.is_set():
            try:
                ev = plate_q.get(timeout=0.05)
            except queue.Empty:
                continue
            
            h, w = ev.crop_bgr.shape[:2]
            if w < 30 or h < 15:
                continue
            
            crop = ev.crop_bgr
            crop = cv2.resize(crop, (320, 80), interpolation=cv2.INTER_CUBIC)

            plate, ocr_conf = ocr.infer(crop)
               
            if len(plate) < args.ocr_min_len or ocr_conf < args.ocr_min_conf:
                continue

            last_plate = plate
            last_plate_conf = ocr_conf
            last_plate_ts = ev.ts_ms
            last_crop_for_inset = ev.crop_bgr

            header = {
                "type": "plate_event",
                "ts_ms": ev.ts_ms,
                "plate": plate,
                "ocr_conf": ocr_conf,
                "det_conf": ev.det_conf,
                "bbox_main": list(ev.bbox_main),
                "metrics": metrics.get() if args.tx_metrics else None,
                "attachments_order": [],
            }
            attachments: Dict[str, bytes] = {}

            if args.tx_crop:
                header["attachments_order"].append("crop_jpg")
                attachments["crop_jpg"] = jpeg_encode(ev.crop_bgr, args.jpeg_quality)

            if args.tx_frame and ev.annot_frame_bgr is not None:
                header["attachments_order"].append("frame_jpg")
                attachments["frame_jpg"] = jpeg_encode(ev.annot_frame_bgr, args.jpeg_quality)

            if not args.tx_json:
                header.pop("metrics", None)

            try:
                print(f"[TX] plate_event plate={plate} det={ev.det_conf:.2f} ocr={ocr_conf:.2f}")
                tx_q.put_nowait((header, attachments))
            except queue.Full:
                pass
            
    def tx_worker():
        while not stop.is_set():
            try:
                header, attachments = tx_q.get(timeout=0.05)
            except queue.Empty:
                continue
            transport.send(header, attachments)
            
    t1 = threading.Thread(target=ocr_worker, daemon=True)
    t2 = threading.Thread(target=tx_worker, daemon=True)
    t1.start()
    t2.start()
    
    # UI
    if not args.no_display:
        cv2.namedWindow("ALPR Demo", cv2.WINDOW_NORMAL)
        if args.fullscreen:
            cv2.setWindowProperty("ALPR Demo", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    # Main loop FPS
    frame_count = 0
    t0 = time.time()
    fps = 0.0
    infer_counter = 0
    latest_box_main: Optional[DetBox] = None
    
    try:
        while True:
            main_bgr, lo_bgr, ts = src.read()
            if main_bgr is None:
                break

            frame_count += 1
            dt = time.time() - t0
            if dt >= 0.5:
                fps = frame_count / dt
                frame_count = 0
                t0 = time.time()

            infer_counter += 1
            do_infer = (args.infer_every <= 1) or (infer_counter % args.infer_every == 0)

            if do_infer and lo_bgr is not None:
                det = detector.infer_one(lo_bgr)

                if det is None:
                    stable_count = 0
                    prev_box_main = None
                    latest_box_main = None
                else:
                    sx = main_bgr.shape[1] / float(lo_bgr.shape[1])
                    sy = main_bgr.shape[0] / float(lo_bgr.shape[0])
                    box_main = DetBox(
                        int(det.x1 * sx), int(det.y1 * sy),
                        int(det.x2 * sx), int(det.y2 * sy),
                        det.conf
                    )
                    latest_box_main = box_main

                    if prev_box_main is not None and iou(prev_box_main, box_main) >= args.stable_iou:
                        stable_count += 1
                    else:
                        stable_count = 1
                    prev_box_main = box_main

                    # trigger OCR event if stable + confident + simple cooldown
                    if box_main.conf >= args.event_min_conf and stable_count >= args.stable_hits:
                        now_t = time.time()
                        if now_t - last_event_time > 0.35:
                            last_event_time = now_t

                            # padded crop
                            x1, y1, x2, y2 = box_main.x1, box_main.y1, box_main.x2, box_main.y2
                            bw, bh = (x2 - x1), (y2 - y1)
                            pad_x = int(bw * args.crop_pad)
                            pad_y = int(bh * args.crop_pad)
                            x1 = clamp(x1 - pad_x, 0, main_bgr.shape[1] - 1)
                            y1 = clamp(y1 - pad_y, 0, main_bgr.shape[0] - 1)
                            x2 = clamp(x2 + pad_x, 0, main_bgr.shape[1] - 1)
                            y2 = clamp(y2 + pad_y, 0, main_bgr.shape[0] - 1)
                            crop = main_bgr[y1:y2, x1:x2].copy()

                            frame_for_tx = main_bgr.copy() if args.tx_frame else None
                            ev = PlateEvent(ts_ms=ts, det_conf=box_main.conf, bbox_main=(x1,y1,x2,y2),
                                            crop_bgr=crop, annot_frame_bgr=frame_for_tx)
                            try:
                                plate_q.put_nowait(ev)
                            except queue.Full:
                                pass

            # draw display
            if latest_box_main is not None:
                draw_box(main_bgr, latest_box_main)

            age_s = (ts - last_plate_ts) / 1000.0 if last_plate_ts else 999.0
            draw_hud(main_bgr, fps, metrics.get(), last_plate, last_plate_conf, age_s)
            draw_inset(main_bgr, last_crop_for_inset)

            if not args.no_display:
                display_frame = main_bgr[:, :, ::-1]  # BGR to RGB for display
                cv2.imshow("ALPR Demo", display_frame)
                k = cv2.waitKey(1) & 0xFF
                if k in (ord("q"), 27):
                    break

    finally:
        stop.set()
        src.release()
        metrics.stop()
        if not args.no_display:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
