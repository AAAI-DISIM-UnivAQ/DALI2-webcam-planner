"""
DALI2 Public Tourist Planner — Web frontend
Serves a public-facing web app on port 9000 that shows real-time
crowd levels and visit recommendations from the DALI2 agents.

Data is read from Redis keys written by webcam_bridge.py:
  webcam:ids              — Set of all webcam IDs seen
  webcam:data:{id}        — Hash with crowd/weather analysis
  webcam:img:{id}         — Base64-encoded JPEG snapshot
"""
from __future__ import annotations

import base64
import logging
import os
import time

import redis
from flask import Flask, Response, jsonify, render_template

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frontend")

app = Flask(__name__)

_redis = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    decode_responses=True,
)

# Separate connection for binary image data (decode_responses=False)
_redis_bin = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    decode_responses=False,
)

APP_TITLE = os.getenv("APP_TITLE", "Tourist Planner AI")


@app.route("/")
def index():
    return render_template("index.html", title=APP_TITLE)


@app.route("/api/status")
def status():
    """Return all webcam data sorted by crowd level (least crowded first)."""
    try:
        webcam_ids = _redis.smembers("webcam:ids")
    except redis.RedisError as exc:
        log.error("Redis error: %s", exc)
        return jsonify({"error": "Redis unavailable", "webcams": [], "timestamp": time.time()}), 503

    webcams = []
    for wid in webcam_ids:
        data = _redis.hgetall(f"webcam:data:{wid}")
        if not data:
            continue
        webcams.append({
            "id": wid,
            "name": data.get("name", wid),
            "crowd_level": int(data.get("crowd_level", -1)),
            "weather": data.get("weather", "unknown"),
            "crowd_description": data.get("crowd_description", ""),
            "weather_description": data.get("weather_description", ""),
            "visibility": data.get("visibility", "unknown"),
            "lat": float(data.get("lat", 0)),
            "lon": float(data.get("lon", 0)),
            "updated_at": float(data.get("updated_at", 0)),
            "has_image": bool(_redis.exists(f"webcam:img:{wid}")),
        })

    webcams.sort(key=lambda x: x["crowd_level"])
    return jsonify({"webcams": webcams, "timestamp": time.time()})


@app.route("/api/image/<webcam_id>")
def get_image(webcam_id: str):
    """Serve the cached webcam snapshot as a JPEG image."""
    # Whitelist characters to prevent path-injection via key name
    safe = all(c.isalnum() or c in "-_." for c in webcam_id)
    if not safe or len(webcam_id) > 64:
        return Response("Not found", status=404)

    img_b64 = _redis_bin.get(f"webcam:img:{webcam_id}")
    if not img_b64:
        return Response("No image available", status=404)

    try:
        img_bytes = base64.b64decode(img_b64)
    except Exception:
        return Response("Invalid image data", status=500)

    # Cache-Control aligned with POLL_INTERVAL so browsers don't re-fetch
    # within the same analysis window.
    poll_interval = int(os.getenv("POLL_INTERVAL", "90"))
    return Response(
        img_bytes,
        mimetype="image/jpeg",
        headers={"Cache-Control": f"public, max-age={poll_interval}"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, debug=False)
