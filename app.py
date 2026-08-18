import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)


@app.route("/")
def home():
    return jsonify({
        "success": True,
        "message": "Server is running"
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

    identifier = url.split("/details/", 1)[1].split("?", 1)[0].split("#", 1)[0]

    try:
        response = requests.get(
            f"https://archive.org/metadata/{identifier}",
            timeout=30
        )

        response.raise_for_status()
        data = response.json()

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    files = data.get("files", [])

    videos = []

    for file in files:
        name = file.get("name", "")

        if name.lower().endswith(".mp4"):
            try:
                size = int(file.get("size", 0))
            except:
                size = 0

            videos.append({
                "name": name,
                "size": size
            })

    if not videos:
        return jsonify({
            "success": False,
            "error": "No MP4 file found"
        }), 404

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

    return jsonify({
        "success": True,
        "url": direct_url,
        "filename": selected["name"],
        "size": selected["size"]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
