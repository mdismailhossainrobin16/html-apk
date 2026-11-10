
import logging
import io
import os
import re
import threading
from html import escape

import requests
import telebot
from PIL import Image, ImageDraw, ImageFilter
from telebot import types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_URL = "https://app-creator-x.ueu.workers.dev/api/v1/build"
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apk_converter_banner.png")

MAX_HTML_SIZE = 10 * 1024 * 1024
MAX_ZIP_SIZE = 10 * 1024 * 1024
MAX_ICON_SIZE = 6 * 1024 * 1024
MAX_ICON_PIXELS = 4_000_000
MAX_TOTAL_BODY = 16_842_752
MAX_TOTAL_FILES = MAX_TOTAL_BODY - 64 * 1024
MAX_APP_NAME_UTF16 = 80
MAX_APP_NAME_UTF8 = 120

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("apk_converter_bot")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit("Set a valid BOT_TOKEN environment variable before running.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
sessions = {}


def ensure_banner():
    if os.path.exists(BANNER_PATH):
        return
    w, h = 1200, 675
    im = Image.new("RGB", (w, h), "#10162d")
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = (x / w + y / h) / 2
            px[x, y] = (int(15 + 18 * t), int(22 + 26 * t), int(53 + 55 * t))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((760, -160, 1320, 400), fill=(101, 88, 255, 95))
    gd.ellipse((-180, 410, 430, 1030), fill=(0, 207, 191, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    im = Image.alpha_composite(im.convert("RGBA"), glow)
    d = ImageDraw.Draw(im)
    phone = (435, 85, 765, 590)
    d.rounded_rectangle(phone, radius=42, fill="#202a4b", outline="#6976b6", width=7)
    d.rounded_rectangle((458, 132, 742, 535), radius=14, fill="#0c1329")
    d.rounded_rectangle((555, 101, 645, 119), radius=9, fill="#6976b6")
    d.ellipse((579, 551, 621, 593), fill="#6976b6")
    d.rounded_rectangle((535, 230, 665, 390), radius=22, fill="#665cff")
    d.polygon([(600, 198), (646, 240), (554, 240)], fill="#8d86ff")
    d.line((574, 219, 560, 200), fill="#8d86ff", width=8)
    d.line((626, 219, 640, 200), fill="#8d86ff", width=8)
    d.ellipse((568, 278, 584, 294), fill="white")
    d.ellipse((616, 278, 632, 294), fill="white")
    d.rounded_rectangle((570, 330, 630, 347), radius=8, fill="#b9b5ff")
    im.convert("RGB").save(BANNER_PATH, "PNG", optimize=True)


def kb_main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚀 Create APK", callback_data="menu:new"))
    kb.add(
        types.InlineKeyboardButton("📘 How it works", callback_data="menu:help"),
        types.InlineKeyboardButton("ℹ️ About", callback_data="menu:about"),
    )
    return kb


def kb_cancel():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data="flow:cancel"))
    return kb


def kb_back():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Back to menu", callback_data="menu:home"))
    return kb


def kb_confirm():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Build APK", callback_data="flow:build"),
        types.InlineKeyboardButton("✏️ Rename", callback_data="flow:rename"),
    )
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data="flow:cancel"))
    return kb


def progress(step, label):
    labels = ["App name", "Website", "Icon", "Review", "Build"]
    cells = ["✅" if i < step else ("🔵" if i == step else "⚪") for i in range(5)]
    return f"<b>Step {step + 1}/5 — {label}</b>\n{'  '.join(cells)}\n"


def reset(chat_id):
    sessions.pop(chat_id, None)


def safe_name(value):
    return escape(value or "")


def validate_app_name(value):
    """Match CreatorX's 1–80 UTF-16 / 120 UTF-8 name rules."""
    value = (value or "").strip()
    if not value:
        return "Please send a non-empty app name."
    utf16_units = len(value.encode("utf-16-le")) // 2
    utf8_bytes = len(value.encode("utf-8"))
    if utf16_units > MAX_APP_NAME_UTF16:
        return f"Name is too long ({utf16_units}/80 UTF-16 units)."
    if utf8_bytes > MAX_APP_NAME_UTF8:
        return f"Name is too large ({utf8_bytes}/120 UTF-8 bytes)."
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return "The app name cannot contain control characters."
    return None


def validate_icon(content):
    """Validate the actual image, not only its filename extension."""
    if not content:
        return "The icon file is empty."
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG"):
                return "Only PNG and JPEG icons are supported."
            width, height = image.size
            if width * height > MAX_ICON_PIXELS:
                return "The icon is too large: maximum is 4 megapixels."
            if image.format == "PNG" and image.info.get("interlace"):
                return "Interlaced PNG icons are not supported. Please export a regular PNG."
    except Exception:
        return "That file is not a valid PNG or JPEG image."
    return None


def send_home(chat_id, intro=True):
    text = (
        "<b>APK Converter Studio</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌐 Website → 📱 Android APK\n\n"
        "Convert an HTML website or ZIP project into a test APK in a few simple steps.\n\n"
        "<i>Fast • Simple • Test-build ready</i>"
        if intro else
        "<b>APK Converter Studio</b>\n\nChoose an action below to continue:"
    )
    if os.path.exists(BANNER_PATH):
        with open(BANNER_PATH, "rb") as photo:
            bot.send_photo(chat_id, photo, caption=text, reply_markup=kb_main_menu())
    else:
        bot.send_message(chat_id, text, reply_markup=kb_main_menu())


def edit_status(chat_id, message_id, text, markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    except Exception as exc:
        logger.debug("Could not edit status message: %s", exc)


def flow_screen(chat_id, text, markup=None, source=None):
    session = sessions.get(chat_id)
    if session is None:
        return None
    msg = source
    if msg is None:
        msg = bot.send_message(chat_id, text, reply_markup=markup)
    elif msg.content_type == "photo":
        bot.edit_message_caption(text, chat_id, msg.message_id, reply_markup=markup)
    else:
        bot.edit_message_text(text, chat_id, msg.message_id, reply_markup=markup)
    session["ui_message"] = msg
    return msg


def flow_next(chat_id, text, handler, *args, source=None, markup=None):
    msg = flow_screen(chat_id, text, markup or kb_cancel(), source=source)
    bot.register_next_step_handler(msg, handler, *args)
    return msg


def edit_ui_message(message, text, markup=None):
    try:
        if message.content_type == "photo":
            bot.edit_message_caption(text, message.chat.id, message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)
    except Exception as exc:
        logger.debug("UI status update failed: %s", exc)


def api_error_message(resp):
    """Turn CreatorX's documented errors into a friendly Telegram message."""
    try:
        payload = resp.json()
        detail = payload.get("error", resp.text)
    except ValueError:
        detail = resp.text or f"HTTP {resp.status_code}"
    status_help = {
        400: "Please check the app name and uploaded files.",
        413: "The complete request is too large. Try smaller files.",
        415: "The upload format was rejected. Please try again.",
        429: "The build service is rate-limited. Please wait a minute and retry.",
        500: "The build service failed unexpectedly. Please retry with smaller files.",
    }.get(resp.status_code, "Please try again later.")
    return f"{escape(str(detail))}\n\n<i>{status_help}</i>"


def animate_build(message, stop_event):
    frames = ["◐", "◓", "◑", "◒"]
    stages = [
        ("Uploading website", "📤", 25),
        ("Preparing WebView", "🧩", 45),
        ("Compiling Android package", "⚙️", 70),
        ("Signing test build", "🔐", 88),
        ("Finalizing APK", "📦", 96),
    ]
    index = 0
    while not stop_event.is_set():
        label, icon, percent = stages[index % len(stages)]
        filled = max(1, percent // 10)
        bar = "🟦" * filled + "▫️" * (10 - filled)
        text = (
            f"<b>{frames[index % len(frames)]} APK Converter Studio</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>BUILD IN PROGRESS</b>\n\n"
            f"{icon} <b>{label}</b>\n"
            f"{bar} <b>{percent}%</b>\n\n"
            "☕ Please wait while the build service works…\n"
            "<i>Your files are being processed securely.</i>"
        )
        edit_ui_message(message, text)
        index += 1
        stop_event.wait(1.5)


def prompt(chat_id, text, handler, status_message=None):
    msg = bot.send_message(chat_id, text, reply_markup=kb_cancel())
    bot.register_next_step_handler(msg, handler)
    return msg


HELP_TEXT = (
    "<b>📘 How it works</b>\n\n"
    "<b>1.</b> Choose <b>Create APK</b>\n"
    "<b>2.</b> Enter an app name (up to 80 UTF-16 units / 120 UTF-8 bytes)\n"
    "<b>3.</b> Upload <code>.html</code>, <code>.htm</code>, or a ZIP with <code>index.html</code> (up to 10 MiB)\n"
    "<b>4.</b> Upload a non-interlaced PNG/JPEG icon (up to 6 MiB, 4 MP)\n"
    "<b>5.</b> Review and build\n\n"
    "⚠️ Builds are for testing only. Each APK uses a new temporary signing key and cannot update an earlier build."
)

ABOUT_TEXT = (
    "<b>ℹ️ About APK Converter Studio</b>\n\n"
    "A clean Telegram interface around the APK CreatorX build API.\n\n"
    "<b>Supported:</b> HTML, HTM, ZIP, PNG, JPEG\n"
    "<b>Output:</b> Android 6.0+ test APK\n\n"
    "<i>Always test the generated APK on a real device.</i>"
)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    reset(message.chat.id)
    send_home(message.chat.id)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=kb_back())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    reset(message.chat.id)
    bot.send_message(message.chat.id, "✖️ <b>Cancelled</b>\n\nNothing was changed.", reply_markup=kb_main_menu())


@bot.message_handler(commands=["newapk"])
def cmd_newapk(message):
    start_flow(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:home")
def cb_home(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id)
    text = "<b>APK Converter Studio</b>\n\nChoose an action below to continue:"
    if call.message.content_type == "photo":
        bot.edit_message_caption(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb_main_menu(),
        )
    else:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb_main_menu(),
        )


@bot.callback_query_handler(func=lambda c: c.data == "menu:help")
def cb_help(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_caption(HELP_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back()) if call.message.content_type == "photo" else bot.edit_message_text(HELP_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:about")
def cb_about(call):
    bot.answer_callback_query(call.id)
    if call.message.content_type == "photo":
        bot.edit_message_caption(ABOUT_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())
    else:
        bot.edit_message_text(ABOUT_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:new")
def cb_new(call):
    bot.answer_callback_query(call.id)
    start_flow(call.message.chat.id, edit=call.message)


@bot.callback_query_handler(func=lambda c: c.data == "flow:cancel")
def cb_cancel(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id, "Cancelled")
    try:
        text = "✖️ <b>Cancelled</b>\n\nChoose an action below:"
        if call.message.content_type == "photo":
            bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=kb_main_menu())
        else:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb_main_menu())
    except Exception:
        bot.send_message(call.message.chat.id, "✖️ <b>Cancelled</b>", reply_markup=kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "flow:rename")
def cb_rename(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id)
    sessions[chat_id]["ui_message"] = call.message
    flow_next(chat_id, progress(0, "New app name") + "📛 <b>Send the new app name</b>\n\nYou can use up to 80 characters.", step_app_name, True)


@bot.callback_query_handler(func=lambda c: c.data == "flow:build")
def cb_build(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions or "icon" not in sessions[chat_id]:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id, "Build started")
    sessions[chat_id]["ui_message"] = call.message
    flow_screen(chat_id, progress(4, "Building") + "⚙️ <b>Preparing your APK…</b>\n\n<i>This may take up to two minutes.</i>", source=call.message)
    threading.Thread(target=build_apk, args=(chat_id,), daemon=True).start()


def start_flow(chat_id, edit=None):
    sessions[chat_id] = {}
    text = progress(0, "App name") + "📛 <b>What should your app be called?</b>\n\nSend a name from 1–80 characters."
    flow_next(chat_id, text, step_app_name, source=edit)


def step_app_name(message, rename=False):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    name = (message.text or "").strip()
    name_error = validate_app_name(name)
    if name_error:
        flow_next(chat_id, progress(0, "App name") + f"⚠️ <b>Invalid app name</b>\n\n{escape(name_error)}\n\nPlease try again:", step_app_name, rename)
        return
    sessions[chat_id]["app_name"] = name
    if rename:
        show_confirmation(chat_id)
        return
    flow_next(chat_id, progress(1, "Website") + f"✅ Name saved: <b>{safe_name(name)}</b>\n\n📄 <b>Upload your website</b>\nSend an <code>.html</code>, <code>.htm</code>, or <code>.zip</code> file.", step_html_file)


def step_html_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    if not message.document:
        flow_next(chat_id, progress(1, "Website") + "⚠️ <b>File required</b>\n\nUpload the website as a document file:", step_html_file)
        return
    doc = message.document
    filename = doc.file_name or "index.html"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("html", "htm", "zip"):
        flow_next(chat_id, progress(1, "Website") + "⚠️ <b>Unsupported file</b>\n\nSend only .html, .htm, or .zip:", step_html_file)
        return
    limit = MAX_ZIP_SIZE if ext == "zip" else MAX_HTML_SIZE
    if doc.file_size and doc.file_size > limit:
        flow_next(chat_id, progress(1, "Website") + f"⚠️ <b>File is too large</b>\n\nMaximum for .{ext} is {limit:,} bytes. Send a smaller file:", step_html_file)
        return
    try:
        info = bot.get_file(doc.file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("Website download failed")
        flow_next(chat_id, progress(1, "Website") + f"⚠️ <b>Download failed</b>\n\n{escape(str(exc))}\n\nPlease try again:", step_html_file)
        return
    sessions[chat_id]["html"] = (filename, content)
    flow_next(chat_id, progress(2, "Icon") + f"✅ Website received: <code>{escape(filename)}</code>\n\n🖼️ <b>Upload your app icon</b>\nSend a non-interlaced PNG or JPEG (max 6 MiB, 4 megapixels).", step_icon_file)


def step_icon_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    filename = "icon.jpg"
    if message.document:
        doc = message.document
        filename = doc.file_name or "icon.png"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("png", "jpg", "jpeg"):
            flow_next(chat_id, progress(2, "Icon") + "⚠️ <b>Unsupported icon</b>\n\nPlease send a PNG or JPEG:", step_icon_file)
            return
        if doc.file_size and doc.file_size > MAX_ICON_SIZE:
            flow_next(chat_id, progress(2, "Icon") + "⚠️ <b>Icon is too large</b>\n\nMaximum size is 6 MiB. Send a smaller icon:", step_icon_file)
            return
        file_id = doc.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    else:
        flow_next(chat_id, progress(2, "Icon") + "⚠️ <b>Icon required</b>\n\nPlease send a PNG or JPEG image:", step_icon_file)
        return
    try:
        info = bot.get_file(file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("Icon download failed")
        flow_next(chat_id, progress(2, "Icon") + f"⚠️ <b>Icon download failed</b>\n\n{escape(str(exc))}\n\nPlease try again:", step_icon_file)
        return
    if len(content) > MAX_ICON_SIZE:
        flow_next(chat_id, progress(2, "Icon") + "⚠️ <b>Icon is too large</b>\n\nMaximum size is 6 MiB. Send a smaller icon:", step_icon_file)
        return
    icon_error = validate_icon(content)
    if icon_error:
        flow_next(chat_id, progress(2, "Icon") + f"⚠️ <b>Invalid icon</b>\n\n{escape(icon_error)}\n\nPlease send another PNG or JPEG:", step_icon_file)
        return
    html_size = len(sessions[chat_id]["html"][1])
    if html_size + len(content) > MAX_TOTAL_FILES:
        reset(chat_id)
        bot.send_message(chat_id, "⚠️ <b>Files are too large together</b>\n\nWebsite + icon exceed the API request limit. Start again with smaller files.", reply_markup=kb_main_menu())
        return
    sessions[chat_id]["icon"] = (filename, content)
    show_confirmation(chat_id)


def show_confirmation(chat_id):
    session = sessions[chat_id]
    html_name, html_bytes = session["html"]
    icon_name, icon_bytes = session["icon"]
    text = (
        progress(3, "Review") + "<b>📋 Ready to build</b>\n\n"
        f"📛 <b>Name:</b> {safe_name(session['app_name'])}\n"
        f"📄 <b>Website:</b> <code>{escape(html_name)}</code> ({len(html_bytes):,} bytes)\n"
        f"🖼️ <b>Icon:</b> <code>{escape(icon_name)}</code> ({len(icon_bytes):,} bytes)\n\n"
        "Everything looks good. Start the build?"
    )
    flow_screen(chat_id, text, kb_confirm())


def build_apk(chat_id):
    session = sessions.get(chat_id)
    if not session:
        return
    ui_message = session.get("ui_message")
    stop_event = threading.Event()
    animation_thread = threading.Thread(target=animate_build, args=(ui_message, stop_event), daemon=True) if ui_message else None
    if animation_thread:
        animation_thread.start()
    app_name = session["app_name"]
    html_name, html_bytes = session["html"]
    icon_name, icon_bytes = session["icon"]
    html_mime = "application/zip" if html_name.lower().endswith(".zip") else "text/html"
    icon_mime = "image/png" if icon_name.lower().endswith("png") else "image/jpeg"
    files = {"html": (html_name, html_bytes, html_mime), "icon": (icon_name, icon_bytes, icon_mime)}
    try:
        resp = requests.post(API_URL, data={"app_name": app_name}, files=files, timeout=120)
        stop_event.set()
        if animation_thread:
            animation_thread.join(timeout=2)
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "application/vnd.android.package-archive" in content_type:
            edit_ui_message(ui_message, "<b>✅ APK BUILD COMPLETE</b>\n\n📦 Your APK is ready. Sending the file now…") if ui_message else None
            disposition = resp.headers.get("Content-Disposition", "")
            match = re.search(r"filename=(?:\"([^\"]+)\"|([^;]+))", disposition)
            apk_name = (match.group(1) or match.group(2)).strip() if match else f"{app_name}.apk"
            if not apk_name.lower().endswith(".apk"):
                apk_name += ".apk"
            bot.send_document(chat_id, (apk_name, resp.content), caption=f"🎉 <b>Build complete!</b>\n\n✅ <code>{escape(apk_name)}</code> is ready.\n\n⚠️ Test build only — this APK uses a temporary signing key and cannot update an earlier build.", reply_markup=kb_main_menu())
        else:
            if ui_message:
                edit_ui_message(ui_message, f"❌ <b>BUILD FAILED</b> ({resp.status_code})\n\n{api_error_message(resp)}", kb_main_menu())
            else:
                bot.send_message(chat_id, f"❌ <b>Build failed</b> ({resp.status_code})\n\n{api_error_message(resp)}", reply_markup=kb_main_menu())
    except requests.RequestException as exc:
        logger.exception("Build API request failed")
        stop_event.set()
        if animation_thread:
            animation_thread.join(timeout=2)
        if ui_message:
            edit_ui_message(ui_message, f"❌ <b>BUILD SERVICE UNAVAILABLE</b>\n\n{escape(str(exc))}\n\nPlease try again later.", kb_main_menu())
        else:
            bot.send_message(chat_id, f"❌ <b>Service unavailable</b>\n\n{escape(str(exc))}", reply_markup=kb_main_menu())
    finally:
        stop_event.set()
        reset(chat_id)


@bot.message_handler(func=lambda m: True, content_types=["text", "document", "photo"])
def fallback(message):
    bot.send_message(message.chat.id, "Choose an option below, or use /cancel to reset:", reply_markup=kb_main_menu())


if __name__ == "__main__":
    ensure_banner()
    logger.info("APK Converter Studio starting…")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
