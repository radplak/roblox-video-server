from flask import Flask, jsonify
import base64

app = Flask(__name__)

WIDTH = 160
HEIGHT = 90


@app.route("/")
def home():
    return "Roblox Video Server is running!"


@app.route("/test")
def test():
    return jsonify({
        "success": True,
        "message": "Roblox connected successfully!"
    })


@app.route("/frame")
def frame():
    # Create a simple test frame.
    # Each pixel is RGB: red, green, blue.
    pixels = bytearray()

    for y in range(HEIGHT):
        for x in range(WIDTH):
            # Moving-looking gradient based on position
            r = int((x / WIDTH) * 255)
            g = int((y / HEIGHT) * 255)
            b = 120

            pixels.extend([r, g, b])

    encoded = base64.b64encode(pixels).decode("ascii")

    return jsonify({
        "width": WIDTH,
        "height": HEIGHT,
        "format": "RGB24",
        "data": encoded
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
