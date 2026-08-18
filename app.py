import os
import io
import json
import time
import math
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests
import numpy as np
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

MAX_WIDTH = 1280
MAX_HEIGHT = 720
TARGET_FPS = 30

REQUEST_TIMEOUT = 30

ARCHIVE_HEADERS = {
    "User-Agent": "RobloxVideoStreamer/1.0"
}

# ============================================================
# HELPERS
# ============================================================

def log(*args):
    print("[Video]", *args, flush=True)


def clamp_resolution(width, height):
    """
    Keep the original resolution if it is <= 720p.

    If larger than 720p, scale down while preserving
    aspect ratio.

    Never upscale.
    """

    width = int(width)
    height = int(height)

    if width <= MAX_WIDTH and height <= MAX_HEIGHT:
        return width, height

    scale = min(
        MAX_WIDTH / width,
        MAX_HEIGHT / height
    )

    new_width = max(2, int(width * scale))
    new_height = max(2, int(height * scale))

    # FFmpeg/YUV friendly even dimensions.
    new_width -= new_width % 2
    new_height -= new_height % 2

    return new_width, new_height


def get_archive_identifier(url):
    """
    Accepts:

        https://archive.org/details/TikTok-123
        https://archive.org/download/TikTok-123/file.mp4

    Returns the archive identifier.
    """

    parsed = urlparse(url)

    if parsed.netloc.lower() not in (
        "archive.org",
        "www.archive.org"
    ):
        raise ValueError("Only Internet Archive URLs are supported.")

    parts = [
        x for x in parsed.path.split("/")
        if x
    ]

    if len(parts) < 2:
        raise ValueError("Invalid Internet Archive URL.")

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
        + identifier
    )

    log("Getting metadata:", identifier)

    response = requests.get(
        url,
        headers=ARCHIVE_HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    if data.get("is_dark"):
        raise ValueError("This Internet Archive item is unavailable.")

    return data


def find_video_file(metadata):
    """
    Select the best MP4/video file from the archive.
    """

    files = metadata.get("files", [])

    candidates = []

    for item in files:
        name = item.get("name", "")

        if not name:
            continue

        lower = name.lower()

        if lower.endswith(".mp4"):
            candidates.append(item)

    if not candidates:
        for item in files:
            name = item.get("name", "")
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

    # Prefer MP4.
    candidates.sort(
        key=lambda x: (
            0 if x.get("name", "").lower().endswith(".mp4") else 1,
            -int(x.get("size", 0) or 0)
        )
    )

    return candidates[0]


def build_archive_download_url(identifier, filename):
    from urllib.parse import quote

    return (
        "https://archive.org/download/"
        + quote(identifier, safe="")
        + "/"
        + quote(filename, safe="/")
    )


def run_ffprobe(video_path):
    """
    Get source video information.
    """

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
        video_path
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed: " + result.stderr
        )

    data = json.loads(result.stdout)

    streams = data.get("streams", [])

    if not streams:
        raise ValueError("No video stream found.")

    stream = streams[0]

    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))

    if width <= 0 or height <= 0:
        raise ValueError("Invalid source resolution.")

    fps_text = (
        stream.get("avg_frame_rate")
        or stream.get("r_frame_rate")
        or "30/1"
    )

    try:
        numerator, denominator = fps_text.split("/")
        source_fps = (
            float(numerator) /
            float(denominator)
        )
    except Exception:
        source_fps = 30.0

    duration = stream.get("duration")

    try:
        duration = float(duration)
    except Exception:
        duration = None

    return {
        "width": width,
        "height": height,
        "fps": source_fps,
        "duration": duration
    }


def download_archive_file(url, destination):
    """
    Download the archive video to a temporary file.
    """

    log("Downloading:", url)

    with requests.get(
        url,
        headers=ARCHIVE_HEADERS,
        timeout=REQUEST_TIMEOUT,
        stream=True
    ) as response:

        response.raise_for_status()

        with open(destination, "wb") as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if chunk:
                    output.write(chunk)


# ============================================================
# VIDEO INFORMATION
# ============================================================

@app.route("/")
def index():
    return jsonify({
        "status": "ok",
        "service": "Roblox Internet Archive Video Streamer",
        "fps": TARGET_FPS,
        "max_resolution": "1280x720"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


@app.route("/video/info", methods=["GET", "POST"])
def video_info():

    try:

        if request.method == "POST":
            body = request.get_json(
                silent=True
            ) or {}

            archive_url = body.get("url")

        else:
            archive_url = request.args.get("url")

        if not archive_url:
            return jsonify({
                "success": False,
                "error": "Missing URL."
            }), 400

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

        download_url = build_archive_download_url(
            identifier,
            filename
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            temp_video = os.path.join(
                temp_dir,
                "source_video"
            )

            download_archive_file(
                download_url,
                temp_video
            )

            source = run_ffprobe(
                temp_video
            )

        width, height = clamp_resolution(
            source["width"],
            source["height"]
        )

        result = {
            "success": True,
            "identifier": identifier,
            "filename": filename,

            "sourceWidth": source["width"],
            "sourceHeight": source["height"],
            "sourceFPS": source["fps"],

            "width": width,
            "height": height,

            "fps": TARGET_FPS,

            "codec": "RGBA",

            "downloadUrl": download_url
        }

        log(
            "Video:",
            filename,
            "| source:",
            source["width"],
            "x",
            source["height"],
            "| output:",
            width,
            "x",
            height,
            "| FPS:",
            TARGET_FPS
        )

        return jsonify(result)

    except Exception as e:

        log("Info error:", repr(e))

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# RAW FRAME STREAM
# ============================================================

def generate_frames(
    video_url,
    width,
    height
):
    """
    Decode the video with FFmpeg and emit individual
    RGBA frames.

    Every frame is:

        width * height * 4 bytes
    """

    scale_filter = (
        f"scale={width}:{height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "format=rgba"
    )

    command = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "error",

        "-i",
        video_url,

        "-vf",
        scale_filter,

        "-r",
        str(TARGET_FPS),

        "-f",
        "rawvideo",

        "-pix_fmt",
        "rgba",

        "pipe:1"
    ]

    log(
        "Starting FFmpeg:",
        width,
        "x",
        height,
        "@",
        TARGET_FPS
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1024 * 1024
    )

    frame_size = (
        width *
        height *
        4
    )

    frame_number = 0

    try:

        while True:

            raw = process.stdout.read(
                frame_size
            )

            if not raw:
                break

            if len(raw) != frame_size:

                log(
                    "Incomplete frame:",
                    len(raw),
                    "expected:",
                    frame_size
                )

                break

            frame_number += 1

            yield raw

    finally:

        try:
            process.kill()
        except Exception:
            pass

        try:
            process.wait(
                timeout=2
            )
        except Exception:
            pass

        log(
            "FFmpeg stopped after",
            frame_number,
            "frames"
        )


@app.route("/video/frames")
def video_frames():

    video_url = request.args.get(
        "url"
    )

    if not video_url:
        return jsonify({
            "success": False,
            "error": "Missing video URL."
        }), 400

    try:

        width = int(
            request.args.get(
                "width",
                "0"
            )
        )

        height = int(
            request.args.get(
                "height",
                "0"
            )
        )

    except ValueError:

        return jsonify({
            "success": False,
            "error": "Invalid resolution."
        }), 400

    if width <= 0 or height <= 0:
        return jsonify({
            "success": False,
            "error": "Invalid resolution."
        }), 400

    # Never allow the client to request something
    # larger than our maximum.
    if width > MAX_WIDTH or height > MAX_HEIGHT:

        width, height = clamp_resolution(
            width,
            height
        )

    def stream():

        frame_number = 0

        for frame in generate_frames(
            video_url,
            width,
            height
        ):

            frame_number += 1

            # ------------------------------------------------
            # Frame packet
            #
            # 4 bytes:
            # frame number
            #
            # followed by:
            # RGBA data
            # ------------------------------------------------

            header = (
                frame_number
                .to_bytes(
                    4,
                    "little"
                )
            )

            yield header
            yield frame

    return Response(
        stream(),
        mimetype="application/octet-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Video-Width": str(width),
            "X-Video-Height": str(height),
            "X-Video-FPS": str(TARGET_FPS)
        }
    )


# ============================================================
# DIRECT INFO + STREAM ENDPOINT
# ============================================================

@app.route("/video/start", methods=["POST"])
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

        download_url = build_archive_download_url(
            identifier,
            filename
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            temp_video = os.path.join(
                temp_dir,
                "source_video"
            )

            download_archive_file(
                download_url,
                temp_video
            )

            source = run_ffprobe(
                temp_video
            )

        width, height = clamp_resolution(
            source["width"],
            source["height"]
        )

        return jsonify({
            "success": True,

            "filename": filename,

            "width": width,
            "height": height,

            "sourceWidth": source["width"],
            "sourceHeight": source["height"],

            "fps": TARGET_FPS,

            "codec": "RGBA",

            "videoUrl": download_url
        })

    except Exception as e:

        log(
            "Start error:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return jsonify({
        "success": False,
        "error": "Endpoint not found."
    }), 404


@app.errorhandler(500)
def internal_error(error):

    return jsonify({
        "success": False,
        "error": "Internal server error."
    }), 500


# ============================================================
# MAIN
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
