# Webcam Recorder

A UVC webcam application (OpenCV + ffmpeg) that:
- **Records video** to disk in continuous UTC-timestamped MP4 segments
- **Streams raw frames** to any process via a Unix domain socket
- **Reads startup parameters** from a JSON config file
- **Applies camera controls** (exposure, gain, white balance, …) at startup via `v4l2-ctl`

Built for the **Arducam B0278 CSI-to-USB UVC adapter** carrying the Raspberry Pi
HQ Camera (Sony IMX477) — the camera enumerates as a standard UVC webcam — but
it works with any UVC camera.

---

## Eventide module

This repository is an installable [Eventide](https://github.com/Y2Kmeltdown/eventide)
module — `eventide-module.json` at the repo root advertises **two** supervisor
services that install together from the dashboard's MODULES tab:

| Service | What it does |
| ------- | ------------ |
| `webcam_datalogger` | Runs `camera_app.py` — segmented MP4 recording into `<recordings_dir>/webcam/`, raw frames published to `/tmp/webcam_frames.sock`. |
| `webcam_mjpeg_server` | Runs `mjpeg_server.py` — consumes the frame socket and serves an MJPEG live stream on port `8087` (nginx proxies it at `/stream/webcam/`). |

Install: open the dashboard → **MODULES** → paste this repo's URL → INSTALL.
The installer creates a dedicated Python venv for the module, installs
`requirements.txt` into it, registers both services with supervisord, and
starts them. `ffmpeg` and `v4l-utils` are installed system-wide via apt
(declared in the manifest).

Manual usage below still works standalone — under Eventide, supervisord runs
the programs for you.

---

## Requirements

### Hardware
- Raspberry Pi 5 (or any Linux host)
- Arducam B0278 CSI-USB UVC adapter + Raspberry Pi HQ Camera (IMX477), **or** any UVC webcam

### Software
```
# System packages (Raspberry Pi OS Bookworm)
sudo apt install ffmpeg v4l-utils python3-opencv

# Python packages
pip install aiohttp numpy opencv-python
```

---

## Project layout

```
webcam_datalogger/
├── camera_app.py        # Main application
├── check_camera.py      # Quick "is the camera connected and streaming?" check
├── mjpeg_server.py      # MJPEG live-stream server (REST-tunable)
├── frame_client.py      # Example frame consumer (OpenCV display)
├── camera_config.json   # Startup configuration
└── README.md
```

---

## First check: is the camera detected?

```bash
python check_camera.py
```

Lists `/dev/video*` nodes, dumps the supported formats and controls
(via `v4l2-ctl`), opens the camera with OpenCV, and captures ~2 s of frames
reporting the measured frame rate and mean brightness. Exit code 0 = OK.
Handy overrides: `--device /dev/video1 --width 1280 --height 720 --fps 60`.

---

## Usage

### Basic recording
```bash
# Record 60-second segments using default config (runs until Ctrl+C)
python camera_app.py

# Custom segment length and camera device
python camera_app.py --segment-duration 30 --device /dev/video1

# Use a different config file
python camera_app.py --config /etc/webcam/prod.json

# Override the socket path at runtime
python camera_app.py --socket /run/webcam.sock

# Verbose / debug logging (includes a full v4l2-ctl --list-ctrls dump)
python camera_app.py --verbose
```

### All CLI flags
| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--config` | `-c` | `camera_config.json` | JSON config file path |
| `--segment-duration` | `-d` | `60.0` | Length of each recorded segment in seconds |
| `--device` | | value from config | Camera index (`0`) or device path (`/dev/video0`) |
| `--socket` | `-s` | value from config | Unix socket path override |
| `--output-dir` | `-o` | value from config | Recordings directory override |
| `--verbose` | `-v` | off | Enable DEBUG logging |

### Live MJPEG stream
```bash
python mjpeg_server.py --port 8082
# Viewer page:   http://<host>:8082/
# MJPEG stream:  http://<host>:8082/stream
# Snapshot:      http://<host>:8082/snapshot
# Settings API:  curl -X PUT http://<host>:8082/api/settings -d '{"quality":60}' -H 'Content-Type: application/json'
```

### Consuming frames from another process
While `camera_app.py` is running, connect to the Unix socket and read
length+timestamp-prefixed frames:

```bash
# Run the bundled OpenCV display client
python frame_client.py
```

---

## Wire protocol (Unix socket)

Every frame is prefixed with its byte length and capture timestamp:

```
┌──────────────────────┬──────────────────────────┬────────────────────┐
│  4 bytes (uint32 LE) │  8 bytes (uint64 LE)     │  N bytes           │
│  payload length      │  capture timestamp (µs)  │  raw BGR24 pixels  │
└──────────────────────┴──────────────────────────┴────────────────────┘
```

Multiple clients can connect simultaneously; each receives every frame.
`payload length` is always `width × height × 3` (OpenCV's native BGR order).

---

## Configuration file (`camera_config.json`)

```jsonc
{
  "device": 0,                      // camera index or "/dev/video0"
  "resolution": { "width": 1920, "height": 1080 },
  "framerate": 60,
  "fourcc": "MJPG",                 // B0278 is MJPG-only; USB2 needs it at 1080p+
  "encoder": {
    // ffmpeg output options between raw input and segment muxer;
    // "-g <2×fps>" is appended automatically if absent.
    "options": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    // Pi hardware encode alternative:
    // "options": ["-c:v", "h264_v4l2m2m", "-b:v", "8M"]
  },
  "output_dir": "recordings",
  "socket_path": "/tmp/webcam_frames.sock",
  "controls": {
    // V4L2 control names → values, applied in order via v4l2-ctl (see below)
    "auto_exposure": 3,             // 1 = manual, 3 = aperture priority
    "white_balance_automatic": true
  }
}
```

Any key omitted from the file falls back to the built-in default.
Note there is only **one** resolution: the same frames feed both the
recordings and the socket (the MJPEG server downscales for streaming).

If the camera rejects the requested mode, the app logs the negotiated
resolution/framerate and continues with it.

---

## Camera controls (v4l2-ctl / UVC)

The `controls` section maps directly to V4L2 control names and is applied at
startup with `v4l2-ctl -d <device> -c name=value`, **in listed order** (order
matters: manual mode must be enabled before its value, e.g. `auto_exposure=1`
before `exposure_time_absolute`). Inspect the actual control set of your
camera with:

```bash
v4l2-ctl -d /dev/video6 --list-ctrls          # all controls + ranges
v4l2-ctl -d /dev/video6 --list-formats-ext    # supported resolutions/framerates
```

### Arducam B0278 + Pi HQ Camera (IMX477) — confirmed control set

Yes, the HQ camera's parameters **can** be modified through the adapter at
runtime. It is a standard UVC device; its onboard ISP/firmware implements the
usual UVC processing-unit/camera-terminal controls, which Linux exposes as
ordinary V4L2 controls (verified against real-device `v4l2-ctl -l` dumps):

| Control | Range | Notes |
| ------- | ----- | ----- |
| `brightness` | -64 … 64 | |
| `contrast` | 0 … 64 | |
| `saturation` | 0 … 128 | |
| `hue` | -40 … 40 | |
| `gamma` | 72 … 500 | |
| `gain` | 0 … 100 | |
| `sharpness` | 0 … 6 | |
| `backlight_compensation` | 0 … 2 | |
| `white_balance_automatic` | 0/1 | |
| `white_balance_temperature` | 2800 … 6500 K | only when auto WB off |
| `power_line_frequency` | 0=off, 1=50 Hz, 2=60 Hz | anti-flicker |
| `auto_exposure` | 1=manual, 3=aperture priority | set 1 before the line below |
| `exposure_time_absolute` | 1 … 5000 | units of 0.1 ms → **0.1 ms … 500 ms** |
| `exposure_dynamic_framerate` | 0/1 | |

Supported modes: 4032×3040 @ 10 fps, 3840×2160 @ 20, 2592×1944 / 2560×1440 @
30, 1920×1080 @ 60, 1280×720 @ 100. Output is **MJPG only**.

Known limitations of the adapter (firmware, not this software):
- **No raw Bayer / no sensor-register access** — the adapter's own ISP does
  debayer, AE/AWB; you get processed frames only.
- **Exposure capped at 0.5 s** (vs ~200 s over CSI/libcamera); longer needs
  custom firmware from Arducam.
- **No manual focus control** — correct: the HQ camera has a manual C/CS lens.
  (`focus_automatic_continuous` is exposed but is a firmware leftover.)
- Low-light auto-exposure tuning is widely reported as poor vs the same
  sensor on CSI; for dark scenes prefer manual exposure + gain.
- `v4l2-ctl` (not OpenCV `CAP_PROP_*`) is the reliable way to drive these —
  OpenCV's V4L2 exposure semantics are inconsistent, which is why this module
  applies controls through `v4l2-ctl`.

---

## How recording works

`camera_app.py` captures BGR frames with OpenCV and pipes them to an
`ffmpeg` subprocess whose **segment muxer** writes
`<output_dir>/%Y%m%dT%H%M%SZ_webcam.mp4`, rolling to a new file at the first
keyframe after each `--segment-duration` boundary. The camera and encoder
never stop between segments, so no frames are lost at the roll. If ffmpeg
crashes it is respawned automatically (a fresh segment starts); after 3 rapid
failures, recording is disabled while the frame socket stays live.

At startup the app **measures the real frame delivery rate** over ~30 frames
and records at that rate — some backends (DirectShow on Windows in
particular) ignore the requested framerate, and encoding at the wrong rate
would time-stretch the video.

The OpenCV capture backend is selectable via `backend` in the config:
`auto` (default) uses V4L2 on Linux and DirectShow on Windows; `v4l2`,
`dshow`, `msmf` and `any` are also accepted. Camera controls via `v4l2-ctl`
and the Unix frame socket are Linux features — on Windows the controls step
is skipped (drive exposure etc. with AMCap or OpenCV `CAP_PROP_*` instead).

---

## Run as a systemd service (optional)

```ini
# /etc/systemd/system/webcam.service
[Unit]
Description=Webcam Recorder
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/webcam/camera_app.py --segment-duration 60 --config /etc/webcam/config.json
WorkingDirectory=/opt/webcam
Restart=on-failure
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now webcam
```
