from flask import Flask, request, jsonify
from urllib.parse import urlparse, parse_qs

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


@app.route("/load", methods=["GET", "POST"])
def load_video():

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        url = body.get("url", "")
    else:
        url = request.args.get("url", "")

    if not url:
        return jsonify({
            "success": False,
            "error": "No YouTube URL was provided."
        }), 400

    # Extract YouTube video ID
    video_id = None

    parsed = urlparse(url)

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

    return jsonify({
        "success": True,
        "video_id": video_id,
        "message": "YouTube URL received!"
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
