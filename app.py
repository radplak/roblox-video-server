import os
import base64
import tempfile
import threading
import subprocess
import time
from collections import OrderedDict

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

WIDTH = 854
HEIGHT = 480
FPS = 30

# How many frames FFmpeg produces in one decode request.
BATCH_SIZE = 30

# Maximum cached decoded frames per video.
MAX_FRAME_CACHE = 180

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


# ============================================================
# CACHE
# ============================================================

VIDEO_INFO_CACHE = {}
VIDEO_FILE_CACHE = {}

FRAME_CACHE = {}

CACHE_LOCK = threading.RLock()


# ============================================================
# HELPERS
# ============================================================

def get_identifier(archive_url):

    if "/details/" not in archive_url:
        raise ValueError("Invalid Internet Archive URL")

    identifier = (
        archive_url
        .split("/details/", 1)[1]
        .split("?", 1)[0]
        .split("#", 1)[0]
        .strip("/")
    )

    if not identifier:
        raise ValueError("Missing Internet Archive identifier")

    return identifier


# ============================================================
# INTERNET ARCHIVE
# ============================================================

def get_archive_video(archive_url):

    identifier = get_identifier(archive_url)

    with CACHE_LOCK:

        cached = VIDEO_INFO_CACHE.get(identifier)

        if cached:
            return cached

    metadata_url = (
        f"https://archive.org/metadata/{identifier}"
    )

    print(
        "[Archive] Metadata:",
        metadata_url
    )

    response = requests.get(
        metadata_url,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    videos = []

    for file_data in data.get("files", []):

        name = file_data.get("name", "")

        if not name.lower().endswith(".mp4"):
            continue

        try:
            size = int(
                file_data.get("size", 0)
            )
        except Exception:
            size = 0

        videos.append({
            "name": name,
            "size": size
        })

    if not videos:
        raise Exception(
            "No MP4 file found on Internet Archive"
        )

    # Prefer the largest MP4.
    videos.sort(
        key=lambda x: x["size"],
        reverse=True
    )

    selected = videos[0]

    direct_url = (
        "https://archive.org/download/"
        f"{identifier}/"
        f"{selected['name']}"
    )

    result = {
        "identifier": identifier,
        "url": direct_url,
        "filename": selected["name"],
        "size": selected["size"]
    }

    with CACHE_LOCK:

        VIDEO_INFO_CACHE[identifier] = result

    return result


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

    if os.path.exists(filename):

        if os.path.getsize(filename) > 0:

            print(
                "[Video] Using cached:",
                filename
            )

            return filename

    temporary = filename + ".download"

    print(
        "[Video] Downloading:",
        url
    )

    with requests.get(
        url,
        stream=True,
        timeout=(30, 300)
    ) as response:

        response.raise_for_status()

        with open(
            temporary,
            "wb"
        ) as file:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
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


# ============================================================
# GET LOCAL VIDEO
# ============================================================

def get_video(archive_url):

    info = get_archive_video(
        archive_url
    )

    identifier = info["identifier"]

    with CACHE_LOCK:

        cached = VIDEO_FILE_CACHE.get(
            identifier
        )

        if cached and os.path.exists(cached):

            result = dict(info)

            result["path"] = cached

            return result

    path = download_video(
        info["url"],
        identifier
    )

    with CACHE_LOCK:

        VIDEO_FILE_CACHE[identifier] = path

    result = dict(info)

    result["path"] = path

    return result


# ============================================================
# VIDEO PROBE
# ============================================================

def probe_video(video_path):

    command = [
        FFMPEG_PATH,

        "-hide_banner",
        "-loglevel",
        "error",

        "-i",
        video_path,

        "-f",
        "null",

        "-"
    ]

    # We don't actually need the output here.
    # This simply verifies FFmpeg can open the file.

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30
    )

    if result.returncode != 0:

        error = result.stderr.decode(
            "utf-8",
            errors="ignore"
        )

        raise Exception(
            "FFmpeg could not open video: "
            + error
        )


# ============================================================
# PERSISTENT FRAME DECODER
# ============================================================

class FrameDecoder:

    def __init__(
        self,
        video_path
    ):

        self.video_path = video_path

        self.process = None

        self.lock = threading.Lock()

        self.start()


    def start(self):

        if self.process:

            try:
                self.process.kill()
            except Exception:
                pass

        command = [

            FFMPEG_PATH,

            "-hide_banner",
            "-loglevel",
            "error",

            # Input
            "-i",
            self.video_path,

            # Video only
            "-an",

            # Exact output frame rate
            "-vf",
            (
                f"scale={WIDTH}:{HEIGHT}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={FPS}"
            ),

            # Raw RGBA
            "-pix_fmt",
            "rgba",

            "-f",
            "rawvideo",

            "pipe:1"
        ]

        print(
            "[FFmpeg] Starting persistent decoder"
        )

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,

            # Large pipes where supported.
            bufsize=1024 * 1024
        )


    def read_frame(self):

        frame_size = (
            WIDTH
            * HEIGHT
            * 4
        )

        with self.lock:

            if not self.process:
                self.start()

            data = bytearray()

            while len(data) < frame_size:

                chunk = self.process.stdout.read(
                    frame_size - len(data)
                )

                if not chunk:
                    break

                data.extend(chunk)

            if len(data) != frame_size:

                print(
                    "[FFmpeg] Decoder ended. Restarting."
                )

                self.start()

                return None

            return bytes(data)


    def read_frames(
        self,
        count
    ):

        frames = []

        for _ in range(count):

            frame = self.read_frame()

            if frame is None:
                break

            frames.append(frame)

        return frames


# ============================================================
# DECODER CACHE
# ============================================================

DECODERS = {}


def get_decoder(identifier, video_path):

    with CACHE_LOCK:

        decoder = DECODERS.get(
            identifier
        )

        if decoder:
            return decoder

        decoder = FrameDecoder(
            video_path
        )

        DECODERS[identifier] = decoder

        return decoder


# ============================================================
# FRAME CACHE
# ============================================================

def get_frame_cache(identifier):

    with CACHE_LOCK:

        if identifier not in FRAME_CACHE:

            FRAME_CACHE[identifier] = OrderedDict()

        return FRAME_CACHE[identifier]


# ============================================================
# DECODE BATCH
# ============================================================

def decode_batch(
    identifier,
    video_path,
    start_frame,
    count
):

    cache = get_frame_cache(
        identifier
    )

    result = []

    # First use cached frames.
    missing = []

    with CACHE_LOCK:

        for index in range(
            start_frame,
            start_frame + count
        ):

            cached = cache.get(
                index
            )

            if cached is not None:

                result.append(
                    (index, cached)
                )

            else:

                missing.append(index)


    # If everything was cached.
    if not missing:

        result.sort(
            key=lambda x: x[0]
        )

        return result


    # Current implementation decodes sequentially.
    #
    # FFmpeg itself handles decoding internally.
    # We avoid starting a separate FFmpeg process
    # for every frame.

    decoder = get_decoder(
        identifier,
        video_path
    )

    # If we're asking for frame 0, decode normally.
    #
    # For later requests the persistent decoder continues
    # forward. This endpoint is intended to be consumed
    # sequentially by the Roblox server.

    frames = decoder.read_frames(
        len(missing)
    )

    for index, frame in zip(
        missing,
        frames
    ):

        with CACHE_LOCK:

            cache[index] = frame

            cache.move_to_end(
                index
            )

            while len(cache) > MAX_FRAME_CACHE:

                cache.popitem(
                    last=False
                )

        result.append(
            (index, frame)
        )

    result.sort(
        key=lambda x: x[0]
    )

    return result


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "message":
            "Roblox Internet Archive "
            "480p/30 FPS frame server",

        "width": WIDTH,

        "height": HEIGHT,

        "fps": FPS,

        "codec": "RGBA",

        "mode": "persistent-ffmpeg"
    })


# ============================================================
# VIDEO INFORMATION
# ============================================================

@app.route("/video")
def video():

    archive_url = request.args.get(
        "url"
    )

    if not archive_url:

        return jsonify({

            "success": False,

            "error": "Missing url"

        }), 400


    if "archive.org/details/" not in archive_url:

        return jsonify({

            "success": False,

            "error":
                "Only Internet Archive URLs "
                "are supported"

        }), 400


    try:

        info = get_archive_video(
            archive_url
        )

        # Make sure the video can actually
        # be downloaded.
        get_video(
            archive_url
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

            "codec":
                "RGBA"
        })


    except Exception as error:

        print(
            "[Video] Error:",
            error
        )

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


# ============================================================
# FRAME BATCH
# ============================================================

@app.route("/frames")
def frames():

    archive_url = request.args.get(
        "url"
    )

    start = request.args.get(
        "start",
        "0"
    )

    count = request.args.get(
        "count",
        str(BATCH_SIZE)
    )


    if not archive_url:

        return jsonify({

            "success": False,

            "error": "Missing url"

        }), 400


    try:

        start = int(start)

    except ValueError:

        return jsonify({

            "success": False,

            "error": "Invalid start"

        }), 400


    try:

        count = int(count)

    except ValueError:

        return jsonify({

            "success": False,

            "error": "Invalid count"

        }), 400


    start = max(
        0,
        start
    )

    count = max(
        1,
        min(count, 60)
    )


    try:

        info = get_video(
            archive_url
        )

        identifier = info[
            "identifier"
        ]

        frames = decode_batch(

            identifier,

            info["path"],

            start,

            count
        )


        encoded_frames = []

        for frame_number, raw in frames:

            encoded_frames.append({

                "frame":
                    frame_number,

                "data":
                    base64.b64encode(
                        raw
                    ).decode("ascii")
            })


        return jsonify({

            "success": True,

            "identifier":
                identifier,

            "width":
                WIDTH,

            "height":
                HEIGHT,

            "fps":
                FPS,

            "frames":
                encoded_frames

        })


    except Exception as error:

        print(
            "[Frames] Error:",
            error
        )

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


# ============================================================
# LEGACY SINGLE FRAME
# ============================================================

@app.route("/frame")
def frame():

    archive_url = request.args.get(
        "url"
    )

    frame_number = request.args.get(
        "frame",
        "0"
    )


    if not archive_url:

        return jsonify({

            "success": False,

            "error": "Missing url"

        }), 400


    try:

        frame_number = int(
            frame_number
        )

    except ValueError:

        return jsonify({

            "success": False,

            "error":
                "Invalid frame number"

        }), 400


    try:

        info = get_video(
            archive_url
        )

        identifier = info[
            "identifier"
        ]

        result = decode_batch(

            identifier,

            info["path"],

            max(
                0,
                frame_number
            ),

            1
        )


        if not result:

            raise Exception(
                "No frame returned"
            )


        actual_frame, raw = result[0]


        return jsonify({

            "success": True,

            "frame":
                actual_frame,

            "width":
                WIDTH,

            "height":
                HEIGHT,

            "format":
                "rgba",

            "data":
                base64.b64encode(
                    raw
                ).decode("ascii")
        })


    except Exception as error:

        print(
            "[Frame] Error:",
            error
        )

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


# ============================================================
# MAIN
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
