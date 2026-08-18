from flask import Flask, jsonify

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
