import os
import json
import time
import uuid
import threading
import subprocess
from urllib.parse import urlparse, quote

import requests
import numpy as np
import zstandard as zstd

from flask import Flask, request, jsonify, Response


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

WIDTH = 854
HEIGHT = 480

TARGET_FPS = 30

FRAME_SIZE = WIDTH * HEIGHT * 4

KEYFRAME_INTERVAL = 30

# Maximum number of frames returned by one packet request.
MAX_BATCH_FRAMES = 10

# Try to keep an HTTP response below this size.
MAX_RESPONSE_BYTES = 2_500_000

REQUEST_TIMEOUT = 60

ARCHIVE_HEADERS = {
    "User-Agent": "RobloxVideoCodec/2.0"
}

SESSION_TIMEOUT = 15 * 60


# ============================================================
# ZSTD
# ============================================================

ZSTD_LEVEL = 1

ZSTD_COMPRESSOR = zstd.ZstdCompressor(
    level=ZSTD_LEVEL
)


# ============================================================
# GLOBAL SESSIONS
# ============================================================

SESSIONS = {}

SESSIONS_LOCK = threading.RLock()


# ============================================================
# LOGGING
# ============================================================

def log(*args):
    print("[Video]", *args, flush=True)


# ============================================================
# ARCHIVE
# ============================================================

def get_archive_identifier(url):

    parsed = urlparse(url)

    if parsed.netloc.lower() not in (
        "archive.org",
        "www.archive.org"
    ):
        raise ValueError(
            "Only Internet Archive URLs are supported."
        )

    parts = [
        x for x in parsed.path.split("/")
        if x
    ]

    if len(parts) < 2:
        raise ValueError(
            "Invalid Internet Archive URL."
        )

    if parts[0] == "details":
        return parts[1]

    if parts[0] == "download":
        return parts[1]

    raise ValueError(
        "URL must be an Internet Archive /details/ or /download/ URL."
    )


def get_archive_metadata(identifier):

    url = (
        "https://archive.org/metadata/"
        + quote(identifier, safe="")
    )

    log(
        "Getting archive metadata:",
        identifier
    )

    response = requests.get(
        url,
        headers=ARCHIVE_HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    if data.get("is_dark"):
        raise ValueError(
            "This Internet Archive item is unavailable."
        )

    return data


def find_video_file(metadata):

    files = metadata.get(
        "files",
        []
    )

    candidates = []

    for item in files:

        name = item.get(
            "name",
            ""
        )

        if not name:
            continue

        lower = name.lower()

        if lower.endswith(".mp4"):

            candidates.append(item)

    if not candidates:

        for item in files:

            name = item.get(
                "name",
                ""
            )

            lower = name.lower()

            if (
                lower.endswith(".webm")
                or lower.endswith(".mkv")
                or lower.endswith(".mov")
                or lower.endswith(".avi")
            ):

                candidates.append(item)

    if not candidates:

        raise ValueError(
            "No supported video file was found in this archive."
        )

    candidates.sort(
        key=lambda item: (
            0
            if item.get(
                "name",
                ""
            ).lower().endswith(".mp4")
            else 1,

            -int(
                item.get(
                    "size",
                    0
                )
                or 0
            )
        )
    )

    return candidates[0]


def build_archive_download_url(
    identifier,
    filename
):

    return (
        "https://archive.org/download/"
        + quote(
            identifier,
            safe=""
        )
        + "/"
        + quote(
            filename,
            safe="/"
        )
    )


# ============================================================
# FFPROBE
# ============================================================

def run_ffprobe(video_url):

    command = [
        "ffprobe",

        "-v",
        "error",

        "-select_streams",
        "v:0",

        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,duration",

        "-of",
        "json",

        video_url
    ]

    result = subprocess.run(
        command,

        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,

        text=True,

        timeout=REQUEST_TIMEOUT
    )

    if result.returncode != 0:

        raise RuntimeError(
            "ffprobe failed: "
            + result.stderr
        )

    try:

        data = json.loads(
            result.stdout
        )

    except Exception:

        raise RuntimeError(
            "ffprobe returned invalid JSON."
        )

    streams = data.get(
        "streams",
        []
    )

    if not streams:

        raise ValueError(
            "No video stream found."
        )

    stream = streams[0]

    source_width = int(
        stream.get(
            "width",
            0
        )
    )

    source_height = int(
        stream.get(
            "height",
            0
        )
    )

    if (
        source_width <= 0
        or source_height <= 0
    ):

        raise ValueError(
            "Invalid source resolution."
        )

    fps_text = (
        stream.get(
            "avg_frame_rate"
        )
        or stream.get(
            "r_frame_rate"
        )
        or "30/1"
    )

    try:

        numerator, denominator = (
            fps_text.split("/")
        )

        source_fps = (
            float(numerator)
            /
            float(denominator)
        )

    except Exception:

        source_fps = 30.0

    duration = stream.get(
        "duration"
    )

    try:

        duration = float(
            duration
        )

    except Exception:

        duration = None

    return {
        "width": source_width,
        "height": source_height,
        "fps": source_fps,
        "duration": duration
    }


# ============================================================
# VIDEO SESSION
# ============================================================

class VideoSession:

    def __init__(
        self,
        session_id,
        archive_url,
        identifier,
        filename,
        video_url,
        source
    ):

        self.id = session_id

        self.archive_url = archive_url

        self.identifier = identifier

        self.filename = filename

        self.video_url = video_url

        self.source_width = source["width"]

        self.source_height = source["height"]

        self.source_fps = source["fps"]

        self.duration = source["duration"]

        self.width = WIDTH

        self.height = HEIGHT

        self.fps = TARGET_FPS

        self.frame_number = 0

        self.previous_frame = None

        self.finished = False

        self.created_at = time.time()

        self.last_used = time.time()

        self.lock = threading.RLock()

        self.process = None

        self.start_ffmpeg()

    # --------------------------------------------------------
    # FFmpeg
    # --------------------------------------------------------

    def start_ffmpeg(self):

        scale_filter = (
            f"scale={WIDTH}:{HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            "format=rgba"
        )

        command = [

            "ffmpeg",

            "-hide_banner",

            "-loglevel",
            "error",

            "-nostdin",

            "-reconnect",
            "1",

            "-reconnect_streamed",
            "1",

            "-reconnect_delay_max",
            "5",

            "-i",
            self.video_url,

            "-an",

            "-vf",
            scale_filter,

            "-r",
            str(TARGET_FPS),

            "-fps_mode",
            "cfr",

            "-f",
            "rawvideo",

            "-pix_fmt",
            "rgba",

            "pipe:1"
        ]

        log(
            "Starting FFmpeg session:",
            self.id
        )

        self.process = subprocess.Popen(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            bufsize=1024 * 1024
        )

    # --------------------------------------------------------
    # Read one frame
    # --------------------------------------------------------

    def read_frame(self):

        if self.finished:
            return None

        if not self.process:
            return None

        raw = self.process.stdout.read(
            FRAME_SIZE
        )

        if not raw:

            self.finished = True

            return None

        if len(raw) != FRAME_SIZE:

            log(
                "Incomplete frame:",
                len(raw),
                "expected:",
                FRAME_SIZE
            )

            self.finished = True

            return None

        return raw

    # --------------------------------------------------------
    # Encode one frame
    # --------------------------------------------------------

    def encode_frame(
        self,
        raw
    ):

        frame_number = self.frame_number

        is_keyframe = (
            frame_number == 0
            or frame_number
            % KEYFRAME_INTERVAL
            == 0
        )

        current = np.frombuffer(
            raw,
            dtype=np.uint8
        )

        if (
            is_keyframe
            or self.previous_frame is None
        ):

            compressed = (
                ZSTD_COMPRESSOR.compress(
                    raw
                )
            )

            packet_type = 1

        else:

            previous = np.frombuffer(
                self.previous_frame,
                dtype=np.uint8
            )

            delta = np.bitwise_xor(
                current,
                previous
            )

            compressed = (
                ZSTD_COMPRESSOR.compress(
                    delta.tobytes()
                )
            )

            packet_type = 0

        self.previous_frame = raw

        self.frame_number += 1

        return (
            frame_number,
            packet_type,
            compressed
        )

    # --------------------------------------------------------
    # Get packets
    # --------------------------------------------------------

    def get_packets(
        self,
        requested_start,
        requested_count
    ):

        with self.lock:

            self.last_used = time.time()

            if requested_count <= 0:

                return b"", False

            requested_count = min(
                requested_count,
                MAX_BATCH_FRAMES
            )

            # The codec is sequential.
            #
            # We cannot seek backwards because delta frames
            # depend on the previous frame.
            #
            # We also don't allow skipping ahead because
            # that would destroy the delta chain.

            if requested_start != self.frame_number:

                raise ValueError(
                    "Session frame mismatch. "
                    f"Expected {self.frame_number}, "
                    f"received {requested_start}."
                )

            packets = []

            total_size = 0

            for _ in range(
                requested_count
            ):

                raw = self.read_frame()

                if raw is None:
                    break

                (
                    frame_number,
                    packet_type,
                    compressed
                ) = self.encode_frame(
                    raw
                )

                # Packet:
                #
                # 4 bytes frame number
                # 1 byte type
                # 4 bytes compressed size
                # N bytes compressed data

                packet = (
                    frame_number.to_bytes(
                        4,
                        "little"
                    )
                    +
                    bytes([
                        packet_type
                    ])
                    +
                    len(
                        compressed
                    ).to_bytes(
                        4,
                        "little"
                    )
                    +
                    compressed
                )

                # Don't allow the response to grow
                # excessively.

                if (
                    packets
                    and
                    total_size
                    + len(packet)
                    > MAX_RESPONSE_BYTES
                ):

                    # We already have at least one
                    # frame, so stop here.

                    # Rollback is not possible because
                    # FFmpeg has already advanced.

                    # Therefore we only use this protection
                    # when the packet itself is too large.
                    break

                packets.append(
                    packet
                )

                total_size += len(
                    packet
                )

            if not packets:

                return b"", self.finished

            return (
                b"".join(packets),
                self.finished
            )

    # --------------------------------------------------------
    # Stop
    # --------------------------------------------------------

    def stop(self):

        with self.lock:

            if self.process:

                try:
                    self.process.kill()

                except Exception:
                    pass

                try:
                    self.process.wait(
                        timeout=2
                    )

                except Exception:
                    pass

                self.process = None

            self.finished = True


# ============================================================
# SESSION CLEANUP
# ============================================================

def cleanup_sessions():

    while True:

        time.sleep(60)

        now = time.time()

        expired = []

        with SESSIONS_LOCK:

            for session_id, session in list(
                SESSIONS.items()
            ):

                if (
                    now
                    - session.last_used
                    > SESSION_TIMEOUT
                ):

                    expired.append(
                        session_id
                    )

            for session_id in expired:

                session = SESSIONS.pop(
                    session_id,
                    None
                )

                if session:

                    log(
                        "Cleaning session:",
                        session_id
                    )

                    session.stop()


cleanup_thread = threading.Thread(
    target=cleanup_sessions,
    daemon=True
)

cleanup_thread.start()


# ============================================================
# ROOT
# ============================================================

@app.route("/")
def index():

    return jsonify({

        "status": "ok",

        "service":
            "Roblox Video Codec",

        "codec":
            "ZSTD-XOR-RGBA",

        "width":
            WIDTH,

        "height":
            HEIGHT,

        "fps":
            TARGET_FPS,

        "keyframeInterval":
            KEYFRAME_INTERVAL
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "healthy"
    })


# ============================================================
# START VIDEO
# ============================================================

@app.route(
    "/video/start",
    methods=["POST"]
)
def video_start():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        archive_url = data.get(
            "url"
        )

        if not archive_url:

            return jsonify({
                "success": False,
                "error": "Missing URL."
            }), 400

        if not isinstance(
            archive_url,
            str
        ):

            return jsonify({
                "success": False,
                "error":
                    "URL must be a string."
            }), 400

        archive_url = archive_url.strip()

        identifier = get_archive_identifier(
            archive_url
        )

        metadata = get_archive_metadata(
            identifier
        )

        video = find_video_file(
            metadata
        )

        filename = video["name"]

        video_url = build_archive_download_url(
            identifier,
            filename
        )

        log(
            "Probing:",
            filename
        )

        source = run_ffprobe(
            video_url
        )

        session_id = uuid.uuid4().hex

        session = VideoSession(

            session_id,

            archive_url,

            identifier,

            filename,

            video_url,

            source
        )

        with SESSIONS_LOCK:

            SESSIONS[
                session_id
            ] = session

        log(
            "Started:",
            filename,

            "| source:",
            source["width"],
            "x",
            source["height"],

            "| output:",
            WIDTH,
            "x",
            HEIGHT,

            "| FPS:",
            TARGET_FPS,

            "| session:",
            session_id
        )

        return jsonify({

            "success": True,

            "session":
                session_id,

            "filename":
                filename,

            "sourceWidth":
                source["width"],

            "sourceHeight":
                source["height"],

            "sourceFPS":
                source["fps"],

            "duration":
                source["duration"],

            "width":
                WIDTH,

            "height":
                HEIGHT,

            "fps":
                TARGET_FPS,

            "codec":
                "ZSTD-XOR-RGBA",

            "keyframeInterval":
                KEYFRAME_INTERVAL
        })

    except subprocess.TimeoutExpired:

        return jsonify({

            "success": False,

            "error":
                "FFprobe timed out."
        }), 504

    except Exception as e:

        log(
            "Start error:",
            repr(e)
        )

        return jsonify({

            "success": False,

            "error":
                str(e)
        }), 500


# ============================================================
# PACKETS
# ============================================================

@app.route(
    "/video/packets",
    methods=["GET"]
)
def video_packets():

    session_id = request.args.get(
        "session"
    )

    if not session_id:

        return jsonify({
            "success": False,
            "error":
                "Missing session."
        }), 400

    try:

        start = int(
            request.args.get(
                "start",
                "0"
            )
        )

        count = int(
            request.args.get(
                "count",
                "10"
            )
        )

    except ValueError:

        return jsonify({
            "success": False,
            "error":
                "Invalid start/count."
        }), 400

    with SESSIONS_LOCK:

        session = SESSIONS.get(
            session_id
        )

    if not session:

        return jsonify({
            "success": False,
            "error":
                "Video session not found."
        }), 404

    try:

        payload, finished = (
            session.get_packets(
                start,
                count
            )
        )

        return Response(

            payload,

            mimetype=
                "application/octet-stream",

            headers={

                "Cache-Control":
                    "no-store",

                "X-Video-Width":
                    str(WIDTH),

                "X-Video-Height":
                    str(HEIGHT),

                "X-Video-FPS":
                    str(TARGET_FPS),

                "X-Video-Finished":
                    "1"
                    if finished
                    else "0"
            }
        )

    except Exception as e:

        log(
            "Packet error:",
            repr(e)
        )

        return jsonify({

            "success": False,

            "error":
                str(e)
        }), 500


# ============================================================
# STOP
# ============================================================

@app.route(
    "/video/stop",
    methods=["POST"]
)
def video_stop():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        session_id = data.get(
            "session"
        )

        if not session_id:

            return jsonify({
                "success": False,
                "error":
                    "Missing session."
            }), 400

        with SESSIONS_LOCK:

            session = SESSIONS.pop(
                session_id,
                None
            )

        if session:

            session.stop()

        return jsonify({
            "success": True
        })

    except Exception as e:

        return jsonify({

            "success": False,

            "error":
                str(e)
        }), 500


# ============================================================
# ERRORS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return jsonify({

        "success": False,

        "error":
            "Endpoint not found."
    }), 404


@app.errorhandler(500)
def internal_error(error):

    return jsonify({

        "success": False,

        "error":
            "Internal server error."
    }), 500


# ============================================================
# LOCAL TEST
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    log(
        "Starting server on port",
        port
    )

    app.run(

        host="0.0.0.0",

        port=port,

        threaded=True
    )
