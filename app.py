import os
import struct
import tempfile
import threading
import subprocess
import traceback
import time

import requests
import imageio_ffmpeg
import numpy as np

from flask import Flask, request, jsonify, Response


app = Flask(__name__)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

FPS = 30

BLOCK_SIZE = 8

KEYFRAME_INTERVAL = 30

QUALITIES = [
    (1024, 576),
    (854, 480),
    (640, 360),
    (480, 270),
    (320, 180),
]


VIDEO_CACHE = {}
STREAMS = {}

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
        raise Exception("Invalid Internet Archive identifier")

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

    return {
        "identifier": identifier,
        "url": (
            "https://archive.org/download/"
            + identifier
            + "/"
            + selected["name"]
        ),
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

    safe_identifier = identifier.replace(
        "/",
        "_"
    )

    filename = os.path.join(
        cache_dir,
        safe_identifier + ".mp4"
    )

    if os.path.exists(filename):

        try:

            if os.path.getsize(filename) > 0:
                return filename

        except Exception:
            pass

    temporary = filename + ".download"

    print(
        "[Video] Downloading:",
        url
    )

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

        path = VIDEO_CACHE.get(
            identifier
        )

        if path and os.path.exists(path):

            info["path"] = path

            return info

    path = download_video(
        info["url"],
        identifier
    )

    with LOCK:

        VIDEO_CACHE[
            identifier
        ] = path

    info["path"] = path

    return info


# ============================================================
# FFmpeg
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

        self.lock = threading.Lock()

        self.process = None

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
                "flags=bilinear"
            ),

            "-r",
            str(FPS),

            "-pix_fmt",
            "rgb24",

            "-f",
            "rawvideo",

            "pipe:1"

        ]

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 1024
        )

    def read_exact(self, amount):

        result = bytearray()

        while len(result) < amount:

            chunk = self.process.stdout.read(
                amount - len(result)
            )

            if not chunk:
                return None

            result.extend(
                chunk
            )

        return bytes(result)

    def get_frame(self):

        with self.lock:

            frame = self.read_exact(
                self.frame_size
            )

            if frame is None:

                return None

            return np.frombuffer(
                frame,
                dtype=np.uint8
            ).reshape(
                self.height,
                self.width,
                3
            )

    def restart(self):

        try:

            if self.process:
                self.process.kill()

        except Exception:
            pass

        self.process = None

        self.start()

    def stop(self):

        try:

            if self.process:
                self.process.kill()

        except Exception:
            pass

        self.process = None


# ============================================================
# RBC1 CODEC
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

    # --------------------------------------------------------
    # RGB888 -> RGB565
    # --------------------------------------------------------

    def rgb565(
        self,
        frame
    ):

        r = frame[:, :, 0].astype(
            np.uint16
        )

        g = frame[:, :, 1].astype(
            np.uint16
        )

        b = frame[:, :, 2].astype(
            np.uint16
        )

        value = (
            ((r >> 3) << 11)
            |
            ((g >> 2) << 5)
            |
            (b >> 3)
        )

        return value

    # --------------------------------------------------------
    # KEYFRAME
    # --------------------------------------------------------

    def encode_keyframe(
        self,
        frame
    ):

        pixels = self.rgb565(
            frame
        )

        payload = pixels.astype(
            "<u2"
        ).tobytes()

        header = struct.pack(
            "<4sBHHI",
            b"RBC1",
            0,
            self.width,
            self.height,
            self.frame_number
        )

        return header + payload

    # --------------------------------------------------------
    # DELTA
    # --------------------------------------------------------

    def encode_delta(
        self,
        frame
    ):

        previous = self.previous

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

        current565 = self.rgb565(
            frame
        )

        previous565 = self.rgb565(
            previous
        )

        output = bytearray()

        output.extend(
            struct.pack(
                "<4sBHHIHH",
                b"RBC1",
                1,
                self.width,
                self.height,
                self.frame_number,
                blocks_x,
                blocks_y
            )
        )

        for by in range(
            blocks_y
        ):

            y0 = by * BLOCK_SIZE

            y1 = min(
                y0 + BLOCK_SIZE,
                self.height
            )

            for bx in range(
                blocks_x
            ):

                x0 = bx * BLOCK_SIZE

                x1 = min(
                    x0 + BLOCK_SIZE,
                    self.width
                )

                current_block = current565[
                    y0:y1,
                    x0:x1
                ]

                previous_block = previous565[
                    y0:y1,
                    x0:x1
                ]

                if np.array_equal(
                    current_block,
                    previous_block
                ):

                    output.append(
                        0
                    )

                    continue

                output.append(
                    1
                )

                output.append(
                    x1 - x0
                )

                output.append(
                    y1 - y0
                )

                output.extend(
                    current_block.astype(
                        "<u2"
                    ).tobytes()
                )

        return bytes(
            output
        )

    # --------------------------------------------------------
    # ENCODE
    # --------------------------------------------------------

    def encode(
        self,
        frame
    ):

        if (
            self.previous is None
            or
            self.frame_number
            % KEYFRAME_INTERVAL
            == 0
        ):

            result = self.encode_keyframe(
                frame
            )

        else:

            result = self.encode_delta(
                frame
            )

        self.previous = frame.copy()

        self.frame_number += 1

        return result


# ============================================================
# STREAM
# ============================================================

class VideoStream:

    def __init__(
        self,
        path,
        width,
        height
    ):

        self.path = path

        self.width = width

        self.height = height

        self.decoder = FFmpegDecoder(
            path,
            width,
            height
        )

        self.encoder = RBC1Encoder(
            width,
            height
        )

        self.lock = threading.Lock()

    def next_frame(self):

        with self.lock:

            frame = self.decoder.get_frame()

            if frame is None:

                print(
                    "[Codec] FFmpeg reached EOF. Restarting."
                )

                self.decoder.restart()

                self.encoder.previous = None

                self.encoder.frame_number = 0

                frame = self.decoder.get_frame()

                if frame is None:

                    raise Exception(
                        "Could not decode video frame"
                    )

            return self.encoder.encode(
                frame
            )

    def stop(self):

        self.decoder.stop()


# ============================================================
# GET STREAM
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

        stream = STREAMS.get(
            key
        )

        if stream:
            return stream

        # Remove old resolutions.
        for old_key in list(
            STREAMS.keys()
        ):

            if (
                old_key[0]
                == identifier
            ):

                old_stream = STREAMS.pop(
                    old_key
                )

                try:
                    old_stream.stop()
                except Exception:
                    pass

        stream = VideoStream(
            path,
            width,
            height
        )

        STREAMS[
            key
        ] = stream

        return stream


# ============================================================
# VIDEO INFO
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

        width, height = QUALITIES[1]

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
                for w, h in QUALITIES
            ]

        })

    except Exception as e:

        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# ONE FRAME
# ============================================================

@app.route("/frame")
def frame():

    url = request.args.get(
        "url"
    )

    width = int(
        request.args.get(
            "width",
            "854"
        )
    )

    height = int(
        request.args.get(
            "height",
            "480"
        )
    )

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

        packet = stream.next_frame()

        return Response(
            packet,
            mimetype=
                "application/octet-stream"
        )

    except Exception as e:

        print(
            "[RBC1] Frame error:",
            repr(e)
        )

        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# MULTI-FRAME
# ============================================================

@app.route("/frames")
def frames():

    url = request.args.get(
        "url"
    )

    width = int(
        request.args.get(
            "width",
            "854"
        )
    )

    height = int(
        request.args.get(
            "height",
            "480"
        )
    )

    count = int(
        request.args.get(
            "count",
            "8"
        )
    )

    count = max(
        1,
        min(
            count,
            12
        )
    )

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

        output = bytearray()

        packets = []

        start = time.time()

        for _ in range(count):

            packet = stream.next_frame()

            packets.append(
                packet
            )

        # ----------------------------------------------------
        # Batch format:
        #
        # uint32 packet count
        #
        # repeated:
        # uint32 packet length
        # packet data
        # ----------------------------------------------------

        output.extend(
            struct.pack(
                "<I",
                len(packets)
            )
        )

        for packet in packets:

            output.extend(
                struct.pack(
                    "<I",
                    len(packet)
                )
            )

            output.extend(
                packet
            )

        elapsed = time.time() - start

        print(
            "[RBC1] Batch:",
            len(packets),
            "frames",
            "bytes:",
            len(output),
            "time:",
            round(elapsed, 3)
        )

        return Response(
            bytes(output),
            mimetype=
                "application/octet-stream"
        )

    except Exception as e:

        print(
            "[RBC1] Batch error:",
            repr(e)
        )

        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
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

        "fps":
            FPS,

        "streams":
            len(STREAMS),

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
        "================================"
    )

    print(
        "RBC1 VIDEO SERVER"
    )

    print(
        "================================"
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
        "================================"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
