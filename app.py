import os

@app.route("/test")
def test():
    path = "/etc/secrets/cookies.txt"

    return {
        "success": True,
        "file_exists": os.path.isfile(path),
        "file_size": os.path.getsize(path) if os.path.isfile(path) else 0
    }
