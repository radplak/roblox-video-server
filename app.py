from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs
import yt_dlp
import os
import tempfile

app = Flask(__name__)

COOKIES = os.environ.get("YOUTUBE_COOKIES", "")


@app.route("/")
def home():
    return "Roblox Video Server is running!"


@app.route("/test")
def test():
    return jsonify({
        "success": True,
        "message": "Roblox connected successfully!",
        "cookies_configured": bool(COOKIES)
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

    cookie_file = None

    try:

        options = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
        }

        # If cookies were configured in Render,
        # create a temporary cookie file for yt-dlp.
        if COOKIES:

            cookie_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False
            )

            cookie_file.write(COOKIES)
            cookie_file.close()

            options["cookiefile"] = cookie_file.name

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

        if cookie_file:

            try:
                os.unlink(cookie_file.name)
            except Exception:
                pass


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
