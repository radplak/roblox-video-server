import os
import tempfile
import threading
import subprocess
from urllib.parse import unquote

import requests
import imageio_ffmpeg

from flask import Flask, request, jsonify, Response


app = Flask(__name__)

VIDEO_CACHE = {}
CACHE_LOCK = threading.Lock()

WIDTH = 160
HEIGHT = 90
FPS = 10

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


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

    # Largest MP4
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


def download_video(url, identifier):
    cache_dir = os.path.join(
        tempfile.gettempdir(),
        "roblox_videos"
    )

    os.makedirs(
        cache_dir,
        exist_ok=True
    )

    safe_identifier = identifier.replace("/", "_")

    filename = os.path.join(
        cache_dir,
        safe_identifier + ".mp4"
    )

    if os.path.exists(filename):
        if os.path.getsize(filename) > 0:
            print("[Video] Using cached video:", filename)
            return filename

    temp_filename = filename + ".download"

    print("[Video] Downloading:", url)

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
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

        raise

    print("[Video] Download complete:", filename)

    return filename


def get_video(archive_url):
    info = get_archive_video(archive_url)

    identifier = info["identifier"]

    with CACHE_LOCK:

        cached = VIDEO_CACHE.get(identifier)

        if cached and os.path.exists(cached):
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


def extract_frame(video_path, frame_number):
    if frame_number < 0:
        frame_number = 0

    timestamp = frame_number / FPS

    command = [
        FFMPEG_PATH,

        "-hide_banner",
        "-loglevel",
        "error",

        "-ss",
        str(timestamp),

        "-i",
        video_path,

        "-frames:v",
        "1",

        "-vf",
        f"scale={WIDTH}:{HEIGHT}",

        "-f",
        "image2pipe",

        "-vcodec",
        "png",

        "pipe:1"
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15
    )

    if result.returncode != 0:

        error = result.stderr.decode(
            "utf-8",
            errors="ignore"
        )

        raise Exception(
            error or "FFmpeg failed"
        )

    if not result.stdout:
        raise Exception(
            "FFmpeg returned an empty frame"
        )

    return result.stdout


@app.route("/")
def home():

    return jsonify({
        "success": True,
        "message": "Roblox Internet Archive video server is running",
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
            "identifier": info["identifier"],
            "url": info["url"],
            "filename": info["filename"],
            "size": info["size"],
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS
        })

    except Exception as e:

        print("[Video] /video error:", e)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/frame")
def frame():

    url = request.args.get("url")

    frame_number = request.args.get(
        "frame",
        "0"
    )

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

    except ValueError:

        return jsonify({
            "success": False,
            "error": "Invalid frame number"
        }), 400

    if frame_number < 0:
        frame_number = 0

    try:

        info = get_video(url)

        png_data = extract_frame(
            info["path"],
            frame_number
        )

        return Response(
            png_data,
            mimetype="image/png",
            headers={
                "Cache-Control": "public, max-age=31536000"
            }
        )

    except Exception as e:

        print(
            "[Video] Frame error:",
            e
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/info")
def info():

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

        video_info = get_archive_video(url)

        return jsonify({
            "success": True,
            "identifier": video_info["identifier"],
            "filename": video_info["filename"],
            "size": video_info["size"],
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS
        })

    except Exception as e:

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
