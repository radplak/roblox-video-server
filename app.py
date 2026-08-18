import os
import base64
import tempfile
import subprocess
import threading
import requests

from flask import Flask, request, jsonify
from PIL import Image

app = Flask(__name__)

VIDEO_CACHE = {}
CACHE_LOCK = threading.Lock()

WIDTH = 160
HEIGHT = 90
FPS = 10


def get_archive_video(archive_url):
    identifier = archive_url.split("/details/", 1)[1].split("?", 1)[0].split("#", 1)[0]

    response = requests.get(
        f"https://archive.org/metadata/{identifier}",
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    videos = []

    for file in data.get("files", []):
        name = file.get("name", "")

        if name.lower().endswith(".mp4"):
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

    videos.sort(key=lambda x: x["size"], reverse=True)

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


def download_video(url, identifier):
    cache_dir = os.path.join(tempfile.gettempdir(), "roblox_videos")
    os.makedirs(cache_dir, exist_ok=True)

    filename = os.path.join(cache_dir, identifier + ".mp4")

    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        return filename

    temp_filename = filename + ".download"

    print("Downloading:", url)

    with requests.get(
        url,
        stream=True,
        timeout=60
    ) as response:

        response.raise_for_status()

        with open(temp_filename, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)

    os.replace(temp_filename, filename)

    print("Download complete:", filename)

    return filename


def get_video(archive_url):
    info = get_archive_video(archive_url)

    identifier = info["identifier"]

    with CACHE_LOCK:
        if identifier in VIDEO_CACHE:
            info["path"] = VIDEO_CACHE[identifier]
            return info

    path = download_video(
        info["url"],
        identifier
    )

    with CACHE_LOCK:
        VIDEO_CACHE[identifier] = path

    info["path"] = path

    return info


def extract_frame(video_path, frame_number):
    timestamp = frame_number / FPS

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",

        "-ss", str(timestamp),

        "-i", video_path,

        "-frames:v", "1",

        "-vf", f"scale={WIDTH}:{HEIGHT}",

        "-f", "image2pipe",
        "-vcodec", "png",

        "pipe:1"
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15
    )

    if result.returncode != 0:
        raise Exception(
            result.stderr.decode(
                "utf-8",
                errors="ignore"
            )
        )

    if not result.stdout:
        raise Exception("FFmpeg returned an empty frame")

    return result.stdout


@app.route("/")
def home():
    return jsonify({
        "success": True,
        "message": "Video frame server is running",
        "resolution": f"{WIDTH}x{HEIGHT}",
        "fps": FPS
    })


@app.route("/video")
def video():
    url = request.args.get("url")

    if not url:
        return jsonify({
            "success": False,
            "error": "Missing url"
        }), 400

    if "archive.org/details/" not in url:
        return jsonify({
            "success": False,
            "error": "Only Internet Archive URLs are supported"
        }), 400

    try:
        info = get_archive_video(url)

        return jsonify({
            "success": True,
            "url": info["url"],
            "filename": info["filename"],
            "size": info["size"],
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/frame")
def frame():
    url = request.args.get("url")
    frame_number = request.args.get("frame", "0")

    if not url:
        return jsonify({
            "success": False,
            "error": "Missing url"
        }), 400

    if "archive.org/details/" not in url:
        return jsonify({
            "success": False,
            "error": "Only Internet Archive URLs are supported"
        }), 400

    try:
        frame_number = int(frame_number)

        if frame_number < 0:
            frame_number = 0

    except ValueError:
        return jsonify({
            "success": False,
            "error": "Invalid frame number"
        }), 400

    try:
        info = get_video(url)

        png_data = extract_frame(
            info["path"],
            frame_number
        )

        encoded = base64.b64encode(
            png_data
        ).decode("ascii")

        return jsonify({
            "success": True,
            "frame": frame_number,
            "width": WIDTH,
            "height": HEIGHT,
            "format": "png",
            "data": encoded
        })

    except Exception as e:
        print("Frame error:", e)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


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
