import os
import io
import json
import time
import base64
import hashlib
import threading
import subprocess
from pathlib import Path

import requests
import numpy as np
import imageio_ffmpeg

from flask import Flask, request, jsonify, Response


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

CACHE_DIR = Path(
    os.environ.get(
        "VIDEO_CACHE_DIR",
        "/tmp/roblox_video_cache"
    )
)

CACHE_DIR.mkdir(
    parents=True,
    exist_ok=True
)


FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 360
DEFAULT_FPS = 30

MAX_WIDTH = 1280
MAX_HEIGHT = 720

CACHE_LOCK = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def archive_identifier(url):

    if "/details/" not in url:
        raise ValueError(
            "Only Internet Archive details URLs are supported"
        )

    identifier = (
        url
        .split("/details/", 1)[1]
        .split("?", 1)[0]
        .split("#", 1)[0]
        .strip("/")
    )

    if not identifier:
        raise ValueError(
            "Invalid Internet Archive identifier"
        )

    return identifier


def get_metadata(identifier):

    response = requests.get(
        f"https://archive.org/metadata/{identifier}",
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def find_video(identifier):

    data = get_metadata(identifier)

    files = data.get(
        "files",
        []
    )

    candidates = []

    for file in files:

        name = file.get(
            "name",
            ""
        )

        if not name.lower().endswith(
            ".mp4"
        ):
            continue

        # Ignore obvious tiny/metadata files.
        try:
            size = int(
                file.get(
                    "size",
                    0
                )
            )
        except Exception:
            size = 0

        if size <= 0:
            continue

        candidates.append(
            {
                "name": name,
                "size": size
            }
        )


    if not candidates:

        raise RuntimeError(
            "No MP4 file found in Internet Archive item"
        )


    # Prefer the largest MP4.
    candidates.sort(
        key=lambda x: x["size"],
        reverse=True
    )


    selected = candidates[0]


    return {
        "identifier": identifier,

        "filename": selected["name"],

        "size": selected["size"],

        "url":
            "https://archive.org/download/"
            +
            identifier
            +
            "/"
            +
            selected["name"]
    }


# ============================================================
# CACHE
# ============================================================

def cache_filename(identifier):

    safe = hashlib.sha256(
        identifier.encode(
            "utf-8"
        )
    ).hexdigest()

    return CACHE_DIR / (
        safe
        +
        ".mp4"
    )


def download_video(info):

    destination = cache_filename(
        info["identifier"]
    )


    if destination.exists():

        if destination.stat().st_size > 0:

            return destination


    temporary =
        destination.with_suffix(
            ".download"
        )


    print(
        "[Video] Downloading:",
        info["url"]
    )


    with requests.get(
        info["url"],
        stream=True,
        timeout=120
    ) as response:

        response.raise_for_status()


        with open(
            temporary,
            "wb"
        ) as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024 * 4
            ):

                if chunk:

                    output.write(
                        chunk
                    )


    os.replace(
        temporary,
        destination
    )


    print(
        "[Video] Cached:",
        destination
    )


    return destination


def get_video(identifier):

    info = find_video(
        identifier
    )

    path = download_video(
        info
    )

    info["path"] = str(
        path
    )

    return info


# ============================================================
# VIDEO INFO
# ============================================================

def probe_video(path):

    command = [

        FFMPEG,

        "-hide_banner",

        "-loglevel",
        "error",

        "-i",
        path,

        "-f",
        "null",

        "-"
    ]


    # Instead of relying on ffprobe being installed separately,
    # use ffmpeg's stderr output for basic information.
    #
    # The actual stream endpoint will normalize the output
    # resolution/FPS itself.

    return True


# ============================================================
# RESOLUTION
# ============================================================

def choose_resolution(requested):

    requested = str(
        requested or ""
    ).lower()


    if requested in (
        "720",
        "720p"
    ):

        return 1280, 720


    if requested in (
        "480",
        "480p"
    ):

        return 854, 480


    if requested in (
        "360",
        "360p"
    ):

        return 640, 360


    return (
        DEFAULT_WIDTH,
        DEFAULT_HEIGHT
    )


# ============================================================
# RAW FRAME GENERATOR
# ============================================================

def frame_generator(
    path,
    width,
    height,
    fps
):

    command = [

        FFMPEG,

        "-hide_banner",

        "-loglevel",
        "error",

        "-i",
        path,

        "-vf",
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        ),

        "-r",
        str(fps),

        "-pix_fmt",
        "rgb24",

        "-f",
        "rawvideo",

        "pipe:1"
    ]


    process = subprocess.Popen(

        command,

        stdout=subprocess.PIPE,

        stderr=subprocess.PIPE,

        bufsize=1024 * 1024 * 8
    )


    frame_size =
        width
        *
        height
        *
        3


    try:

        while True:

            raw =
                process.stdout.read(
                    frame_size
                )


            if not raw:
                break


            if len(raw) != frame_size:

                break


            yield raw


    finally:

        try:
            process.stdout.close()
        except Exception:
            pass


        try:
            process.kill()
        except Exception:
            pass


# ============================================================
# SIMPLE RGB565 COMPRESSION
# ============================================================

def rgb888_to_rgb565(raw):

    pixels = np.frombuffer(
        raw,
        dtype=np.uint8
    )


    pixels = pixels.reshape(
        (-1, 3)
    )


    r = (
        pixels[:, 0].astype(
            np.uint16
        )
        >>
        3
    )


    g = (
        pixels[:, 1].astype(
            np.uint16
        )
        >>
        2
    )


    b = (
        pixels[:, 2].astype(
            np.uint16
        )
        >>
        3
    )


    rgb565 = (
        (r << 11)
        |
        (g << 5)
        |
        b
    )


    return rgb565.astype(
        "<u2"
    ).tobytes()


# ============================================================
# STREAM PACKET
# ============================================================

def make_packet(
    frame_number,
    width,
    height,
    raw
):

    compressed =
        rgb888_to_rgb565(
            raw
        )


    header = {

        "codec": "RGB565",

        "frame": frame_number,

        "width": width,

        "height": height,

        "bytes": len(compressed)

    }


    header_bytes =
        json.dumps(
            header,
            separators=(
                ",",
                ":"
            )
        ).encode(
            "utf-8"
        )


    # Format:
    #
    # [4 bytes header length]
    # [JSON header]
    # [RGB565 frame]
    #

    length =
        len(
            header_bytes
        )


    prefix =
        length.to_bytes(
            4,
            "little"
        )


    return (
        prefix
        +
        header_bytes
        +
        compressed
    )


# ============================================================
# INFO
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "service":
            "Roblox Video Stream Server",

        "codec":
            "RGB565",

        "maxResolution":
            "1280x720",

        "fps":
            30

    })


# ============================================================
# VIDEO
# ============================================================

@app.route("/video")
def video():

    url =
        request.args.get(
            "url"
        )


    if not url:

        return jsonify({

            "success": False,

            "error":
                "Missing url"

        }), 400


    try:

        identifier =
            archive_identifier(
                url
            )


        info =
            get_video(
                identifier
            )


        return jsonify({

            "success": True,

            "identifier":
                identifier,

            "filename":
                info["filename"],

            "url":
                info["url"],

            "size":
                info["size"],

            "codec":
                "RGB565",

            "fps":
                DEFAULT_FPS,

            "width":
                DEFAULT_WIDTH,

            "height":
                DEFAULT_HEIGHT

        })


    except Exception as error:

        print(
            "[Video] /video error:",
            error
        )


        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500


# ============================================================
# STREAM
# ============================================================

@app.route("/stream")
def stream():

    url =
        request.args.get(
            "url"
        )


    quality =
        request.args.get(
            "quality",
            "360"
        )


    fps_string =
        request.args.get(
            "fps",
            "30"
        )


    if not url:

        return jsonify({

            "success": False,

            "error":
                "Missing url"

        }), 400


    try:

        fps =
            int(
                fps_string
            )


        fps =
            max(
                1,
                min(
                    fps,
                    30
                )
            )


    except Exception:

        fps = 30


    try:

        identifier =
            archive_identifier(
                url
            )


        info =
            get_video(
                identifier
            )


        width, height =
            choose_resolution(
                quality
            )


        if width > MAX_WIDTH:
            width = MAX_WIDTH

        if height > MAX_HEIGHT:
            height = MAX_HEIGHT


    except Exception as error:

        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500


    def generate():

        frame_number = 0


        for raw in frame_generator(

            info["path"],

            width,

            height,

            fps

        ):

            packet =
                make_packet(

                    frame_number,

                    width,

                    height,

                    raw

                )


            frame_number += 1


            # Length-prefix each packet.
            yield (
                len(packet)
                .to_bytes(
                    4,
                    "little"
                )
                +
                packet
            )


    return Response(

        generate(),

        mimetype=
            "application/octet-stream",

        headers={

            "Cache-Control":
                "no-cache",

            "X-Video-Codec":
                "RGB565",

            "X-Video-Width":
                str(width),

            "X-Video-Height":
                str(height),

            "X-Video-FPS":
                str(fps)

        }

    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port =
        int(
            os.environ.get(
                "PORT",
                10000
            )
        )


    print(
        "[Video] Video stream server starting"
    )


    app.run(

        host="0.0.0.0",

        port=port,

        threaded=True

    )
