import os
import base64
import tempfile
import subprocess
import threading
from collections import OrderedDict

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

TARGET_FPS = 30

# Maximum resolution.
# The source will NOT be enlarged.
MAX_WIDTH = 1280
MAX_HEIGHT = 720

# Frames returned by one request.
MAX_BATCH = 6

# Maximum frames kept in memory for a video.
MAX_CACHE_FRAMES = 180

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


# ============================================================
# GLOBAL CACHE
# ============================================================

VIDEO_INFO_CACHE = {}
VIDEO_FILE_CACHE = {}
DECODERS = {}
FRAME_CACHE = {}

CACHE_LOCK = threading.RLock()


# ============================================================
# GET INTERNET ARCHIVE IDENTIFIER
# ============================================================

def get_identifier(url):

    if "/details/" not in url:

        raise ValueError(
            "Invalid Internet Archive URL"
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
            "Missing Internet Archive identifier"
        )

    return identifier


# ============================================================
# INTERNET ARCHIVE METADATA
# ============================================================

def get_archive_video(archive_url):

    identifier = get_identifier(
        archive_url
    )

    with CACHE_LOCK:

        cached = VIDEO_INFO_CACHE.get(
            identifier
        )

        if cached:

            return cached


    metadata_url = (
        "https://archive.org/metadata/"
        + identifier
    )


    print(
        "[Archive] Requesting metadata:",
        identifier
    )


    response = requests.get(
        metadata_url,
        timeout=30
    )

    response.raise_for_status()

    metadata = response.json()


    videos = []


    for file_data in metadata.get(
        "files",
        []
    ):

        name = file_data.get(
            "name",
            ""
        )


        if not name.lower().endswith(
            ".mp4"
        ):

            continue


        try:

            size = int(
                file_data.get(
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
            "No MP4 file found on Internet Archive"
        )


    # Prefer largest MP4.
    videos.sort(
        key=lambda item: item["size"],
        reverse=True
    )


    selected = videos[0]


    direct_url = (
        "https://archive.org/download/"
        + identifier
        + "/"
        + selected["name"]
    )


    result = {

        "identifier":
            identifier,

        "filename":
            selected["name"],

        "size":
            selected["size"],

        "url":
            direct_url

    }


    with CACHE_LOCK:

        VIDEO_INFO_CACHE[
            identifier
        ] = result


    return result


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def download_video(
    url,
    identifier
):

    directory = os.path.join(
        tempfile.gettempdir(),
        "roblox_video_cache"
    )


    os.makedirs(
        directory,
        exist_ok=True
    )


    safe_name = (
        identifier
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )


    path = os.path.join(
        directory,
        safe_name + ".mp4"
    )


    if os.path.exists(path):

        if os.path.getsize(path) > 0:

            print(
                "[Video] Using cached video:",
                path
            )

            return path


    temporary_path =
        path + ".download"


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
            temporary_path,
            "wb"
        ) as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if chunk:

                    output.write(
                        chunk
                    )


    os.replace(
        temporary_path,
        path
    )


    print(
        "[Video] Download complete:",
        path
    )


    return path


# ============================================================
# GET LOCAL VIDEO
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


    with CACHE_LOCK:

        cached = VIDEO_FILE_CACHE.get(
            identifier
        )


        if cached and os.path.exists(
            cached
        ):

            result = dict(info)

            result["path"] = cached

            return result


    path = download_video(

        info["url"],

        identifier

    )


    with CACHE_LOCK:

        VIDEO_FILE_CACHE[
            identifier
        ] = path


    result = dict(info)

    result["path"] = path

    return result


# ============================================================
# PROBE SOURCE VIDEO
# ============================================================

def probe_video(
    video_path
):

    command = [

        "ffprobe",

        "-v",
        "error",

        "-select_streams",
        "v:0",

        "-show_entries",
        "stream=width,height,r_frame_rate",

        "-of",
        "default=noprint_wrappers=1",

        video_path
    ]


    try:

        result = subprocess.run(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            text=True,

            timeout=30

        )


    except FileNotFoundError:

        # imageio-ffmpeg provides ffmpeg.
        # If ffprobe isn't available, use FFmpeg fallback.
        return probe_with_ffmpeg(
            video_path
        )


    if result.returncode != 0:

        raise Exception(
            result.stderr.strip()
        )


    width = None
    height = None


    for line in result.stdout.splitlines():

        if line.startswith(
            "width="
        ):

            width = int(
                line.split("=", 1)[1]
            )


        elif line.startswith(
            "height="
        ):

            height = int(
                line.split("=", 1)[1]
            )


    if not width or not height:

        raise Exception(
            "Unable to determine video resolution"
        )


    return width, height


# ============================================================
# FFMPEG FALLBACK PROBE
# ============================================================

def probe_with_ffmpeg(
    video_path
):

    command = [

        FFMPEG_PATH,

        "-hide_banner",

        "-i",

        video_path

    ]


    result = subprocess.run(

        command,

        stdout=subprocess.PIPE,

        stderr=subprocess.PIPE,

        text=True,

        timeout=30

    )


    output = result.stderr


    import re


    match = re.search(

        r"Video:.*?(\d{2,5})x(\d{2,5})",

        output

    )


    if not match:

        raise Exception(
            "Unable to determine source resolution"
        )


    width = int(
        match.group(1)
    )

    height = int(
        match.group(2)
    )


    return width, height


# ============================================================
# CALCULATE OUTPUT RESOLUTION
# ============================================================

def calculate_output_size(
    source_width,
    source_height
):

    # Never upscale.
    if (
        source_width <= MAX_WIDTH
        and
        source_height <= MAX_HEIGHT
    ):

        return (
            source_width,
            source_height
        )


    scale = min(

        MAX_WIDTH / source_width,

        MAX_HEIGHT / source_height

    )


    output_width = int(
        source_width * scale
    )

    output_height = int(
        source_height * scale
    )


    # RGBA images should use even dimensions.
    output_width -= (
        output_width % 2
    )

    output_height -= (
        output_height % 2
    )


    output_width = max(
        2,
        output_width
    )

    output_height = max(
        2,
        output_height
    )


    return (
        output_width,
        output_height
    )


# ============================================================
# VIDEO DECODER
# ============================================================

class VideoDecoder:

    def __init__(
        self,
        video_path,
        output_width,
        output_height
    ):

        self.video_path = video_path

        self.width = output_width

        self.height = output_height

        self.process = None

        self.lock = threading.Lock()

        self.start()


    def start(self):

        if self.process:

            try:
                self.process.kill()
            except Exception:
                pass


            try:
                self.process.wait(
                    timeout=1
                )
            except Exception:
                pass


        # ----------------------------------------------------
        # IMPORTANT:
        #
        # We only scale when needed.
        #
        # If the source is already <=720p, FFmpeg receives
        # the source dimensions without a scale filter.
        # ----------------------------------------------------

        scale_filter = (
            f"scale={self.width}:{self.height}"
        )


        command = [

            FFMPEG_PATH,

            "-hide_banner",

            "-loglevel",
            "error",

            "-i",
            self.video_path,

            "-an",

            "-vf",

            (
                scale_filter
                + ",fps="
                + str(TARGET_FPS)
            ),

            "-pix_fmt",
            "rgba",

            "-f",
            "rawvideo",

            "pipe:1"
        ]


        print(
            "[FFmpeg] Starting decoder:",
            self.width,
            "x",
            self.height,
            "@",
            TARGET_FPS
        )


        self.process = subprocess.Popen(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            bufsize=1024 * 1024
        )


    def read_frame(
        self
    ):

        frame_size = (

            self.width
            * self.height
            * 4

        )


        with self.lock:

            if not self.process:

                self.start()


            data = bytearray()


            while len(data) < frame_size:

                chunk = self.process.stdout.read(

                    frame_size
                    - len(data)

                )


                if not chunk:

                    break


                data.extend(
                    chunk
                )


            if len(data) != frame_size:

                print(
                    "[FFmpeg] End of video / decoder restart"
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


            frames.append(
                frame
            )


        return frames


# ============================================================
# GET DECODER
# ============================================================

def get_decoder(
    identifier,
    video_path,
    width,
    height
):

    with CACHE_LOCK:

        decoder = DECODERS.get(
            identifier
        )


        if decoder:

            return decoder


        decoder = VideoDecoder(

            video_path,

            width,

            height

        )


        DECODERS[
            identifier
        ] = decoder


        return decoder


# ============================================================
# GET FRAME CACHE
# ============================================================

def get_frame_cache(
    identifier
):

    with CACHE_LOCK:

        if identifier not in FRAME_CACHE:

            FRAME_CACHE[
                identifier
            ] = OrderedDict()


        return FRAME_CACHE[
            identifier
        ]


# ============================================================
# DECODE FRAMES
# ============================================================

def decode_frames(

    identifier,

    video_path,

    width,

    height,

    start_frame,

    count

):

    cache = get_frame_cache(
        identifier
    )


    result = []

    missing = []


    with CACHE_LOCK:

        for frame_number in range(

            start_frame,

            start_frame + count

        ):

            cached = cache.get(
                frame_number
            )


            if cached is not None:

                result.append(
                    (
                        frame_number,
                        cached
                    )
                )

            else:

                missing.append(
                    frame_number
                )


    # Everything was cached.
    if not missing:

        result.sort(
            key=lambda x: x[0]
        )

        return result


    decoder = get_decoder(

        identifier,

        video_path,

        width,

        height

    )


    frames = decoder.read_frames(
        len(missing)
    )


    for frame_number, frame in zip(

        missing,

        frames

    ):

        with CACHE_LOCK:

            cache[
                frame_number
            ] = frame

            cache.move_to_end(
                frame_number
            )


            while len(cache) > MAX_CACHE_FRAMES:

                cache.popitem(
                    last=False
                )


        result.append(

            (
                frame_number,
                frame
            )

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
            "Internet Archive Roblox video server",

        "max_width":
            MAX_WIDTH,

        "max_height":
            MAX_HEIGHT,

        "fps":
            TARGET_FPS,

        "format":
            "RGBA",

        "mode":
            "source-resolution-max-720p"

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

            "error":
                "Missing url"

        }), 400


    if (
        "archive.org/details/"
        not in archive_url
    ):

        return jsonify({

            "success": False,

            "error":
                "Only Internet Archive URLs are supported"

        }), 400


    try:

        info = get_video(
            archive_url
        )


        source_width, source_height = (
            probe_video(
                info["path"]
            )
        )


        output_width, output_height = (
            calculate_output_size(

                source_width,

                source_height

            )
        )


        print(
            "[Video] Source:",
            source_width,
            "x",
            source_height
        )


        print(
            "[Video] Output:",
            output_width,
            "x",
            output_height
        )


        print(
            "[Video] FPS:",
            TARGET_FPS
        )


        return jsonify({

            "success": True,

            "identifier":
                info["identifier"],

            "filename":
                info["filename"],

            "sourceWidth":
                source_width,

            "sourceHeight":
                source_height,

            "width":
                output_width,

            "height":
                output_height,

            "fps":
                TARGET_FPS,

            "codec":
                "RGBA",

            "scaled":
                (
                    output_width != source_width
                    or
                    output_height != source_height
                )

        })


    except Exception as error:

        print(
            "[Video] Error:",
            repr(error)
        )


        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500


# ============================================================
# FRAME BATCH
# ============================================================

@app.route("/frames")
def frames():

    archive_url = request.args.get(
        "url"
    )


    start_string = request.args.get(
        "start",
        "0"
    )


    count_string = request.args.get(
        "count",
        "3"
    )


    if not archive_url:

        return jsonify({

            "success": False,

            "error":
                "Missing url"

        }), 400


    try:

        start_frame = max(

            0,

            int(start_string)

        )

    except ValueError:

        return jsonify({

            "success": False,

            "error":
                "Invalid start"

        }), 400


    try:

        count = int(
            count_string
        )

    except ValueError:

        count = 3


    count = max(
        1,
        min(count, MAX_BATCH)
    )


    try:

        info = get_video(
            archive_url
        )


        source_width, source_height = (
            probe_video(
                info["path"]
            )
        )


        output_width, output_height = (
            calculate_output_size(

                source_width,

                source_height

            )
        )


        frames = decode_frames(

            info["identifier"],

            info["path"],

            output_width,

            output_height,

            start_frame,

            count

        )


        encoded_frames = []


        for frame_number, raw_frame in frames:

            encoded_frames.append({

                "frame":
                    frame_number,

                "data":
                    base64.b64encode(
                        raw_frame
                    ).decode(
                        "ascii"
                    )

            })


        return jsonify({

            "success": True,

            "width":
                output_width,

            "height":
                output_height,

            "fps":
                TARGET_FPS,

            "frames":
                encoded_frames

        })


    except Exception as error:

        print(
            "[Frames] Error:",
            repr(error)
        )


        return jsonify({

            "success": False,

            "error":
                str(error)

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
