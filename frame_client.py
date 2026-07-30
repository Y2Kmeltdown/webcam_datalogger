#!/usr/bin/env python3
"""
frame_client.py – example consumer of raw frames from camera_app.py

Connects to the Unix socket, reads length+timestamp-prefixed BGR24 frames,
and displays them with OpenCV. Swap the display call for your own
processing pipeline.

Wire format (per frame):
    [4 bytes LE uint32: payload length]
    [8 bytes LE uint64: capture timestamp, microseconds since Unix epoch]
    [N bytes: raw BGR24 pixels, width*height*3]

Usage:
    python frame_client.py
    python frame_client.py --socket /tmp/webcam_frames.sock --width 1920 --height 1080
"""

import argparse
import socket
import struct
import sys
import time

import cv2
import numpy as np


def recv_exactly(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from the socket, blocking until available."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed by server.")
        buf.extend(chunk)
    return bytes(buf)


def main():
    parser = argparse.ArgumentParser(description="Webcam raw frame display client.")
    parser.add_argument("--socket", default="/tmp/webcam_frames.sock", metavar="PATH")
    parser.add_argument("--width",  type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args()

    print(f"Connecting to {args.socket} …")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(args.socket)
    except FileNotFoundError:
        sys.exit(f"Socket '{args.socket}' not found. Is camera_app.py running?")

    print("Connected. Press 'q' to quit.")

    try:
        while True:
            # Read 12-byte header: [uint32 payload_len][uint64 timestamp_us]
            header = recv_exactly(conn, 12)
            payload_len, timestamp_us = struct.unpack("<IQ", header)

            # Read the raw frame bytes (already BGR — what OpenCV captured)
            raw = recv_exactly(conn, payload_len)
            bgr = np.frombuffer(raw, dtype=np.uint8).reshape(
                (args.height, args.width, 3)
            )

            age_ms = (time.time() * 1_000_000 - timestamp_us) / 1000.0
            print(f"frame {bgr.shape[1]}x{bgr.shape[0]}  "
                  f"captured {age_ms:.1f} ms ago")

            #cv2.imshow("Webcam live", bgr)
            #if cv2.waitKey(1) & 0xFF == ord("q"):
                #break
    except (ConnectionError, struct.error) as exc:
        print(f"Stream ended: {exc}")
    finally:
        conn.close()
        #cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
