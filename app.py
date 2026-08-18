import os
import struct
import tempfile
import threading
import subprocess
import time
import traceback

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify, Response


app = Flask(__name__)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

FPS = 30

# Maximum EditableImage-friendly target.
QUALITIES = [
    (1024, 576),
    (854, 480),
    (640, 360),
    (480, 270),
    (320, 180),
]

# RBC1 settings.
BLOCK_SIZE = 8

# Full keyframe every N frames.
KEYFRAME_INTERVAL = 30

VIDEO_CACHE = {}

DECODERS = {}

LOCK = threading.Lock()


# ============================================================
# INTERNET ARCHIVE
# ============================================================

def get_archive_video(archive_url):

    identifier = (
        archive_url
        .split("/details/", 1)[1]
        .split("?", 1)[0]
        .split("#", 1)[0]
        .strip("/")
    )

    if not identifier:
        raise Exception("Missing Internet Archive identifier")

    response = requests.get(
        f"https://archive.org/metadata/{identifier}",
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    videos = []

    for file in data.get("files", []):

        name = file.get("name", "")

        if not name.lower().endswith(".mp4"):
            continue

        try:
            size = int(file.get("size", 0))
        except Exception:
            size = 0

        videos.append({
            "name": name,
            "size": size
        })

    if not videos:
        raise Exception("No MP4 file found")

    videos.sort(
        key=lambda x: x["size"],
        reverse=True
    )

    selected = videos[0]

    direct_url = (
        "https://archive.org/download/"
        + identifier
        + "/"
        + selected["name"]
    )

    return {
        "identifier": identifier,
        "url": direct_url,
        "filename": selected["name"],
        "size": selected["size"]
    }


# ============================================================
# DOWNLOAD
# ============================================================

def download_video(url, identifier):

    cache_dir = os.path.join(
        tempfile.gettempdir(),
        "roblox_videos"
    )

    os.makedirs(
        cache_dir,
        exist_ok=True
    )

    filename = os.path.join(
        cache_dir,
        identifier.replace("/", "_") + ".mp4"
    )

    if os.path.exists(filename):

        try:
            if os.path.getsize(filename) > 0:
                return filename
        except Exception:
            pass

    temporary = filename + ".download"

    print("[Video] Downloading:", url)

    with requests.get(
        url,
        stream=True,
        timeout=120
    ) as response:

        response.raise_for_status()

        with open(
            temporary,
            "wb"
        ) as file:

            for chunk in response.iter_content(
                chunk_size=4 * 1024 * 1024
            ):

                if chunk:
                    file.write(chunk)

    os.replace(
        temporary,
        filename
    )

    print(
        "[Video] Download complete:",
        filename
    )

    return filename


def get_video(archive_url):

    info = get_archive_video(
        archive_url
    )

    identifier = info["identifier"]

    with LOCK:

        if identifier in VIDEO_CACHE:

            path = VIDEO_CACHE[identifier]

            if os.path.exists(path):

                info["path"] = path

                return info

    path = download_video(
        info["url"],
        identifier
    )

    with LOCK:
        VIDEO_CACHE[identifier] = path

    info["path"] = path

    return info


# ============================================================
# RGB888 -> RGB565
# ============================================================

def rgb565(r, g, b):

    return (
        ((r >> 3) << 11)
        |
        ((g >> 2) << 5)
        |
        (b >> 3)
    )


# ============================================================
# RGB565 -> RGBA
#
# Used only for understanding / testing.
# ============================================================

def rgb565_to_rgba(value):

    r = (value >> 11) & 31
    g = (value >> 5) & 63
    b = value & 31

    r = (r << 3) | (r >> 2)
    g = (g << 2) | (g >> 4)
    b = (b << 3) | (b >> 2)

    return r, g, b, 255


# ============================================================
# FFmpeg decoder
# ============================================================

class FFmpegDecoder:

    def __init__(
        self,
        path,
        width,
        height
    ):

        self.path = path

        self.width = width

        self.height = height

        self.frame_size = (
            width
            * height
            * 3
        )

        self.process = None

        self.lock = threading.Lock()

        self.start()

    def start(self):

        command = [

            FFMPEG_PATH,

            "-hide_banner",
            "-loglevel",
            "error",

            "-threads",
            "0",

            "-i",
            self.path,

            "-vf",
            (
                f"scale={self.width}:{self.height}:"
                "flags=bicubic"
            ),

            "-r",
            str(FPS),

            "-pix_fmt",
            "rgb24",

            "-f",
            "rawvideo",

            "pipe:1"
        ]

        print(
            "[Codec] Starting FFmpeg:",
            self.width,
            "x",
            self.height
        )

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

    def read_exact(self, size):

        data = bytearray()

        while len(data) < size:

            chunk = self.process.stdout.read(
                min(
                    1024 * 1024,
                    size - len(data)
                )
            )

            if not chunk:
                return None

            data.extend(chunk)

        return bytes(data)

    def get_frame(self):

        with self.lock:

            if self.process is None:
                return None

            frame = self.read_exact(
                self.frame_size
            )

            if frame is not None:
                return frame

            try:

                error = (
                    self.process.stderr.read()
                    .decode(
                        "utf-8",
                        errors="ignore"
                    )
                )

                if error.strip():
                    print(
                        "[Codec] FFmpeg:",
                        error
                    )

            except Exception:
                pass

            return None

    def stop(self):

        if self.process:

            try:
                self.process.kill()
            except Exception:
                pass

            self.process = None


# ============================================================
# RBC1 ENCODER
# ============================================================

class RBC1Encoder:

    def __init__(
        self,
        width,
        height
    ):

        self.width = width

        self.height = height

        self.frame_number = 0

        self.previous = None

        self.decoder_frame = None

    # --------------------------------------------------------
    # Read RGB pixel
    # --------------------------------------------------------

    def get_pixel(
        self,
        frame,
        x,
        y
    ):

        index = (
            (y * self.width + x)
            * 3
        )

        return (
            frame[index],
            frame[index + 1],
            frame[index + 2]
        )

    # --------------------------------------------------------
    # Encode a complete frame
    #
    # Format:
    #
    # RBC1
    # frame type
    # width
    # height
    # frame number
    # RGB565 pixels
    # --------------------------------------------------------

    def encode_keyframe(
        self,
        frame
    ):

        output = bytearray()

        output.extend(
            b"RBC1"
        )

        output.append(
            0
        )

        output.extend(
            struct.pack(
                "<HHI",
                self.width,
                self.height,
                self.frame_number
            )
        )

        pixel_count = (
            self.width
            * self.height
        )

        pixels = bytearray(
            pixel_count * 2
        )

        out_index = 0

        for i in range(
            0,
            len(frame),
            3
        ):

            value = rgb565(
                frame[i],
                frame[i + 1],
                frame[i + 2]
            )

            pixels[
                out_index
            ] = value & 255

            pixels[
                out_index + 1
            ] = (value >> 8) & 255

            out_index += 2

        output.extend(
            pixels
        )

        return bytes(output)

    # --------------------------------------------------------
    # Encode delta frame
    #
    # Image divided into 8x8 blocks.
    #
    # Block:
    #
    # 0 = unchanged
    #
    # 1 = changed + raw RGB565
    #
    # This is intentionally simple for RBC1.
    # --------------------------------------------------------

    def encode_delta(
        self,
        frame
    ):

        output = bytearray()

        output.extend(
            b"RBC1"
        )

        output.append(
            1
        )

        output.extend(
            struct.pack(
                "<HHI",
                self.width,
                self.height,
                self.frame_number
            )
        )

        blocks_x = (
            self.width
            + BLOCK_SIZE
            - 1
        ) // BLOCK_SIZE

        blocks_y = (
            self.height
            + BLOCK_SIZE
            - 1
        ) // BLOCK_SIZE

        output.extend(
            struct.pack(
                "<HH",
                blocks_x,
                blocks_y
            )
        )

        previous = self.previous

        for by in range(
            blocks_y
        ):

            for bx in range(
                blocks_x
            ):

                x0 = bx * BLOCK_SIZE
                y0 = by * BLOCK_SIZE

                x1 = min(
                    x0 + BLOCK_SIZE,
                    self.width
                )

                y1 = min(
                    y0 + BLOCK_SIZE,
                    self.height
                )

                changed = False

                # Check block difference.
                for y in range(
                    y0,
                    y1
                ):

                    start = (
                        (y * self.width + x0)
                        * 3
                    )

                    end = (
                        (y * self.width + x1)
                        * 3
                    )

                    if (
                        frame[start:end]
                        !=
                        previous[start:end]
                    ):

                        changed = True
                        break

                if not changed:

                    output.append(
                        0
                    )

                    continue

                output.append(
                    1
                )

                # Write dimensions.
                output.append(
                    x1 - x0
                )

                output.append(
                    y1 - y0
                )

                # Write pixels.
                for y in range(
                    y0,
                    y1
                ):

                    for x in range(
                        x0,
                        x1
                    ):

                        r, g, b = self.get_pixel(
                            frame,
                            x,
                            y
                        )

                        value = rgb565(
                            r,
                            g,
                            b
                        )

                        output.extend(
                            struct.pack(
                                "<H",
                                value
                            )
                        )

        return bytes(output)

    # --------------------------------------------------------
    # Encode
    # --------------------------------------------------------

    def encode(
        self,
        frame
    ):

        force_keyframe = (
            self.previous is None
            or
            self.frame_number
            % KEYFRAME_INTERVAL
            == 0
        )

        if force_keyframe:

            packet = self.encode_keyframe(
                frame
            )

        else:

            packet = self.encode_delta(
                frame
            )

        self.previous = frame

        self.frame_number += 1

        return packet


# ============================================================
# DECODER STATE
# ============================================================

class StreamState:

    def __init__(
        self,
        video_path,
        width,
        height
    ):

        self.video_path = video_path

        self.width = width

        self.height = height

        self.ffmpeg = FFmpegDecoder(
            video_path,
            width,
            height
        )

        self.encoder = RBC1Encoder(
            width,
            height
        )

        self.lock = threading.Lock()

    def next_packet(self):

        with self.lock:

            frame = self.ffmpeg.get_frame()

            if frame is None:

                # Restart decoder.
                self.ffmpeg.stop()

                self.ffmpeg = FFmpegDecoder(
                    self.video_path,
                    self.width,
                    self.height
                )

                frame = self.ffmpeg.get_frame()

                if frame is None:

                    raise Exception(
                        "FFmpeg could not produce a frame"
                    )

            return self.encoder.encode(
                frame
            )

    def stop(self):

        self.ffmpeg.stop()


# ============================================================
# STREAM STATE
# ============================================================

def get_stream(
    identifier,
    path,
    width,
    height
):

    key = (
        identifier,
        width,
        height
    )

    with LOCK:

        stream = DECODERS.get(
            key
        )

        if stream:
            return stream

        # Stop other resolutions for this video.
        old_keys = [
            k for k in DECODERS
            if k[0] == identifier
            and k != key
        ]

        for old_key in old_keys:

            old_stream = DECODERS.pop(
                old_key
            )

            try:
                old_stream.stop()
            except Exception:
                pass

        stream = StreamState(
            path,
            width,
            height
        )

        DECODERS[
            key
        ] = stream

        return stream


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "server":
            "RBC1",

        "fps":
            FPS,

        "blockSize":
            BLOCK_SIZE,

        "keyframeInterval":
            KEYFRAME_INTERVAL,

        "qualities": [

            {
                "width": w,
                "height": h
            }

            for w, h
            in QUALITIES
        ]

    })


# ============================================================
# VIDEO
# ============================================================

@app.route("/video")
def video():

    url = request.args.get(
        "url"
    )

    if not url:

        return jsonify({
            "success": False,
            "error": "Missing url"
        }), 400

    if "archive.org/details/" not in url:

        return jsonify({
            "success": False,
            "error":
                "Only Internet Archive URLs are supported"
        }), 400

    try:

        info = get_archive_video(
            url
        )

        width, height = QUALITIES[0]

        return jsonify({

            "success": True,

            "identifier":
                info["identifier"],

            "url":
                info["url"],

            "filename":
                info["filename"],

            "size":
                info["size"],

            "width":
                width,

            "height":
                height,

            "fps":
                FPS,

            "codec":
                "RBC1",

            "qualities": [

                {
                    "width": w,
                    "height": h
                }

                for w, h
                in QUALITIES
            ]

        })

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


# ============================================================
# RBC1 FRAME
# ============================================================

@app.route("/frame")
def frame():

    url = request.args.get(
        "url"
    )

    if not url:

        return jsonify({
            "success": False,
            "error": "Missing url"
        }), 400

    try:

        width = int(
            request.args.get(
                "width",
                "1024"
            )
        )

        height = int(
            request.args.get(
                "height",
                "576"
            )
        )

    except ValueError:

        return jsonify({
            "success": False,
            "error": "Invalid resolution"
        }), 400

    if (
        width,
        height
    ) not in QUALITIES:

        return jsonify({
            "success": False,
            "error": "Unsupported resolution"
        }), 400

    try:

        info = get_video(
            url
        )

        stream = get_stream(
            info["identifier"],
            info["path"],
            width,
            height
        )

        packet = stream.next_packet()

        print(
            "[RBC1]",
            width,
            "x",
            height,
            "frame:",
            stream.encoder.frame_number - 1,
            "packet:",
            len(packet),
            "bytes"
        )

        return Response(

            packet,

            mimetype=
                "application/octet-stream",

            headers={

                "Cache-Control":
                    "no-cache",

                "X-RBC1":
                    "1",

                "X-Width":
                    str(width),

                "X-Height":
                    str(height),

                "X-FPS":
                    str(FPS)

            }

        )

    except Exception as e:

        print(
            "[RBC1] ERROR:",
            repr(e)
        )

        traceback.print_exc()

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "success": True,

        "codec":
            "RBC1",

        "streams":
            len(DECODERS),

        "videos":
            len(VIDEO_CACHE)

    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    print(
        "========================================"
    )

    print(
        "RBC1 Roblox Video Server"
    )

    print(
        "========================================"
    )

    print(
        "Port:",
        port
    )

    print(
        "FPS:",
        FPS
    )

    print(
        "FFmpeg:",
        FFMPEG_PATH
    )

    print(
        "Block size:",
        BLOCK_SIZE
    )

    print(
        "Keyframe interval:",
        KEYFRAME_INTERVAL
    )

    print(
        "========================================"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
