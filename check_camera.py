#!/usr/bin/env python3
"""
check_camera.py — verify the UVC camera is connected and delivering frames.

Intended as a first-run sanity check on the Linux payload (Raspberry Pi OS),
but works anywhere OpenCV does.

What it does:
  1. Lists /dev/video* devices (Linux) and, when v4l2-ctl is available,
     prints the supported formats/resolutions and the camera control list.
  2. Opens the camera with OpenCV using the requested FOURCC / resolution /
     framerate (defaults taken from camera_config.json) and reports what the
     camera actually negotiated.
  3. Captures ~2 seconds of frames and reports the measured frame rate and
     mean brightness — a near-zero brightness with a covered lens is normal,
     so check it with the lens uncovered.

Exit code 0 = camera OK, 1 = a check failed.

Usage:
    python check_camera.py                          # use camera_config.json
    python check_camera.py --device /dev/video0
    python check_camera.py --width 1280 --height 720 --fps 60
"""

import argparse
import getpass
import glob
import os
import shutil
import stat
import subprocess
import sys
import time

try:
    import grp
    import pwd
except ImportError:  # Windows has no grp/pwd — only used on Linux paths
    grp = pwd = None

import cv2
import numpy as np

from camera_app import load_config, resolve_backend, resolve_device


def _run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    return (res.stdout or res.stderr).strip()


def permission_hint(dev_path: str):
    """If dev_path exists but is not accessible, explain the group fix."""
    if not sys.platform.startswith("linux") or not os.path.exists(dev_path):
        return
    if os.access(dev_path, os.R_OK | os.W_OK):
        return
    st = os.stat(dev_path)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    print(f"[HINT] {dev_path} is {owner}:{group} mode "
          f"{stat.S_IMODE(st.st_mode):04o} — user '{getpass.getuser()}' "
          f"has no read/write access.")
    print(f"       Fix:  sudo usermod -aG {group} $USER   (then log out/in)")
    print(f"       Or run this check with sudo. Eventide supervisor services")
    print(f"       run as root and are not affected by this.")


def show_device_info(dev_path: str):
    """Print V4L2 device formats and controls (Linux only, needs v4l2-ctl)."""
    if not sys.platform.startswith("linux"):
        print("(--list-formats-ext / --list-ctrls are Linux-only, skipped)")
        return
    if not shutil.which("v4l2-ctl"):
        print("(v4l2-ctl not found — sudo apt install v4l-utils for device info)")
        return
    print("--- Devices (v4l2-ctl --list-devices) ---")
    print(_run(["v4l2-ctl", "--list-devices"]))
    print("--- Supported formats (v4l2-ctl --list-formats-ext) ---")
    out = _run(["v4l2-ctl", "-d", dev_path, "--list-formats-ext"])
    print(out)
    print("--- Controls (v4l2-ctl --list-ctrls) ---")
    out += "\n" + _run(["v4l2-ctl", "-d", dev_path, "--list-ctrls"])
    print(out.split("\n", 1)[1])
    if "Permission denied" in out:
        permission_hint(dev_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the UVC camera is connected and streaming.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", "-c", default="camera_config.json", metavar="FILE",
                        help="Config to take device/backend/format defaults from.")
    parser.add_argument("--device", default=None, metavar="DEV",
                        help="Camera index or /dev/videoX path (overrides config).")
    parser.add_argument("--width", type=int, default=None, metavar="PX")
    parser.add_argument("--height", type=int, default=None, metavar="PX")
    parser.add_argument("--fps", type=float, default=None, metavar="FPS")
    parser.add_argument("--fourcc", default=None, metavar="XXXX")
    parser.add_argument("--seconds", type=float, default=2.0, metavar="SECS",
                        help="How long to capture while measuring frame rate.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    device = args.device if args.device is not None else cfg["device"]
    width = args.width or int(cfg["resolution"]["width"])
    height = args.height or int(cfg["resolution"]["height"])
    fps = args.fps or float(cfg["framerate"])
    fourcc = (args.fourcc or str(cfg["fourcc"])).upper()
    if len(fourcc) != 4:
        sys.exit(f"FOURCC must be exactly 4 characters, got '{fourcc}'.")

    dev_arg, dev_path = resolve_device(device)

    # -- 1. what is plugged in? ------------------------------------------------
    print("=== Device enumeration ===")
    if sys.platform.startswith("linux"):
        nodes = sorted(glob.glob("/dev/video*"))
        print("Video nodes:", ", ".join(nodes) if nodes else "NONE FOUND")
        if not nodes:
            print("[FAIL] No /dev/video* devices — camera not detected by the OS.")
            return 1
    else:
        print("(non-Linux platform — skipping /dev/video* listing)")
    print()
    show_device_info(dev_path)
    print()

    # -- 2. open + negotiate ----------------------------------------------------
    print("=== OpenCV capture ===")
    print(f"Requesting {width}x{height} @ {fps:g} fps, FOURCC {fourcc} on {dev_path} …")
    cap = cv2.VideoCapture(dev_arg, resolve_backend(str(cfg.get("backend", "auto"))))
    if not cap.isOpened():
        print(f"[FAIL] Could not open camera '{dev_arg}'.")
        permission_hint(dev_path)
        if sys.platform.startswith("linux"):
            print("       Also check the device list above: on boards with hardware")
            print("       codec/ISP nodes (e.g. video-dec*/video-enc*), /dev/video0 is")
            print("       often NOT the camera. Find the node named like your camera")
            print("       (e.g. 'Arducam IMX477 HQ Camera') and pass --device /dev/videoN.")
        return 1

    cap.set(cv2.CAP_PROP_BUFFERSIZE, int(cfg.get("buffer_size", 4)))
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)

    got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    got_fps = cap.get(cv2.CAP_PROP_FPS) or fps
    got_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    got_fourcc_str = "".join(chr((got_fourcc >> (8 * i)) & 0xFF) for i in range(4))
    print(f"Negotiated: {got_w}x{got_h} @ {got_fps:g} fps, FOURCC '{got_fourcc_str}'")
    if (got_w, got_h) != (width, height):
        print(f"[WARN] Camera did not honor the requested {width}x{height}.")
    if abs(got_fps - fps) > 0.5:
        print(f"[WARN] Requested {fps:g} fps but got {got_fps:g} — UVC cameras "
              f"offer discrete rates per resolution (see format list above);")
        print(f"       camera_app.py decimates to the configured target instead.")
    if got_fourcc_str.strip("\x00") != fourcc:
        print(f"[WARN] Requested FOURCC {fourcc} but got '{got_fourcc_str}'.")

    # -- 3. capture ----------------------------------------------------------
    print(f"Capturing for {args.seconds:g} s …")
    frames = 0
    brightness = 0.0
    start = time.monotonic()
    while time.monotonic() - start < args.seconds:
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frames += 1
        brightness += float(frame.mean())
    cap.release()

    if frames == 0:
        print("[FAIL] Camera opened but delivered no frames.")
        return 1

    measured_fps = frames / (time.monotonic() - start)
    mean_brightness = brightness / frames
    print(f"Captured {frames} frames: {measured_fps:.1f} fps, "
          f"mean brightness {mean_brightness:.1f}/255")

    print()
    print(f"[OK] Camera '{dev_arg}' is connected and streaming.")
    if mean_brightness < 2:
        print("     (image is essentially black — check the lens cap / exposure)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
