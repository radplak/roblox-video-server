import os
import tempfile
import threading
import subprocess
import time
from collections import deque

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify, Response


app = Flask(__name__)

# ============================================================
# VIDEO SETTINGS
# ============================================================

WIDTH = 160
HEIGHT = 90
FPS = 30

# Keep a small buffer so FFmpeg can decode ahead.
BUFFER_SIZE = 8

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# ============================================================
# CACHES
# ============================================================

VIDEO_CACHE = {}

DECODERS = {}

CACHE_LOCK = threading.Lock()


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
        f"https://archive.org/download/"
        f"{identifier}/"
        f"{selected['name']}"
    )

    return {
        "identifier": identifier,
        "url": direct_url,
        "filename": selected["name"],
        "size": selected["size"]
    }


# ============================================================
# DOWNLOAD / CACHE
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

        if os.path.getsize(filename) > 0:

            print(
                "[Video] Using cached:",
                filename
            )

            return filename

    temp_filename = filename + ".download"

    print(
        "[Video] Downloading:",
        url
    )

    try:

        with requests.get(
            url,
            stream=True,
            timeout=60
        ) as response:

            response.raise_for_status()

            with open(
                temp_filename,
                "wb"
            ) as file:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
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
        "[Video] Download complete:",
        filename
    )

    return filename


def get_video(archive_url):

    info = get_archive_video(
        archive_url
    )

    identifier = info["identifier"]

    with CACHE_LOCK:

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

    with CACHE_LOCK:

        VIDEO_CACHE[identifier] = path

    info["path"] = path

    return info


# ============================================================
# CONTINUOUS DECODER
# ============================================================

class VideoDecoder:

    def __init__(
        self,
        video_path
    ):

        self.video_path = video_path

        self.frame_size = (
            WIDTH
            * HEIGHT
            * 2
        )

        self.frames = deque(
            maxlen=BUFFER_SIZE
        )

        self.lock = threading.Lock()

        self.condition = threading.Condition(
            self.lock
        )

        self.running = True

        self.process = None

        self.thread = threading.Thread(
            target=self._decode_loop,
            daemon=True
        )

        self.thread.start()

    def _start_ffmpeg(self):

        command = [

            FFMPEG_PATH,

            "-hide_banner",
            "-loglevel",
            "error",

            # Decode continuously.
            "-threads",
            "0",

            "-i",
            self.video_path,

            # Resize once.
            "-vf",
            f"scale={WIDTH}:{HEIGHT}",

            # RGB565 is half the size of RGBA.
            "-pix_fmt",
            "rgb565le",

            "-f",
            "rawvideo",

            "pipe:1"
        ]

        print(
            "[Video] Starting FFmpeg:",
            self.video_path
        )

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

    def _read_exact(self, size):

        data = bytearray()

        while len(data) < size:

            chunk = self.process.stdout.read(
                size - len(data)
            )

            if not chunk:
                return None

            data.extend(chunk)

        return bytes(data)

    def _decode_loop(self):

        while self.running:

            try:

                self._start_ffmpeg()

                while self.running:

                    frame = self._read_exact(
                        self.frame_size
                    )

                    if frame is None:
                        break

                    with self.condition:

                        # Wait while buffer is full.
                        while (
                            len(self.frames)
                            >= BUFFER_SIZE
                            and self.running
                        ):

                            self.condition.wait(
                                timeout=0.1
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

                # Restart at beginning when video ends.
                time.sleep(0.05)

    def get_frame(self):

        with self.condition:

            while (
                not self.frames
                and self.running
            ):

                self.condition.wait(
                    timeout=1
                )

            if not self.frames:
                return None

            frame = self.frames.popleft()

            self.condition.notify_all()

            return frame

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
# GET / CREATE DECODER
# ============================================================

def get_decoder(
    identifier,
    video_path
):

    with CACHE_LOCK:

        decoder = DECODERS.get(
            identifier
        )

        if decoder:

            return decoder

        decoder = VideoDecoder(
            video_path
        )

        DECODERS[identifier] = decoder

        return decoder


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "message":
            "Roblox Internet Archive "
            "continuous video server",

        "resolution":
            f"{WIDTH}x{HEIGHT}",

        "fps":
            FPS,

        "format":
            "RGB565",

        "decoder":
            "FFmpeg continuous CPU decoding",

        "threads":
            "automatic"
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
                WIDTH,

            "height":
                HEIGHT,

            "fps":
                FPS,

            "format":
                "RGB565"
        })

    except Exception as e:

        print(
            "[Video] /video error:",
            e
        )

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


# ============================================================
# NEXT FRAME
# ============================================================

@app.route("/frame")
def frame():

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

        info = get_video(
            url
        )

        decoder = get_decoder(
            info["identifier"],
            info["path"]
        )

        raw_frame = decoder.get_frame()

        if raw_frame is None:

            raise Exception(
                "Decoder returned no frame"
            )

        return Response(

            raw_frame,

            mimetype=
                "application/octet-stream",

            headers={

                "X-Video-Width":
                    str(WIDTH),

                "X-Video-Height":
                    str(HEIGHT),

                "X-Video-FPS":
                    str(FPS),

                "X-Video-Format":
                    "RGB565",

                "Cache-Control":
                    "no-cache"
            }
        )

    except Exception as e:

        print(
            "[Video] Frame error:",
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
        port=port
    )
