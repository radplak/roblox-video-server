from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs
import yt_dlp

app = Flask(__name__)


@app.route("/")
def home():
    return "Roblox Video Server is running!"


@app.route("/test")
def test():
    return jsonify({
        "success": True,
        "message": "Roblox connected successfully!"
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

    try:

        options = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        return jsonify({
            "success": True,
            "video_id": video_id,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
