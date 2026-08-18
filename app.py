```python
from flask import Flask, request, jsonify
import requests
import re
from urllib.parse import urlparse

app = Flask(__name__)

IA_METADATA = "https://archive.org/metadata/{}"


def get_identifier(url):
    parsed = urlparse(url)

    if parsed.netloc not in ("archive.org", "www.archive.org"):
        return None

    match = re.match(r"^/details/([^/?#]+)", parsed.path)

    if not match:
        return None

    return match.group(1)


@app.route("/")
def index():
    return jsonify({
        "success": True,
        "message": "Internet Archive video server is running"
    })


@app.route("/video")
def video():
    url = request.args.get("url")

    if not url:
        return jsonify({
            "success": False,
            "error": "Missing url parameter"
        }), 400

    identifier = get_identifier(url)

    if not identifier:
        return jsonify({
            "success": False,
            "error": "Invalid Internet Archive URL"
        }), 400

    try:
        response = requests.get(
            IA_METADATA.format(identifier),
            timeout=20
        )

        response.raise_for_status()
        metadata = response.json()

    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Unable to access Internet Archive: {str(e)}"
        }), 502

    files = metadata.get("files", [])

    video_files = []

    for file in files:
        name = file.get("name", "")
        fmt = str(file.get("format", "")).lower()

        if (
            fmt in ("mpeg4", "h.264", "mp4")
            or name.lower().endswith(".mp4")
        ):
            video_files.append(file)

    if not video_files:
        return jsonify({
            "success": False,
            "error": "No MP4 video was found for this item"
        }), 404

    # Prefer the largest MP4 available.
    video_files.sort(
        key=lambda x: int(x.get("size", 0) or 0),
        reverse=True
    )

    selected = video_files[0]
    filename = selected["name"]

    direct_url = (
        f"https://archive.org/download/"
        f"{identifier}/{filename}"
    )

    return jsonify({
        "success": True,
        "identifier": identifier,
        "filename": filename,
        "url": direct_url,
        "size": selected.get("size"),
        "format": selected.get("format")
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000
    )
```
