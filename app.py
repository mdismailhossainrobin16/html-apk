"""APK Converter Studio — Website + Telegram bot in one Railway service.

* Website  : served on $PORT (Railway "web" service)
* Telegram : starts automatically if a valid BOT_TOKEN env var is set
"""
import io
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image
from waitress import serve

API_URL = "https://app-creator-x.ueu.workers.dev/api/v1/build"

MAX_HTML_SIZE = 10 * 1024 * 1024
MAX_ICON_SIZE = 6 * 1024 * 1024
MAX_ICON_PIXELS = 4_000_000
MAX_TOTAL_BODY = 16_842_752
MAX_APP_NAME_UTF16 = 80
MAX_APP_NAME_UTF8 = 120

# Abuse protection: builds per IP per minute, and parallel builds overall
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "3"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL_BUILDS", "4"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("apk_converter")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_TOTAL_BODY

_hits = defaultdict(deque)
_hits_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


def rate_limited(ip):
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        return False


def validate_app_name(value):
    value = (value or "").strip()
    if not value:
        return "Please enter an app name."
    if len(value.encode("utf-16-le")) // 2 > MAX_APP_NAME_UTF16:
        return "App name is too long (max 80 characters)."
    if len(value.encode("utf-8")) > MAX_APP_NAME_UTF8:
        return "App name is too large (max 120 bytes)."
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        return "App name cannot contain control characters."
    return None


def validate_icon(content):
    if not content:
        return "The icon file is empty."
    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.format not in ("PNG", "JPEG"):
                return "Only PNG and JPEG icons are supported."
            w, h = img.size
            if w * h > MAX_ICON_PIXELS:
                return "Icon is too large: maximum is 4 megapixels."
            if img.format == "PNG" and img.info.get("interlace"):
                return "Interlaced PNG icons are not supported. Export a regular PNG."
    except Exception:
        return "That file is not a valid PNG or JPEG image."
    return None


def api_error_message(resp):
    try:
        data = resp.json()
        detail = data.get("error") or data.get("message") or resp.text
    except Exception:
        detail = resp.text or f"HTTP {resp.status_code}"
    known = {
        400: "The build service rejected the files. Check your HTML/ZIP and icon.",
        413: "Files are too large for the build service.",
        429: "The build service is rate-limited. Wait a minute and retry.",
        500: "The build service failed unexpectedly. Try smaller files.",
    }
    return known.get(resp.status_code, str(detail)[:300])


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/build")
def build():
    ip = client_ip()
    if rate_limited(ip):
        return jsonify(error="Too many builds. Please wait a minute and try again."), 429

    app_name = (request.form.get("app_name") or "").strip()
    html_file = request.files.get("html")
    icon_file = request.files.get("icon")

    err = validate_app_name(app_name)
    if err:
        return jsonify(error=err), 400
    if not html_file or not html_file.filename:
        return jsonify(error="Please upload an HTML or ZIP file."), 400
    if not icon_file or not icon_file.filename:
        return jsonify(error="Please upload an app icon (PNG or JPEG)."), 400

    html_name = html_file.filename
    if not html_name.lower().endswith((".html", ".htm", ".zip")):
        return jsonify(error="Website file must be .html, .htm or .zip."), 400
    html_bytes = html_file.read()
    if not html_bytes:
        return jsonify(error="The website file is empty."), 400
    if len(html_bytes) > MAX_HTML_SIZE:
        return jsonify(error="Website file is too large (max 10 MB)."), 400

    icon_bytes = icon_file.read()
    if len(icon_bytes) > MAX_ICON_SIZE:
        return jsonify(error="Icon is too large (max 6 MB)."), 400
    err = validate_icon(icon_bytes)
    if err:
        return jsonify(error=err), 400

    html_mime = "application/zip" if html_name.lower().endswith(".zip") else "text/html"
    icon_mime = "image/png" if icon_bytes[:4] == b"\x89PNG" else "image/jpeg"
    files = {
        "html": (html_name, html_bytes, html_mime),
        "icon": (icon_file.filename, icon_bytes, icon_mime),
    }

    if not _slots.acquire(blocking=False):
        return jsonify(error="Server is busy building other apps. Try again in a minute."), 503
    try:
        logger.info("Build started: %s (%s)", app_name, ip)
        resp = requests.post(API_URL, data={"app_name": app_name}, files=files, timeout=170)
    except requests.RequestException as exc:
        logger.exception("Build API request failed")
        return jsonify(error=f"Build service unavailable: {exc}"), 502
    finally:
        _slots.release()

    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code == 200 and "application/vnd.android.package-archive" in ctype:
        disp = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename=(?:"([^"]+)"|([^;]+))', disp)
        apk_name = (m.group(1) or m.group(2)).strip() if m else f"{app_name}.apk"
        if not apk_name.lower().endswith(".apk"):
            apk_name += ".apk"
        safe = re.sub(r'[^\w.\- ]', "_", apk_name, flags=re.UNICODE) or "app.apk"
        out = app.response_class(resp.content, mimetype="application/vnd.android.package-archive")
        out.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        out.headers["X-APK-Name"] = safe
        out.headers["Access-Control-Expose-Headers"] = "X-APK-Name, Content-Disposition"
        logger.info("Build complete: %s", safe)
        return out

    return jsonify(error=api_error_message(resp)), (resp.status_code if 400 <= resp.status_code < 600 else 502)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Upload too large. Total size limit is about 16 MB."), 413


def start_bot():
    token = os.environ.get("BOT_TOKEN", "")
    if not token or ":" not in token:
        logger.info("BOT_TOKEN not set — running website only.")
        return
    try:
        import main as tgbot  # starts nothing on import; handlers are registered
        try:
            tgbot.ensure_banner()
        except Exception:
            logger.warning("Banner could not be prepared", exc_info=True)

        def run():
            logger.info("Telegram bot polling started")
            tgbot.bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)

        threading.Thread(target=run, daemon=True).start()
    except SystemExit as exc:
        logger.error("Bot not started: %s", exc)
    except Exception:
        logger.exception("Bot failed to start — website keeps running")


if __name__ == "__main__":
    start_bot()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Website running on port %s", port)
    serve(app, host="0.0.0.0", port=port, threads=16, channel_timeout=300)
