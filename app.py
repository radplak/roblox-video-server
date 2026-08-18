import os
import json
import struct
import threading
import time
from urllib.parse import urlparse, quote
import subprocess
import tempfile

import numpy as np
import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

OUTPUT_WIDTH = 854
OUTPUT_HEIGHT = 480
TARGET_FPS = 30
FRAME_SIZE = OUTPUT_WIDTH * OUTPUT_HEIGHT * 4  # RGBA

REQUEST_TIMEOUT = 60
ARCHIVE_HEADERS = {"User-Agent": "RobloxVideoStreamer/1.0"}

CACHE_ROOT = "/tmp/video_cache"
VIDEO_DIR = os.path.join(CACHE_ROOT, "source")
FRAME_DIR = os.path.join(CACHE_ROOT, "frames")

MAX_BATCH = 15          # frames per /frames response (compressed, so this is cheap now)
FRAME_WAIT_TIMEOUT = 5.0  # seconds to wait for extraction to catch up before responding partial

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(FRAME_DIR, exist_ok=True)


def log(*args):
    print("[Video]", *args, flush=True)


# ============================================================
# INTERNET ARCHIVE RESOLUTION
# ============================================================

def get_archive_identifier(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("archive.org", "www.archive.org"):
        raise ValueError("Only Internet Archive URLs are supported.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        raise ValueError("URL must be an Internet Archive /details/ URL.")
    return parts[1]


def get_archive_metadata(identifier):
    url = "https://archive.org/metadata/" + quote(identifier, safe="")
    response = requests.get(url, headers=ARCHIVE_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("is_dark"):
        raise ValueError("This Internet Archive item is unavailable.")
    return data


def find_video_file(metadata):
    files = metadata.get("files", [])
    mp4s = [f for f in files if f.get("name", "").lower().endswith(".mp4")]
    others = [f for f in files if f.get("name", "").lower().endswith((".webm", ".mkv", ".mov", ".avi"))]
    candidates = mp4s or others
    if not candidates:
        raise ValueError("No supported video file was found.")

    # Prefer archive.org's own transcoded "*.ia.mp4" derivative when present:
    # it's purpose-built for streaming, much smaller than raw uploads, and
    # since we downscale to 480p anyway, source quality beyond that is wasted
    # bandwidth. Falling back to raw originals only when no derivative exists.
    ia_derivatives = [f for f in candidates if f.get("name", "").lower().endswith(".ia.mp4")]
    if ia_derivatives:
        ia_derivatives.sort(key=lambda f: int(f.get("size", 0) or 0))
        return ia_derivatives[0]

    candidates.sort(key=lambda f: -int(f.get("size", 0) or 0))
    return candidates[0]


def build_archive_download_url(identifier, filename):
    return "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(filename, safe="/")


def resolve_video(archive_url):
    identifier = get_archive_identifier(archive_url)
    metadata = get_archive_metadata(identifier)
    video = find_video_file(metadata)
    filename = video["name"]
    download_url = build_archive_download_url(identifier, filename)
    return identifier, filename, download_url


# ============================================================
# SOURCE VIDEO CACHE (download once, reuse forever)
# ============================================================

_download_locks = {}
_download_locks_guard = threading.Lock()


def _lock_for(identifier):
    with _download_locks_guard:
        if identifier not in _download_locks:
            _download_locks[identifier] = threading.Lock()
        return _download_locks[identifier]


def get_cached_video_path(identifier, filename, download_url):
    """Downloads the source video once per identifier and reuses it after that."""
    safe_name = identifier.replace("/", "_")
    dest_path = os.path.join(VIDEO_DIR, safe_name)

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path

    lock = _lock_for(identifier)
    with lock:
        # Re-check after acquiring the lock in case another thread just finished.
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path

        log("Downloading (first time only):", download_url)
        tmp_path = dest_path + ".part"
        with requests.get(download_url, headers=ARCHIVE_HEADERS, timeout=REQUEST_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
        os.replace(tmp_path, dest_path)
        log("Downloaded:", dest_path)

    return dest_path


def get_video_info(path):
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "json", path
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise ValueError("No video stream found.")
    stream = streams[0]
    duration = stream.get("duration")
    try:
        duration = float(duration)
    except Exception:
        duration = None
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "duration": duration,
    }


# ============================================================
# DELTA + RLE "CODEC"
#
# Each frame is XORed against the previously decoded raw frame
# (frame 0 is XORed against all-zero), then run-length encoded.
# Static regions collapse to near-nothing; this is a real,
# if simple, inter-frame compressor - not just a transport tweak.
# ============================================================

def rle_encode(data: bytes) -> bytes:
    arr = np.frombuffer(data, dtype=np.uint8)
    if arr.size == 0:
        return b""
    change_idx = np.nonzero(np.diff(arr))[0] + 1
    starts = np.concatenate(([0], change_idx))
    ends = np.concatenate((change_idx, [arr.size]))
    lengths = (ends - starts).tolist()
    values = arr[starts].tolist()

    out = bytearray()
    for length, value in zip(lengths, values):
        while length > 255:
            out.append(255)
            out.append(value)
            length -= 255
        out.append(length)
        out.append(value)
    return bytes(out)


# ============================================================
# BACKGROUND FRAME EXTRACTION (decode + compress once, cache to disk)
# ============================================================

_jobs = {}
_jobs_guard = threading.Lock()


class ExtractionJob:
    def __init__(self, identifier, video_path):
        self.identifier = identifier
        self.video_path = video_path
        self.frames_dir = os.path.join(FRAME_DIR, identifier.replace("/", "_"))
        os.makedirs(self.frames_dir, exist_ok=True)
        self.ready = 0
        self.done = False
        self.error = None
        self.total_estimate = 0
        self.lock = threading.Lock()

    def frame_path(self, index):
        return os.path.join(self.frames_dir, f"{index:06d}.bin")

    def run(self):
        try:
            vf = (
                f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=rgba"
            )
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", self.video_path,
                "-vf", vf,
                "-r", str(TARGET_FPS),
                "-f", "rawvideo", "-pix_fmt", "rgba",
                "pipe:1",
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            previous = np.zeros(FRAME_SIZE, dtype=np.uint8)
            index = 0

            while True:
                raw = process.stdout.read(FRAME_SIZE)
                if len(raw) != FRAME_SIZE:
                    break

                # Resume support: skip re-encoding frames already cached from a prior run.
                out_path = self.frame_path(index)
                current = np.frombuffer(raw, dtype=np.uint8)

                if not os.path.exists(out_path):
                    delta = np.bitwise_xor(current, previous).tobytes()
                    encoded = rle_encode(delta)
                    tmp_path = out_path + ".part"
                    with open(tmp_path, "wb") as f:
                        f.write(encoded)
                    os.replace(tmp_path, out_path)

                previous = current
                index += 1
                with self.lock:
                    self.ready = index

            process.stdout.close()
            process.wait(timeout=5)

            with self.lock:
                self.done = True
            log("Extraction complete:", self.identifier, "-", index, "frames")

        except Exception as e:
            log("Extraction failed:", self.identifier, repr(e))
            with self.lock:
                self.error = str(e)
                self.done = True


def get_or_start_job(identifier, video_path, duration_hint):
    with _jobs_guard:
        job = _jobs.get(identifier)
        if job is None:
            job = ExtractionJob(identifier, video_path)
            job.total_estimate = int((duration_hint or 0) * TARGET_FPS)
            _jobs[identifier] = job
            thread = threading.Thread(target=job.run, daemon=True)
            thread.start()
        return job


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return jsonify({
        "status": "ok",
        "service": "Roblox Video Server",
        "resolution": f"{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}",
        "fps": TARGET_FPS,
        "codec": "delta+rle",
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


@app.route("/video", methods=["GET"])
def video_info():
    archive_url = request.args.get("url")
    if not archive_url:
        return jsonify({"success": False, "error": "Missing URL."}), 400

    try:
        identifier, filename, download_url = resolve_video(archive_url)
        video_path = get_cached_video_path(identifier, filename, download_url)
        source = get_video_info(video_path)
        job = get_or_start_job(identifier, video_path, source["duration"])

        return jsonify({
            "success": True,
            "identifier": identifier,
            "filename": filename,
            "sourceWidth": source["width"],
            "sourceHeight": source["height"],
            "width": OUTPUT_WIDTH,
            "height": OUTPUT_HEIGHT,
            "fps": TARGET_FPS,
            "codec": "delta+rle",
            "totalFramesEstimate": job.total_estimate,
        })

    except Exception as e:
        log("Video info error:", repr(e))
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/frames", methods=["GET"])
def frames():
    archive_url = request.args.get("url")
    if not archive_url:
        return jsonify({"success": False, "error": "Missing URL."}), 400

    try:
        start_frame = max(0, int(request.args.get("start", "0")))
        count = max(1, min(int(request.args.get("count", "5")), MAX_BATCH))
    except ValueError:
        return jsonify({"success": False, "error": "Invalid frame parameters."}), 400

    try:
        identifier, filename, download_url = resolve_video(archive_url)
        video_path = get_cached_video_path(identifier, filename, download_url)
        source = get_video_info(video_path)
        job = get_or_start_job(identifier, video_path, source["duration"])

        # Give extraction a chance to catch up to the requested range.
        deadline = time.time() + FRAME_WAIT_TIMEOUT
        while time.time() < deadline:
            with job.lock:
                ready, done, error = job.ready, job.done, job.error
            if error:
                return jsonify({"success": False, "error": error}), 500
            if ready >= start_frame + count or (done and ready <= start_frame):
                break
            time.sleep(0.05)

        with job.lock:
            ready, done = job.ready, job.done

        body = bytearray()
        sent = 0
        for i in range(start_frame, start_frame + count):
            if i >= ready:
                break
            path = job.frame_path(i)
            if not os.path.exists(path):
                break
            with open(path, "rb") as f:
                payload = f.read()
            body += struct.pack("<II", i, len(payload))
            body += payload
            sent += 1

        headers = {
            "X-Start-Frame": str(start_frame),
            "X-Frame-Count": str(sent),
            "X-Width": str(OUTPUT_WIDTH),
            "X-Height": str(OUTPUT_HEIGHT),
            "X-Complete": "1" if done else "0",
            "X-Frames-Ready": str(ready),
        }
        return Response(bytes(body), mimetype="application/octet-stream", headers=headers)

    except Exception as e:
        log("Frame error:", repr(e))
        return jsonify({"success": False, "error": str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({"success": False, "error": "Endpoint not found."}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"success": False, "error": "Internal server error."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    log("Starting server on port", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
