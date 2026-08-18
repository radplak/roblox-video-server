import os
import io
import json
import base64
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, quote

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

OUTPUT_WIDTH = 854
OUTPUT_HEIGHT = 480
TARGET_FPS = 30

REQUEST_TIMEOUT = 60

ARCHIVE_HEADERS = {
    "User-Agent": "RobloxVideoStreamer/1.0"
}

# Maximum number of frames encoded into one response.
MAX_BATCH = 3

# ============================================================
# LOG
# ============================================================

def log(*args):
    print("[Video]", *args, flush=True)


# ============================================================
# INTERNET ARCHIVE
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
        "URL must be an Internet Archive /details/ URL."
    )


def get_archive_metadata(identifier):

    url = (
        "https://archive.org/metadata/"
        + quote(identifier, safe="")
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
        raise ValueError(
            "This Internet Archive item is unavailable."
        )

    return data


def find_video_file(metadata):

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

            if lower.endswith((
                ".webm",
                ".mkv",
                ".mov",
                ".avi"
            )):

                candidates.append(item)

    if not candidates:

        raise ValueError(
            "No supported video file was found."
        )

    candidates.sort(
        key=lambda item: (
            0
            if item.get("name", "")
            .lower()
            .endswith(".mp4")
            else 1,

            -int(
                item.get("size", 0)
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
        + quote(identifier, safe="")
        + "/"
        + quote(filename, safe="/")
    )


# ============================================================
# DOWNLOAD
# ============================================================

def download_video(url, destination):

    log("Downloading:", url)

    with requests.get(
        url,
        headers=ARCHIVE_HEADERS,
        timeout=REQUEST_TIMEOUT,
        stream=True
    ) as response:

        response.raise_for_status()

        with open(
            destination,
            "wb"
        ) as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if chunk:
                    output.write(chunk)


# ============================================================
# FFPROBE
# ============================================================

def get_video_info(path):

    command = [
        "ffprobe",

        "-v",
        "error",

        "-select_streams",
        "v:0",

        "-show_entries",
        "stream=width,height,duration",

        "-of",
        "json",

        path
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        raise RuntimeError(
            result.stderr
        )

    data = json.loads(
        result.stdout
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

    width = int(
        stream.get(
            "width",
            0
        )
    )

    height = int(
        stream.get(
            "height",
            0
        )
    )

    duration = stream.get(
        "duration"
    )

    try:
        duration = float(duration)
    except Exception:
        duration = None

    return {
        "width": width,
        "height": height,
        "duration": duration
    }


# ============================================================
# GET ARCHIVE VIDEO
# ============================================================

def resolve_video(archive_url):

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

    return (
        identifier,
        filename,
        download_url
    )


# ============================================================
# DECODE FRAMES
# ============================================================

def decode_frames(
    video_path,
    start_frame,
    count
):

    start_time = (
        start_frame / TARGET_FPS
    )

    duration = (
        count / TARGET_FPS
    )

    vf = (
        f"scale={OUTPUT_WIDTH}:"
        f"{OUTPUT_HEIGHT}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_WIDTH}:"
        f"{OUTPUT_HEIGHT}:"
        "(ow-iw)/2:"
        "(oh-ih)/2,"
        "format=rgba"
    )

    command = [

        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "error",

        "-ss",
        str(start_time),

        "-i",
        video_path,

        "-t",
        str(duration),

        "-vf",
        vf,

        "-r",
        str(TARGET_FPS),

        "-frames:v",
        str(count),

        "-f",
        "rawvideo",

        "-pix_fmt",
        "rgba",

        "pipe:1"
    ]

    log(
        "Decoding:",
        "start =", start_frame,
        "count =", count
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    frame_size = (
        OUTPUT_WIDTH
        * OUTPUT_HEIGHT
        * 4
    )

    frames = []

    try:

        for _ in range(count):

            raw = process.stdout.read(
                frame_size
            )

            if len(raw) != frame_size:
                break

            frames.append(
                base64.b64encode(
                    raw
                ).decode("ascii")
            )

    finally:

        try:
            process.stdout.close()
        except Exception:
            pass

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

    return frames


# ============================================================
# ROOT
# ============================================================

@app.route("/")
def index():

    return jsonify({
        "status": "ok",
        "service": "Roblox Video Server",
        "resolution": "854x480",
        "fps": TARGET_FPS,
        "codec": "RGBA"
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
# VIDEO INFO
# ============================================================

@app.route("/video", methods=["GET"])
def video_info():

    archive_url = request.args.get(
        "url"
    )

    if not archive_url:

        return jsonify({
            "success": False,
            "error": "Missing URL."
        }), 400

    try:

        (
            identifier,
            filename,
            download_url
        ) = resolve_video(
            archive_url
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            temporary_path = os.path.join(
                temp_dir,
                "source_video"
            )

            download_video(
                download_url,
                temporary_path
            )

            source = get_video_info(
                temporary_path
            )

        log(
            "Video:",
            filename
        )

        log(
            "Source:",
            source["width"],
            "x",
            source["height"]
        )

        log(
            "Output:",
            OUTPUT_WIDTH,
            "x",
            OUTPUT_HEIGHT
        )

        return jsonify({

            "success": True,

            "identifier":
                identifier,

            "filename":
                filename,

            "sourceWidth":
                source["width"],

            "sourceHeight":
                source["height"],

            "width":
                OUTPUT_WIDTH,

            "height":
                OUTPUT_HEIGHT,

            "fps":
                TARGET_FPS,

            "codec":
                "RGBA",

            "scaled":
                True

        })

    except Exception as e:

        log(
            "Video info error:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# FRAME BATCH
# ============================================================

@app.route("/frames", methods=["GET"])
def frames():

    archive_url = request.args.get(
        "url"
    )

    if not archive_url:

        return jsonify({
            "success": False,
            "error": "Missing URL."
        }), 400

    try:

        start_frame = int(
            request.args.get(
                "start",
                "0"
            )
        )

        count = int(
            request.args.get(
                "count",
                "3"
            )
        )

    except ValueError:

        return jsonify({
            "success": False,
            "error": "Invalid frame parameters."
        }), 400

    start_frame = max(
        0,
        start_frame
    )

    count = max(
        1,
        min(
            count,
            MAX_BATCH
        )
    )

    try:

        (
            identifier,
            filename,
            download_url
        ) = resolve_video(
            archive_url
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            temporary_path = os.path.join(
                temp_dir,
                "source_video"
            )

            download_video(
                download_url,
                temporary_path
            )

            raw_frames = decode_frames(
                temporary_path,
                start_frame,
                count
            )

        result_frames = []

        for index, data in enumerate(
            raw_frames
        ):

            result_frames.append({

                "frame":
                    start_frame + index,

                "data":
                    data

            })

        log(
            "Returning",
            len(result_frames),
            "frames"
        )

        return jsonify({

            "success": True,

            "width":
                OUTPUT_WIDTH,

            "height":
                OUTPUT_HEIGHT,

            "fps":
                TARGET_FPS,

            "frames":
                result_frames

        })

    except Exception as e:

        log(
            "Frame error:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# ERRORS
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
