```python
import os
import re
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

IA_METADATA = "https://archive.org/metadata/{}"


def get_identifier(url):
    try:
        parsed = urlparse(url)

        if parsed.netloc.lower() not in (
            "archive.org",
            "www.archive.org",
        ):
            return None

        match = re.match(
            r"^/details/([^/?#]+)",
            parsed.path,
        )

        if not match:
            return None

        return match.group(1)

    except Exception:
        return None


@app.get("/")
def index():
    return jsonify({
        "success": True,
        "message": "Internet Archive video server is running"
    })


@app.get("/video")
def video():
    url = request.args.get("url", "").strip()

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
            timeout=20,
        )

        response.raise_for_status()
        metadata = response.json()

    except requests.RequestException as e:
        return jsonify({
            "success": False,
            "error": f"Internet Archive request failed: {e}"
        }), 502

    except ValueError:
        return jsonify({
            "success": False,
            "error": "Internet Archive returned invalid JSON"
        }), 502

    files = metadata.get("files", [])

    candidates = []

    for file in files:
        name = str(file.get("name", ""))
        file_format = str(file.get("format", "")).lower()

        if (
            name.lower().endswith(".mp4")
            or file_format in ("mpeg4", "mp4", "h.264")
        ):
            size = file.get("size", 0)

            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0

            candidates.append({
                "name": name,
                "format": file.get("format"),
                "size": size,
            })

    if not candidates:
        return jsonify({
            "success": False,
            "error": "No MP4 video was found"
        }), 404

    candidates.sort(
        key=lambda item: item["size"],
        reverse=True,
    )

    selected = candidates[0]

    direct_url = (
        f"https://archive.org/download/"
        f"{identifier}/"
        f"{selected['name']}"
    )

    return jsonify({
        "success": True,
        "identifier": identifier,
        "filename": selected["name"],
        "format": selected["format"],
        "size": selected["size"],
        "url": direct_url,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
    )
```
