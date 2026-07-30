#!/usr/bin/env python3
"""
mjpeg_server.py — MJPEG HTTP server for camera_app.py (webcam_datalogger)

Reads raw BGR24 frames from the Unix domain socket produced by
camera_app.py, scales and JPEG-encodes them, and serves a standard
multipart/x-mixed-replace MJPEG stream over HTTP using aiohttp.

Stream parameters (FPS, JPEG quality, output resolution) can be changed
at runtime via a JSON REST API without restarting the server.

Socket wire format (per frame):
    [4 bytes LE uint32: payload length]
    [8 bytes LE uint64: capture timestamp, microseconds since Unix epoch]
    [N bytes: raw BGR24 pixels]

The capture timestamp is forwarded to MJPEG clients as an
X-Capture-Timestamp header on each multipart part boundary.

Endpoints:
    GET  /                  Embedded viewer HTML page
    GET  /stream            MJPEG multipart stream
    GET  /snapshot          Latest frame as a single JPEG
    GET  /api/settings      Return current stream settings (JSON)
    PUT  /api/settings      Update one or more settings (JSON body)
    GET  /api/streaming     Return streaming state  {"streaming": true|false}
    PUT  /api/streaming     Set streaming state     {"streaming": true|false}

Usage:
    python mjpeg_server.py
    python mjpeg_server.py --socket /tmp/webcam_frames.sock \
                           --src-width 1920 --src-height 1080 \
                           --out-width 1280 --out-height 720 \
                           --fps 30 --quality 80 \
                           --host 0.0.0.0 --port 8087
"""

import argparse
import asyncio
import logging
import signal
import socket
import struct
import sys
import time
import threading
from asyncio import Queue
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from aiohttp import web

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
log = logging.getLogger("mjpeg")

BOUNDARY = "webcamframe"

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """
    Add CORS headers to every response and handle OPTIONS preflights.

    aiohttp raises HTTP exceptions (e.g. HTTPServiceUnavailable for a
    disabled stream) before the handler returns a response object, so we
    catch those here and stamp the headers on them too — otherwise the
    browser sees the error status without ACAO and blocks it as a CORS
    failure rather than surfacing the real status code.
    """
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers.update(CORS_HEADERS)
        raise
    response.headers.update(CORS_HEADERS)
    return response


# ---------------------------------------------------------------------------
# StreamConfig — single source of truth for all tunable parameters
# ---------------------------------------------------------------------------

@dataclass
class StreamConfig:
    """
    All parameters that affect how raw socket frames are processed before
    being sent to HTTP clients.

    Thread-safe: a threading.Lock protects all reads/writes so the executor
    thread (frame processing) and the asyncio thread (HTTP API) can both
    access it safely.
    """
    # Source resolution — must match camera_app.py config; read-only at runtime
    src_width: int
    src_height: int

    # Output (MJPEG) resolution — can be freely scaled down from source
    out_width: int
    out_height: int

    # JPEG compression quality 1–100
    quality: int

    _lock: threading.Lock = None

    def __post_init__(self):
        self._lock = threading.Lock()

    def get(self) -> dict:
        """Return a plain-dict snapshot. Safe to call from any thread."""
        with self._lock:
            return {
                "src_width":  self.src_width,
                "src_height": self.src_height,
                "out_width":  self.out_width,
                "out_height": self.out_height,
                "quality":    self.quality,
            }

    def update(self, **kwargs) -> list[str]:
        """
        Apply validated updates. Returns a list of error strings (empty = ok).
        Mutable keys: out_width, out_height, quality.
        src_width / src_height are intentionally read-only.
        """
        errors = []
        with self._lock:
            for key, value in kwargs.items():
                if key in ("src_width", "src_height"):
                    errors.append(f"'{key}' is read-only (set by camera_app config).")
                elif key == "out_width":
                    v = int(value)
                    if v < 1 or v > self.src_width:
                        errors.append(f"out_width must be 1–{self.src_width}.")
                    else:
                        self.out_width = v
                elif key == "out_height":
                    v = int(value)
                    if v < 1 or v > self.src_height:
                        errors.append(f"out_height must be 1–{self.src_height}.")
                    else:
                        self.out_height = v
                elif key == "quality":
                    v = int(value)
                    if v < 1 or v > 100:
                        errors.append("quality must be 1–100.")
                    else:
                        self.quality = v
                else:
                    errors.append(f"Unknown setting '{key}'.")
        return errors


# ---------------------------------------------------------------------------
# Frame distributor
# ---------------------------------------------------------------------------

class FrameDistributor:
    """
    Holds the latest JPEG and fans it out to all connected HTTP clients via
    per-client asyncio Queues. Slow clients have their oldest frame dropped
    rather than blocking the pipeline.

    Each queue item is a (jpeg_bytes, timestamp_us) tuple so the MJPEG
    handler can forward the capture timestamp to streaming clients
    as an X-Capture-Timestamp header on each multipart boundary.

    The streaming gate (self.streaming) can be toggled via the API.
    When False:
      - publish() still accepts frames so the socket reader keeps draining
        and latest_jpeg stays fresh for /snapshot, but nothing is pushed
        to client queues so active /stream connections pause silently.
      - New /stream requests receive a 503 immediately rather than hanging.
    When set back to True, all currently-connected clients resume instantly
    with no reconnect required on either side.
    """

    def __init__(self):
        self._queues: list[Queue[tuple[bytes, int]]] = []
        self._lock = asyncio.Lock()
        self.latest_jpeg: Optional[bytes] = None
        self.latest_timestamp_us: int = 0
        self.streaming: bool = True          # gate: controlled by /api/streaming

    async def register(self) -> Queue:
        q: Queue[tuple[bytes, int]] = Queue(maxsize=2)
        async with self._lock:
            self._queues.append(q)
        log.info("MJPEG client registered (total: %d).", len(self._queues))
        return q

    async def unregister(self, q: Queue):
        async with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass
        log.info("MJPEG client unregistered (remaining: %d).", len(self._queues))

    async def publish(self, jpeg: bytes, timestamp_us: int):
        # Always update latest_* so /snapshot stays current even when paused
        self.latest_jpeg = jpeg
        self.latest_timestamp_us = timestamp_us

        # Gate: skip fan-out when streaming is disabled
        if not self.streaming:
            return

        async with self._lock:
            dead = []
            for q in self._queues:
                try:
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    q.put_nowait((jpeg, timestamp_us))
                except Exception:
                    dead.append(q)
            for d in dead:
                self._queues.remove(d)


# ---------------------------------------------------------------------------
# Frame processing (runs on executor thread)
# ---------------------------------------------------------------------------

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Unix socket closed by camera_app.")
        buf.extend(chunk)
    return bytes(buf)


def _process_frame(
    raw: bytes,
    src_width: int,
    src_height: int,
    out_width: int,
    out_height: int,
    quality: int,
) -> bytes:
    """
    Decode raw BGR24 bytes → ndarray, resize to the requested output
    resolution (using INTER_AREA for downscaling quality), then JPEG-encode.

    This function runs entirely on a thread-pool executor so it never blocks
    the asyncio event loop.
    """
    bgr = np.frombuffer(raw, dtype=np.uint8).reshape((src_height, src_width, 3))

    # Only resize if the output dimensions actually differ from the source
    if out_width != src_width or out_height != src_height:
        bgr = cv2.resize(bgr, (out_width, out_height), interpolation=cv2.INTER_AREA)

    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed.")
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Unix socket reader task
# ---------------------------------------------------------------------------

async def socket_reader(
    loop: asyncio.AbstractEventLoop,
    distributor: FrameDistributor,
    cfg: StreamConfig,
    socket_path: str,
    reconnect_delay: float = 2.0,
):
    """
    Continuously reads raw frames from the Unix socket, applies the current
    StreamConfig parameters (sampled per-frame so API changes take effect
    immediately), and publishes processed JPEGs to the distributor.

    Reads a 12-byte header before each frame:
        [4 bytes LE uint32: payload_len][8 bytes LE uint64: timestamp µs]

    The timestamp is passed through to the distributor and forwarded to all
    active /stream clients as an X-Capture-Timestamp MJPEG part header.

    Reconnects automatically if camera_app.py is restarted.
    """
    while True:
        sock = socket.socket(AF_UNIX, socket.SOCK_STREAM)
        try:
            log.info("Connecting to Unix socket '%s' …", socket_path)
            await loop.run_in_executor(None, sock.connect, socket_path)
            log.info("Connected to camera socket.")

            while True:
                # Read 12-byte header: [uint32 payload_len][uint64 timestamp_us]
                header = await loop.run_in_executor(None, _recv_exactly, sock, 12)
                payload_len, timestamp_us = struct.unpack("<IQ", header)
                raw = await loop.run_in_executor(None, _recv_exactly, sock, payload_len)

                # Snapshot config atomically before deciding what to do
                params = cfg.get()

                # Resize + JPEG encode on executor thread
                jpeg = await loop.run_in_executor(
                    None,
                    _process_frame,
                    raw,
                    params["src_width"],
                    params["src_height"],
                    params["out_width"],
                    params["out_height"],
                    params["quality"],
                )

                await distributor.publish(jpeg, timestamp_us)

        except (ConnectionError, ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            log.warning("Socket error: %s — retrying in %.1f s.", exc, reconnect_delay)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        await asyncio.sleep(reconnect_delay)


# ---------------------------------------------------------------------------
# HTTP handlers — stream / snapshot / index
# ---------------------------------------------------------------------------

async def handle_stream(request: web.Request) -> web.StreamResponse:
    """Serve the MJPEG multipart stream to one client.

    Returns 503 immediately if streaming is currently disabled so clients
    get a clear error rather than an open connection that never sends frames.
    """
    distributor: FrameDistributor = request.app["distributor"]

    if not distributor.streaming:
        raise web.HTTPServiceUnavailable(reason="Streaming is currently disabled.")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "close",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await response.prepare(request)

    q = await distributor.register()
    log.info("Stream started for %s.", request.remote)

    try:
        while True:
            jpeg, timestamp_us = await asyncio.wait_for(q.get(), timeout=10.0)
            part = (
                f"--{BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n"
                f"X-Capture-Timestamp: {timestamp_us}\r\n"
                f"\r\n"
            ).encode() + jpeg + b"\r\n"
            await response.write(part)
    except (asyncio.TimeoutError, ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        await distributor.unregister(q)
        log.info("Stream ended for %s.", request.remote)

    return response


async def handle_snapshot(request: web.Request) -> web.Response:
    """Return the most recent frame as a single JPEG."""
    distributor: FrameDistributor = request.app["distributor"]
    if distributor.latest_jpeg is None:
        raise web.HTTPServiceUnavailable(reason="No frame available yet.")
    return web.Response(
        body=distributor.latest_jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


async def handle_index(request: web.Request) -> web.Response:
    """Minimal HTML viewer page with a streaming toggle button."""
    host = request.host
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Webcam Live</title>
  <style>
    body {{ margin: 0; background: #111; display: flex; flex-direction: column;
            align-items: center; justify-content: center; min-height: 100vh;
            font-family: monospace; gap: 0.75rem; padding: 1rem; }}
    #feed {{ max-width: 100%; border: 2px solid #333; }}
    .info {{ color: #aaa; font-size: 0.85em; text-align: center; }}
    .info a {{ color: #7dd3fc; text-decoration: none; }}
    #toggle {{
      padding: 0.4rem 1.2rem; border: none; border-radius: 4px; cursor: pointer;
      font-family: monospace; font-size: 0.85rem; letter-spacing: 0.05em;
      transition: background 0.15s;
    }}
    #toggle.on  {{ background: #dc2626; color: #fff; }}
    #toggle.off {{ background: #16a34a; color: #fff; }}
    #status {{ font-size: 0.75rem; color: #64748b; min-height: 1em; }}
  </style>
</head>
<body>
  <img id="feed" src="/stream" alt="Webcam live stream"
       onerror="this.alt='Stream paused or unavailable'">
  <div class="info">
    <a href="/stream">/stream</a> &nbsp;|&nbsp;
    <a href="/snapshot">/snapshot</a> &nbsp;|&nbsp;
    <a href="/api/settings">/api/settings</a> &nbsp;|&nbsp;
    <a href="/api/streaming">/api/streaming</a>
  </div>
  <button id="toggle" class="on">Disable stream</button>
  <div id="status"></div>

  <script>
    const feed   = document.getElementById("feed");
    const btn    = document.getElementById("toggle");
    const status = document.getElementById("status");
    let active = true;

    async function fetchState() {{
      try {{
        const r = await fetch("/api/streaming");
        const d = await r.json();
        setState(d.streaming);
      }} catch (e) {{ /* ignore on load */ }}
    }}

    function setState(on) {{
      active = on;
      btn.textContent = on ? "Disable stream" : "Enable stream";
      btn.className   = on ? "on" : "off";
      if (on && !feed.src.endsWith("/stream")) {{
        feed.src = "/stream?" + Date.now();   // force reload after re-enable
      }}
    }}

    btn.addEventListener("click", async () => {{
      const next = !active;
      try {{
        const r = await fetch("/api/streaming", {{
          method:  "PUT",
          headers: {{"Content-Type": "application/json"}},
          body:    JSON.stringify({{streaming: next}}),
        }});
        const d = await r.json();
        setState(d.streaming);
        status.textContent = d.streaming ? "Stream enabled." : "Stream disabled.";
      }} catch (e) {{
        status.textContent = "Error: " + e.message;
      }}
    }});

    fetchState();
  </script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# HTTP handlers — settings REST API
# ---------------------------------------------------------------------------

async def handle_get_settings(request: web.Request) -> web.Response:
    """
    GET /api/settings

    Returns the current stream settings as JSON.

    Example response:
        {
            "src_width": 1920, "src_height": 1080,
            "out_width": 1280, "out_height": 720,
            "quality": 80
        }
    """
    cfg: StreamConfig = request.app["cfg"]
    return web.json_response(cfg.get())


async def handle_put_settings(request: web.Request) -> web.Response:
    """
    PUT /api/settings

    Accepts a JSON object with any subset of the mutable settings:
        out_width   integer  1 – src_width
        out_height  integer  1 – src_height
        quality     integer  1 – 100

    All supplied fields are validated; the entire update is rejected if any
    field is invalid. Returns the full updated settings on success, or a 400
    with an "errors" array on failure.

    Example:
        curl -X PUT http://pi:8087/api/settings \
             -H 'Content-Type: application/json' \
             -d '{"quality": 60, "out_width": 640, "out_height": 360}'
    """
    cfg: StreamConfig = request.app["cfg"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Request body must be valid JSON.")

    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="JSON body must be an object.")

    errors = cfg.update(**body)
    if errors:
        return web.json_response({"errors": errors}, status=400)

    updated = cfg.get()
    log.info("Settings updated: %s", updated)
    return web.json_response(updated)


# ---------------------------------------------------------------------------
# HTTP handlers — streaming on/off API
# ---------------------------------------------------------------------------

async def handle_get_streaming(request: web.Request) -> web.Response:
    """
    GET /api/streaming

    Returns the current streaming state.

    Example response:
        {"streaming": true}
    """
    distributor: FrameDistributor = request.app["distributor"]
    return web.json_response({"streaming": distributor.streaming})


async def handle_put_streaming(request: web.Request) -> web.Response:
    """
    PUT /api/streaming

    Enables or disables the MJPEG stream fan-out.

    Request body:
        {"streaming": true}   — resume sending frames to all /stream clients
        {"streaming": false}  — stop sending frames; connections stay open,
                                /snapshot stays fresh, socket keeps draining

    Returns the updated state:
        {"streaming": false}

    Examples:
        curl -X PUT http://pi:8087/api/streaming \\
             -H 'Content-Type: application/json' \\
             -d '{"streaming": false}'
    """
    distributor: FrameDistributor = request.app["distributor"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Request body must be valid JSON.")

    if not isinstance(body, dict) or "streaming" not in body:
        raise web.HTTPBadRequest(reason='Body must be a JSON object with a "streaming" key.')

    value = body["streaming"]
    if not isinstance(value, bool):
        raise web.HTTPBadRequest(reason='"streaming" must be a boolean (true or false).')

    distributor.streaming = value
    log.info("Streaming %s via API.", "enabled" if value else "disabled")
    return web.json_response({"streaming": distributor.streaming})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def make_app(distributor: FrameDistributor, cfg: StreamConfig) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["distributor"] = distributor
    app["cfg"] = cfg
    app.router.add_get("/",             handle_index)
    app.router.add_get("/stream",       handle_stream)
    app.router.add_get("/snapshot",     handle_snapshot)
    app.router.add_get("/api/settings",  handle_get_settings)
    app.router.add_put("/api/settings",  handle_put_settings)
    app.router.add_get("/api/streaming", handle_get_streaming)
    app.router.add_put("/api/streaming", handle_put_streaming)
    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Webcam MJPEG server — serves scaled camera frames over HTTP "
                    "with a live-tunable settings API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--socket",     default="/tmp/webcam_frames.sock", metavar="PATH",
                        help="Unix socket path produced by camera_app.py.")
    parser.add_argument("--src-width",  type=int, default=1920, metavar="PX",
                        help="Source frame width from camera_app (read-only at runtime).")
    parser.add_argument("--src-height", type=int, default=1080, metavar="PX",
                        help="Source frame height from camera_app (read-only at runtime).")
    parser.add_argument("--out-width",  type=int, default=None, metavar="PX",
                        help="Initial output MJPEG width. Defaults to src-width.")
    parser.add_argument("--out-height", type=int, default=None, metavar="PX",
                        help="Initial output MJPEG height. Defaults to src-height.")
    parser.add_argument("--quality",    type=int, default=80, metavar="1-100",
                        help="Initial JPEG compression quality.")
    parser.add_argument("--host",       default="0.0.0.0", metavar="ADDR",
                        help="Address to bind the HTTP server.")
    parser.add_argument("--port",       type=int, default=8087, metavar="PORT",
                        help="TCP port for the HTTP server.")
    parser.add_argument("--reconnect-delay", type=float, default=2.0, metavar="SECS",
                        help="Seconds before retrying a dropped socket connection.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main(args: argparse.Namespace):
    cfg = StreamConfig(
        src_width=args.src_width,
        src_height=args.src_height,
        out_width=args.out_width  or args.src_width,
        out_height=args.out_height or args.src_height,
        quality=args.quality,
    )

    distributor = FrameDistributor()
    app = make_app(distributor, cfg)
    loop = asyncio.get_running_loop()

    reader_task = asyncio.create_task(
        socket_reader(
            loop=loop,
            distributor=distributor,
            cfg=cfg,
            socket_path=args.socket,
            reconnect_delay=args.reconnect_delay,
        ),
        name="socket-reader",
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    log.info(
        "MJPEG server at http://%s:%d/  |  "
        "settings: PUT/GET http://%s:%d/api/settings  |  "
        "streaming: PUT/GET http://%s:%d/api/streaming",
        args.host, args.port, args.host, args.port, args.host, args.port,
    )
    log.info("Initial config: %s", cfg.get())

    stop_event = asyncio.Event()

    def _request_stop(*_):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)

    await stop_event.wait()

    log.info("Shutting down …")
    reader_task.cancel()
    try:
        await reader_task
    except asyncio.CancelledError:
        pass
    await runner.cleanup()
    log.info("Done.")


def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if not (1 <= args.quality <= 100):
        sys.exit("--quality must be between 1 and 100.")

    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
