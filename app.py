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

# ============================================================
# CONFIG
# ============================================================

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


# ============================================================
# GLOBAL CACHE
# ============================================================

VIDEO_CACHE = {}

STREAMS = {}

LOCK = threading.Lock()


# ============================================================
# INTERNET ARCHIVE
# ============================================================

def get_archive_video(archive_url):

    if "/details/" not in archive_url:

        raise Exception(
            "Invalid Internet Archive URL"
        )

    identifier = (
        archive_url
        .split("/details/", 1)[1]
        .split("?", 1)[0]
        .split("#", 1)[0]
        .strip("/")
    )

    if not identifier:

        raise Exception(
            "Invalid Internet Archive identifier"
        )

    print(
        "[Archive] Identifier:",
        identifier
    )

    response = requests.get(
        f"https://archive.org/metadata/{identifier}",
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if "error" in data:

        raise Exception(
            data["error"]
        )

    videos = []

    for file in data.get("files", []):

        name = file.get(
            "name",
            ""
        )

        if not name.lower().endswith(
            ".mp4"
        ):
            continue

        try:

            size = int(
                file.get(
                    "size",
                    0
                )
            )

        except Exception:

            size = 0

        videos.append({

            "name": name,

            "size": size

        })

    if not videos:

        raise Exception(
            "No MP4 file found"
        )

    # Largest MP4 is normally the best quality.
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

    print(
        "[Archive] Selected:",
        selected["name"]
    )

    print(
        "[Archive] Size:",
        selected["size"]
    )

    return {

        "identifier":
            identifier,

        "url":
            direct_url,

        "filename":
            selected["name"],

        "size":
            selected["size"]

    }


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def download_video(
    url,
    identifier
):

    cache_dir = os.path.join(
        tempfile.gettempdir(),
        "roblox_videos"
    )

    os.makedirs(
        cache_dir,
        exist_ok=True
    )

    safe_identifier = (
        identifier
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )

    filename = os.path.join(
        cache_dir,
        safe_identifier + ".mp4"
    )

    # Already downloaded.
    if os.path.exists(filename):

        try:

            if os.path.getsize(
                filename
            ) > 0:

                print(
                    "[Video] Using cached file:",
                    filename
                )

                return filename

        except Exception:

            pass

    temporary = (
        filename
        + ".download"
    )

    # Remove incomplete download.
    try:

        if os.path.exists(
            temporary
        ):

            os.remove(
                temporary
            )

    except Exception:

        pass

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

                    file.write(
                        chunk
                    )

    os.replace(
        temporary,
        filename
    )

    print(
        "[Video] Download complete:",
        filename
    )

    return filename


# ============================================================
# GET VIDEO
# ============================================================

def get_video(
    archive_url
):

    info = get_archive_video(
        archive_url
    )

    identifier = info[
        "identifier"
    ]

    with LOCK:

        cached = VIDEO_CACHE.get(
            identifier
        )

        if (
            cached
            and
            os.path.exists(cached)
        ):

            info[
                "path"
            ] = cached

            return info

    path = download_video(
        info["url"],
        identifier
    )

    with LOCK:

        VIDEO_CACHE[
            identifier
        ] = path

    info[
        "path"
    ] = path

    return info


# ============================================================
# FFMPEG DECODER
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

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

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
                f"scale={self.width}:"
                f"{self.height}:"
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

        print(
            "[FFmpeg] Starting:",
            self.width,
            "x",
            self.height
        )

        self.process = subprocess.Popen(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            bufsize=1024 * 1024

        )

    # --------------------------------------------------------
    # READ EXACT
    # --------------------------------------------------------

    def read_exact(
        self,
        amount
    ):

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

        return bytes(
            result
        )

    # --------------------------------------------------------
    # GET FRAME
    # --------------------------------------------------------

    def get_frame(
        self
    ):

        with self.lock:

            if self.process is None:

                return None

            raw = self.read_exact(
                self.frame_size
            )

            if raw is None:

                return None

            return np.frombuffer(
                raw,
                dtype=np.uint8
            ).reshape(
                self.height,
                self.width,
                3
            )

    # --------------------------------------------------------
    # RESTART
    # --------------------------------------------------------

    def restart(
        self
    ):

        print(
            "[FFmpeg] Restarting decoder"
        )

        try:

            if self.process:

                self.process.kill()

                self.process.wait(
                    timeout=2
                )

        except Exception:

            pass

        self.process = None

        self.start()

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    def stop(
        self
    ):

        try:

            if self.process:

                self.process.kill()

                self.process.wait(
                    timeout=2
                )

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

    # --------------------------------------------------------
    # RGB888 -> RGB565
    # --------------------------------------------------------

    def rgb565(
        self,
        frame
    ):

        r = frame[
            :,
            :,
            0
        ].astype(
            np.uint16
        )

        g = frame[
            :,
            :,
            1
        ].astype(
            np.uint16
        )

        b = frame[
            :,
            :,
            2
        ].astype(
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

        return (
            header
            +
            payload
        )

    # --------------------------------------------------------
    # DELTA FRAME
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

            y0 = (
                by
                * BLOCK_SIZE
            )

            y1 = min(
                y0 + BLOCK_SIZE,
                self.height
            )

            for bx in range(
                blocks_x
            ):

                x0 = (
                    bx
                    * BLOCK_SIZE
                )

                x1 = min(
                    x0 + BLOCK_SIZE,
                    self.width
                )

                current_block = (
                    current565[
                        y0:y1,
                        x0:x1
                    ]
                )

                previous_block = (
                    previous565[
                        y0:y1,
                        x0:x1
                    ]
                )

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

            result = (
                self.encode_keyframe(
                    frame
                )
            )

        else:

            result = (
                self.encode_delta(
                    frame
                )
            )

        self.previous = (
            frame.copy()
        )

        self.frame_number += 1

        return result


# ============================================================
# VIDEO STREAM
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

        self.decoder = (
            FFmpegDecoder(
                path,
                width,
                height
            )
        )

        self.encoder = (
            RBC1Encoder(
                width,
                height
            )
        )

        self.lock = threading.Lock()

    # --------------------------------------------------------
    # NEXT FRAME
    # --------------------------------------------------------

    def next_frame(
        self
    ):

        with self.lock:

            frame = (
                self.decoder.get_frame()
            )

            if frame is None:

                print(
                    "[Codec] FFmpeg reached EOF"
                )

                self.decoder.restart()

                self.encoder.previous = None

                self.encoder.frame_number = 0

                frame = (
                    self.decoder.get_frame()
                )

                if frame is None:

                    raise Exception(
                        "Could not decode video frame"
                    )

            return (
                self.encoder.encode(
                    frame
                )
            )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    def stop(
        self
    ):

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

        existing = STREAMS.get(
            key
        )

        if existing:

            return existing

        # Remove streams for the same
        # video at another resolution.
        old_keys = []

        for old_key in STREAMS:

            if old_key[0] == identifier:

                old_keys.append(
                    old_key
                )

        for old_key in old_keys:

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
# ROOT
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success":
            True,

        "message":
            "RBC1 video server is running",

        "codec":
            "RBC1",

        "fps":
            FPS,

        "qualities": [

            {
                "width": width,
                "height": height
            }

            for width, height
            in QUALITIES

        ]

    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "success":
            True,

        "codec":
            "RBC1",

        "fps":
            FPS,

        "videos":
            len(VIDEO_CACHE),

        "streams":
            len(STREAMS)

    })


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

            "success":
                False,

            "error":
                "Missing url"

        }), 400

    if (
        "archive.org/details/"
        not in url
    ):

        return jsonify({

            "success":
                False,

            "error":
                "Only Internet Archive URLs are supported"

        }), 400

    try:

        info = get_archive_video(
            url
        )

        # Start at 854x480.
        width, height = (
            QUALITIES[1]
        )

        return jsonify({

            "success":
                True,

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

        print(
            "[Video] /video error:",
            repr(e)
        )

        traceback.print_exc()

        return jsonify({

            "success":
                False,

            "error":
                str(e)

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

    if not url:

        return jsonify({

            "success":
                False,

            "error":
                "Missing url"

        }), 400

    if (
        width,
        height
    ) not in QUALITIES:

        return jsonify({

            "success":
                False,

            "error":
                "Unsupported resolution"

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

        packet = (
            stream.next_frame()
        )

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

            "success":
                False,

            "error":
                str(e)

        }), 500


# ============================================================
# MULTIPLE FRAMES
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

    # Prevent ridiculous requests.
    count = max(
        1,
        min(
            count,
            12
        )
    )

    if not url:

        return jsonify({

            "success":
                False,

            "error":
                "Missing url"

        }), 400

    if (
        width,
        height
    ) not in QUALITIES:

        return jsonify({

            "success":
                False,

            "error":
                "Unsupported resolution"

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

        packets = []

        start_time = (
            time.time()
        )

        for _ in range(
            count
        ):

            packet = (
                stream.next_frame()
            )

            packets.append(
                packet
            )

        # ----------------------------------------------------
        # BATCH FORMAT
        #
        # uint32 packet count
        #
        # for each packet:
        #
        # uint32 packet size
        # packet bytes
        # ----------------------------------------------------

        output = bytearray()

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

        elapsed = (
            time.time()
            - start_time
        )

        print(

            "[RBC1] Batch:",

            len(packets),

            "frames |",

            "bytes:",

            len(output),

            "| time:",

            round(
                elapsed,
                3
            ),

            "sec"

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

            "success":
                False,

            "error":
                str(e)

        }), 500


# ============================================================
# SHUTDOWN
# ============================================================

def shutdown_streams():

    print(
        "[Video] Stopping streams..."
    )

    with LOCK:

        for stream in STREAMS.values():

            try:

                stream.stop()

            except Exception:

                pass

        STREAMS.clear()


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

    print(
        "========================================"
    )

    print(
        "RBC1 ROBLOX VIDEO SERVER"
    )

    print(
        "========================================"
    )

    print(
        "FFmpeg:",
        FFMPEG_PATH
    )

    print(
        "FPS:",
        FPS
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
