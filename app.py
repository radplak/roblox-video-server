from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs
import yt_dlp
import os
import shutil
import tempfile

app = Flask(__name__)

SECRET_COOKIE_FILE = "/etc/secrets/cookies.txt"


@app.route("/")
def home():
    return "Roblox Video Server is running!"


@app.route("/test")
def test():
    file_exists = os.path.isfile(SECRET_COOKIE_FILE)

    return jsonify({
        "success": True,
        "message": "Roblox connected successfully!",
        "file_exists": file_exists,
        "file_size": os.path.getsize(SECRET_COOKIE_FILE)
        if file_exists else 0
    })


@app.route("/load")
def load_video():

    url = request.args.get("url", "")

    if not url:
        return jsonify({
            "success": False,
            "error": "No YouTube URL was provided."
        }), 400

    parsed = urlparse(url)

    video_id = None

    if parsed.hostname in ("youtube.com", "www.youtube.com"):
        query = parse_qs(parsed.query)
        video_id = query.get("v", [None])[0]

    elif parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/")

    if not video_id:
        return jsonify({
            "success": False,
            "error": "Invalid YouTube URL."
        }), 400

    temporary_cookie_file = None

    try:

        options = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
        }

        # Copy the read-only Render Secret File
        # into /tmp, which is writable.
        if os.path.isfile(SECRET_COOKIE_FILE):

            temporary_cookie_file = os.path.join(
                tempfile.gettempdir(),
                "youtube_cookies.txt"
            )

            shutil.copyfile(
                SECRET_COOKIE_FILE,
                temporary_cookie_file
            )

            options["cookiefile"] = temporary_cookie_file

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

        return jsonify({
            "success": True,
            "video_id": video_id,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps")
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:

        # Delete the temporary copy when we're finished.
        if temporary_cookie_file and os.path.exists(
            temporary_cookie_file
        ):
            try:
                os.remove(temporary_cookie_file)
            except Exception:
                pass


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
