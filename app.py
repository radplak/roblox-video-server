import os
import json
import struct
import threading
import time
import hashlib
from urllib.parse import urlparse, quote, unquote
import subprocess

import numpy as np
import scipy.io.wavfile as wav
import requests
import zstandard as zstd
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ============================================================
# CONFIG (OPTIMIZED FOR STABLE STREAMING WITH AUDIO)
# ============================================================

OUTPUT_WIDTH = 320
OUTPUT_HEIGHT = 180
TARGET_FPS = 24
FRAME_SIZE = OUTPUT_WIDTH * OUTPUT_HEIGHT * 4  # RGBA

NUM_AUDIO_CHANNELS = 4
AUDIO_SAMPLE_RATE = 22050

REQUEST_TIMEOUT = 60
DEFAULT_HEADERS = {"User-Agent": "RobloxVideoStreamer/1.0"}

CACHE_ROOT = "/tmp/video_cache"
VIDEO_DIR = os.path.join(CACHE_ROOT, "source")
FRAME_DIR = os.path.join(CACHE_ROOT, "frames")

MAX_BATCH = 48
FRAME_WAIT_TIMEOUT = 5.0

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(FRAME_DIR, exist_ok=True)


def log(*args):
    print("[Media]", *args, flush=True)


def is_image_path(path):
    return path.lower().endswith(IMAGE_EXTENSIONS)


# ============================================================
# UNIFIED MEDIA RESOLUTION
# ============================================================

def is_direct_media_url(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    
    if "discordapp.com" in domain or "discordapp.net" in domain or "discord.com" in domain:
        return True
        
    if path.endswith((".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif") + IMAGE_EXTENSIONS):
        return True
        
    return False


def get_archive_identifier(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("archive.org", "www.archive.org"):
        raise ValueError("Only Internet Archive and direct media URLs are supported.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("details", "download"):
        raise ValueError("URL must be an Internet Archive /details/ URL.")
    return parts[1]


def get_archive_metadata(identifier):
    url = "https://archive.org/metadata/" + quote(identifier, safe="")
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("is_dark"):
        raise ValueError("This Internet Archive item is unavailable.")
    return data


def find_video_file(metadata):
    files = metadata.get("files", [])
    candidates = [f for f in files if f.get("name", "").lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".avi"))]
    if not candidates:
        raise ValueError("No supported media file was found.")

    candidates.sort(key=lambda f: -int(f.get("size", 0) or 0))
    return candidates[0]


def build_archive_download_url(identifier, filename):
    return "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(filename, safe="/")


def resolve_video(input_url):
    input_url = input_url.strip()
    
    if is_direct_media_url(input_url):
        parsed = urlparse(input_url)
        clean_path = parsed.path
        identifier = "direct_" + hashlib.md5(clean_path.encode("utf-8")).hexdigest()[:12]
        
        filename = os.path.basename(unquote(clean_path)) or "media.mp4"
        download_url = input_url
        return identifier, filename, download_url

    identifier = get_archive_identifier(input_url)
    metadata = get_archive_metadata(identifier)
    video = find_video_file(metadata)
    filename = video["name"]
    download_url = build_archive_download_url(identifier, filename)
    return identifier, filename, download_url


# ============================================================
# SOURCE MEDIA CACHE
# ============================================================

_download_locks = {}
_download_locks_guard = threading.Lock()


def _lock_for(identifier):
    with _download_locks_guard:
        if identifier not in _download_locks:
            _download_locks[identifier] = threading.Lock()
        return _download_locks[identifier]


def get_cached_video_path(identifier, filename, download_url):
    safe_name = identifier.replace("/", "_")
    dest_path = os.path.join(VIDEO_DIR, safe_name)

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path

    lock = _lock_for(identifier)
    with lock:
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path

        log("Downloading media:", download_url)
        tmp_path = dest_path + ".part"
        with requests.get(download_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
        os.replace(tmp_path, dest_path)
        log("Downloaded media:", dest_path)

    return dest_path


def get_video_info(path):
    is_img = is_image_path(path)
    if is_img:
        command = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json", path
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise ValueError("Invalid image file.")
        return {
            "width": int(streams[0].get("width", 0)),
            "height": int(streams[0].get("height", 0)),
            "duration": 0.0,
            "mediaType": "image"
        }

    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "json", path
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise ValueError("No video stream found.")
    stream = streams[0]
    
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "duration": float(stream.get("duration", 0.0)),
        "mediaType": "video"
    }


# ============================================================
# CODEC & EXTRACTION
# ============================================================

_zstd_compressor = zstd.ZstdCompressor(level=3)


def encode_frame_payload(delta_bytes: bytes) -> bytes:
    return _zstd_compressor.compress(delta_bytes)


def extract_full_audio_track(video_path, wav_path):
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-vn", "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE),
        "-acodec", "pcm_s16le", wav_path
    ]
    subprocess.run(command, check=True)


def get_audio_features_for_frame(audio_data, frame_index, samples_per_frame):
    start = frame_index * samples_per_frame
    end = start + samples_per_frame
    chunk = audio_data[start:end]

    audio_bytes = bytearray()
    
    if len(chunk) == samples_per_frame:
        windowed = chunk * np.hanning(len(chunk))
        fft_data = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(chunk), 1.0 / AUDIO_SAMPLE_RATE)

        peak_indices = np.argsort(fft_data)[-NUM_AUDIO_CHANNELS:][::-1]
        max_possible_mag = (samples_per_frame * 32768) / 2

        for idx in peak_indices:
            freq = int(freqs[idx])
            magnitude = fft_data[idx] / max_possible_mag
            volume = min(1.0, float(magnitude * 3.5))
            vol_byte = int(volume * 255)

            audio_bytes.extend(struct.pack(">HB", freq, vol_byte))
    else:
        audio_bytes.extend(b"\x00" * (NUM_AUDIO_CHANNELS * 3))

    return bytes(audio_bytes)


_jobs = {}
_jobs_guard = threading.Lock()


class ExtractionJob:
    def __init__(self, identifier, video_path, is_image):
        self.identifier = identifier
        self.video_path = video_path
        self.is_image = is_image
        self.frames_dir = os.path.join(FRAME_DIR, identifier.replace("/", "_"))
        os.makedirs(self.frames_dir, exist_ok=True)
        self.ready = 0
        self.done = False
        self.error = None
        self.total_estimate = 1 if is_image else 0
        self.lock = threading.Lock()

    def frame_path(self, index):
        return os.path.join(self.frames_dir, f"{index:06d}.bin")

    def run(self):
        try:
            audio_data = np.array([], dtype=np.int16)
            samples_per_frame = int(AUDIO_SAMPLE_RATE / TARGET_FPS)

            if not self.is_image:
                wav_path = os.path.join(self.frames_dir, "audio.wav")
                try:
                    extract_full_audio_track(self.video_path, wav_path)
                    _, audio_data = wav.read(wav_path)
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except Exception:
                    pass

            vf = (
                f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=rgba"
            )
            
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", self.video_path,
                "-vf", vf,
            ]
            
            if not self.is_image:
                command.extend(["-r", str(TARGET_FPS)])
                
            command.extend(["-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1"])

            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            previous = np.zeros(FRAME_SIZE, dtype=np.uint8)
            index = 0

            while True:
                raw = process.stdout.read(FRAME_SIZE)
                if len(raw) != FRAME_SIZE:
                    break

                out_path = self.frame_path(index)
                current = np.frombuffer(raw, dtype=np.uint8)

                if not os.path.exists(out_path):
                    delta = np.bitwise_xor(current, previous).tobytes()
                    encoded_video = encode_frame_payload(delta)
                    audio_payload = get_audio_features_for_frame(audio_data, index, samples_per_frame)

                    header = struct.pack("<H", len(audio_payload))
                    full_payload = header + audio_payload + encoded_video

                    tmp_path = out_path + ".part"
                    with open(tmp_path, "wb") as f:
                        f.write(full_payload)
                    os.replace(tmp_path, out_path)

                previous = current
                index += 1
                with self.lock:
                    self.ready = index

                if self.is_image:
                    break  # Images only need 1 frame

            process.stdout.close()
            process.wait(timeout=5)

            with self.lock:
                self.done = True
            log("Processed:", self.identifier, f"({index} frames, image={self.is_image})")

        except Exception as e:
            log("Extraction failed:", self.identifier, repr(e))
            with self.lock:
                self.error = str(e)
                self.done = True


def get_or_start_job(identifier, video_path, duration_hint, is_image):
    with _jobs_guard:
        job = _jobs.get(identifier)
        if job is None:
            job = ExtractionJob(identifier, video_path, is_image)
            job.total_estimate = 1 if is_image else max(1, int((duration_hint or 0) * TARGET_FPS))
            _jobs[identifier] = job
            thread = threading.Thread(target=job.run, daemon=True)
            thread.start()
        return job


# ============================================================
# ROUTES
# ============================================================

@app.route("/video", methods=["GET"])
def video_info():
    archive_url = request.args.get("url")
    if not archive_url:
        return jsonify({"success": False, "error": "Missing URL."}), 400

    try:
        identifier, filename, download_url = resolve_video(archive_url)
        video_path = get_cached_video_path(identifier, filename, download_url)
        source = get_video_info(video_path)
        is_image = (source["mediaType"] == "image")
        
        job = get_or_start_job(identifier, video_path, source["duration"], is_image)

        return jsonify({
            "success": True,
            "mediaType": source["mediaType"], # "image" or "video"
            "identifier": identifier,
            "filename": filename,
            "sourceWidth": source["width"],
            "sourceHeight": source["height"],
            "width": OUTPUT_WIDTH,
            "height": OUTPUT_HEIGHT,
            "fps": 0 if is_image else TARGET_FPS,
            "audioChannels": 0 if is_image else NUM_AUDIO_CHANNELS,
            "codec": "delta+zstd+sine_audio",
            "totalFramesEstimate": job.total_estimate,
        })

    except Exception as e:
        log("Media info error:", repr(e))
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/frames", methods=["GET"])
def frames():
    archive_url = request.args.get("url")
    if not archive_url:
        return jsonify({"success": False, "error": "Missing URL."}), 400

    try:
        start_frame = max(0, int(request.args.get("start", "0")))
        count = max(1, min(int(request.args.get("count", "48")), MAX_BATCH))
    except ValueError:
        return jsonify({"success": False, "error": "Invalid frame parameters."}), 400

    try:
        identifier, filename, download_url = resolve_video(archive_url)
        video_path = get_cached_video_path(identifier, filename, download_url)
        source = get_video_info(video_path)
        is_image = (source["mediaType"] == "image")
        
        job = get_or_start_job(identifier, video_path, source["duration"], is_image)

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
            "X-Audio-Channels": "0" if is_image else str(NUM_AUDIO_CHANNELS),
            "X-Complete": "1" if (done or is_image) else "0",
            "X-Frames-Ready": str(ready),
        }
        return Response(bytes(body), mimetype="application/octet-stream", headers=headers)

    except Exception as e:
        log("Frame error:", repr(e))
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    log("Starting media server on port", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
