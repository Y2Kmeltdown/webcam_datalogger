#!/usr/bin/env python3
"""
Webcam Recorder
UVC webcam application (e.g. Raspberry Pi HQ Camera on the Arducam B0278
CSI-to-USB UVC adapter, or any standard webcam) with:
  - Config-file driven startup parameters
  - Continuous segmented recording: raw BGR frames are piped to ffmpeg, whose
    segment muxer rolls to a new UTC-timestamped MP4 at the next keyframe —
    the encoder and camera pipeline never stop between segments
  - Uninterrupted raw frame streaming over a Unix domain socket
  - Camera controls applied at startup through v4l2-ctl (standard UVC/V4L2
    control IDs — the reliable path for the B0278; OpenCV CAP_PROP exposure
    semantics are inconsistent across backends)
"""

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2

# Some Windows Python builds don't expose socket.AF_UNIX even though the OS
# has supported Unix sockets since Win10 17063 — the value is 1 everywhere.
AF_UNIX = getattr(socket, "AF_UNIX", 1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("webcam")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "device": 0,                          # index or "/dev/videoX"
    "backend": "auto",                    # auto/v4l2/dshow/msmf/any
    "resolution": {"width": 1920, "height": 1080},
    "framerate": 30,
    "fourcc": "MJPG",                     # B0278 is MJPG-only; USB2 needs it
    "encoder": {
        # ffmpeg output options, inserted between the raw input and the
        # segment muxer. A keyframe interval (-g) of ~2 s is appended
        # automatically if not present here.
        "options": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
    },
    "output_dir": "recordings",
    "socket_path": "/tmp/webcam_frames.sock",
    "controls": {
        # V4L2/UVC control names applied via `v4l2-ctl -c name=value`.
        "auto_exposure": 3,               # 1 = manual, 3 = aperture priority
        "white_balance_automatic": True,
    },
}


def load_config(path: str) -> dict:
    """Load JSON config and merge over built-in defaults."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["encoder"] = dict(DEFAULT_CONFIG["encoder"])
    cfg["controls"] = dict(DEFAULT_CONFIG["controls"])

    config_path = Path(path)
    if not config_path.exists():
        log.warning("Config '%s' not found — using built-in defaults.", path)
        return cfg

    with config_path.open() as fh:
        user_cfg = json.load(fh)

    for key, value in user_cfg.items():
        if key.startswith("_"):
            continue
        if key == "controls" and isinstance(value, dict):
            cfg["controls"].update(value)
        else:
            cfg[key] = value

    log.info("Loaded config from '%s'.", path)
    return cfg


def resolve_device(device) -> tuple:
    """Return (opencv_arg, v4l2_device_path) for a config 'device' value."""
    if isinstance(device, int) or (isinstance(device, str) and device.isdigit()):
        idx = int(device)
        return idx, f"/dev/video{idx}"
    return device, str(device)


BACKENDS = {
    "v4l2": cv2.CAP_V4L2,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}


def resolve_backend(name: str) -> int:
    """
    Map the config 'backend' to an OpenCV capture backend id.
    "auto" picks per platform: V4L2 on Linux, DirectShow on Windows,
    OpenCV's own choice anywhere else.
    """
    name = (name or "auto").lower()
    if name == "auto":
        if sys.platform.startswith("linux"):
            return cv2.CAP_V4L2
        if sys.platform == "win32":
            return cv2.CAP_DSHOW
        return cv2.CAP_ANY
    if name not in BACKENDS:
        raise ValueError(
            f"Unknown backend '{name}' (choose auto/v4l2/dshow/msmf/any)."
        )
    return BACKENDS[name]


# ---------------------------------------------------------------------------
# Camera controls (v4l2-ctl)
# ---------------------------------------------------------------------------

def apply_controls(dev_path: str, controls: dict):
    """
    Apply camera controls with `v4l2-ctl -d <dev> -c name=value`, in config
    order. Order matters for the B0278: auto_exposure=1 (manual) must be set
    before exposure_time_absolute, and white_balance_automatic=0 before
    white_balance_temperature.
    """
    active = {k: v for k, v in controls.items() if not k.startswith("_")}
    if not active:
        return

    if not sys.platform.startswith("linux"):
        log.info("v4l2-ctl controls are Linux-only — skipping on this "
                 "platform (%s).", sys.platform)
        return

    if not shutil.which("v4l2-ctl"):
        log.warning("v4l2-ctl not found — camera controls NOT applied "
                    "(install v4l-utils).")
        return

    for name, value in active.items():
        val = ("1" if value else "0") if isinstance(value, bool) else str(value)
        res = subprocess.run(
            ["v4l2-ctl", "-d", dev_path, "-c", f"{name}={val}"],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            log.warning("Control %s=%s rejected: %s",
                        name, val, (res.stderr or res.stdout).strip())
        else:
            log.info("Control applied: %s=%s", name, val)

    if log.isEnabledFor(logging.DEBUG):
        dump = subprocess.run(["v4l2-ctl", "-d", dev_path, "--list-ctrls"],
                              capture_output=True, text=True)
        log.debug("Current controls:\n%s", dump.stdout)


# ---------------------------------------------------------------------------
# Unix-socket frame server
# ---------------------------------------------------------------------------

class FrameSocketServer:
    """
    Broadcasts raw BGR24 frames to any number of Unix-socket clients.

    Wire format per frame:
        ┌──────────────────────┬──────────────────────────┬────────────────────┐
        │ 4 bytes (uint32 LE)  │  8 bytes (uint64 LE)     │  N bytes           │
        │ payload length       │  capture timestamp (µs)  │  raw BGR24 pixels  │
        │                      │  microseconds since epoch│                    │
        └──────────────────────┴──────────────────────────┴────────────────────┘

    The timestamp is recorded immediately after VideoCapture.read() returns,
    giving the closest possible approximation to when the host received the frame.
    """

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False

    def start(self):
        if Path(self.socket_path).exists():
            os.unlink(self.socket_path)
        self._server = socket.socket(AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(8)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="sock-accept"
        )
        self._accept_thread.start()
        log.info("Frame socket listening at '%s'.", self.socket_path)

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            for client in self._clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()
        if Path(self.socket_path).exists():
            os.unlink(self.socket_path)
        log.info("Frame socket server stopped.")

    def _accept_loop(self):
        while self._running:
            try:
                self._server.settimeout(1.0)
                conn, _ = self._server.accept()
                with self._lock:
                    self._clients.append(conn)
                log.info("New frame client (total: %d).", len(self._clients))
            except socket.timeout:
                continue
            except OSError:
                break

    def send_frame(self, frame_bytes: bytes, timestamp_us: int):
        """
        Broadcast one frame to all connected clients.

        Args:
            frame_bytes:   Raw BGR24 pixel data.
            timestamp_us:  Capture time as microseconds since the Unix epoch
                           (int(time.time() * 1_000_000) at the capture site).
        """
        if not self._clients:
            return
        # Pack length (4 bytes) + timestamp (8 bytes) + pixels
        header = struct.pack("<IQ", len(frame_bytes), timestamp_us)
        payload = header + frame_bytes
        dead: list[socket.socket] = []
        with self._lock:
            for client in self._clients:
                try:
                    client.sendall(payload)
                except (BrokenPipeError, OSError):
                    dead.append(client)
            for d in dead:
                self._clients.remove(d)
                try:
                    d.close()
                except OSError:
                    pass
        if dead:
            log.info("%d client(s) dropped (remaining: %d).", len(dead), len(self._clients))


# ---------------------------------------------------------------------------
# Segmenting encoder (ffmpeg subprocess)
# ---------------------------------------------------------------------------

def build_ffmpeg_cmd(cfg: dict, segment_duration: float,
                     width: int, height: int, fps: float) -> list[str]:
    """
    Assemble the ffmpeg pipeline: raw BGR24 on stdin → encoder options from
    config → segment muxer writing UTC-timestamped MP4s. The muxer cuts each
    segment at the first keyframe after `segment_duration` seconds, so the
    encoder never stops and no frames are lost between segments.
    """
    options = [str(o) for o in cfg["encoder"].get("options", [])]
    if "-g" not in options:
        options += ["-g", str(max(1, int(round(fps * 2))))]  # keyframe every ~2 s

    out_pattern = os.path.join(cfg["output_dir"], "%Y%m%dT%H%M%SZ_webcam.mp4")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", f"{fps:g}",
        "-i", "pipe:0",
        "-an",
        *options,
        "-f", "segment",
        "-segment_time", f"{segment_duration:g}",
        "-reset_timestamps", "1",
        "-strftime", "1",          # expands the pattern in UTC (use_localtime default off)
        out_pattern,
    ]


class SegmentingEncoder:
    """
    Owns the ffmpeg subprocess. If ffmpeg dies it is respawned (a fresh
    segment starts automatically); after MAX_RESTARTS rapid failures,
    recording is disabled while the frame socket keeps running.
    """

    MAX_RESTARTS = 3
    RESTART_WINDOW = 30.0  # seconds

    def __init__(self, cfg: dict, segment_duration: float,
                 width: int, height: int, fps: float):
        os.makedirs(cfg["output_dir"], exist_ok=True)
        self._cmd = build_ffmpeg_cmd(cfg, segment_duration, width, height, fps)
        self._proc: subprocess.Popen | None = None
        self._restarts: list[float] = []
        self.disabled = False
        self._spawn()

    # ------------------------------------------------------------------
    def _spawn(self):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg not found on PATH — install ffmpeg "
                "(Raspberry Pi OS: sudo apt install ffmpeg)."
            )
        log.info("Starting encoder: %s", " ".join(self._cmd))
        self._proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE)

    # ------------------------------------------------------------------
    def _restart(self, reason: str):
        now = time.monotonic()
        self._restarts = [t for t in self._restarts
                          if now - t < self.RESTART_WINDOW]
        if len(self._restarts) >= self.MAX_RESTARTS:
            self.disabled = True
            self._proc = None
            log.error(
                "ffmpeg failed %d times within %.0f s (%s) — recording "
                "disabled; frame socket still live.",
                self.MAX_RESTARTS, self.RESTART_WINDOW, reason,
            )
            return
        self._restarts.append(now)
        log.warning("Restarting ffmpeg (%s).", reason)
        self._spawn()

    # ------------------------------------------------------------------
    def write_frame(self, data: bytes):
        if self.disabled or self._proc is None:
            return
        rc = self._proc.poll()
        if rc is not None:
            self._restart(f"exited with code {rc}")
            if self.disabled or self._proc is None:
                return
        try:
            self._proc.stdin.write(data)
        except (BrokenPipeError, OSError) as exc:
            self._restart(f"pipe error: {exc}")

    # ------------------------------------------------------------------
    def stop(self):
        """Close stdin so ffmpeg flushes and finalizes the last segment."""
        proc, self._proc = self._proc, None
        self.disabled = True
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.info("Encoder stopped.")


# ---------------------------------------------------------------------------
# Main recorder
# ---------------------------------------------------------------------------

class WebcamRecorder:
    MAX_GRAB_FAILURES = 50  # consecutive read() failures before giving up

    def __init__(self, cfg: dict, segment_duration: float):
        self.cfg = cfg
        self.segment_duration = segment_duration
        self.socket_server = FrameSocketServer(cfg["socket_path"])
        self.cap: cv2.VideoCapture | None = None
        self._running = False
        self.width = 0
        self.height = 0
        self.fps = 0.0

    # ------------------------------------------------------------------
    def configure(self):
        res = self.cfg["resolution"]
        w, h = int(res["width"]), int(res["height"])
        fps = float(self.cfg["framerate"])
        fourcc = str(self.cfg["fourcc"])
        if len(fourcc) != 4:
            raise ValueError(f"fourcc must be exactly 4 characters, got '{fourcc}'.")

        dev_arg, dev_path = resolve_device(self.cfg["device"])
        backend = resolve_backend(str(self.cfg.get("backend", "auto")))
        log.info("Opening %s (%dx%d @ %g fps, FOURCC %s, backend %s) …",
                 dev_path, w, h, fps, fourcc, self.cfg.get("backend", "auto"))

        cap = cv2.VideoCapture(dev_arg, backend)
        if not cap.isOpened():
            raise RuntimeError(
                f"Could not open camera '{dev_arg}' "
                f"(backend {self.cfg.get('backend', 'auto')})."
            )

        # FOURCC first: many UVC drivers only expose high resolutions under MJPG
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimize latency, drop stale frames

        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = cap.get(cv2.CAP_PROP_FPS) or fps
        actual_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc_str = "".join(
            chr((actual_fourcc >> (8 * i)) & 0xFF) for i in range(4)
        )

        if (self.width, self.height) != (w, h):
            log.warning("Camera negotiated %dx%d instead of requested %dx%d.",
                        self.width, self.height, w, h)
        if abs(self.fps - fps) > 0.5:
            log.warning("Camera negotiated %g fps instead of requested %g.",
                        self.fps, fps)
        log.info("Capture format: %dx%d @ %g fps, FOURCC '%s'.",
                 self.width, self.height, self.fps, fourcc_str)

        self.cap = cap
        self._calibrate_fps()
        apply_controls(dev_path, self.cfg["controls"])

    # ------------------------------------------------------------------
    def _calibrate_fps(self, warm_up: int = 5, sample: int = 30):
        """
        Measure the real delivery rate by timing `sample` frames. Some
        backends (notably DirectShow on Windows) ignore CAP_PROP_FPS and
        stream at the driver's default rate — if the encoder is told the
        wrong rate, recorded video plays back time-stretched. Fall back to
        the negotiated value if the measurement looks implausible.
        """
        for _ in range(warm_up):
            self.cap.read()
        start = time.monotonic()
        grabbed = 0
        while grabbed < sample:
            ok, frame = self.cap.read()
            if ok and frame is not None:
                grabbed += 1
        measured = sample / (time.monotonic() - start)

        if not (1.0 <= measured <= 240.0):
            log.warning("Implausible measured rate %.1f fps — keeping %g fps.",
                        measured, self.fps)
            return
        if abs(measured - self.fps) / self.fps > 0.05:
            log.warning("Camera delivers %.1f fps, not the requested %g — "
                        "recording at the measured rate.", measured, self.fps)
            self.fps = measured
        else:
            log.info("Measured capture rate: %.1f fps.", measured)

    # ------------------------------------------------------------------
    def run(self):
        self.socket_server.start()
        encoder = SegmentingEncoder(
            self.cfg, self.segment_duration, self.width, self.height, self.fps
        )
        self._running = True

        log.info(
            "Recording continuously. Segment length: %.1f s. "
            "Press Ctrl+C or send SIGTERM to stop.",
            self.segment_duration,
        )

        failures = 0
        frames = 0
        stat_start = time.monotonic()
        try:
            while self._running:
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= self.MAX_GRAB_FAILURES:
                        raise RuntimeError(
                            f"Camera stopped delivering frames "
                            f"({failures} consecutive failures)."
                        )
                    log.warning("Frame grab failed (%d/%d).",
                                failures, self.MAX_GRAB_FAILURES)
                    time.sleep(0.2)
                    continue
                failures = 0

                # Timestamp recorded immediately after capture returns —
                # closest approximation to when the host received the frame.
                timestamp_us = int(time.time() * 1_000_000)
                data = frame.tobytes()

                self.socket_server.send_frame(data, timestamp_us)
                encoder.write_frame(data)

                frames += 1
                elapsed = time.monotonic() - stat_start
                if elapsed >= 15.0:
                    log.info("Capture rate: %.1f fps.", frames / elapsed)
                    frames = 0
                    stat_start = time.monotonic()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down.")

        self._stop(encoder)

    # ------------------------------------------------------------------
    def _stop(self, encoder: SegmentingEncoder):
        log.info("Stopping recorder …")
        self._running = False
        encoder.stop()
        if self.cap is not None:
            self.cap.release()
        self.socket_server.stop()
        log.info("Shutdown complete.")

    # ------------------------------------------------------------------
    def request_stop(self):
        """Thread-safe stop trigger (used by signal handler)."""
        self._running = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Webcam Recorder — continuously records fixed-length MP4 segments "
            "labelled with UTC timestamps while streaming raw frames over a "
            "Unix domain socket. Runs indefinitely until Ctrl+C or SIGTERM."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", "-c", default="camera_config.json", metavar="FILE",
                        help="Path to JSON configuration file.")
    parser.add_argument("--segment-duration", "-d", type=float, default=60.0, metavar="SECONDS",
                        help="Length of each recorded video segment in seconds.")
    parser.add_argument("--device", default=None, metavar="DEV",
                        help="Override camera index or /dev/videoX path from config.")
    parser.add_argument("--socket", "-s", default=None, metavar="PATH",
                        help="Override the Unix socket path from config.")
    parser.add_argument("--output-dir", "-o", default=None, metavar="DIR",
                        help="Override the output directory from config.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.segment_duration <= 0:
        log.error("--segment-duration must be a positive number.")
        sys.exit(1)

    cfg = load_config(args.config)

    if args.device is not None:
        cfg["device"] = int(args.device) if args.device.isdigit() else args.device
    if args.socket:
        cfg["socket_path"] = args.socket
    if args.output_dir:
        cfg["output_dir"] = args.output_dir

    recorder = WebcamRecorder(cfg, segment_duration=args.segment_duration)

    def _on_sigterm(signum, frame):
        log.info("SIGTERM received.")
        recorder.request_stop()

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        recorder.configure()
        recorder.run()
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
