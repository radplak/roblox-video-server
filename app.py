import os
import tempfile
import threading
import subprocess
import time
import struct
import traceback
from collections import deque

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify, Response


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

FPS = 30

QUALITY_LEVELS = [
    (854, 480),
    (640, 360),
    (480, 270),
    (320, 180),
    (160, 90),
]

BUFFER_FRAMES = 6

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


# ============================================================
# GLOBAL STATE
# ============================================================

VIDEO_CACHE = {}
DECODERS = {}

LOCK = threading.Lock()


# ============================================================
# INTERNET ARCHIVE
# ============================================================

def get_archive_video(archive_url):

    archive_url = archive_url.strip()

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
            "Missing Internet Archive identifier"
        )

    print(
        "[Archive] Identifier:",
        identifier
    )

    metadata_url = (
        "https://archive.org/metadata/"
        + identifier
    )

    response = requests.get(
        metadata_url,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    videos = []

    for file in data.get("files", []):

        name = file.get(
            "name",
            ""
        )

        if not name.lower().endswith(".mp4"):
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
            "No MP4 file found in Internet Archive item"
        )

    # Largest MP4 first.
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

    print(
        "[Archive] URL:",
        direct_url
    )

    return {
        "identifier": identifier,
        "url": direct_url,
        "filename": selected["name"],
        "size": selected["size"]
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

    safe_identifier = identifier.replace(
        "/",
        "_"
    )

    filename = os.path.join(
        cache_dir,
        safe_identifier + ".mp4"
    )

    # Already downloaded.
    if os.path.exists(filename):

        try:

            if os.path.getsize(filename) > 0:

                print(
                    "[Video] Using cached file:",
                    filename
                )

                return filename

        except Exception:
            pass

    temp_filename = (
        filename
        + ".download"
    )

    print(
        "[Video] Downloading:"
    )

    print(
        url
    )

    try:

        with requests.get(
            url,
            stream=True,
            timeout=120
        ) as response:

            response.raise_for_status()

            with open(
                temp_filename,
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
            temp_filename,
            filename
        )

    except Exception:

        if os.path.exists(
            temp_filename
        ):

            try:
                os.remove(
                    temp_filename
                )
            except Exception:
                pass

        raise

    print(
        "[Video] Download complete:"
    )

    print(
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

        if cached:

            if os.path.exists(
                cached
            ):

                info["path"] = cached

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
# FFmpeg DECODER
# ============================================================

class Decoder:

    def __init__(
        self,
        video_path,
        width,
        height
    ):

        self.video_path = video_path

        self.width = width

        self.height = height

        self.frame_size = (
            self.width
            * self.height
            * 4
        )

        self.frames = deque(
            maxlen=BUFFER_FRAMES
        )

        self.condition = (
            threading.Condition()
        )

        self.running = True

        self.process = None

        self.thread = threading.Thread(
            target=self.decode_loop,
            daemon=True
        )

        print(
            "[Decoder] Creating decoder:",
            self.width,
            "x",
            self.height
        )

        print(
            "[Decoder] Frame bytes:",
            self.frame_size
        )

        self.thread.start()


    # ========================================================
    # START FFMPEG
    # ========================================================

    def start_ffmpeg(self):

        command = [

            FFMPEG_PATH,

            "-hide_banner",

            "-loglevel",
            "error",

            # Let FFmpeg use all available CPU threads.
            "-threads",
            "0",

            # Loop the video.
            "-stream_loop",
            "-1",

            "-i",
            self.video_path,

            # Resize.
            "-vf",
            (
                "scale="
                + str(self.width)
                + ":"
                + str(self.height)
                + ":flags=lanczos"
            ),

            # Output exactly 30 FPS.
            "-r",
            str(FPS),

            # EditableImage expects RGBA.
            "-pix_fmt",
            "rgba",

            # Raw frames.
            "-f",
            "rawvideo",

            "pipe:1"
        ]

        print(
            "[Decoder] Starting FFmpeg:"
        )

        print(
            " ".join(
                command
            )
        )

        self.process = subprocess.Popen(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            bufsize=0
        )


    # ========================================================
    # READ EXACT BYTES
    # ========================================================

    def read_exact(
        self,
        size
    ):

        data = bytearray()

        while len(data) < size:

            if not self.process:
                return None

            chunk = self.process.stdout.read(
                min(
                    1024 * 1024,
                    size - len(data)
                )
            )

            if not chunk:

                return None

            data.extend(
                chunk
            )

        return bytes(
            data
        )


    # ========================================================
    # READ FFMPEG STDERR
    # ========================================================

    def read_ffmpeg_error(
        self
    ):

        if not self.process:
            return ""

        try:

            if self.process.stderr:

                data = (
                    self.process.stderr.read()
                )

                if data:

                    return data.decode(
                        "utf-8",
                        errors="ignore"
                    )

        except Exception as e:

            print(
                "[Decoder] Error reading FFmpeg stderr:",
                repr(e)
            )

        return ""


    # ========================================================
    # DECODE LOOP
    # ========================================================

    def decode_loop(self):

        print(
            "[Decoder] Decode thread started:",
            self.width,
            "x",
            self.height
        )

        while self.running:

            try:

                self.start_ffmpeg()

                frame_count = 0

                while self.running:

                    frame = self.read_exact(
                        self.frame_size
                    )

                    if frame is None:

                        print(
                            "[Decoder] FFmpeg stopped producing frames"
                        )

                        break

                    frame_count += 1

                    with self.condition:

                        # Wait while the buffer is full.
                        while (
                            len(self.frames)
                            >= BUFFER_FRAMES
                            and self.running
                        ):

                            self.condition.wait(
                                timeout=0.05
                            )

                        if not self.running:
                            break

                        self.frames.append(
                            frame
                        )

                        self.condition.notify_all()

                    if frame_count == 1:

                        print(
                            "[Decoder] First frame decoded:",
                            self.width,
                            "x",
                            self.height
                        )

            except Exception as e:

                print(
                    "[Decoder] Decode exception:",
                    repr(e)
                )

                traceback.print_exc()

            finally:

                # Try to read FFmpeg's error output.
                error = (
                    self.read_ffmpeg_error()
                )

                if error.strip():

                    print(
                        "[Decoder] FFmpeg stderr:"
                    )

                    print(
                        error
                    )

                # Kill FFmpeg.
                if self.process:

                    try:

                        if (
                            self.process.poll()
                            is None
                        ):

                            self.process.kill()

                    except Exception as e:

                        print(
                            "[Decoder] Failed to kill FFmpeg:",
                            repr(e)
                        )

                    try:

                        self.process.wait(
                            timeout=2
                        )

                    except Exception:
                        pass

                    self.process = None

            # Restart FFmpeg if necessary.
            if self.running:

                print(
                    "[Decoder] Restarting FFmpeg..."
                )

                time.sleep(
                    0.5
                )

        print(
            "[Decoder] Decode thread stopped:",
            self.width,
            "x",
            self.height
        )


    # ========================================================
    # GET FRAMES
    # ========================================================

    def get_frames(
        self,
        count
    ):

        result = []

        with self.condition:

            deadline = (
                time.time()
                + 5
            )

            while (
                len(result)
                < count
                and self.running
            ):

                if self.frames:

                    result.append(
                        self.frames.popleft()
                    )

                    self.condition.notify_all()

                    continue

                remaining = (
                    deadline
                    - time.time()
                )

                if remaining <= 0:

                    break

                self.condition.wait(
                    timeout=min(
                        remaining,
                        0.25
                    )
                )

        return result


    # ========================================================
    # STOP
    # ========================================================

    def stop(self):

        print(
            "[Decoder] Stopping:",
            self.width,
            "x",
            self.height
        )

        self.running = False

        with self.condition:

            self.condition.notify_all()

        if self.process:

            try:

                if (
                    self.process.poll()
                    is None
                ):

                    self.process.kill()

            except Exception:
                pass


# ============================================================
# GET / CREATE DECODER
# ============================================================

def get_decoder(
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

        decoder = DECODERS.get(
            key
        )

        if decoder:

            if decoder.running:

                return decoder

        print(
            "[Decoder] Creating new decoder:",
            key
        )

        decoder = Decoder(
            path,
            width,
            height
        )

        DECODERS[
            key
        ] = decoder

        return decoder


# ============================================================
# STOP OLD DECODERS
# ============================================================

def stop_other_decoders(
    identifier,
    width,
    height
):

    with LOCK:

        keys_to_stop = []

        for key in DECODERS:

            if key[0] != identifier:
                continue

            if (
                key[1] == width
                and key[2] == height
            ):
                continue

            keys_to_stop.append(
                key
            )

        for key in keys_to_stop:

            decoder = DECODERS.pop(
                key,
                None
            )

            if decoder:

                decoder.stop()


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "message":
            "Roblox Internet Archive video server",

        "fps":
            FPS,

        "qualities": [

            {
                "width": width,
                "height": height
            }

            for width, height
            in QUALITY_LEVELS
        ],

        "ffmpeg":
            FFMPEG_PATH

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

            "success": False,

            "error":
                "Missing url"

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

        width, height = (
            QUALITY_LEVELS[0]
        )

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

            "qualities": [

                {
                    "width": w,
                    "height": h
                }

                for w, h
                in QUALITY_LEVELS
            ]

        })

    except Exception as e:

        print(
            "[Video] /video error:",
            repr(e)
        )

        traceback.print_exc()

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


# ============================================================
# FRAMES
# ============================================================

@app.route("/frames")
def frames():

    url = request.args.get(
        "url"
    )

    if not url:

        return jsonify({

            "success": False,

            "error":
                "Missing url"

        }), 400

    if "archive.org/details/" not in url:

        return jsonify({

            "success": False,

            "error":
                "Only Internet Archive URLs are supported"

        }), 400

    try:

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
                "2"
            )
        )

    except ValueError:

        return jsonify({

            "success": False,

            "error":
                "Invalid width, height, or count"

        }), 400

    # Validate resolution.
    if (
        width,
        height
    ) not in QUALITY_LEVELS:

        return jsonify({

            "success": False,

            "error":
                "Unsupported resolution"

        }), 400

    # Never allow giant batches.
    count = max(
        1,
        min(
            count,
            2
        )
    )

    print(
        "[Frames] Request:",
        width,
        "x",
        height,
        "count:",
        count
    )

    try:

        # Get/download video.
        info = get_video(
            url
        )

        identifier = info[
            "identifier"
        ]

        path = info[
            "path"
        ]

        if not os.path.exists(
            path
        ):

            raise Exception(
                "Cached video file does not exist"
            )

        file_size = os.path.getsize(
            path
        )

        if file_size <= 0:

            raise Exception(
                "Video file is empty"
            )

        print(
            "[Frames] Video:",
            path
        )

        print(
            "[Frames] Video size:",
            file_size
        )

        # Stop decoders for other qualities.
        stop_other_decoders(
            identifier,
            width,
            height
        )

        # Get decoder.
        decoder = get_decoder(
            identifier,
            path,
            width,
            height
        )

        # Wait/get frames.
        batch = decoder.get_frames(
            count
        )

        if not batch:

            raise Exception(
                "Decoder returned no frames"
            )

        # ----------------------------------------------------
        # PACKET
        #
        # uint32 frame count
        #
        # For every frame:
        #
        # uint32 frame size
        # frame bytes
        # ----------------------------------------------------

        output = bytearray()

        output.extend(
            struct.pack(
                "<I",
                len(batch)
            )
        )

        for frame in batch:

            if len(frame) != (
                width
                * height
                * 4
            ):

                raise Exception(
                    "Invalid frame size: "
                    + str(len(frame))
                )

            output.extend(
                struct.pack(
                    "<I",
                    len(frame)
                )
            )

            output.extend(
                frame
            )

        print(
            "[Frames] Returning:",
            len(batch),
            "frames"
        )

        print(
            "[Frames] Response size:",
            len(output)
        )

        return Response(

            bytes(output),

            status=200,

            mimetype=
                "application/octet-stream",

            headers={

                "Cache-Control":
                    "no-cache",

                "X-Video-Width":
                    str(width),

                "X-Video-Height":
                    str(height),

                "X-Video-FPS":
                    str(FPS),

                "X-Video-Frames":
                    str(len(batch))

            }

        )

    except Exception as e:

        print(
            ""
        )

        print(
            "========================================"
        )

        print(
            "[Frames] ERROR"
        )

        print(
            "========================================"
        )

        print(
            "URL:",
            url
        )

        print(
            "Resolution:",
            width,
            "x",
            height
        )

        print(
            "Error:",
            repr(e)
        )

        traceback.print_exc()

        print(
            "========================================"
        )

        return jsonify({

            "success": False,

            "error":
                str(e),

            "type":
                type(e).__name__

        }), 500


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "success": True,

        "status":
            "online",

        "decoders":
            len(DECODERS),

        "cached_videos":
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
        "Roblox Internet Archive Video Server"
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
        "Qualities:",
        QUALITY_LEVELS
    )

    print(
        "========================================"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
