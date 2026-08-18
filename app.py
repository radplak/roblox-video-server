import os
import tempfile
import threading
import subprocess
import time
import struct
from collections import deque

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify, Response


app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

FPS = 30

# Highest quality we attempt first.
# The server automatically falls back if decoding is too slow.
QUALITY_LEVELS = [
    (854, 480),
    (640, 360),
    (480, 270),
    (320, 180),
    (160, 90),
]

BUFFER_FRAMES = 12

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
        raise Exception("Invalid Internet Archive URL")

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
            "size": size,
        })

    if not videos:
        raise Exception(
            "No MP4 file found"
        )

    videos.sort(
        key=lambda x: x["size"],
        reverse=True
    )

    selected = videos[0]

    direct_url = (
        f"https://archive.org/download/"
        f"{identifier}/"
        f"{selected['name']}"
    )

    return {
        "identifier": identifier,
        "url": direct_url,
        "filename": selected["name"],
        "size": selected["size"],
    }


# ============================================================
# DOWNLOAD
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

    if os.path.exists(filename):

        if os.path.getsize(filename) > 0:

            print(
                "[Video] Using cached:",
                filename
            )

            return filename

    temp_filename = (
        filename + ".download"
    )

    print(
        "[Video] Downloading:",
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
                        file.write(chunk)

        os.replace(
            temp_filename,
            filename
        )

    except Exception:

        if os.path.exists(
            temp_filename
        ):
            os.remove(
                temp_filename
            )

        raise

    print(
        "[Video] Download complete"
    )

    return filename


def get_video(
    archive_url
):

    info = get_archive_video(
        archive_url
    )

    identifier = info["identifier"]

    with LOCK:

        cached = VIDEO_CACHE.get(
            identifier
        )

        if cached and os.path.exists(
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
# CONTINUOUS FFmpeg DECODER
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
            width
            * height
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

        self.thread.start()

    # --------------------------------------------------------

    def start_ffmpeg(self):

        command = [

            FFMPEG_PATH,

            "-hide_banner",
            "-loglevel",
            "error",

            # Let FFmpeg use available CPU threads.
            "-threads",
            "0",

            "-re",

            "-stream_loop",
            "-1",

            "-i",
            self.video_path,

            "-vf",
            (
                f"scale={self.width}:{self.height}:"
                "flags=lanczos"
            ),

            "-r",
            str(FPS),

            "-pix_fmt",
            "rgba",

            "-f",
            "rawvideo",

            "pipe:1",
        ]

        print(
            "[Video] FFmpeg:",
            self.width,
            "x",
            self.height,
            "@",
            FPS
        )

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

    # --------------------------------------------------------

    def read_exact(
        self,
        size
    ):

        data = bytearray()

        while len(data) < size:

            chunk = self.process.stdout.read(
                size - len(data)
            )

            if not chunk:
                return None

            data.extend(chunk)

        return bytes(data)

    # --------------------------------------------------------

    def decode_loop(self):

        while self.running:

            try:

                self.start_ffmpeg()

                while self.running:

                    frame = self.read_exact(
                        self.frame_size
                    )

                    if frame is None:
                        break

                    with self.condition:

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

            except Exception as e:

                print(
                    "[Video] Decoder error:",
                    e
                )

            finally:

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

            if self.running:

                time.sleep(
                    0.1
                )

    # --------------------------------------------------------

    def get_frames(
        self,
        count
    ):

        result = []

        with self.condition:

            while (
                len(result) < count
                and self.running
            ):

                if self.frames:

                    result.append(
                        self.frames.popleft()
                    )

                    self.condition.notify_all()

                else:

                    self.condition.wait(
                        timeout=1
                    )

                    if not self.frames:
                        break

        return result

    # --------------------------------------------------------

    def stop(self):

        self.running = False

        with self.condition:

            self.condition.notify_all()

        if self.process:

            try:
                self.process.kill()
            except Exception:
                pass


# ============================================================
# DECODER MANAGEMENT
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
            return decoder

        decoder = Decoder(
            path,
            width,
            height
        )

        DECODERS[key] = decoder

        return decoder


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "message":
            "Roblox adaptive video server",

        "fps":
            FPS,

        "qualities":
            [
                {
                    "width": w,
                    "height": h
                }
                for w, h
                in QUALITY_LEVELS
            ],

        "decoder":
            "FFmpeg CPU multithreaded",

        "mode":
            "continuous"
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

        # Start with highest requested quality.
        width, height = QUALITY_LEVELS[0]

        return jsonify({

            "success": True,

            "identifier":
                info["identifier"],

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

            "qualities":
                [
                    {
                        "width": w,
                        "height": h
                    }
                    for w, h
                    in QUALITY_LEVELS
                ]
        })

    except Exception as e:

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


# ============================================================
# FRAME BATCH
# ============================================================

@app.route("/frames")
def frames():

    url = request.args.get(
        "url"
    )

    width = request.args.get(
        "width",
        "854"
    )

    height = request.args.get(
        "height",
        "480"
    )

    count = request.args.get(
        "count",
        "2"
    )

    try:

        width = int(width)
        height = int(height)
        count = int(count)

    except ValueError:

        return jsonify({
            "success": False,
            "error": "Invalid parameters"
        }), 400

    allowed = False

    for w, h in QUALITY_LEVELS:

        if (
            width == w
            and height == h
        ):

            allowed = True
            break

    if not allowed:

        return jsonify({
            "success": False,
            "error":
                "Unsupported resolution"
        }), 400

    count = max(
        1,
        min(
            count,
            3
        )
    )

    try:

        info = get_video(
            url
        )

        decoder = get_decoder(
            info["identifier"],
            info["path"],
            width,
            height
        )

        batch = decoder.get_frames(
            count
        )

        if not batch:

            raise Exception(
                "No frames available"
            )

        # ----------------------------------------------------
        # PACKET FORMAT
        #
        # uint32 frame count
        # repeated:
        # uint32 frame size
        # raw RGBA frame
        # ----------------------------------------------------

        output = bytearray()

        output.extend(
            struct.pack(
                "<I",
                len(batch)
            )
        )

        for frame in batch:

            output.extend(
                struct.pack(
                    "<I",
                    len(frame)
                )
            )

            output.extend(
                frame
            )

        return Response(

            bytes(output),

            mimetype=
                "application/octet-stream",

            headers={

                "X-Video-Width":
                    str(width),

                "X-Video-Height":
                    str(height),

                "X-Video-FPS":
                    str(FPS),

                "X-Video-Frames":
                    str(len(batch)),

                "Cache-Control":
                    "no-cache",

                "Content-Length":
                    str(len(output))
            }
        )

    except Exception as e:

        print(
            "[Video] Batch error:",
            e
        )

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


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

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
