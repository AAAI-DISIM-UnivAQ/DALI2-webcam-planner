"""
DALI2 Webcam Bridge — fetches webcam snapshots from the Windy API,
sends each image to GPT-4o (via OpenRouter) for crowd/weather analysis,
and publishes structured crowd_report events to DALI2 agents through
the Redis LINDA channel.

LINDA protocol:  TO:CONTENT:FROM
    Outbound (TO=planner, FROM=webcam_bridge):
        crowd_report(webcam_id, name, crowd_level, weather, description)
    Outbound (TO=planner, FROM=webcam_bridge):
        analysis_error(webcam_id, reason)
    Inbound  (TO=webcam_bridge, FROM=planner):
        request_scan          — trigger an immediate full scan
        request_plan           — (ignored here, handled by DALI2 internally)

Run:
    python webcam_bridge.py [--redis-host HOST] [--redis-port PORT]
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from typing import List, Optional

import redis
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webcam_bridge")

LINDA_CHANNEL = "LINDA"
BRIDGE_NAME = "webcam_bridge"

# ── GPT-4o Vision prompt ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are a crowd and weather analyst for tourist monuments.
Given a webcam image of a monument / tourist area, respond ONLY with a JSON
object (no markdown, no explanation) with these exact keys:
{
  "crowd_level": <integer 0-10>,
  "crowd_description": "<one sentence about people density>",
  "weather": "<sunny|cloudy|rainy|foggy|night|unknown>",
  "weather_description": "<one sentence about weather>",
  "visibility": "<good|moderate|poor>"
}
crowd_level: 0 = empty, 1-3 = few people, 4-6 = moderate, 7-9 = crowded, 10 = packed.
If the image is dark (night), still estimate crowd_level from visible lights/people, and set weather to "night".
If the image is unclear, do your best and note it in crowd_description."""


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class Webcam:
    """One monitored webcam parsed from the WEBCAMS env var."""
    id: str
    name: str
    lat: float = 0.0
    lon: float = 0.0


def parse_webcams(env_str: str) -> List[Webcam]:
    """Parse WEBCAMS env var: id:name:lat:lon,id:name:lat:lon,..."""
    cams = []
    for entry in env_str.split(","):
        parts = entry.strip().split(":")
        if len(parts) < 2:
            continue
        wid = parts[0].strip()
        name = parts[1].strip()
        lat = float(parts[2]) if len(parts) > 2 else 0.0
        lon = float(parts[3]) if len(parts) > 3 else 0.0
        cams.append(Webcam(id=wid, name=name, lat=lat, lon=lon))
    return cams


# ── Windy API ─────────────────────────────────────────────────────────

def fetch_webcam_image_url(webcam_id: str, api_key: str) -> Optional[str]:
    """Fetch the current preview image URL from Windy API."""
    url = f"https://api.windy.com/webcams/api/v3/webcams/{webcam_id}"
    params = {"lang": "en", "include": "images"}
    headers = {"accept": "application/json", "x-windy-api-key": api_key}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data["images"]["current"]["preview"]
    except Exception as e:
        log.error("Windy API error for %s: %s", webcam_id, e)
        return None


def download_image_b64(image_url: str) -> Optional[str]:
    """Download an image and return its base64 encoding."""
    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        log.error("Image download error: %s", e)
        return None


# ── OpenRouter / GPT-4o Vision ────────────────────────────────────────

def analyze_image(image_b64: str, webcam_name: str,
                  api_key: str, model: str) -> Optional[dict]:
    """Send a base64 image to GPT-4o via OpenRouter for crowd/weather analysis."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Analyze this webcam image of '{webcam_name}'. "
                         f"Return ONLY the JSON object."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            ]}
        ],
        "max_tokens": 300,
        "temperature": 0.2,
    }
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json=payload, timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # Strip possible markdown fences
        content = re.sub(r"```json\s*", "", content)
        content = re.sub(r"```\s*", "", content)
        return json.loads(content.strip())
    except Exception as e:
        log.error("OpenRouter error: %s", e)
        return None


# ── LINDA helpers ─────────────────────────────────────────────────────

def linda_publish(r: redis.Redis, to: str, content: str, frm: str = BRIDGE_NAME):
    """Publish a message on the LINDA channel."""
    msg = f"{to}:{content}:{frm}"
    r.publish(LINDA_CHANNEL, msg)
    log.info("LINDA → %s", msg)


def escape_prolog(s: str) -> str:
    """Escape a string for use inside Prolog single-quoted atoms."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ── Main loop ─────────────────────────────────────────────────────────

class WebcamBridge:
    def __init__(self, webcams: List[Webcam], windy_key: str,
                 openrouter_key: str, model: str, poll_interval: int,
                 redis_host: str, redis_port: int):
        self.webcams = webcams
        self.windy_key = windy_key
        self.openrouter_key = openrouter_key
        self.model = model
        self.poll_interval = max(poll_interval, 60)  # enforce minimum
        self.r = redis.Redis(host=redis_host, port=redis_port,
                             decode_responses=True)
        self._force_scan = threading.Event()
        self._stop = threading.Event()

    def scan_all(self):
        """Fetch + analyze all webcams and publish results."""
        for cam in self.webcams:
            if self._stop.is_set():
                break
            log.info("── Scanning %s (%s) ──", cam.name, cam.id)

            # 1. Fetch image URL from Windy
            img_url = fetch_webcam_image_url(cam.id, self.windy_key)
            if not img_url:
                linda_publish(self.r, "planner",
                              f"analysis_error('{cam.id}','windy_api_error')")
                continue

            # 2. Download and encode
            img_b64 = download_image_b64(img_url)
            if not img_b64:
                linda_publish(self.r, "planner",
                              f"analysis_error('{cam.id}','image_download_error')")
                continue

            # 3. Analyze with GPT-4o
            result = analyze_image(img_b64, cam.name,
                                   self.openrouter_key, self.model)
            if not result:
                linda_publish(self.r, "planner",
                              f"analysis_error('{cam.id}','llm_error')")
                continue

            # 4. Publish crowd_report to DALI2
            crowd = result.get("crowd_level", -1)
            weather = escape_prolog(result.get("weather", "unknown"))
            desc = escape_prolog(result.get("crowd_description", ""))
            vis = escape_prolog(result.get("visibility", "unknown"))
            w_desc = escape_prolog(result.get("weather_description", ""))

            content = (
                f"crowd_report('{cam.id}','{escape_prolog(cam.name)}',"
                f"{crowd},'{weather}','{desc}','{vis}','{w_desc}',"
                f"{cam.lat},{cam.lon})"
            )
            linda_publish(self.r, "planner", content)

            # Small delay between webcam calls to be nice to APIs
            if cam != self.webcams[-1]:
                time.sleep(2)

    def _listener(self):
        """Listen for LINDA messages addressed to webcam_bridge."""
        ps = self.r.pubsub()
        ps.subscribe(LINDA_CHANNEL)
        for msg in ps.listen():
            if self._stop.is_set():
                break
            if msg["type"] != "message":
                continue
            data = msg["data"]
            parts = data.split(":", 2)
            if len(parts) < 2:
                continue
            to = parts[0]
            if to != BRIDGE_NAME and to != "*":
                continue
            content = parts[1] if len(parts) > 1 else ""
            if "request_scan" in content:
                log.info("Scan requested by agent")
                self._force_scan.set()

    def run(self):
        """Main loop: periodic scan + listen for commands."""
        # Start listener thread
        t = threading.Thread(target=self._listener, daemon=True)
        t.start()

        log.info("Webcam bridge started — %d webcam(s), poll every %ds",
                 len(self.webcams), self.poll_interval)
        for cam in self.webcams:
            log.info("  • %s (%s) @ (%.4f, %.4f)", cam.name, cam.id,
                     cam.lat, cam.lon)

        # Initial scan
        self.scan_all()

        while not self._stop.is_set():
            # Wait for poll interval or forced scan
            triggered = self._force_scan.wait(timeout=self.poll_interval)
            if triggered:
                self._force_scan.clear()
            if not self._stop.is_set():
                self.scan_all()


def main():
    parser = argparse.ArgumentParser(description="DALI2 Webcam Bridge")
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"))
    parser.add_argument("--redis-port", type=int,
                        default=int(os.getenv("REDIS_PORT", "6379")))
    args = parser.parse_args()

    # Load config from env
    windy_key = os.getenv("WINDY_API_KEY", "")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")
    poll_interval = int(os.getenv("POLL_INTERVAL", "90"))
    webcams_str = os.getenv("WEBCAMS", "")

    if not windy_key:
        log.error("WINDY_API_KEY not set"); sys.exit(1)
    if not openrouter_key:
        log.error("OPENROUTER_API_KEY not set"); sys.exit(1)
    if not webcams_str:
        log.error("WEBCAMS not set"); sys.exit(1)

    webcams = parse_webcams(webcams_str)
    if not webcams:
        log.error("No valid webcams parsed from WEBCAMS env"); sys.exit(1)

    bridge = WebcamBridge(
        webcams=webcams,
        windy_key=windy_key,
        openrouter_key=openrouter_key,
        model=model,
        poll_interval=poll_interval,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
    )
    try:
        bridge.run()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        bridge._stop.set()


if __name__ == "__main__":
    main()
