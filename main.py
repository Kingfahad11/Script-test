import os
import re
import io
import ast
import sys
import json
import time
import html
import shutil
import psutil
import zipfile
import threading
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types

# 📡 Channel Backup: নতুন/রিসেট সার্ভারে লোকাল DB না থাকলে প্রাইভেট চ্যানেল থেকে ডাটা ফিরিয়ে আনে।
# অবশ্যই `import admin`-এর আগে — কারণ admin ইমপোর্ট হলেই খালি DB তৈরি হয়ে যায়।
import channel_backup
import code_audit
import code_doctor

# রিস্টোরের জন্য টোকেন ও চ্যানেল আইডি আগেই লাগে (ENV-তে থাকলে ENV অগ্রাধিকার পায়)
try:    # হোস্টে `.env` ফাইল থাকলে (BOT_TOKEN, DB_CHANNEL_ID, ADMIN_IDS ...) সেখান থেকে লোড হবে
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

def _http_port() -> int:
    """হেলথ-পোর্ট: হোস্ট যে ENV দেয় (PORT / SERVER_PORT), না থাকলে 10000। পোর্ট না মিললে হেলথ সার্ভার চুপচাপ বন্ধ থাকে — বট চলে।"""
    for k in ("PORT", "SERVER_PORT"):
        v = os.environ.get(k, "").strip()
        if v.isdigit() and 0 < int(v) < 65536:
            return int(v)
    return 10000

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8993873874:AAFh_nG9r5LtcVIkRh58D5eWG_BHEi_lPzk")
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "-1003995141877"))

# 📡 সার্ভার রিসেটে লোকাল DB না থাকলে প্রাইভেট চ্যানেলের PIN করা ব্যাকআপ থেকে DB ফেরত আনে
# (ইউজারের কোড-ফাইল পরে restore_missing_project_files() ফেরত আনে)
# 🛡 রিস্টোর (নেটওয়ার্ক রিট্রাইসহ কয়েক মিনিট লাগতে পারে) শুরুর আগেই হেলথ-পোর্ট খুলে রাখা হয়, যাতে হোস্ট "পোর্ট খোলেনি" ভেবে সার্ভিস রিস্টার্ট না করে।
class _EarlyHealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
    def do_GET(self):
        self.do_HEAD()
        self.wfile.write(b"PyHost Cloud 24/7 Multi-Bot Hosting Engine is Live & Healthy!")
    def log_message(self, format, *args):
        return

def _early_health_server():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", _http_port()), _EarlyHealthHandler)
        srv.daemon_threads = True
        srv.serve_forever()
    except Exception:
        pass

threading.Thread(target=_early_health_server, daemon=True).start()

CH_RESTORED = channel_backup.restore_db_if_needed(BOT_TOKEN, DB_CHANNEL_ID)

import admin

# ======================== টেলিগ্রাম বট ইনিশিয়ালাইজেশন ========================
# সুপারফাস্ট মাল্টি-থ্রেডিং ও নো-ল্যাগ হ্যান্ডলিং (15 Workers)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=15)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "user_projects")
os.makedirs(PROJECTS_DIR, exist_ok=True)

# ======================== 📡 Database Channel ========================
# ইউজার যে কোড/ফাইল আপলোড করে রান করে তার কপি আর অ্যাডমিনের বট-চ্যাটে যাবে না —
# সরাসরি এই চ্যানেলে জমা হবে। (বটকে অবশ্যই চ্যানেলের Admin বানাতে হবে — "Post Messages" পারমিশনসহ)
# চাইলে Render/হোস্টের ENV-এ DB_CHANNEL_ID দিয়ে চ্যানেল বদলানো যাবে।
def send_code_copy_to_channel(data: bytes, file_name: str, caption: str, card: str = None):
    """ইউজারের কোড/ZIP-এর কপি Database চ্যানেলে পাঠায় (ছোট ক্যাপশনসহ), তারপর বিস্তারিত অডিট কার্ডটা
    ওই ডকুমেন্টের রিপ্লাই হিসেবে পাঠায় (ক্যাপশন-সীমা ১০২৪, মেসেজ-সীমা ৪০৯৬)। সফল হলে ডকুমেন্ট-মেসেজ, নইলে False।
    রেট-লিমিট (429) এলে একবার অপেক্ষা করে আবার চেষ্টা করে; ব্যর্থ হলে কনসোলে কারণ প্রিন্ট করে
    (ইউজারের চ্যাটে কিছুই দেখানো হয় না)।"""
    sent_msg = None
    _card_as_caption = False
    # কার্ড (ট্যাগ বাদে) ১০২৪ অক্ষরের মধ্যে হলে ফাইলের ক্যাপশন হিসেবেই যায় — ফাইল ও কার্ড একই মেসেজে।
    # বড় হলে (যেমন ZIP/থ্রেট লিস্ট) আগের মতো ফাইলের রিপ্লাইতে আলাদা মেসেজ।
    if card:
        _plain = html.unescape(re.sub(r"<[^>]+>", "", card))
        if len(_plain) <= 1024:
            caption, card, _card_as_caption = card, None, True
    for attempt in range(2):
        try:
            bio = io.BytesIO(data)
            bio.name = file_name
            sent_msg = bot.send_document(DB_CHANNEL_ID, bio, caption=caption if _card_as_caption else caption[:1000], parse_mode="HTML")
            break
        except Exception as e:
            err = str(e)
            if attempt == 0 and ("429" in err or "Too Many Requests" in err):
                m = re.search(r"retry after (\d+)", err)
                time.sleep(min(int(m.group(1)) if m else 3, 30) + 1)
                continue
            print(f"[DB-CHANNEL] কপি পাঠানো যায়নি ({DB_CHANNEL_ID}): {err[:200]}")
            return False
    if not sent_msg:
        return False
    if card:
        for attempt in range(2):
            try:
                _cm = bot.send_message(DB_CHANNEL_ID, card, parse_mode="HTML", reply_to_message_id=sent_msg.message_id,
                                       disable_web_page_preview=True)
                try:
                    sent_msg._card_msg_id = _cm.message_id     # 🧹 Channel Clean-এ কার্ডসহ মুছতে
                except Exception:
                    pass
                break
            except Exception as e:
                err = str(e)
                if attempt == 0 and ("429" in err or "Too Many Requests" in err):
                    time.sleep(4)
                    continue
                print(f"[DB-CHANNEL] অডিট কার্ড পাঠানো যায়নি: {err[:160]}")
                break
    return sent_msg

def free_hosting_blocked_message() -> str:
    """🆓 ফ্রি হোস্টিং অ্যাডমিন থেকে বন্ধ থাকলে ফ্রি-প্ল্যান ইউজারদের এই মেসেজ দেখানো হয়।
    পেইড প্ল্যান/24-7-কভার্ড স্লট এতে প্রভাবিত হয় না।"""
    hours = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
    return (
        f"🔒 <b>ফ্রি হোস্টিং ({hours} ঘণ্টা) সাময়িকভাবে বন্ধ আছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "সার্ভার মেইনটেনেন্স/লোডের কারণে অ্যাডমিন ফ্রি সেশন সাময়িকভাবে বন্ধ রেখেছেন।\n\n"
        "👉 এখনই বট চালাতে <b>💎 Upgrade Plan</b> থেকে যেকোনো প্ল্যান নিন (24/7 আনলিমিটেড), "
        "অথবা কিছুক্ষণ পর আবার চেষ্টা করুন।"
    )

def queue_user_code_copy(from_user, project_id: int, file_name: str, data: bytes, source: str = "File Upload (.py)"):
    """ব্যাকগ্রাউন্ড থ্রেডে ইউজারের কোডের কপি + একটা বিস্তারিত অডিট কার্ড Database চ্যানেলে পাঠায়
    (ডেপ্লয়মেন্ট ধীর করে না)। বট টোকেন কোডে থাকলে অটো-ডিটেক্ট ও মাস্ক করে দেখায়, আর হালকা
    হিউরিস্টিক সিকিউরিটি স্ক্যানও চালায়।"""
    user_id = from_user.id
    username = getattr(from_user, "username", None)
    full_name = (getattr(from_user, "first_name", "") or "").strip()
    if getattr(from_user, "last_name", None):
        full_name = (full_name + " " + from_user.last_name).strip()
    full_name = full_name or "Unknown"

    def _worker():
        try:
            proj = admin.get_project_by_id(project_id)
            proj_name = proj["project_name"] if proj else f"Bot #{project_id}"
            slot_no = proj.get("slot_number") if proj else None
            slot_label = f"Slot #{slot_no}" if slot_no else "Unassigned"

            u_data = admin.get_user(user_id)
            plan = u_data.get("plan", "FREE").upper()
            if plan == "FREE":
                plan_status = f"{admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)}h/Session"
            else:
                plan_status = "24/7"

            info = code_audit.analyze(file_name, data, extract_pkgs=_extract_packages_from_code)
            bot_user, bot_live = None, None
            if info["token"]:
                bot_live, uname_or_err = validate_bot_token(info["token"])
                bot_user = uname_or_err if bot_live else "Invalid token"
            plan_line = (f"{plan} • {admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)}H Session" if plan == "FREE" else f"{plan} • 24/7")
            card = code_audit.build_card(info, {
                "source": source, "name": full_name, "user_id": user_id, "username": username,
                "plan_line": plan_line, "project": proj_name, "slot": f"#{slot_no}" if slot_no else "Unassigned",
                "bot_user": bot_user, "bot_live": bot_live,
            })
            caption = (f"📥 <b>{html.escape(file_name)}</b> • {len(data) / 1024:.1f} KB\n"
                       f"👤 {html.escape(full_name)} • <code>{user_id}</code>" + (f" • @{html.escape(username)}" if username else "") + "\n"
                       f"📦 {html.escape(proj_name)}")
            sent = send_code_copy_to_channel(data, file_name, caption, card)
            # 📡 ইনডেক্স রাখা: রিসেটের পর এই file_id দিয়েই কোড ফেরত আনা হবে
            if sent and getattr(sent, "document", None):
                admin.add_code_backup(user_id, project_id, proj_name, file_name, len(data),
                                      sent.document.file_id, sent.message_id, source,
                                      card_msg_id=getattr(sent, "_card_msg_id", None))
                channel_backup.schedule_push(bot, DB_CHANNEL_ID, delay=30)
        except Exception as e:
            print(f"[DB-CHANNEL] worker error: {str(e)[:200]}")
    threading.Thread(target=_worker, daemon=True).start()

# প্রসেস ও মেমোরি স্টেট
active_processes = {}
user_states = {}
installed_packages_cache = set()
_PIP_LOCK = threading.RLock()   # 🛡 একসাথে অনেকগুলো pip চললে RAM/CPU ফেটে যায় (সার্ভার রিস্টার্টে ১১টা বট একসাথে) — তাই সব pip একটার পর একটা
upload_batches = {}   # user_id -> মাল্টি-ফাইল আপলোড ব্যাচ (একাধিক ফাইল একটাই কার্ডে দেখানোর জন্য)
upload_batch_lock = threading.RLock()
upload_target_hint = {}   # user_id -> {"pid", "ts"}: একসাথে/পরপর আসা ফাইলগুলো যেন একই বটে যায়
UPLOAD_EXPLICIT_TTL = 1800   # স্লট/Update Code চাপার পর ৩০ মিনিট পর্যন্ত সেই বটই টার্গেট থাকবে
UPLOAD_HINT_TTL = 45         # পরপর আসা ফাইলের মধ্যে সর্বোচ্চ গ্যাপ (সেকেন্ড)
UPLOAD_BATCH_WINDOW = 2   # সেকেন্ড — এই সময়ের মধ্যে নতুন ফাইল না এলে অটো ডিপ্লয় শুরু হবে
# ⭐ NEW: "🔴 Live Mode" লগ ভিউয়ের জন্য একটিভ সেশন ট্র্যাক করে (chat_id_message_id -> {"stop": bool})
live_log_sessions = {}

# ----------------- Rate Limiter (স্প্যাম প্রতিরোধক) -----------------
# প্রতি ইউজারের শেষ মেসেজের সময় ট্র্যাক করে — MAX_MSG_PER_SEC সেকেন্ডে ১টার বেশি
# মেসেজ এলে সাইলেন্টলি ইগনোর করা হয় (ইউজারকে কোনো এরর দেখানো হয় না)
_rate_limit_store: dict = {}   # {user_id: last_message_timestamp}
MAX_MSG_INTERVAL_SEC = 1.2     # ইউজার প্রতি মিনিমাম গ্যাপ (সেকেন্ড)

def check_rate_limit(user_id: int) -> bool:
    """True হলে মেসেজটি প্রসেস করা যাবে, False হলে রেটলিমিটেড — বাদ দেওয়া হবে।"""
    now = time.monotonic()
    last = _rate_limit_store.get(user_id, 0)
    if now - last < MAX_MSG_INTERVAL_SEC:
        return False
    _rate_limit_store[user_id] = now
    return True

# ----------------- বট টোকেন ভ্যালিডেটর -----------------
def validate_bot_token(token: str, timeout: int = 10) -> tuple:
    """Telegram getMe API-তে কল করে টোকেনটি সত্যিই কাজ করে কিনা যাচাই করে।
    রিটার্ন করে (is_valid: bool, bot_username: str | error_msg: str)।
    ডিপ্লয়ের আগে এটা করা হয় যাতে ভুল টোকেন দিয়ে অকারণে স্লট নষ্ট না হয়।
    timeout ছোট রাখলে (যেমন ডিপ্লয় সাকসেস কার্ডে বট হ্যান্ডল দেখানোর সময়) ধীর নেটওয়ার্কে
    ইউজারকে বেশিক্ষণ অপেক্ষা করাতে হয় না।"""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getMe",
            headers={"User-Agent": "PyHostCloud"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            uname = data["result"].get("username", "unknown")
            return True, f"@{uname}"
        return False, "Telegram API ভুল ফলাফল দিয়েছে।"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "টোকেনটি ভুল বা বাতিল হয়ে গেছে (401 Unauthorized)।"
        return False, f"HTTP {e.code} এরর।"
    except Exception as ex:
        return False, f"নেটওয়ার্ক সমস্যা: {str(ex)[:100]}"


PIP_ALIASES = {
    # আগের গুলো
    "telebot": "pyTelegramBotAPI", "bs4": "beautifulsoup4", "PIL": "Pillow", "cv2": "opencv-python-headless",
    "dotenv": "python-dotenv", "telegram": "python-telegram-bot", "yaml": "PyYAML", "sklearn": "scikit-learn",
    "discord": "discord.py", "dateutil": "python-dateutil", "jose": "python-jose", "jwt": "PyJWT",
    "dns": "dnspython", "magic": "python-magic", "nmap": "python-nmap",
    # ⭐ নতুন: ইম্পোর্টের নাম ≠ pip প্যাকেজের নাম — এগুলো না থাকলে ভুল/অস্তিত্বহীন প্যাকেজ ইনস্টল হয়ে বট ক্র্যাশ করত
    "Crypto": "pycryptodome", "Cryptodome": "pycryptodomex", "OpenSSL": "pyOpenSSL", "serial": "pyserial", "usb": "pyusb",
    "fitz": "PyMuPDF", "docx": "python-docx", "pptx": "python-pptx", "attr": "attrs", "skimage": "scikit-image",
    "socks": "PySocks", "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "zmq": "pyzmq", "bson": "pymongo",
    "gridfs": "pymongo", "markdown": "Markdown", "faker": "Faker", "slugify": "python-slugify", "decouple": "python-decouple",
    "speech_recognition": "SpeechRecognition", "googleapiclient": "google-api-python-client", "google_auth_oauthlib": "google-auth-oauthlib",
    "flask_cors": "flask-cors", "flask_sqlalchemy": "Flask-SQLAlchemy", "flask_login": "Flask-Login", "flask_wtf": "Flask-WTF",
    "flask_migrate": "Flask-Migrate", "flask_socketio": "Flask-SocketIO", "socketio": "python-socketio", "engineio": "python-engineio",
    "binance": "python-binance", "kucoin": "python-kucoin", "bitcoinlib": "bitcoinlib", "ccxt": "ccxt", "tweepy": "tweepy",
    "pymysql": "PyMySQL", "mysql": "mysql-connector-python", "pyrogram": "pyrogram", "pyaes": "pyaes", "tgcrypto": "TgCrypto",
    "telethon": "Telethon", "aiogram": "aiogram", "pytz": "pytz", "tzlocal": "tzlocal", "ujson": "ujson", "orjson": "orjson",
    "lxml": "lxml", "html5lib": "html5lib", "chardet": "chardet", "regex": "regex", "emoji": "emoji", "qrcode": "qrcode[pil]",
    "pyzbar": "pyzbar", "gtts": "gTTS", "pydub": "pydub", "moviepy": "moviepy", "imageio": "imageio", "yt_dlp": "yt-dlp",
    "youtube_dl": "youtube_dl", "pytube": "pytube", "instaloader": "instaloader", "praw": "praw", "pymongo": "pymongo",
    "motor": "motor", "redis": "redis", "aioredis": "aioredis", "sqlalchemy": "SQLAlchemy", "peewee": "peewee",
    "tortoise": "tortoise-orm", "pydantic": "pydantic", "fastapi": "fastapi", "uvicorn": "uvicorn", "starlette": "starlette",
    "flask": "Flask", "jinja2": "Jinja2", "werkzeug": "Werkzeug", "websockets": "websockets", "websocket": "websocket-client",
    "httpx": "httpx", "aiohttp": "aiohttp", "aiofiles": "aiofiles", "requests_html": "requests-html", "cloudscraper": "cloudscraper",
    "selenium": "selenium", "playwright": "playwright", "pyppeteer": "pyppeteer", "openpyxl": "openpyxl", "xlrd": "xlrd",
    "xlsxwriter": "XlsxWriter", "reportlab": "reportlab", "PyPDF2": "PyPDF2", "pypdf": "pypdf", "tabulate": "tabulate",
    "colorama": "colorama", "termcolor": "termcolor", "rich": "rich", "tqdm": "tqdm", "click": "click", "schedule": "schedule",
    "apscheduler": "APScheduler", "croniter": "croniter", "humanize": "humanize", "psutil": "psutil", "pyfiglet": "pyfiglet",
    "numpy": "numpy", "pandas": "pandas", "scipy": "scipy", "matplotlib": "matplotlib", "seaborn": "seaborn", "plotly": "plotly",
    "openai": "openai", "anthropic": "anthropic", "groq": "groq", "cohere": "cohere", "g4f": "g4f", "translate": "translate",
    "googletrans": "googletrans==4.0.0rc1", "deep_translator": "deep-translator", "wikipedia": "wikipedia", "pyshorteners": "pyshorteners",
    "whois": "python-whois", "geopy": "geopy", "phonenumbers": "phonenumbers", "pyjokes": "pyjokes", "wget": "wget",
    "gspread": "gspread", "oauth2client": "oauth2client", "firebase_admin": "firebase-admin", "pyrebase": "pyrebase4",
    "boto3": "boto3", "botocore": "botocore", "paramiko": "paramiko", "pexpect": "pexpect", "docker": "docker",
    "cachetools": "cachetools", "async_timeout": "async-timeout", "nest_asyncio": "nest-asyncio", "uvloop": "uvloop",
}

# google.* নেমস্পেস — রুট নাম 'google' দিয়ে প্যাকেজ বোঝা যায় না, তাই পুরো ডটেড নাম দেখে ঠিক করা হয়
GOOGLE_PIP = {
    "google.generativeai": "google-generativeai", "google.genai": "google-genai", "google.protobuf": "protobuf",
    "google.auth": "google-auth", "google.oauth2": "google-auth", "google.cloud.firestore": "google-cloud-firestore",
    "google.cloud.storage": "google-cloud-storage", "google.cloud.vision": "google-cloud-vision",
    "google.cloud.translate": "google-cloud-translate", "google.cloud.texttospeech": "google-cloud-texttospeech",
    "google.cloud.speech": "google-cloud-speech", "google.cloud.bigquery": "google-cloud-bigquery",
}

# সাধারণত ইউজারের নিজের ফাইল (config.py, utils.py...) — ফাইলটা আপলোড না থাকলে PyPI-র অন্য একটা প্যাকেজ ভুল করে ইনস্টল না করার জন্য
LIKELY_LOCAL_MODULES = {
    "config", "configs", "settings", "utils", "util", "helpers", "helper", "constants", "const", "keyboards", "keyboard", "states",
    "handlers", "database", "db", "models", "localization", "locales", "messages", "texts", "strings", "functions", "keep_alive",
    "keepalive", "bot_config", "cfg", "data", "core", "common", "tools", "middlewares", "filters", "services", "storage",
}
# Windows-only — Linux সার্ভারে ইনস্টল হয় না; চেষ্টা করে সময় নষ্ট করার দরকার নেই
WINDOWS_ONLY_MODULES = {"win32api", "win32con", "win32gui", "win32com", "win32file", "win32process", "win32clipboard", "pythoncom", "pywintypes", "wmi", "winshell", "winsound", "msvcrt", "_winreg"}

def resolve_pip_name(module_name: str) -> str:
    """ইম্পোর্টের নাম (ডটেড হতে পারে) → pip প্যাকেজের নাম।"""
    if module_name in GOOGLE_PIP:
        return GOOGLE_PIP[module_name]
    parts = module_name.split(".")
    if parts[0] == "google":
        for n in (3, 2):
            key = ".".join(parts[:n])
            if key in GOOGLE_PIP:
                return GOOGLE_PIP[key]
        if len(parts) >= 3 and parts[1] == "cloud":
            return "google-cloud-" + parts[2].replace("_", "-")
        if len(parts) >= 2:
            return "google-" + parts[1].replace("_", "-")
    return PIP_ALIASES.get(parts[0], parts[0])

# ----------------- Render Health Web Server & Self Keep-Alive -----------------
class RenderHealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"PyHost Cloud 24/7 Multi-Bot Hosting Engine is Live & Healthy!")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = _http_port()
    try:
        # ThreadingHTTPServer: একসাথে একাধিক রিকোয়েস্ট (health-ping) আটকে না যাওয়ার জন্য
        server = ThreadingHTTPServer(("0.0.0.0", port), RenderHealthHandler)
        server.daemon_threads = True
        server.serve_forever()
    except Exception:
        pass

threading.Thread(target=start_health_server, daemon=True).start()

# সেলফ-পিং কিপ অ্যালাইভ (সার্ভার স্লিপ মোড প্রতিরোধক)
def self_keep_alive_pinger():
    port = _http_port()
    url = f"http://127.0.0.1:{port}/"
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass

threading.Thread(target=self_keep_alive_pinger, daemon=True).start()

# ----------------- ফাস্ট ডিপেন্ডেন্সি স্ক্যানার ও সিঙ্ক ইনস্টলার -----------------
def _extract_packages_from_code(code: str, local_names=None) -> list:
    """কোড টেক্সট থেকে ইম্পোর্ট করা থার্ড-পার্টি প্যাকেজ বের করে (AST দিয়ে, ব্যর্থ হলে regex fallback)।
    scan_python_imports (ফাইল-ভিত্তিক) ও queue_user_code_copy (মেমোরি-ভিত্তিক) দুটোই এটা শেয়ার করে।"""
    detected = set()      # মডিউলের পুরো (ডটেড) নাম — google.* চেনার জন্য
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    detected.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                # ⭐ FIX: `from .x import y` (রিলেটিভ ইম্পোর্ট) আগে ভুল করে PyPI প্যাকেজ 'x' ধরা হতো
                if node.module and not node.level:
                    if node.module in ("google", "google.cloud"):
                        # `from google.cloud import firestore` → google.cloud.firestore (নাহলে 'google-cloud' নামের ভুল প্যাকেজ ধরা পড়ত)
                        for alias in node.names:
                            detected.add(f"{node.module}.{alias.name}")
                    else:
                        detected.add(node.module)
    except Exception:
        raw_imports = re.findall(r"^(?:from|import)\s+([a-zA-Z0-9_\.]+)", code, re.MULTILINE)
        detected.update(i for i in raw_imports if not i.startswith("."))

    # ⭐ FIX: আগে মাত্র ~২৫টি stdlib মডিউল বাদ যেত। ফলে string/uuid/queue/csv/html/copy/glob ইত্যাদি
    # এবং ইউজারের নিজের ফাইল (config.py, utils.py...) ভুলে PyPI-এর অন্য প্যাকেজ হিসেবে pip install
    # হতো — এতে ডিপ্লয় ধীর হতো, ভুল/পুরনো প্যাকেজ বসে বট ক্র্যাশ করত। এখন পূর্ণ stdlib লিস্ট
    # (sys.stdlib_module_names) এবং স্ক্রিপ্টের পাশের লোকাল .py ফাইল/ফোল্ডার বাদ দেওয়া হয়।
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__", "__main__"}
    skip = stdlib | set(local_names or ())

    packages = []
    for full in detected:
        root = full.split(".")[0]
        if root in skip or root in WINDOWS_ONLY_MODULES or not root:
            continue
        # ফাইল আপলোডে না থাকলেও config/utils/db-জাতীয় নাম PyPI থেকে ইনস্টল করা হয় না (ভুল প্যাকেজ বসে যায়)
        if root.lower() in LIKELY_LOCAL_MODULES:
            continue
        packages.append(resolve_pip_name(full))

    return sorted(list(set(packages)))

def scan_python_imports(file_path):
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        code = f.read()
    local_names = set()
    try:
        base_dir = os.path.dirname(os.path.abspath(file_path))
        for entry in os.listdir(base_dir):
            full = os.path.join(base_dir, entry)
            if entry.endswith(".py"):
                local_names.add(entry[:-3])
            elif os.path.isdir(full):
                local_names.add(entry)
    except Exception:
        pass
    return _extract_packages_from_code(code, local_names)

def extract_bot_token_from_code(data: bytes):
    """আপলোড করা কোডের ভেতর থেকে বট টোকেন প্যাটার্ন (যদি সরাসরি কোডে লেখা থাকে) খুঁজে বের করে।"""
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r'\d{8,10}:[a-zA-Z0-9_\-]{35}', text)
    return m.group(0) if m else None

def mask_bot_token(token: str) -> str:
    """চ্যানেলে সম্পূর্ণ টোকেন না দেখিয়ে মাস্ক করে দেখায় (নিরাপত্তার জন্য)।"""
    if ":" not in token:
        return (token[:6] + "...") if len(token) > 6 else token
    bot_id, secret = token.split(":", 1)
    if len(secret) <= 6:
        return f"{bot_id}:{secret}"
    return f"{bot_id}:{secret[:3]}...{secret[-3:]}"

def scan_code_security(data: bytes) -> str:
    """হালকা হিউরিস্টিক স্ক্যান — সত্যিকারের ম্যালওয়্যার ডিটেকশন নয়, শুধু কিছু সন্দেহজনক প্যাটার্ন
    থাকলে অ্যাডমিনকে flag করে দেখায় যাতে ম্যানুয়ালি রিভিউ করা সহজ হয়।"""
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return "⚠️ Unreadable"
    suspicious_patterns = [
        r'os\.system\s*\(',
        r'subprocess\.(Popen|call|run)\s*\([^)]{0,120}shell\s*=\s*True',
        r'\beval\s*\(',
        r'\bexec\s*\(',
        r'rm\s+-rf\s+/',
        r'base64\.b64decode\s*\(',
    ]
    flagged = sum(1 for p in suspicious_patterns if re.search(p, text))
    if flagged:
        return f"⚠️ Flagged ({flagged} suspicious pattern{'s' if flagged > 1 else ''})"
    return "✅ Clean (No Threat)"

def sanitize_upload_filename(file_name: str) -> str:
    """
    টেলিগ্রাম থেকে আসা ফাইলের নাম স্যানিটাইজ করে — শুধু বেসনেইম রাখা হয় যাতে কেউ
    ফাইলের নামে "../../" জাতীয় কিছু ঢুকিয়ে প্রজেক্ট ফোল্ডারের বাইরে লিখতে না পারে
    (Path Traversal প্রতিরোধ)। ".env"-এর মতো ডটফাইল নাম ঠিক রেখেই এটা করা হয়।
    """
    raw = (file_name or "").replace("\\", "/")
    safe = os.path.basename(raw).strip()
    if not safe or safe in (".", ".."):
        safe = f"upload_{int(time.time())}.dat"
    return safe

_ZIP_SKIP_DIRS = {"__MACOSX", "__pycache__", ".git", "venv", ".venv", "env", "site-packages", "node_modules"}
ZIP_MAX_TOTAL_BYTES = 200 * 1024 * 1024
ZIP_MAX_FILES = 3000

def _zip_parts(name: str) -> list:
    """ZIP এন্ট্রির নামকে পাথ-কম্পোনেন্টের লিস্টে ভাঙে (উইন্ডোজ \\ ও খালি/'.' অংশ বাদ)।"""
    return [p for p in name.replace("\\", "/").split("/") if p and p != "."]

def _zip_is_junk(name: str) -> bool:
    """macOS/উইন্ডোজের অপ্রয়োজনীয় ফাইল (__MACOSX, ._xxx, .DS_Store, Thumbs.db) স্কিপ করার জন্য।"""
    parts = _zip_parts(name)
    if not parts or parts[0] == "__MACOSX":
        return True
    base = parts[-1]
    return base in (".DS_Store", "Thumbs.db") or base.startswith("._")

def _zip_plan_root(infos):
    """
    ZIP-এর ভেতরের গঠন দেখে ঠিক করে কোন ফোল্ডারটা প্রজেক্টের আসল রুট হবে:
      • সব ফাইল একই র‍্যাপার ফোল্ডারে থাকলে (যেমন mybot/main.py) সেটা বাদ যাবে (একাধিক লেভেল হলেও)।
      • তারপরেও রুটে কোনো .py না থাকলে, যে সাবফোল্ডারে বটের .py আছে (main.py থাকলে সেটা আগে)
        তার ভেতরের সব ফাইল রুটে উঠবে; রুটের অন্য ফাইল (requirements.txt, README ইত্যাদি) থেকে যাবে।
    রিটার্ন: (common_prefix_list, sub_dir_list)
    """
    files = []
    for i in infos:
        if i.is_dir() or _zip_is_junk(i.filename):
            continue
        files.append(_zip_parts(i.filename))
    if not files:
        return [], []
    common = list(os.path.commonprefix([f[:-1] for f in files]))
    rel_files = [f[len(common):] for f in files]
    if any(len(r) == 1 and r[0].lower().endswith(".py") for r in rel_files):
        return common, []
    cands = {}
    for r in rel_files:
        if len(r) > 1 and r[-1].lower().endswith(".py") and not (set(r[:-1]) & _ZIP_SKIP_DIRS):
            cands.setdefault(tuple(r[:-1]), []).append(r[-1])
    if not cands:
        return common, []
    best = min(cands.items(), key=lambda kv: (0 if "main.py" in kv[1] else 1, len(kv[0]), -len(kv[1])))[0]
    return common, list(best)

def safe_extract_zip(zip_path: str, dest_dir: str, strip_root: bool = False, strict: bool = False):
    """
    ZIP ফাইল নিরাপদে এক্সট্রাক্ট করে — প্রতিটি এন্ট্রির চূড়ান্ত (resolved) পাথ dest_dir-এর
    ভেতরেই থাকছে কিনা যাচাই করা হয় (Zip Slip / Path Traversal অ্যাটাক ঠেকাতে)। সন্দেহজনক এন্ট্রি
    স্কিপ হয়, পুরো এক্সট্রাকশন আটকায় না। রিটার্ন করে স্কিপ করা এন্ট্রির নামের লিস্ট।

    strip_root=True (ইউজার আপলোডের জন্য): র‍্যাপার ফোল্ডার ফ্ল্যাটেন + __MACOSX/._ জাঙ্ক বাদ — ফোল্ডারসহ বা ছাড়া
    যেকোনো ZIP-ই কাজ করে। ডিফল্ট (False) আচরণ আগের মতোই (GitHub Sync/Deploy নিজে flatten করে)।
    strict=True: খারাপ/পাসওয়ার্ড-সুরক্ষিত/অতিরিক্ত বড় ZIP-এ চুপচাপ না গিয়ে এক্সেপশন তোলে।
    """
    dest_abs = os.path.abspath(dest_dir)
    skipped = []
    size_msg = "ZIP অনেক বড় (সর্বোচ্চ ২০০MB / ৩০০০ ফাইল)"
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        infos = zip_ref.infolist()
        # ZIP-bomb সুরক্ষা: এক্সট্রাক্ট করলে মোট সাইজ/ফাইল সংখ্যা সীমা ছাড়ালে কিছুই এক্সট্রাক্ট করা হয় না
        if len(infos) > ZIP_MAX_FILES or sum(i.file_size for i in infos) > ZIP_MAX_TOTAL_BYTES:
            if strict:
                raise ValueError(size_msg)
            return [size_msg]

        if not strip_root:
            for member in infos:
                member_path = os.path.abspath(os.path.join(dest_abs, member.filename))
                if member_path != dest_abs and not member_path.startswith(dest_abs + os.sep):
                    skipped.append(member.filename)
                    continue
                try:
                    zip_ref.extract(member, dest_abs)
                except Exception:
                    skipped.append(member.filename)
            return skipped

        common, sub = _zip_plan_root(infos)
        written = 0
        for member in infos:
            if _zip_is_junk(member.filename):
                continue
            rel = _zip_parts(member.filename)[len(common):]
            if sub and rel[:len(sub)] == sub:
                rel = rel[len(sub):]
            if not rel:
                continue
            target = os.path.abspath(os.path.join(dest_abs, *rel))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                skipped.append(member.filename)
                continue
            try:
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zip_ref.open(member) as src_f, open(target, "wb") as out_f:
                    while True:
                        chunk = src_f.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > ZIP_MAX_TOTAL_BYTES:
                            raise ValueError(size_msg)
                        out_f.write(chunk)
            except ValueError:
                raise
            except (RuntimeError, zipfile.BadZipFile):
                if strict:
                    raise
                skipped.append(member.filename)
            except Exception:
                skipped.append(member.filename)
    return skipped

def pick_entry_point(p_dir: str, names=None):
    """
    প্রজেক্ট ফোল্ডারের রুট থেকে বটের মূল ফাইল বাছে: main.py → bot.py → app.py → run.py …;
    এগুলো না থাকলে কোড দেখে (polling/run/__main__ আছে কিনা) স্কোর করে — config.py/utils.py-এর মতো
    হেল্পার ফাইল ভুল করে বেছে নেওয়া এড়াতে।
    """
    try:
        py = sorted(f for f in os.listdir(p_dir)
                    if f.endswith(".py") and not f.startswith("._") and os.path.isfile(os.path.join(p_dir, f)))
    except Exception:
        return None
    if names:
        # ⭐ শুধু এইবার আপলোড হওয়া ফাইলগুলোর মধ্য থেকে বাছা (আগে py_files[0] ধরা হতো — যেটা config.py/utils.py-ও হতে পারত)
        wanted = {os.path.basename(n) for n in names}
        py = [f for f in py if f in wanted] or py
    if not py:
        return None
    if "main.py" in py:
        return "main.py"
    for name in ("bot.py", "app.py", "run.py", "start.py", "index.py", "__main__.py", "server.py", "launcher.py", "start_bot.py", "run_bot.py", "mybot.py", "telegram_bot.py"):
        if name in py:
            return name
    helpers = {"config", "settings", "utils", "util", "helpers", "helper", "constants", "keep_alive", "keepalive", "database", "db", "models", "setup", "conftest", "test", "tests", "__init__", "localization", "keyboards", "states", "handlers"}
    best, best_score = py[0], -999
    for f in py:
        score = 0
        try:
            with open(os.path.join(p_dir, f), "r", encoding="utf-8", errors="ignore") as fh:
                code = fh.read(200000)
            if re.search(r"if\s+__name__\s*==", code):
                score += 3
            if re.search(r"infinity_polling|\.polling\(|run_polling|\.run\(|run_until_complete|start_polling|idle\(\)|asyncio\.run\(|serve_forever|run_forever\(|uvicorn\.run|while\s+True\s*:", code):
                score += 3
            if re.search(r"^from\s+\.", code, re.M):
                score -= 2
        except Exception:
            pass
        if f[:-3].lower() in helpers:
            score -= 5
        if score > best_score:
            best, best_score = f, score
    return best

def install_missing_packages_sync(pkg_list):
    """সব প্যাকেজ এক pip কমান্ডে একসাথে ইনস্টল করে (প্রতিটির জন্য আলাদা pip প্রসেস চালু
    করলে যে বাড়তি সময় লাগে সেটা এড়াতে) — ডিপ্লয় অনেক দ্রুত হয়।"""
    needed = [pkg for pkg in pkg_list if pkg not in installed_packages_cache]
    if not needed:
        return True, []

    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary", "--no-cache-dir", *needed],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300
        )
        if res.returncode == 0:
            for pkg in needed:
                installed_packages_cache.add(pkg)
            return True, needed
    except Exception:
        pass

    # ব্যাচ ইনস্টল ব্যর্থ হলে (যেমন একটা প্যাকেজের নাম ভুল) একটা একটা করে ফলব্যাক
    installed = []
    for pkg in needed:
        try:
            res = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary", "--no-cache-dir", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=150)
            if res.returncode == 0:
                installed_packages_cache.add(pkg)
                installed.append(pkg)
        except Exception:
            pass
    return True, installed

# ----------------- অপ্টিমাইজড প্রসেস এক্সিকিউশন ও কাউন্টার ফিক্স -----------------
HOST_SECRET_ENV_KEYS = {"BOT_TOKEN", "DB_CHANNEL_ID", "ADMIN_ID", "ADMIN_IDS", "OWNER_ID"}

def _user_script_env() -> dict:
    """ইউজারের বট প্রসেসকে হোস্ট-বটের গোপন এনভায়রনমেন্ট ভ্যারিয়েবল (BOT_TOKEN ইত্যাদি) ছাড়া চালানো হয়,
    যাতে কেউ os.environ পড়ে হোস্ট বটের টোকেন নিয়ে যেতে না পারে।"""
    env = {k: v for k, v in os.environ.items() if k.upper() not in HOST_SECRET_ENV_KEYS}
    # ⭐ যেকোনো ধরনের স্ক্রিপ্ট যেন চলে: UTF-8 (emoji/বাংলা print-এ UnicodeEncodeError না হয়), বাফার ছাড়া লগ, GUI ছাড়া matplotlib
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("MPLBACKEND", "Agg")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("TERM", "xterm")
    return env

def _normalize_py_sources(p_dir: str, limit: int = 60):
    """⭐ যেকোনো এনকোডিংয়ের .py ফাইল চালানো যাওয়ার জন্য: UTF-16 (Windows Notepad 'Unicode'), cp1252/latin-1 বা NUL-বাইটওয়ালা ফাইল
    UTF-8-এ বদলে দেয় (যেগুলোতে Python 'SyntaxError: Non-UTF-8 code' বা 'null bytes' দিয়ে চালুই হতো না)। ঠিক থাকা ফাইল ছোঁয়া হয় না।"""
    n = 0
    for root, dirs, files in os.walk(p_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "venv", ".venv", "node_modules", "site-packages")]
        for fn in files:
            if not fn.endswith(".py") or fn.startswith("._"):
                continue
            n += 1
            if n > limit:
                return
            fp = os.path.join(root, fn)
            try:
                with open(fp, "rb") as f:
                    raw = f.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    continue
                text = None
                if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                    text = raw.decode("utf-16")
                elif b"\x00" in raw[:4000]:
                    text = raw.decode("utf-16-le", errors="ignore") if raw[1:2] == b"\x00" else raw.replace(b"\x00", b"").decode("utf-8", errors="replace")
                else:
                    try:
                        raw.decode("utf-8")
                    except UnicodeDecodeError:
                        if re.search(rb"coding[:=]\s*[-\w.]+", raw[:300]) and not re.search(rb"coding[:=]\s*utf-?8", raw[:300], re.I):
                            continue      # ফাইল নিজেই অন্য এনকোডিং ঘোষণা করেছে — Python নিজে সামলাবে
                        text = raw.decode("cp1252", errors="replace")
                if text is not None:
                    text = text.lstrip("\ufeff")
                    with open(fp, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text.replace("\r\n", "\n"))
            except Exception:
                pass

def pip_install_requirements(req_file: str, timeout: int = 240) -> bool:
    """requirements.txt ইনস্টল — টাইমআউটসহ, যাতে একটা আটকে যাওয়া pip পুরো ডিপ্লয় ঝুলিয়ে না রাখে।"""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "--disable-pip-version-check", "--no-input", "--prefer-binary", "--no-cache-dir"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        if res.returncode == 0:
            return True
    except Exception:
        pass
    # ⭐ FIX: requirements.txt-এ একটা ভুল/অচল লাইন (যেমন Windows-only প্যাকেজ বা ভুল ভার্সন) থাকলে পুরো ফাইলই ব্যর্থ হতো —
    # এখন লাইন ধরে ধরে আলাদা ইনস্টল করে, যেগুলো পারে সেগুলো বসায়।
    try:
        ok_any = False
        with open(req_file, "r", encoding="utf-8", errors="ignore") as rf:
            lines = [l.strip() for l in rf.read().splitlines()]
        for ln in lines[:80]:
            if not ln or ln.startswith("#") or ln.startswith("-") or ln.lower().startswith(("pywin32", "wmi", "pypiwin32")):
                continue
            ln = ln.split(" #")[0].strip()
            try:
                r1 = subprocess.run([sys.executable, "-m", "pip", "install", ln, "--disable-pip-version-check", "--no-input", "--prefer-binary", "--no-cache-dir"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=150)
                if r1.returncode != 0 and re.search(r"[=<>!~]", ln):
                    # নির্দিষ্ট ভার্সন পাওয়া না গেলে ভার্সন ছাড়া চেষ্টা
                    base = re.split(r"[=<>!~;\[ ]", ln, 1)[0]
                    if base:
                        r1 = subprocess.run([sys.executable, "-m", "pip", "install", base, "--disable-pip-version-check", "--no-input", "--prefer-binary", "--no-cache-dir"],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=150)
                ok_any = ok_any or r1.returncode == 0
            except Exception:
                pass
        return ok_any
    except Exception:
        return False


def _serialize_pip(fn):
    def _w(*a, **k):
        with _PIP_LOCK:
            return fn(*a, **k)
    _w.__name__ = fn.__name__
    return _w

install_missing_packages_sync = _serialize_pip(install_missing_packages_sync)
pip_install_requirements = _serialize_pip(pip_install_requirements)

def execute_project_script(user_id: int, project_id: int, file_name: str = None, preserve_restarts: bool = False, bypass_free_gate: bool = False):
    project = admin.get_project_by_id(project_id)
    if not project:
        return False, "Project not found!"

    # 🆓 ফ্রি হোস্টিং টগল — অ্যাডমিন বন্ধ রাখলে নতুন ফ্রি সেশন (Start/Restart/নতুন ডিপ্লয়) আটকানো হবে।
    # bypass_free_gate=True শুধু ইতিমধ্যে চলতে থাকা বটের ক্র্যাশ-রিকভারি অটো-রিস্টার্টে ব্যবহৃত হয়,
    # যাতে টগল অফ করলে চলমান ফ্রি বটগুলো মাঝপথে বিপর্যস্ত না হয়ে যায়।
    if not bypass_free_gate and not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True):
        u_info_gate = admin.get_user(user_id)
        slot_status_gate = admin.get_effective_slot_status(u_info_gate, project)
        if not slot_status_gate["is_24_7"]:
            return False, free_hosting_blocked_message()

    p_key = f"{user_id}_{project_id}"
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    os.makedirs(p_dir, exist_ok=True)

    if file_name:
        admin.update_project_main_file(project_id, file_name)
    else:
        file_name = project["main_file"]

    script_path = os.path.join(p_dir, file_name)
    log_path = os.path.join(p_dir, "app.log")

    if not os.path.exists(script_path):
        return False, f"<code>{file_name}</code> ফাইলটি পাওয়া যায়নি!"

    # বট রানের আগে DB থেকে env vars .env ফাইলে লেখা হচ্ছে
    try:
        admin.write_env_file(user_id, project_id)
    except Exception:
        pass

    old_restarts = 0
    if p_key in active_processes:
        try:
            if preserve_restarts:
                old_restarts = active_processes[p_key].get("restarts", 0)
            active_processes[p_key]["process"].terminate()
            if "log_handle" in active_processes[p_key]:
                active_processes[p_key]["log_handle"].close()
        except Exception:
            pass

    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n--- [Bot Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
    log_file.flush()

    # ⭐ প্রতিটি বটের নিজস্ব PORT (হোস্টের health-সার্ভার ও অন্য বটের সাথে পোর্ট-সংঘর্ষে Flask/FastAPI-জাতীয় স্ক্রিপ্ট ক্র্যাশ না করতে),
    # PYTHONPATH-এ প্রজেক্ট ফোল্ডার (সাবফোল্ডার/প্যাকেজ ইম্পোর্ট ঠিকমতো), stdin বন্ধ (input() ঝুলে না থেকে সাথে সাথে EOFError দেয়)
    _env = _user_script_env()
    _env["PORT"] = str(30000 + (int(project_id) % 30000))
    _env["PYTHONPATH"] = p_dir + (os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
    try:
        _normalize_py_sources(p_dir)
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, "-u", script_path],
        cwd=p_dir,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=_env
    )

    active_processes[p_key] = {
        "process": proc,
        "start_time": time.time(),
        "file": file_name,
        "log_path": log_path,
        "log_handle": log_file,
        "user_id": user_id,
        "project_id": project_id,
        "project_name": project["project_name"],
        "restarts": old_restarts,
        "warning_sent": False
    }

    admin.update_project_status(project_id, "RUNNING")
    admin.increment_deploy_count(user_id)

    # প্রথম বট চালু করার অ্যাচিভমেন্ট ব্যাজ
    try:
        u_check = admin.get_user(user_id)
        if (u_check.get("total_deploys") or 0) <= 1:
            if admin.award_badge(user_id, "FIRST_BOT"):
                b = admin.ACHIEVEMENTS["FIRST_BOT"]
                bot.send_message(
                    user_id,
                    f"{b['emoji']} <b>নতুন ব্যাজ অর্জিত: {b['title']}!</b>\n{b['desc']} 🎉",
                    parse_mode="HTML"
                )
    except Exception:
        pass

    return True, "Success"

def stop_project_script(user_id: int, project_id: int):
    p_key = f"{user_id}_{project_id}"
    if p_key in active_processes:
        # ⭐ FIX: terminate()-এর পর del হওয়ার ফাঁকে ওয়াচডগ মরা প্রসেস দেখে "ক্র্যাশ" ধরে
        # বন্ধ করা বটটাই আবার চালু করে দিত — তাই আগে 'stopping' ফ্ল্যাগ বসানো হয়।
        active_processes[p_key]["stopping"] = True
        try:
            active_processes[p_key]["process"].terminate()
            active_processes[p_key]["log_handle"].close()
        except Exception:
            pass
        active_processes.pop(p_key, None)
    admin.update_project_status(project_id, "STOPPED")
    return True

def restart_all_active_bots():
    count = 0
    for p_key, data in list(active_processes.items()):
        uid = data["user_id"]
        pid = data["project_id"]
        fn = data["file"]
        execute_project_script(uid, pid, fn, preserve_restarts=False, bypass_free_gate=True)
        count += 1
    return count

def is_slot_locked(u_data: dict, project: dict) -> bool:
    """অ্যাডমিন ফ্রি হোস্টিং বন্ধ রাখলে, যেসব স্লট প্ল্যান/এক্সটেনশন দিয়ে 24/7 কভার্ড নয়
    (অর্থাৎ সত্যিকারের ফ্রি স্লট), সেগুলো এই ফাংশন অনুযায়ী "লকড" ধরা হবে — Start/Stop/Restart/
    Upload/Rename/GitHub Sync ইত্যাদি সব অ্যাকশন বন্ধ থাকবে, শুধু Upgrade বাটন দেখাবে।"""
    if admin.SYSTEM_CONFIG.get("free_hosting_enabled", True):
        return False
    slot_status = admin.get_effective_slot_status(u_data, project)
    return not slot_status["is_24_7"]

def build_gh_backup_panel():
    """💾 ডাটা ব্যাকআপ প্যানেল (প্রাইভেট DB-চ্যানেল)।"""
    st = channel_backup.status
    last = st["last_ok"].strftime("%d %b • %H:%M") if st["last_ok"] else "এখনো হয়নি"
    text = (
        "💾 <b>ডাটা ব্যাকআপ</b> (📡 প্রাইভেট চ্যানেল)\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 <b>চালু</b> — ডাটাবেস PIN করা মেসেজে থাকে, ইউজারের কোড-কপি চ্যানেলে আগে থেকেই যায়।\n"
        "⏱️ DB বদলালে প্রতি ~২ মিনিটে আপডেট; রিস্টার্ট/ডেপ্লয়ের ঠিক আগেও একবার\n"
        "📦 ব্যাকআপ ~১৮ MB ছাড়ালে নিজে ভাগ হয়ে নতুন ফাইল পিন হয় — আগের ফাইলগুলো চ্যানেলে থেকে যায়\n"
        f"✅ সর্বশেষ সফল: <b>{last}</b>"
        + (f"\n📊 {st['last_users']} ইউজার • {st['last_projects']} বট • {channel_backup._fmt_size(st['last_size'])}" if st["last_ok"] else "")
        + (f"\n🔢 ব্যাকআপ #{st.get('last_seq')}" + (f" • 🧩 {st.get('last_parts')}টি ফাইল (জেনারেশন #{st.get('last_gen')})" if (st.get('last_parts') or 1) > 1 else " • 📌 একক ফাইল") if st["last_ok"] else "")
        + (f"\n❌ <code>{html.escape(st['last_error'])}</code>" if st["last_error"] else "")
        + ("\n⚠️ <b>PIN করা যায়নি</b> — চ্যানেলে বটকে <b>Pin Messages</b> পারমিশন দিন, নাহলে রিস্টোর কাজ করবে না" if st["pin_warning"] else "")
        + ("\n🛑 <b>আগের রিস্টোর ব্যর্থ — ওভাররাইট বন্ধ</b>, বট রিস্টার্ট দিন" if st["restore_failed"] else "")
        + "\n━━━━━━━━━━━━━━━━━━━━━━\n🔁 রিসেটের পর লোকাল ডাটা না থাকলে বট নিজে থেকেই সব ইউজার, প্ল্যান ও বট ফিরিয়ে আনে — ইউজার কিছু টেরই পাবে না।"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("📡 Backup Now", callback_data="adm_chb_now", style="success"),
        make_inline_button("🔄 Refresh", callback_data="adm_ghb_status", style="primary")
    )
    return text, markup

# ======================== 🧹 Channel Clean (শুধু DB-ব্যাকআপের তথ্য) ========================
# ইউজারদের কোড-কপি কখনো মোছা হয় না — শুধু প্রাইভেট চ্যানেলে জমা DB-ব্যাকআপ ফাইলগুলো পরিষ্কার করা যায়।
_chc_lock = threading.Lock()

def build_channel_clean_panel():
    try:
        info = channel_backup.inspect(bot, DB_CHANNEL_ID)
    except Exception as e:
        info = {"error": str(e)[:150]}
    fs = channel_backup._fmt_size
    if info.get("error"):
        body = f"⚠️ চ্যানেল পড়া যায়নি: <code>{html.escape(info['error'])}</code>\n"
    elif not info.get("pinned"):
        body = "📌 PIN করা কোনো DB-ব্যাকআপ নেই।\n"
    else:
        body = (f"📌 বর্তমান ব্যাকআপ: <b>{'মাল্টি-ফাইল (' + str(info['parts']) + 'টি পার্ট)' if info['mode'] == 'multi' else 'একক ফাইল'}</b> • {fs(info['size'])}\n"
                f"🗄 পুরনো জেনারেশন (মোছার যোগ্য): <b>{info['old_msgs']}</b>টি মেসেজ\n")
    text = ("🧹 <b>CHANNEL CLEAN — DB ব্যাকআপ</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + body +
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔒 <b>ইউজারদের কোড-কপি কখনো মোছা হয় না।</b>\n"
            "🗑 পুরনো জেনারেশন মুছলে বর্তমান PIN করা ব্যাকআপ অক্ষত থাকে।\n"
            "♻️ রিসেট করলে সব DB-ব্যাকআপ মুছে বর্তমান ডাটাবেস থেকে নতুন একটা তৈরি হয়।")
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        make_inline_button("🗑 পুরনো DB-ব্যাকআপ মুছুন", callback_data="adm_chc_ask_old", style="primary"),
        make_inline_button("♻️ DB-ব্যাকআপ রিসেট (মুছে নতুন)", callback_data="adm_chc_ask_reset", style="danger"),
        make_inline_button("🔄 Refresh", callback_data="adm_chc_panel", style="success"),
    )
    return text, mk

def _run_channel_clean(chat_id: int, msg_id: int, kind: str):
    if not _chc_lock.acquire(blocking=False):
        try:
            bot.send_message(chat_id, "⏳ আরেকটি ক্লিন চলছে।")
        except Exception:
            pass
        return
    try:
        ok, info = channel_backup.clean(bot, DB_CHANNEL_ID, kind)
        t2, mk2 = build_channel_clean_panel()
        head = ("✅ <b>সম্পন্ন:</b> " if ok else "❌ <b>ব্যর্থ:</b> ") + html.escape(info) + "\n\n"
        try:
            bot.edit_message_text(head + t2, chat_id, msg_id, reply_markup=mk2, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, head, parse_mode="HTML")
    except Exception as e:
        try:
            bot.send_message(chat_id, f"❌ ক্লিন ব্যর্থ: <code>{html.escape(str(e)[:200])}</code>", parse_mode="HTML")
        except Exception:
            pass
    finally:
        _chc_lock.release()

def build_vip_unlock_panel():
    """🔓 VIP All Slot Unlock — অ্যাডমিন কন্ট্রোল প্যানেলের টেক্সট ও বাটন।"""
    until = admin.get_vip_unlock_until()
    if until:
        status = (
            f"🟢 <b>এখন চালু আছে</b>\n"
            f"⏳ বাকি: <b>{admin.format_unlock_remaining(until)}</b>\n"
            f"🕒 শেষ হবে: <code>{until.strftime('%d %b %Y • %H:%M')}</code>"
        )
    else:
        status = "🔴 <b>বর্তমানে বন্ধ আছে</b>"
    text = (
        "🔓 <b>VIP ALL SLOT UNLOCK</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{status}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎚 আনলক হবে: <b>প্রতি ইউজারের {to_bn_num(admin.get_vip_unlock_slots())}টি স্লট</b> (Slot 1–{admin.get_vip_unlock_slots()})\n"
        "চালু করলে নির্বাচিত সময় পর্যন্ত ওই স্লটগুলো আনলক হবে এবং "
        "বট ফ্রি-সেশন লিমিট ছাড়াই <b>24/7</b> চলবে।\n"
        "💎 পেইড প্ল্যান ও Extend Validity আগের মতোই থাকবে। মেয়াদ শেষে নিজে থেকেই বন্ধ হবে।\n"
        + ("\n⚠️ নতুন সময় বাছাই করলে বর্তমান টাইমার <b>এখন থেকে</b> নতুন করে শুরু হবে।\n" if until else "")
        + "\n1️⃣ আগে <b>কয়টি স্লট</b> আনলক হবে বাছুন (✅ = বর্তমান)\n"
        "2️⃣ তারপর <b>কতদিনের</b> জন্য সেটা বাছুন 👇"
    )
    cur_slots = admin.get_vip_unlock_slots()
    markup = types.InlineKeyboardMarkup(row_width=5)
    markup.add(*[
        make_inline_button(f"✅ {n}" if n == cur_slots else f"{n}", callback_data=f"adm_vipu_slots_{n}",
                           style="success" if n == cur_slots else "primary")
        for n in range(1, admin.MAX_BOT_SLOTS + 1)
    ])
    markup.row_width = 3
    markup.add(
        make_inline_button("2 Days", callback_data="adm_vipu_set_48", style="primary"),
        make_inline_button("7 Days", callback_data="adm_vipu_set_168", style="primary"),
        make_inline_button("15 Days", callback_data="adm_vipu_set_360", style="primary"),
    )
    markup.add(make_inline_button("✏️ Custom (নিজে লিখুন)", callback_data="adm_vipu_custom", style="success"))
    if until:
        markup.add(make_inline_button("🛑 এখনই বন্ধ করুন", callback_data="adm_vipu_off", style="danger"))
    return text, markup

def enforce_vip_slot_limits() -> int:
    """VIP Unlock-এর স্লট-সংখ্যা কমালে: নতুন সীমার বাইরের স্লটে (ইভেন্টের কারণে খোলা) চলা বটগুলো বন্ধ করে।
    পেইড/Extend Validity-তে কভার্ড বট ছোঁয়া হয় না। রিটার্ন: কতটি বন্ধ হলো।"""
    stopped = 0
    for p_key, data in list(active_processes.items()):
        try:
            uid, pid = data["user_id"], data["project_id"]
            project = admin.get_project_by_id(pid)
            if not project:
                continue
            u_data = admin.get_user(uid)
            if admin.get_effective_slot_status(u_data, project)["is_24_7"]:
                continue
            slot_no = project.get("slot_number") or 0
            if slot_no > admin.get_max_bot_limit_for_user(u_data):
                stop_project_script(uid, pid)
                stopped += 1
                try:
                    bot.send_message(
                        uid,
                        "🔒 <b>VIP Unlock-এর স্লট-সংখ্যা কমানো হয়েছে</b>\n"
                        f"🤖 <b>বট:</b> <code>{html.escape(data['project_name'])}</code>\n"
                        "এই স্লটটি এখন আর আনলক নেই, তাই বটটি বন্ধ হয়েছে। ২৪/৭ চালাতে <b>💎 Upgrade Plan</b> দেখুন।",
                        parse_mode="HTML")
                except Exception:
                    pass
        except Exception:
            pass
    return stopped

def end_vip_unlock(notify_admin_chat=None) -> int:
    """VIP Unlock ইভেন্ট শেষ করে (মেয়াদ পেরোলে অটো / অ্যাডমিন বন্ধ করলে ম্যানুয়ালি)।
    - যেসব চলমান বট স্লট-লিমিটের বাইরে (ইভেন্টের কারণে খোলা স্লটে) সেগুলো বন্ধ হয়।
    - ফ্রি স্লটে চলা বাকি বটগুলোর ফ্রি-সেশন টাইমার নতুন করে শুরু হয়, যাতে হঠাৎ করে একসাথে না কেটে যায়।
    - ফ্রি হোস্টিং OFF থাকলে ফ্রি বটগুলোও লক হয়।
    রিটার্ন: কতগুলো বট বন্ধ হলো।"""
    admin.clear_vip_unlock()
    stopped = 0
    for p_key, data in list(active_processes.items()):
        uid = data["user_id"]
        pid = data["project_id"]
        project = admin.get_project_by_id(pid)
        if not project:
            continue
        u_data = admin.get_user(uid)
        slot_status = admin.get_effective_slot_status(u_data, project)
        if slot_status["is_24_7"]:
            continue
        slot_no = project.get("slot_number") or 0
        over_limit = slot_no > admin.get_max_bot_limit_for_user(u_data)
        free_off = not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True)
        if over_limit or free_off:
            stop_project_script(uid, pid)
            stopped += 1
            try:
                bot.send_message(
                    uid,
                    "🔓 <b>VIP Unlock ইভেন্ট শেষ হয়েছে!</b>\n"
                    f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "এই স্লটটি এখন আপনার প্ল্যানের আওতার বাইরে, তাই বটটি বন্ধ হয়েছে। "
                    "২৪/৭ চালিয়ে যেতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            data["start_time"] = time.time()
            data["warning_sent"] = False
    return stopped

def lock_all_free_bots() -> int:
    """অ্যাডমিন ফ্রি হোস্টিং OFF করলে এই মুহূর্তে চলতে থাকা সব ফ্রি (non-24/7) বট বন্ধ করে দেয়,
    যাতে টগল অফ করার সাথে সাথেই সব ফ্রি স্লট সত্যিকারভাবে লক হয়ে যায়। পেইড/24-7-কভার্ড বট
    এতে প্রভাবিত হয় না। রিটার্ন করে কতটি বট বন্ধ হলো।"""
    stopped = 0
    for p_key, data in list(active_processes.items()):
        uid = data["user_id"]
        pid = data["project_id"]
        project = admin.get_project_by_id(pid)
        if not project:
            continue
        u_data = admin.get_user(uid)
        slot_status = admin.get_effective_slot_status(u_data, project)
        if not slot_status["is_24_7"]:
            stop_project_script(uid, pid)
            stopped += 1
            try:
                bot.send_message(
                    uid,
                    f"🔒 <b>ফ্রি হোস্টিং সাময়িকভাবে বন্ধ করা হয়েছে!</b>\n"
                    f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "আপনার বটটি সাময়িকভাবে বন্ধ রাখা হয়েছে। অ্যাডমিন আবার চালু করলে অথবা "
                    "💎 <b>Upgrade Plan</b> থেকে যেকোনো প্ল্যান নিলে আপনি আবার চালাতে পারবেন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    return stopped

# ----------------- স্মার্ট ওয়াচডগ ও অটোমেটিক এক্সপায়ারি অ্যালার্ট -----------------

def _award_first_upgrade_badge(uid: int):
    try:
        if admin.award_badge(uid, "FIRST_UPGRADE"):
            b = admin.ACHIEVEMENTS["FIRST_UPGRADE"]
            bot.send_message(uid, f"{b['emoji']} <b>নতুন ব্যাজ অর্জিত: {b['title']}!</b>\n{b['desc']} 🎉", parse_mode="HTML")
    except Exception:
        pass

_auto_pip_tried = {}   # {p_key: {module_name}} — মিসিং-প্যাকেজ অটো-ফিক্স একই মডিউলে বারবার চেষ্টা আটকাতে

def _read_log_tail(log_path, max_lines: int = 12) -> str:
    """বটের app.log-এর শেষ কয়েক লাইন (বড় ফাইল পুরো মেমোরিতে না নিয়ে শেষ ৬ KB থেকে)।"""
    try:
        if not log_path or not os.path.exists(log_path):
            return ""
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 6144))
            chunk = f.read().decode("utf-8", errors="ignore")
        return "\n".join(chunk.splitlines()[-max_lines:])
    except Exception:
        return ""

def hosting_watchdog_thread():
    while True:
        time.sleep(5)
        now = time.time()

        # 🔓 VIP All Slot Unlock — মেয়াদ পেরিয়ে গেলে অটো বন্ধ ও ক্লিনআপ
        try:
            if admin.SYSTEM_CONFIG.get("vip_unlock_until") and not admin.is_vip_unlock_active():
                end_vip_unlock()
                for aid in admin.ADMIN_IDS:
                    try:
                        bot.send_message(aid, "🔓 <b>VIP All Slot Unlock</b> ইভেন্টের মেয়াদ শেষ হয়েছে — অটো বন্ধ করা হয়েছে।", parse_mode="HTML")
                    except Exception:
                        pass
        except Exception:
            pass
        
        # ১. লাইভ প্রসেস মনিটরিং ও ক্র্যাশ কন্ট্রোল
        for p_key in list(active_processes.keys()):
            data = active_processes.get(p_key)
            if not data:
                continue

            uid = data["user_id"]
            pid = data["project_id"]
            proc = data["process"]
            start_t = data["start_time"]
            u_info = admin.get_user(uid)
            plan = u_info.get("plan", "FREE").upper()

            # স্লটের প্রকৃত (effective) স্ট্যাটাস — অ্যাকাউন্ট প্ল্যান + স্লটের নিজস্ব Extend
            # Validity ক্রয় উভয়ই একসাথে বিবেচনা করা হয়, যাতে একটা একটিভ প্ল্যান থাকা সত্ত্বেও
            # স্লট ভুলবশত বন্ধ না হয়ে যায়, বা স্লট আলাদাভাবে কেনা থাকলে সেটা কাজেই না লাগে।
            proj_info = admin.get_project_by_id(pid)
            slot_status = admin.get_effective_slot_status(u_info, proj_info) if proj_info else None

            # কোনো কভারেজই (প্ল্যান বা স্লট) না থাকলে এবং আগে কেনা মেয়াদ সত্যিই শেষ হয়ে গেলে বট বন্ধ
            if slot_status and slot_status["is_expired"]:
                try:
                    proc.terminate()
                    data["log_handle"].close()
                except Exception:
                    pass
                if p_key in active_processes:
                    del active_processes[p_key]
                admin.update_project_status(pid, "STOPPED")
                try:
                    bot.send_message(
                        uid,
                        f"⏳ <b>বট স্লটের মেয়াদ শেষ হয়েছে!</b>\n"
                        f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "👉 বট চালু রাখতে <b>⏳ Extend Validity</b> থেকে মেয়াদ বাড়িয়ে নিন।",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                continue

            # যদি বট ১ মিনিটের বেশি স্বাভাবিক চলে, রিস্টার্ট কাউন্টার রিসেট হবে
            if proc.poll() is None and (now - start_t) > 60 and data.get("restarts", 0) > 0:
                data["restarts"] = 0

            # ১২ ঘণ্টার ফ্রি-সেশন ক্যাপ শুধুমাত্র সেই স্লটেই প্রযোজ্য যেটা না অ্যাকাউন্ট প্ল্যান
            # দ্বারা, না নিজস্ব Extend Validity ক্রয় দ্বারা কভার্ড — অর্থাৎ, সত্যিকারের ফ্রি স্লট।
            # (আগে এই চেক শুধু plan == "FREE" দেখেই চলত, ফলে কেউ শুধু এই স্লটের জন্য Extend
            # Validity কিনলেও অ্যাকাউন্ট প্ল্যান FREE থাকলে বটটি তবুও ১২ ঘণ্টা পর বন্ধ হয়ে যেত।)
            is_covered_24_7 = bool(slot_status and slot_status["is_24_7"])
            if not is_covered_24_7:
                elapsed = now - start_t
                if elapsed >= admin.get_free_warning_seconds() and not data.get("warning_sent", False):
                    data["warning_sent"] = True
                    try:
                        bot.send_message(
                            uid,
                            f"⏳ <b>হোস্টিং সেশন অ্যালার্ট (৩০ মিনিট বাকি)!</b> ⚠️\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 <b>বটের নাম:</b> <code>{data['project_name']}</code>\n"
                            f"⏱️ আপনার {to_bn_num(admin.SYSTEM_CONFIG.get('free_hosting_hours', 12))} ঘণ্টার ফ্রি সেশনের মেয়াদ আর মাত্র <b>৩০ মিনিট</b> পর শেষ হবে।\n\n"
                            "💡 <b>বট নিরবচ্ছিন্ন সচল রাখতে:</b>\n"
                            "• মেয়াদ শেষ হলে <b>🎛️ Control Panel</b> থেকে রিস্টার্ট করুন (ফ্রি)\n"
                            "• অথবা ২৪/৭ নন-স্টপ হোস্টিং পেতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন!\n"
                            "━━━━━━━━━━━━━━━━━━━━━━",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                # ১২ ঘণ্টা পূর্ণ হলে ফ্রি সেশন বন্ধ
                if elapsed >= admin.get_free_session_seconds():
                    try:
                        proc.terminate()
                        data["log_handle"].close()
                    except Exception:
                        pass
                    del active_processes[p_key]
                    admin.update_project_status(pid, "STOPPED")
                    try:
                        bot.send_message(
                            uid,
                            f"⏳ <b>{to_bn_num(admin.SYSTEM_CONFIG.get('free_hosting_hours', 12))} ঘণ্টার ফ্রি হোস্টিং সেশন পূর্ণ হয়েছে!</b>\n"
                            f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "👉 পুনরায় চালু করতে <b>🎛️ Control Panel</b> থেকে Start চাপুন অথবা ২৪/৭ আনলিমিটেড চালাতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন।",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    continue

            # ক্র্যাশ ডিটেকশন ও স্মার্ট রিস্টার্ট হ্যান্ডলার
            if proc.poll() is not None:
                if data.get("stopping"):
                    continue   # ইউজার/অ্যাডমিন নিজে থামিয়েছে — ক্র্যাশ নয়
                if data.get("installing"):
                    continue   # মিসিং প্যাকেজ ইনস্টল চলছে

                rc = proc.returncode
                tail = _read_log_tail(data.get("log_path"), 12)

                # ✅ ১. exit code 0 = বটের কোড নিজে থেকে শেষ হয়েছে (ক্র্যাশ নয়) — রিস্টার্ট লুপ/ক্র্যাশ মেসেজ নয়
                if rc == 0:
                    try:
                        data["log_handle"].close()
                    except Exception:
                        pass
                    active_processes.pop(p_key, None)
                    admin.update_project_status(pid, "STOPPED")
                    try:
                        bot.send_message(
                            uid,
                            f"ℹ️ <b>বটের কোড কাজ শেষ করে নিজে থেকে বন্ধ হয়েছে।</b>\n"
                            f"🤖 <b>বট:</b> <code>{html.escape(data['project_name'])}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "এটা ক্র্যাশ নয় — আপনার কোড শেষ পর্যন্ত চলে exit করেছে। বট ২৪/৭ চালু রাখতে কোডে "
                            "<code>bot.infinity_polling()</code> (বা সমতুল্য লুপ) থাকতে হবে। আবার চালাতে <b>🎛️ Control Panel</b> থেকে Start দিন।",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    continue

                # 📦 ২. ModuleNotFoundError → মিসিং প্যাকেজ নিজে ইনস্টল করে আবার চালানো (ক্র্যাশ কাউন্টে ধরা হয় না)
                mod_match = re.search(r"No module named '([A-Za-z0-9_\.]+)'", tail)
                if mod_match:
                    full_mod = mod_match.group(1)
                    root_mod = full_mod.split(".")[0]
                    tried = _auto_pip_tried.setdefault(p_key, set())
                    _proj_dir = os.path.dirname(data.get("log_path") or "")
                    _is_local = (root_mod.lower() in LIKELY_LOCAL_MODULES or root_mod in WINDOWS_ONLY_MODULES
                                 or os.path.exists(os.path.join(_proj_dir, root_mod + ".py")) or os.path.isdir(os.path.join(_proj_dir, root_mod)))
                    if root_mod not in tried and len(tried) < 12 and not _is_local:
                        tried.add(root_mod)
                        data["installing"] = True
                        pip_name = resolve_pip_name(full_mod)

                        def _fix_and_restart(_uid=uid, _pid=pid, _data=data, _pip=pip_name):
                            try:
                                install_missing_packages_sync([_pip])
                                ok_x, _m = execute_project_script(_uid, _pid, _data["file"], preserve_restarts=True, bypass_free_gate=True)
                                if not ok_x:
                                    _data["installing"] = False
                            except Exception:
                                _data["installing"] = False
                        threading.Thread(target=_fix_and_restart, daemon=True).start()
                        continue

                # 🔁 ৩. সত্যিকারের ক্র্যাশ (non-zero exit): ধীরে ধীরে বাড়া ডিলে (১s, ৩s, ৮s) সহ সর্বোচ্চ ৩ বার অটো-রিস্টার্ট।
                # (আগে sleep(1) দিয়ে পুরো ওয়াচডগ থ্রেড — অর্থাৎ সব ইউজারের মনিটরিং — আটকে যেত।)
                if "restart_at" not in data:
                    data["restarts"] = data.get("restarts", 0) + 1
                    n_try = data["restarts"]
                    if n_try <= 3:
                        data["restart_at"] = now + (1, 3, 8)[n_try - 1]
                        continue
                elif now < data["restart_at"]:
                    continue
                else:
                    n_try = data["restarts"]
                    ok_re, re_msg = execute_project_script(uid, pid, data["file"], preserve_restarts=True, bypass_free_gate=True)
                    if ok_re:
                        try:
                            with open(data["log_path"], "a", encoding="utf-8") as lf:
                                lf.write(f"\n--- [Auto-restart {n_try}/3 after exit code {rc}] ---\n")
                        except Exception:
                            pass
                        # ⭐ FIX: প্রথমবারের ছোট ক্র্যাশে (যেমন স্টার্টআপে নেটওয়ার্ক ব্লিপ, যা রিস্টার্টেই ঠিক হয়ে যায়)
                        # ইউজারকে "ক্র্যাশ" মেসেজ পাঠানো হতো — অথচ বট ঠিকই চলছে। এখন শুধু একই বট
                        # পরপর বারবার ক্র্যাশ করলেই (২য় বার থেকে) জানানো হয়, সাথে আসল এররটাও দেখানো হয়।
                        if n_try >= 2:
                            try:
                                _diag = code_doctor.diagnose_traceback(tail, os.path.dirname(data["log_path"]))
                                if _diag:
                                    _txt = code_doctor.render_crash_card(_diag, data["project_name"], attempt=n_try)
                                else:
                                    _txt = (f"⚠️ <b>বটটি বারবার বন্ধ হয়ে যাচ্ছে:</b> <code>{html.escape(data['project_name'])}</code>\n"
                                            f"🔁 অটো-রিস্টার্ট {n_try}/3\n\n📜 <b>শেষ এরর:</b>\n<pre>{html.escape(tail[-700:] or 'লগে কিছু নেই')}</pre>")
                                bot.send_message(uid, _txt, parse_mode="HTML")
                            except Exception:
                                pass
                        continue
                    # রিস্টার্টই করা যায়নি (যেমন ফাইল মুছে গেছে) → নিচের ফাইনাল-স্টপে যাবে
                    tail = (re_msg or "") + "\n" + tail
                    n_try = 99

                if n_try > 3:
                    last_err = tail.strip() or "লগ ফাইলে কোনো বিস্তারিত পাওয়া যায়নি।"
                    try:
                        data["log_handle"].close()
                    except Exception:
                        pass
                    try:
                        _diag = code_doctor.diagnose_traceback(last_err, os.path.dirname(data["log_path"]))
                    except Exception:
                        _diag = None
                    try:
                        # 🩺 Traceback থেকে ঠিক কোন ফাইলের কত নম্বর লাইনে সমস্যা + বাংলায় সমাধান; না পেলে আগের মতো লগের অংশ
                        _crash_text = (code_doctor.render_crash_card(_diag, data["project_name"], final=True, exit_code=rc) if _diag else (
                            f"🚨 <b>বট ক্র্যাশ অ্যালার্ট (Bot Stopped)!</b> 🔴\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 <b>বটের নাম:</b> <code>{html.escape(data['project_name'])}</code>\n"
                            f"❌ কোডে ভুলের কারণে ৩ বার অটো-রিস্টার্টের পরও বটটি চালু থাকেনি (exit code: <code>{rc}</code>)।\n\n"
                            f"📜 <b>এরর বিবরণ:</b>\n<pre>{html.escape(last_err[-1200:])}</pre>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "👉 কোড সংশোধন করে আবার <b>🎛️ Control Panel</b> থেকে Start দিন."))
                        bot.send_message(uid, _crash_text, parse_mode="HTML")
                    except Exception:
                        pass
                    admin.update_project_status(pid, "STOPPED")
                    # অ্যাডমিনকে সাইলেন্ট ক্র্যাশ নোটিফিকেশন
                    u_info = admin.get_user(uid)
                    uname = u_info.get("username") or str(uid)
                    crash_notify = (
                        "🚨 <b>BOT PERMANENTLY CRASHED!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>User:</b> @{html.escape(str(uname))} (<code>{uid}</code>)\n"
                        f"🤖 <b>Bot:</b> <code>{html.escape(data['project_name'])}</code>\n"
                        f"🔁 <b>Restarts:</b> 3 attempts failed (exit {rc})\n"
                        f"🕒 <b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    for aid in admin.ADMIN_IDS:
                        try:
                            bot.send_message(aid, crash_notify, parse_mode="HTML")
                        except Exception:
                            pass
                    active_processes.pop(p_key, None)
            else:
                # বট স্বাভাবিক চলছে — কিউমুলেটিভ আপটাইম (Leaderboard), RAM অ্যালার্ট ও
                # আপটাইম মাইলস্টোন (৭/৩০ দিন) চেক করা হচ্ছে (প্রতি টিকে ৫ সেকেন্ড যোগ হয়)
                try:
                    admin.add_uptime_seconds(uid, 5)
                except Exception:
                    pass

                try:
                    mem_mb, _cpu = admin.get_bot_resource_usage(proc.pid)
                    ram_limit = admin.SYSTEM_CONFIG.get("max_ram_mb_per_process", 256)
                    threshold = ram_limit * 0.8
                    if mem_mb >= threshold and not data.get("ram_alert_sent", False):
                        data["ram_alert_sent"] = True
                        try:
                            bot.send_message(
                                uid,
                                "⚠️ <b>রিসোর্স অ্যালার্ট: হাই RAM ব্যবহার!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                                f"💾 <b>RAM ব্যবহার:</b> <code>{mem_mb} MB</code> / <code>{ram_limit} MB</code> ({int(mem_mb*100/ram_limit)}%)\n\n"
                                "👉 বট অপ্টিমাইজ করুন অথবা কোডে মেমরি লিক আছে কিনা যাচাই করুন।",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    elif mem_mb < threshold * 0.9:
                        data["ram_alert_sent"] = False
                except Exception:
                    pass

                try:
                    elapsed_run = now - start_t
                    for ms_days, ms_key in ((7, "UPTIME_7D"), (30, "UPTIME_30D")):
                        if elapsed_run >= ms_days * 86400 and not admin.has_milestone_sent(pid, ms_key):
                            admin.mark_milestone_sent(pid, ms_key)
                            newly = admin.award_badge(uid, ms_key)
                            badge_line = ""
                            if newly:
                                b = admin.ACHIEVEMENTS[ms_key]
                                badge_line = f"\n\n{b['emoji']} <b>নতুন ব্যাজ অর্জিত:</b> {b['title']}!"
                            try:
                                bot.send_message(
                                    uid,
                                    f"🎯 <b>আপটাইম মাইলস্টোন অর্জিত!</b>\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                                    f"⏱️ একটানা <b>{ms_days} দিন</b> সচল আছে! 🔥{badge_line}",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                except Exception:
                    pass

        # ২ক. শিডিউল্ড Start/Stop — প্রতিটি বটের নির্ধারিত সময় হলে স্বয়ংক্রিয়ভাবে চালু/বন্ধ
        try:
            now_hm = datetime.now().strftime("%H:%M")
            today_str = datetime.now().strftime("%Y-%m-%d")
            for sp in admin.get_scheduled_projects():
                s_uid = sp["user_id"]
                s_pid = sp["project_id"]
                s_key = f"{s_uid}_{s_pid}"
                start_tag = f"start_{today_str}_{sp.get('sched_start_time')}"
                stop_tag = f"stop_{today_str}_{sp.get('sched_stop_time')}"
                if sp.get("sched_start_time") == now_hm and sp.get("sched_last_action") != start_tag:
                    admin.mark_schedule_action(s_pid, start_tag)
                    if s_key not in active_processes:
                        ok, _ = execute_project_script(s_uid, s_pid)
                        if ok:
                            try:
                                bot.send_message(s_uid, f"⏰ <b>শিডিউল অনুযায়ী বট চালু হয়েছে:</b> <code>{sp['project_name']}</code>", parse_mode="HTML")
                            except Exception:
                                pass
                if sp.get("sched_stop_time") == now_hm and sp.get("sched_last_action") != stop_tag:
                    admin.mark_schedule_action(s_pid, stop_tag)
                    if s_key in active_processes:
                        stop_project_script(s_uid, s_pid)
                        try:
                            bot.send_message(s_uid, f"⏰ <b>শিডিউল অনুযায়ী বট বন্ধ হয়েছে:</b> <code>{sp['project_name']}</code>", parse_mode="HTML")
                        except Exception:
                            pass
        except Exception:
            pass

        # ২খ. অটো-ব্রডকাস্ট — এডমিনের সেট করা শিডিউল্ড মেসেজ সময়মতো পাঠানো
        try:
            due = admin.get_due_broadcasts()
            for b in due:
                sent_count = 0
                for u in admin.get_all_users():
                    try:
                        bot.send_message(u["user_id"], b["message"], parse_mode="HTML")
                        sent_count += 1
                    except Exception:
                        pass
                admin.mark_broadcast_sent(b["id"], sent_count)
        except Exception:
            pass


        # ২. পেইড সাবস্ক্রিপশন মেয়াদ অ্যালার্ট (২৪ ঘণ্টা ও ২ ঘণ্টা আগে)
        try:
            conn = admin.get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id, username, plan, plan_expiry, expiry_alert_24h, expiry_alert_2h, expiry_alert_3d FROM users WHERE plan IN ('PRO', 'PREMIUM', 'BUSINESS', 'VIP')")
            paid_users = cur.fetchall()
            now_dt = datetime.now()

            for pu in paid_users:
                exp_str = pu["plan_expiry"]
                if exp_str and exp_str != "LIFETIME":
                    try:
                        exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                        diff = exp_dt - now_dt
                        
                        # ৩ দিন আগে রিমাইন্ডার (প্রথম ও সবচেয়ে গুরুত্বপূর্ণ অ্যালার্ট)
                        if timedelta(days=0) < diff <= timedelta(days=3) and not pu["expiry_alert_3d"]:
                            cur.execute("UPDATE users SET expiry_alert_3d = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            days_left = max(1, diff.days)
                            bot.send_message(
                                pu["user_id"],
                                f"📅 <b>সাবস্ক্রিপশন রিমাইন্ডার ({days_left} দিন বাকি)!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👑 <b>প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                f"⏳ <b>মেয়াদ শেষ:</b> <code>{exp_str}</code>\n\n"
                                f"🔔 আপনার প্ল্যানের মেয়াদ মাত্র <b>{days_left} দিন</b> পরে শেষ হয়ে যাবে!\n"
                                "এখনই রিনিউ করলে মেয়াদের উপরে যোগ হবে (কোনো দিন নষ্ট হবে না)।\n\n"
                                "👉 <b>💎 Upgrade Plan</b> থেকে রিনিউ করুন।",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )
                        
                        # ২৪ ঘণ্টা আগে রিমাইন্ডার
                        if timedelta(hours=0) < diff <= timedelta(hours=24) and not pu["expiry_alert_24h"]:
                            cur.execute("UPDATE users SET expiry_alert_24h = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"🔔 <b>সাবস্ক্রিপশন রিনিউয়াল রিমাইন্ডার (২৪ ঘণ্টা বাকি)!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 <b>বর্তমান প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                f"⏳ আপনার প্ল্যানের মেয়াদ শেষ হবে: <code>{exp_str}</code>\n\n"
                                "👉 বট ২৪/৭ সচল রাখতে এখনই <b>💎 Upgrade Plan</b> থেকে রিনিউ করে নিন!",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )

                        # ২ ঘণ্টা আগে রিমাইন্ডার
                        elif timedelta(hours=0) < diff <= timedelta(hours=2) and not pu["expiry_alert_2h"]:
                            cur.execute("UPDATE users SET expiry_alert_2h = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"⚠️ <b>জরুরি নোটিশ: প্ল্যানের মেয়াদ আর মাত্র ২ ঘণ্টা বাকি!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 <b>প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                "👉 এখনই <b>💎 Upgrade Plan</b> ট্যাপ করে মেয়াদ বাড়িয়ে নিন।",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )

                        # মেয়াদ সম্পূর্ণ শেষ হলে ফ্রি প্ল্যানে নেওয়া
                        elif diff <= timedelta(seconds=0):
                            cur.execute("UPDATE users SET plan = 'FREE', plan_expiry = 'LIFETIME', expiry_alert_24h = 0, expiry_alert_2h = 0, expiry_alert_3d = 0 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"ℹ️ <b>আপনার প্রিমিয়াম সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে।</b>\nএকাউন্টটি ফ্রি প্ল্যানে ({to_bn_num(admin.SYSTEM_CONFIG.get('free_hosting_hours', 12))} ঘণ্টা/সেশন) ফিরিয়ে নেওয়া হয়েছে। ২৪/৭ চালাতে যেকোনো সময় রিনিউ করতে পারবেন।",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )
                    except Exception:
                        pass
            conn.close()
        except Exception:
            pass

threading.Thread(target=hosting_watchdog_thread, daemon=True).start()

def restore_missing_project_files():
    """DB-র ইনডেক্স দেখে প্রতিটি বটের কোড চ্যানেলের কপি থেকে ফেরত আনে।
    • ফোল্ডারে কোনো .py না থাকলে (নতুন সার্ভার): ZIP এক্সট্রাক্ট + .py/অন্য ফাইল — আপলোডের ক্রমে (পরের আপলোড আগেরটাকে ওভাররাইট করে)
    • .py আছে (ডিস্ক টিকে আছে): শুধু মিসিং সাধারণ ফাইল আনে, ZIP ছোঁয় না
    রিটার্ন: কতগুলো ফাইল ফেরত এলো।"""
    restored = 0
    try:
        rows = admin.get_latest_code_backups()      # id ASC
    except Exception:
        return 0
    by_proj = {}
    for r in rows:
        by_proj.setdefault((r["user_id"], r["project_id"]), []).append(r)
    for (uid, pid), items in by_proj.items():
        p_dir = os.path.join(PROJECTS_DIR, f"{uid}_{pid}")
        fresh = not (os.path.isdir(p_dir) and any(f.endswith(".py") for f in os.listdir(p_dir)))
        for r in items:
            try:
                fname = os.path.basename(r["file_name"] or "")
                if not fname or fname.startswith(".."):
                    continue
                is_zip = fname.lower().endswith(".zip")
                path = os.path.join(p_dir, fname)
                if is_zip and not fresh:
                    continue
                if not is_zip and os.path.exists(path) and not fresh:
                    continue
                if (r.get("size") or 0) > 20 * 1024 * 1024:
                    print(f"[CODE-RESTORE] #{pid} {fname} ২০MB-র বেশি — স্কিপ")
                    continue
                info = bot.get_file(r["file_id"])
                data = bot.download_file(info.file_path)
                os.makedirs(p_dir, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
                if is_zip:
                    safe_extract_zip(path, p_dir, strip_root=True)
                    os.remove(path)
                restored += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"[CODE-RESTORE] #{pid} {str(e)[:100]}")
    if restored:
        print(f"[CODE-RESTORE] ✅ {restored}টি ফাইল চ্যানেল থেকে ফেরত এসেছে।")
    return restored

# ======================== 🛡 RAM-গার্ড + ক্র্যাশ-লুপ প্রতিরোধ ========================
# সমস্যা: সার্ভার রিস্টার্টের পর DB-তে RUNNING থাকা সব বট (যেমন ১১টা) ১ সেকেন্ড গ্যাপে একসাথে চালু হতো + একসাথে pip ইনস্টল —
# ফ্রি হোস্টের RAM (সাধারণত ৫১২MB) ভরে OS প্রসেস কিল করত → ফোল্ডার মুছে নতুন করে রিস্টোর → আবার ১১টা চালু → আবার কিল... (৪ মিনিট পরপর লুপ)।
# সমাধান: (১) প্রতিটি বট চালুর আগে RAM দেখা, (২) একটার পর একটা চালু + pip সিরিয়াল, (৩) RAM ৯০% ছুঁলে নতুন/ফ্রি বটকে লাইনে (queue) পাঠানো,
# (৪) ঘনঘন রিস্টার্ট ধরা পড়লে 'সেফ মোড', (৫) অ্যাডমিন-নোটিশ থ্রটল।
MEM_SOFT = 0.72      # এর নিচে থাকলে নতুন বট চালু করা যায়
MEM_HARD = 0.90      # এর ওপরে গেলে গার্ড একটা বট লাইনে পাঠায়
_deferred_resume = []                 # [(user_id, project_id)] — RAM মিললে একে একে চালু হবে (DB স্ট্যাটাস RUNNING-ই থাকে)
_deferred_lock = threading.Lock()

HOST_RAM_MB = 2048     # 🖥 হোস্টের (Botkeep) RAM — বদলালে এখানে বা ENV `MEM_LIMIT_MB`-এ দিন

def _mem_limit_mb() -> int:
    """ব্যবহারযোগ্য RAM (MB): ENV `MEM_LIMIT_MB` সবার আগে; না থাকলে কন্টেইনারের সীমা, তবে HOST_RAM_MB-এর বেশি ধরা হয় না।"""
    env = os.environ.get("MEM_LIMIT_MB", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    detected = None
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                raw = f.read().strip()
            if raw and raw != "max":
                v = int(raw)
                if 0 < v < (1 << 50):
                    detected = max(128, v // (1024 * 1024))
                    break
        except Exception:
            pass
    if detected is None:
        try:
            detected = int(psutil.virtual_memory().total // (1024 * 1024))
        except Exception:
            detected = HOST_RAM_MB
    return min(detected, HOST_RAM_MB)

def _mem_used_mb() -> int:
    """হোস্ট প্রসেস + তার সব চাইল্ড (ইউজারদের বট, pip) মিলিয়ে ব্যবহৃত RAM (MB)।"""
    try:
        me = psutil.Process()
        total = me.memory_info().rss
        for c in me.children(recursive=True):
            try:
                total += c.memory_info().rss
            except Exception:
                pass
        return int(total // (1024 * 1024))
    except Exception:
        return 0

def _mem_ratio() -> float:
    lim = _mem_limit_mb() or 512
    return _mem_used_mb() / lim

def _wait_for_memory(max_wait: int = 90) -> bool:
    """RAM MEM_SOFT-এর নিচে নামা পর্যন্ত অপেক্ষা। সময় পেরোলে False।"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if _mem_ratio() < MEM_SOFT:
            return True
        time.sleep(4)
    return _mem_ratio() < MEM_SOFT

def _notice_allowed(key: str, gap: int = 1800) -> bool:
    """অ্যাডমিন-মেসেজ স্প্যাম ঠেকায়: শেষ মেসেজের gap সেকেন্ডের মধ্যে আবার পাঠায় না (টাইমস্ট্যাম্প DB-তে থাকে)।"""
    try:
        last = float(admin.SYSTEM_CONFIG.get(key) or 0)
    except Exception:
        last = 0
    now = time.time()
    if now - last < gap:
        return False
    admin.set_system_setting(key, int(now))
    return True

def _register_boot() -> int:
    """এই স্টার্ট নথিভুক্ত করে; গত ২০ মিনিটে (এটাসহ) কতবার স্টার্ট হয়েছে সেটা রিটার্ন করে।"""
    now = time.time()
    try:
        times = [float(x) for x in str(admin.SYSTEM_CONFIG.get("boot_log") or "").split(",") if x.strip()]
    except Exception:
        times = []
    times = [t for t in times if now - t < 1200][-8:] + [now]
    admin.set_system_setting("boot_log", ",".join(str(int(t)) for t in times))
    try:
        channel_backup.schedule_push(bot, DB_CHANNEL_ID, delay=20)   # লুপ-তথ্য ব্যাকআপে ঢুকুক
    except Exception:
        pass
    return len(times)

def _defer_resume(uid: int, pid: int):
    with _deferred_lock:
        if (uid, pid) not in _deferred_resume:
            _deferred_resume.append((uid, pid))

def _prepare_deps_for_resume(uid: int, pid: int):
    """নতুন সার্ভারে (pip প্যাকেজ নেই) বট চালানোর আগে একটার পর একটা ডিপেন্ডেন্সি ইনস্টল —
    নইলে ১১টা বট একসাথে ModuleNotFoundError খেয়ে ক্র্যাশ-রিস্টার্ট + pip লুপে RAM ফাটায়।"""
    try:
        p_dir = os.path.join(PROJECTS_DIR, f"{uid}_{pid}")
        req = os.path.join(p_dir, "requirements.txt")
        if os.path.exists(req):
            pip_install_requirements(req)
            return
        proj = admin.get_project_by_id(pid) or {}
        mf = proj.get("main_file")
        if mf:
            det = scan_python_imports(os.path.join(p_dir, mf))
            if det:
                install_missing_packages_sync(det)
    except Exception as e:
        print(f"[RESUME] #{pid} ডিপেন্ডেন্সি প্রস্তুতি ব্যর্থ: {str(e)[:100]}")

def _resume_one(uid: int, pid: int, fresh_server: bool) -> str:
    """রিটার্ন: 'started' | 'deferred' | 'failed' | 'skip'"""
    p_key = f"{uid}_{pid}"
    if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
        return "skip"
    if not _wait_for_memory(60):
        return "deferred"
    if fresh_server:
        _prepare_deps_for_resume(uid, pid)
        if not _wait_for_memory(60):
            return "deferred"
    ok, _ = execute_project_script(uid, pid)
    if ok:
        return "started"
    admin.update_project_status(pid, "STOPPED")
    return "failed"

def _resume_priority(r) -> tuple:
    """পেইড/২৪-৭ বট আগে, ফ্রি পরে (RAM কম পড়লে ফ্রিরাই লাইনে দাঁড়ায়)।"""
    try:
        u = admin.get_user(r["user_id"]) or {}
        proj = admin.get_project_by_id(r["project_id"]) or {}
        return (0 if admin.get_effective_slot_status(u, proj)["is_24_7"] else 1, r["project_id"])
    except Exception:
        return (1, r["project_id"])

def _deferred_resume_loop():
    """RAM ফাঁকা হলে লাইনে থাকা বটগুলো একটা একটা করে চালু করে।"""
    while True:
        time.sleep(45)
        try:
            with _deferred_lock:
                item = _deferred_resume[0] if _deferred_resume else None
            if not item:
                continue
            if _mem_ratio() >= MEM_SOFT:
                continue
            uid, pid = item
            with _deferred_lock:
                if item in _deferred_resume:
                    _deferred_resume.remove(item)
            proj = admin.get_project_by_id(pid)
            if not proj or str(proj.get("status", "")).upper() != "RUNNING":
                continue
            res = _resume_one(uid, pid, fresh_server=False)
            if res == "deferred":
                _defer_resume(uid, pid)
            time.sleep(10)
        except Exception:
            pass

def _ram_guard_loop():
    """RAM MEM_HARD ছাড়ালে OS প্রসেস কিল করার আগেই একটা বট নিজে থেকে থামিয়ে লাইনে পাঠায় (ফ্রি বট আগে, নতুনটা আগে)।
    DB স্ট্যাটাস RUNNING-ই থাকে — RAM ফাঁকা হলে নিজে থেকে আবার চালু হয়; ইউজারের কিছু করতে হয় না।"""
    while True:
        time.sleep(10)
        try:
            lim = _mem_limit_mb()
            used = _mem_used_mb()
            if used < lim * MEM_HARD:
                continue
            cands = []
            for p_key, d in list(active_processes.items()):
                try:
                    if d.get("stopping") or d["process"].poll() is not None:
                        continue
                    u = admin.get_user(d["user_id"]) or {}
                    proj = admin.get_project_by_id(d["project_id"]) or {}
                    covered = admin.get_effective_slot_status(u, proj)["is_24_7"]
                    cands.append((1 if covered else 0, -d.get("start_time", 0), p_key, d))
                except Exception:
                    pass
            if not cands:
                continue
            cands.sort(key=lambda x: (x[0], x[1]))
            _, _, p_key, d = cands[0]
            d["stopping"] = True
            try:
                d["process"].terminate()
                d["log_handle"].close()
            except Exception:
                pass
            active_processes.pop(p_key, None)
            _defer_resume(d["user_id"], d["project_id"])
            print(f"[RAM-GUARD] {used}/{lim}MB — {p_key} লাইনে পাঠানো হলো")
            if _notice_allowed("last_ram_notice_ts", 1800):
                for aid in admin.ADMIN_IDS:
                    try:
                        bot.send_message(
                            aid,
                            "🧠 <b>RAM প্রায় ভরে গেছে!</b>\n"
                            f"📊 ব্যবহার: <b>{used}/{lim} MB</b>\n"
                            "সার্ভার ক্র্যাশ ঠেকাতে কিছু বট সাময়িকভাবে লাইনে রাখা হচ্ছে — RAM ফাঁকা হলে নিজে থেকে আবার চালু হবে।\n"
                            f"⏳ লাইনে এখন: <b>{len(_deferred_resume)}</b>টি\n"
                            "💡 স্থায়ী সমাধান: হোস্টের RAM বাড়ান, নইলে একসাথে চলা বটের সংখ্যা এই সীমার মধ্যে থাকবে।",
                            parse_mode="HTML")
                    except Exception:
                        pass
            time.sleep(20)     # মেমরি নামার সময় দিন
        except Exception:
            pass

def _startup_resume_bots():
    """সার্ভার রিস্টার্ট/রিসেটের পর DB-তে RUNNING মার্ক করা বটগুলো আবার চালু করে।
    আগে সব বট ১ সেকেন্ড গ্যাপে একসাথে চালু হতো → RAM ভরে সার্ভার কিল হয়ে ক্র্যাশ-লুপ হতো। এখন:
      • পেইড বট আগে, ফ্রি পরে • প্রতিটির আগে RAM চেক • নতুন সার্ভারে ডিপেন্ডেন্সি একটার পর একটা • বটগুলোর মাঝে বিরতি
      • ২০ মিনিটে ৩+ বার স্টার্ট হলে 'সেফ মোড' (প্রথমে মাত্র ৩টা, বাকি ধীরে ধীরে)
      • জায়গা না মিললে বট লাইনে থাকে (DB-তে RUNNING), RAM ফাঁকা হলে নিজে চালু হয়"""
    time.sleep(8)   # বট পুরোপুরি আপ হওয়ার জন্য একটু অপেক্ষা
    boots = _register_boot()
    safe_mode = boots >= 3
    files_back = restore_missing_project_files()
    fresh_server = bool(CH_RESTORED or files_back)
    try:
        conn = admin.get_db_connection()
        rows = conn.execute("SELECT project_id, user_id FROM projects WHERE UPPER(status) = 'RUNNING'").fetchall()
        conn.close()
        rows = sorted([dict(r) for r in rows], key=_resume_priority)
    except Exception:
        rows = []
    started, queued, failed = 0, 0, 0
    gap = 15 if safe_mode else 6         # প্রতিটি বট চালুর পর RAM স্থির হওয়ার সময় (২GB RAM-এ কম বিরতিই যথেষ্ট)
    vip_rows = [r for r in rows if _resume_priority(r)[0] == 0]
    normal_rows = [r for r in rows if _resume_priority(r)[0] != 0]
    # ধাপ ১: VIP/24-7 বট একটা একটা করে → ধাপ ২: নরমাল (ফ্রি) বট একটা একটা করে
    for group in (vip_rows, normal_rows):
        for r in group:
            try:
                res = _resume_one(r["user_id"], r["project_id"], fresh_server)
                if res == "started":
                    started += 1
                    time.sleep(gap)
                elif res == "deferred":
                    _defer_resume(r["user_id"], r["project_id"])
                    queued += 1
                elif res == "failed":
                    failed += 1
            except Exception:
                pass
    if CH_RESTORED or started or files_back or queued:
        info = channel_backup.status.get("restored") or {}
        # একই কারণে বারবার মেসেজ স্প্যাম নয় — ৩০ মিনিটে ১টা (সেফ মোডে সবসময় ১টা জানানো হয়, কিন্তু সেটাও ৩০ মিনিটে ১টা)
        if _notice_allowed("last_boot_notice_ts", 1800):
            lim, used = _mem_limit_mb(), _mem_used_mb()
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(
                        aid,
                        (("📡 <b>চ্যানেল থেকে ডাটা রিস্টোর হয়েছে!</b>\n"
                          + f"👥 {info.get('users', '?')} ইউজার • 🤖 {info.get('projects', '?')} বট\n")
                         if CH_RESTORED else "🔄 <b>সার্ভার রিস্টার্ট হয়েছে।</b>\n")
                        + (f"📄 চ্যানেলের কোড-কপি থেকে <b>{files_back}</b>টি ফাইল ফেরত আনা হয়েছে।\n" if files_back else "")
                        + f"▶️ <b>{started}</b>টি বট আবার চালু করা হয়েছে।\n"
                        + (f"⏳ <b>{queued}</b>টি বট RAM-এর জন্য লাইনে — জায়গা মিললে নিজে চালু হবে।\n" if queued else "")
                        + (f"⚠️ <b>{failed}</b>টি বট চালু হয়নি।\n" if failed else "")
                        + f"🧠 RAM: <b>{used}/{lim} MB</b>"
                        + (f"\n\n🛡 <b>সেফ মোড:</b> ২০ মিনিটে <b>{boots}</b> বার সার্ভার রিস্টার্ট হয়েছে — সম্ভবত RAM ভরে হোস্ট কিল করছিল। "
                           "তাই বটগুলো আরও ধীরে (VIP আগে, তারপর নরমাল) একটা একটা করে চালু হচ্ছে।" if safe_mode else ""),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

threading.Thread(target=_ram_guard_loop, daemon=True, name="ram-guard").start()
threading.Thread(target=_deferred_resume_loop, daemon=True, name="resume-queue").start()
threading.Thread(target=_startup_resume_bots, daemon=True).start()

def _ch_backup_failed_notice(ok, msg):
    for aid in admin.ADMIN_IDS:
        try:
            bot.send_message(aid, f"⚠️ <b>চ্যানেল ডাটা-ব্যাকআপ ব্যর্থ!</b>\n<code>{html.escape(str(msg))}</code>", parse_mode="HTML")
        except Exception:
            pass

channel_backup.start_periodic(bot, DB_CHANNEL_ID, on_failure=_ch_backup_failed_notice)

import signal
def _graceful_shutdown(signum, frame):
    """Render ডেপ্লয়/রিস্টার্টের আগে SIGTERM পাঠায় (~৩০ সেকেন্ড সময় দিয়ে) — এই ফাঁকে সর্বশেষ DB ব্যাকআপ চ্যানেলে রেখে যাই,
    যাতে নতুন ইনস্ট্যান্স একদম আপ-টু-ডেট ডাটা ফেরত পায়।"""
    try:
        t = threading.Thread(target=channel_backup.push, args=(bot, DB_CHANNEL_ID), kwargs={"quick": True}, daemon=True)
        t.start()
        t.join(24)
    finally:
        os._exit(0)

try:
    signal.signal(signal.SIGTERM, _graceful_shutdown)
except Exception:
    pass   # মেইন থ্রেড ছাড়া অন্য জায়গা থেকে চললে সিগন্যাল বসানো যায় না — তখন শুধু স্বাভাবিক ব্যাকআপ চলবে

def daily_report_scheduler():
    """প্রতি মিনিটে চেক করে — ইউজারের সেট করা সময় হলে Daily Report পাঠায়।
    Bangladesh Time (UTC+6) ব্যবহার করা হচ্ছে।"""
    import time as _time
    sent_today = {}  # {user_id: date_str} — একই দিনে দুইবার পাঠানো এড়াতে

    while True:
        try:
            # UTC+6 = Bangladesh Time
            now_bst = datetime.utcnow()
            from datetime import timezone, timedelta as _td
            bst = now_bst + _td(hours=6)
            current_time = bst.strftime("%H:%M")
            current_date = bst.strftime("%Y-%m-%d")

            users_with_report = admin.get_users_with_daily_report()
            for u in users_with_report:
                uid = u["user_id"]
                report_time = u["daily_report_time"]
                if report_time != current_time:
                    continue
                if sent_today.get(uid) == current_date:
                    continue

                # রিপোর্ট তৈরি করা
                try:
                    projects = admin.get_user_projects(uid)
                    total = len(projects)
                    running = sum(1 for p in projects if f"{uid}_{p['project_id']}" in active_processes and active_processes[f"{uid}_{p['project_id']}"]['process'].poll() is None)
                    stopped = total - running

                    u_data = admin.get_user(uid)
                    pinfo = admin.get_plan_display_info(u_data)

                    report = (
                        f"📊 <b>Daily Report — {current_date}</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏰ <b>সময়:</b> {current_time} BST\n"
                        f"👑 <b>প্ল্যান:</b> {pinfo['icon']} {pinfo['display_name']}\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🤖 <b>মোট বট:</b> {total}টি\n"
                        f"🟢 <b>চলছে:</b> {running}টি\n"
                        f"🔴 <b>বন্ধ:</b> {stopped}টি\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    if total > 0:
                        for p in projects:
                            p_key = f"{uid}_{p['project_id']}"
                            is_run = p_key in active_processes and active_processes[p_key]['process'].poll() is None
                            status = "🟢 চলছে" if is_run else "🔴 বন্ধ"
                            report += f"• {html.escape(p['project_name'])}: {status}\n"

                    markup = types.InlineKeyboardMarkup()
                    markup.add(make_inline_button("🎛️ Control Panel", callback_data="open_cp", style="primary"))
                    bot.send_message(uid, report, reply_markup=markup, parse_mode="HTML")
                    sent_today[uid] = current_date
                except Exception:
                    pass

            # পুরনো sent_today রেকর্ড পরিষ্কার (RAM বাঁচাতে)
            if len(sent_today) > 5000:
                sent_today.clear()

        except Exception:
            pass
        _time.sleep(60)  # প্রতি মিনিটে একবার চেক

threading.Thread(target=daily_report_scheduler, daemon=True).start()


# ----------------- বোল্ড ফন্ট (𝗕𝗼𝗹𝗱) — সব বাটনে স্টাইল ফন্ট -----------------
def bold_font(text):
    """ইংরেজি অক্ষর/সংখ্যাকে বোল্ড ইউনিকোড ফন্টে রূপান্তর করে (Git → 𝗚𝗶𝘁, Slot 1 → 𝗦𝗹𝗼𝘁 𝟭)।"""
    out = []
    for ch in str(text):
        o = ord(ch)
        if 65 <= o <= 90:
            out.append(chr(0x1D5D4 + o - 65))
        elif 97 <= o <= 122:
            out.append(chr(0x1D5EE + o - 97))
        elif 48 <= o <= 57:
            out.append(chr(0x1D7EC + o - 48))
        else:
            out.append(ch)
    return "".join(out)

def unbold_font(text):
    out = []
    for ch in str(text):
        o = ord(ch)
        if 0x1D5D4 <= o <= 0x1D5ED:
            out.append(chr(65 + o - 0x1D5D4))
        elif 0x1D5EE <= o <= 0x1D607:
            out.append(chr(97 + o - 0x1D5EE))
        elif 0x1D7EC <= o <= 0x1D7F5:
            out.append(chr(48 + o - 0x1D7EC))
        else:
            out.append(ch)
    return "".join(out)

def normalize_button_text(text):
    """বোল্ড ফন্টের রিপ্লাই-বাটন চাপলে যে টেক্সট আসে সেটাকে আসল ইংরেজি লেবেলে ফিরিয়ে আনে,
    যাতে আগের সব `text == "..."` চেক আগের মতোই কাজ করে।"""
    if not text or len(text) > 60:
        return text
    if any(0x1D5D4 <= ord(c) <= 0x1D7F5 for c in text) and not text[0].isalnum():
        return unbold_font(text)
    return text

def make_kb(text, **kwargs):
    return types.KeyboardButton(bold_font(text), **kwargs)

# ----------------- 🛒 মার্কেটপ্লেস টেমপ্লেট রেজিস্ট্রি -----------------
# নতুন টেমপ্লেট যোগ করতে শুধু এই ডিকশনারিতে একটা এন্ট্রি বসালেই লিস্ট, ডিটেইলস, ডেপ্লয় সব অটো কাজ করবে।
# কোডে __TOKEN__ জায়গায় ইউজারের বট টোকেন বসে যায়।
MARKETPLACE_TEMPLATES = {
    "group_automator": {
        "emoji": '👥',
        "name": 'Premium Group Automator',
        "proj_name": 'Group Automator',
        "category": '🛡️ Group Management',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~30 sec',
        "features": ['🛡 Captcha (বাটন বা অঙ্কের) + টাইমআউটে অটো কিক', '🔗 Anti-Link (ডোমেইন Whitelist), 🚫 Word Filter, ⚡ Anti-Flood', '⚠️ Warn সিস্টেম — সীমা ও শাস্তি (মিউট/কিক/ব্যান) বাটনে সেট', '🔒 Locks: ফরওয়ার্ড, স্টিকার, ভয়েস, ফাইল, ছবি/ভিডিও', '👋 কাস্টম Welcome/Rules, 🧹 সার্ভিস মেসেজ ক্লিন', '🛠 /warn /mute /kick /ban /del /pin /trust ইত্যাদি মডারেশন কমান্ড'],
        "commands": '/settings · /rules · /warns · রিপ্লাই করে: /warn /mute 10m /kick /ban /del /trust',
        "about": 'গ্রুপ ম্যানেজমেন্ট বট — বাটন সেটিংস প্যানেল, ক্যাপচা, অ্যান্টি-স্প্যাম, লক, ওয়ার্ন সিস্টেম ও মডারেশন টুলসহ।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', 'বটকে গ্রুপে Admin করুন (Delete + Restrict পারমিশন)', 'গ্রুপে /settings লিখে বাটন থেকে সব ফিচার ঠিক করুন', 'মডারেশনে মেসেজে রিপ্লাই করে /warn /mute /kick দিন'],
        "need": 'শুধু বট টোকেন (@BotFather থেকে) + গ্রুপে Admin পারমিশন',
        "preview": '👤 Rahim joined\n🤖 👋 Welcome Rahim! ✅ বাটনে ক্লিক করে ভেরিফাই করুন\n👤 [লিংক স্প্যাম] → 🤖 ডিলিট + ⚠️ Warning 1/3',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)

from collections import deque

DB = Store('group_data.json', {'chats': {}, 'warns': {}, 'pending': {}, 'trusted': {}})
FLOOD = {}
CT_OPTS, FN_OPTS, FS_OPTS, WL_OPTS = [60, 120, 180, 300, 600], [3, 5, 7, 10], [5, 10, 15, 30], [2, 3, 4, 5, 10]
WA_OPTS = ['mute1h', 'mute24h', 'kick', 'ban']
WA_NAME = {'mute1h': '🔇 ১ ঘণ্টা মিউট', 'mute24h': '🔇 ২৪ ঘণ্টা মিউট', 'kick': '👢 কিক', 'ban': '⛔ ব্যান'}
CYCLES = {'ctime': CT_OPTS, 'cmode': ['button', 'math'], 'fn': FN_OPTS, 'fsec': FS_OPTS, 'wlimit': WL_OPTS, 'waction': WA_OPTS}
BOOLS = ['captcha', 'antilink', 'wordf', 'flood', 'lfwd', 'lsticker', 'lvoice', 'ldoc', 'lmedia', 'welcome_on', 'delserv']
DEFAULTS = {'captcha': True, 'cmode': 'button', 'ctime': 180, 'antilink': True, 'allow': [], 'wordf': False, 'words': [], 'flood': False, 'fn': 5, 'fsec': 10,
            'wlimit': 3, 'waction': 'mute1h', 'lfwd': False, 'lsticker': False, 'lvoice': False, 'ldoc': False, 'lmedia': False,
            'welcome_on': True, 'welcome': '', 'rules': '', 'delserv': False}
GROUP_CT = ['text', 'photo', 'video', 'document', 'audio', 'voice', 'video_note', 'sticker', 'animation', 'contact', 'location', 'poll']
GROUPS = ('group', 'supergroup')


def cfg(chat_id):
    c = DB.d['chats'].setdefault(str(chat_id), {})
    for k, v in DEFAULTS.items():
        if k not in c:
            c[k] = json.loads(json.dumps(v))
    return c


def wkey(chat, uid):
    return '%s:%s' % (chat, uid)


def trusted(chat, uid):
    return uid in DB.d['trusted'].get(str(chat), [])


def exempt(chat, uid):
    return is_chat_admin(chat, uid) or trusted(chat, uid)


# ---------------------------------------------------------------- telegram actions (version tolerant)
def perms(allow):
    kws = dict(can_send_messages=allow, can_send_polls=allow, can_send_other_messages=allow, can_add_web_page_previews=allow, can_invite_users=allow)
    extra = dict(can_send_audios=allow, can_send_documents=allow, can_send_photos=allow, can_send_videos=allow, can_send_video_notes=allow, can_send_voice_notes=allow)
    try:
        return types.ChatPermissions(**kws, **extra)
    except TypeError:
        try:
            return types.ChatPermissions(can_send_media_messages=allow, **kws)
        except TypeError:
            return types.ChatPermissions(**kws)


def restrict(chat, uid, allow, secs=0):
    kw = {'until_date': int(time.time() + secs)} if secs else {}
    try:
        bot.restrict_chat_member(chat, uid, permissions=perms(allow), **kw)
        return True
    except TypeError:
        try:
            bot.restrict_chat_member(chat, uid, can_send_messages=allow, can_send_media_messages=allow, can_send_other_messages=allow, can_add_web_page_previews=allow, **kw)
            return True
        except Exception as e:
            log('restrict', str(e)[:150])
    except Exception as e:
        log('restrict', str(e)[:150])
    return False


def ban(chat, uid):
    fn = getattr(bot, 'ban_chat_member', None) or getattr(bot, 'kick_chat_member')
    try:
        fn(chat, uid)
        return True
    except Exception as e:
        log('ban', str(e)[:150])
        return False


def kick(chat, uid):
    if not ban(chat, uid):
        return False
    try:
        bot.unban_chat_member(chat, uid, only_if_banned=True)
    except Exception:
        try:
            bot.unban_chat_member(chat, uid)
        except Exception:
            pass
    return True


def punish(chat, user, action):
    if action == 'mute1h':
        return restrict(chat, user.id, False, 3600), '🔇 %s ১ ঘণ্টার জন্য মিউট হয়েছে।' % mention(user)
    if action == 'mute24h':
        return restrict(chat, user.id, False, 86400), '🔇 %s ২৪ ঘণ্টার জন্য মিউট হয়েছে।' % mention(user)
    if action == 'kick':
        return kick(chat, user.id), '👢 %s গ্রুপ থেকে বের করে দেওয়া হয়েছে।' % mention(user)
    return ban(chat, user.id), '⛔ %s ব্যান হয়েছে।' % mention(user)


def add_warn(chat, user, reason, admin_by=None):
    c = cfg(chat)
    k = wkey(chat, user.id)
    DB.d['warns'][k] = DB.d['warns'].get(k, 0) + 1
    n = DB.d['warns'][k]
    if n >= c['wlimit']:
        DB.d['warns'][k] = 0
        DB.save()
        ok, txt = punish(chat, user, c['waction'])
        return say(chat, '⚠️ <b>%d/%d ওয়ার্নিং পূর্ণ</b> (%s)\n%s%s' % (n, c['wlimit'], esc(reason), txt, '' if ok else '\n⚠️ বটের পারমিশন যাচাই করুন।'))
    DB.save()
    return say(chat, '⚠️ %s — <b>Warning %d/%d</b>\n📌 কারণ: %s' % (mention(user), n, c['wlimit'], esc(reason)),
               kb([btn('↩️ Warn সরান (Admin)', 'gw:%d' % user.id)]))


@route('gw')
def cb_unwarn(c, a):
    chat = c.message.chat.id
    if not is_chat_admin(chat, c.from_user.id):
        return ack(c, '🚫 শুধু অ্যাডমিন', True)
    uid = to_int(a[0] if a else '', 0)
    k = wkey(chat, uid)
    DB.d['warns'][k] = max(0, DB.d['warns'].get(k, 0) - 1)
    DB.save()
    ack(c, '✅ ওয়ার্নিং সরানো হয়েছে')
    edit(c, '✅ ওয়ার্নিং সরানো হয়েছে (এখন %d/%d) — by %s' % (DB.d['warns'][k], cfg(chat)['wlimit'], esc(uname_of(c.from_user))), None)


# ---------------------------------------------------------------- private home
def home_view(uid):
    text = ('🛡️ <b>Premium Group Automator</b>\n' + LINE + '\nআপনার গ্রুপের জন্য অল-ইন-ওয়ান মডারেশন বট:\n'
            '• 🛡 ক্যাপচা (বাটন/ম্যাথ) • 🔗 অ্যান্টি-লিংক • 🚫 শব্দ ফিল্টার • ⚡ অ্যান্টি-ফ্লাড\n• ⚠️ ওয়ার্ন সিস্টেম • 🔒 লক • 👋 Welcome • 📜 Rules\n\n'
            '<b>শুরু করুন:</b>\n১. বটকে গ্রুপে যোগ করে <b>Admin</b> বানান (Delete + Restrict পারমিশন)\n২. গ্রুপে <code>/settings</code> লিখে বাটনে সব ঠিক করুন' + FOOTER)
    link = 'https://t.me/%s?startgroup=true&admin=delete_messages+restrict_members+pin_messages+invite_users' % me().username
    return text, kb([btn('➕ গ্রুপে যোগ করুন', url=link)], [btn('📖 কমান্ড তালিকা', 'ghelp')])


HELP = ('📖 <b>কমান্ড তালিকা</b>\n' + LINE + '\n<b>অ্যাডমিন (রিপ্লাই করে):</b>\n'
        '/warn [কারণ] · /unwarn · /warns · /resetwarns\n/mute [10m|2h|1d] · /unmute · /kick · /ban · /unban\n/del — মেসেজ মুছুন · /pin · /unpin\n/trust · /untrust — ফিল্টার ছাড়\n\n'
        '<b>অ্যাডমিন (সরাসরি):</b>\n/settings — বাটন প্যানেল\n/setrules টেক্সট · /setwelcome টেক্সট ({name} {chat})\n/allow ডোমেইন · /unallow ডোমেইন\n/addword শব্দ · /delword শব্দ · /words\n\n'
        '<b>সবার জন্য:</b> /rules · /warns')


@route('ghelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


@bot.message_handler(commands=['start', 'menu'])
def start(m):
    if m.chat.type == 'private':
        clear_state(m.from_user.id)
        t, k = home_view(m.from_user.id)
        return say(m.chat.id, t, k)
    say(m.chat.id, '🛡️ <b>Group Automator সক্রিয়!</b>\nঅ্যাডমিনরা <code>/settings</code> লিখে সব কিছু বাটনে ঠিক করুন।')


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()) if m.chat.type == 'private' else None)


@bot.my_chat_member_handler()
def on_added(upd):
    try:
        if upd.chat.type in GROUPS and upd.new_chat_member.status in ('member', 'administrator') and upd.old_chat_member.status in ('left', 'kicked'):
            cfg(upd.chat.id)
            DB.save()
            say(upd.chat.id, '🛡️ <b>ধন্যবাদ!</b> আমাকে <b>Admin</b> বানান (Delete messages + Restrict members) যাতে সব ফিচার কাজ করে।\nতারপর অ্যাডমিনরা <code>/settings</code> লিখুন।')
    except Exception as e:
        log('on_added', repr(e))


# ---------------------------------------------------------------- settings panels
def bot_status(chat):
    try:
        m_ = bot.get_chat_member(chat, me().id)
    except Exception:
        return '⚠️ বটের অবস্থা জানা যায়নি'
    if m_.status != 'administrator':
        return '⚠️ <b>বট Admin নয়</b> — Admin বানান নইলে ফিচার কাজ করবে না'
    miss = [n for n, f in (('Delete', 'can_delete_messages'), ('Restrict', 'can_restrict_members')) if getattr(m_, f, None) is False]
    return '✅ বট Admin' + ((' — ⚠️ পারমিশন নেই: ' + ', '.join(miss)) if miss else ' (পারমিশন ঠিক আছে)')


def onoff(v):
    return '✅ ON' if v else '❌ OFF'


def bk(panel='main'):
    return [btn('◀️ Back', 'gs:' + panel), btn('❌ Close', 'gs:x')]


def toggle_btn(label, key, c, panel):
    return btn('%s %s' % (label, onoff(c[key])), 'gt:%s:%s' % (key, panel))


def cycle_btn(label, key, c, panel, fmt=None):
    v = c[key]
    return btn('%s %s' % (label, fmt(v) if fmt else v), 'gc:%s:%s' % (key, panel))


def panel(chat, name):
    c = cfg(chat)
    if name == 'cap':
        text = '🛡 <b>Captcha</b>\n' + LINE + '\nনতুন সদস্যকে মিউট করে ভেরিফাই করানো হয়। সময়ে না করলে কিক।\n\nমোড: <b>%s</b>\nসময়: <b>%d সেকেন্ড</b>' % ('বাটন' if c['cmode'] == 'button' else 'ম্যাথ (যোগ)', c['ctime'])
        rows = [[toggle_btn('🛡 Captcha', 'captcha', c, 'cap')], [cycle_btn('🧩 মোড:', 'cmode', c, 'cap', lambda v: 'বাটন' if v == 'button' else 'ম্যাথ'), cycle_btn('⏱', 'ctime', c, 'cap', lambda v: '%ds' % v)], bk()]
    elif name == 'lnk':
        text = '🔗 <b>Anti-Link</b>\n' + LINE + '\nঅ্যাডমিন/Trusted ছাড়া কেউ লিংক দিলে মেসেজ মুছে ওয়ার্ন হবে।\n\n✅ অনুমোদিত ডোমেইন/লিংক: <b>%d</b> টি' % len(c['allow'])
        rows = [[toggle_btn('🔗 Anti-Link', 'antilink', c, 'lnk')], [btn('✅ অনুমোদিত তালিকা (%d)' % len(c['allow']), 'gl:a')], bk()]
    elif name == 'wrd':
        text = '🚫 <b>Word Filter</b>\n' + LINE + '\nনিষিদ্ধ শব্দ থাকলে মেসেজ মুছে ওয়ার্ন হবে।\n\n🔤 শব্দ: <b>%d</b> টি' % len(c['words'])
        rows = [[toggle_btn('🚫 Word Filter', 'wordf', c, 'wrd')], [btn('📝 শব্দ তালিকা (%d)' % len(c['words']), 'gl:w')], bk()]
    elif name == 'fld':
        text = '⚡ <b>Anti-Flood</b>\n' + LINE + '\n%d সেকেন্ডে %d টির বেশি মেসেজ দিলে ১০ মিনিট মিউট।' % (c['fsec'], c['fn'])
        rows = [[toggle_btn('⚡ Anti-Flood', 'flood', c, 'fld')], [cycle_btn('💬 মেসেজ:', 'fn', c, 'fld'), cycle_btn('⏱ সেকেন্ড:', 'fsec', c, 'fld')], bk()]
    elif name == 'wrn':
        text = '⚠️ <b>Warn System</b>\n' + LINE + '\n%d টি ওয়ার্নিং পূর্ণ হলে: <b>%s</b>' % (c['wlimit'], WA_NAME[c['waction']])
        rows = [[cycle_btn('🔢 লিমিট:', 'wlimit', c, 'wrn'), cycle_btn('⚙️ একশন:', 'waction', c, 'wrn', lambda v: WA_NAME[v])], bk()]
    elif name == 'lck':
        text = '🔒 <b>Locks</b>\n' + LINE + '\nঅ্যাডমিন/Trusted ছাড়া এসব মেসেজ পাঠানো যাবে না (মেসেজ মুছে যাবে)।'
        rows = [[toggle_btn('↪️ ফরওয়ার্ড', 'lfwd', c, 'lck'), toggle_btn('🎭 স্টিকার/GIF', 'lsticker', c, 'lck')],
                [toggle_btn('🎤 ভয়েস/ভিডিও নোট', 'lvoice', c, 'lck'), toggle_btn('📄 ফাইল/অডিও', 'ldoc', c, 'lck')],
                [toggle_btn('🖼 ছবি/ভিডিও', 'lmedia', c, 'lck')], bk()]
    elif name == 'wel':
        text = '👋 <b>Welcome</b>\n' + LINE + '\n<code>{name}</code> ও <code>{chat}</code> ব্যবহার করা যায়।\n\n<b>বর্তমান:</b>\n%s' % (esc(c['welcome']) if c['welcome'] else 'ডিফল্ট: 👋 স্বাগতম {name}!')
        rows = [[toggle_btn('👋 Welcome', 'welcome_on', c, 'wel')], [btn('✏️ টেক্সট সেট', 'gi:wel'), btn('👁 প্রিভিউ', 'gp:wel')], [btn('♻️ ডিফল্ট', 'gp:welr')], bk()]
    elif name == 'rul':
        text = '📜 <b>Rules</b>\n' + LINE + '\n%s' % (esc(c['rules']) if c['rules'] else 'এখনো কোনো নিয়ম সেট করা হয়নি।')
        rows = [[btn('✏️ রুলস সেট', 'gi:rul'), btn('🗑 মুছুন', 'gp:rulr')], bk()]
    else:
        text = ('⚙️ <b>Group Settings</b>\n' + LINE + '\n%s\n\n🛡 Captcha: %s (%s)\n🔗 Anti-Link: %s\n🚫 Word Filter: %s\n⚡ Anti-Flood: %s\n⚠️ Warn: %d → %s\n🔒 Locks: %d চালু\n👋 Welcome: %s   🧹 সার্ভিস মেসেজ মোছা: %s'
                % (bot_status(chat), onoff(c['captcha']), 'বাটন' if c['cmode'] == 'button' else 'ম্যাথ', onoff(c['antilink']), onoff(c['wordf']), onoff(c['flood']), c['wlimit'], WA_NAME[c['waction']],
                   len([1 for k in ('lfwd', 'lsticker', 'lvoice', 'ldoc', 'lmedia') if c[k]]), onoff(c['welcome_on']), onoff(c['delserv'])))
        rows = [[btn('🛡 Captcha', 'gs:cap'), btn('🔗 Anti-Link', 'gs:lnk')], [btn('🚫 Word Filter', 'gs:wrd'), btn('⚡ Anti-Flood', 'gs:fld')],
                [btn('⚠️ Warn System', 'gs:wrn'), btn('🔒 Locks', 'gs:lck')], [btn('👋 Welcome', 'gs:wel'), btn('📜 Rules', 'gs:rul')],
                [toggle_btn('🧹 সার্ভিস মেসেজ মুছুন', 'delserv', c, 'main')], [btn('❌ Close', 'gs:x')]]
    return text, kb(*rows)


def admin_cb(c):
    if c.message.chat.type not in GROUPS:
        ack(c, 'এটি গ্রুপের জন্য', True)
        return False
    if not is_chat_admin(c.message.chat.id, c.from_user.id):
        ack(c, '🚫 শুধু গ্রুপ অ্যাডমিন', True)
        return False
    return True


@route('gs')
def cb_settings(c, a):
    if not admin_cb(c):
        return
    name = a[0] if a else 'main'
    if name == 'x':
        ack(c)
        return edit(c, '⚙️ সেটিংস প্যানেল বন্ধ করা হয়েছে। আবার খুলতে /settings', None)
    ack(c)
    t, k = panel(c.message.chat.id, name)
    edit(c, t, k)


@route('gt')
def cb_toggle(c, a):
    if not admin_cb(c) or len(a) < 2 or a[0] not in BOOLS:
        return
    cf = cfg(c.message.chat.id)
    cf[a[0]] = not cf[a[0]]
    DB.save()
    ack(c, '%s: %s' % (a[0], 'ON' if cf[a[0]] else 'OFF'))
    t, k = panel(c.message.chat.id, a[1])
    edit(c, t, k)


@route('gc')
def cb_cycle(c, a):
    if not admin_cb(c) or len(a) < 2 or a[0] not in CYCLES:
        return
    cf = cfg(c.message.chat.id)
    opts = CYCLES[a[0]]
    cf[a[0]] = opts[(opts.index(cf[a[0]]) + 1) % len(opts)] if cf[a[0]] in opts else opts[0]
    DB.save()
    ack(c)
    t, k = panel(c.message.chat.id, a[1])
    edit(c, t, k)


@route('gl')
def cb_lists(c, a):
    if not admin_cb(c) or not a or a[0] not in ('a', 'w'):
        return
    chat = c.message.chat.id
    cf = cfg(chat)
    key, title, back = ('allow', '✅ অনুমোদিত ডোমেইন/লিংক', 'lnk') if a[0] == 'a' else ('words', '🚫 নিষিদ্ধ শব্দ', 'wrd')
    if len(a) >= 3 and a[1] == 'd':
        i = to_int(a[2], -1)
        if 0 <= i < len(cf[key]):
            cf[key].pop(i)
            DB.save()
            ack(c, '🗑 মুছে ফেলা হয়েছে')
    else:
        ack(c)
    rows = [[btn('🗑 %s' % x[:30], 'gl:%s:d:%d' % (a[0], i))] for i, x in enumerate(cf[key][:15])]
    rows.append([btn('➕ যোগ করুন', 'gi:%s' % ('al' if a[0] == 'a' else 'wd'))])
    rows.append(bk(back))
    edit(c, '%s (%d)\nমুছতে চাপুন।' % (title, len(cf[key])), kb(*rows))


@route('gi')
def cb_input(c, a):
    if not admin_cb(c) or not a:
        return
    what = a[0]
    prompts = {'wel': '✏️ নতুন <b>Welcome টেক্সট</b> এই গ্রুপে লিখুন ({name}, {chat} চলবে):', 'rul': '✏️ <b>গ্রুপ রুলস</b> এই গ্রুপে লিখুন:',
               'al': '✅ যে <b>ডোমেইন/লিংক</b> অনুমোদন করবেন তা লিখুন (যেমন <code>youtube.com</code> বা <code>t.me/mychannel</code>):', 'wd': '🚫 নিষিদ্ধ <b>শব্দ</b> লিখুন (একাধিক হলে কমা দিয়ে):'}
    if what not in prompts:
        return
    ack(c)
    set_state(c.from_user.id, 'gi', chat=c.message.chat.id, what=what)
    say(c.message.chat.id, prompts[what] + '\n<i>(%s — পরের মেসেজটি ইনপুট হিসেবে নেওয়া হবে; বাতিল: /cancel)</i>' % esc(uname_of(c.from_user)))


@route('gp')
def cb_preview(c, a):
    if not admin_cb(c) or not a:
        return
    chat = c.message.chat.id
    cf = cfg(chat)
    if a[0] == 'wel':
        ack(c)
        return say(chat, welcome_text(cf, c.from_user, c.message.chat.title))
    if a[0] == 'welr':
        cf['welcome'] = ''
        DB.save()
        ack(c, '♻️ ডিফল্টে ফেরানো হয়েছে')
        t, k = panel(chat, 'wel')
        return edit(c, t, k)
    if a[0] == 'rulr':
        cf['rules'] = ''
        DB.save()
        ack(c, '🗑 মুছে ফেলা হয়েছে')
        t, k = panel(chat, 'rul')
        return edit(c, t, k)


@bot.message_handler(commands=['settings'])
def settings_cmd(m):
    if m.chat.type not in GROUPS:
        return say(m.chat.id, '⚙️ <code>/settings</code> গ্রুপে ব্যবহার করুন।')
    if not is_chat_admin(m.chat.id, m.from_user.id):
        return say(m.chat.id, '🚫 শুধু গ্রুপ অ্যাডমিন সেটিংস দেখতে পারেন।')
    t, k = panel(m.chat.id, 'main')
    say(m.chat.id, t, k)


# ---------------------------------------------------------------- join / captcha
def welcome_text(cf, user, title):
    w = cf['welcome'] or '👋 স্বাগতম {name}!'
    return w.replace('{name}', mention(user)).replace('{chat}', esc(title or ''))


def send_welcome(chat, user, title):
    cf = cfg(chat)
    if not cf['welcome_on']:
        return
    rows = [[btn('📜 নিয়মাবলী', 'gr')]] if cf['rules'] else []
    say(chat, welcome_text(cf, user, title), kb(*rows) if rows else None)


@route('gr')
def cb_rules(c, a):
    r = cfg(c.message.chat.id)['rules']
    ack(c)
    say(c.message.chat.id, '📜 <b>গ্রুপ রুলস</b>\n\n' + (esc(r) if r else 'কোনো রুলস সেট করা নেই।'))


@bot.message_handler(content_types=['new_chat_members'])
def on_join(m):
    chat = m.chat.id
    cf = cfg(chat)
    if cf['delserv']:
        delete_msg(chat, m.message_id)
    for u in m.new_chat_members:
        if u.id == me().id or u.is_bot:
            continue
        if not cf['captcha'] or exempt(chat, u.id):
            send_welcome(chat, u, m.chat.title)
            continue
        if not restrict(chat, u.id, False):
            send_welcome(chat, u, m.chat.title)
            say(chat, '⚠️ ক্যাপচা কাজ করছে না — বটকে <b>Restrict members</b> পারমিশন দিন।')
            continue
        if cf['cmode'] == 'math':
            x, y = random.randint(2, 9), random.randint(2, 9)
            opts = {x + y}
            while len(opts) < 4:
                opts.add(random.randint(4, 20))
            opts = list(opts)
            random.shuffle(opts)
            rows = [[btn(str(o), 'gm:%d:%d' % (u.id, o)) for o in opts]]
            text = '👋 %s\n\n🧮 <b>%d + %d = ?</b>\n⏳ %d সেকেন্ডের মধ্যে সঠিক উত্তরে চাপ দিন, নইলে বের করে দেওয়া হবে।' % (mention(u), x, y, cf['ctime'])
            ans = x + y
        else:
            rows = [[btn('✅ আমি রোবট না', 'gv:%d' % u.id)]]
            text = '👋 স্বাগতম %s!\n\n⏳ %d সেকেন্ডের মধ্যে নিচের বাটনে ক্লিক করে ভেরিফাই করুন, নাহলে গ্রুপ থেকে বের করে দেওয়া হবে।' % (mention(u), cf['ctime'])
            ans = None
        r = say(chat, text, kb(*rows))
        DB.d['pending'][wkey(chat, u.id)] = {'exp': time.time() + cf['ctime'], 'mid': r.message_id if r else None, 'ans': ans, 'tries': 0, 'title': m.chat.title or ''}
    DB.save()


def verified(c, chat, uid, p):
    DB.d['pending'].pop(wkey(chat, uid), None)
    DB.save()
    restrict(chat, uid, True)
    ack(c, '✅ ভেরিফাই সফল!')
    delete_msg(chat, c.message.message_id)
    send_welcome(chat, c.from_user, p.get('title'))


@route('gv')
def cb_verify(c, a):
    uid = to_int(a[0] if a else '', 0)
    if c.from_user.id != uid:
        return ack(c, '⚠️ এই বাটন আপনার জন্য নয়!', True)
    chat = c.message.chat.id
    p = DB.d['pending'].get(wkey(chat, uid))
    if not p:
        return ack(c, 'ভেরিফিকেশনের মেয়াদ শেষ বা সম্পন্ন', True)
    verified(c, chat, uid, p)


@route('gm')
def cb_math(c, a):
    uid = to_int(a[0] if a else '', 0)
    if c.from_user.id != uid:
        return ack(c, '⚠️ এই বাটন আপনার জন্য নয়!', True)
    chat = c.message.chat.id
    p = DB.d['pending'].get(wkey(chat, uid))
    if not p or p['ans'] is None:
        return ack(c, 'ভেরিফিকেশনের মেয়াদ শেষ বা সম্পন্ন', True)
    if to_int(a[1] if len(a) > 1 else '', -1) == p['ans']:
        return verified(c, chat, uid, p)
    p['tries'] += 1
    if p['tries'] >= 2:
        DB.d['pending'].pop(wkey(chat, uid), None)
        DB.save()
        ack(c, '❌ দুইবার ভুল — বের করে দেওয়া হলো', True)
        kick(chat, uid)
        return delete_msg(chat, c.message.message_id)
    DB.save()
    ack(c, '❌ ভুল উত্তর! আর একবার সুযোগ আছে।', True)


def captcha_tick():
    now_t = time.time()
    changed = False
    for k, p in list(DB.d['pending'].items()):
        if p['exp'] <= now_t:
            chat, uid = [int(x) for x in k.split(':')]
            kick(chat, uid)
            if p.get('mid'):
                delete_msg(chat, p['mid'])
            DB.d['pending'].pop(k, None)
            changed = True
    for k, dq in list(FLOOD.items()):
        if not dq or now_t - dq[-1] > 120:
            FLOOD.pop(k, None)
    if changed:
        DB.save()


@bot.message_handler(content_types=['left_chat_member'])
def on_left(m):
    if cfg(m.chat.id)['delserv']:
        delete_msg(m.chat.id, m.message_id)


# ---------------------------------------------------------------- moderation commands
def target_of(m):
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    return None


def parse_secs(arg):
    secs = parse_duration(arg.split()[0]) if arg else None
    return secs


ADMIN_CMDS = ['warn', 'unwarn', 'resetwarns', 'mute', 'unmute', 'kick', 'ban', 'unban', 'del', 'pin', 'unpin', 'trust', 'untrust']


@bot.message_handler(commands=ADMIN_CMDS)
def mod_cmd(m):
    if m.chat.type not in GROUPS:
        return say(m.chat.id, '⚠️ এই কমান্ড গ্রুপে ব্যবহার করুন।')
    chat = m.chat.id
    if not is_chat_admin(chat, m.from_user.id):
        return say(m.chat.id, '⚠️ শুধু গ্রুপ অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।', reply_to=m.message_id)
    cmd = m.text.split()[0][1:].split('@')[0].lower()
    arg = cmd_arg(m)
    t = target_of(m)
    if cmd == 'unban':
        uid = to_int(arg.split()[0], None) if arg else (t.id if t else None)
        if uid is None:
            return say(chat, '⚠️ ব্যবহার: <code>/unban ইউজার_আইডি</code> বা কারো মেসেজে রিপ্লাই করুন।')
        try:
            bot.unban_chat_member(chat, uid, only_if_banned=True)
        except Exception:
            return say(chat, '⚠️ আনব্যান করা যায়নি।')
        return say(chat, '✅ <code>%d</code> আনব্যান হয়েছে।' % uid)
    if cmd in ('pin', 'unpin') and not t:
        return say(chat, '⚠️ যে মেসেজ %s করবেন তাতে রিপ্লাই করে কমান্ড দিন।' % ('পিন' if cmd == 'pin' else 'আনপিন'), reply_to=m.message_id)
    if cmd == 'pin':
        try:
            bot.pin_chat_message(chat, m.reply_to_message.message_id)
            return say(chat, '📌 মেসেজ পিন করা হয়েছে।')
        except Exception:
            return say(chat, '⚠️ পিন করা যায়নি — বটের Pin পারমিশন লাগবে।')
    if cmd == 'unpin':
        try:
            bot.unpin_chat_message(chat, m.reply_to_message.message_id)
            return say(chat, '📌 আনপিন করা হয়েছে।')
        except Exception:
            return say(chat, '⚠️ আনপিন করা যায়নি।')
    if cmd == 'del':
        if not t:
            return say(chat, '⚠️ যে মেসেজ মুছবেন তাতে রিপ্লাই করে <code>/del</code> দিন।', reply_to=m.message_id)
        delete_msg(chat, m.reply_to_message.message_id)
        return delete_msg(chat, m.message_id)
    if not t:
        return say(chat, '⚠️ যাকে এই একশন দিতে চান তার মেসেজে রিপ্লাই করে কমান্ডটি দিন।', reply_to=m.message_id)
    if t.id == me().id or is_chat_admin(chat, t.id) and cmd in ('warn', 'mute', 'kick', 'ban'):
        return say(chat, '🚫 অ্যাডমিন/বটের ওপর এই একশন নেওয়া যায় না।', reply_to=m.message_id)
    if cmd == 'warn':
        return add_warn(chat, t, arg or 'অ্যাডমিন ওয়ার্নিং')
    if cmd == 'unwarn':
        k = wkey(chat, t.id)
        DB.d['warns'][k] = max(0, DB.d['warns'].get(k, 0) - 1)
        DB.save()
        return say(chat, '✅ %s — Warning %d/%d' % (mention(t), DB.d['warns'][k], cfg(chat)['wlimit']))
    if cmd == 'resetwarns':
        DB.d['warns'][wkey(chat, t.id)] = 0
        DB.save()
        return say(chat, '✅ %s-এর সব ওয়ার্নিং মুছে ফেলা হয়েছে।' % mention(t))
    if cmd == 'mute':
        secs = parse_secs(arg)
        ok = restrict(chat, t.id, False, secs or 0)
        return say(chat, ('🔇 %s মিউট হয়েছে%s।' % (mention(t), (' (%s)' % fmt_dur(secs)) if secs else ' (অনির্দিষ্ট কালের জন্য)')) if ok else '⚠️ মিউট করা যায়নি — বট অ্যাডমিন কিনা চেক করুন।')
    if cmd == 'unmute':
        ok = restrict(chat, t.id, True)
        return say(chat, ('🔊 %s আনমিউট হয়েছে।' % mention(t)) if ok else '⚠️ আনমিউট করা যায়নি।')
    if cmd == 'kick':
        return say(chat, ('👢 %s গ্রুপ থেকে বের করে দেওয়া হয়েছে।' % mention(t)) if kick(chat, t.id) else '⚠️ কিক করা যায়নি — বট অ্যাডমিন কিনা চেক করুন।')
    if cmd == 'ban':
        return say(chat, ('⛔ %s ব্যান হয়েছে।' % mention(t)) if ban(chat, t.id) else '⚠️ ব্যান করা যায়নি — বট অ্যাডমিন কিনা চেক করুন।')
    if cmd in ('trust', 'untrust'):
        lst = DB.d['trusted'].setdefault(str(chat), [])
        if cmd == 'trust' and t.id not in lst:
            lst.append(t.id)
        if cmd == 'untrust' and t.id in lst:
            lst.remove(t.id)
        DB.save()
        return say(chat, ('🤝 %s এখন Trusted (লিংক/ফ্লাড ফিল্টার প্রযোজ্য নয়)।' if cmd == 'trust' else '🔒 %s আর Trusted নয়।') % mention(t))


@bot.message_handler(commands=['warns'])
def warns_cmd(m):
    if m.chat.type not in GROUPS:
        return
    t = target_of(m) or m.from_user
    say(m.chat.id, '⚠️ %s — Warning %d/%d' % (mention(t), DB.d['warns'].get(wkey(m.chat.id, t.id), 0), cfg(m.chat.id)['wlimit']))


@bot.message_handler(commands=['rules'])
def rules_cmd(m):
    r = cfg(m.chat.id)['rules']
    say(m.chat.id, '📜 <b>গ্রুপ রুলস</b>\n\n' + (esc(r) if r else 'এখনো কোনো রুলস সেট করা হয়নি।'))


def list_edit(m, key, add, what):
    if m.chat.type not in GROUPS or not is_chat_admin(m.chat.id, m.from_user.id):
        return say(m.chat.id, '⚠️ শুধু গ্রুপ অ্যাডমিন।')
    arg = cmd_arg(m).strip().lower()
    cf = cfg(m.chat.id)
    if not arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/%s %s</code>' % (m.text.split()[0][1:].split('@')[0], what))
    for item in [x.strip() for x in arg.split(',') if x.strip()]:
        if add and item not in cf[key]:
            cf[key].append(item[:60])
        if not add and item in cf[key]:
            cf[key].remove(item)
    DB.save()
    say(m.chat.id, '✅ আপডেট হয়েছে (মোট %d টি)।' % len(cf[key]))


@bot.message_handler(commands=['allow'])
def allow_cmd(m):
    list_edit(m, 'allow', True, 'youtube.com')


@bot.message_handler(commands=['unallow'])
def unallow_cmd(m):
    list_edit(m, 'allow', False, 'youtube.com')


@bot.message_handler(commands=['addword'])
def addword_cmd(m):
    list_edit(m, 'words', True, 'শব্দ')


@bot.message_handler(commands=['delword'])
def delword_cmd(m):
    list_edit(m, 'words', False, 'শব্দ')


@bot.message_handler(commands=['words'])
def words_cmd(m):
    if m.chat.type in GROUPS and is_chat_admin(m.chat.id, m.from_user.id):
        say(m.chat.id, '🚫 <b>নিষিদ্ধ শব্দ:</b> ' + (esc(', '.join(cfg(m.chat.id)['words'])) or 'নেই'))


@bot.message_handler(commands=['setrules', 'setwelcome'])
def set_text_cmd(m):
    if m.chat.type not in GROUPS or not is_chat_admin(m.chat.id, m.from_user.id):
        return say(m.chat.id, '⚠️ শুধু গ্রুপ অ্যাডমিন।')
    arg = cmd_arg(m)
    key = 'rules' if m.text.startswith('/setrules') else 'welcome'
    if not arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/%s টেক্সট</code>' % m.text.split()[0][1:].split('@')[0])
    cfg(m.chat.id)[key] = arg[:1500]
    DB.save()
    say(m.chat.id, '✅ সেট হয়েছে।')


# ---------------------------------------------------------------- automatic moderation
def is_forward(m):
    return bool(getattr(m, 'forward_origin', None) or getattr(m, 'forward_date', None) or getattr(m, 'forward_from', None) or getattr(m, 'forward_from_chat', None))


def link_tokens(m):
    text = m.text or m.caption or ''
    low = text.lower()
    toks = re.findall(r'(?:https?://|www\.|t\.me/|telegram\.me/|tg://)\S+', low)
    for e in (m.entities or m.caption_entities or []):
        if e.type == 'text_link' and e.url:
            toks.append(e.url.lower())
    return toks


def bad_word(text, words):
    low = text.lower()
    for w in words:
        if w and w in low:
            return w
    return None


def flood_hit(chat, uid, cf):
    dq = FLOOD.setdefault((chat, uid), deque(maxlen=20))
    now_t = time.time()
    dq.append(now_t)
    recent = [t for t in dq if now_t - t <= cf['fsec']]
    if len(recent) > cf['fn']:
        dq.clear()
        return True
    return False


def notice(chat, text):
    r = say(chat, text)
    if r is not None:
        delete_later(chat, [r.message_id], 15)


@bot.message_handler(func=lambda m: m.chat.type in GROUPS, content_types=GROUP_CT)
def on_group(m):
    chat, uid = m.chat.id, m.from_user.id
    s = get_state(uid)
    if s and s['k'] == 'gi' and s['d']['chat'] == chat and m.text and not m.text.startswith('/') and is_chat_admin(chat, uid):
        return handle_input(m, s)
    if m.text and m.text.startswith('/'):
        return
    if exempt(chat, uid):
        return
    cf = cfg(chat)
    locks = (('lfwd', is_forward(m), 'ফরওয়ার্ড মেসেজ'), ('lsticker', bool(m.sticker or m.animation), 'স্টিকার/GIF'), ('lvoice', bool(m.voice or m.video_note), 'ভয়েস/ভিডিও নোট'),
             ('ldoc', bool(m.document or m.audio), 'ফাইল/অডিও'), ('lmedia', bool(m.photo or m.video), 'ছবি/ভিডিও'))
    for key, hit, label in locks:
        if cf[key] and hit:
            delete_msg(chat, m.message_id)
            return notice(chat, '🔒 %s, এই গ্রুপে %s পাঠানো নিষেধ।' % (mention(m.from_user), label))
    text = m.text or m.caption or ''
    if cf['antilink']:
        toks = link_tokens(m)
        if toks and not all(any(a in t for a in cf['allow']) for t in toks):
            delete_msg(chat, m.message_id)
            return add_warn(chat, m.from_user, 'লিংক পাঠানো নিষেধ')
    if cf['wordf'] and text:
        w = bad_word(text, cf['words'])
        if w:
            delete_msg(chat, m.message_id)
            return add_warn(chat, m.from_user, 'নিষিদ্ধ শব্দ')
    if cf['flood'] and flood_hit(chat, uid, cf):
        if restrict(chat, uid, False, 600):
            notice(chat, '⚡ %s অতিরিক্ত দ্রুত মেসেজ দেওয়ায় ১০ মিনিট মিউট হয়েছে।' % mention(m.from_user))


def handle_input(m, s):
    chat, what, txt = s['d']['chat'], s['d']['what'], m.text.strip()
    cf = cfg(chat)
    clear_state(m.from_user.id)
    if what == 'wel':
        cf['welcome'] = txt[:1500]
        msg, back = '✅ Welcome টেক্সট সেট হয়েছে।', 'wel'
    elif what == 'rul':
        cf['rules'] = txt[:1500]
        msg, back = '✅ রুলস সেট হয়েছে।', 'rul'
    elif what == 'al':
        cf['allow'] = list(dict.fromkeys(cf['allow'] + [x.strip().lower()[:60] for x in txt.split(',') if x.strip()]))[:40]
        msg, back = '✅ অনুমোদিত তালিকা আপডেট হয়েছে।', 'lnk'
    else:
        cf['words'] = list(dict.fromkeys(cf['words'] + [x.strip().lower()[:40] for x in txt.split(',') if x.strip()]))[:100]
        msg, back = '✅ শব্দ তালিকা আপডেট হয়েছে।', 'wrd'
    DB.save()
    delete_msg(chat, m.message_id)
    t, k = panel(chat, back)
    say(chat, msg + '\n\n' + t, k)


start_worker(captcha_tick, 15)
run(commands=[('start', 'বট চালু'), ('settings', 'গ্রুপ সেটিংস (অ্যাডমিন)'), ('rules', 'গ্রুপ রুলস'), ('warns', 'ওয়ার্নিং দেখুন'), ('help', 'কমান্ড তালিকা')],
    allowed=['message', 'callback_query', 'my_chat_member'])
''',
    },
    "file_locker": {
        "emoji": '🔒',
        "name": 'Advanced File Locker Bot',
        "proj_name": 'File Locker Bot',
        "category": '🔐 Security & Storage',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~25 sec',
        "features": ['ফাইল/ছবি/ভিডিও/ভয়েস/টেক্সট — একসাথে ১০টি আইটেম এক লকে', '⏳ মেয়াদ, 👁 ভিউ-লিমিট, 🧨 আনলকের পর অটো-ডিলিট, 🚫 ফরওয়ার্ড/সেভ ব্লক', '🛡 ৩ বার ভুল পাসওয়ার্ডে ১০ মিনিট ব্লক + মালিককে সতর্কতা', '🔗 শেয়ার লিংক (পাসওয়ার্ড লাগবে) ও 🔔 আনলক নোটিফিকেশন', '📂 My Locks — মেয়াদ/ভিউ বাড়ানো, পাসওয়ার্ড বদল, ডিলিট', 'পাসওয়ার্ড সল্টেড হ্যাশে সেভ, মেসেজ অটো-ডিলিট'],
        "commands": 'ক্যাপশনে /lock পাসওয়ার্ড [মিনিট] [ভিউ] · /unlock ID পাসওয়ার্ড',
        "about": 'বাটন-চালিত ফাইল লকার — পাসওয়ার্ড, মেয়াদ, ভিউ-লিমিট, অটো-ডিলিট ও ফরওয়ার্ড-প্রটেকশনসহ নিরাপদ শেয়ারিং।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', '/start → 🔒 নতুন Lock, ফাইল পাঠিয়ে ✅ Done চাপুন', 'পাসওয়ার্ড দিন, মেয়াদ/ভিউ/অটো-ডিলিট বেছে 🔒 Lock করুন', 'লিংক বা ID + পাসওয়ার্ড শেয়ার করুন', '📂 My Locks থেকে ম্যানেজ করুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 [photo] /lock 1234 30 2\n🤖 🔒 লক! ID: K7X２QA (৩০ মিনিট, ২ বার দেখা যাবে)\n👤 /unlock K7X2QA 1234\n🤖 [photo] ✅ আনলক সফল! (বাকি ১ বার)',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('locks.json', {'locks': {}})
FAILS = {}
EXPIRY = [(0, '♾ কখনো না'), (600, '10 মিনিট'), (3600, '1 ঘণ্টা'), (86400, '24 ঘণ্টা'), (604800, '7 দিন'), (2592000, '30 দিন')]
VIEWS = [(0, '♾ আনলিমিটেড'), (1, '1 বার'), (3, '3 বার'), (5, '5 বার'), (10, '10 বার')]
AUTODEL = [(0, 'বন্ধ'), (30, '30 সেকেন্ড'), (60, '1 মিনিট'), (300, '5 মিনিট')]
EXP_NAME, VIEW_NAME, AD_NAME = dict(EXPIRY), dict(VIEWS), dict(AUTODEL)
MAX_LOCKS, MAX_ITEMS = 50, 10
KIND_ICON = {'document': '📄', 'photo': '🖼', 'video': '🎬', 'audio': '🎵', 'voice': '🎤', 'animation': '🎞', 'video_note': '⭕', 'text': '📝'}
ALPHA = string.ascii_uppercase.replace('O', '').replace('I', '') + '23456789'


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode(), 20000).hex()


def new_id():
    while True:
        lid = ''.join(random.choice(ALPHA) for _ in range(6))
        if lid not in DB.d['locks']:
            return lid


def my_locks(uid):
    return sorted([(k, v) for k, v in DB.d['locks'].items() if v['owner'] == uid], key=lambda kv: -kv[1]['created'])


def make_lock(uid, items, pw, exp=0, views=0, autodel=0, protect=False, notify=True):
    lid = new_id()
    salt = secrets.token_hex(8)
    DB.d['locks'][lid] = {'owner': uid, 'items': items, 'salt': salt, 'pw': hash_pw(pw, salt), 'exp': (time.time() + exp) if exp else None,
                          'max': views or None, 'views': 0, 'notify': notify, 'autodel': autodel, 'protect': protect, 'created': time.time()}
    DB.save()
    return lid


def time_left(lk):
    if not lk['exp']:
        return '♾'
    left = lk['exp'] - time.time()
    return fmt_dur(left) if left > 0 else 'শেষ'


def views_left(lk):
    return '♾' if not lk['max'] else '%d/%d' % (max(0, lk['max'] - lk['views']), lk['max'])


def link_for(lid):
    return bot_link('u_%s' % lid)


def home_view(uid):
    n = len(my_locks(uid))
    text = ('🔒 <b>Advanced File Locker</b>\n' + LINE + '\nআপনার ফাইল, ছবি, ভিডিও বা টেক্সট পাসওয়ার্ড দিয়ে লক করুন।\n'
            'এক্সপায়ারি, ভিউ-লিমিট, অটো-ডিলিট ও ফরওয়ার্ড-প্রটেকশন সবই বাটনে।\n\n📂 আপনার লক: <b>%d</b>' % n + FOOTER)
    return text, kb([btn('🔒 নতুন Lock', 'lk'), btn('🔓 Unlock', 'ul')], [btn('📂 My Locks (%d)' % n, 'ml')], [btn('❓ Help', 'lhelp')])


HELP = ('❓ <b>File Locker — সাহায্য</b>\n' + LINE + '\n'
        '🔒 <b>Lock:</b> ফাইল/ছবি/ভিডিও/ভয়েস/টেক্সট পাঠান (একসাথে ১০টি পর্যন্ত) → পাসওয়ার্ড দিন → অপশন বেছে নিন\n'
        '🔓 <b>Unlock:</b> ID ও পাসওয়ার্ড দিয়ে, অথবা শেয়ার লিংক খুলে\n'
        '⏳ এক্সপায়ারি • 👁 ভিউ-লিমিট • 🧨 আনলকের পর চ্যাট থেকে অটো-ডিলিট • 🚫 ফরওয়ার্ড/সেভ ব্লক\n'
        '🛡 ৩ বার ভুল পাসওয়ার্ডে ১০ মিনিট ব্লক; মালিক নোটিফিকেশন পান\n'
        '📂 My Locks থেকে মেয়াদ/ভিউ বাড়ানো, পাসওয়ার্ড বদল, ডিলিট\n\n'
        '⌨️ দ্রুত: ফাইল পাঠিয়ে ক্যাপশনে <code>/lock পাসওয়ার্ড [মিনিট] [ভিউলিমিট]</code>\n<code>/unlock ID পাসওয়ার্ড</code>')


@route('lhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


# ---------------------------------------------------------------- lock wizard
def items_view(d):
    lines = ['🔒 <b>নতুন Lock — ধাপ ১/৩</b>\n' + LINE + '\nযা লক করতে চান পাঠান — ফাইল, ছবি, ভিডিও, ভয়েস বা টেক্সট।\nএকাধিক হলে একে একে পাঠান।\n']
    for i, it in enumerate(d['items'], 1):
        lines.append('%d. %s %s' % (i, KIND_ICON.get(it['k'], '📄'), esc(it['n'][:40])))
    lines.append('\n📦 এখন: <b>%d/%d</b>' % (len(d['items']), MAX_ITEMS))
    rows = []
    if d['items']:
        rows.append([btn('✅ Done (%d)' % len(d['items']), 'lk:done'), btn('↩️ শেষটি বাদ', 'lk:undo')])
    rows.append([btn('✖ Cancel', 'home')])
    return '\n'.join(lines), kb(*rows)


@route('lk')
def cb_lock(c, a):
    uid = c.from_user.id
    act = a[0] if a else ''
    if not act:
        if len(my_locks(uid)) >= MAX_LOCKS:
            return ack(c, 'সর্বোচ্চ %d টি লক রাখা যায়। পুরনো মুছুন।' % MAX_LOCKS, True)
        ack(c)
        set_state(uid, 'lk_items', items=[], exp=0, views=0, autodel=0, protect=False, notify=True)
        t, k = items_view(STATE[uid]['d'])
        return edit(c, t, k)
    s = get_state(uid)
    if not s or s['k'] not in ('lk_items', 'lk_panel'):
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    d = s['d']
    if act == 'undo' and s['k'] == 'lk_items':
        if d['items']:
            d['items'].pop()
        ack(c)
        t, k = items_view(d)
        return edit(c, t, k)
    if act == 'done' and s['k'] == 'lk_items':
        if not d['items']:
            return ack(c, 'আগে কিছু পাঠান', True)
        ack(c)
        s['k'] = 'lk_pw'
        return edit(c, '🔒 <b>নতুন Lock — ধাপ ২/৩</b>\n\n🔑 একটি <b>পাসওয়ার্ড</b> লিখে পাঠান (কমপক্ষে ৪ অক্ষর)।\n<i>নিরাপত্তার জন্য মেসেজটি পাঠানোর সাথে সাথে মুছে ফেলা হবে।</i>', kb(nav()))
    if s['k'] != 'lk_panel':
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    if act == 'e':
        vals = [v for v, _ in EXPIRY]
        d['exp'] = vals[(vals.index(d['exp']) + 1) % len(vals)]
    elif act == 'v':
        vals = [v for v, _ in VIEWS]
        d['views'] = vals[(vals.index(d['views']) + 1) % len(vals)]
    elif act == 'd':
        vals = [v for v, _ in AUTODEL]
        d['autodel'] = vals[(vals.index(d['autodel']) + 1) % len(vals)]
    elif act == 'p':
        d['protect'] = not d['protect']
    elif act == 'n':
        d['notify'] = not d['notify']
    elif act == 'go':
        if len(my_locks(uid)) >= MAX_LOCKS:
            return ack(c, 'সর্বোচ্চ %d টি লক' % MAX_LOCKS, True)
        lid = make_lock(uid, d['items'], d['pw'], d['exp'], d['views'], d['autodel'], d['protect'], d['notify'])
        clear_state(uid)
        ack(c, '🔒 লক হয়েছে!')
        return edit(c, created_text(lid), created_kb(lid))
    ack(c)
    t, k = panel_view(d)
    edit(c, t, k)


def panel_view(d):
    text = ('🔒 <b>নতুন Lock — ধাপ ৩/৩</b>\n' + LINE + '\n📦 আইটেম: <b>%d</b>\n\n⏳ মেয়াদ: <b>%s</b>\n👁 ভিউ-লিমিট: <b>%s</b>\n🧨 আনলকের পর অটো-ডিলিট: <b>%s</b>\n🚫 ফরওয়ার্ড/সেভ ব্লক: <b>%s</b>\n🔔 আনলকে নোটিফিকেশন: <b>%s</b>'
            % (len(d['items']), EXP_NAME[d['exp']], VIEW_NAME[d['views']], AD_NAME[d['autodel']], 'হ্যাঁ' if d['protect'] else 'না', 'হ্যাঁ' if d['notify'] else 'না'))
    return text, kb([btn('⏳ %s' % EXP_NAME[d['exp']], 'lk:e'), btn('👁 %s' % VIEW_NAME[d['views']], 'lk:v')],
                    [btn('🧨 %s' % AD_NAME[d['autodel']], 'lk:d'), btn('🚫 %s' % ('ON' if d['protect'] else 'OFF'), 'lk:p'), btn('🔔 %s' % ('ON' if d['notify'] else 'OFF'), 'lk:n')],
                    [btn('🔒 Lock করুন', 'lk:go')], nav())


def created_text(lid):
    lk = DB.d['locks'][lid]
    return ('🔒 <b>লক হয়েছে!</b>\n' + LINE + '\n🆔 ID: <code>%s</code>\n📦 আইটেম: %d\n⏳ মেয়াদ: %s\n👁 ভিউ: %s\n\n🔗 শেয়ার লিংক:\n<code>%s</code>\n\n'
            'খুলতে ID + পাসওয়ার্ড লাগবে। <i>পাসওয়ার্ডটি নিরাপদে রাখুন — রিকভার করা যায় না।</i>' % (lid, len(lk['items']), time_left(lk), views_left(lk), link_for(lid)))


def created_kb(lid):
    return kb([btn('📤 Share', url='https://t.me/share/url?url=' + urllib.parse.quote(link_for(lid))), btn('📂 My Locks', 'ml')], [btn('🔒 আরেকটি', 'lk'), btn('🏠 Home', 'home')])


def add_item(m, d):
    r = extract_media(m)
    if r:
        kind, fid, name, size = r
        d['items'].append({'k': kind, 'f': fid, 'n': name})
    elif m.text:
        d['items'].append({'k': 'text', 'f': '', 'n': m.text.strip()[:3500], 'x': m.text.strip()[:3500]})
    else:
        return False
    return True


# ---------------------------------------------------------------- unlock
@route('ul')
def cb_unlock(c, a):
    ack(c)
    set_state(c.from_user.id, 'ul_id')
    edit(c, '🔓 <b>Unlock</b>\n\nলকের <b>ID</b> লিখুন (৬ অক্ষর, যেমন <code>AB3K9X</code>):', kb(nav()))


def deliver(chat_id, uid, lid, lk):
    sent = []
    cap = '✅ <b>আনলক সফল!</b>'
    for i, it in enumerate(lk['items']):
        if it['k'] == 'text':
            r = say(chat_id, '📝 ' + esc(it.get('x', it['n'])), protect_content=True if lk['protect'] else None)
        else:
            r = send_media(chat_id, it['k'], it['f'], cap if i == 0 else None, lk['protect'])
        if r is not None:
            sent.append(r.message_id)
    if lk['autodel'] and sent:
        delete_later(chat_id, sent, lk['autodel'])
    return sent


def do_unlock(chat_id, uid, lid, pw, name='User'):
    lid = lid.strip().upper()
    lk = DB.d['locks'].get(lid)
    fk = (uid, lid)
    fl = FAILS.get(fk, [0, 0])
    if fl[1] > time.time():
        return say(chat_id, '⛔ অনেকবার ভুল পাসওয়ার্ড দিয়েছেন। <b>%s</b> পরে আবার চেষ্টা করুন।' % fmt_dur(fl[1] - time.time()), kb(nav()))
    if not lk:
        return say(chat_id, '❌ ID বা পাসওয়ার্ড ভুল।', kb([btn('🔓 আবার', 'ul')], nav()))
    if lk['exp'] and time.time() > lk['exp']:
        DB.d['locks'].pop(lid, None)
        DB.save()
        return say(chat_id, '⌛ এই লকটির মেয়াদ শেষ হয়ে গেছে।', kb(nav()))
    if hash_pw(pw, lk['salt']) != lk['pw']:
        fl[0] += 1
        if fl[0] >= 3:
            fl = [0, time.time() + 600]
            if lk['notify']:
                say(lk['owner'], '⚠️ <b>সতর্কতা!</b>\nআপনার লক <code>%s</code> খোলার চেষ্টায় ৩ বার ভুল পাসওয়ার্ড দেওয়া হয়েছে।\n👤 %s (<code>%s</code>)\nতাকে ১০ মিনিট ব্লক করা হয়েছে।' % (lid, esc(name), uid))
        FAILS[fk] = fl
        if len(FAILS) > 5000:
            FAILS.clear()
        left = 3 - fl[0] if fl[1] <= time.time() else 0
        return say(chat_id, '❌ ID বা পাসওয়ার্ড ভুল।' + ('\n⚠️ আর %d বার ভুল হলে ১০ মিনিট ব্লক।' % left if left else '\n⛔ ১০ মিনিটের জন্য ব্লক করা হয়েছে।'), kb([btn('🔓 আবার', 'ul')], nav()))
    FAILS.pop(fk, None)
    lk['views'] += 1
    remaining = None
    if lk['max']:
        remaining = lk['max'] - lk['views']
        if remaining < 0:
            DB.d['locks'].pop(lid, None)
            DB.save()
            return say(chat_id, '⚠️ এই লকের ভিউ-লিমিট শেষ হয়ে গেছে।', kb(nav()))
        if remaining == 0:
            DB.d['locks'].pop(lid, None)
    DB.save()
    deliver(chat_id, uid, lid, lk)
    info = []
    if remaining is not None:
        info.append('👁 বাকি: %d বার' % remaining + (' (লক মুছে গেছে)' if remaining == 0 else ''))
    if lk['autodel']:
        info.append('🧨 %s পরে এই চ্যাট থেকে মুছে যাবে' % AD_NAME[lk['autodel']])
    if info:
        say(chat_id, '\n'.join(info), kb(nav()))
    if lk['notify'] and uid != lk['owner']:
        say(lk['owner'], '🔓 আপনার লক <code>%s</code> খোলা হয়েছে।\n👤 %s (<code>%s</code>)' % (lid, esc(name), uid))


# ---------------------------------------------------------------- my locks
@route('ml')
def cb_mylocks(c, a):
    uid = c.from_user.id
    ack(c)
    clear_state(uid)
    ls = my_locks(uid)
    if not ls:
        return edit(c, '📂 <b>My Locks</b>\n\nএখনো কিছু লক করেননি।', kb([btn('🔒 নতুন Lock', 'lk')], nav()))
    chunk, page, pages = paginate(ls, to_int(a[0], 0) if a else 0, 6)
    rows = [[btn('🔒 %s • %d আইটেম • ⏳ %s • 👁 %s' % (k, len(v['items']), time_left(v), views_left(v)), 'mv:%s:%d' % (k, page))] for k, v in chunk]
    pg = pager('ml', page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav())
    edit(c, '📂 <b>My Locks (%d)</b>\nম্যানেজ করতে চাপুন।' % len(ls), kb(*rows))


def lock_card(lid, lk, page=0):
    text = ('🔒 <b>Lock <code>%s</code></b>\n' % lid + LINE + '\n📦 আইটেম: %d\n⏳ মেয়াদ বাকি: <b>%s</b>\n👁 ভিউ বাকি: <b>%s</b>\n🧨 অটো-ডিলিট: %s\n🚫 প্রটেক্টেড: %s\n🔔 নোটিফিকেশন: %s\n\n🔗 <code>%s</code>'
            % (len(lk['items']), time_left(lk), views_left(lk), AD_NAME[lk['autodel']], 'হ্যাঁ' if lk['protect'] else 'না', 'ON' if lk['notify'] else 'OFF', link_for(lid)))
    rows = [[btn('⏳ +1 ঘণ্টা', 'mx:e1:%s' % lid), btn('⏳ +24 ঘণ্টা', 'mx:e24:%s' % lid), btn('♾ মেয়াদ নেই', 'mx:e0:%s' % lid)],
            [btn('👁 +1', 'mx:v1:%s' % lid), btn('👁 +5', 'mx:v5:%s' % lid), btn('👁 ♾', 'mx:v0:%s' % lid)],
            [btn('🔑 পাসওয়ার্ড বদল', 'mx:pw:%s' % lid), btn('🔔 Notify %s' % ('OFF' if lk['notify'] else 'ON'), 'mx:nt:%s' % lid)],
            [btn('📤 Share', url='https://t.me/share/url?url=' + urllib.parse.quote(link_for(lid))), btn('🗑 ডিলিট', 'mx:del:%s' % lid)],
            nav('ml:%d' % page)]
    return text, kb(*rows)


@route('mv')
def cb_lock_view(c, a):
    uid = c.from_user.id
    lid = a[0] if a else ''
    lk = DB.d['locks'].get(lid)
    if not lk or lk['owner'] != uid:
        return ack(c, 'লকটি পাওয়া যায়নি', True)
    ack(c)
    t, k = lock_card(lid, lk, to_int(a[1], 0) if len(a) > 1 else 0)
    edit(c, t, k)


@route('mx')
def cb_lock_act(c, a):
    uid = c.from_user.id
    if len(a) < 2:
        return ack(c)
    act, lid = a[0], a[1]
    lk = DB.d['locks'].get(lid)
    if not lk or lk['owner'] != uid:
        return ack(c, 'লকটি পাওয়া যায়নি', True)
    if act in ('e1', 'e24'):
        base = max(lk['exp'] or time.time(), time.time())
        lk['exp'] = base + (3600 if act == 'e1' else 86400)
    elif act == 'e0':
        lk['exp'] = None
    elif act in ('v1', 'v5'):
        if lk['max']:
            lk['max'] += 1 if act == 'v1' else 5
    elif act == 'v0':
        lk['max'] = None
    elif act == 'nt':
        lk['notify'] = not lk['notify']
    elif act == 'pw':
        ack(c)
        set_state(uid, 'ch_pw', lid=lid)
        return say(c.message.chat.id, '🔑 <b>নতুন পাসওয়ার্ড</b> লিখে পাঠান (কমপক্ষে ৪ অক্ষর)।', kb(nav('mv:%s:0' % lid)))
    elif act == 'del':
        return edit(c, '🗑 <b>লক <code>%s</code> ডিলিট করবেন?</b>\nএই আইডি দিয়ে আর কিছু খোলা যাবে না।' % lid, kb([btn('✅ হ্যাঁ', 'mx:dy:%s' % lid), btn('✖ না', 'mv:%s:0' % lid)]))
    elif act == 'dy':
        DB.d['locks'].pop(lid, None)
        DB.save()
        ack(c, '🗑 ডিলিট হয়েছে')
        return cb_mylocks(c, [])
    DB.save()
    ack(c, '✅ আপডেট হয়েছে')
    t, k = lock_card(lid, lk)
    edit(c, t, k)


def cleanup_tick():
    now_t = time.time()
    dead = [k for k, v in DB.d['locks'].items() if v['exp'] and now_t > v['exp']]
    for k in dead:
        DB.d['locks'].pop(k, None)
    if dead:
        DB.save()


# ---------------------------------------------------------------- commands & messages
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    uid = m.from_user.id
    clear_state(uid)
    arg = cmd_arg(m)
    if arg.startswith('u_'):
        lid = arg[2:].upper()
        if lid in DB.d['locks']:
            set_state(uid, 'ul_pw', lid=lid)
            return say(m.chat.id, '🔒 <b>এই কনটেন্ট পাসওয়ার্ড-সুরক্ষিত</b>\n🆔 <code>%s</code>\n\n🔑 পাসওয়ার্ড লিখে পাঠান:' % lid, kb(nav()))
        return say(m.chat.id, '❌ লকটি পাওয়া যায়নি বা মেয়াদ শেষ।', kb(nav()))
    t, k = home_view(uid)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()))


@bot.message_handler(commands=['unlock'])
def unlock_cmd(m):
    parts = (m.text or '').split()
    delete_msg(m.chat.id, m.message_id)
    if len(parts) < 3:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/unlock ID পাসওয়ার্ড</code>\nঅথবা 🔓 Unlock বাটন চাপুন।', kb([btn('🔓 Unlock', 'ul')], nav()))
    if not rate_ok(m.from_user.id, 'unlock', 1.0):
        return
    do_unlock(m.chat.id, m.from_user.id, parts[1], ' '.join(parts[2:]), uname_of(m.from_user))


@bot.message_handler(commands=['lock'])
def lock_text_cmd(m):
    say(m.chat.id, '⚠️ ফাইল/ছবি/ভিডিও পাঠিয়ে ক্যাপশনে লিখুন: <code>/lock পাসওয়ার্ড [মিনিট] [ভিউলিমিট]</code>\nঅথবা বাটনে 👇', kb([btn('🔒 নতুন Lock', 'lk')], nav()))


@bot.message_handler(func=lambda m: m.chat.type == 'private', content_types=MEDIA_TYPES + ['text'])
def on_any(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = (m.text or '').strip()
    cap = (m.caption or '').strip()
    if cap.startswith('/lock'):
        parts = cap.split()
        if len(parts) < 2:
            return say(m.chat.id, '⚠️ ক্যাপশনে লিখুন: <code>/lock পাসওয়ার্ড [মিনিট] [ভিউলিমিট]</code>')
        exp = to_int(parts[2], None) if len(parts) > 2 else 0
        views = to_int(parts[3], None) if len(parts) > 3 else 0
        if exp is None or views is None or exp < 0 or views < 0:
            return say(m.chat.id, '⚠️ মিনিট ও ভিউলিমিট সংখ্যা হতে হবে।')
        if len(my_locks(uid)) >= MAX_LOCKS:
            return say(m.chat.id, '⚠️ সর্বোচ্চ %d টি লক রাখা যায়।' % MAX_LOCKS)
        d = {'items': []}
        if not add_item(m, d):
            return
        lid = make_lock(uid, d['items'], parts[1], exp * 60, views)
        delete_msg(m.chat.id, m.message_id)
        return say(m.chat.id, created_text(lid), created_kb(lid))
    if not s:
        if txt:
            t, k = home_view(uid)
            return say(m.chat.id, '💡 বাটন থেকে বেছে নিন:\n\n' + t, k)
        return say(m.chat.id, '📎 ফাইল লক করতে নিচের বাটন চাপুন:', kb([btn('🔒 নতুন Lock', 'lk')], nav()))
    key, d = s['k'], s['d']
    if key == 'lk_items':
        if len(d['items']) >= MAX_ITEMS:
            return say(m.chat.id, '⚠️ সর্বোচ্চ %d টি আইটেম। ✅ Done চাপুন।' % MAX_ITEMS)
        if not add_item(m, d):
            return say(m.chat.id, '⚠️ এই ধরনের মেসেজ লক করা যায় না।')
        t, k = items_view(d)
        return say(m.chat.id, t, k)
    if not txt:
        return say(m.chat.id, 'ℹ️ এখন টেক্সট পাঠান, অথবা /cancel দিন।')
    if key == 'lk_pw':
        delete_msg(m.chat.id, m.message_id)
        if len(txt) < 4:
            return say(m.chat.id, '⚠️ পাসওয়ার্ড কমপক্ষে ৪ অক্ষরের হতে হবে। আবার লিখুন:')
        d['pw'] = txt
        s['k'] = 'lk_panel'
        t, k = panel_view(d)
        return say(m.chat.id, '✅ পাসওয়ার্ড সেট হয়েছে (মেসেজটি মুছে দেওয়া হয়েছে)।\n\n' + t, k)
    if key == 'ul_id':
        lid = txt.upper()
        if not re.fullmatch(r'[A-Z0-9]{6}', lid):
            return say(m.chat.id, '⚠️ ID ৬ অক্ষরের হয় (যেমন AB3K9X)। আবার লিখুন:')
        set_state(uid, 'ul_pw', lid=lid)
        return say(m.chat.id, '🔑 <b>%s</b> — পাসওয়ার্ড লিখুন:' % lid, kb(nav()))
    if key == 'ul_pw':
        delete_msg(m.chat.id, m.message_id)
        clear_state(uid)
        if not rate_ok(uid, 'unlock', 1.0):
            return
        return do_unlock(m.chat.id, uid, d['lid'], txt, uname_of(m.from_user))
    if key == 'ch_pw':
        delete_msg(m.chat.id, m.message_id)
        lk = DB.d['locks'].get(d['lid'])
        if len(txt) < 4:
            return say(m.chat.id, '⚠️ কমপক্ষে ৪ অক্ষর। আবার লিখুন:')
        clear_state(uid)
        if not lk or lk['owner'] != uid:
            return say(m.chat.id, '⚠️ লকটি পাওয়া যায়নি।')
        lk['salt'] = secrets.token_hex(8)
        lk['pw'] = hash_pw(txt, lk['salt'])
        DB.save()
        return say(m.chat.id, '✅ পাসওয়ার্ড বদলানো হয়েছে (মেসেজটি মুছে দেওয়া হয়েছে)।', kb([btn('📂 My Locks', 'ml')], nav()))
    say(m.chat.id, 'ℹ️ বাটন ব্যবহার করুন, অথবা /cancel দিয়ে বাতিল করুন।')


start_worker(cleanup_tick, 30)
run(commands=[('start', 'মেনু খুলুন'), ('unlock', 'ID পাসওয়ার্ড দিয়ে আনলক'), ('lock', 'লক করার নিয়ম'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "echo": {
        "emoji": '💬',
        "name": 'FAQ & Support Bot',
        "proj_name": 'FAQ Support Bot',
        "category": '💬 FAQ & Support',
        "level": '⭐⭐ Intermediate',
        "setup": '~30 sec',
        "features": ['প্রথম /start দেওয়া ব্যক্তি অটো Owner', '📚 FAQ Manager — ক্যাটাগরি, ছবি-সহ উত্তর, এডিট/ডিলিট (সব বাটনে)', '🔍 স্মার্ট সার্চ — ইউজার যেকোনো কথা লিখলেই মিলে যাওয়া FAQ দেখায় (বাংলা সাপোর্ট)', '🎫 সাপোর্ট টিকিট — ইউজার→Owner, বাটনে Reply/Close/Ban, ইউজারের Reply ও ⭐ রেটিং', '📢 Broadcast (প্রিভিউ + কনফার্ম), 🚫 Ban/Unban', '✏️ কাস্টম Welcome ও About/Contact তথ্য'],
        "commands": '/faq · /search · /support · /mytickets · Owner: /panel · /addfaq প্রশ্ন|উত্তর · /delfaq প্রশ্ন',
        "about": 'পূর্ণাঙ্গ FAQ ও সাপোর্ট ডেস্ক — ক্যাটাগরিভিত্তিক FAQ, স্মার্ট সার্চ, টিকিট সিস্টেম, ব্রডকাস্ট ও ব্যান ম্যানেজমেন্ট।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', 'সবার আগে আপনি /start দিন — আপনি Owner হবেন', '🛠 Owner Panel → 📚 FAQ Manager থেকে ক্যাটাগরি ও FAQ যোগ করুন', 'ইউজার FAQ দেখবে, সার্চ করবে বা 🎫 Contact Support করবে', 'টিকিট আপনার কাছে আসবে — বাটনে Reply/Close করুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 /addfaq রিফান্ড কীভাবে পাবো?|৭ দিনের মধ্যে সাপোর্টে জানান\n👤 /faq → [বাটন: রিফান্ড কীভাবে পাবো?]\n👤 [ট্যাপ] 🤖 ৭ দিনের মধ্যে সাপোর্টে জানান',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('faq_data.json', {'owner': None, 'cats': {}, 'users': {}, 'tickets': {}, 'banned': [], 'next': 1, 'about': '', 'welcome': ''})
MAX_OPEN_PER_USER = 3
ST_ICON = {'open': '🟡', 'answered': '🟢', 'closed': '⚫'}
ST_NAME = {'open': 'খোলা', 'answered': 'উত্তর দেওয়া হয়েছে', 'closed': 'বন্ধ'}


def owner_id():
    return DB.d.get('owner')


def is_owner(uid):
    return DB.d.get('owner') == uid


def nid():
    with DB.lock:
        n = DB.d['next']
        DB.d['next'] += 1
    return str(n)


def faq_total():
    return sum(len(c['faqs']) for c in DB.d['cats'].values())


def open_tickets():
    return [t for t in DB.d['tickets'].values() if t['status'] != 'closed']


def reg_user(u):
    rec = DB.d['users'].setdefault(str(u.id), {'name': '', 'blocked': False})
    rec['name'] = uname_of(u)[:40]
    rec['blocked'] = False


def banned(uid):
    return uid in DB.d['banned']


def tokens(s):
    return [t for t in re.findall(r'[\u0980-\u09FF\w]+', (s or '').lower()) if len(t) > 1]


def search_faq(query, limit=6):
    qt = set(tokens(query))
    res = []
    if not qt:
        return res
    ql = query.lower().strip()
    for cid, cat in DB.d['cats'].items():
        for fid, f in cat['faqs'].items():
            qk, ak = set(tokens(f['q'])), set(tokens(f['a']))
            score = 2 * len(qt & qk) + len(qt & ak)
            if ql and ql in f['q'].lower():
                score += 3
            for t in qt:
                if len(t) >= 3 and any(len(x) >= 3 and (x.startswith(t) or t.startswith(x)) for x in qk):
                    score += 1
            if score >= 2:
                res.append((score, cid, fid))
    res.sort(key=lambda x: -x[0])
    return res[:limit]


# ---------------------------------------------------------------- home
def home_view(uid):
    d = DB.d
    welcome = d.get('welcome') or '💬 <b>FAQ &amp; Support</b>\nসাধারণ প্রশ্নের উত্তর খুঁজুন, না পেলে সরাসরি সাপোর্টে মেসেজ করুন।'
    text = welcome + '\n' + LINE + '\n❓ FAQ: <b>%d</b> টি' % faq_total() + FOOTER
    rows = [[btn('❓ FAQ', 'fq'), btn('🔍 Search', 'sr')], [btn('🎫 Contact Support', 'tn'), btn('📋 My Tickets', 'tm')]]
    if d.get('about'):
        rows.append([btn('ℹ️ About / Contact', 'abt')])
    if is_owner(uid):
        n = len(open_tickets())
        rows.append([btn('🛠 Owner Panel%s' % (' (%d)' % n if n else ''), 'ow')])
    return text, kb(*rows)


@route('abt')
def cb_about(c, a):
    edit(c, 'ℹ️ <b>About / Contact</b>\n' + LINE + '\n' + (DB.d.get('about') or 'তথ্য দেওয়া হয়নি।'), kb(nav()))


# ---------------------------------------------------------------- FAQ browsing
def faq_cat_view(cid, page=0, owner_view=False):
    cat = DB.d['cats'].get(cid)
    if not cat:
        return '⚠️ ক্যাটাগরি পাওয়া যায়নি।', kb(nav())
    items = list(cat['faqs'].items())
    chunk, page, pages = paginate(items, page, 8)
    rows = [[btn('❓ %s' % f['q'][:40], 'fa:%s:%s' % (cid, fid))] for fid, f in chunk]
    pg = pager('fc:%s' % cid, page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav('fq' if len(DB.d['cats']) > 1 else None))
    return '%s <b>%s</b>\nপ্রশ্ন বেছে নিন:' % ('📂', esc(cat['name'])), kb(*rows)


@route('fq')
def cb_faq(c, a):
    ack(c)
    cats = DB.d['cats']
    cats_nonempty = {k: v for k, v in cats.items() if v['faqs']}
    if not cats_nonempty:
        return edit(c, '❓ <b>FAQ</b>\n\nএখনো কোনো FAQ যোগ করা হয়নি।', kb([btn('🎫 Contact Support', 'tn')], nav()))
    if len(cats_nonempty) == 1:
        t, k = faq_cat_view(next(iter(cats_nonempty)))
        return edit(c, t, k)
    rows = [[btn('📂 %s (%d)' % (v['name'][:28], len(v['faqs'])), 'fc:%s:0' % k)] for k, v in cats_nonempty.items()]
    rows.append(nav())
    edit(c, '❓ <b>FAQ — ক্যাটাগরি বেছে নিন</b>', kb(*rows))


@route('fc')
def cb_faq_cat(c, a):
    ack(c)
    t, k = faq_cat_view(a[0] if a else '', to_int(a[1], 0) if len(a) > 1 else 0)
    edit(c, t, k)


def send_answer(chat_id, cid, fid, back=None):
    f = DB.d['cats'].get(cid, {}).get('faqs', {}).get(fid)
    if not f:
        return say(chat_id, '❌ FAQ পাওয়া যায়নি।')
    text = '❓ <b>%s</b>\n\n%s' % (esc(f['q']), esc(f['a']))
    markup = kb([btn('🎫 আরও সাহায্য দরকার?', 'tn')], nav(back))
    if f.get('ph'):
        if len(text) <= 1000:
            return send_media(chat_id, 'photo', f['ph'], text, False, markup)
        send_media(chat_id, 'photo', f['ph'], None, False)
    say(chat_id, text, markup)


@route('fa')
def cb_answer(c, a):
    if len(a) < 2:
        return ack(c)
    ack(c)
    send_answer(c.message.chat.id, a[0], a[1], 'fc:%s:0' % a[0])


@route('sr')
def cb_search(c, a):
    ack(c)
    set_state(c.from_user.id, 'search')
    edit(c, '🔍 <b>Search</b>\n\nআপনার প্রশ্ন বা কিওয়ার্ড লিখুন:', kb(nav()))


def do_search(chat_id, query):
    res = search_faq(query)
    if not res:
        return say(chat_id, '🤔 "%s" এর সাথে মেলে এমন FAQ পাইনি।\n\nসরাসরি সাপোর্টে জানাতে পারেন 👇' % esc(query[:60]), kb([btn('🎫 Contact Support', 'tn')], [btn('❓ সব FAQ', 'fq'), btn('🏠 Home', 'home')]))
    rows = [[btn('❓ %s' % DB.d['cats'][cid]['faqs'][fid]['q'][:40], 'fa:%s:%s' % (cid, fid))] for _, cid, fid in res]
    rows.append([btn('🎫 উত্তর পাইনি — সাপোর্ট', 'tn')])
    rows.append(nav())
    say(chat_id, '🔍 <b>%d টি সম্ভাব্য উত্তর পেয়েছি:</b>' % len(res), kb(*rows))


# ---------------------------------------------------------------- tickets (user side)
def conv_text(t, last=6):
    lines = []
    for m_ in t['msgs'][-last:]:
        lines.append('%s %s' % ('👤' if m_['f'] == 'u' else '🛠', esc(m_['t'][:300])))
    return '\n'.join(lines)


def ticket_card(tid, t, owner_view):
    return ('🎫 <b>Ticket #%s</b> %s %s\n' % (tid, ST_ICON[t['status']], ST_NAME[t['status']]) + ('👤 %s (<code>%s</code>)\n' % (esc(t['name']), t['uid']) if owner_view else '') + LINE + '\n' + conv_text(t) +
            ('\n\n⭐ রেটিং: %d/5' % t['rating'] if t.get('rating') else ''))


def ticket_kb(tid, t, owner_view, back):
    rows = []
    if t['status'] != 'closed':
        if owner_view:
            rows.append([btn('💬 Reply', 'or:%s' % tid), btn('✅ Close', 'oc:%s' % tid), btn('🚫 Ban', 'ob:%s:%s' % (t['uid'], tid))])
        else:
            rows.append([btn('💬 Reply', 'tu:%s' % tid), btn('✅ Resolved', 'tz:%s' % tid)])
    rows.append(nav(back))
    return kb(*rows)


@route('tn')
def cb_new_ticket(c, a):
    uid = c.from_user.id
    if banned(uid):
        return ack(c, '🚫 আপনি সাপোর্ট ব্যবহার করতে পারছেন না', True)
    if not owner_id():
        return ack(c, 'সাপোর্ট এখনো সেটআপ হয়নি', True)
    mine_open = [t for t in DB.d['tickets'].values() if t['uid'] == uid and t['status'] != 'closed']
    if len(mine_open) >= MAX_OPEN_PER_USER:
        return ack(c, 'আপনার %d টি টিকিট খোলা আছে — আগে সেগুলো সমাধান করুন' % MAX_OPEN_PER_USER, True)
    ack(c)
    set_state(uid, 'tk_new')
    say(c.message.chat.id, '🎫 <b>Contact Support</b>\n\nআপনার সমস্যা বা প্রশ্ন লিখে পাঠান (ছবি/ফাইলও দিতে পারেন)।\nসাপোর্ট টিম উত্তর দিলে এখানেই জানতে পারবেন।', kb(nav()))


def notify_owner(tid, t, m=None, new=True):
    o = owner_id()
    if not o:
        return
    if m is not None:
        _call(bot.copy_message, o, m.chat.id, m.message_id)
    say(o, ('🆕 <b>নতুন টিকিট</b>' if new else '💬 <b>ইউজারের নতুন মেসেজ</b>') + '\n\n' + ticket_card(tid, t, True), ticket_kb(tid, t, True, 'ot:open:0'))


def mtext(m):
    return (m.text or m.caption or '').strip() or ('[%s]' % (m.content_type or 'media'))


@route('tm')
def cb_my_tickets(c, a):
    uid = c.from_user.id
    ack(c)
    mine = sorted([(k, t) for k, t in DB.d['tickets'].items() if t['uid'] == uid], key=lambda kt: -int(kt[0]))
    if not mine:
        return edit(c, '📋 <b>My Tickets</b>\n\nআপনার কোনো টিকিট নেই।', kb([btn('🎫 Contact Support', 'tn')], nav()))
    chunk, page, pages = paginate(mine, to_int(a[0], 0) if a else 0, 6)
    rows = [[btn('%s #%s — %s' % (ST_ICON[t['status']], k, t['msgs'][0]['t'][:24]), 'tv:%s' % k)] for k, t in chunk]
    pg = pager('tm', page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav())
    edit(c, '📋 <b>My Tickets (%d)</b>' % len(mine), kb(*rows))


@route('tv')
def cb_view_ticket(c, a):
    t = DB.d['tickets'].get(a[0] if a else '')
    if not t or t['uid'] != c.from_user.id:
        return ack(c, 'টিকিট পাওয়া যায়নি', True)
    ack(c)
    edit(c, ticket_card(a[0], t, False), ticket_kb(a[0], t, False, 'tm'))


@route('tu')
def cb_user_reply(c, a):
    t = DB.d['tickets'].get(a[0] if a else '')
    if not t or t['uid'] != c.from_user.id or t['status'] == 'closed':
        return ack(c, 'টিকিটটি খোলা নেই', True)
    ack(c)
    set_state(c.from_user.id, 'tk_ureply', tid=a[0])
    say(c.message.chat.id, '💬 <b>Ticket #%s</b>\nআপনার মেসেজ লিখুন:' % a[0], kb(nav()))


@route('tz')
def cb_resolved(c, a):
    tid = a[0] if a else ''
    t = DB.d['tickets'].get(tid)
    if not t or t['uid'] != c.from_user.id:
        return ack(c, 'টিকিট পাওয়া যায়নি', True)
    close_ticket(tid, t, by_owner=False)
    ack(c, '✅ সমাধান হয়েছে')
    edit(c, ticket_card(tid, t, False), ticket_kb(tid, t, False, 'tm'))


def close_ticket(tid, t, by_owner):
    t['status'] = 'closed'
    DB.save()
    if by_owner:
        say(t['uid'], '✅ <b>Ticket #%s বন্ধ করা হয়েছে।</b>\nসাপোর্ট কেমন লেগেছে? রেটিং দিন:' % tid,
            kb([btn('⭐' * i, 'tq:%s:%d' % (tid, i)) for i in (1, 2, 3)], [btn('⭐' * i, 'tq:%s:%d' % (tid, i)) for i in (4, 5)]))
    else:
        o = owner_id()
        if o:
            say(o, '✅ ইউজার Ticket #%s সমাধান হয়েছে বলে জানিয়েছেন।' % tid)


@route('tq')
def cb_rate(c, a):
    if len(a) < 2:
        return ack(c)
    t = DB.d['tickets'].get(a[0])
    n = to_int(a[1], 0)
    if not t or t['uid'] != c.from_user.id or not 1 <= n <= 5:
        return ack(c, 'টিকিট পাওয়া যায়নি', True)
    t['rating'] = n
    DB.save()
    ack(c, 'ধন্যবাদ!')
    edit(c, '🙏 ধন্যবাদ! আপনার রেটিং: %s' % ('⭐' * n), kb(nav()))
    if owner_id():
        say(owner_id(), '⭐ Ticket #%s রেটিং: %d/5' % (a[0], n))


# ---------------------------------------------------------------- owner panel
def owner_only(c):
    if not is_owner(c.from_user.id):
        ack(c, '🚫 শুধু Owner', True)
        return False
    return True


@route('ow')
def cb_owner(c, a):
    if not owner_only(c):
        return
    ack(c)
    clear_state(c.from_user.id)
    n_open = len([t for t in DB.d['tickets'].values() if t['status'] == 'open'])
    edit(c, '🛠 <b>Owner Panel</b>\n' + LINE + '\n❓ FAQ: %d   🎫 খোলা টিকিট: %d\n👥 ইউজার: %d   🚫 ব্যান: %d' % (faq_total(), n_open, len(DB.d['users']), len(DB.d['banned'])),
         kb([btn('📚 FAQ Manager', 'fm'), btn('🎫 Tickets (%d)' % n_open, 'ot:open:0')],
            [btn('📢 Broadcast', 'bc'), btn('🚫 Banned', 'bl')],
            [btn('✏️ Welcome Msg', 'wm'), btn('ℹ️ About Info', 'abe')], nav()))


@route('ot')
def cb_tickets(c, a):
    if not owner_only(c):
        return
    ack(c)
    flt = a[0] if a and a[0] in ('open', 'answered', 'closed') else 'open'
    items = sorted([(k, t) for k, t in DB.d['tickets'].items() if t['status'] == flt], key=lambda kt: -int(kt[0]))
    chunk, page, pages = paginate(items, to_int(a[1], 0) if len(a) > 1 else 0, 6)
    tabs = [btn(('• ' if f == flt else '') + ST_ICON[f] + ' ' + n, 'ot:%s:0' % f) for f, n in (('open', 'খোলা'), ('answered', 'উত্তর'), ('closed', 'বন্ধ'))]
    rows = [tabs] + [[btn('#%s %s — %s' % (k, t['name'][:12], t['msgs'][-1]['t'][:20]), 'ov:%s' % k)] for k, t in chunk]
    pg = pager('ot:%s' % flt, page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav('ow'))
    edit(c, '🎫 <b>Tickets — %s (%d)</b>' % (ST_NAME[flt], len(items)) + ('' if items else '\n\nকোনো টিকিট নেই।'), kb(*rows))


@route('ov')
def cb_ticket_view(c, a):
    if not owner_only(c):
        return
    t = DB.d['tickets'].get(a[0] if a else '')
    if not t:
        return ack(c, 'টিকিট পাওয়া যায়নি', True)
    ack(c)
    edit(c, ticket_card(a[0], t, True), ticket_kb(a[0], t, True, 'ot:%s:0' % t['status']))


@route('or')
def cb_owner_reply(c, a):
    if not owner_only(c):
        return
    t = DB.d['tickets'].get(a[0] if a else '')
    if not t:
        return ack(c, 'টিকিট পাওয়া যায়নি', True)
    ack(c)
    set_state(c.from_user.id, 'tk_oreply', tid=a[0])
    say(c.message.chat.id, '💬 <b>Ticket #%s — %s কে উত্তর দিন</b>\n(টেক্সট/ছবি/ফাইল পাঠাতে পারেন)' % (a[0], esc(t['name'])), kb(nav()))


@route('oc')
def cb_owner_close(c, a):
    if not owner_only(c):
        return
    t = DB.d['tickets'].get(a[0] if a else '')
    if not t:
        return ack(c, 'টিকিট পাওয়া যায়নি', True)
    close_ticket(a[0], t, True)
    ack(c, '✅ টিকিট বন্ধ')
    edit(c, ticket_card(a[0], t, True), ticket_kb(a[0], t, True, 'ot:open:0'))


@route('ob')
def cb_ban(c, a):
    if not owner_only(c) or not a:
        return
    uid = to_int(a[0], 0)
    if uid and uid != owner_id() and uid not in DB.d['banned']:
        DB.d['banned'].append(uid)
        for k, t in DB.d['tickets'].items():
            if t['uid'] == uid:
                t['status'] = 'closed'
        DB.save()
    ack(c, '🚫 ব্যান করা হয়েছে')
    edit(c, '🚫 ইউজার <code>%s</code> ব্যান করা হয়েছে এবং তার টিকিট বন্ধ হয়েছে।' % uid, kb([btn('🚫 Banned List', 'bl')], nav('ow')))


@route('bl')
def cb_banned(c, a):
    if not owner_only(c):
        return
    ack(c)
    rows = [[btn('♻️ Unban %s' % (DB.d['users'].get(str(u), {}).get('name') or u), 'ub:%d' % u)] for u in DB.d['banned'][:20]]
    rows.append(nav('ow'))
    edit(c, '🚫 <b>Banned Users (%d)</b>' % len(DB.d['banned']) + ('' if DB.d['banned'] else '\n\nকেউ ব্যান নেই।'), kb(*rows))


@route('ub')
def cb_unban(c, a):
    if not owner_only(c):
        return
    uid = to_int(a[0] if a else '', 0)
    if uid in DB.d['banned']:
        DB.d['banned'].remove(uid)
        DB.save()
    ack(c, '♻️ Unban হয়েছে')
    cb_banned(c, [])


@route('wm')
def cb_welcome(c, a):
    if not owner_only(c):
        return
    ack(c)
    set_state(c.from_user.id, 'welcome')
    edit(c, '✏️ <b>Welcome মেসেজ</b>\n\nবর্তমান:\n%s\n\nনতুন মেসেজ লিখুন (HTML চলবে)। মুছে ডিফল্টে ফিরতে <code>-</code> পাঠান।' % (esc(DB.d.get('welcome') or '(ডিফল্ট)')), kb(nav('ow')))


@route('abe')
def cb_about_edit(c, a):
    if not owner_only(c):
        return
    ack(c)
    set_state(c.from_user.id, 'about')
    edit(c, 'ℹ️ <b>About / Contact তথ্য</b>\n\nবর্তমান:\n%s\n\nনতুন তথ্য লিখুন (ফোন, ইমেইল, সময়সূচি ইত্যাদি)। মুছতে <code>-</code> পাঠান।' % esc(DB.d.get('about') or '(নেই)'), kb(nav('ow')))


# ---------------------------------------------------------------- broadcast
@route('bc')
def cb_broadcast(c, a):
    if not owner_only(c):
        return
    ack(c)
    set_state(c.from_user.id, 'bc')
    edit(c, '📢 <b>Broadcast</b>\n\nযে মেসেজটি সবাইকে পাঠাতে চান সেটি এখানে পাঠান (টেক্সট/ছবি/ভিডিও/ফাইল)।\n👥 প্রাপক: <b>%d</b> জন' % len([u for u in DB.d['users'].values() if not u.get('blocked')]), kb(nav('ow')))


def broadcast_worker(chat_id, mid, owner):
    ok = fail = 0
    for uid_s, rec in list(DB.d['users'].items()):
        uid = int(uid_s)
        if rec.get('blocked') or uid in DB.d['banned'] or uid == owner:
            continue
        try:
            bot.copy_message(uid, chat_id, mid)
            ok += 1
        except Exception as e:
            fail += 1
            if '403' in str(e) or 'blocked' in str(e).lower() or 'deactivated' in str(e).lower():
                rec['blocked'] = True
        time.sleep(0.05)
    DB.save()
    say(owner, '📢 <b>Broadcast শেষ</b>\n✅ পৌঁছেছে: %d\n❌ ব্যর্থ/ব্লক: %d' % (ok, fail), kb(nav('ow')))


@route('bs')
def cb_bc_send(c, a):
    if not owner_only(c):
        return
    s = get_state(c.from_user.id)
    if not s or s['k'] != 'bc_confirm':
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    d = s['d']
    clear_state(c.from_user.id)
    ack(c, '📢 পাঠানো শুরু হয়েছে')
    edit(c, '📢 Broadcast চলছে... শেষ হলে জানাব।', None)
    t = threading.Thread(target=broadcast_worker, args=(d['chat'], d['mid'], c.from_user.id), daemon=True)
    t.start()


# ---------------------------------------------------------------- FAQ manager
def fm_view():
    cats = DB.d['cats']
    rows = [[btn('📂 %s (%d)' % (v['name'][:26], len(v['faqs'])), 'fmc:%s' % k)] for k, v in cats.items()]
    rows.append([btn('➕ নতুন ক্যাটাগরি', 'fmn')])
    rows.append(nav('ow'))
    return '📚 <b>FAQ Manager</b>\n' + LINE + '\nক্যাটাগরি বেছে নিন বা নতুন বানান।\n<i>লিগ্যাসি: /addfaq প্রশ্ন|উত্তর</i>', kb(*rows)


@route('fm')
def cb_fm(c, a):
    if not owner_only(c):
        return
    ack(c)
    clear_state(c.from_user.id)
    t, k = fm_view()
    edit(c, t, k)


@route('fmn')
def cb_fm_newcat(c, a):
    if not owner_only(c):
        return
    ack(c)
    set_state(c.from_user.id, 'cat_new')
    edit(c, '📂 <b>নতুন ক্যাটাগরি</b>\n\nনাম লিখুন (যেমন: <i>পেমেন্ট</i>, <i>অ্যাকাউন্ট</i>):', kb(nav('fm')))


def cat_manage_view(cid):
    cat = DB.d['cats'].get(cid)
    if not cat:
        return '⚠️ ক্যাটাগরি পাওয়া যায়নি।', kb(nav('fm'))
    rows = [[btn('✏️ %s' % f['q'][:30] + (' 🖼' if f.get('ph') else ''), 'fe:%s:%s' % (cid, fid))] for fid, f in list(cat['faqs'].items())[:20]]
    rows.append([btn('➕ FAQ যোগ', 'fma:%s' % cid)])
    rows.append([btn('✏️ নাম বদলান', 'fmr:%s' % cid), btn('🗑 ক্যাটাগরি ডিলিট', 'fmx:%s' % cid)])
    rows.append(nav('fm'))
    return '📂 <b>%s</b> — %d টি FAQ\nসম্পাদনা/ডিলিট করতে FAQ-তে চাপুন।' % (esc(cat['name']), len(cat['faqs'])), kb(*rows)


@route('fmc')
def cb_fm_cat(c, a):
    if not owner_only(c):
        return
    ack(c)
    clear_state(c.from_user.id)
    t, k = cat_manage_view(a[0] if a else '')
    edit(c, t, k)


@route('fma')
def cb_fm_add(c, a):
    if not owner_only(c) or not a or a[0] not in DB.d['cats']:
        return
    ack(c)
    set_state(c.from_user.id, 'fa_q', cid=a[0])
    edit(c, '➕ <b>নতুন FAQ — ধাপ ১/৩</b>\n\n❓ <b>প্রশ্নটি</b> লিখুন:', kb(nav('fmc:%s' % a[0])))


@route('fmr')
def cb_fm_ren(c, a):
    if not owner_only(c) or not a or a[0] not in DB.d['cats']:
        return
    ack(c)
    set_state(c.from_user.id, 'cat_ren', cid=a[0])
    edit(c, '✏️ ক্যাটাগরির নতুন নাম লিখুন:', kb(nav('fmc:%s' % a[0])))


@route('fmx')
def cb_fm_del(c, a):
    if not owner_only(c) or not a or a[0] not in DB.d['cats']:
        return
    edit(c, '🗑 <b>"%s" ক্যাটাগরি ডিলিট করবেন?</b>\nভেতরের সব FAQ মুছে যাবে।' % esc(DB.d['cats'][a[0]]['name']), kb([btn('✅ হ্যাঁ', 'fmy:%s' % a[0]), btn('✖ না', 'fmc:%s' % a[0])]))


@route('fmy')
def cb_fm_del_yes(c, a):
    if not owner_only(c):
        return
    DB.d['cats'].pop(a[0] if a else '', None)
    DB.save()
    ack(c, '🗑 ডিলিট হয়েছে')
    t, k = fm_view()
    edit(c, t, k)


@route('fe')
def cb_faq_edit(c, a):
    if not owner_only(c) or len(a) < 2:
        return
    f = DB.d['cats'].get(a[0], {}).get('faqs', {}).get(a[1])
    if not f:
        return ack(c, 'FAQ পাওয়া যায়নি', True)
    ack(c)
    act = a[2] if len(a) > 2 else ''
    if act == 'q':
        set_state(c.from_user.id, 'fe_q', cid=a[0], fid=a[1])
        return edit(c, '✏️ নতুন <b>প্রশ্ন</b> লিখুন:', kb(nav('fe:%s:%s' % (a[0], a[1]))))
    if act == 'a':
        set_state(c.from_user.id, 'fe_a', cid=a[0], fid=a[1])
        return edit(c, '✏️ নতুন <b>উত্তর</b> লিখুন:', kb(nav('fe:%s:%s' % (a[0], a[1]))))
    if act == 'p':
        set_state(c.from_user.id, 'fe_p', cid=a[0], fid=a[1])
        return edit(c, '🖼 নতুন <b>ছবি</b> পাঠান। ছবি সরাতে <code>-</code> লিখুন:', kb(nav('fe:%s:%s' % (a[0], a[1]))))
    if act == 'd':
        return edit(c, '🗑 <b>এই FAQ ডিলিট করবেন?</b>\n\n❓ %s' % esc(f['q']), kb([btn('✅ হ্যাঁ', 'fe:%s:%s:y' % (a[0], a[1])), btn('✖ না', 'fe:%s:%s' % (a[0], a[1]))]))
    if act == 'y':
        DB.d['cats'][a[0]]['faqs'].pop(a[1], None)
        DB.save()
        t, k = cat_manage_view(a[0])
        return edit(c, t, k)
    edit(c, '❓ <b>%s</b>\n\n%s' % (esc(f['q']), esc(f['a'])) + ('\n\n🖼 ছবি আছে' if f.get('ph') else ''),
         kb([btn('✏️ প্রশ্ন', 'fe:%s:%s:q' % (a[0], a[1])), btn('✏️ উত্তর', 'fe:%s:%s:a' % (a[0], a[1]))],
            [btn('🖼 ছবি', 'fe:%s:%s:p' % (a[0], a[1])), btn('🗑 ডিলিট', 'fe:%s:%s:d' % (a[0], a[1]))], nav('fmc:%s' % a[0])))


@route('faskip')
def cb_fa_skip(c, a):
    s = get_state(c.from_user.id)
    if not owner_only(c) or not s or s['k'] != 'fa_ph':
        return ack(c, 'সেশন শেষ', True)
    ack(c)
    save_faq(c.message.chat.id, c.from_user.id, s, '')


def save_faq(chat_id, uid, s, photo):
    d = s['d']
    cat = DB.d['cats'].get(d['cid'])
    clear_state(uid)
    if not cat:
        return say(chat_id, '⚠️ ক্যাটাগরি পাওয়া যায়নি।')
    cat['faqs'][nid()] = {'q': d['q'][:300], 'a': d['a'][:3500], 'ph': photo}
    DB.save()
    say(chat_id, '✅ <b>FAQ যোগ হয়েছে!</b>\n❓ %s' % esc(d['q'][:100]), kb([btn('➕ আরেকটি', 'fma:%s' % d['cid']), btn('📂 ক্যাটাগরি', 'fmc:%s' % d['cid'])]))


def default_cat():
    for k, v in DB.d['cats'].items():
        if v['name'] == 'General':
            return k
    k = nid()
    DB.d['cats'][k] = {'name': 'General', 'faqs': {}}
    return k


# ---------------------------------------------------------------- commands
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    if m.chat.type != 'private':
        return say(m.chat.id, '💬 সাপোর্টের জন্য আমাকে প্রাইভেটে মেসেজ করুন।', kb([btn('💬 Open', url=bot_link())]))
    uid = m.from_user.id
    clear_state(uid)
    reg_user(m.from_user)
    first = False
    if DB.d.get('owner') is None:
        DB.d['owner'] = uid
        first = True
    DB.save()
    t, k = home_view(uid)
    if first:
        t = ('👑 <b>আপনি এই বটের Owner হয়ে গেছেন!</b>\nনিচের <b>🛠 Owner Panel</b> থেকে FAQ যোগ করুন, টিকিট দেখুন ও Broadcast করুন।\n\n' + t)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, '❓ <b>সাহায্য</b>\n\n/faq — সব FAQ\n/search কিওয়ার্ড — খুঁজুন\n/support — সাপোর্টে মেসেজ\n/mytickets — আপনার টিকিট\n\n(Owner: /addfaq প্রশ্ন|উত্তর · /delfaq প্রশ্ন · /panel)', kb(nav()))


@bot.message_handler(commands=['faq'])
def faq_cmd(m):
    cats = {k: v for k, v in DB.d['cats'].items() if v['faqs']}
    if not cats:
        return say(m.chat.id, 'এখনো কোনো FAQ যোগ করা হয়নি।', kb([btn('🎫 Contact Support', 'tn')]))
    if len(cats) == 1:
        t, k = faq_cat_view(next(iter(cats)))
        return say(m.chat.id, t, k)
    rows = [[btn('📂 %s (%d)' % (v['name'][:28], len(v['faqs'])), 'fc:%s:0' % k)] for k, v in cats.items()]
    say(m.chat.id, '❓ <b>FAQ — ক্যাটাগরি বেছে নিন</b>', kb(*rows))


@bot.message_handler(commands=['search'])
def search_cmd(m):
    q = cmd_arg(m)
    if not q:
        set_state(m.from_user.id, 'search')
        return say(m.chat.id, '🔍 আপনার প্রশ্ন বা কিওয়ার্ড লিখুন:')
    do_search(m.chat.id, q)


@bot.message_handler(commands=['support'])
def support_cmd(m):
    uid = m.from_user.id
    if banned(uid):
        return say(m.chat.id, '🚫 আপনি সাপোর্ট ব্যবহার করতে পারছেন না।')
    set_state(uid, 'tk_new')
    say(m.chat.id, '🎫 আপনার সমস্যা বা প্রশ্ন লিখে পাঠান (ছবি/ফাইলও দিতে পারেন):', kb(nav()))


@bot.message_handler(commands=['mytickets'])
def mytickets_cmd(m):
    mine = [(k, t) for k, t in DB.d['tickets'].items() if t['uid'] == m.from_user.id]
    if not mine:
        return say(m.chat.id, 'আপনার কোনো টিকিট নেই।', kb([btn('🎫 Contact Support', 'tn')]))
    say(m.chat.id, '📋 <b>আপনার টিকিট</b>', kb(*[[btn('%s #%s' % (ST_ICON[t['status']], k), 'tv:%s' % k)] for k, t in mine[-10:]]))


@bot.message_handler(commands=['panel'])
def panel_cmd(m):
    if not is_owner(m.from_user.id):
        return
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['addfaq'])
def addfaq_cmd(m):
    if not is_owner(m.from_user.id):
        return say(m.chat.id, '⚠️ শুধু Owner FAQ যোগ করতে পারবেন।')
    arg = cmd_arg(m)
    if '|' not in arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/addfaq প্রশ্ন|উত্তর</code>\nঅথবা 🛠 Owner Panel → FAQ Manager', kb(nav()))
    q, a = arg.split('|', 1)
    if not q.strip() or not a.strip():
        return say(m.chat.id, '⚠️ প্রশ্ন ও উত্তর দুটোই দিন।')
    cid = default_cat()
    DB.d['cats'][cid]['faqs'][nid()] = {'q': q.strip()[:300], 'a': a.strip()[:3500], 'ph': ''}
    DB.save()
    say(m.chat.id, '✅ FAQ যোগ হয়েছে: <b>%s</b>' % esc(q.strip()))


@bot.message_handler(commands=['delfaq'])
def delfaq_cmd(m):
    if not is_owner(m.from_user.id):
        return say(m.chat.id, '⚠️ শুধু Owner FAQ মুছতে পারবেন।')
    arg = cmd_arg(m).lower()
    if not arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/delfaq প্রশ্ন</code>')
    for cid, cat in DB.d['cats'].items():
        for fid, f in list(cat['faqs'].items()):
            if f['q'].lower() == arg:
                cat['faqs'].pop(fid)
                DB.save()
                return say(m.chat.id, '🗑️ মুছে ফেলা হয়েছে।')
    say(m.chat.id, '❌ এই নামে কোনো FAQ পাওয়া যায়নি।')


# ---------------------------------------------------------------- messages
@bot.message_handler(func=lambda m: m.chat.type == 'private', content_types=['text', 'photo', 'document', 'video', 'audio', 'voice', 'animation', 'video_note', 'sticker'])
def on_any(m):
    uid = m.from_user.id
    if m.text and m.text.startswith('/'):
        return
    s = get_state(uid)
    txt = (m.text or '').strip()
    reg_user(m.from_user)
    if not s:
        if banned(uid):
            return say(m.chat.id, '🚫 আপনি এই বট ব্যবহার করতে পারছেন না।')
        if is_owner(uid) and not txt:
            return
        if txt:
            if not rate_ok(uid, 'free', 1.0):
                return
            return do_search(m.chat.id, txt)
        return say(m.chat.id, '📎 সাপোর্টে ফাইল পাঠাতে প্রথমে 🎫 Contact Support চাপুন।', kb([btn('🎫 Contact Support', 'tn')], nav()))
    key, d = s['k'], s['d']
    if key == 'search':
        clear_state(uid)
        return do_search(m.chat.id, txt) if txt else None
    if key == 'tk_new':
        if banned(uid):
            clear_state(uid)
            return say(m.chat.id, '🚫 আপনি সাপোর্ট ব্যবহার করতে পারছেন না।')
        if not rate_ok(uid, 'ticket', 8):
            return say(m.chat.id, '⏳ একটু অপেক্ষা করে আবার পাঠান।')
        clear_state(uid)
        tid = nid()
        t = {'uid': uid, 'name': uname_of(m.from_user), 'status': 'open', 'msgs': [{'f': 'u', 't': mtext(m)[:1500], 'ts': int(time.time())}], 'created': int(time.time()), 'rating': None}
        DB.d['tickets'][tid] = t
        DB.save()
        notify_owner(tid, t, m, True)
        return say(m.chat.id, '✅ <b>Ticket #%s তৈরি হয়েছে!</b>\nসাপোর্ট টিম শীঘ্রই উত্তর দেবে — উত্তর এলে এখানেই জানতে পারবেন।' % tid, kb([btn('📋 My Tickets', 'tm'), btn('🏠 Home', 'home')]))
    if key == 'tk_ureply':
        t = DB.d['tickets'].get(d['tid'])
        clear_state(uid)
        if not t or t['uid'] != uid or t['status'] == 'closed':
            return say(m.chat.id, '⚠️ টিকিটটি খোলা নেই।')
        t['msgs'].append({'f': 'u', 't': mtext(m)[:1500], 'ts': int(time.time())})
        t['status'] = 'open'
        DB.save()
        notify_owner(d['tid'], t, m, False)
        return say(m.chat.id, '✅ আপনার মেসেজ পাঠানো হয়েছে।', kb([btn('📋 My Tickets', 'tm'), btn('🏠 Home', 'home')]))
    if not is_owner(uid):
        return say(m.chat.id, 'ℹ️ বাটন ব্যবহার করুন, অথবা /cancel দিন।')
    # ---- owner-only states
    if key == 'tk_oreply':
        t = DB.d['tickets'].get(d['tid'])
        clear_state(uid)
        if not t:
            return say(m.chat.id, '⚠️ টিকিট পাওয়া যায়নি।')
        t['msgs'].append({'f': 'o', 't': mtext(m)[:1500], 'ts': int(time.time())})
        t['status'] = 'answered'
        DB.save()
        if m.content_type != 'text':
            _call(bot.copy_message, t['uid'], m.chat.id, m.message_id)
        r = say(t['uid'], '💬 <b>সাপোর্টের উত্তর (Ticket #%s)</b>\n\n%s' % (d['tid'], esc(txt) if txt else '(উপরের মেসেজ দেখুন)'),
                kb([btn('💬 Reply', 'tu:%s' % d['tid']), btn('✅ Resolved', 'tz:%s' % d['tid'])]))
        return say(m.chat.id, '✅ উত্তর পাঠানো হয়েছে।' if r else '⚠️ ইউজারকে মেসেজ পাঠানো যায়নি (বট ব্লক করেছে?)', kb([btn('🎫 Tickets', 'ot:open:0'), btn('🛠 Panel', 'ow')]))
    if key == 'welcome':
        DB.d['welcome'] = '' if txt == '-' else txt[:1500]
        DB.save()
        clear_state(uid)
        return say(m.chat.id, '✅ Welcome মেসেজ আপডেট হয়েছে।', kb([btn('👀 প্রিভিউ', 'home'), btn('🛠 Panel', 'ow')]))
    if key == 'about':
        DB.d['about'] = '' if txt == '-' else txt[:1500]
        DB.save()
        clear_state(uid)
        return say(m.chat.id, '✅ About তথ্য আপডেট হয়েছে।', kb([btn('🛠 Panel', 'ow')]))
    if key == 'bc':
        s['k'] = 'bc_confirm'
        d['chat'], d['mid'] = m.chat.id, m.message_id
        n = len([u for u in DB.d['users'].values() if not u.get('blocked')]) - 1
        return say(m.chat.id, '📢 <b>উপরের মেসেজটি %d জনকে পাঠাবেন?</b>' % max(n, 0), kb([btn('✅ পাঠান', 'bs'), btn('✖ বাতিল', 'ow')]), reply_to=m.message_id)
    if key == 'cat_new':
        cid = nid()
        DB.d['cats'][cid] = {'name': txt[:40], 'faqs': {}}
        DB.save()
        clear_state(uid)
        return say(m.chat.id, '✅ ক্যাটাগরি তৈরি হয়েছে।', kb([btn('➕ FAQ যোগ', 'fma:%s' % cid), btn('📚 Manager', 'fm')]))
    if key == 'cat_ren':
        if d['cid'] in DB.d['cats']:
            DB.d['cats'][d['cid']]['name'] = txt[:40]
            DB.save()
        clear_state(uid)
        return say(m.chat.id, '✅ নাম বদলানো হয়েছে।', kb([btn('📂 ক্যাটাগরি', 'fmc:%s' % d['cid'])]))
    if key == 'fa_q':
        d['q'] = txt[:300]
        s['k'] = 'fa_a'
        return say(m.chat.id, '➕ <b>ধাপ ২/৩</b>\n\n💬 <b>উত্তরটি</b> লিখুন:', kb(nav()))
    if key == 'fa_a':
        d['a'] = txt[:3500]
        s['k'] = 'fa_ph'
        return say(m.chat.id, '➕ <b>ধাপ ৩/৩</b>\n\n🖼 চাইলে একটি <b>ছবি</b> পাঠান, নইলে স্কিপ করুন:', kb([btn('⏭ Skip', 'faskip')], nav()))
    if key == 'fa_ph':
        if m.photo:
            return save_faq(m.chat.id, uid, s, m.photo[-1].file_id)
        return say(m.chat.id, 'ℹ️ ছবি পাঠান অথবা ⏭ Skip চাপুন।')
    if key in ('fe_q', 'fe_a', 'fe_p'):
        f = DB.d['cats'].get(d['cid'], {}).get('faqs', {}).get(d['fid'])
        if not f:
            clear_state(uid)
            return say(m.chat.id, '⚠️ FAQ পাওয়া যায়নি।')
        if key == 'fe_p':
            if m.photo:
                f['ph'] = m.photo[-1].file_id
            elif txt == '-':
                f['ph'] = ''
            else:
                return say(m.chat.id, 'ℹ️ ছবি পাঠান, অথবা সরাতে <code>-</code> লিখুন।')
        elif key == 'fe_q':
            f['q'] = txt[:300]
        else:
            f['a'] = txt[:3500]
        DB.save()
        clear_state(uid)
        return say(m.chat.id, '✅ আপডেট হয়েছে।', kb([btn('✏️ আবার দেখুন', 'fe:%s:%s' % (d['cid'], d['fid'])), btn('📂 ক্যাটাগরি', 'fmc:%s' % d['cid'])]))
    say(m.chat.id, 'ℹ️ বাটন ব্যবহার করুন, অথবা /cancel দিন।')


run(commands=[('start', 'মেনু খুলুন'), ('faq', 'সব FAQ দেখুন'), ('search', 'FAQ খুঁজুন'), ('support', 'সাপোর্টে মেসেজ'),
              ('mytickets', 'আমার টিকিট'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "qr_generator": {
        "emoji": '📷',
        "name": 'QR Code Generator Bot',
        "proj_name": 'QR Generator Bot',
        "category": '🧰 Utility',
        "level": '⭐⭐ Intermediate',
        "setup": '~20 sec',
        "features": ['৮ ধরনের QR — টেক্সট/লিংক, WiFi, Contact, Email, Phone, SMS, লোকেশন ও UPI (সব ধাপে ধাপে বাটনে)', '🎨 Style — ৬টি রঙ, ৫টি ব্যাকগ্রাউন্ড, সাইজ ৩০০–১০০০, এরর-কারেকশন L/M/Q/H', '📎 PNG ও SVG ফাইল ডাউনলোড (প্রিন্টের জন্য)', '🕘 History — আগের ১০টি QR এক ট্যাপে আবার বানান', 'সরাসরি টেক্সট/লিংক পাঠালেও সাথে সাথে QR (একসাথে ৫টা পর্যন্ত)'],
        "commands": '/qr টেক্সট · /wifi SSID|পাস|WPA · /contact নাম|ফোন|ইমেইল',
        "about": 'বাটন-মেনুতে ৮ ধরনের QR বানানো যায় — WiFi, Contact, Email, SMS, লোকেশন, UPI সহ। রঙ/সাইজ বদলানো, PNG/SVG ডাউনলোড ও History আছে।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', '/start দিয়ে মেনু খুলুন, QR-এর ধরন বেছে নিন — বট ধাপে ধাপে তথ্য চাইবে', 'যেকোনো টেক্সট/লিংক সরাসরি পাঠালেও QR পাবেন', '🎨 Style থেকে রঙ, সাইজ ও এরর-কারেকশন বদলান', 'ফলাফলের নিচের বাটনে PNG/SVG ফাইল ও History পাবেন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 /wifi HomeWifi|pass123|WPA\n🤖 [QR ছবি] 📶 WiFi QR রেডি — স্ক্যান করে কানেক্ট করুন!',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('qr_data.json', {'users': {}})
FG = [('⚫ কালো', '000000'), ('🔵 নীল', '1e40af'), ('🔴 লাল', 'b91c1c'), ('🟢 সবুজ', '15803d'), ('🟣 বেগুনি', '6d28d9'), ('🟠 কমলা', 'c2410c')]
BG = [('⚪ সাদা', 'ffffff'), ('🟡 ক্রিম', 'fef9c3'), ('🔷 হালকা নীল', 'dbeafe'), ('🟩 হালকা সবুজ', 'dcfce7'), ('🌸 গোলাপি', 'fce7f3')]
SIZES = [300, 500, 800, 1000]
ECCS = [('L', 'L (7%)'), ('M', 'M (15%)'), ('Q', 'Q (25%)'), ('H', 'H (30%)')]
MAX_LEN = 900


def ucfg(uid):
    u = DB.d['users'].setdefault(str(uid), {})
    u.setdefault('style', {'fg': '000000', 'bg': 'ffffff', 'size': 500, 'ecc': 'M'})
    u.setdefault('hist', [])
    return u


def wifi_esc(s):
    return re.sub(r'([\\;,:"])', r'\\\1', s)


def v_email(s):
    return None if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', s) else '⚠️ সঠিক ইমেইল দিন (যেমন name@example.com)'


def v_phone(s):
    return None if re.fullmatch(r'\+?[0-9][0-9\-\s]{5,18}', norm_digits(s)) else '⚠️ সঠিক ফোন নম্বর দিন (যেমন +8801700000000)'


def v_geo(s):
    mt = re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*', norm_digits(s))
    if not mt or abs(float(mt.group(1))) > 90 or abs(float(mt.group(2))) > 180:
        return '⚠️ লোকেশন পাঠান অথবা লিখুন: <code>23.8103,90.4125</code>'
    return None


def v_upi(s):
    return None if re.fullmatch(r'[\w.\-]{2,}@[\w]{2,}', s) else '⚠️ সঠিক UPI ID দিন (যেমন name@bank)'


def v_amount(s):
    v = to_float(s)
    return None if v is not None and v > 0 else '⚠️ পরিমাণ সংখ্যায় দিন (যেমন 250)'


def b_vcard(v):
    lines = ['BEGIN:VCARD', 'VERSION:3.0', 'FN:' + v['name']]
    if v.get('phone'):
        lines.append('TEL:' + v['phone'])
    if v.get('email'):
        lines.append('EMAIL:' + v['email'])
    if v.get('org'):
        lines.append('ORG:' + v['org'])
    if v.get('url'):
        lines.append('URL:' + v['url'])
    lines.append('END:VCARD')
    return '\n'.join(lines)


def b_mail(v):
    q = {k: v[k] for k in ('subject', 'body') if v.get(k)}
    return 'mailto:' + v['to'] + (('?' + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)) if q else '')


def b_upi(v):
    q = {'pa': v['pa'], 'cu': 'INR'}
    for k, kk in (('pn', 'pn'), ('am', 'am'), ('tn', 'tn')):
        if v.get(k):
            q[kk] = v[k]
    return 'upi://pay?' + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def b_wifi(v):
    sec = v['sec']
    return 'WIFI:T:%s;S:%s;P:%s;H:%s;;' % ('nopass' if sec == 'nopass' else sec, wifi_esc(v['ssid']),
                                          wifi_esc(v.get('pw', '')), 'true' if v.get('hid') == 'Yes' else 'false')


FORMS = {
    'text': {'title': '📝 Text / Link', 'fields': [
        {'k': 'data', 'p': '📝 যে টেক্সট বা লিংকের QR চান সেটা পাঠান:', 't': 'req'}],
        'build': lambda v: v['data'], 'label': lambda v: v['data'][:40]},
    'wifi': {'title': '📶 WiFi', 'fields': [
        {'k': 'ssid', 'p': '📶 WiFi-এর নাম (SSID) লিখুন:', 't': 'req'},
        {'k': 'sec', 'p': '🔐 সিকিউরিটি টাইপ বেছে নিন:', 't': 'choice', 'o': ['WPA', 'WEP', 'nopass']},
        {'k': 'pw', 'p': '🔑 WiFi পাসওয়ার্ড লিখুন:', 't': 'req', 'if': lambda v: v.get('sec') != 'nopass'},
        {'k': 'hid', 'p': '👁 নেটওয়ার্কটি কি হিডেন?', 't': 'choice', 'o': ['No', 'Yes']}],
        'build': b_wifi, 'label': lambda v: 'WiFi: ' + v['ssid'][:30]},
    'contact': {'title': '👤 Contact (vCard)', 'fields': [
        {'k': 'name', 'p': '👤 পুরো নাম লিখুন:', 't': 'req'},
        {'k': 'phone', 'p': '📞 ফোন নম্বর:', 't': 'opt', 'check': v_phone},
        {'k': 'email', 'p': '📧 ইমেইল:', 't': 'opt', 'check': v_email},
        {'k': 'org', 'p': '🏢 কোম্পানি / প্রতিষ্ঠান:', 't': 'opt'},
        {'k': 'url', 'p': '🌐 ওয়েবসাইট:', 't': 'opt'}],
        'build': b_vcard, 'label': lambda v: 'Contact: ' + v['name'][:30]},
    'mail': {'title': '📧 Email', 'fields': [
        {'k': 'to', 'p': '📧 প্রাপকের ইমেইল:', 't': 'req', 'check': v_email},
        {'k': 'subject', 'p': '📌 সাবজেক্ট:', 't': 'opt'},
        {'k': 'body', 'p': '✉️ মেসেজ:', 't': 'opt'}],
        'build': b_mail, 'label': lambda v: 'Email: ' + v['to'][:30]},
    'tel': {'title': '📞 Phone', 'fields': [
        {'k': 'n', 'p': '📞 ফোন নম্বর লিখুন (কান্ট্রি কোডসহ):', 't': 'req', 'check': v_phone}],
        'build': lambda v: 'tel:' + norm_digits(v['n']).replace(' ', ''), 'label': lambda v: 'Phone: ' + v['n'][:20]},
    'sms': {'title': '💬 SMS', 'fields': [
        {'k': 'n', 'p': '📞 যে নম্বরে SMS যাবে:', 't': 'req', 'check': v_phone},
        {'k': 'msg', 'p': '💬 মেসেজের টেক্সট:', 't': 'opt'}],
        'build': lambda v: 'SMSTO:%s:%s' % (norm_digits(v['n']).replace(' ', ''), v.get('msg', '')), 'label': lambda v: 'SMS: ' + v['n'][:20]},
    'geo': {'title': '📍 Location', 'fields': [
        {'k': 'loc', 'p': '📍 লোকেশন শেয়ার করুন (📎 → Location) অথবা লিখুন: <code>23.8103,90.4125</code>', 't': 'req', 'check': v_geo}],
        'build': lambda v: 'geo:' + re.sub(r'\s+', ',', norm_digits(v['loc']).strip().replace(',', ' ')), 'label': lambda v: 'Location: ' + v['loc'][:25]},
    'upi': {'title': '💸 UPI Payment', 'fields': [
        {'k': 'pa', 'p': '💸 UPI ID লিখুন (যেমন name@bank):', 't': 'req', 'check': v_upi},
        {'k': 'pn', 'p': '👤 প্রাপকের নাম:', 't': 'opt'},
        {'k': 'am', 'p': '💰 পরিমাণ (খালি রাখলে প্রদানকারী নিজে দেবে):', 't': 'opt', 'check': v_amount},
        {'k': 'tn', 'p': '📝 নোট:', 't': 'opt'}],
        'build': b_upi, 'label': lambda v: 'UPI: ' + v['pa'][:30]},
}
ORDER = ['text', 'wifi', 'contact', 'mail', 'tel', 'sms', 'geo', 'upi']

HELP = ('❓ <b>QR Generator — সাহায্য</b>\n' + LINE + '\n'
        '• মেনু থেকে টাইপ বেছে নিন, বট ধাপে ধাপে তথ্য চাইবে\n'
        '• সরাসরি টেক্সট/লিংক পাঠালেও সাথে সাথে QR পাবেন (একাধিক লাইনে সর্বোচ্চ ৫টা)\n'
        '• <code>/qr টেক্সট</code> — দ্রুত QR\n'
        '• <code>/wifi SSID|পাসওয়ার্ড|WPA</code>\n'
        '• <code>/contact নাম|ফোন|ইমেইল</code>\n'
        '• 🎨 Style থেকে রঙ, সাইজ ও এরর-কারেকশন বদলান\n'
        '• 🕘 History থেকে আগের QR আবার বানান\n'
        '• /cancel — চলমান ধাপ বাতিল\n\n'
        '💡 লোগো/ডিজাইন ছাপানোর জন্য Q বা H এরর-কারেকশন আর বড় সাইজ ভালো।')


def home_view(uid):
    rows = []
    for i in range(0, len(ORDER), 2):
        rows.append([btn(FORMS[k]['title'], 'qf:' + k) for k in ORDER[i:i + 2]])
    rows.append([btn('🎨 Style', 'qs'), btn('🕘 History', 'qh')])
    rows.append([btn('❓ Help', 'qhelp')])
    return ('📷 <b>QR Code Generator</b>\n' + LINE + '\nকী ধরনের QR বানাতে চান? নিচ থেকে বেছে নিন,\nঅথবা সরাসরি যেকোনো টেক্সট/লিংক পাঠান।' + FOOTER), kb(*rows)


def qr_bytes(payload, st, fmt='png'):
    params = {'size': '%dx%d' % (st['size'], st['size']), 'margin': 10, 'data': payload, 'color': st['fg'],
              'bgcolor': st['bg'], 'ecc': st['ecc'], 'format': fmt}
    status, body = http('https://api.qrserver.com/v1/create-qr-code/', params=params, timeout=25)
    return body if status == 200 and body else None


def result_kb():
    return kb([btn('🎨 Style', 'qs'), btn('📎 PNG ফাইল', 'qd:png'), btn('📎 SVG', 'qd:svg')],
              [btn('🔁 নতুন QR', 'home'), btn('🕘 History', 'qh')])


def gen(chat_id, uid, payload, label, remember=True):
    if len(payload) > MAX_LEN:
        say(chat_id, '⚠️ ডাটা অনেক বড় (%d অক্ষর)। সর্বোচ্চ %d অক্ষর দিন।' % (len(payload), MAX_LEN), kb(nav()))
        return False
    u = ucfg(uid)
    try:
        bot.send_chat_action(chat_id, 'upload_photo')
    except Exception:
        pass
    data = None
    try:
        data = qr_bytes(payload, u['style'])
    except Exception as e:
        log('qr fetch', repr(e))
    if not data:
        say(chat_id, '⚠️ QR বানানো যায়নি — QR সার্ভার এখন সাড়া দিচ্ছে না। একটু পরে আবার চেষ্টা করুন।', kb(nav()))
        return False
    bio = io.BytesIO(data)
    bio.name = 'qr.png'
    with DB.lock:
        u['last'] = {'p': payload, 'l': label}
        if remember:
            u['hist'] = [h for h in u['hist'] if h['p'] != payload]
            u['hist'].insert(0, {'p': payload, 'l': label, 't': int(time.time())})
            del u['hist'][10:]
        DB.save()
    _call(bot.send_photo, chat_id, bio, caption='✅ <b>QR কোড রেডি!</b>\n📌 %s\n\n<i>ক্যামেরা দিয়ে স্ক্যান করুন।</i>%s' % (esc(label[:80]), FOOTER),
          parse_mode='HTML', reply_markup=result_kb())
    return True


# ---------------------------------------------------------------- guided forms
def cur_field(d):
    fields = FORMS[d['t']]['fields']
    while d['i'] < len(fields):
        f = fields[d['i']]
        cond = f.get('if')
        if cond and not cond(d['v']):
            d['i'] += 1
            continue
        return f
    return None


def ask_next(chat_id, uid, c=None):
    s = get_state(uid)
    if not s or s['k'] != 'form':
        return
    d = s['d']
    f = cur_field(d)
    if f is None:
        form = FORMS[d['t']]
        v = d['v']
        clear_state(uid)
        gen(chat_id, uid, form['build'](v), form['label'](v))
        return
    rows = []
    if f['t'] == 'choice':
        rows.append([btn(o, 'qfc:%d' % i) for i, o in enumerate(f['o'])])
    if f['t'] == 'opt':
        rows.append([btn('⏭ Skip', 'qfs')])
    rows.append([btn('✖ Cancel', 'home')])
    total = len(FORMS[d['t']]['fields'])
    text = '%s\n<b>ধাপ %d/%d</b>\n\n%s' % (FORMS[d['t']]['title'], d['i'] + 1, total, f['p'])
    if c is not None:
        edit(c, text, kb(*rows))
    else:
        say(chat_id, text, kb(*rows))


@route('qf')
def cb_form(c, a):
    key = a[0] if a else ''
    if key not in FORMS:
        return ack(c, 'অজানা অপশন')
    ack(c)
    set_state(c.from_user.id, 'form', t=key, i=0, v={})
    ask_next(c.message.chat.id, c.from_user.id, c)


@route('qfc')
def cb_choice(c, a):
    s = get_state(c.from_user.id)
    if not s or s['k'] != 'form':
        return ack(c, 'সেশন শেষ হয়ে গেছে, আবার শুরু করুন', True)
    d = s['d']
    f = cur_field(d)
    idx = to_int(a[0] if a else '', -1)
    if not f or f['t'] != 'choice' or not 0 <= idx < len(f['o']):
        return ack(c)
    ack(c)
    d['v'][f['k']] = f['o'][idx]
    d['i'] += 1
    ask_next(c.message.chat.id, c.from_user.id, c)


@route('qfs')
def cb_skip(c, a):
    s = get_state(c.from_user.id)
    if not s or s['k'] != 'form':
        return ack(c, 'সেশন শেষ হয়ে গেছে, আবার শুরু করুন', True)
    ack(c)
    s['d']['i'] += 1
    ask_next(c.message.chat.id, c.from_user.id, c)


def accept_value(m, text):
    uid = m.from_user.id
    s = get_state(uid)
    d = s['d']
    f = cur_field(d)
    if f is None:
        return
    if f['t'] == 'choice':
        say(m.chat.id, 'ℹ️ উপরের বাটন থেকে একটি অপশন বেছে নিন।')
        return
    val = text.strip()
    chk = f.get('check')
    err = chk(val) if chk else None
    if err:
        say(m.chat.id, err)
        return
    d['v'][f['k']] = val
    d['i'] += 1
    ask_next(m.chat.id, uid)


# ---------------------------------------------------------------- style / history / files
def style_view(uid):
    st = ucfg(uid)['style']
    fgn = dict((h, n) for n, h in FG).get(st['fg'], '#' + st['fg'])
    bgn = dict((h, n) for n, h in BG).get(st['bg'], '#' + st['bg'])
    text = ('🎨 <b>QR Style</b>\n' + LINE + '\n🖌 রঙ: <b>%s</b>\n🎞 ব্যাকগ্রাউন্ড: <b>%s</b>\n📐 সাইজ: <b>%dx%d</b>\n🛡 এরর-কারেকশন: <b>%s</b>\n\n'
            '<i>পরিবর্তন করলে পরের সব QR-এ প্রযোজ্য হবে। শেষ QR নতুন স্টাইলে দেখতে "🔄 শেষ QR আবার" চাপুন।</i>'
            % (fgn, bgn, st['size'], st['size'], st['ecc']))
    rows = [[btn(('✅ ' if st['fg'] == h else '') + n, 'qs:fg:' + h) for n, h in FG[:3]],
            [btn(('✅ ' if st['fg'] == h else '') + n, 'qs:fg:' + h) for n, h in FG[3:]],
            [btn(('✅ ' if st['bg'] == h else '') + n, 'qs:bg:' + h) for n, h in BG[:3]],
            [btn(('✅ ' if st['bg'] == h else '') + n, 'qs:bg:' + h) for n, h in BG[3:]],
            [btn(('✅ ' if st['size'] == s else '') + str(s), 'qs:sz:%d' % s) for s in SIZES],
            [btn(('✅ ' if st['ecc'] == k else '') + lbl, 'qs:ec:' + k) for k, lbl in ECCS],
            [btn('🔄 শেষ QR আবার', 'qs:go'), btn('♻️ Reset', 'qs:rs')],
            nav()]
    return text, kb(*rows)


@route('qs')
def cb_style(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    st = u['style']
    if len(a) >= 2:
        k, v = a[0], a[1]
        if k == 'fg' and v in [h for _, h in FG]:
            st['fg'] = v
        elif k == 'bg' and v in [h for _, h in BG]:
            st['bg'] = v
        elif k == 'sz' and to_int(v) in SIZES:
            st['size'] = to_int(v)
        elif k == 'ec' and v in [x for x, _ in ECCS]:
            st['ecc'] = v
        DB.save()
        if st['fg'] == st['bg']:
            ack(c, '⚠️ রঙ ও ব্যাকগ্রাউন্ড একই — স্ক্যান হবে না!', True)
    elif a and a[0] == 'rs':
        u['style'] = {'fg': '000000', 'bg': 'ffffff', 'size': 500, 'ecc': 'M'}
        DB.save()
        ack(c, '♻️ ডিফল্টে ফেরানো হয়েছে')
    elif a and a[0] == 'go':
        last = u.get('last')
        if not last:
            return ack(c, 'এখনো কোনো QR বানাননি', True)
        ack(c, '⏳ বানানো হচ্ছে...')
        gen(c.message.chat.id, uid, last['p'], last['l'], remember=False)
        return
    t, k = style_view(uid)
    edit(c, t, k)


@route('qd')
def cb_file(c, a):
    uid = c.from_user.id
    last = ucfg(uid).get('last')
    fmt = a[0] if a and a[0] in ('png', 'svg') else 'png'
    if not last:
        return ack(c, 'আগে একটি QR বানান', True)
    ack(c, '⏳ ফাইল তৈরি হচ্ছে...')
    try:
        data = qr_bytes(last['p'], ucfg(uid)['style'], fmt)
    except Exception:
        data = None
    if not data:
        return say(c.message.chat.id, '⚠️ ফাইল বানানো যায়নি, একটু পরে চেষ্টা করুন।')
    bio = io.BytesIO(data)
    bio.name = 'qr.' + fmt
    _call(bot.send_document, c.message.chat.id, bio, caption='📎 <b>QR (%s)</b>\n%s' % (fmt.upper(), esc(last['l'][:80])), parse_mode='HTML')


@route('qh')
def cb_hist(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    if a and a[0] == 'c':
        u['hist'] = []
        DB.save()
        ack(c, '🗑 History মুছে ফেলা হয়েছে')
    elif a and a[0] == 'g':
        i = to_int(a[1] if len(a) > 1 else '', -1)
        if not 0 <= i < len(u['hist']):
            return ack(c, 'পাওয়া যায়নি', True)
        ack(c, '⏳ বানানো হচ্ছে...')
        h = u['hist'][i]
        gen(c.message.chat.id, uid, h['p'], h['l'])
        return
    if not u['hist']:
        return edit(c, '🕘 <b>History</b>\n\nএখনো কোনো QR বানাননি।', kb(nav()))
    rows = [[btn('🔁 %s' % h['l'][:36], 'qh:g:%d' % i)] for i, h in enumerate(u['hist'])]
    rows.append([btn('🗑 সব মুছুন', 'qh:c')])
    rows.append(nav())
    edit(c, '🕘 <b>আপনার শেষ %d টি QR</b>\nচাপ দিলে আবার তৈরি হবে।' % len(u['hist']), kb(*rows))


@route('qhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


# ---------------------------------------------------------------- commands & messages
@bot.message_handler(commands=['start', 'help', 'menu'])
def start(m):
    clear_state(m.from_user.id)
    if (m.text or '').startswith('/help'):
        say(m.chat.id, HELP, kb(nav()))
        return
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['qr'])
def qr_cmd(m):
    arg = cmd_arg(m)
    if not arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/qr আপনার টেক্সট</code>')
    gen(m.chat.id, m.from_user.id, arg, arg[:40])


@bot.message_handler(commands=['wifi'])
def wifi_cmd(m):
    arg = cmd_arg(m)
    if arg.count('|') < 1:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/wifi SSID|পাসওয়ার্ড|WPA</code>\nঅথবা মেনু থেকে 📶 WiFi বেছে নিন।', kb(nav()))
    bits = [x.strip() for x in arg.split('|')]
    sec = bits[2].upper() if len(bits) > 2 else 'WPA'
    sec = sec if sec in ('WPA', 'WEP') else ('nopass' if sec == 'NOPASS' else 'WPA')
    v = {'ssid': bits[0], 'pw': bits[1], 'sec': sec, 'hid': 'No'}
    gen(m.chat.id, m.from_user.id, b_wifi(v), 'WiFi: ' + bits[0][:30])


@bot.message_handler(commands=['contact'])
def contact_cmd(m):
    arg = cmd_arg(m)
    if arg.count('|') < 1:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/contact নাম|ফোন|ইমেইল</code>\nঅথবা মেনু থেকে 👤 Contact বেছে নিন।', kb(nav()))
    bits = [x.strip() for x in arg.split('|')]
    v = {'name': bits[0], 'phone': bits[1] if len(bits) > 1 else '', 'email': bits[2] if len(bits) > 2 else ''}
    gen(m.chat.id, m.from_user.id, b_vcard(v), 'Contact: ' + bits[0][:30])


@bot.message_handler(content_types=['location'])
def on_location(m):
    s = get_state(m.from_user.id)
    if s and s['k'] == 'form' and s['d']['t'] == 'geo':
        accept_value(m, '%s,%s' % (m.location.latitude, m.location.longitude))
    else:
        gen(m.chat.id, m.from_user.id, 'geo:%s,%s' % (m.location.latitude, m.location.longitude), 'Location')


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    if s and s['k'] == 'form':
        return accept_value(m, m.text)
    if not rate_ok(uid, 'qr', 1.0):
        return say(m.chat.id, '⏳ একটু ধীরে — একসাথে অনেক মেসেজ পাঠাবেন না।')
    lines = [l.strip() for l in m.text.splitlines() if l.strip()][:5]
    if len(lines) == 1:
        gen(m.chat.id, uid, lines[0], lines[0][:40])
    else:
        for i, line in enumerate(lines, 1):
            gen(m.chat.id, uid, line, '%d/%d: %s' % (i, len(lines), line[:40]))


run(commands=[('start', 'মেনু খুলুন'), ('qr', 'দ্রুত QR বানান'), ('wifi', 'WiFi QR'), ('contact', 'Contact QR'),
              ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "poll_bot": {
        "emoji": '📊',
        "name": 'Quick Poll Bot',
        "proj_name": 'Poll Bot',
        "category": '📊 Engagement',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~30 sec',
        "features": ['বাটন-প্যানেলে পোল বিল্ডার — Anonymous / Public / Multiple / Quiz (সঠিক উত্তর + ব্যাখ্যা সহ)', '⏱ টাইমার ৩০ সেকেন্ড থেকে ২৪ ঘণ্টা — সময় শেষে অটো বন্ধ', '📣 নিজের চ্যানেল/গ্রুপে পাঠান — টার্গেট সেভ করা থাকে', '⏰ শিডিউল: একবার / প্রতিদিন / সাপ্তাহিক — আপনার Timezone অনুযায়ী, লিস্ট থেকে Pause/Now/Delete', '📊 প্রতিটি পোলের নিচে Live ও Stop-Results বাটন (শুধু নির্মাতা/অ্যাডমিন)', '⚡ Quick Poll ও পুরনো /poll কমান্ডও চালু'],
        "commands": '/poll · /pollpublic · /pollmulti প্রশ্ন | অপশন … · /schedulepoll HH:MM | প্রশ্ন | অপশন …',
        "about": 'পূর্ণাঙ্গ পোল বট — বাটনে পোল বিল্ডার, কুইজ-মোড, টাইমার, চ্যানেল/গ্রুপে পাঠানো, শিডিউল ও লাইভ রেজাল্ট।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', '/start → ➕ নতুন Poll চাপুন, প্রশ্ন ও অপশন পাঠান', 'প্যানেলে টাইপ/টাইমার/টার্গেট বেছে 🚀 পাঠান বা ⏰ Schedule করুন', 'চ্যানেলে পাঠাতে 📣 Targets-এ যোগ করুন (বটকে সেখানে Admin করুন)', 'পোলের নিচের 📊 Live / 🛑 Stop বাটনে রেজাল্ট দেখুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 /schedulepoll 20:00 | রাতে কী খাবো? | ভাত | বিরিয়ানি\n🤖 ✅ রাত ৮টায় অটো পোল চালু হবে!\n[৮টায়] 🤖 📊 রাতে কী খাবো?',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('polls.json', {'users': {}, 'targets': {}, 'sched': [], 'polls': {}, 'live': {}, 'next': 1})
TYPES = [('anon', '🔒 Anonymous'), ('public', '👁 Public'), ('multi', '☑️ Multiple'), ('quiz', '🧠 Quiz')]
TYPE_NAME = dict(TYPES)
TIMERS = [(0, '♾ নেই'), (30, '30 সেকেন্ড'), (60, '1 মিনিট'), (300, '5 মিনিট'), (1800, '30 মিনিট'), (3600, '1 ঘণ্টা'), (86400, '24 ঘণ্টা')]
TIMER_NAME = dict(TIMERS)
WD_BN = ['সোম', 'মঙ্গল', 'বুধ', 'বৃহ', 'শুক্র', 'শনি', 'রবি']
TZ_CHOICES = [('🇧🇩 +6', 360), ('🇮🇳 +5:30', 330), ('🇵🇰 +5', 300), ('🇦🇪 +4', 240), ('🇸🇦 +3', 180), ('🇬🇧 +0', 0),
              ('🇪🇺 +1', 60), ('🇺🇸 -5', -300), ('🇺🇸 -8', -480), ('🇲🇾 +8', 480), ('🇯🇵 +9', 540), ('🇦🇺 +10', 600)]
MAX_SCHED = 20
MAX_TARGETS = 8


def ucfg(uid):
    u = DB.d['users'].setdefault(str(uid), {})
    u.setdefault('tz', 6.0)
    return u


def targets(uid):
    return DB.d['targets'].setdefault(str(uid), [])


def new_poll():
    return {'q': '', 'opts': [], 'type': 'anon', 'close': 0, 'tid': None, 'correct': 0, 'expl': ''}


def poll_valid(p):
    return bool(p['q'].strip()) and 2 <= len(p['opts']) <= 10


def parse_pipe(raw):
    items = [x.strip() for x in raw.split('|') if x.strip()]
    if len(items) < 3 or len(items) > 11:
        return None
    return items[0][:300], [i[:100] for i in items[1:]]


# ---------------------------------------------------------------- sending
def send_poll_obj(chat_id, p, creator):
    kw = {'is_anonymous': p['type'] in ('anon', 'multi', 'quiz'), 'allows_multiple_answers': p['type'] == 'multi'}
    if p['type'] == 'public':
        kw['is_anonymous'] = False
    if p['type'] == 'quiz':
        kw['type'] = 'quiz'
        kw['correct_option_id'] = min(p.get('correct', 0), len(p['opts']) - 1)
        kw['allows_multiple_answers'] = False
        if p.get('expl'):
            kw['explanation'] = p['expl'][:200]
    if p.get('close'):
        if p['close'] <= 600:
            kw['open_period'] = p['close']
        else:
            kw['close_date'] = int(time.time()) + p['close']
    try:
        msg = bot.send_poll(chat_id, p['q'][:300], [o[:100] for o in p['opts']], **kw)
    except TypeError as e:
        log('send_poll typeerror', e)
        return None
    except Exception as e:
        log('send_poll', str(e)[:160])
        return str(e)[:160]
    key = '%s:%s' % (chat_id, msg.message_id)
    with DB.lock:
        DB.d['polls'][key] = {'by': creator, 'q': p['q'][:80]}
        while len(DB.d['polls']) > 400:
            DB.d['polls'].pop(next(iter(DB.d['polls'])))
        pid = getattr(getattr(msg, 'poll', None), 'id', None)
        if pid:
            DB.d['live'][str(pid)] = key
            while len(DB.d['live']) > 400:
                DB.d['live'].pop(next(iter(DB.d['live'])))
        DB.save()
    _call(bot.edit_message_reply_markup, chat_id, msg.message_id,
          reply_markup=kb([btn('📊 Live', 'pr:l:%s:%s' % (chat_id, msg.message_id)), btn('🛑 Stop & Results', 'pr:s:%s:%s' % (chat_id, msg.message_id))]))
    return True


def bar(pct):
    n = int(round(pct / 10.0))
    return '▓' * n + '░' * (10 - n)


def results_text(title, opts, total, closed):
    lines = ['📊 <b>%s</b>%s\n' % (esc(title), ' (বন্ধ)' if closed else '')]
    for text, cnt in opts:
        pct = (cnt * 100.0 / total) if total else 0
        lines.append('%s %d%%  <b>%s</b> (%d)' % (bar(pct), round(pct), esc(text), cnt))
    lines.append('\n🗳 মোট ভোট: <b>%d</b>' % total)
    return '\n'.join(lines)


@route('pr')
def cb_results(c, a):
    if len(a) < 3:
        return ack(c)
    act, chat, mid = a[0], a[1], a[2]
    key = '%s:%s' % (chat, mid)
    info = DB.d['polls'].get(key)
    uid = c.from_user.id
    allowed = (info and info['by'] == uid) or is_chat_admin(to_int(chat, 0), uid)
    if not allowed:
        return ack(c, '🚫 শুধু পোল-নির্মাতা বা অ্যাডমিন', True)
    if act == 's':
        try:
            p = bot.stop_poll(to_int(chat), to_int(mid))
        except Exception as e:
            return ack(c, 'পোল আগেই বন্ধ হয়েছে বা পাওয়া যায়নি', True)
        ack(c, '🛑 পোল বন্ধ করা হয়েছে')
        text = results_text(p.question, [(o.text, o.voter_count) for o in p.options], p.total_voter_count, True)
        _call(bot.edit_message_reply_markup, to_int(chat), to_int(mid), reply_markup=None)
        return say(c.message.chat.id, text)
    live = None
    for pid, k in DB.d['live'].items():
        if k == key:
            live = DB.d.setdefault('state', {}).get(pid)
    if not live:
        return ack(c, 'এখনো কোনো ভোট আসেনি বা তথ্য পাওয়া যায়নি', True)
    ack(c)
    say(c.message.chat.id, results_text(live['q'], live['opts'], live['total'], False))


@bot.poll_handler(func=lambda p: True)
def on_poll_state(p):
    try:
        with DB.lock:
            DB.d.setdefault('state', {})[str(p.id)] = {'q': p.question, 'opts': [(o.text, o.voter_count) for o in p.options], 'total': p.total_voter_count}
            while len(DB.d['state']) > 400:
                DB.d['state'].pop(next(iter(DB.d['state'])))
    except Exception as e:
        log('poll state', repr(e))


# ---------------------------------------------------------------- home
def home_view(uid):
    n = len([s for s in DB.d['sched'] if s['owner'] == uid])
    text = ('📊 <b>Poll Bot</b>\n' + LINE + '\nঅনলাইন পোল, কুইজ, শিডিউল ও চ্যানেল/গ্রুপে পাঠানো — সব বাটনে।\n\n'
            '📅 আপনার শিডিউল: <b>%d</b>  •  📣 টার্গেট: <b>%d</b>' % (n, len(targets(uid))) + FOOTER)
    return text, kb([btn('➕ নতুন Poll', 'pn'), btn('⚡ Quick Poll', 'pq')],
                    [btn('📅 Scheduled (%d)' % n, 'sl'), btn('📣 Targets', 'pt')],
                    [btn('🌍 Timezone', 'tz'), btn('❓ Help', 'phelp')])


HELP = ('❓ <b>Poll Bot — সাহায্য</b>\n' + LINE + '\n'
        '➕ <b>নতুন Poll:</b> প্রশ্ন → অপশন → প্যানেলে টাইপ/টাইমার/টার্গেট বেছে 🚀 Send বা ⏰ Schedule\n'
        '🧩 টাইপ: Anonymous, Public, Multiple, Quiz (সঠিক উত্তর + ব্যাখ্যাসহ)\n'
        '⏱ টাইমার: ৩০ সেকেন্ড থেকে ২৪ ঘণ্টা পর্যন্ত — সময় শেষে অটো বন্ধ\n'
        '📣 টার্গেট: আপনার চ্যানেল/গ্রুপে পাঠান (বটকে অ্যাডমিন করুন)\n'
        '⏰ শিডিউল: একবার / প্রতিদিন / প্রতি সপ্তাহে — নির্দিষ্ট সময়ে অটো পোল\n'
        '📊 প্রতিটি পোলের নিচে <b>Live</b> ও <b>Stop &amp; Results</b> বাটন (শুধু নির্মাতা/অ্যাডমিন)\n\n'
        '⌨️ গ্রুপে/চ্যাটে দ্রুত:\n<code>/poll প্রশ্ন | অপশন ১ | অপশন ২</code>\n<code>/pollpublic ...</code> · <code>/pollmulti ...</code>\n'
        '<code>/schedulepoll HH:MM | প্রশ্ন | অপশন ১ | অপশন ২</code> (প্রতিদিন)')


@route('phelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


# ---------------------------------------------------------------- builder
@route('pn')
def cb_new(c, a):
    ack(c)
    set_state(c.from_user.id, 'pb_q', p=new_poll(), chat=c.message.chat.id)
    edit(c, '➕ <b>নতুন Poll — ধাপ ১</b>\n\n❓ পোলের প্রশ্ন লিখে পাঠান (সর্বোচ্চ ৩০০ অক্ষর):', kb(nav()))


def opts_view(p):
    lines = ['➕ <b>নতুন Poll — ধাপ ২</b>\n\n❓ <b>%s</b>\n' % esc(p['q'])]
    for i, o in enumerate(p['opts']):
        lines.append('%s. %s' % (chr(65 + i), esc(o)))
    lines.append('\n📝 অপশন পাঠান (প্রতি মেসেজে একটি, অথবা প্রতি লাইনে একটি)।\nন্যূনতম ২টি, সর্বোচ্চ ১০টি — এখন %d টি।' % len(p['opts']))
    rows = []
    if len(p['opts']) >= 2:
        rows.append([btn('✅ Done (%d)' % len(p['opts']), 'po:done')])
    rows.append([btn('↩️ শেষটি মুছুন', 'po:undo'), btn('✖ Cancel', 'home')])
    return '\n'.join(lines), kb(*rows)


@route('po')
def cb_opts(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    if not s or s['k'] not in ('pb_opts', 'pb_q'):
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    p = s['d']['p']
    act = a[0] if a else ''
    if act == 'undo':
        if p['opts']:
            p['opts'].pop()
        ack(c)
        t, k = opts_view(p)
        return edit(c, t, k)
    if act == 'done':
        if len(p['opts']) < 2:
            return ack(c, 'কমপক্ষে ২টি অপশন দিন', True)
        ack(c)
        s['k'] = 'pb_panel'
        t, k = panel_view(uid)
        return edit(c, t, k)


def panel_view(uid):
    s = get_state(uid)
    p = s['d']['p']
    tg = 'এই চ্যাট'
    for t in targets(uid):
        if t['id'] == p['tid']:
            tg = t['title']
    lines = ['📊 <b>Poll প্যানেল</b>\n' + LINE + '\n❓ <b>%s</b>\n' % esc(p['q'])]
    for i, o in enumerate(p['opts']):
        mark = ' ✅' if p['type'] == 'quiz' and i == p['correct'] else ''
        lines.append('%s. %s%s' % (chr(65 + i), esc(o), mark))
    lines.append('\n🧩 টাইপ: <b>%s</b>\n⏱ টাইমার: <b>%s</b>\n📍 টার্গেট: <b>%s</b>' % (TYPE_NAME[p['type']], TIMER_NAME.get(p['close'], '%ds' % p['close']), esc(tg)))
    if p['type'] == 'quiz':
        lines.append('💡 ব্যাখ্যা: %s' % (esc(p['expl'][:60]) if p['expl'] else '—'))
    rows = [[btn('🧩 টাইপ: %s' % TYPE_NAME[p['type']], 'pp:t'), btn('⏱ %s' % TIMER_NAME.get(p['close'], '?'), 'pp:m')],
            [btn('📍 %s' % tg[:20], 'pp:g'), btn('➕ টার্গেট', 'pt:add')]]
    if p['type'] == 'quiz':
        rows.append([btn('✅ সঠিক: %s' % chr(65 + p['correct']), 'pp:c'), btn('💡 ব্যাখ্যা', 'pp:e')])
    rows.append([btn('🚀 এখনই পাঠান', 'pp:go'), btn('⏰ Schedule', 'pp:sc')])
    rows.append([btn('✏️ প্রশ্ন', 'pp:q'), btn('🧹 অপশন নতুন করে', 'pp:o'), btn('✖', 'home')])
    return '\n'.join(lines), kb(*rows)


@route('pp')
def cb_panel(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    act = a[0] if a else ''
    if s and act == 'back' and s['k'] in ('pb_sched', 'pb_when'):
        s['k'] = 'pb_panel'
    if not s or s['k'] != 'pb_panel':
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    p = s['d']['p']
    if act == 't':
        keys = [k for k, _ in TYPES]
        p['type'] = keys[(keys.index(p['type']) + 1) % len(keys)]
    elif act == 'm':
        vals = [v for v, _ in TIMERS]
        p['close'] = vals[(vals.index(p['close']) + 1) % len(vals)] if p['close'] in vals else 0
    elif act == 'g':
        ids = [None] + [t['id'] for t in targets(uid)]
        p['tid'] = ids[(ids.index(p['tid']) + 1) % len(ids)] if p['tid'] in ids else None
    elif act == 'c':
        p['correct'] = (p['correct'] + 1) % len(p['opts'])
    elif act == 'e':
        ack(c)
        s['k'] = 'pb_expl'
        return edit(c, '💡 <b>ব্যাখ্যা লিখুন</b> (সঠিক উত্তরের পরে দেখাবে, সর্বোচ্চ ২০০ অক্ষর)।\nমুছতে <code>-</code> পাঠান।', kb(nav()))
    elif act == 'q':
        ack(c)
        s['k'] = 'pb_edit_q'
        return edit(c, '✏️ নতুন প্রশ্ন লিখে পাঠান:', kb(nav()))
    elif act == 'o':
        ack(c)
        p['opts'] = []
        s['k'] = 'pb_opts'
        t, k = opts_view(p)
        return edit(c, t, k)
    elif act == 'go':
        if not poll_valid(p):
            return ack(c, 'প্রশ্ন ও ২–১০টি অপশন লাগবে', True)
        chat = p['tid'] or s['d']['chat']
        res = send_poll_obj(chat, p, uid)
        if res is True:
            ack(c, '🚀 পাঠানো হয়েছে')
            clear_state(uid)
            return edit(c, '✅ <b>পোল পাঠানো হয়েছে!</b>' + ('\n📍 টার্গেটে পাঠানো হয়েছে।' if p['tid'] else ''), kb([btn('➕ আরেকটি', 'pn'), btn('🏠 Home', 'home')]))
        return ack(c, ('⚠️ পাঠানো যায়নি: %s' % res) if isinstance(res, str) else '⚠️ পাঠানো যায়নি', True)
    elif act == 'sc':
        if not poll_valid(p):
            return ack(c, 'প্রশ্ন ও ২–১০টি অপশন লাগবে', True)
        if len([x for x in DB.d['sched'] if x['owner'] == uid]) >= MAX_SCHED:
            return ack(c, 'সর্বোচ্চ %d টি শিডিউল' % MAX_SCHED, True)
        ack(c)
        s['k'] = 'pb_sched'
        return edit(c, '⏰ <b>কখন পাঠাবেন?</b>\n\n🌍 আপনার সময়: <b>%s</b> (%s)' % (fmt_ts(time.time(), ucfg(uid)['tz'], '%d %b, %H:%M'), tz_label(ucfg(uid)['tz'])),
                    kb([btn('1️⃣ একবার', 'ps:m:once'), btn('📆 প্রতিদিন', 'ps:m:daily')], [btn('🗓 প্রতি সপ্তাহে', 'ps:m:weekly')], nav('pp:back')))
    ack(c)
    t, k = panel_view(uid)
    edit(c, t, k)


@route('ps')
def cb_sched(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    if not s or s['k'] not in ('pb_sched', 'pb_when', 'pb_panel'):
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    d = s['d']
    if a[0] == 'm' and len(a) > 1 and a[1] in ('once', 'daily', 'weekly'):
        ack(c)
        d['mode'] = a[1]
        if a[1] == 'weekly':
            rows = [[btn(n, 'ps:w:%d' % i) for i, n in enumerate(WD_BN[:4])], [btn(n, 'ps:w:%d' % (i + 4)) for i, n in enumerate(WD_BN[4:])], nav('pp:back')]
            return edit(c, '🗓 <b>কোন বারে?</b>', kb(*rows))
        s['k'] = 'pb_when'
        hint = ('<code>HH:MM</code> (আজ/কাল), <code>2026-12-25 18:30</code>, <code>2h</code>' if a[1] == 'once' else '<code>HH:MM</code> (যেমন 20:00)')
        return edit(c, '⏰ <b>সময় লিখুন</b>\n\n%s\n🌍 %s' % (hint, tz_label(ucfg(uid)['tz'])), kb(nav('pp:back')))
    if a[0] == 'w' and len(a) > 1:
        wd = to_int(a[1], -1)
        if not 0 <= wd <= 6:
            return ack(c)
        ack(c)
        d['mode'], d['wd'] = 'weekly', wd
        s['k'] = 'pb_when'
        return edit(c, '⏰ <b>%s-বার কয়টায়?</b>\n\n<code>HH:MM</code> লিখুন (যেমন 20:00)\n🌍 %s' % (WD_BN[wd], tz_label(ucfg(uid)['tz'])), kb(nav('pp:back')))


def parse_clock(txt, off):
    t = norm_digits(txt).strip().lower()
    mt = re.fullmatch(r'(\d{1,2}):(\d{2})\s*(am|pm)?', t)
    if not mt:
        return None
    h, mi = int(mt.group(1)), int(mt.group(2))
    if mt.group(3) == 'pm' and h < 12:
        h += 12
    if mt.group(3) == 'am' and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def first_ts(mode, txt, off, wd=None):
    now_l = local_dt(time.time(), off)
    t = norm_digits(txt).strip()
    if mode == 'once':
        secs = parse_duration(t)
        if secs:
            return time.time() + secs
        for fmt in ('%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M'):
            try:
                return local_to_ts(datetime.datetime.strptime(t, fmt), off)
            except ValueError:
                pass
    hm = parse_clock(t, off)
    if not hm:
        return None
    cand = now_l.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if mode == 'weekly':
        delta = (wd - cand.weekday()) % 7
        cand += datetime.timedelta(days=delta)
        if cand <= now_l:
            cand += datetime.timedelta(days=7)
    elif cand <= now_l:
        cand += datetime.timedelta(days=1)
    return local_to_ts(cand, off)


def sched_when(sc, off):
    base = fmt_ts(sc['ts'], off, '%d %b %H:%M')
    if sc['mode'] == 'daily':
        return 'প্রতিদিন %s (পরের: %s)' % (fmt_ts(sc['ts'], off, '%H:%M'), base)
    if sc['mode'] == 'weekly':
        return '%s-বার %s (পরের: %s)' % (WD_BN[local_dt(sc['ts'], off).weekday()], fmt_ts(sc['ts'], off, '%H:%M'), base)
    return 'একবার — %s' % base


def add_sched(uid, chat, p, mode, ts):
    with DB.lock:
        sid = DB.d['next']
        DB.d['next'] += 1
        DB.d['sched'].append({'id': sid, 'owner': uid, 'chat': chat, 'poll': json.loads(json.dumps(p)), 'mode': mode, 'ts': ts, 'paused': False, 'fails': 0})
        DB.save()
    return sid


# ---------------------------------------------------------------- scheduled list
@route('sl')
def cb_list(c, a):
    uid = c.from_user.id
    ack(c)
    off = ucfg(uid)['tz']
    mine = sorted([s for s in DB.d['sched'] if s['owner'] == uid], key=lambda x: x['ts'])
    if not mine:
        return edit(c, '📅 <b>Scheduled Polls</b>\n\nকোনো শিডিউল নেই।', kb([btn('➕ নতুন Poll', 'pn')], nav()))
    rows = [[btn('%s %s • %s' % ('⏸' if s['paused'] else '⏰', s['poll']['q'][:20], fmt_ts(s['ts'], off, '%d %b %H:%M')), 'sv:%d' % s['id'])] for s in mine[:12]]
    rows.append(nav())
    edit(c, '📅 <b>Scheduled Polls (%d)</b>\nচাপ দিয়ে ম্যানেজ করুন। 🌍 %s' % (len(mine), tz_label(off)), kb(*rows))


def sc_by_id(uid, sid):
    for s in DB.d['sched']:
        if s['id'] == sid and s['owner'] == uid:
            return s
    return None


@route('sv')
def cb_sched_view(c, a):
    uid = c.from_user.id
    sc = sc_by_id(uid, to_int(a[0] if a else '', -1))
    if not sc:
        return ack(c, 'শিডিউলটি পাওয়া যায়নি', True)
    ack(c)
    p, off = sc['poll'], ucfg(uid)['tz']
    txt = ('⏰ <b>Scheduled Poll #%d</b>\n' % sc['id'] + LINE + '\n❓ <b>%s</b>\n%s\n\n🧩 %s  •  ⏱ %s\n🕒 %s%s' %
           (esc(p['q']), '\n'.join('%s. %s' % (chr(65 + i), esc(o)) for i, o in enumerate(p['opts'])), TYPE_NAME[p['type']], TIMER_NAME.get(p['close'], '?'),
            sched_when(sc, off), '\n⏸ <b>পজ করা</b>' if sc['paused'] else ''))
    edit(c, txt, kb([btn('▶️ Resume' if sc['paused'] else '⏸ Pause', 'sa:p:%d' % sc['id']), btn('🚀 এখনই', 'sa:n:%d' % sc['id'])], [btn('🗑 ডিলিট', 'sa:d:%d' % sc['id'])], nav('sl')))


@route('sa')
def cb_sched_act(c, a):
    uid = c.from_user.id
    if len(a) < 2:
        return ack(c)
    sc = sc_by_id(uid, to_int(a[1], -1))
    if not sc:
        return ack(c, 'শিডিউলটি পাওয়া যায়নি', True)
    if a[0] == 'p':
        sc['paused'] = not sc['paused']
        sc['fails'] = 0
        DB.save()
        ack(c, '⏸ পজ' if sc['paused'] else '▶️ চালু')
        return cb_sched_view(c, [str(sc['id'])])
    if a[0] == 'n':
        res = send_poll_obj(sc['chat'], sc['poll'], uid)
        return ack(c, '🚀 পাঠানো হয়েছে' if res is True else '⚠️ পাঠানো যায়নি', res is not True)
    if a[0] == 'd':
        DB.d['sched'] = [s for s in DB.d['sched'] if s['id'] != sc['id']]
        DB.save()
        ack(c, '🗑 ডিলিট হয়েছে')
        return cb_list(c, [])


def sched_tick():
    now_t = time.time()
    with DB.lock:
        due = [s for s in DB.d['sched'] if not s['paused'] and s['ts'] <= now_t]
    changed = False
    for sc in due:
        res = send_poll_obj(sc['chat'], sc['poll'], sc['owner'])
        changed = True
        if res is True:
            sc['fails'] = 0
            if sc['mode'] == 'once':
                DB.d['sched'] = [s for s in DB.d['sched'] if s['id'] != sc['id']]
            else:
                step = 86400 if sc['mode'] == 'daily' else 7 * 86400
                while sc['ts'] <= now_t:
                    sc['ts'] += step
        else:
            sc['fails'] += 1
            sc['ts'] = now_t + 60 * sc['fails']
            if sc['fails'] >= 3:
                sc['paused'] = True
                say(sc['owner'], '⚠️ <b>শিডিউল পোল পাঠানো যায়নি</b> (#%d)\n"%s"\n\nকারণ: %s\nবটকে টার্গেট চ্যাটে অ্যাডমিন করে ▶️ Resume করুন।' % (sc['id'], esc(sc['poll']['q'][:50]), esc(str(res or 'অজানা'))),
                    kb([btn('📅 Scheduled', 'sl')]))
    if changed:
        DB.save()


# ---------------------------------------------------------------- targets
def targets_view(uid):
    ts = targets(uid)
    lines = ['📣 <b>Targets</b>\n' + LINE + '\nআপনার চ্যানেল/গ্রুপে পোল পাঠাতে এখানে যোগ করুন।\n<i>বটকে সেখানে অ্যাডমিন করতে হবে (চ্যানেলে "Post messages" অনুমতি)।</i>\n']
    rows = []
    for i, t in enumerate(ts):
        lines.append('• %s' % esc(t['title']))
        rows.append([btn('🗑 %s' % t['title'][:28], 'pt:d:%d' % i)])
    if not ts:
        lines.append('এখনো কোনো টার্গেট নেই।')
    if len(ts) < MAX_TARGETS:
        rows.append([btn('➕ টার্গেট যোগ করুন', 'pt:add')])
    rows.append(nav())
    return '\n'.join(lines), kb(*rows)


@route('pt')
def cb_targets(c, a):
    uid = c.from_user.id
    ts = targets(uid)
    if a and a[0] == 'add':
        ack(c)
        if len(ts) >= MAX_TARGETS:
            return ack(c, 'সর্বোচ্চ %d টি টার্গেট' % MAX_TARGETS, True)
        prev = get_state(uid)
        set_state(uid, 'pt_add')
        if prev and prev['k'] == 'pb_panel':
            STATE[uid]['keep'] = prev
        return say(c.message.chat.id, '📣 <b>টার্গেট যোগ</b>\n\nচ্যানেল/গ্রুপের <code>@username</code> অথবা আইডি (<code>-100...</code>) পাঠান।\nঅথবা সেখান থেকে যেকোনো মেসেজ এখানে ফরওয়ার্ড করুন।\n\n⚠️ আগে বটকে সেই চ্যাটে অ্যাডমিন করুন।', kb(nav()))
    if a and a[0] == 'd':
        i = to_int(a[1] if len(a) > 1 else '', -1)
        if 0 <= i < len(ts):
            ts.pop(i)
            DB.save()
            ack(c, '🗑 সরানো হয়েছে')
    else:
        ack(c)
    t, k = targets_view(uid)
    edit(c, t, k)


def add_target(m, ref):
    uid = m.from_user.id
    s = get_state(uid)
    keep = s['keep'] if s and s.get('keep') else None
    try:
        chat = bot.get_chat(ref)
    except Exception:
        return say(m.chat.id, '⚠️ চ্যাটটি খুঁজে পাইনি। বটকে সেখানে যোগ করে আবার চেষ্টা করুন।')
    if not is_chat_admin(chat.id, uid, fresh=True):
        return say(m.chat.id, '🚫 আপনি ওই চ্যাটের অ্যাডমিন নন।')
    try:
        bst = bot.get_chat_member(chat.id, me().id).status
    except Exception:
        bst = ''
    if bst not in ('administrator', 'creator') and chat.type == 'channel':
        return say(m.chat.id, '⚠️ বট এই চ্যানেলে অ্যাডমিন নয়। অ্যাডমিন করে আবার পাঠান।')
    if bst not in ('administrator', 'creator', 'member'):
        return say(m.chat.id, '⚠️ বট এই চ্যাটে নেই। আগে যোগ করুন।')
    ts = targets(uid)
    if not any(t['id'] == chat.id for t in ts):
        ts.append({'id': chat.id, 'title': (chat.title or str(chat.id))[:40]})
        DB.save()
    clear_state(uid)
    if keep:
        STATE[uid] = keep
        keep['t'] = time.time()
        t, k = panel_view(uid)
        return say(m.chat.id, '✅ টার্গেট যোগ হয়েছে: <b>%s</b>\n\n' % esc(chat.title or chat.id) + t, k)
    t, k = targets_view(uid)
    say(m.chat.id, '✅ টার্গেট যোগ হয়েছে: <b>%s</b>\n\n' % esc(chat.title or chat.id) + t, k)


# ---------------------------------------------------------------- quick poll & tz
@route('pq')
def cb_quick(c, a):
    ack(c)
    set_state(c.from_user.id, 'quick', chat=c.message.chat.id)
    edit(c, '⚡ <b>Quick Poll</b>\n\nএক মেসেজে লিখুন:\n<code>প্রশ্ন | অপশন ১ | অপশন ২ | অপশন ৩</code>', kb(nav()))


def tz_view(uid):
    off = ucfg(uid)['tz']
    rows = []
    for i in range(0, len(TZ_CHOICES), 3):
        rows.append([btn(('✅ ' if int(off * 60) == m else '') + lbl, 'tz:s:%d' % m) for lbl, m in TZ_CHOICES[i:i + 3]])
    rows.append([btn('✏️ নিজে লিখুন', 'tz:c')])
    rows.append(nav())
    return ('🌍 <b>Timezone</b>\n' + LINE + '\nবর্তমান: <b>%s</b>\nআপনার এখনকার সময় (বট অনুযায়ী): <b>%s</b>\n\nশিডিউল পোল এই সময় অনুযায়ী চলবে।' % (tz_label(off), fmt_ts(time.time(), off, '%d %b, %H:%M'))), kb(*rows)


@route('tz')
def cb_tz(c, a):
    uid = c.from_user.id
    if len(a) >= 2 and a[0] == 's':
        m = to_int(a[1])
        if m is not None and -720 <= m <= 840:
            ucfg(uid)['tz'] = m / 60.0
            DB.save()
            ack(c, '✅ Timezone সেট হয়েছে')
    elif a and a[0] == 'c':
        ack(c)
        set_state(uid, 'tz_custom')
        return edit(c, '✏️ UTC অফসেট লিখুন, যেমন <code>+6</code>, <code>+5:30</code>, <code>-4</code>', kb(nav('tz')))
    t, k = tz_view(uid)
    edit(c, t, k)


# ---------------------------------------------------------------- commands
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    clear_state(m.from_user.id)
    if m.chat.type != 'private':
        return say(m.chat.id, '📊 <b>Poll Bot</b>\n\nগ্রুপে দ্রুত পোল: <code>/poll প্রশ্ন | অপশন ১ | অপশন ২</code>\nপূর্ণ ফিচারের জন্য আমাকে প্রাইভেটে খুলুন 👇', kb([btn('📊 Open Poll Bot', url=bot_link())]))
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()) if m.chat.type == 'private' else None)


def quick(m, ptype):
    parsed = parse_pipe(cmd_arg(m))
    if not parsed:
        return say(m.chat.id, '⚠️ একটা প্রশ্ন এবং ২–১০টা অপশন দিন।\n\n<code>/poll প্রশ্ন | অপশন ১ | অপশন ২</code>', reply_to=m.message_id)
    p = new_poll()
    p['q'], p['opts'], p['type'] = parsed[0], parsed[1], ptype
    res = send_poll_obj(m.chat.id, p, m.from_user.id)
    if res is not True:
        say(m.chat.id, '⚠️ পোল পাঠানো যায়নি।' + ((' কারণ: %s' % esc(res)) if isinstance(res, str) else ''))


@bot.message_handler(commands=['poll'])
def poll_cmd(m):
    quick(m, 'anon')


@bot.message_handler(commands=['pollpublic'])
def pollpublic_cmd(m):
    quick(m, 'public')


@bot.message_handler(commands=['pollmulti'])
def pollmulti_cmd(m):
    quick(m, 'multi')


@bot.message_handler(commands=['schedulepoll'])
def schedule_cmd(m):
    arg = cmd_arg(m)
    if '|' not in arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/schedulepoll HH:MM | প্রশ্ন | অপশন ১ | অপশন ২</code>')
    tpart, _, rest = arg.partition('|')
    off = ucfg(m.from_user.id)['tz']
    hm = parse_clock(tpart, off)
    items = [x.strip() for x in rest.split('|') if x.strip()]
    if not hm:
        return say(m.chat.id, '⚠️ সময় HH:MM ফরম্যাটে দিন (যেমন 20:00)')
    if len(items) < 3:
        return say(m.chat.id, '⚠️ একটা প্রশ্ন এবং কমপক্ষে ২টা অপশন দিন।')
    if len([x for x in DB.d['sched'] if x['owner'] == m.from_user.id]) >= MAX_SCHED:
        return say(m.chat.id, '⚠️ সর্বোচ্চ %d টি শিডিউল।' % MAX_SCHED)
    p = new_poll()
    p['q'], p['opts'] = items[0][:300], [i[:100] for i in items[1:11]]
    ts = first_ts('daily', '%02d:%02d' % hm, off)
    sid = add_sched(m.from_user.id, m.chat.id, p, 'daily', ts)
    say(m.chat.id, '✅ প্রতিদিন <b>%02d:%02d</b> (%s)-এ অটো পোল শিডিউল হয়েছে! (#%d)\nপরের পোল: %s' % (hm[0], hm[1], tz_label(off), sid, fmt_ts(ts, off)),
        kb([btn('📅 Scheduled', 'sl')]) if m.chat.type == 'private' else None)


# ---------------------------------------------------------------- text & forwards
def forwarded_chat(m):
    fo = getattr(m, 'forward_origin', None)
    ch = getattr(fo, 'chat', None) if fo else None
    return ch or getattr(m, 'forward_from_chat', None)


@bot.message_handler(func=lambda m: m.chat.type == 'private' and (bool(m.text) and not m.text.startswith('/') or forwarded_chat(m) is not None),
                     content_types=['text', 'photo', 'video', 'document', 'audio', 'voice', 'animation', 'sticker'])
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = (m.text or '').strip()
    if not s:
        if txt:
            t, k = home_view(uid)
            say(m.chat.id, '💡 নতুন পোল বানাতে ➕ বাটন চাপুন।\n\n' + t, k)
        return
    key, d = s['k'], s['d']
    if key == 'pt_add':
        fc = forwarded_chat(m)
        if fc is not None:
            return add_target(m, fc.id)
        if not txt:
            return
        ref = txt if txt.startswith('@') else (to_int(txt) if to_int(txt) is not None else '@' + txt)
        return add_target(m, ref)
    if not txt:
        return
    if key == 'quick':
        parsed = parse_pipe(txt)
        if not parsed:
            return say(m.chat.id, '⚠️ প্রশ্ন + ২–১০টা অপশন দিন: <code>প্রশ্ন | অপশন ১ | অপশন ২</code>')
        p = new_poll()
        p['q'], p['opts'] = parsed
        clear_state(uid)
        res = send_poll_obj(d['chat'], p, uid)
        return say(m.chat.id, '✅ পোল পাঠানো হয়েছে।' if res is True else '⚠️ পাঠানো যায়নি।', kb([btn('⚡ আরেকটি', 'pq'), btn('🏠 Home', 'home')]))
    if key in ('pb_q', 'pb_edit_q'):
        d['p']['q'] = txt[:300]
        if key == 'pb_edit_q':
            s['k'] = 'pb_panel'
            t, k = panel_view(uid)
            return say(m.chat.id, t, k)
        s['k'] = 'pb_opts'
        t, k = opts_view(d['p'])
        return say(m.chat.id, t, k)
    if key == 'pb_opts':
        p = d['p']
        for line in [x.strip() for x in txt.splitlines() if x.strip()]:
            if len(p['opts']) >= 10:
                break
            p['opts'].append(line[:100])
        t, k = opts_view(p)
        return say(m.chat.id, t, k)
    if key == 'pb_expl':
        d['p']['expl'] = '' if txt == '-' else txt[:200]
        s['k'] = 'pb_panel'
        t, k = panel_view(uid)
        return say(m.chat.id, t, k)
    if key == 'pb_when':
        off = ucfg(uid)['tz']
        ts = first_ts(d['mode'], txt, off, d.get('wd'))
        if not ts or ts < time.time() + 3 or ts > time.time() + 366 * 86400:
            return say(m.chat.id, '⚠️ সময় বুঝতে পারিনি বা অতীতে। আবার লিখুন (যেমন <code>20:00</code>)।')
        p = d['p']
        chat = p['tid'] or d['chat']
        sid = add_sched(uid, chat, p, d['mode'], ts)
        clear_state(uid)
        sc = sc_by_id(uid, sid)
        return say(m.chat.id, '✅ <b>শিডিউল হয়েছে!</b> (#%d)\n\n❓ %s\n🕒 %s' % (sid, esc(p['q']), sched_when(sc, off)), kb([btn('📅 Scheduled', 'sl'), btn('➕ নতুন', 'pn')], nav()))
    if key == 'tz_custom':
        off = parse_tz(txt)
        if off is None:
            return say(m.chat.id, '⚠️ সঠিক অফসেট দিন (যেমন +6 বা -4:30)।')
        clear_state(uid)
        ucfg(uid)['tz'] = off
        DB.save()
        t, k = tz_view(uid)
        return say(m.chat.id, '✅ Timezone সেট হয়েছে।\n\n' + t, k)
    say(m.chat.id, 'ℹ️ উপরের বাটন ব্যবহার করুন, অথবা /cancel দিয়ে বাতিল করুন।')


start_worker(sched_tick, 20)
run(commands=[('start', 'মেনু খুলুন'), ('poll', 'দ্রুত পোল (Anonymous)'), ('pollpublic', 'Public পোল'), ('pollmulti', 'Multiple পোল'),
              ('schedulepoll', 'প্রতিদিনের অটো পোল'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')],
    allowed=['message', 'callback_query', 'poll'])
''',
    },
    "weather_bot": {
        "emoji": '🌤️',
        "name": 'Weather Info Bot',
        "proj_name": 'Weather Bot',
        "category": '🌦️ Info & Lifestyle',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~20 sec',
        "features": ['শহরের নাম বা 📍 লোকেশন — লাইভ আবহাওয়া, অনুভূত তাপমাত্রা, আর্দ্রতা, বাতাস, UV', '৪টি ভিউ বাটনে: এখন / ঘণ্টাভিত্তিক / ৩ দিন / সূর্য-চাঁদ', '°C ↔ °F, ⭐ Favorites, 🔄 Refresh', '🔔 Daily Alerts — ৩টি শহর, প্রতিটার সময় আলাদা (ঘণ্টা বেছে নিন)', '🌧 Rain Alert — বৃষ্টির সম্ভাবনা ৭০%+ হলে আগাম সতর্কতা', '🌍 Timezone সেট (বাংলাদেশ ডিফল্ট)'],
        "commands": '/weather শহর · /forecast শহর · /subscribe শহর · /unsubscribe',
        "about": 'বাটন-চালিত আবহাওয়া বট — ঘণ্টাভিত্তিক/৩ দিনের ভিউ, ফেভারিট শহর, নিজের সময়ে ডেইলি অ্যালার্ট ও বৃষ্টির আগাম সতর্কতা।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', 'শহরের নাম লিখুন (যেমন Dhaka) বা 📍 লোকেশন শেয়ার করুন', 'কার্ডের বাটনে ঘণ্টা/৩ দিন/সূর্য ভিউ, °C/°F ও ⭐ Save', '🔔 Daily বাটনে অ্যালার্টের সময় বেছে নিন; 🌍 Timezone ঠিক রাখুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 Dhaka\n🤖 🌤️ Dhaka: এখন +৩১°C, আংশিক মেঘলা\n   📅 আগামী ৩ দিন: ৩২°/৩০°/২৯°',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('weather.json', {'users': {}, 'q': {}})
WC = TTL(600, 200)
DAYS_BN = ['সোম', 'মঙ্গল', 'বুধ', 'বৃহ', 'শুক্র', 'শনি', 'রবি']
TZ_CHOICES = [('🇧🇩 +6', 360), ('🇮🇳 +5:30', 330), ('🇵🇰 +5', 300), ('🇦🇪 +4', 240), ('🇸🇦 +3', 180), ('🇬🇧 +0', 0),
              ('🇪🇺 +1', 60), ('🇺🇸 -5', -300), ('🇺🇸 -8', -480), ('🇲🇾 +8', 480), ('🇯🇵 +9', 540), ('🇦🇺 +10', 600)]
MAX_FAV, MAX_SUB = 8, 3
SAVED = 'কোনো তথ্য নেই'


def ucfg(uid):
    u = DB.d['users'].setdefault(str(uid), {})
    u.setdefault('unit', 'C')
    u.setdefault('fav', [])
    u.setdefault('subs', [])
    u.setdefault('tz', 6.0)
    u.setdefault('rain', True)
    u.setdefault('recent', [])
    u.setdefault('rain_sent', {})
    return u


def qid_for(query):
    q = DB.d['q']
    for k, v in q.items():
        if v == query:
            return k
    k = secrets.token_hex(3)
    q[k] = query
    while len(q) > 400:
        q.pop(next(iter(q)))
    DB.save()
    return k


def query_of(qid):
    return DB.d['q'].get(qid)


def num(x, d=0):
    try:
        return int(float(x))
    except Exception:
        return d


def wicon(code, desc=''):
    c = num(code, -1)
    if c == 113:
        return '☀️'
    if c == 116:
        return '⛅'
    if c in (119, 122):
        return '☁️'
    if c in (143, 248, 260):
        return '🌫️'
    if c in (200, 386, 389, 392, 395):
        return '⛈️'
    if c in (179, 182, 185, 227, 230, 281, 284, 311, 314, 317, 320, 323, 326, 329, 332, 335, 338, 350, 362, 365, 368, 371, 374, 377):
        return '❄️'
    if c in (176, 263, 266, 293, 296, 299, 302, 305, 308, 353, 356, 359):
        return '🌧️'
    d = (desc or '').lower()
    return '🌧️' if 'rain' in d else ('☀️' if 'sun' in d or 'clear' in d else '🌤️')


def parse_weather(j, query):
    cur = j['current_condition'][0]
    area = (j.get('nearest_area') or [{}])[0]

    def first(x, d=''):
        try:
            return x[0]['value']
        except Exception:
            return d
    name = first(area.get('areaName'), query)
    country = first(area.get('country'), '')
    try:
        lt = datetime.datetime.strptime(cur.get('localObsDateTime', ''), '%Y-%m-%d %I:%M %p')
    except Exception:
        lt = datetime.datetime.now()
    out = {'name': name, 'country': country, 'lt': lt.strftime('%Y-%m-%d %H:%M'),
           'cur': {'c': num(cur.get('temp_C')), 'f': num(cur.get('temp_F')), 'fc': num(cur.get('FeelsLikeC')), 'ff': num(cur.get('FeelsLikeF')),
                   'hum': num(cur.get('humidity')), 'wind': num(cur.get('windspeedKmph')), 'dir': cur.get('winddir16Point', ''),
                   'press': num(cur.get('pressure')), 'vis': num(cur.get('visibility')), 'uv': num(cur.get('uvIndex')),
                   'cloud': num(cur.get('cloudcover')), 'precip': cur.get('precipMM', '0'), 'desc': first(cur.get('weatherDesc'), ''),
                   'code': cur.get('weatherCode')},
           'days': []}
    for d in (j.get('weather') or [])[:3]:
        astro = (d.get('astronomy') or [{}])[0]
        hrs = []
        for h in d.get('hourly') or []:
            hrs.append({'h': num(h.get('time')) // 100, 'c': num(h.get('tempC')), 'f': num(h.get('tempF')), 'rain': num(h.get('chanceofrain')),
                        'desc': first(h.get('weatherDesc'), ''), 'code': h.get('weatherCode'), 'wind': num(h.get('windspeedKmph'))})
        out['days'].append({'date': d.get('date', ''), 'maxc': num(d.get('maxtempC')), 'minc': num(d.get('mintempC')),
                            'maxf': num(d.get('maxtempF')), 'minf': num(d.get('mintempF')), 'sunrise': astro.get('sunrise', '-'),
                            'sunset': astro.get('sunset', '-'), 'moon': astro.get('moon_phase', '-'), 'uv': num(d.get('uvIndex')), 'hours': hrs})
    return out


def fetch_weather(query):
    key = query.lower()
    hit = WC.get(key)
    if hit:
        return hit
    try:
        st, j = http_json('https://wttr.in/' + urllib.parse.quote(query, safe=''), params={'format': 'j1'}, timeout=15)
    except Exception as e:
        log('wttr', repr(e))
        raise RuntimeError('network')
    if st == 200 and isinstance(j, dict) and j.get('current_condition'):
        try:
            data = parse_weather(j, query)
        except Exception as e:
            log('parse', repr(e))
            raise RuntimeError('parse')
        WC.set(key, data)
        return data
    if st in (404, 400) or (st == 200 and j is None):
        return None
    raise RuntimeError('http %s' % st)


def T(c, f, unit):
    return '%d°%s' % ((f if unit == 'F' else c), unit)


def upcoming(data, n=8):
    now = data['lt']
    out = []
    for d in data['days']:
        for h in d['hours']:
            stamp = '%s %02d:00' % (d['date'], h['h'])
            if stamp >= now[:13] + ':00' or stamp > now:
                out.append((d['date'], h))
    return out[:n]


def day_name(date_s):
    try:
        dt = datetime.datetime.strptime(date_s, '%Y-%m-%d')
        return '%s (%s)' % (DAYS_BN[dt.weekday()], dt.strftime('%d %b'))
    except Exception:
        return date_s


def render(data, view, unit):
    c = data['cur']
    head = '%s <b>%s%s</b>' % (wicon(c['code'], c['desc']), esc(data['name']), (', ' + esc(data['country'])) if data['country'] else '')
    if view == 'hourly':
        lines = [head + '\n🕒 <b>আগামী ঘণ্টাগুলো</b>\n' + LINE]
        for date_s, h in upcoming(data, 8):
            lines.append('%02d:00  %s <b>%s</b>  🌧 %d%%  💨 %d' % (h['h'], wicon(h['code'], h['desc']), T(h['c'], h['f'], unit), h['rain'], h['wind']))
        if len(lines) == 1:
            lines.append('তথ্য পাওয়া যায়নি।')
        return '\n'.join(lines) + '\n\n<i>🌧 = বৃষ্টির সম্ভাবনা • 💨 = বাতাস (km/h) • স্থানীয় সময়</i>'
    if view == '3day':
        lines = [head + '\n📅 <b>৩ দিনের পূর্বাভাস</b>\n' + LINE]
        for d in data['days']:
            rain = max([h['rain'] for h in d['hours']] or [0])
            mid = d['hours'][len(d['hours']) // 2] if d['hours'] else None
            ic = wicon(mid['code'], mid['desc']) if mid else '🌤️'
            lines.append('%s <b>%s</b>\n   ↑ %s  ↓ %s  🌧 %d%%  🔆 UV %d' % (ic, day_name(d['date']), T(d['maxc'], d['maxf'], unit), T(d['minc'], d['minf'], unit), rain, d['uv']))
        return '\n'.join(lines)
    if view == 'astro':
        lines = [head + '\n🌅 <b>সূর্য ও চাঁদ</b>\n' + LINE]
        for d in data['days']:
            lines.append('<b>%s</b>\n   🌅 %s   🌇 %s\n   🌙 %s' % (day_name(d['date']), d['sunrise'], d['sunset'], d['moon']))
        return '\n'.join(lines)
    today = data['days'][0] if data['days'] else None
    rng = ''
    if today:
        rain = max([h['rain'] for h in today['hours']] or [0])
        rng = '\n📅 আজ: ↑ %s  ↓ %s  🌧 %d%%' % (T(today['maxc'], today['maxf'], unit), T(today['minc'], today['minf'], unit), rain)
        sun = '\n🌅 %s   🌇 %s' % (today['sunrise'], today['sunset'])
    else:
        sun = ''
    return ('%s\n' % head + LINE + '\n%s <b>%s</b>\n🌡 <b>%s</b>  (অনুভূত %s)\n💧 আর্দ্রতা %d%%   💨 %d km/h %s\n🔆 UV %d   👁 %d km   🔽 %d hPa   ☁️ %d%%%s%s\n\n🕒 স্থানীয় সময়: %s'
            % (wicon(c['code'], c['desc']), esc(c['desc']), T(c['c'], c['f'], unit), T(c['fc'], c['ff'], unit), c['hum'], c['wind'], c['dir'],
               c['uv'], c['vis'], c['press'], c['cloud'], rng, sun, data['lt'][11:]))


def wkb(qid, view, unit, is_fav):
    def tab(label, v):
        return btn(('• ' if v == view else '') + label, 'wv:%s:%s' % (v, qid))
    return kb([tab('🌡 এখন', 'now'), tab('🕒 ঘণ্টা', 'hourly'), tab('📅 ৩ দিন', '3day'), tab('🌅 সূর্য', 'astro')],
              [btn('🔄 Refresh', 'wr:%s:%s' % (view, qid)), btn('°F' if unit == 'C' else '°C', 'wu:%s:%s' % (view, qid)),
               btn('💔 Unsave' if is_fav else '⭐ Save', 'wf:%s:%s' % (view, qid)), btn('🔔 Daily', 'sb:n:%s' % qid)],
              nav())


def show(chat_id, uid, query, view='now', c=None, force=False):
    u = ucfg(uid)
    if force:
        WC.pop(query.lower())
    try:
        data = fetch_weather(query)
    except RuntimeError:
        txt = '⚠️ এই মুহূর্তে আবহাওয়া সার্ভারে পৌঁছানো যাচ্ছে না। একটু পরে আবার চেষ্টা করুন।'
        return edit(c, txt, kb(nav())) if c else say(chat_id, txt, kb(nav()))
    if data is None:
        txt = '🔎 <b>"%s"</b> নামে কোনো জায়গা পাইনি।\n\n💡 ইংরেজিতে শহরের নাম লিখুন (যেমন <code>Dhaka</code>) অথবা 📍 লোকেশন শেয়ার করুন।' % esc(query[:40])
        return edit(c, txt, kb(nav())) if c else say(chat_id, txt, kb(nav()))
    if query in u['recent']:
        u['recent'].remove(query)
    u['recent'].insert(0, query)
    del u['recent'][5:]
    DB.save()
    qid = qid_for(query)
    text = render(data, view, u['unit']) + FOOTER
    markup = wkb(qid, view, u['unit'], query in u['fav'])
    if c:
        edit(c, text, markup)
    else:
        say(chat_id, text, markup)


# ---------------------------------------------------------------- home
def home_view(uid):
    u = ucfg(uid)
    rows = []
    favs = u['fav']
    for i in range(0, len(favs), 2):
        rows.append([btn('⭐ %s' % q[:18], 'wq:%s' % qid_for(q)) for q in favs[i:i + 2]])
    rec = [q for q in u['recent'] if q not in favs][:2]
    if rec:
        rows.append([btn('🕘 %s' % q[:18], 'wq:%s' % qid_for(q)) for q in rec])
    rows.append([btn('🔍 শহর খুঁজুন', 'wsearch'), btn('📍 আমার লোকেশন', 'wloc')])
    rows.append([btn('⭐ Favorites', 'wfav'), btn('🔔 Alerts', 'sb')])
    rows.append([btn('🌍 Timezone', 'tz'), btn('❓ Help', 'whelp')])
    text = ('🌤️ <b>Weather Bot</b>\n' + LINE + '\nশহরের নাম লিখুন (ইংরেজিতে), যেমন <code>Dhaka</code>\nঅথবা লোকেশন শেয়ার করুন।\n\n'
            '🌡 ইউনিট: <b>°%s</b> • 🌍 সময়: <b>%s</b>' % (u['unit'], tz_label(u['tz'])) + FOOTER)
    return text, kb(*rows)


HELP = ('❓ <b>Weather Bot — সাহায্য</b>\n' + LINE + '\n'
        '• শহরের নাম লিখলেই আবহাওয়া দেখাবে (ইংরেজিতে ভালো কাজ করে)\n'
        '• 📍 লোকেশন শেয়ার করলে সরাসরি সেই জায়গার আবহাওয়া\n'
        '• কার্ডের বাটন: এখন / ঘণ্টাভিত্তিক / ৩ দিন / সূর্য-চাঁদ, °C↔°F, ⭐ Save\n'
        '• 🔔 Daily — প্রতিদিন আপনার পছন্দের সময়ে অটো আপডেট (সর্বোচ্চ %d টি শহর)\n'
        '• 🌧 Rain Alert — Daily Alert-এর শহরে বৃষ্টির সম্ভাবনা বেশি হলে আগাম জানাবে\n'
        '• 🌍 Timezone ঠিক রাখুন, নাহলে অ্যালার্ট ভুল সময়ে আসবে\n\n'
        '⌨️ <code>/weather শহর</code> · <code>/forecast শহর</code> · <code>/subscribe শহর</code> · <code>/unsubscribe</code>' % MAX_SUB)


@route('whelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


@route('wsearch')
def cb_search(c, a):
    ack(c)
    set_state(c.from_user.id, 'search')
    edit(c, '🔍 <b>শহরের নাম লিখুন</b>\nযেমন: <code>Dhaka</code>, <code>Chittagong</code>, <code>London</code>', kb(nav()))


@route('wloc')
def cb_loc(c, a):
    ack(c)
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    mk.add(types.KeyboardButton('📍 আমার লোকেশন পাঠান', request_location=True))
    say(c.message.chat.id, '📍 নিচের বাটনে চাপ দিন (অথবা 📎 → Location দিয়ে যেকোনো জায়গা পাঠান)।', mk)


@route('wq')
def cb_quick(c, a):
    q = query_of(a[0]) if a else None
    if not q:
        return ack(c, 'পুরনো বাটন — আবার সার্চ করুন', True)
    ack(c)
    show(c.message.chat.id, c.from_user.id, q, 'now', c)


@route('wv')
def cb_view(c, a):
    if len(a) < 2 or a[0] not in ('now', 'hourly', '3day', 'astro'):
        return ack(c)
    q = query_of(a[1])
    if not q:
        return ack(c, 'পুরনো বাটন — আবার সার্চ করুন', True)
    ack(c)
    show(c.message.chat.id, c.from_user.id, q, a[0], c)


@route('wr')
def cb_refresh(c, a):
    q = query_of(a[1]) if len(a) > 1 else None
    if not q or not rate_ok(c.from_user.id, 'wr', 3):
        return ack(c, '⏳ একটু অপেক্ষা করুন' if q else 'পুরনো বাটন', not q)
    ack(c, '🔄 আপডেট হচ্ছে...')
    show(c.message.chat.id, c.from_user.id, q, a[0], c, force=True)


@route('wu')
def cb_unit(c, a):
    u = ucfg(c.from_user.id)
    u['unit'] = 'F' if u['unit'] == 'C' else 'C'
    DB.save()
    q = query_of(a[1]) if len(a) > 1 else None
    ack(c, '🌡 ইউনিট: °%s' % u['unit'])
    if q:
        show(c.message.chat.id, c.from_user.id, q, a[0], c)


@route('wf')
def cb_fav_toggle(c, a):
    u = ucfg(c.from_user.id)
    q = query_of(a[1]) if len(a) > 1 else None
    if not q:
        return ack(c, 'পুরনো বাটন', True)
    if q in u['fav']:
        u['fav'].remove(q)
        ack(c, '💔 Favorites থেকে সরানো হয়েছে')
    else:
        if len(u['fav']) >= MAX_FAV:
            return ack(c, 'সর্বোচ্চ %d টি Favorite রাখা যায়' % MAX_FAV, True)
        u['fav'].append(q)
        ack(c, '⭐ Favorites-এ যোগ হয়েছে')
    DB.save()
    show(c.message.chat.id, c.from_user.id, q, a[0], c)


@route('wfav')
def cb_favs(c, a):
    u = ucfg(c.from_user.id)
    ack(c)
    if not u['fav']:
        return edit(c, '⭐ <b>Favorites</b>\n\nএখনো কিছু সেভ করেননি।\nআবহাওয়া দেখে ⭐ Save চাপুন।', kb([btn('🔍 শহর খুঁজুন', 'wsearch')], nav()))
    rows = [[btn('🌤 %s' % q[:28], 'wq:%s' % qid_for(q)), btn('🗑', 'wd:%d' % i)] for i, q in enumerate(u['fav'])]
    rows.append(nav())
    edit(c, '⭐ <b>Favorites (%d)</b>\nচাপ দিলে আবহাওয়া দেখাবে।' % len(u['fav']), kb(*rows))


@route('wd')
def cb_fav_del(c, a):
    u = ucfg(c.from_user.id)
    i = to_int(a[0] if a else '', -1)
    if 0 <= i < len(u['fav']):
        u['fav'].pop(i)
        DB.save()
        ack(c, '🗑 মুছে ফেলা হয়েছে')
    cb_favs(c, [])


# ---------------------------------------------------------------- timezone
def tz_view(uid):
    u = ucfg(uid)
    rows = []
    for i in range(0, len(TZ_CHOICES), 3):
        rows.append([btn(('✅ ' if int(u['tz'] * 60) == m else '') + lbl, 'tz:s:%d' % m) for lbl, m in TZ_CHOICES[i:i + 3]])
    rows.append([btn('✏️ নিজে লিখুন', 'tz:c')])
    rows.append(nav())
    now_s = fmt_ts(time.time(), u['tz'], '%d %b, %H:%M')
    return ('🌍 <b>Timezone</b>\n' + LINE + '\nবর্তমান: <b>%s</b>\nআপনার এখনকার সময় (বট অনুযায়ী): <b>%s</b>\n\nDaily অ্যালার্ট আপনার এই সময় অনুযায়ী আসবে। ভুল হলে ঠিক করুন:' % (tz_label(u['tz']), now_s)), kb(*rows)


@route('tz')
def cb_tz(c, a):
    u = ucfg(c.from_user.id)
    if len(a) >= 2 and a[0] == 's':
        m = to_int(a[1])
        if m is not None and -720 <= m <= 840:
            u['tz'] = m / 60.0
            DB.save()
            ack(c, '✅ Timezone সেট হয়েছে')
    elif a and a[0] == 'c':
        ack(c)
        set_state(c.from_user.id, 'tz_custom')
        return edit(c, '✏️ UTC অফসেট লিখুন, যেমন <code>+6</code>, <code>+5:30</code>, <code>-4</code>', kb(nav('tz')))
    t, k = tz_view(c.from_user.id)
    edit(c, t, k)


# ---------------------------------------------------------------- daily alerts
def subs_view(uid):
    u = ucfg(uid)
    lines = ['🔔 <b>Daily Alerts</b>\n' + LINE]
    rows = []
    if not u['subs']:
        lines.append('এখনো কোনো অ্যালার্ট নেই।\nআবহাওয়া কার্ডের 🔔 Daily বাটন চাপুন অথবা লিখুন:\n<code>/subscribe Dhaka</code>')
    for i, s in enumerate(u['subs']):
        lines.append('• <b>%s</b> — প্রতিদিন %02d:00' % (esc(s['q']), s['h']))
        rows.append([btn('⏰ %s (%02d:00)' % (s['q'][:16], s['h']), 'sb:h:%d' % i), btn('🗑', 'sb:x:%d' % i)])
    rows.append([btn('🌧 Rain Alert: %s' % ('ON ✅' if u['rain'] else 'OFF ❌'), 'sb:r')])
    rows.append([btn('➕ নতুন শহর', 'sb:add')])
    rows.append(nav())
    lines.append('\n🌍 সময়: <b>%s</b> (বদলাতে Home → Timezone)' % tz_label(u['tz']))
    if u['rain']:
        lines.append('🌧 বৃষ্টির সম্ভাবনা ≥ 70% হলে আগাম মেসেজ আসবে।')
    return '\n'.join(lines), kb(*rows)


def hour_picker(prefix, back):
    rows, cur = [], []
    for h in range(4, 24):
        cur.append(btn('%02d:00' % h, '%s:%d' % (prefix, h)))
        if len(cur) == 5:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append(nav(back))
    return kb(*rows)


@route('sb')
def cb_subs(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    act = a[0] if a else ''
    if act == 'n' and len(a) > 1:      # new from weather card
        q = query_of(a[1])
        if not q:
            return ack(c, 'পুরনো বাটন', True)
        if any(s['q'] == q for s in u['subs']):
            return ack(c, 'এই শহরের অ্যালার্ট আগেই আছে', True)
        if len(u['subs']) >= MAX_SUB:
            return ack(c, 'সর্বোচ্চ %d টি শহর' % MAX_SUB, True)
        ack(c)
        return say(c.message.chat.id, '🔔 <b>%s</b>\nপ্রতিদিন কখন আবহাওয়া পেতে চান? (আপনার সময়: %s)' % (esc(q), tz_label(u['tz'])), hour_picker('sb:t:%s' % a[1], 'sb'))
    if act == 't' and len(a) > 2:      # choose hour for new
        q, h = query_of(a[1]), to_int(a[2])
        if not q or h is None or not 0 <= h <= 23:
            return ack(c, 'পুরনো বাটন', True)
        if not any(s['q'] == q for s in u['subs']) and len(u['subs']) < MAX_SUB:
            u['subs'].append({'q': q, 'h': h, 'last': ''})
            DB.save()
        ack(c, '✅ অ্যালার্ট সেট হয়েছে')
    elif act == 'h' and len(a) > 1:    # change hour
        i = to_int(a[1], -1)
        if not 0 <= i < len(u['subs']):
            return ack(c, 'পাওয়া যায়নি', True)
        ack(c)
        return edit(c, '⏰ <b>%s</b> — নতুন সময় বেছে নিন:' % esc(u['subs'][i]['q']), hour_picker('sb:ht:%d' % i, 'sb'))
    elif act == 'ht' and len(a) > 2:
        i, h = to_int(a[1], -1), to_int(a[2])
        if 0 <= i < len(u['subs']) and h is not None and 0 <= h <= 23:
            u['subs'][i]['h'] = h
            u['subs'][i]['last'] = ''
            DB.save()
            ack(c, '✅ সময় বদলানো হয়েছে')
    elif act == 'x' and len(a) > 1:
        i = to_int(a[1], -1)
        if 0 <= i < len(u['subs']):
            u['subs'].pop(i)
            DB.save()
            ack(c, '🗑 মুছে ফেলা হয়েছে')
    elif act == 'r':
        u['rain'] = not u['rain']
        DB.save()
        ack(c, '🌧 Rain Alert %s' % ('চালু' if u['rain'] else 'বন্ধ'))
    elif act == 'add':
        ack(c)
        if len(u['subs']) >= MAX_SUB:
            return ack(c, 'সর্বোচ্চ %d টি শহর' % MAX_SUB, True)
        set_state(uid, 'sub_city')
        return edit(c, '🔔 <b>যে শহরের Daily অ্যালার্ট চান তার নাম লিখুন</b> (ইংরেজিতে):', kb(nav('sb')))
    else:
        ack(c)
    t, k = subs_view(uid)
    edit(c, t, k)


def local_now(u):
    return local_dt(time.time(), u['tz'])


def alert_tick():
    with DB.lock:
        users = [(uid, u) for uid, u in DB.d['users'].items() if u.get('subs')]
    changed = False
    for uid, u in users:
        ln = local_now(u)
        today = ln.strftime('%Y-%m-%d')
        for s in u['subs']:
            due = s.get('last') != today and s['h'] <= ln.hour < s['h'] + 2
            if due:
                try:
                    data = fetch_weather(s['q'])
                except RuntimeError:
                    continue
                s['last'] = today
                changed = True
                if data:
                    qid = qid_for(s['q'])
                    say(int(uid), '☀️ <b>আজকের আবহাওয়া</b>\n\n' + render(data, 'now', u['unit']) + FOOTER,
                        kb([btn('🕒 ঘণ্টা', 'wv:hourly:%s' % qid), btn('📅 ৩ দিন', 'wv:3day:%s' % qid)], [btn('🌤 বিস্তারিত', 'wq:%s' % qid)]))
            if u.get('rain') and ln.hour >= 5:
                key = s['q']
                last = u['rain_sent'].get(key, 0)
                if time.time() - last < 8 * 3600:
                    continue
                try:
                    data = fetch_weather(s['q'])
                except RuntimeError:
                    continue
                if not data:
                    continue
                nxt = upcoming(data, 3)
                worst = max([h['rain'] for _, h in nxt] or [0])
                if worst >= 70:
                    u['rain_sent'][key] = time.time()
                    changed = True
                    say(int(uid), '🌧 <b>বৃষ্টির সতর্কতা — %s</b>\n\nআগামী কয়েক ঘণ্টায় বৃষ্টির সম্ভাবনা <b>%d%%</b>। ছাতা সাথে রাখুন ☂️' % (esc(data['name']), worst),
                        kb([btn('🕒 ঘণ্টাভিত্তিক দেখুন', 'wv:hourly:%s' % qid_for(s['q']))]))
    if changed:
        DB.save()


# ---------------------------------------------------------------- commands & messages
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    clear_state(m.from_user.id)
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()))


@bot.message_handler(commands=['weather', 'forecast'])
def weather_cmd(m):
    q = cmd_arg(m)[:60]
    if not q:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/weather Dhaka</code>', kb(nav()))
    show(m.chat.id, m.from_user.id, q, '3day' if m.text.startswith('/forecast') else 'now')


@bot.message_handler(commands=['subscribe'])
def subscribe_cmd(m):
    q = cmd_arg(m)[:60]
    u = ucfg(m.from_user.id)
    if not q:
        t, k = subs_view(m.from_user.id)
        return say(m.chat.id, t, k)
    if any(s['q'].lower() == q.lower() for s in u['subs']):
        return say(m.chat.id, 'ℹ️ এই শহরের অ্যালার্ট আগেই আছে।')
    if len(u['subs']) >= MAX_SUB:
        return say(m.chat.id, '⚠️ সর্বোচ্চ %d টি শহরের অ্যালার্ট রাখা যায়। আগে একটি মুছুন।' % MAX_SUB)
    u['subs'].append({'q': q, 'h': 8, 'last': ''})
    DB.save()
    qid = qid_for(q)
    say(m.chat.id, '✅ প্রতিদিন সকাল ৮টায় (%s) <b>%s</b>-এর আবহাওয়া পাবেন।\nসময় বদলাতে নিচের বাটন চাপুন।' % (tz_label(u['tz']), esc(q)),
        kb([btn('⏰ সময় বদলান', 'sb:h:%d' % (len(u['subs']) - 1))], nav()))


@bot.message_handler(commands=['unsubscribe'])
def unsubscribe_cmd(m):
    u = ucfg(m.from_user.id)
    if u['subs']:
        u['subs'] = []
        DB.save()
        say(m.chat.id, '✅ সব Daily অ্যালার্ট বন্ধ করা হয়েছে।')
    else:
        say(m.chat.id, 'আপনার কোনো অ্যালার্ট নেই।')


@bot.message_handler(content_types=['location'])
def on_location(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '📍 লোকেশন পেয়েছি, আবহাওয়া আনছি...', types.ReplyKeyboardRemove())
    show(m.chat.id, m.from_user.id, '%.4f,%.4f' % (m.location.latitude, m.location.longitude))


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = m.text.strip()[:60]
    if not rate_ok(uid, 'w', 0.8):
        return say(m.chat.id, '⏳ একটু ধীরে।')
    if s and s['k'] == 'tz_custom':
        off = parse_tz(txt)
        if off is None:
            return say(m.chat.id, '⚠️ সঠিক অফসেট দিন (যেমন +6 বা -4:30)।')
        clear_state(uid)
        ucfg(uid)['tz'] = off
        DB.save()
        t, k = tz_view(uid)
        return say(m.chat.id, '✅ Timezone সেট হয়েছে।\n\n' + t, k)
    if s and s['k'] == 'sub_city':
        u = ucfg(uid)
        clear_state(uid)
        if any(x['q'].lower() == txt.lower() for x in u['subs']):
            return say(m.chat.id, 'ℹ️ এই শহরের অ্যালার্ট আগেই আছে।')
        try:
            data = fetch_weather(txt)
        except RuntimeError:
            return say(m.chat.id, '⚠️ এখন যাচাই করা যাচ্ছে না, একটু পরে চেষ্টা করুন।')
        if data is None:
            return say(m.chat.id, '🔎 "%s" নামে কোনো জায়গা পাইনি। অন্য নাম লিখুন।' % esc(txt))
        return say(m.chat.id, '🔔 <b>%s</b>\nপ্রতিদিন কখন আবহাওয়া পেতে চান? (আপনার সময়: %s)' % (esc(data['name']), tz_label(u['tz'])), hour_picker('sb:t:%s' % qid_for(txt), 'sb'))
    clear_state(uid)
    show(m.chat.id, uid, txt)


start_worker(alert_tick, 30)
run(commands=[('start', 'মেনু খুলুন'), ('weather', 'শহরের আবহাওয়া'), ('forecast', '৩ দিনের পূর্বাভাস'), ('subscribe', 'Daily অ্যালার্ট'),
              ('unsubscribe', 'অ্যালার্ট বন্ধ'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "reminder_bot": {
        "emoji": '⏰',
        "name": 'Smart Reminder Bot',
        "proj_name": 'Reminder Bot',
        "category": '⏰ Productivity',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~30 sec',
        "features": ['➕ বাটনে ধাপে ধাপে রিমাইন্ডার — টেক্সট → ধরন → সময় → রিপিট → কনফার্ম', 'সময় দেওয়া যায় 30m / 2h30m / 21:30 / কাল 09:00 / 25-12 18:30 ফরম্যাটে (বাংলা সংখ্যাও চলে)', '🔁 রিপিট: একবার, প্রতিদিন, কর্মদিবস, সাপ্তাহিক, মাসিক', '😴 Snooze (10m/1h/কাল) ও ✅ Done বাটন রিমাইন্ডারের মেসেজেই', '✏️ লিস্ট থেকে টেক্সট/সময়/রিপিট এডিট, ⏸ Pause, 🗑 ডিলিট', '📜 History ও 🌍 Timezone সেটিং'],
        "commands": '/remind 30m টেক্সট [daily|weekly|monthly|weekdays] · /myreminders',
        "about": 'পূর্ণাঙ্গ বাটন-চালিত রিমাইন্ডার — ধরন/আইকন, ৫ রকম রিপিট, Snooze, এডিট/পজ, History ও Timezone সহ। বট রিস্টার্ট হলেও রিমাইন্ডার হারায় না।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', '/start → ➕ নতুন Reminder চাপুন', 'কী মনে করাব লিখুন, ধরন ও সময় বেছে নিন, রিপিট ঠিক করে ✅ সেভ করুন', '📋 My Reminders থেকে এডিট/পজ/ডিলিট', 'দ্রুত: /remind 30m চা খেতে হবে'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 /remind 1d ওষুধ খেতে হবে daily\n🤖 ✅ প্রতিদিন এই সময়ে মনে করিয়ে দেব!\n[পরদিন] 🤖 ⏰ রিমাইন্ডার! ওষুধ খেতে হবে',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)

import calendar

DB = Store('reminders.json', {'users': {}, 'items': [], 'history': [], 'next': 1})
TAGS = [('📌', 'General'), ('💼', 'Work'), ('💊', 'Health'), ('🎂', 'Birthday'), ('🛒', 'Shopping'), ('📚', 'Study')]
REPEATS = [('once', '1️⃣ একবার'), ('daily', '📆 প্রতিদিন'), ('weekdays', '💼 সপ্তাহের কর্মদিবস'), ('weekly', '🗓 প্রতি সপ্তাহে'), ('monthly', '📅 প্রতি মাসে')]
REPEAT_NAME = dict(REPEATS)
TZ_CHOICES = [('🇧🇩 +6', 360), ('🇮🇳 +5:30', 330), ('🇵🇰 +5', 300), ('🇦🇪 +4', 240), ('🇸🇦 +3', 180), ('🇬🇧 +0', 0),
              ('🇪🇺 +1', 60), ('🇺🇸 -5', -300), ('🇺🇸 -8', -480), ('🇲🇾 +8', 480), ('🇯🇵 +9', 540), ('🇦🇺 +10', 600)]
MAX_ACTIVE = 100
MAX_AHEAD = 366 * 86400
SNOOZES = [(600, '😴 10m'), (3600, '😴 1h'), (86400, '🗓 কাল')]


def ucfg(uid):
    u = DB.d['users'].setdefault(str(uid), {})
    u.setdefault('tz', 6.0)
    return u


def utz(uid):
    return ucfg(uid)['tz']


def get_item(rid):
    for i in DB.d['items']:
        if i['id'] == rid:
            return i
    return None


def mine(uid):
    return sorted([i for i in DB.d['items'] if i['uid'] == uid], key=lambda x: x['ts'])


def tag_icon(tag):
    return dict((n, e) for e, n in TAGS).get(tag, '📌')


def next_ts(ts, rule, off):
    d = local_dt(ts, off)
    if rule == 'daily':
        d += datetime.timedelta(days=1)
    elif rule == 'weekly':
        d += datetime.timedelta(days=7)
    elif rule == 'weekdays':
        d += datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
    elif rule == 'monthly':
        month = d.month % 12 + 1
        year = d.year + (1 if d.month == 12 else 0)
        day = min(d.day, calendar.monthrange(year, month)[1])
        d = d.replace(year=year, month=month, day=day)
    else:
        return None
    return local_to_ts(d, off)


def parse_when(text, off):
    t = norm_digits(text).strip().lower()
    secs = parse_duration(t)
    if secs:
        return time.time() + secs
    now_l = local_dt(time.time(), off)
    mt = re.fullmatch(r'(?:today\s+|আজ\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?', t)
    if mt:
        h, mi = int(mt.group(1)), int(mt.group(2))
        if mt.group(3) == 'pm' and h < 12:
            h += 12
        if mt.group(3) == 'am' and h == 12:
            h = 0
        if h > 23 or mi > 59:
            return None
        cand = now_l.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand <= now_l:
            cand += datetime.timedelta(days=1)
        return local_to_ts(cand, off)
    mt = re.fullmatch(r'(?:tomorrow\s+|কাল\s+)(\d{1,2}):(\d{2})\s*(am|pm)?', t)
    if mt:
        h, mi = int(mt.group(1)), int(mt.group(2))
        if mt.group(3) == 'pm' and h < 12:
            h += 12
        if h > 23 or mi > 59:
            return None
        cand = (now_l + datetime.timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return local_to_ts(cand, off)
    for fmt in ('%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M', '%d/%m %H:%M', '%d-%m %H:%M'):
        try:
            d = datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
        if '%Y' not in fmt:
            d = d.replace(year=now_l.year)
            if d <= now_l:
                d = d.replace(year=now_l.year + 1)
        return local_to_ts(d, off)
    return None


def when_str(ts, off):
    return fmt_ts(ts, off, '%a %d %b, %H:%M')


def item_line(i, off):
    rep = '' if i['rule'] == 'once' else ' 🔁'
    pause = ' ⏸' if i.get('paused') else ''
    return '%s %s — %s%s%s' % (tag_icon(i.get('tag')), esc(i['text'][:34]), when_str(i['ts'], off), rep, pause)


# ---------------------------------------------------------------- home
def home_view(uid):
    off = utz(uid)
    n = len([i for i in mine(uid) if not i.get('paused')])
    text = ('⏰ <b>Smart Reminder</b>\n' + LINE + '\n'
            'সময়মতো মনে করিয়ে দেব — ওষুধ, মিটিং, জন্মদিন, বিল 🔔\n\n'
            '📋 সক্রিয় রিমাইন্ডার: <b>%d</b>\n🌍 আপনার সময়: <b>%s</b> (%s)' % (n, fmt_ts(time.time(), off, '%d %b, %H:%M'), tz_label(off)) + FOOTER)
    return text, kb([btn('➕ নতুন Reminder', 'rn')], [btn('📋 My Reminders', 'rl'), btn('📜 History', 'rh')],
                    [btn('🌍 Timezone', 'tz'), btn('❓ Help', 'rhelp')])


HELP = ('❓ <b>Smart Reminder — সাহায্য</b>\n' + LINE + '\n'
        '➕ <b>নতুন Reminder:</b> লেখা → সময় → রিপিট → কনফার্ম — সব বাটনে\n'
        '⏱ সময়ের ফরম্যাট: <code>30m</code>, <code>2h</code>, <code>1d</code>, <code>1h30m</code>, <code>21:30</code>, <code>কাল 09:00</code>, <code>25-12 18:30</code>, <code>2026-12-25 18:30</code>\n'
        '🔁 রিপিট: প্রতিদিন / কর্মদিবস / সাপ্তাহিক / মাসিক\n'
        '😴 রিমাইন্ডার এলে Snooze বা Done চাপতে পারবেন\n'
        '✏️ লিস্ট থেকে যেকোনো রিমাইন্ডার এডিট, পজ বা ডিলিট করা যায়\n\n'
        '⌨️ দ্রুত: <code>/remind 30m চা খেতে হবে</code> (শেষে daily/weekly লিখলে রিপিট) · <code>/myreminders</code>')


@route('rhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


# ---------------------------------------------------------------- creation wizard
def when_kb(back):
    return kb([btn('⏱ 10 মিনিট', 'rw:600'), btn('⏱ 30 মিনিট', 'rw:1800'), btn('⏱ 1 ঘণ্টা', 'rw:3600')],
              [btn('⏱ 3 ঘণ্টা', 'rw:10800'), btn('🌙 আজ রাত 9টা', 'rw:t21'), btn('🌅 কাল সকাল 9টা', 'rw:n9')],
              [btn('⌨️ নিজে লিখুন (সময়/তারিখ)', 'rw:c')], nav(back))


@route('rn')
def cb_new(c, a):
    uid = c.from_user.id
    if len([i for i in DB.d['items'] if i['uid'] == uid]) >= MAX_ACTIVE:
        return ack(c, 'সর্বোচ্চ %d টি রিমাইন্ডার রাখা যায়। কিছু মুছুন।' % MAX_ACTIVE, True)
    ack(c)
    set_state(uid, 'new_text', edit_id=None)
    edit(c, '➕ <b>নতুন Reminder — ধাপ ১/৩</b>\n\n📝 কী মনে করিয়ে দেব? লিখে পাঠান:', kb(nav()))


def ask_tag(chat_id, d):
    rows = [[btn('%s %s' % (e, n), 'rt:%s' % n) for e, n in TAGS[:3]], [btn('%s %s' % (e, n), 'rt:%s' % n) for e, n in TAGS[3:]]]
    say(chat_id, '🏷 <b>ধরন বেছে নিন</b> (আইকনের জন্য):', kb(*rows))


@route('rt')
def cb_tag(c, a):
    s = get_state(c.from_user.id)
    if not s or s['k'] != 'new_tag':
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    ack(c)
    s['d']['tag'] = a[0] if a else 'General'
    s['k'] = 'new_when'
    edit(c, '➕ <b>ধাপ ২/৩</b>\n\n📝 <i>%s</i>\n\n⏰ কখন মনে করাব?' % esc(s['d']['text'][:80]), when_kb(None))


def after_when(chat_id, uid, ts, c=None):
    s = get_state(uid)
    if not s:
        return
    if ts < time.time() + 4:
        return say(chat_id, '⚠️ সময় অতীতে বা খুব কাছে। ভবিষ্যতের সময় দিন।')
    if ts > time.time() + MAX_AHEAD:
        return say(chat_id, '⚠️ সর্বোচ্চ ১ বছর পর্যন্ত সময় দেওয়া যায়।')
    s['d']['ts'] = ts
    s['k'] = 'new_rep'
    rows = [[btn(lbl, 'rr:%s' % k)] for k, lbl in REPEATS]
    txt = '➕ <b>ধাপ ৩/৩</b>\n\n⏰ %s\n\n🔁 কত বার রিপিট হবে?' % when_str(ts, utz(uid))
    if c is not None:
        edit(c, txt, kb(*rows, nav()))
    else:
        say(chat_id, txt, kb(*rows, nav()))


@route('rw')
def cb_when(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    if not s or s['k'] not in ('new_when', 'resched'):
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    ack(c)
    v = a[0] if a else ''
    off = utz(uid)
    if v == 'c':
        s['d']['custom'] = True
        return edit(c, '⌨️ <b>সময় লিখুন</b>\n\n• <code>45m</code>, <code>2h30m</code>, <code>1d</code>\n• <code>21:30</code> (আজ/কাল যেটা আগে)\n• <code>কাল 09:00</code>\n• <code>25-12 18:30</code> বা <code>2026-12-25 18:30</code>\n\n🌍 আপনার সময়: %s' % tz_label(off), kb(nav()))
    now_l = local_dt(time.time(), off)
    if v == 't21':
        cand = now_l.replace(hour=21, minute=0, second=0, microsecond=0)
        if cand <= now_l:
            cand += datetime.timedelta(days=1)
        ts = local_to_ts(cand, off)
    elif v == 'n9':
        ts = local_to_ts((now_l + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0), off)
    else:
        secs = to_int(v)
        if not secs:
            return
        ts = time.time() + secs
    if s['k'] == 'resched':
        return apply_resched(c.message.chat.id, uid, ts, c)
    after_when(c.message.chat.id, uid, ts, c)


@route('rr')
def cb_repeat(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    if not s or s['k'] not in ('new_rep', 'edit_rep') or not a or a[0] not in REPEAT_NAME:
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    ack(c)
    d = s['d']
    d['rule'] = a[0]
    if s['k'] == 'edit_rep':
        it = get_item(d['id'])
        if it and it['uid'] == uid:
            it['rule'] = a[0]
            DB.save()
        clear_state(uid)
        return show_item(c, uid, d['id'])
    d['paused'] = False
    off = utz(uid)
    txt = ('✅ <b>কনফার্ম করুন</b>\n' + LINE + '\n%s <b>%s</b>\n⏰ %s\n🔁 %s\n🌍 %s' %
           (tag_icon(d.get('tag')), esc(d['text'][:200]), when_str(d['ts'], off), REPEAT_NAME[a[0]], tz_label(off)))
    s['k'] = 'new_confirm'
    edit(c, txt, kb([btn('✅ সেভ করুন', 'rs')], [btn('✖ বাতিল', 'home')]))


@route('rs')
def cb_save(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    if not s or s['k'] != 'new_confirm':
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    d = s['d']
    with DB.lock:
        rid = DB.d['next']
        DB.d['next'] += 1
        DB.d['items'].append({'id': rid, 'uid': uid, 'chat': c.message.chat.id, 'text': d['text'], 'tag': d.get('tag', 'General'),
                              'ts': d['ts'], 'rule': d['rule'], 'paused': False, 'n': 0})
        DB.save()
    clear_state(uid)
    ack(c, '✅ সেভ হয়েছে')
    edit(c, '✅ <b>রিমাইন্ডার সেট হয়েছে!</b>\n\n%s %s\n⏰ %s\n🆔 #%d' % (tag_icon(d.get('tag')), esc(d['text'][:200]), when_str(d['ts'], utz(uid)), rid),
         kb([btn('➕ আরেকটি', 'rn'), btn('📋 My Reminders', 'rl')], nav()))


# ---------------------------------------------------------------- list / detail / edit
def list_view(uid, page=0):
    off = utz(uid)
    items = mine(uid)
    if not items:
        return '📋 <b>My Reminders</b>\n\nকোনো সক্রিয় রিমাইন্ডার নেই।', kb([btn('➕ নতুন Reminder', 'rn')], nav())
    chunk, page, pages = paginate(items, page, 6)
    rows = [[btn('%s %s' % (tag_icon(i.get('tag')), (i['text'][:22] + ' • ' + fmt_ts(i['ts'], off, '%d %b %H:%M'))), 'rv:%d:%d' % (i['id'], page))] for i in chunk]
    pg = pager('rl', page, pages)
    if pg:
        rows.append(pg)
    rows.append([btn('➕ নতুন', 'rn'), btn('🏠 Home', 'home')])
    return '📋 <b>My Reminders (%d)</b>\n<i>⏸ = পজ করা • 🔁 = রিপিট</i>\nচাপ দিলে এডিট/ডিলিট করতে পারবেন।' % len(items), kb(*rows)


@route('rl')
def cb_list(c, a):
    ack(c)
    clear_state(c.from_user.id)
    t, k = list_view(c.from_user.id, to_int(a[0], 0) if a else 0)
    edit(c, t, k)


def show_item(c, uid, rid, page=0):
    it = get_item(rid)
    if not it or it['uid'] != uid:
        return edit(c, '⚠️ রিমাইন্ডারটি পাওয়া যায়নি।', kb([btn('📋 My Reminders', 'rl')], nav()))
    off = utz(uid)
    txt = ('%s <b>Reminder #%d</b>\n' % (tag_icon(it.get('tag')), it['id']) + LINE + '\n📝 %s\n⏰ %s\n🔁 %s\n🌍 %s%s' %
           (esc(it['text']), when_str(it['ts'], off), REPEAT_NAME.get(it['rule'], it['rule']), tz_label(off), '\n⏸ <b>পজ করা আছে</b>' if it.get('paused') else ''))
    rows = [[btn('✏️ টেক্সট', 're:t:%d' % rid), btn('🕒 সময়', 're:w:%d' % rid)],
            [btn('🔁 রিপিট', 're:r:%d' % rid), btn('▶️ Resume' if it.get('paused') else '⏸ Pause', 're:p:%d' % rid)],
            [btn('🗑 ডিলিট', 're:d:%d' % rid)], nav('rl:%d' % page)]
    edit(c, txt, kb(*rows))


@route('rv')
def cb_view(c, a):
    ack(c)
    clear_state(c.from_user.id)
    show_item(c, c.from_user.id, to_int(a[0] if a else '', -1), to_int(a[1], 0) if len(a) > 1 else 0)


def apply_resched(chat_id, uid, ts, c=None):
    s = get_state(uid)
    if not s:
        return
    if ts < time.time() + 4 or ts > time.time() + MAX_AHEAD:
        return say(chat_id, '⚠️ সময় ভবিষ্যতের এবং ১ বছরের মধ্যে হতে হবে।')
    it = get_item(s['d']['id'])
    clear_state(uid)
    if not it or it['uid'] != uid:
        return say(chat_id, '⚠️ রিমাইন্ডারটি পাওয়া যায়নি।')
    it['ts'] = ts
    it['paused'] = False
    DB.save()
    if c is not None:
        show_item(c, uid, it['id'])
    else:
        say(chat_id, '✅ সময় বদলানো হয়েছে: <b>%s</b>' % when_str(ts, utz(uid)), kb([btn('📋 My Reminders', 'rl')], nav()))


@route('re')
def cb_edit(c, a):
    uid = c.from_user.id
    if len(a) < 2:
        return ack(c)
    act, rid = a[0], to_int(a[1], -1)
    it = get_item(rid)
    if not it or it['uid'] != uid:
        return ack(c, 'রিমাইন্ডারটি পাওয়া যায়নি', True)
    ack(c)
    if act == 't':
        set_state(uid, 'edit_text', id=rid)
        return edit(c, '✏️ <b>নতুন টেক্সট লিখুন:</b>\n\nবর্তমান: <i>%s</i>' % esc(it['text'][:200]), kb(nav('rv:%d:0' % rid)))
    if act == 'w':
        set_state(uid, 'resched', id=rid)
        return edit(c, '🕒 <b>নতুন সময় বেছে নিন:</b>', when_kb('rv:%d:0' % rid))
    if act == 'r':
        set_state(uid, 'edit_rep', id=rid)
        return edit(c, '🔁 <b>রিপিট বেছে নিন:</b>', kb(*[[btn(l, 'rr:%s' % k)] for k, l in REPEATS], nav('rv:%d:0' % rid)))
    if act == 'p':
        it['paused'] = not it.get('paused')
        if not it['paused'] and it['ts'] < time.time():
            nt = next_ts(it['ts'], it['rule'], utz(uid))
            while nt and nt < time.time():
                nt = next_ts(nt, it['rule'], utz(uid))
            it['ts'] = nt or time.time() + 60
        DB.save()
        return show_item(c, uid, rid)
    if act == 'd':
        return edit(c, '🗑 <b>রিমাইন্ডার #%d ডিলিট করবেন?</b>\n\n%s' % (rid, esc(it['text'][:100])), kb([btn('✅ হ্যাঁ, ডিলিট', 're:y:%d' % rid), btn('✖ না', 'rv:%d:0' % rid)]))
    if act == 'y':
        DB.d['items'] = [i for i in DB.d['items'] if i['id'] != rid]
        DB.save()
        ack(c, '🗑 ডিলিট হয়েছে')
        t, k = list_view(uid)
        return edit(c, t, k)


@route('rh')
def cb_history(c, a):
    ack(c)
    off = utz(c.from_user.id)
    hist = [h for h in DB.d['history'] if h['uid'] == c.from_user.id][-10:][::-1]
    if not hist:
        return edit(c, '📜 <b>History</b>\n\nএখনো কোনো রিমাইন্ডার সম্পন্ন হয়নি।', kb(nav()))
    lines = ['📜 <b>শেষ %d টি রিমাইন্ডার</b>\n' % len(hist)]
    for h in hist:
        lines.append('%s %s %s — %s' % ('✅' if h.get('done') else '🔔', tag_icon(h.get('tag')), esc(h['text'][:36]), fmt_ts(h['ts'], off, '%d %b %H:%M')))
    edit(c, '\n'.join(lines), kb([btn('🗑 History মুছুন', 'rhx')], nav()))


@route('rhx')
def cb_history_clear(c, a):
    DB.d['history'] = [h for h in DB.d['history'] if h['uid'] != c.from_user.id]
    DB.save()
    ack(c, '🗑 মুছে ফেলা হয়েছে')
    edit(c, '📜 <b>History</b>\n\nপরিষ্কার করা হয়েছে।', kb(nav()))


# ---------------------------------------------------------------- when reminder fires
@route('rd')
def cb_done(c, a):
    rid = to_int(a[0] if a else '', -1)
    for h in DB.d['history']:
        if h['id'] == rid and h['uid'] == c.from_user.id:
            h['done'] = True
    DB.save()
    ack(c, '✅ সম্পন্ন!')
    edit(c, '✅ <b>সম্পন্ন</b>\n\n' + (c.message.text or '').split('\n', 2)[-1][:300], None)


@route('rz')
def cb_snooze(c, a):
    uid = c.from_user.id
    rid, secs = to_int(a[0] if a else '', -1), to_int(a[1] if len(a) > 1 else '', 0)
    src = None
    for h in DB.d['history']:
        if h['id'] == rid and h['uid'] == uid:
            src = h
    it = get_item(rid)
    if secs not in [s for s, _ in SNOOZES] or (not src and not it):
        return ack(c, 'এখন Snooze করা যাচ্ছে না', True)
    base = it or src
    ts = time.time() + secs
    if secs == 86400:
        ts = time.time() + 86400
    with DB.lock:
        if it and it['rule'] == 'once':
            it['ts'] = ts
            it['paused'] = False
        else:
            nid = DB.d['next']
            DB.d['next'] += 1
            DB.d['items'].append({'id': nid, 'uid': uid, 'chat': base['chat'], 'text': base['text'], 'tag': base.get('tag', 'General'), 'ts': ts, 'rule': 'once', 'paused': False, 'n': 0})
        DB.save()
    ack(c, '😴 Snooze করা হয়েছে')
    edit(c, '😴 <b>Snoozed</b> — %s-এ আবার মনে করাব।\n\n%s' % (when_str(ts, utz(uid)), esc(base['text'][:200])), None)


def fire(it):
    kbm = kb([btn('✅ Done', 'rd:%d' % it['id'])], [btn(lbl, 'rz:%d:%d' % (it['id'], s)) for s, lbl in SNOOZES])
    r = say(it['chat'], '⏰ <b>রিমাইন্ডার!</b>\n\n%s %s' % (tag_icon(it.get('tag')), esc(it['text'])), kbm)
    return r is not None


def reminder_tick():
    now_t = time.time()
    with DB.lock:
        due = [i for i in DB.d['items'] if i['ts'] <= now_t and not i.get('paused')]
    if not due:
        return
    for it in due:
        ok = fire(it)
        with DB.lock:
            if not ok:
                it['n'] = it.get('n', 0) + 1
                it['ts'] = now_t + 120 * it['n']
                if it['n'] >= 8:
                    DB.d['items'] = [x for x in DB.d['items'] if x['id'] != it['id']]
                continue
            DB.d['history'].append({'id': it['id'], 'uid': it['uid'], 'chat': it['chat'], 'text': it['text'], 'tag': it.get('tag'), 'ts': it['ts'], 'done': False})
            del DB.d['history'][:-300]
            nt = next_ts(it['ts'], it['rule'], utz(it['uid']))
            while nt is not None and nt <= now_t:
                nt = next_ts(nt, it['rule'], utz(it['uid']))
            if nt is None:
                DB.d['items'] = [x for x in DB.d['items'] if x['id'] != it['id']]
            else:
                it['ts'] = nt
                it['n'] = 0
    DB.save()


# ---------------------------------------------------------------- timezone
def tz_view(uid):
    off = utz(uid)
    rows = []
    for i in range(0, len(TZ_CHOICES), 3):
        rows.append([btn(('✅ ' if int(off * 60) == m else '') + lbl, 'tz:s:%d' % m) for lbl, m in TZ_CHOICES[i:i + 3]])
    rows.append([btn('✏️ নিজে লিখুন', 'tz:c')])
    rows.append(nav())
    return ('🌍 <b>Timezone</b>\n' + LINE + '\nবর্তমান: <b>%s</b>\nআপনার এখনকার সময় (বট অনুযায়ী): <b>%s</b>\n\nসময়/তারিখ দিয়ে রিমাইন্ডার সেট করতে সঠিক Timezone জরুরি।' % (tz_label(off), fmt_ts(time.time(), off, '%d %b, %H:%M'))), kb(*rows)


@route('tz')
def cb_tz(c, a):
    uid = c.from_user.id
    if len(a) >= 2 and a[0] == 's':
        m = to_int(a[1])
        if m is not None and -720 <= m <= 840:
            old = utz(uid)
            ucfg(uid)['tz'] = m / 60.0
            DB.save()
            ack(c, '✅ Timezone সেট হয়েছে')
    elif a and a[0] == 'c':
        ack(c)
        set_state(uid, 'tz_custom')
        return edit(c, '✏️ UTC অফসেট লিখুন, যেমন <code>+6</code>, <code>+5:30</code>, <code>-4</code>', kb(nav('tz')))
    t, k = tz_view(uid)
    edit(c, t, k)


# ---------------------------------------------------------------- commands & text
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    clear_state(m.from_user.id)
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()))


@bot.message_handler(commands=['remind'])
def remind_cmd(m):
    uid = m.from_user.id
    parts = cmd_arg(m).split(None, 1)
    if len(parts) < 2:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/remind 30m চা খেতে হবে</code>\nঅথবা বাটনে সেট করুন 👇', kb([btn('➕ নতুন Reminder', 'rn')], nav()))
    secs = parse_duration(parts[0])
    ts = time.time() + secs if secs else parse_when(parts[0], utz(uid))
    if not ts or ts < time.time() + 4 or ts > time.time() + MAX_AHEAD:
        return say(m.chat.id, '⚠️ সময় বুঝতে পারিনি। যেমন: <code>30m</code>, <code>2h</code>, <code>21:30</code>')
    body, rule = parts[1], 'once'
    for tag in ('daily', 'weekly', 'monthly', 'weekdays'):
        if body.lower().endswith(' ' + tag) or body.lower() == tag:
            rule, body = tag, body[:-len(tag)].strip() or 'রিমাইন্ডার'
            break
    if len([i for i in DB.d['items'] if i['uid'] == uid]) >= MAX_ACTIVE:
        return say(m.chat.id, '⚠️ সর্বোচ্চ %d টি রিমাইন্ডার রাখা যায়।' % MAX_ACTIVE)
    with DB.lock:
        rid = DB.d['next']
        DB.d['next'] += 1
        DB.d['items'].append({'id': rid, 'uid': uid, 'chat': m.chat.id, 'text': body[:500], 'tag': 'General', 'ts': ts, 'rule': rule, 'paused': False, 'n': 0})
        DB.save()
    say(m.chat.id, '✅ ঠিক আছে! <b>%s</b>-এ মনে করিয়ে দেব%s।\n🆔 #%d' % (when_str(ts, utz(uid)), '' if rule == 'once' else ' (%s)' % REPEAT_NAME[rule], rid), kb([btn('📋 My Reminders', 'rl')], nav()))


@bot.message_handler(commands=['myreminders'])
def my_cmd(m):
    t, k = list_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = m.text.strip()
    if not s:
        t, k = home_view(uid)
        return say(m.chat.id, '💡 নতুন রিমাইন্ডার বানাতে ➕ বাটন চাপুন।\n\n' + t, k)
    key, d = s['k'], s['d']
    if key == 'new_text':
        if len(txt) > 500:
            return say(m.chat.id, '⚠️ টেক্সট সর্বোচ্চ ৫০০ অক্ষরের হতে পারে।')
        d['text'] = txt
        s['k'] = 'new_tag'
        return ask_tag(m.chat.id, d)
    if key == 'edit_text':
        it = get_item(d['id'])
        clear_state(uid)
        if not it or it['uid'] != uid:
            return say(m.chat.id, '⚠️ রিমাইন্ডারটি পাওয়া যায়নি।')
        it['text'] = txt[:500]
        DB.save()
        return say(m.chat.id, '✅ টেক্সট বদলানো হয়েছে।', kb([btn('📋 My Reminders', 'rl')], nav()))
    if key in ('new_when', 'resched') and d.get('custom'):
        ts = parse_when(txt, utz(uid))
        if not ts:
            return say(m.chat.id, '⚠️ সময় বুঝতে পারিনি। যেমন লিখুন: <code>45m</code>, <code>21:30</code>, <code>কাল 09:00</code>, <code>25-12 18:30</code>')
        if key == 'resched':
            return apply_resched(m.chat.id, uid, ts)
        return after_when(m.chat.id, uid, ts)
    if key == 'tz_custom':
        off = parse_tz(txt)
        if off is None:
            return say(m.chat.id, '⚠️ সঠিক অফসেট দিন (যেমন +6 বা -4:30)।')
        clear_state(uid)
        ucfg(uid)['tz'] = off
        DB.save()
        t, k = tz_view(uid)
        return say(m.chat.id, '✅ Timezone সেট হয়েছে।\n\n' + t, k)
    say(m.chat.id, 'ℹ️ উপরের বাটন থেকে বেছে নিন, অথবা /cancel দিয়ে বাতিল করুন।')


start_worker(reminder_tick, 3)
run(commands=[('start', 'মেনু খুলুন'), ('remind', 'দ্রুত রিমাইন্ডার'), ('myreminders', 'আমার রিমাইন্ডার'),
              ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "currency_converter": {
        "emoji": '💱',
        "name": 'Currency Converter Bot',
        "proj_name": 'Currency Converter',
        "category": '💱 Finance',
        "level": '⭐⭐ Intermediate',
        "setup": '~20 sec',
        "features": ['১৫০+ কারেন্সি লাইভ রেটে — বাটনে ধাপে ধাপে অথবা "100 USD to BDT", "$50 in taka" লিখে', 'এক ট্যাপে ১/১০/১০০/১,০০০ অ্যামাউন্ট ও 🔁 Reverse', '📈 Rates — যেকোনো বেসের লিস্ট + ১৫০+ কারেন্সির পূর্ণ তালিকা (পেজ করা)', '⭐ Favorites — পছন্দের পেয়ার, 🔺/🔻 পরিবর্তন সহ', '🔔 Rate Alerts — টার্গেট রেটে পৌঁছালে মেসেজ', '🧮 Multi-Convert — একসাথে অনেক কারেন্সিতে', 'API বন্ধ থাকলে ৬ ঘণ্টা পর্যন্ত ক্যাশ রেট'],
        "commands": '/convert 100 USD BDT · /rates [কারেন্সি] · /fav USD BDT · /alerts',
        "about": 'বাটন-চালিত কারেন্সি কনভার্টার — লাইভ রেট, ফেভারিট পেয়ার, রেট অ্যালার্ট ও মাল্টি-কনভার্ট সহ। বাংলা (ডলার/টাকা) ইনপুটও বোঝে।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', '/start → 💱 Convert চাপুন অথবা সরাসরি লিখুন: 100 USD to BDT', 'ফলাফলের নিচে ⭐ Save / 🔔 Alert / 📈 Rates বাটন আছে', '🔔 Alerts থেকে "১ USD ১২৫ BDT হলে জানাও" সেট করুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 /convert 100 USD BDT\n🤖 💱 100 USD = ১২,১৫০.০০ BDT',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('currency.json', {'users': {}, 'next': 1})
FLAGS = {'USD': '🇺🇸', 'EUR': '🇪🇺', 'GBP': '🇬🇧', 'BDT': '🇧🇩', 'INR': '🇮🇳', 'SAR': '🇸🇦', 'AED': '🇦🇪', 'JPY': '🇯🇵',
         'CNY': '🇨🇳', 'MYR': '🇲🇾', 'KWD': '🇰🇼', 'CAD': '🇨🇦', 'AUD': '🇦🇺', 'SGD': '🇸🇬', 'PKR': '🇵🇰', 'QAR': '🇶🇦',
         'OMR': '🇴🇲', 'BHD': '🇧🇭', 'TRY': '🇹🇷', 'KRW': '🇰🇷', 'CHF': '🇨🇭', 'RUB': '🇷🇺', 'THB': '🇹🇭', 'NPR': '🇳🇵',
         'LKR': '🇱🇰', 'IDR': '🇮🇩', 'EGP': '🇪🇬', 'ZAR': '🇿🇦', 'BRL': '🇧🇷', 'NZD': '🇳🇿', 'HKD': '🇭🇰', 'SEK': '🇸🇪'}
GRID = ['USD', 'EUR', 'GBP', 'BDT', 'INR', 'SAR', 'AED', 'JPY', 'CNY', 'MYR', 'KWD', 'CAD']
POPULAR = ['USD', 'EUR', 'GBP', 'INR', 'SAR', 'AED', 'JPY', 'CNY', 'MYR', 'KWD', 'CAD', 'AUD', 'SGD', 'PKR']
ALIASES = {'টাকা': 'BDT', 'ডলার': 'USD', 'ইউরো': 'EUR', 'পাউন্ড': 'GBP', 'রুপি': 'INR', 'রুপী': 'INR', 'রিয়াল': 'SAR',
           'দিরহাম': 'AED', 'ইয়েন': 'JPY', 'ইউয়ান': 'CNY', 'রিঙ্গিত': 'MYR', 'দিনার': 'KWD', 'taka': 'BDT', 'dollar': 'USD',
           'euro': 'EUR', 'pound': 'GBP', 'rupee': 'INR', 'riyal': 'SAR', 'dirham': 'AED', 'yen': 'JPY', 'yuan': 'CNY',
           '$': 'USD', '€': 'EUR', '£': 'GBP', '৳': 'BDT', '₹': 'INR', '¥': 'JPY'}
RC = TTL(600, 80)
STALE = {}
MAX_FAV, MAX_ALERT = 10, 5


def flag(c):
    return FLAGS.get(c, '🌐')


def ucfg(uid):
    u = DB.d['users'].setdefault(str(uid), {})
    u.setdefault('fav', [])
    u.setdefault('alerts', [])
    u.setdefault('snap', {})
    u.setdefault('base', 'USD')
    return u


def get_rates(base):
    base = base.upper()
    hit = RC.get(base)
    if hit:
        return hit
    try:
        st, j = http_json('https://open.er-api.com/v6/latest/' + base, timeout=12)
        if isinstance(j, dict) and j.get('result') == 'success' and isinstance(j.get('rates'), dict):
            val = {'rates': j['rates'], 'upd': j.get('time_last_update_utc', ''), 'stale': False}
            RC.set(base, val)
            STALE[base] = (time.time(), val)
            return val
        if isinstance(j, dict) and j.get('error-type') == 'unsupported-code':
            return None
    except Exception as e:
        log('rates', repr(e))
    old = STALE.get(base)
    if old and time.time() - old[0] < 6 * 3600:
        v = dict(old[1])
        v['stale'] = True
        return v
    raise RuntimeError('rates unavailable')


def fmt_amt(x):
    return '{:,.2f}'.format(x) if abs(x) >= 0.01 else '{:,.6f}'.format(x)


def fmt_rate(r):
    if r >= 100:
        return '{:,.2f}'.format(r)
    if r >= 1:
        return '{:,.4f}'.format(r)
    if r >= 0.01:
        return '{:,.5f}'.format(r)
    return '{:,.8f}'.format(r)


def short_time(upd):
    return upd.replace(' +0000', ' UTC')[:25] if upd else ''


def resolve_cur(tok):
    t = tok.strip()
    if t in ALIASES:
        return ALIASES[t]
    if t.lower() in ALIASES:
        return ALIASES[t.lower()]
    if re.fullmatch(r'[A-Za-z]{3}', t):
        return t.upper()
    return None


def parse_convert(text):
    t = norm_digits(text).strip()
    t = t.replace('->', ' ').replace('→', ' ').replace('=>', ' ').replace('=', ' ')
    t = re.sub(r'(?i)\b(to|in|into)\b', ' ', t)
    t = re.sub(r'^([$€£৳₹¥])\s*', r'\1 ', t)
    t = re.sub(r'(\d)([^\d\s.,])', r'\1 \2', t)
    toks = t.split()
    amount, curs = None, []
    for tok in toks:
        v = to_float(tok)
        if v is not None and amount is None:
            amount = v
            continue
        c = resolve_cur(tok)
        if c:
            curs.append(c)
        else:
            return None
    if len(curs) == 2 and amount is None:
        amount = 1.0
    if amount is None or amount <= 0 or amount > 1e12 or not curs or len(curs) > 2:
        return None
    if len(curs) == 1:
        curs.append('USD' if curs[0] == 'BDT' else 'BDT')
    return amount, curs[0], curs[1]


def result_view(amount, frm, to):
    d = get_rates(frm)
    if d is None or to not in d['rates']:
        return None, None
    rate = d['rates'][to]
    text = ('💱 <b>%s %s %s</b>\n= <b>%s %s %s</b>\n' % (flag(frm), fmt_amt(amount), frm, flag(to), fmt_amt(amount * rate), to) + LINE +
            '\n📈 1 %s = %s %s\n📉 1 %s = %s %s\n🕒 %s' % (frm, fmt_rate(rate), to, to, fmt_rate(1 / rate), frm, short_time(d['upd'])) +
            ('\n⚠️ <i>লাইভ সার্ভার পাওয়া যায়নি — পুরনো ক্যাশ রেট।</i>' if d.get('stale') else '') + FOOTER)
    amts = [1, 10, 100, 1000, 10000]
    rows = [[btn(('✅ ' if abs(a - amount) < 1e-9 else '') + '{:,}'.format(a), 'cr:%g:%s:%s' % (a, frm, to)) for a in amts[:3]],
            [btn(('✅ ' if abs(a - amount) < 1e-9 else '') + '{:,}'.format(a), 'cr:%g:%s:%s' % (a, frm, to)) for a in amts[3:]] +
            [btn('✏️ অন্য Amount', 'cv:q:%s:%s' % (frm, to))],
            [btn('🔁 Reverse', 'cr:%g:%s:%s' % (amount, to, frm)), btn('⭐ Save', 'fv:a:%s:%s' % (frm, to)), btn('🔔 Alert', 'al:p:%s:%s' % (frm, to))],
            [btn('📈 %s Rates' % frm, 'rt:' + frm), btn('🏠 Home', 'home')]]
    return text, kb(*rows)


def home_view(uid):
    text = ('💱 <b>Currency Converter</b>\n' + LINE + '\n'
            '🌍 লাইভ রেট • ১৫০+ কারেন্সি\n\n'
            '✍️ সরাসরি লিখুন:\n<code>100 USD to BDT</code>\n<code>$50 in taka</code>\n<code>500 ডলার টাকা</code>' + FOOTER)
    return text, kb([btn('💱 Convert', 'cv'), btn('📈 Rates', 'rt')],
                    [btn('⭐ Favorites', 'fv'), btn('🔔 Alerts', 'al')],
                    [btn('🧮 Multi-Convert', 'cm'), btn('❓ Help', 'chelp')])


HELP = ('❓ <b>Currency Converter — সাহায্য</b>\n' + LINE + '\n'
        '• <b>Convert:</b> বাটনে ধাপে ধাপে, অথবা সরাসরি <code>100 USD BDT</code>\n'
        '• <b>Rates:</b> যেকোনো বেস কারেন্সির লাইভ রেট; ১৫০+ কারেন্সির পূর্ণ তালিকাও আছে\n'
        '• <b>Favorites:</b> পছন্দের পেয়ার সেভ করুন, আগের তুলনায় রেট বাড়ল/কমল দেখুন\n'
        '• <b>Alerts:</b> "১ USD ১২৫ টাকা হলে জানাও" — টার্গেটে পৌঁছালে বট মেসেজ দেবে\n'
        '• <b>Multi-Convert:</b> একটি অ্যামাউন্ট একসাথে অনেক কারেন্সিতে\n\n'
        '⌨️ <code>/convert 100 USD BDT</code> · <code>/rates EUR</code> · <code>/fav USD BDT</code> · <code>/alerts</code>\n'
        'ℹ️ রেট ১০ মিনিট ক্যাশ হয়; ব্যাংকের আসল রেটের সাথে সামান্য পার্থক্য থাকতে পারে।')


@route('chelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


def grid(prefix, extra=None):
    rows = []
    for i in range(0, len(GRID), 4):
        rows.append([btn('%s %s' % (flag(c), c), '%s:%s' % (prefix, c)) for c in GRID[i:i + 4]])
    if extra:
        rows.append(extra)
    return rows


def send_result(chat_id, amount, frm, to, c=None):
    try:
        text, markup = result_view(amount, frm, to)
    except RuntimeError:
        return say(chat_id, '⚠️ রেট আনা যায়নি, একটু পরে চেষ্টা করুন।', kb(nav()))
    if text is None:
        return say(chat_id, '⚠️ কারেন্সি কোড ঠিক আছে কিনা দেখুন (যেমন USD, BDT, EUR)।', kb(nav()))
    if c is not None:
        edit(c, text, markup)
    else:
        say(chat_id, text, markup)


# ---------------------------------------------------------------- convert wizard
@route('cv')
def cb_convert(c, a):
    uid = c.from_user.id
    ack(c)
    if not a:
        set_state(uid, 'cv', frm=None, to=None, stage='from')
        return edit(c, '💱 <b>Convert — ধাপ ১/৩</b>\n\nকোন কারেন্সি থেকে কনভার্ট করবেন?', kb(*grid('cv:f', [btn('🔍 অন্য কোড লিখুন', 'cv:o:f')]), nav()))
    s = get_state(uid)
    act = a[0]
    if act == 'q':
        set_state(uid, 'cv', frm=a[1], to=a[2], stage='amount')
        return say(c.message.chat.id, '✏️ <b>%s → %s</b>\nকত পরিমাণ কনভার্ট করবেন? সংখ্যা লিখুন:' % (a[1], a[2]), kb(nav()))
    if not s or s['k'] != 'cv':
        return cb_convert(c, [])
    d = s['d']
    if act == 'o':
        d['stage'] = 'from_typed' if a[1] == 'f' else 'to_typed'
        return edit(c, '🔍 কারেন্সি কোড লিখুন (যেমন <code>MYR</code> বা <code>টাকা</code>):', kb(nav()))
    if act == 'f':
        d['frm'] = a[1]
        d['stage'] = 'to'
        return edit(c, '💱 <b>Convert — ধাপ ২/৩</b>\n\n%s %s থেকে কোন কারেন্সিতে?' % (flag(a[1]), a[1]), kb(*grid('cv:t', [btn('🔍 অন্য কোড লিখুন', 'cv:o:t')]), nav('cv')))
    if act == 't':
        d['to'] = a[1]
        d['stage'] = 'amount'
        return ask_amount(c, d)
    if act == 'a':
        amt = to_float(a[1]) if len(a) > 1 else None
        if not d.get('frm') or not d.get('to') or amt is None:
            return cb_convert(c, [])
        clear_state(uid)
        return send_result(c.message.chat.id, amt, d['frm'], d['to'], c)


def ask_amount(target, d):
    rows = [[btn('{:,}'.format(x), 'cv:a:%d' % x) for x in (1, 10, 100)], [btn('{:,}'.format(x), 'cv:a:%d' % x) for x in (500, 1000, 5000)], nav('cv')]
    edit(target, '💱 <b>Convert — ধাপ ৩/৩</b>\n\n%s %s → %s %s\nকত পরিমাণ? বাটন চাপুন অথবা সংখ্যা লিখে পাঠান:' % (flag(d['frm']), d['frm'], flag(d['to']), d['to']), kb(*rows))


@route('cr')
def cb_result(c, a):
    if len(a) < 3:
        return ack(c)
    amt = to_float(a[0])
    if amt is None:
        return ack(c)
    ack(c)
    send_result(c.message.chat.id, amt, a[1], a[2], c)


# ---------------------------------------------------------------- rates
@route('rt')
def cb_rates(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    base = a[0] if a else u['base']
    ack(c)
    try:
        d = get_rates(base)
    except RuntimeError:
        return edit(c, '⚠️ রেট আনা যায়নি, একটু পরে চেষ্টা করুন।', kb(nav()))
    if d is None:
        return edit(c, '⚠️ কারেন্সি কোড ঠিক নেই।', kb(nav()))
    u['base'] = base
    DB.save()
    lines = ['📈 <b>1 %s %s =</b>\n' % (flag(base), base)]
    for cur in ['BDT'] + POPULAR:
        if cur != base and cur in d['rates']:
            lines.append('%s <b>%s</b>: %s' % (flag(cur), cur, fmt_rate(d['rates'][cur])))
    lines.append('\n🕒 %s%s' % (short_time(d['upd']), ' (ক্যাশ)' if d.get('stale') else ''))
    rows = grid('rt', [btn('🌍 সব কারেন্সি (%d)' % len(d['rates']), 'ra:%s:0' % base), btn('🔍 অন্য বেস', 'rs')])
    rows.append(nav())
    edit(c, '\n'.join(lines), kb(*rows))


@route('ra')
def cb_all(c, a):
    base = a[0] if a else 'USD'
    page = to_int(a[1], 0) if len(a) > 1 else 0
    try:
        d = get_rates(base)
    except RuntimeError:
        return ack(c, 'রেট আনা যায়নি', True)
    if d is None:
        return ack(c, 'কারেন্সি কোড ঠিক নেই', True)
    items = sorted(d['rates'].items())
    chunk, page, pages = paginate(items, page, 15)
    text = '🌍 <b>1 %s =</b>  (পৃষ্ঠা %d/%d)\n\n' % (base, page + 1, pages) + '\n'.join('%s <b>%s</b>: %s' % (flag(k), k, fmt_rate(v)) for k, v in chunk)
    rows = []
    pg = pager('ra:%s' % base, page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav('rt:' + base))
    edit(c, text, kb(*rows))


@route('rs')
def cb_rates_search(c, a):
    ack(c)
    set_state(c.from_user.id, 'rates')
    edit(c, '🔍 যে কারেন্সির রেট দেখতে চান তার কোড লিখুন (যেমন <code>EUR</code> বা <code>পাউন্ড</code>):', kb(nav('rt')))


# ---------------------------------------------------------------- favorites
def fav_view(uid):
    u = ucfg(uid)
    if not u['fav']:
        return ('⭐ <b>Favorites</b>\n\nএখনো কোনো পেয়ার সেভ করেননি।\nCovert রেজাল্টে ⭐ Save চাপুন অথবা লিখুন:\n<code>/fav USD BDT</code>',
                kb([btn('💱 Convert', 'cv')], nav()))
    lines = ['⭐ <b>Favorites</b>\n' + LINE]
    rows = []
    now_t = time.time()
    for i, (f, t) in enumerate(u['fav']):
        try:
            d = get_rates(f)
            r = d['rates'].get(t) if d else None
        except RuntimeError:
            r = None
        if r is None:
            lines.append('%s %s → %s %s: <i>পাওয়া যায়নি</i>' % (flag(f), f, flag(t), t))
        else:
            key = '%s|%s' % (f, t)
            sn = u['snap'].get(key)
            arrow = ''
            if sn and sn['r']:
                ch = (r - sn['r']) / sn['r'] * 100
                if abs(ch) >= 0.005:
                    arrow = '  %s %+.2f%%' % ('🔺' if ch > 0 else '🔻', ch)
            if not sn or now_t - sn['t'] > 3600 * 6:
                u['snap'][key] = {'r': r, 't': now_t}
            lines.append('%s %s → %s %s: <b>%s</b>%s' % (flag(f), f, flag(t), t, fmt_rate(r), arrow))
        rows.append([btn('💱 %s→%s' % (f, t), 'cr:1:%s:%s' % (f, t)), btn('🗑', 'fv:d:%d' % i)])
    DB.save()
    lines.append('\n<i>🔺/🔻 = কয়েক ঘণ্টা আগের তুলনায় পরিবর্তন</i>')
    rows.append(nav())
    return '\n'.join(lines), kb(*rows)


@route('fv')
def cb_fav(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    if len(a) >= 3 and a[0] == 'a':
        pair = [a[1], a[2]]
        if pair in u['fav']:
            return ack(c, 'আগেই সেভ করা আছে')
        if len(u['fav']) >= MAX_FAV:
            return ack(c, 'সর্বোচ্চ %d টি পেয়ার সেভ করা যায়' % MAX_FAV, True)
        u['fav'].append(pair)
        DB.save()
        return ack(c, '⭐ সেভ হয়েছে')
    if len(a) >= 2 and a[0] == 'd':
        i = to_int(a[1], -1)
        if 0 <= i < len(u['fav']):
            u['fav'].pop(i)
            DB.save()
            ack(c, '🗑 মুছে ফেলা হয়েছে')
    ack(c)
    t, k = fav_view(uid)
    edit(c, t, k)


@bot.message_handler(commands=['fav'])
def fav_cmd(m):
    uid = m.from_user.id
    toks = [resolve_cur(x) for x in cmd_arg(m).split()]
    u = ucfg(uid)
    if len(toks) == 2 and all(toks):
        if [toks[0], toks[1]] not in u['fav'] and len(u['fav']) < MAX_FAV:
            u['fav'].append([toks[0], toks[1]])
            DB.save()
    t, k = fav_view(uid)
    say(m.chat.id, t, k)


# ---------------------------------------------------------------- multi convert
@route('cm')
def cb_multi(c, a):
    ack(c)
    set_state(c.from_user.id, 'multi')
    edit(c, '🧮 <b>Multi-Convert</b>\n\nপরিমাণ ও কারেন্সি লিখুন, যেমন:\n<code>100 USD</code>\n<code>5000 টাকা</code>\n\nএকসাথে অনেক কারেন্সিতে দেখাব।', kb(nav()))


def send_multi(chat_id, amount, frm):
    try:
        d = get_rates(frm)
    except RuntimeError:
        return say(chat_id, '⚠️ রেট আনা যায়নি, একটু পরে চেষ্টা করুন।', kb(nav()))
    if d is None:
        return say(chat_id, '⚠️ কারেন্সি কোড ঠিক নেই।', kb(nav()))
    lines = ['🧮 <b>%s %s %s =</b>\n' % (flag(frm), fmt_amt(amount), frm)]
    for cur in ['BDT'] + POPULAR:
        if cur != frm and cur in d['rates']:
            lines.append('%s <b>%s</b>  %s' % (flag(cur), cur, fmt_amt(amount * d['rates'][cur])))
    say(chat_id, '\n'.join(lines) + '\n\n🕒 ' + short_time(d['upd']), kb([btn('🧮 আবার', 'cm'), btn('🏠 Home', 'home')]))


# ---------------------------------------------------------------- alerts
def alerts_view(uid):
    u = ucfg(uid)
    lines = ['🔔 <b>Rate Alerts</b>\n' + LINE]
    rows = []
    if not u['alerts']:
        lines.append('এখনো কোনো অ্যালার্ট নেই।\nটার্গেট রেটে পৌঁছালে বট আপনাকে মেসেজ দেবে।')
    for al in u['alerts']:
        lines.append('%s %s→%s %s <b>%s</b>' % ('📈' if al['dir'] == 'up' else '📉', al['f'], al['t'], '≥' if al['dir'] == 'up' else '≤', fmt_rate(al['val'])))
        rows.append([btn('🗑 %s→%s %s %s' % (al['f'], al['t'], '≥' if al['dir'] == 'up' else '≤', fmt_rate(al['val'])), 'al:x:%d' % al['id'])])
    if len(u['alerts']) < MAX_ALERT:
        rows.append([btn('➕ নতুন Alert', 'al:new')])
    rows.append(nav())
    lines.append('\n<i>প্রতি ~৫ মিনিটে চেক হয়। টার্গেটে পৌঁছালে একবার জানানো হবে। (সর্বোচ্চ %d টি)</i>' % MAX_ALERT)
    return '\n'.join(lines), kb(*rows)


@route('al')
def cb_alert(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    act = a[0] if a else ''
    if act == 'x':
        aid = to_int(a[1] if len(a) > 1 else '', -1)
        u['alerts'] = [x for x in u['alerts'] if x['id'] != aid]
        DB.save()
        ack(c, '🗑 অ্যালার্ট মুছে গেছে')
    elif act == 'new':
        ack(c)
        if len(u['alerts']) >= MAX_ALERT:
            return ack(c, 'সর্বোচ্চ %d টি অ্যালার্ট' % MAX_ALERT, True)
        set_state(uid, 'al_pair')
        rows = [[btn('%s→%s' % (f, t), 'al:p:%s:%s' % (f, t))] for f, t in u['fav'][:6]]
        rows.append(nav('al'))
        return edit(c, '🔔 <b>নতুন Alert</b>\n\nকোন পেয়ারের জন্য? লিখুন (যেমন <code>USD BDT</code>)' + (' অথবা নিচ থেকে বাছুন:' if rows[:-1] else ''), kb(*rows))
    elif act == 'p':
        if len(a) < 3:
            return ack(c)
        ack(c)
        if len(u['alerts']) >= MAX_ALERT:
            return ack(c, 'সর্বোচ্চ %d টি অ্যালার্ট' % MAX_ALERT, True)
        return ask_dir(c, uid, a[1], a[2])
    elif act == 'd':
        s = get_state(uid)
        if not s or s['k'] != 'al_dir' or len(a) < 2 or a[1] not in ('up', 'dn'):
            return ack(c, 'আবার শুরু করুন', True)
        ack(c)
        s['d']['dir'] = a[1]
        set_state(uid, 'al_val', **s['d'])
        return edit(c, '🎯 <b>টার্গেট রেট লিখুন</b>\n\n%s→%s এর রেট %s হলে জানাব।\nযেমন: <code>125</code>' % (
            s['d']['f'], s['d']['t'], 'এর সমান বা বেশি' if a[1] == 'up' else 'এর সমান বা কম'), kb(nav('al')))
    else:
        ack(c)
    t, k = alerts_view(uid)
    edit(c, t, k)


def ask_dir(target, uid, f, t):
    try:
        d = get_rates(f)
        r = d['rates'].get(t) if d else None
    except RuntimeError:
        r = None
    if r is None:
        return edit(target, '⚠️ এই পেয়ারের রেট পাওয়া যায়নি।', kb(nav('al')))
    set_state(uid, 'al_dir', f=f, t=t, now=r)
    edit(target, '🔔 <b>%s → %s</b>\nবর্তমান রেট: <b>%s</b>\n\nকখন জানাব?' % (f, t, fmt_rate(r)),
         kb([btn('📈 বাড়লে (≥)', 'al:d:up'), btn('📉 কমলে (≤)', 'al:d:dn')], nav('al')))


@bot.message_handler(commands=['alerts'])
def alerts_cmd(m):
    t, k = alerts_view(m.from_user.id)
    say(m.chat.id, t, k)


def alert_tick():
    with DB.lock:
        users = [(uid, u) for uid, u in DB.d['users'].items() if u.get('alerts')]
    changed = False
    for uid, u in users:
        for al in list(u['alerts']):
            try:
                d = get_rates(al['f'])
            except RuntimeError:
                continue
            r = d['rates'].get(al['t']) if d else None
            if r is None:
                continue
            if (al['dir'] == 'up' and r >= al['val']) or (al['dir'] == 'dn' and r <= al['val']):
                u['alerts'] = [x for x in u['alerts'] if x['id'] != al['id']]
                changed = True
                say(int(uid), '🔔 <b>Rate Alert!</b>\n\n%s %s → %s %s\nএখন: <b>1 %s = %s %s</b>\nটার্গেট ছিল: %s %s' % (
                    flag(al['f']), al['f'], flag(al['t']), al['t'], al['f'], fmt_rate(r), al['t'], '≥' if al['dir'] == 'up' else '≤', fmt_rate(al['val'])),
                    kb([btn('💱 Convert', 'cr:1:%s:%s' % (al['f'], al['t'])), btn('🔔 নতুন Alert', 'al:new')]))
    if changed:
        DB.save()


# ---------------------------------------------------------------- commands & text
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    clear_state(m.from_user.id)
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()))


@bot.message_handler(commands=['convert'])
def convert_cmd(m):
    p = parse_convert(cmd_arg(m))
    if not p:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/convert 100 USD BDT</code>', kb([btn('💱 বাটনে Convert', 'cv')], nav()))
    send_result(m.chat.id, p[0], p[1], p[2])


@bot.message_handler(commands=['rates'])
def rates_cmd(m):
    arg = cmd_arg(m).split()
    base = (resolve_cur(arg[0]) if arg else None) or ucfg(m.from_user.id)['base']
    try:
        d = get_rates(base)
    except RuntimeError:
        return say(m.chat.id, '⚠️ রেট আনা যায়নি, একটু পরে চেষ্টা করুন।')
    if d is None:
        return say(m.chat.id, '⚠️ কারেন্সি কোড ঠিক নেই।')
    lines = ['📈 <b>1 %s %s =</b>\n' % (flag(base), base)]
    for cur in ['BDT'] + POPULAR:
        if cur != base and cur in d['rates']:
            lines.append('%s <b>%s</b>: %s' % (flag(cur), cur, fmt_rate(d['rates'][cur])))
    say(m.chat.id, '\n'.join(lines), kb(*grid('rt', [btn('🌍 সব কারেন্সি', 'ra:%s:0' % base)]), nav()))


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = m.text.strip()
    if not rate_ok(uid, 'cur', 0.5):
        return
    if s:
        k, d = s['k'], s['d']
        if k == 'cv' and d['stage'] in ('from_typed', 'to_typed'):
            cur = resolve_cur(txt.split()[0])
            if not cur:
                return say(m.chat.id, '⚠️ কারেন্সি কোড ঠিক নেই। আবার লিখুন (যেমন MYR)।')
            if d['stage'] == 'from_typed':
                d['frm'], d['stage'] = cur, 'to'
                return say(m.chat.id, '💱 <b>ধাপ ২/৩</b>\n\n%s %s থেকে কোন কারেন্সিতে?' % (flag(cur), cur), kb(*grid('cv:t', [btn('🔍 অন্য কোড লিখুন', 'cv:o:t')]), nav('cv')))
            d['to'], d['stage'] = cur, 'amount'
            return say(m.chat.id, '💱 <b>ধাপ ৩/৩</b>\n\n%s → %s\nকত পরিমাণ? সংখ্যা লিখুন:' % (d['frm'], cur), kb([btn('{:,}'.format(x), 'cv:a:%d' % x) for x in (1, 10, 100, 1000)], nav('cv')))
        if k == 'cv' and d['stage'] == 'amount':
            amt = to_float(txt)
            if amt is None or amt <= 0:
                return say(m.chat.id, '⚠️ শুধু সংখ্যা লিখুন (যেমন 250)।')
            clear_state(uid)
            return send_result(m.chat.id, amt, d['frm'], d['to'])
        if k == 'rates':
            cur = resolve_cur(txt.split()[0])
            clear_state(uid)
            if not cur:
                return say(m.chat.id, '⚠️ কারেন্সি কোড ঠিক নেই।')
            ucfg(uid)['base'] = cur
            return say(m.chat.id, '📈 <b>%s</b> সিলেক্ট হয়েছে।' % cur, kb([btn('📈 Rates দেখুন', 'rt:' + cur)], nav()))
        if k == 'multi':
            p = parse_convert(txt)
            if not p:
                return say(m.chat.id, '⚠️ যেমন লিখুন: <code>100 USD</code>')
            clear_state(uid)
            return send_multi(m.chat.id, p[0], p[1])
        if k == 'al_pair':
            toks = [resolve_cur(x) for x in txt.split()]
            if len(toks) != 2 or not all(toks):
                return say(m.chat.id, '⚠️ যেমন লিখুন: <code>USD BDT</code>')
            return ask_dir(m, uid, toks[0], toks[1])
        if k == 'al_val':
            v = to_float(txt)
            if v is None or v <= 0:
                return say(m.chat.id, '⚠️ শুধু সংখ্যা লিখুন (যেমন 125)।')
            u = ucfg(uid)
            with DB.lock:
                aid = DB.d['next']
                DB.d['next'] += 1
                u['alerts'].append({'id': aid, 'f': d['f'], 't': d['t'], 'dir': d['dir'], 'val': v})
                DB.save()
            clear_state(uid)
            t, kk = alerts_view(uid)
            return say(m.chat.id, '✅ অ্যালার্ট সেট হয়েছে!\n\n' + t, kk)
    p = parse_convert(txt)
    if p:
        return send_result(m.chat.id, p[0], p[1], p[2])
    t, k = home_view(uid)
    say(m.chat.id, '🤔 বুঝতে পারিনি। এভাবে লিখুন: <code>100 USD to BDT</code>\n\n' + t, k)


start_worker(alert_tick, 300)
run(commands=[('start', 'মেনু খুলুন'), ('convert', 'কারেন্সি কনভার্ট'), ('rates', 'লাইভ রেট'), ('fav', 'ফেভারিট পেয়ার'),
              ('alerts', 'রেট অ্যালার্ট'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "quiz_bot": {
        "emoji": '🧠',
        "name": 'Quiz Bot',
        "proj_name": 'Quiz Bot',
        "category": '🎮 Fun & Learning',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~30 sec',
        "features": ['৪৯টি বিল্ট-ইন প্রশ্ন, ৬ ক্যাটাগরি (বাংলাদেশ, সাধারণ জ্ঞান, বিজ্ঞান, গণিত, English, প্রযুক্তি)', '🎯 টাইমার, পয়েন্ট, স্পিড-বোনাস ও স্ট্রিক সহ খেলা', '📅 Daily Quiz (২× পয়েন্ট) ও ডেইলি স্ট্রিক', '👥 Group Quiz — /startquiz দিয়ে গ্রুপে সবাই একসাথে, শেষে লিডারবোর্ড', '📚 My Sets — নিজের প্রশ্ন, বাল্ক ইম্পোর্ট, শেয়ার লিংক, সেট-লিডারবোর্ড', '🏆 গ্লোবাল লিডারবোর্ড ও 📊 পরিসংখ্যান'],
        "commands": '/quiz প্রশ্ন | সঠিক | ভুল১ | ভুল২ · /startquiz · /stopquiz · /leaderboard · /mystats',
        "about": 'কুইজ প্ল্যাটফর্ম — নিজে খেলুন, গ্রুপে প্রতিযোগিতা করুন, নিজের কুইজ সেট বানিয়ে লিংকে শেয়ার করুন।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', '/start → 🎯 খেলুন, বিষয় ও টাইমার বেছে শুরু করুন', '📚 My Sets-এ নিজের প্রশ্ন যোগ বা বাল্ক ইম্পোর্ট করুন, লিংক শেয়ার করুন', 'গ্রুপে বট যোগ করে /startquiz লিখুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 /quiz বাংলাদেশের রাজধানী কী? | ঢাকা | চট্টগ্রাম | রাজশাহী\n🤖 🧠 [Quiz Poll] বাংলাদেশের রাজধানী কী?',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('quiz.json', {'users': {}, 'sets': {}, 'next': 1})
SESS, GSESS, POLLS, CFG = {}, {}, {}, {}
COUNTS = [5, 10, 15, 20]
TIMERS_P = [0, 15, 20, 30, 45]
TIMERS_G = [15, 20, 30, 45, 60]
MAX_SETS, MAX_QS = 20, 100

BANK = {
    '🇧🇩 বাংলাদেশ': [
        ('বাংলাদেশের রাজধানী কোনটি?', 'ঢাকা', ['চট্টগ্রাম', 'খুলনা', 'রাজশাহী']),
        ('বাংলাদেশের জাতীয় ফুল কোনটি?', 'শাপলা', ['গোলাপ', 'জুঁই', 'কৃষ্ণচূড়া']),
        ('বাংলাদেশের জাতীয় ফল কোনটি?', 'কাঁঠাল', ['আম', 'লিচু', 'কলা']),
        ('বাংলাদেশের স্বাধীনতা দিবস কবে?', '২৬ মার্চ', ['১৬ ডিসেম্বর', '২১ ফেব্রুয়ারি', '১৫ আগস্ট']),
        ('বাংলাদেশের বিজয় দিবস কবে?', '১৬ ডিসেম্বর', ['২৬ মার্চ', '২১ ফেব্রুয়ারি', '১৭ এপ্রিল']),
        ('ভাষা আন্দোলন কত সালে হয়েছিল?', '১৯৫২', ['১৯৪৭', '১৯৭১', '১৯৬৯']),
        ('বাংলাদেশের জাতীয় পশু কোনটি?', 'রয়েল বেঙ্গল টাইগার', ['সিংহ', 'হাতি', 'হরিণ']),
        ('বাংলাদেশের মুদ্রার নাম কী?', 'টাকা', ['রুপি', 'রিয়াল', 'ডলার']),
        ('সুন্দরবন কোন সাগরের তীরে অবস্থিত?', 'বঙ্গোপসাগর', ['আরব সাগর', 'লোহিত সাগর', 'দক্ষিণ চীন সাগর']),
    ],
    '🌍 সাধারণ জ্ঞান': [
        ('পৃথিবীর বৃহত্তম মহাসাগর কোনটি?', 'প্রশান্ত মহাসাগর', ['আটলান্টিক মহাসাগর', 'ভারত মহাসাগর', 'আর্কটিক মহাসাগর']),
        ('পৃথিবীর বৃহত্তম মহাদেশ কোনটি?', 'এশিয়া', ['আফ্রিকা', 'ইউরোপ', 'উত্তর আমেরিকা']),
        ('পৃথিবীর সর্বোচ্চ পর্বতশৃঙ্গ কোনটি?', 'মাউন্ট এভারেস্ট', ['কে-টু', 'কাঞ্চনজঙ্ঘা', 'কিলিমাঞ্জারো']),
        ('জাতিসংঘের সদর দপ্তর কোথায়?', 'নিউ ইয়র্ক', ['জেনেভা', 'প্যারিস', 'লন্ডন']),
        ('ক্ষেত্রফলে বিশ্বের বৃহত্তম দেশ কোনটি?', 'রাশিয়া', ['কানাডা', 'চীন', 'যুক্তরাষ্ট্র']),
        ('অলিম্পিক গেমস সাধারণত কত বছর পর পর হয়?', '৪ বছর', ['২ বছর', '৩ বছর', '৫ বছর']),
        ('একটি দাবা বোর্ডে মোট ঘর কয়টি?', '৬৪', ['৩২', '৮১', '১০০']),
        ('২০২২ সালের ফিফা বিশ্বকাপ কোথায় হয়েছিল?', 'কাতার', ['রাশিয়া', 'ব্রাজিল', 'জার্মানি']),
    ],
    '🔬 বিজ্ঞান': [
        ('পানির রাসায়নিক সংকেত কোনটি?', 'H₂O', ['CO₂', 'O₂', 'NaCl']),
        ('সূর্য কী?', 'তারা', ['গ্রহ', 'উপগ্রহ', 'ধূমকেতু']),
        ('প্রাপ্তবয়স্ক মানুষের দেহে হাড়ের সংখ্যা কত?', '২০৬', ['১৫০', '২৫৬', '৩০৬']),
        ('আলোর গতি প্রায় কত?', '৩,০০,০০০ কিমি/সেকেন্ড', ['৩০,০০০ কিমি/সেকেন্ড', '১,৫০,০০০ কিমি/সেকেন্ড', '৩,০০০ কিমি/সেকেন্ড']),
        ('সবচেয়ে হালকা মৌলিক পদার্থ কোনটি?', 'হাইড্রোজেন', ['হিলিয়াম', 'অক্সিজেন', 'লিথিয়াম']),
        ('সালোকসংশ্লেষণে উদ্ভিদ কোন গ্যাস গ্রহণ করে?', 'কার্বন ডাই-অক্সাইড', ['অক্সিজেন', 'নাইট্রোজেন', 'হাইড্রোজেন']),
        ('রক্তে অক্সিজেন বহন করে কোনটি?', 'হিমোগ্লোবিন', ['ইনসুলিন', 'কেরাটিন', 'অ্যামাইলেজ']),
        ('সৌরজগতের বৃহত্তম গ্রহ কোনটি?', 'বৃহস্পতি', ['শনি', 'পৃথিবী', 'মঙ্গল']),
    ],
    '🔢 গণিত': [
        ('১২ × ১২ = কত?', '১৪৪', ['১২৪', '১৫৪', '১৬৪']),
        ('২০০-এর ১৫% কত?', '৩০', ['২৫', '৩৫', '২০']),
        ('√১৪৪ = কত?', '১২', ['১৪', '১৬', '১০']),
        ('ত্রিভুজের তিন কোণের সমষ্টি কত?', '১৮০°', ['৯০°', '২৭০°', '৩৬০°']),
        ('২⁵ = কত?', '৩২', ['১৬', '২৫', '৬৪']),
        ('π (পাই)-এর মান প্রায় কত?', '৩.১৪', ['২.৭১', '৩.৪১', '১.৬১']),
        ('৭ × ৮ = কত?', '৫৬', ['৪৮', '৬৪', '৫৪']),
        ('বৃত্তের ব্যাসার্ধ ৭ হলে ব্যাস কত?', '১৪', ['৭', '২১', '৪৯']),
    ],
    '🔤 English': [
        ('Opposite of "brave"?', 'Cowardly', ['Bold', 'Fearless', 'Strong']),
        ('Plural of "child"?', 'Children', ['Childs', 'Childes', 'Childrens']),
        ('Past tense of "go"?', 'Went', ['Goed', 'Gone', 'Going']),
        ('Synonym of "happy"?', 'Joyful', ['Sad', 'Angry', 'Tired']),
        ('Which one is a noun?', 'Table', ['Run', 'Quickly', 'Beautiful']),
        ('She ___ to school every day.', 'goes', ['go', 'going', 'gone']),
        ('Correct spelling?', 'Receive', ['Recieve', 'Receve', 'Receeve']),
        ('An apple a day keeps the ___ away.', 'doctor', ['teacher', 'dentist', 'nurse']),
    ],
    '💻 প্রযুক্তি': [
        ('HTML-এর পূর্ণরূপ কী?', 'HyperText Markup Language', ['HighText Machine Language', 'HyperTool Multi Language', 'Hyper Transfer Markup Language']),
        ('CPU-এর পূর্ণরূপ কী?', 'Central Processing Unit', ['Central Program Unit', 'Computer Personal Unit', 'Central Processor Utility']),
        ('Python কী?', 'প্রোগ্রামিং ভাষা', ['ওয়েব ব্রাউজার', 'অপারেটিং সিস্টেম', 'ডাটাবেস']),
        ('WWW-এর পূর্ণরূপ কী?', 'World Wide Web', ['World Web Wide', 'Wide World Web', 'Web World Wide']),
        ('Telegram কী ধরনের অ্যাপ?', 'মেসেজিং অ্যাপ', ['ভিডিও এডিটর', 'ব্যাংকিং অ্যাপ', 'গেম']),
        ('RAM কী ধরনের মেমোরি?', 'অস্থায়ী (Volatile)', ['স্থায়ী (Non-volatile)', 'শুধু পড়ার (ROM)', 'ক্লাউড']),
        ('১ বাইটে কত বিট থাকে?', '৮', ['৪', '১৬', '১০']),
        ('কোনটি ওয়েব ব্রাউজার?', 'Chrome', ['Excel', 'Photoshop', 'Notepad']),
    ],
}
CATS = list(BANK.keys())


def norm_bank():
    out = {}
    for cat, items in BANK.items():
        out[cat] = [{'q': q, 'a': a, 'w': w, 'ex': ''} for q, a, w in items]
    return out


BANK_N = norm_bank()


def user(uid, name=None):
    u = DB.d['users'].setdefault(str(uid), {'name': 'User', 'points': 0, 'played': 0, 'correct': 0, 'answered': 0, 'best_streak': 0, 'daily': '', 'dstreak': 0})
    if name:
        u['name'] = name[:30]
    return u


def get_set(sid):
    return DB.d['sets'].get(str(sid))


def my_sets(uid):
    return [(k, v) for k, v in DB.d['sets'].items() if v['owner'] == uid]


def set_by_arg(arg):
    sid = to_int(arg, -1)
    return sid, get_set(sid)


def shuffled(q):
    opts = [q['a']] + list(q['w'][:9])
    random.shuffle(opts)
    return [o[:100] for o in opts], opts.index(q['a'])


def pick_questions(src, n):
    if src == 'mix':
        pool = [q for items in BANK_N.values() for q in items]
    elif src.startswith('c'):
        pool = list(BANK_N[CATS[int(src[1:])]])
    else:
        st = get_set(src[1:])
        pool = list(st['qs']) if st else []
    random.shuffle(pool)
    return pool[:n]


def src_label(src):
    if src == 'mix':
        return '🎲 মিক্সড'
    if src.startswith('c'):
        return CATS[int(src[1:])]
    st = get_set(src[1:])
    return '📚 ' + (st['name'] if st else 'সেট')


# ---------------------------------------------------------------- home & menus
def home_view(uid):
    u = user(uid)
    text = ('🧠 <b>Quiz Bot</b>\n' + LINE + '\nনিজে খেলুন, বন্ধুদের সাথে গ্রুপে খেলুন, বা নিজের কুইজ সেট বানান!\n\n'
            '⭐ আপনার পয়েন্ট: <b>%d</b>   🎮 খেলা: <b>%d</b>' % (u['points'], u['played']) + FOOTER)
    return text, kb([btn('🎯 খেলুন', 'qp'), btn('📅 Daily Quiz', 'qd')],
                    [btn('👥 Group Quiz', 'qg'), btn('📚 My Sets', 'qm')],
                    [btn('🏆 Leaderboard', 'qlb'), btn('📊 My Stats', 'qst')],
                    [btn('❓ Help', 'qhelp')])


HELP = ('❓ <b>Quiz Bot — সাহায্য</b>\n' + LINE + '\n'
        '🎯 <b>খেলুন:</b> ক্যাটাগরি (বাংলাদেশ, সাধারণ জ্ঞান, বিজ্ঞান, গণিত, English, প্রযুক্তি) বা নিজের সেট থেকে, টাইমারসহ\n'
        '⭐ সঠিক উত্তরে ১০ পয়েন্ট + টাইমার থাকলে দ্রুত উত্তরের বোনাস + টানা সঠিকের স্ট্রিক বোনাস\n'
        '📅 <b>Daily Quiz:</b> প্রতিদিন একটি প্রশ্ন — ২× পয়েন্ট ও ডেইলি স্ট্রিক\n'
        '👥 <b>Group Quiz:</b> গ্রুপে /startquiz — সবাই একসাথে, শেষে লিডারবোর্ড\n'
        '📚 <b>My Sets:</b> নিজের কুইজ সেট বানান, বাল্ক ইম্পোর্ট করুন, লিংক শেয়ার করুন, সেট-লিডারবোর্ড দেখুন\n\n'
        '📥 <b>বাল্ক ফরম্যাট</b> (প্রতি লাইনে একটি):\n<code>প্রশ্ন | সঠিক উত্তর | ভুল ১ | ভুল ২ // ব্যাখ্যা</code>\n\n'
        '⌨️ <code>/quiz প্রশ্ন | সঠিক | ভুল ১ | ভুল ২</code> — এখনই কুইজ পোল · /startquiz · /stopquiz · /leaderboard · /mystats')


@route('qhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


def cfg_view(cfg, mode):
    ts = TIMERS_G if mode == 'g' else TIMERS_P
    t = cfg['t'] if cfg['t'] in ts else ts[0]
    cfg['t'] = t
    cats = [[btn(('✅ ' if cfg['src'] == 'c%d' % i else '') + CATS[i], 'qc:%s:s:c%d' % (mode, i)) for i in (j, j + 1)] for j in (0, 2, 4)]
    rows = cats + [[btn(('✅ ' if cfg['src'] == 'mix' else '') + '🎲 মিক্সড', 'qc:%s:s:mix' % mode)]]
    sets = my_sets(cfg['owner'])
    for k, v in sets[:4]:
        rows.append([btn(('✅ ' if cfg['src'] == 's' + k else '') + '📚 %s (%d)' % (v['name'][:20], len(v['qs'])), 'qc:%s:s:s%s' % (mode, k))])
    rows.append([btn('🔢 প্রশ্ন: %d' % cfg['n'], 'qc:%s:n' % mode), btn('⏱ টাইমার: %s' % ('নেই' if not t else '%ds' % t), 'qc:%s:t' % mode)])
    rows.append([btn('▶️ শুরু করুন', 'qc:%s:go' % mode)])
    rows.append(nav())
    text = '🎯 <b>%s</b>\n' % ('Group Quiz সেটআপ' if mode == 'g' else 'কুইজ সেটআপ') + LINE + '\n📂 বিষয়: <b>%s</b>\n🔢 প্রশ্ন: <b>%d</b>   ⏱ টাইমার: <b>%s</b>' % (src_label(cfg['src']), cfg['n'], 'নেই' if not t else '%d সেকেন্ড' % t)
    if mode == 'g':
        text += '\n\n👑 হোস্ট: %s' % esc(cfg.get('hname', ''))
    return text, kb(*rows)


@route('qp')
def cb_play(c, a):
    ack(c)
    uid = c.from_user.id
    CFG[('p', uid)] = {'src': 'mix', 'n': 10, 't': 0, 'owner': uid}
    t, k = cfg_view(CFG[('p', uid)], 'p')
    edit(c, t, k)


@route('qc')
def cb_cfg(c, a):
    if len(a) < 2:
        return ack(c)
    mode, act = a[0], a[1]
    uid = c.from_user.id
    key = ('g', c.message.chat.id) if mode == 'g' else ('p', uid)
    cfg = CFG.get(key)
    if not cfg:
        return ack(c, 'সেশন শেষ — আবার শুরু করুন', True)
    if mode == 'g' and uid != cfg['host'] and not is_chat_admin(c.message.chat.id, uid):
        return ack(c, '🚫 শুধু হোস্ট বা অ্যাডমিন সেটআপ বদলাতে পারে', True)
    if act == 's' and len(a) > 2:
        src = a[2]
        if src == 'mix' or (src.startswith('c') and src[1:].isdigit() and int(src[1:]) < len(CATS)) or (src.startswith('s') and get_set(src[1:])):
            cfg['src'] = src
    elif act == 'n':
        cfg['n'] = COUNTS[(COUNTS.index(cfg['n']) + 1) % len(COUNTS)] if cfg['n'] in COUNTS else 10
    elif act == 't':
        ts = TIMERS_G if mode == 'g' else TIMERS_P
        cfg['t'] = ts[(ts.index(cfg['t']) + 1) % len(ts)] if cfg['t'] in ts else ts[0]
    elif act == 'go':
        qs = pick_questions(cfg['src'], cfg['n'])
        if not qs:
            return ack(c, 'এই সেটে কোনো প্রশ্ন নেই', True)
        ack(c, '🚀 শুরু হচ্ছে...')
        CFG.pop(key, None)
        if mode == 'g':
            return start_group(c.message.chat.id, cfg, qs, c)
        return start_single(uid, c.message.chat.id, c.from_user.first_name, qs, cfg['t'], cfg['src'])
    ack(c)
    t, k = cfg_view(cfg, mode)
    edit(c, t, k)


# ---------------------------------------------------------------- single player
def points_for(correct, t, elapsed, streak):
    if not correct:
        return 0
    pts = 10
    if t:
        pts += max(0, int((t - elapsed) / float(t) * 5))
    if streak >= 2:
        pts += min(streak - 1, 5)
    return pts


def start_single(uid, chat_id, name, qs, t, src, daily=False):
    old = SESS.pop(uid, None)
    user(uid, name)
    SESS[uid] = {'qs': qs, 'i': 0, 'score': 0, 'ok': 0, 'streak': 0, 'best': 0, 'chat': chat_id, 't': t, 'src': src, 'name': name,
                 'daily': daily, 'cur': -1, 'answered': True, 'pid': None, 'skipped': 0, 'nans': 0}
    single_next(uid)


def single_next(uid):
    s = SESS.get(uid)
    if not s:
        return
    if s['i'] >= len(s['qs']):
        return finish_single(uid)
    q = s['qs'][s['i']]
    opts, ci = shuffled(q)
    kw = {'type': 'quiz', 'correct_option_id': ci, 'is_anonymous': False,
          'reply_markup': kb([btn('⏭ Skip', 'qz:skip:%d' % s['i']), btn('⏹ End', 'qz:end')])}
    if q.get('ex'):
        kw['explanation'] = q['ex'][:200]
    if s['t']:
        kw['open_period'] = s['t']
    try:
        msg = bot.send_poll(s['chat'], ('(%d/%d) %s' % (s['i'] + 1, len(s['qs']), q['q']))[:300], opts, **kw)
    except Exception as e:
        log('quiz send', str(e)[:150])
        SESS.pop(uid, None)
        return say(s['chat'], '⚠️ কুইজ পাঠানো যায়নি। আবার চেষ্টা করুন।', kb(nav()))
    s['pid'], s['ci'], s['cur'], s['sent'], s['answered'], s['msg'] = str(msg.poll.id), ci, s['i'], time.time(), False, msg.message_id
    POLLS[s['pid']] = ('s', uid)
    if len(POLLS) > 3000:
        POLLS.pop(next(iter(POLLS)))
    if s['t']:
        tm = threading.Timer(s['t'] + 2, single_timeout, [uid, s['i']])
        tm.daemon = True
        tm.start()


def single_timeout(uid, idx):
    s = SESS.get(uid)
    if not s or s['cur'] != idx or s['answered']:
        return
    s['answered'], s['streak'], s['i'] = True, 0, s['i'] + 1
    s['skipped'] += 1
    single_next(uid)


def schedule_next(uid, idx):
    tm = threading.Timer(1.6, lambda: single_next(uid) if SESS.get(uid) and SESS[uid]['cur'] == idx else None)
    tm.daemon = True
    tm.start()


@bot.poll_answer_handler(func=lambda pa: True)
def on_answer(pa):
    info = POLLS.get(str(pa.poll_id))
    if not info or not pa.option_ids:
        return
    uid = pa.user.id
    if info[0] == 's':
        s = SESS.get(info[1])
        if not s or uid != info[1] or s['pid'] != str(pa.poll_id) or s['answered']:
            return
        s['answered'] = True
        s['nans'] += 1
        ok = pa.option_ids[0] == s['ci']
        s['streak'] = s['streak'] + 1 if ok else 0
        s['best'] = max(s['best'], s['streak'])
        pts = points_for(ok, s['t'], time.time() - s['sent'], s['streak'])
        if s['daily']:
            pts *= 2
        s['score'] += pts
        s['ok'] += 1 if ok else 0
        s['i'] += 1
        schedule_next(uid, s['cur'])
    else:
        g = GSESS.get(info[1])
        if not g or not g['active'] or g['pid'] != str(pa.poll_id):
            return
        rec = g['scores'].setdefault(uid, {'name': uname_of(pa.user), 'pts': 0, 'ok': 0, 'n': 0})
        if g['cur'] in rec.setdefault('seen', []):
            return
        rec['seen'].append(g['cur'])
        ok = pa.option_ids[0] == g['ci']
        rec['n'] += 1
        if ok:
            rec['ok'] += 1
            rec['pts'] += points_for(True, g['t'], time.time() - g['sent'], 0)


@route('qz')
def cb_inquiz(c, a):
    uid = c.from_user.id
    s = SESS.get(uid)
    if not s:
        return ack(c, 'এই কুইজ শেষ হয়ে গেছে', True)
    if a and a[0] == 'end':
        ack(c, '⏹ কুইজ শেষ করা হলো')
        _call(bot.stop_poll, s['chat'], s.get('msg'))
        return finish_single(uid)
    if a and a[0] == 'skip' and len(a) > 1 and to_int(a[1], -1) == s['cur'] and not s['answered']:
        ack(c, '⏭ স্কিপ')
        s['answered'], s['streak'], s['i'] = True, 0, s['i'] + 1
        s['skipped'] += 1
        _call(bot.stop_poll, s['chat'], s.get('msg'))
        return schedule_next(uid, s['cur'])
    ack(c, 'এই প্রশ্নটি আগেই শেষ')


def finish_single(uid):
    s = SESS.pop(uid, None)
    if not s:
        return
    u = user(uid, s['name'])
    total = len(s['qs'])
    u['points'] += s['score']
    u['played'] += 1
    u['correct'] += s['ok']
    u['answered'] += s['nans']
    u['best_streak'] = max(u['best_streak'], s['best'])
    extra = ''
    if s['daily']:
        today = fmt_ts(time.time(), 6.0, '%Y-%m-%d')
        yday = fmt_ts(time.time() - 86400, 6.0, '%Y-%m-%d')
        u['dstreak'] = u['dstreak'] + 1 if u['daily'] == yday else 1
        u['daily'] = today
        extra = '\n📅 ডেইলি স্ট্রিক: <b>%d দিন</b> 🔥' % u['dstreak']
    if s['src'].startswith('s'):
        st = get_set(s['src'][1:])
        if st:
            rec = st.setdefault('lb', {}).setdefault(str(uid), {'name': s['name'][:30], 'best': 0})
            if s['score'] > rec['best']:
                rec['best'] = s['score']
                extra += '\n🏅 এই সেটে নতুন সেরা স্কোর!'
            st['plays'] = st.get('plays', 0) + 1
    DB.save()
    rank = 1 + len([x for x in DB.d['users'].values() if x['points'] > u['points']])
    acc = int(round(s['ok'] * 100.0 / max(1, s['nans'])))
    text = ('🏁 <b>কুইজ শেষ!</b>\n' + LINE + '\n📂 %s\n✅ সঠিক: <b>%d/%d</b> (%d%%)\n⭐ এই রাউন্ডের পয়েন্ট: <b>%d</b>\n🔥 সর্বোচ্চ স্ট্রিক: <b>%d</b>\n'
            '🏆 মোট পয়েন্ট: <b>%d</b>  (র‍্যাংক #%d)%s' % (src_label(s['src']) if not s['daily'] else '📅 Daily Quiz', s['ok'], total, acc, s['score'], s['best'], u['points'], rank, extra))
    again = [btn('🔁 আবার খেলুন', 'qp'), btn('🏆 Leaderboard', 'qlb')]
    say(s['chat'], text + FOOTER, kb(again, nav()))


# ---------------------------------------------------------------- daily
@route('qd')
def cb_daily(c, a):
    uid = c.from_user.id
    u = user(uid, c.from_user.first_name)
    today = fmt_ts(time.time(), 6.0, '%Y-%m-%d')
    if u['daily'] == today:
        return edit(c, '📅 <b>Daily Quiz</b>\n\nআজকের কুইজ আপনি আগেই খেলেছেন ✅\n🔥 স্ট্রিক: <b>%d দিন</b>\n\nকাল আবার আসুন!' % u['dstreak'], kb([btn('🎯 অন্য কুইজ খেলুন', 'qp')], nav()))
    pool = [q for items in BANK_N.values() for q in items]
    rnd = random.Random(today)
    q = rnd.choice(pool)
    ack(c, '📅 Daily Quiz শুরু')
    start_single(uid, c.message.chat.id, c.from_user.first_name, [q], 30, 'mix', daily=True)


# ---------------------------------------------------------------- group quiz
@route('qg')
def cb_group_info(c, a):
    edit(c, '👥 <b>Group Quiz</b>\n' + LINE + '\n১. আমাকে গ্রুপে যোগ করুন\n২. গ্রুপে লিখুন <code>/startquiz</code>\n৩. বিষয়, প্রশ্ন সংখ্যা ও টাইমার বেছে ▶️ শুরু করুন\n৪. সবাই পোলে উত্তর দেবে — শেষে লিডারবোর্ড!\n\n🛑 থামাতে: <code>/stopquiz</code>',
         kb([btn('➕ গ্রুপে যোগ করুন', url='https://t.me/%s?startgroup=true' % me().username)], nav()))


def start_group(chat_id, cfg, qs, c=None):
    old = GSESS.get(chat_id)
    if old and old['active']:
        return say(chat_id, '⚠️ এই গ্রুপে আগে থেকেই একটি কুইজ চলছে। /stopquiz দিয়ে থামান।')
    GSESS[chat_id] = {'qs': qs, 'i': 0, 'scores': {}, 'chat': chat_id, 't': cfg['t'] or 20, 'host': cfg['host'], 'active': True, 'cur': -1, 'pid': None, 'src': cfg['src']}
    say(chat_id, '🧠 <b>Group Quiz শুরু!</b>\n📂 %s • %d প্রশ্ন • ⏱ %d সেকেন্ড\n\nপ্রস্তুত হোন... ⏳' % (src_label(cfg['src']), len(qs), cfg['t'] or 20))
    tm = threading.Timer(3, group_next, [chat_id])
    tm.daemon = True
    tm.start()


def group_next(chat_id):
    g = GSESS.get(chat_id)
    if not g or not g['active']:
        return
    if g['i'] >= len(g['qs']):
        return group_finish(chat_id)
    q = g['qs'][g['i']]
    opts, ci = shuffled(q)
    kw = {'type': 'quiz', 'correct_option_id': ci, 'is_anonymous': False, 'open_period': g['t']}
    if q.get('ex'):
        kw['explanation'] = q['ex'][:200]
    try:
        msg = bot.send_poll(chat_id, ('(%d/%d) %s' % (g['i'] + 1, len(g['qs']), q['q']))[:300], opts, **kw)
    except Exception as e:
        log('group quiz send', str(e)[:150])
        g['active'] = False
        return say(chat_id, '⚠️ কুইজ পাঠানো যায়নি (বট কি পোল পাঠানোর অনুমতি পেয়েছে?)।')
    g['pid'], g['ci'], g['cur'], g['sent'] = str(msg.poll.id), ci, g['i'], time.time()
    POLLS[g['pid']] = ('g', chat_id)
    tm = threading.Timer(g['t'] + 2, group_advance, [chat_id, g['i']])
    tm.daemon = True
    tm.start()


def group_advance(chat_id, idx):
    g = GSESS.get(chat_id)
    if not g or not g['active'] or g['cur'] != idx:
        return
    g['i'] += 1
    group_next(chat_id)


def group_finish(chat_id):
    g = GSESS.pop(chat_id, None)
    if not g:
        return
    g['active'] = False
    board = sorted(g['scores'].items(), key=lambda kv: (-kv[1]['pts'], -kv[1]['ok']))
    medals = ['🥇', '🥈', '🥉']
    lines = ['🏁 <b>Group Quiz শেষ!</b>\n' + LINE]
    if not board:
        lines.append('কেউ অংশ নেয়নি 😴')
    for i, (uid, r) in enumerate(board[:10]):
        lines.append('%s <b>%s</b> — %d পয়েন্ট (%d/%d)' % (medals[i] if i < 3 else '%d.' % (i + 1), esc(r['name']), r['pts'], r['ok'], len(g['qs'])))
        u = user(uid, r['name'])
        u['points'] += r['pts']
        u['played'] += 1
        u['correct'] += r['ok']
        u['answered'] += r['n']
    DB.save()
    say(chat_id, '\n'.join(lines), kb([btn('🔁 আবার', 'qgr')]))


@route('qgr')
def cb_group_again(c, a):
    if c.message.chat.type == 'private':
        return ack(c)
    ack(c)
    begin_group_setup(c.message.chat.id, c.from_user)


def begin_group_setup(chat_id, u):
    if GSESS.get(chat_id) and GSESS[chat_id]['active']:
        return say(chat_id, '⚠️ এই গ্রুপে আগে থেকেই একটি কুইজ চলছে। /stopquiz দিয়ে থামান।')
    CFG[('g', chat_id)] = {'src': 'mix', 'n': 10, 't': 20, 'host': u.id, 'owner': u.id, 'hname': uname_of(u)}
    t, k = cfg_view(CFG[('g', chat_id)], 'g')
    say(chat_id, t, k)


@bot.message_handler(commands=['startquiz'])
def startquiz_cmd(m):
    if m.chat.type == 'private':
        return say(m.chat.id, '👥 <code>/startquiz</code> গ্রুপে ব্যবহার করুন। প্রাইভেটে 🎯 খেলুন বাটন চাপুন।', kb([btn('🎯 খেলুন', 'qp')], nav()))
    begin_group_setup(m.chat.id, m.from_user)


@bot.message_handler(commands=['stopquiz'])
def stopquiz_cmd(m):
    g = GSESS.get(m.chat.id)
    if not g or not g['active']:
        return say(m.chat.id, 'ℹ️ এখন কোনো কুইজ চলছে না।')
    if m.from_user.id != g['host'] and not is_chat_admin(m.chat.id, m.from_user.id):
        return say(m.chat.id, '🚫 শুধু হোস্ট বা অ্যাডমিন থামাতে পারে।')
    group_finish(m.chat.id)


# ---------------------------------------------------------------- stats & leaderboard
def lb_text(items, title):
    medals = ['🥇', '🥈', '🥉']
    lines = ['🏆 <b>%s</b>\n' % title + LINE]
    if not items:
        lines.append('এখনো কেউ খেলেনি।')
    for i, (name, pts) in enumerate(items[:10]):
        lines.append('%s <b>%s</b> — %d' % (medals[i] if i < 3 else '%d.' % (i + 1), esc(name), pts))
    return '\n'.join(lines)


@route('qlb')
def cb_lb(c, a):
    ack(c)
    if a and a[0] == 's':
        st = get_set(a[1] if len(a) > 1 else '')
        if not st:
            return edit(c, '⚠️ সেটটি পাওয়া যায়নি।', kb(nav()))
        items = sorted([(r['name'], r['best']) for r in st.get('lb', {}).values()], key=lambda x: -x[1])
        return edit(c, lb_text(items, 'লিডারবোর্ড — ' + esc(st['name'])), kb(nav('qv:%s' % a[1])))
    items = sorted([(u['name'], u['points']) for u in DB.d['users'].values() if u['points'] > 0], key=lambda x: -x[1])
    edit(c, lb_text(items, 'গ্লোবাল লিডারবোর্ড'), kb(nav()))


@bot.message_handler(commands=['leaderboard'])
def lb_cmd(m):
    items = sorted([(u['name'], u['points']) for u in DB.d['users'].values() if u['points'] > 0], key=lambda x: -x[1])
    say(m.chat.id, lb_text(items, 'গ্লোবাল লিডারবোর্ড'))


def stats_text(uid):
    u = user(uid)
    rank = 1 + len([x for x in DB.d['users'].values() if x['points'] > u['points']])
    acc = int(round(u['correct'] * 100.0 / u['answered'])) if u['answered'] else 0
    return ('📊 <b>আপনার পরিসংখ্যান</b>\n' + LINE + '\n⭐ মোট পয়েন্ট: <b>%d</b> (র‍্যাংক #%d)\n🎮 খেলা: <b>%d</b>\n✅ সঠিক উত্তর: <b>%d</b> (নির্ভুলতা %d%%)\n🔥 সর্বোচ্চ স্ট্রিক: <b>%d</b>\n📅 ডেইলি স্ট্রিক: <b>%d দিন</b>'
            % (u['points'], rank, u['played'], u['correct'], acc, u['best_streak'], u['dstreak']))


@route('qst')
def cb_stats(c, a):
    ack(c)
    edit(c, stats_text(c.from_user.id), kb(nav()))


@bot.message_handler(commands=['mystats'])
def stats_cmd(m):
    say(m.chat.id, stats_text(m.from_user.id))


# ---------------------------------------------------------------- my sets
def sets_view(uid):
    ms = my_sets(uid)
    lines = ['📚 <b>My Sets (%d/%d)</b>\n' % (len(ms), MAX_SETS) + LINE]
    rows = []
    for k, v in ms:
        rows.append([btn('📚 %s • %d প্রশ্ন' % (v['name'][:22], len(v['qs'])), 'qv:%s' % k)])
    if not ms:
        lines.append('এখনো কোনো সেট নেই। নিজের প্রশ্ন দিয়ে সেট বানান, লিংক শেয়ার করে সবাইকে খেলান!')
    if len(ms) < MAX_SETS:
        rows.append([btn('➕ নতুন সেট', 'qb:new')])
    rows.append(nav())
    return '\n'.join(lines), kb(*rows)


@route('qm')
def cb_sets(c, a):
    ack(c)
    clear_state(c.from_user.id)
    t, k = sets_view(c.from_user.id)
    edit(c, t, k)


def set_card(sid, st, uid):
    link = bot_link('q_%s' % sid)
    text = ('📚 <b>%s</b>\n' % esc(st['name']) + LINE + '\n❓ প্রশ্ন: <b>%d</b>/%d\n🎮 খেলা হয়েছে: <b>%d</b> বার\n🔗 শেয়ার লিংক:\n<code>%s</code>' % (len(st['qs']), MAX_QS, st.get('plays', 0), link))
    if st['owner'] != uid:
        return text, kb([btn('▶️ খেলুন', 'qpl:%s' % sid), btn('🏆 Leaderboard', 'qlb:s:%s' % sid)], nav())
    rows = [[btn('▶️ খেলুন', 'qpl:%s' % sid), btn('🏆 Leaderboard', 'qlb:s:%s' % sid)],
            [btn('➕ প্রশ্ন যোগ', 'qb:q:%s' % sid), btn('📥 Bulk Import', 'qb:bulk:%s' % sid)],
            [btn('📋 প্রশ্নগুলো', 'qa:l:%s:0' % sid), btn('📤 Share', url='https://t.me/share/url?url=' + urllib.parse.quote(link))],
            [btn('✏️ নাম বদলান', 'qb:ren:%s' % sid), btn('🗑 সেট ডিলিট', 'qa:ds:%s' % sid)], nav('qm')]
    return text, kb(*rows)


@route('qv')
def cb_set_view(c, a):
    sid, st = set_by_arg(a[0] if a else '')
    if not st:
        return ack(c, 'সেটটি পাওয়া যায়নি', True)
    ack(c)
    clear_state(c.from_user.id)
    t, k = set_card(sid, st, c.from_user.id)
    edit(c, t, k)


@route('qpl')
def cb_set_play(c, a):
    sid, st = set_by_arg(a[0] if a else '')
    if not st or not st['qs']:
        return ack(c, 'সেটটিতে কোনো প্রশ্ন নেই', True)
    ack(c)
    uid = c.from_user.id
    CFG[('p', uid)] = {'src': 's%d' % sid, 'n': 10, 't': 0, 'owner': uid}
    t, k = cfg_view(CFG[('p', uid)], 'p')
    edit(c, t, k)


@route('qa')
def cb_set_actions(c, a):
    uid = c.from_user.id
    if len(a) < 2:
        return ack(c)
    sid, st = set_by_arg(a[1])
    if not st or st['owner'] != uid:
        return ack(c, 'সেটটি পাওয়া যায়নি', True)
    act = a[0]
    if act == 'ds':
        return edit(c, '🗑 <b>"%s" সেটটি ডিলিট করবেন?</b>\nসব প্রশ্ন ও লিডারবোর্ড মুছে যাবে।' % esc(st['name']), kb([btn('✅ হ্যাঁ, ডিলিট', 'qa:dy:%s' % sid), btn('✖ না', 'qv:%s' % sid)]))
    if act == 'dy':
        DB.d['sets'].pop(str(sid), None)
        DB.save()
        ack(c, '🗑 ডিলিট হয়েছে')
        t, k = sets_view(uid)
        return edit(c, t, k)
    if act == 'l':
        ack(c)
        page = to_int(a[2], 0) if len(a) > 2 else 0
        items = list(enumerate(st['qs']))
        chunk, page, pages = paginate(items, page, 5)
        rows = [[btn('🗑 %d. %s' % (i + 1, q['q'][:30]), 'qa:dq:%s:%d:%d' % (sid, i, page))] for i, q in chunk]
        pg = pager('qa:l:%s' % sid, page, pages)
        if pg:
            rows.append(pg)
        rows.append(nav('qv:%s' % sid))
        return edit(c, '📋 <b>%s — প্রশ্নসমূহ</b>\nডিলিট করতে চাপুন।' % esc(st['name']), kb(*rows) if chunk else kb(nav('qv:%s' % sid)))
    if act == 'dq':
        i, page = to_int(a[2], -1), to_int(a[3], 0) if len(a) > 3 else 0
        if 0 <= i < len(st['qs']):
            st['qs'].pop(i)
            DB.save()
            ack(c, '🗑 প্রশ্ন মুছে গেছে')
        return cb_set_actions(c, ['l', str(sid), str(page)])


# ---------------------------------------------------------------- builder
@route('qb')
def cb_build(c, a):
    uid = c.from_user.id
    act = a[0] if a else ''
    if act == 'new':
        if len(my_sets(uid)) >= MAX_SETS:
            return ack(c, 'সর্বোচ্চ %d টি সেট' % MAX_SETS, True)
        ack(c)
        set_state(uid, 'sname')
        return edit(c, '📚 <b>নতুন সেট</b>\n\nসেটের নাম লিখুন (যেমন: <i>আমার ইংরেজি কুইজ</i>):', kb(nav('qm')))
    sid, st = set_by_arg(a[1] if len(a) > 1 else '')
    if act in ('q', 'bulk', 'ren', 'more'):
        if not st or st['owner'] != uid:
            return ack(c, 'সেটটি পাওয়া যায়নি', True)
        ack(c)
        if act in ('q', 'more'):
            if len(st['qs']) >= MAX_QS:
                return ack(c, 'সর্বোচ্চ %d টি প্রশ্ন' % MAX_QS, True)
            set_state(uid, 'bq', sid=sid)
            return edit(c, '➕ <b>প্রশ্ন যোগ — "%s"</b>\n\n❓ প্রশ্নটি লিখুন:' % esc(st['name']), kb(nav('qv:%d' % sid)))
        if act == 'bulk':
            set_state(uid, 'bbulk', sid=sid)
            return edit(c, '📥 <b>Bulk Import</b>\n\nপ্রতি লাইনে একটি প্রশ্ন:\n<code>প্রশ্ন | সঠিক উত্তর | ভুল ১ | ভুল ২ // ব্যাখ্যা</code>\n\n(ব্যাখ্যা ঐচ্ছিক, ভুল অপশন ১–৯টি)', kb(nav('qv:%d' % sid)))
        set_state(uid, 'bren', sid=sid)
        return edit(c, '✏️ নতুন নাম লিখুন:', kb(nav('qv:%d' % sid)))
    s = get_state(uid)
    if act == 'done':
        if not s or s['k'] != 'bw':
            return ack(c, 'সেশন শেষ', True)
        if not s['d']['w']:
            return ack(c, 'কমপক্ষে ১টি ভুল অপশন দিন', True)
        ack(c)
        s['k'] = 'bex'
        return edit(c, '💡 <b>ব্যাখ্যা</b> (ঐচ্ছিক) — উত্তরের পর দেখানো হবে। লিখুন, অথবা স্কিপ করুন:', kb([btn('⏭ Skip', 'qb:skip')], nav()))
    if act == 'skip':
        if not s or s['k'] != 'bex':
            return ack(c, 'সেশন শেষ', True)
        ack(c)
        return save_question(c.message.chat.id, uid, s, '')


def save_question(chat_id, uid, s, ex):
    d = s['d']
    st = get_set(d['sid'])
    clear_state(uid)
    if not st or st['owner'] != uid:
        return say(chat_id, '⚠️ সেটটি পাওয়া যায়নি।')
    st['qs'].append({'q': d['q'][:300], 'a': d['a'][:100], 'w': [x[:100] for x in d['w'][:9]], 'ex': ex[:200]})
    DB.save()
    say(chat_id, '✅ প্রশ্ন #%d যোগ হয়েছে!' % len(st['qs']), kb([btn('➕ আরেকটি', 'qb:more:%d' % d['sid']), btn('✅ শেষ', 'qv:%d' % d['sid'])]))


def parse_bulk_line(line):
    ex = ''
    if '//' in line:
        line, _, ex = line.partition('//')
    bits = [b.strip() for b in line.split('|') if b.strip()]
    if len(bits) < 3 or len(bits) > 11:
        return None
    return {'q': bits[0][:300], 'a': bits[1][:100], 'w': [b[:100] for b in bits[2:11]], 'ex': ex.strip()[:200]}


# ---------------------------------------------------------------- commands
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    uid = m.from_user.id
    clear_state(uid)
    user(uid, m.from_user.first_name)
    arg = cmd_arg(m)
    if arg.startswith('q_'):
        sid, st = set_by_arg(arg[2:])
        if st:
            owner = DB.d['users'].get(str(st['owner']), {}).get('name', '')
            t, k = set_card(sid, st, uid)
            return say(m.chat.id, '📨 <b>আপনাকে একটি কুইজ সেটে আমন্ত্রণ!</b>' + ((' (by %s)' % esc(owner)) if owner else '') + '\n\n' + t, k)
    if m.chat.type != 'private':
        return say(m.chat.id, '🧠 <b>Quiz Bot</b>\nগ্রুপে কুইজ শুরু করতে: <code>/startquiz</code>')
    t, k = home_view(uid)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()) if m.chat.type == 'private' else None)


@bot.message_handler(commands=['quiz'])
def quiz_cmd(m):
    lines = [ln.strip() for ln in (m.text or '').split('\n') if ln.strip()]
    bodies = []
    for ln in lines:
        if ln.startswith('/quiz'):
            parts = ln.split(None, 1)
            bodies.append(parts[1] if len(parts) > 1 else '')
        elif bodies:
            bodies[-1] += ' ' + ln
    items = [parse_bulk_line(b) for b in bodies if b.count('|') >= 2]
    items = [i for i in items if i]
    if not items:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/quiz প্রশ্ন | সঠিক উত্তর | ভুল ১ | ভুল ২</code>\nএক মেসেজে একাধিক /quiz লাইনও দেওয়া যায়।', reply_to=m.message_id)
    sent, err = 0, ''
    for q in items[:10]:
        opts, ci = shuffled(q)
        try:
            kw = {'type': 'quiz', 'correct_option_id': ci, 'is_anonymous': False}
            if q['ex']:
                kw['explanation'] = q['ex']
            bot.send_poll(m.chat.id, q['q'][:300], opts, **kw)
            sent += 1
        except Exception as e:
            err = str(e)[:120]
    if err:
        say(m.chat.id, '✅ %d টা কুইজ পাঠানো হয়েছে।\n⚠️ কিছু ব্যর্থ: %s' % (sent, esc(err)))


@bot.message_handler(func=lambda m: m.chat.type == 'private' and bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = m.text.strip()
    if not s:
        t, k = home_view(uid)
        return say(m.chat.id, '💡 বাটন থেকে বেছে নিন:\n\n' + t, k)
    key, d = s['k'], s['d']
    if key == 'sname':
        with DB.lock:
            sid = DB.d['next']
            DB.d['next'] += 1
            DB.d['sets'][str(sid)] = {'owner': uid, 'name': txt[:40], 'qs': [], 'plays': 0, 'lb': {}}
            DB.save()
        clear_state(uid)
        return say(m.chat.id, '✅ সেট তৈরি হয়েছে: <b>%s</b>\nএখন প্রশ্ন যোগ করুন।' % esc(txt[:40]), kb([btn('➕ প্রশ্ন যোগ', 'qb:q:%d' % sid), btn('📥 Bulk Import', 'qb:bulk:%d' % sid)], [btn('📚 সেট দেখুন', 'qv:%d' % sid)]))
    if key == 'bren':
        st = get_set(d['sid'])
        clear_state(uid)
        if st and st['owner'] == uid:
            st['name'] = txt[:40]
            DB.save()
        return say(m.chat.id, '✅ নাম বদলানো হয়েছে।', kb([btn('📚 সেট দেখুন', 'qv:%d' % d['sid'])]))
    if key == 'bq':
        d['q'] = txt[:300]
        s['k'] = 'ba'
        return say(m.chat.id, '✅ <b>সঠিক উত্তর</b> লিখুন:', kb(nav()))
    if key == 'ba':
        d['a'], d['w'] = txt[:100], []
        s['k'] = 'bw'
        return say(m.chat.id, '❌ <b>ভুল অপশন</b> পাঠান (প্রতি লাইনে একটি; ১–৯টি)।\nশেষ হলে ✅ Done চাপুন।', kb([btn('✅ Done', 'qb:done')], nav()))
    if key == 'bw':
        for line in [x.strip() for x in txt.splitlines() if x.strip()]:
            if len(d['w']) < 9:
                d['w'].append(line[:100])
        return say(m.chat.id, '❌ ভুল অপশন: %d টি\n%s' % (len(d['w']), '\n'.join('• ' + esc(x) for x in d['w'])), kb([btn('✅ Done', 'qb:done')], nav()))
    if key == 'bex':
        return save_question(m.chat.id, uid, s, txt)
    if key == 'bbulk':
        st = get_set(d['sid'])
        if not st or st['owner'] != uid:
            clear_state(uid)
            return say(m.chat.id, '⚠️ সেটটি পাওয়া যায়নি।')
        ok, bad = 0, 0
        for line in [x for x in txt.splitlines() if x.strip()]:
            q = parse_bulk_line(line)
            if q and len(st['qs']) < MAX_QS:
                st['qs'].append(q)
                ok += 1
            else:
                bad += 1
        DB.save()
        clear_state(uid)
        return say(m.chat.id, '📥 <b>ইম্পোর্ট শেষ</b>\n✅ যোগ হয়েছে: %d\n⚠️ বাদ গেছে: %d (ফরম্যাট ভুল বা সীমা পূর্ণ)\n📚 মোট: %d' % (ok, bad, len(st['qs'])), kb([btn('📚 সেট দেখুন', 'qv:%d' % d['sid'])]))
    say(m.chat.id, 'ℹ️ বাটন ব্যবহার করুন, অথবা /cancel দিয়ে বাতিল করুন।')


run(commands=[('start', 'মেনু খুলুন'), ('quiz', 'এখনই কুইজ পোল'), ('startquiz', 'গ্রুপে কুইজ শুরু'), ('stopquiz', 'গ্রুপ কুইজ থামান'),
              ('leaderboard', 'লিডারবোর্ড'), ('mystats', 'আমার পরিসংখ্যান'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')],
    allowed=['message', 'callback_query', 'poll_answer', 'my_chat_member'])
''',
    },
    "url_shortener": {
        "emoji": '✂️',
        "name": 'URL Shortener Bot',
        "proj_name": 'URL Shortener',
        "category": '🧰 Utility',
        "level": '⭐⭐ Intermediate',
        "setup": '~20 sec',
        "features": ['লিংক পাঠালেই শর্ট লিংক — একসাথে ৫টা পর্যন্ত', '৩টি সার্ভিস (is.gd, v.gd প্রিভিউ-সেফ, TinyURL) — একটা ডাউন থাকলে অটো ফলব্যাক', '✏️ কাস্টম অ্যালিয়াস, 📷 QR, 📤 Share ও 📊 Click Stats বাটন', '🔎 Expand — শর্ট লিংক কোথায় যায় দেখুন (প্রাইভেট নেটওয়ার্ক ব্লক করা, SSRF-সেফ)', '🕘 My Links — ৫০টি পর্যন্ত লিংক ম্যানেজ (দেখা, অন্য প্রোভাইডার, ডিলিট)'],
        "commands": '/short <url> [নাম] · /expand <url> · /mylinks',
        "about": 'শর্ট লিংক, কাস্টম নাম, QR, ক্লিক-স্ট্যাটস পেজ, লিংক Expand ও পুরো History ম্যানেজমেন্টসহ বাটন-চালিত URL শর্টনার।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', 'যেকোনো লম্বা লিংক পাঠান — সাথে সাথে শর্ট হবে', 'রেজাল্টের নিচের বাটন থেকে অ্যালিয়াস/QR/Expand/অন্য প্রোভাইডার বেছে নিন', '🛰 Provider থেকে ডিফল্ট সার্ভিস বদলান', '🕘 My Links থেকে আগের লিংক ম্যানেজ করুন'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 https://example.com/a/very/long/path?x=1\n🤖 🔗 শর্ট লিংক: https://is.gd/abc123',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)

import socket, ipaddress

DB = Store('short_links.json', {'users': {}, 'next': 1})
PROVS = {
    'isgd': ('is.gd', 'https://is.gd/create.php'),
    'vgd': ('v.gd (প্রিভিউ সেফ)', 'https://v.gd/create.php'),
    'tiny': ('TinyURL', 'https://tinyurl.com/api-create.php'),
}
PROV_ORDER = ['isgd', 'vgd', 'tiny']
URL_RE = re.compile(r'(?:https?://|www\.)[^\s<>"]+', re.I)
ALIAS_RE = re.compile(r'^[A-Za-z0-9_]{5,30}$')
HIST_MAX = 50
COMMON_TLD = {'com', 'net', 'org', 'io', 'me', 'co', 'app', 'dev', 'xyz', 'info', 'bd', 'in', 'uk', 'us', 'link', 'ly', 'gl', 'biz', 'tv', 'edu', 'gov', 'online', 'site', 'shop', 'store', 'news', 'ai'}


def ucfg(uid):
    u = DB.d['users'].setdefault(str(uid), {})
    u.setdefault('prov', 'isgd')
    u.setdefault('hist', [])
    return u


def norm_url(s):
    s = (s or '').strip().strip('<>').rstrip('.,;)')
    if not s:
        return None
    if not re.match(r'^[a-z][a-z0-9+.\-]*://', s, re.I):
        if re.match(r'^(?:www\.)?[\w\-]+(?:\.[\w\-]+)*\.[a-z]{2,}(?:[/?#]\S*)?$', s, re.I):
            s = 'https://' + s
        else:
            return None
    if not s.lower().startswith(('http://', 'https://')):
        return None
    host = urllib.parse.urlparse(s).hostname
    if not host or '.' not in host:
        return None
    return s[:2000]


def domain_of(u):
    try:
        return (urllib.parse.urlparse(u).hostname or '')[:50]
    except Exception:
        return ''


def shorten_one(url, prov, alias=None):
    if prov in ('isgd', 'vgd'):
        params = {'format': 'json', 'url': url, 'logstats': 1}
        if alias:
            params['shorturl'] = alias
        status, j = http_json(PROVS[prov][1], params=params, timeout=15)
        if isinstance(j, dict) and j.get('shorturl'):
            return True, j['shorturl']
        return False, str((j or {}).get('errormessage') or 'HTTP %s' % status)[:160]
    status, body = http(PROVS['tiny'][1], params={'url': url}, timeout=15)
    t = body.decode('utf-8', 'replace').strip()
    if status == 200 and t.startswith('http'):
        return True, t
    return False, (t[:150] or 'HTTP %s' % status)


def shorten(url, prov, alias=None):
    order = [prov] + [p for p in PROV_ORDER if p != prov]
    if alias:
        order = [p for p in order if p != 'tiny']
    last = 'unknown'
    for p in order:
        try:
            ok, res = shorten_one(url, p, alias)
        except Exception as e:
            ok, res = False, 'network: %s' % type(e).__name__
        if ok:
            return True, res, p
        last = res
        if alias and 'already' in res.lower():
            break
    return False, last, prov


def add_hist(uid, long_url, short, prov):
    u = ucfg(uid)
    with DB.lock:
        lid = DB.d['next']
        DB.d['next'] += 1
        u['hist'].insert(0, {'id': lid, 'long': long_url, 'short': short, 'prov': prov, 't': int(time.time())})
        del u['hist'][HIST_MAX:]
        DB.save()
    return lid


def find(uid, lid):
    for h in ucfg(uid)['hist']:
        if h['id'] == lid:
            return h
    return None


def card(h):
    warn = '\n⚠️ <i>এই লিংকটি HTTPS নয় (অনিরাপদ হতে পারে)</i>' if h['long'].lower().startswith('http://') else ''
    return ('✂️ <b>শর্ট লিংক তৈরি!</b>\n' + LINE + '\n🔗 <code>%s</code>\n🌐 মূল: <b>%s</b>\n🛰 Provider: %s\n🕒 %s%s%s'
            % (esc(h['short']), esc(domain_of(h['long'])), PROVS.get(h['prov'], ('?',))[0], fmt_ts(h['t'], 6, '%d %b, %H:%M'), warn, FOOTER))


def card_kb(h, back=None):
    code = h['short'].rstrip('/').rsplit('/', 1)[-1]
    row1 = [btn('📤 Share', url='https://t.me/share/url?url=' + urllib.parse.quote(h['short'])), btn('📷 QR', 'uq:%d' % h['id'])]
    row2 = [btn('🔎 Expand', 'ux:%d' % h['id']), btn('✏️ Custom Alias', 'ua:%d' % h['id'])]
    row3 = [btn('🔄 অন্য Provider', 'uo:%d' % h['id']), btn('🗑 Remove', 'ud:%d' % h['id'])]
    rows = [row1, row2, row3]
    if h['prov'] in ('isgd', 'vgd'):
        host = 'is.gd' if h['prov'] == 'isgd' else 'v.gd'
        rows.insert(1, [btn('📊 Click Stats', url='https://%s/stats.php?url=%s' % (host, urllib.parse.quote(code)))])
    rows.append(nav(back))
    return kb(*rows)


def home_view(uid):
    u = ucfg(uid)
    text = ('✂️ <b>URL Shortener</b>\n' + LINE + '\n'
            '• যেকোনো লম্বা লিংক পাঠান — সাথে সাথে শর্ট হবে\n'
            '• একসাথে ৫টা পর্যন্ত লিংক দেওয়া যায়\n'
            '• কাস্টম নাম, QR, লিংক Expand ও History আছে\n\n'
            '🛰 বর্তমান Provider: <b>%s</b>' % PROVS[u['prov']][0] + FOOTER)
    return text, kb([btn('✂️ লিংক শর্ট করুন', 'us'), btn('🔎 লিংক Expand', 'ue')],
                    [btn('🕘 My Links (%d)' % len(u['hist']), 'uh'), btn('🛰 Provider', 'up')],
                    [btn('❓ Help', 'uhelp')])


HELP = ('❓ <b>URL Shortener — সাহায্য</b>\n' + LINE + '\n'
        '• লিংক পাঠান → শর্ট হবে (একাধিক লিংক হলে সর্বোচ্চ ৫টা)\n'
        '• <code>/short https://example.com mylink</code> — কাস্টম নামসহ\n'
        '• <code>/expand লিংক</code> — শর্ট লিংক কোথায় যায় দেখুন\n'
        '• <code>/mylinks</code> — আপনার তৈরি লিংকগুলো\n\n'
        '🛰 <b>Provider:</b> is.gd (ক্লিক স্ট্যাটসসহ), v.gd (খোলার আগে প্রিভিউ পেজ — বেশি নিরাপদ), TinyURL।\n'
        'একটি সার্ভিস ডাউন থাকলে বট অটো অন্যটি ব্যবহার করে।\n'
        '🔒 Expand শুধু পাবলিক ওয়েবসাইটে কাজ করে।')


def do_shorten(chat_id, uid, urls):
    u = ucfg(uid)
    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass
    results = []
    for url in urls[:5]:
        ok, res, prov = shorten(url, u['prov'])
        if ok:
            results.append((url, res, prov, add_hist(uid, url, res, prov)))
        else:
            results.append((url, res, prov, None))
    if len(results) == 1:
        url, res, prov, lid = results[0]
        if lid is None:
            return say(chat_id, '⚠️ শর্ট করা যায়নি: <i>%s</i>\n\nলিংকটি সঠিক কিনা দেখুন বা একটু পরে আবার চেষ্টা করুন।' % esc(res), kb(nav()))
        return say(chat_id, card(find(uid, lid)), card_kb(find(uid, lid)))
    lines = ['✂️ <b>%d টি লিংক প্রসেস হয়েছে</b>\n' % len(results)]
    for url, res, prov, lid in results:
        lines.append(('✅ <code>%s</code>\n    ↳ %s' % (esc(res), esc(domain_of(url)))) if lid else '⚠️ %s → %s' % (esc(url[:40]), esc(res[:60])))
    say(chat_id, '\n'.join(lines) + FOOTER, kb([btn('🕘 My Links', 'uh'), btn('🏠 Home', 'home')]))


@route('us')
def cb_shorten(c, a):
    ack(c)
    set_state(c.from_user.id, 'url')
    edit(c, '✂️ <b>লিংক পাঠান</b>\n\nলম্বা লিংকটি পেস্ট করুন (একাধিক লিংক দিলে সর্বোচ্চ ৫টা)।', kb(nav()))


@route('ue')
def cb_expand_prompt(c, a):
    ack(c)
    set_state(c.from_user.id, 'expand')
    edit(c, '🔎 <b>Expand</b>\n\nযে শর্ট লিংকটি কোথায় যায় জানতে চান সেটি পাঠান।', kb(nav()))


@route('uhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


@route('up')
def cb_prov(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    if len(a) >= 2 and a[0] == 'set' and a[1] in PROVS:
        u['prov'] = a[1]
        DB.save()
        ack(c, '✅ Provider বদলানো হয়েছে')
    rows = [[btn(('✅ ' if u['prov'] == k else '') + PROVS[k][0], 'up:set:' + k)] for k in PROV_ORDER]
    rows.append(nav())
    edit(c, '🛰 <b>Provider বেছে নিন</b>\n' + LINE + '\n• <b>is.gd</b> — দ্রুত, ক্লিক স্ট্যাটস পেজ\n• <b>v.gd</b> — খোলার আগে প্রিভিউ/সতর্কতা পেজ\n• <b>TinyURL</b> — আলাদা সার্ভিস (ব্যাকআপ)', kb(*rows))


@route('uh')
def cb_hist(c, a):
    uid = c.from_user.id
    u = ucfg(uid)
    if not u['hist']:
        return edit(c, '🕘 <b>My Links</b>\n\nএখনো কোনো লিংক বানাননি।', kb([btn('✂️ লিংক শর্ট করুন', 'us')], nav()))
    page = to_int(a[0], 0) if a else 0
    items, page, pages = paginate(u['hist'], page, 6)
    rows = [[btn('🔗 %s — %s' % (h['short'].replace('https://', '').replace('http://', '')[:24], domain_of(h['long'])[:18]), 'uv:%d:%d' % (h['id'], page))] for h in items]
    pg = pager('uh', page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav())
    edit(c, '🕘 <b>আপনার লিংক (%d)</b>\nদেখতে বা ম্যানেজ করতে চাপ দিন।' % len(u['hist']), kb(*rows))


@route('uv')
def cb_view(c, a):
    h = find(c.from_user.id, to_int(a[0] if a else '', -1))
    if not h:
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    page = a[1] if len(a) > 1 else '0'
    edit(c, card(h), card_kb(h, 'uh:' + page))


@route('ux')
def cb_expand(c, a):
    h = find(c.from_user.id, to_int(a[0] if a else '', -1))
    if not h:
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    ack(c, '🔎 চেক করা হচ্ছে...')
    send_expand(c.message.chat.id, h['short'])


@route('uq')
def cb_qr(c, a):
    h = find(c.from_user.id, to_int(a[0] if a else '', -1))
    if not h:
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    ack(c, '⏳ QR বানানো হচ্ছে...')
    try:
        status, body = http('https://api.qrserver.com/v1/create-qr-code/', params={'size': '500x500', 'margin': 10, 'data': h['short']}, timeout=20)
    except Exception:
        status, body = 0, b''
    if status != 200 or not body:
        return say(c.message.chat.id, '⚠️ QR বানানো যায়নি, একটু পরে চেষ্টা করুন।')
    bio = io.BytesIO(body)
    bio.name = 'qr.png'
    _call(bot.send_photo, c.message.chat.id, bio, caption='📷 <b>QR:</b> <code>%s</code>' % esc(h['short']), parse_mode='HTML')


@route('uo')
def cb_other(c, a):
    uid = c.from_user.id
    h = find(uid, to_int(a[0] if a else '', -1))
    if not h:
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    nxt = PROV_ORDER[(PROV_ORDER.index(h['prov']) + 1) % len(PROV_ORDER)] if h['prov'] in PROV_ORDER else 'isgd'
    ack(c, '⏳ %s দিয়ে বানানো হচ্ছে...' % PROVS[nxt][0])
    try:
        ok, res = shorten_one(h['long'], nxt)
    except Exception:
        ok, res = False, 'network'
    if not ok:
        return say(c.message.chat.id, '⚠️ %s দিয়ে হয়নি: <i>%s</i>' % (PROVS[nxt][0], esc(res)))
    lid = add_hist(uid, h['long'], res, nxt)
    say(c.message.chat.id, card(find(uid, lid)), card_kb(find(uid, lid)))


@route('ua')
def cb_alias(c, a):
    h = find(c.from_user.id, to_int(a[0] if a else '', -1))
    if not h:
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    ack(c)
    set_state(c.from_user.id, 'alias', lid=h['id'])
    say(c.message.chat.id, '✏️ <b>কাস্টম নাম লিখুন</b>\n৫–৩০ অক্ষর; শুধু <code>a-z A-Z 0-9 _</code>\nযেমন: <code>my_offer</code>\n\n(বাতিল: /cancel)', kb(nav()))


@route('ud')
def cb_del(c, a):
    uid = c.from_user.id
    lid = to_int(a[0] if a else '', -1)
    u = ucfg(uid)
    before = len(u['hist'])
    u['hist'] = [h for h in u['hist'] if h['id'] != lid]
    DB.save()
    ack(c, '🗑 History থেকে সরানো হয়েছে' if len(u['hist']) < before else 'আগেই সরানো হয়েছে')
    edit(c, '🗑 <b>লিংকটি আপনার History থেকে সরানো হয়েছে।</b>\n<i>(শর্ট লিংকটি নিজে থেকে অনলাইনে সচল থাকবে।)</i>', kb([btn('🕘 My Links', 'uh'), btn('🏠 Home', 'home')]))


# ---------------------------------------------------------------- expand
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for i in infos:
        try:
            ip = ipaddress.ip_address(i[4][0].split('%')[0])
        except Exception:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def expand(url, hops=8):
    chain = [url]
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(hops):
        host = urllib.parse.urlparse(chain[-1]).hostname or ''
        if not public_host(host):
            return chain, 'blocked'
        req = urllib.request.Request(chain[-1], headers={'User-Agent': UA}, method='HEAD')
        try:
            r = opener.open(req, timeout=10)
            code, loc = r.status, r.headers.get('Location')
        except urllib.error.HTTPError as e:
            code, loc = e.code, e.headers.get('Location')
        except Exception:
            return chain, 'error'
        if code in (301, 302, 303, 307, 308) and loc:
            nxt = urllib.parse.urljoin(chain[-1], loc)
            if not nxt.lower().startswith(('http://', 'https://')):
                return chain, 'ok'
            chain.append(nxt)
            continue
        return chain, 'ok'
    return chain, 'many'


def send_expand(chat_id, url):
    url = norm_url(url)
    if not url:
        return say(chat_id, '⚠️ সঠিক লিংক দিন।', kb(nav()))
    chain, st = expand(url)
    if st == 'blocked':
        return say(chat_id, '🚫 এই ঠিকানা বা তার রিডাইরেক্ট প্রাইভেট/অভ্যন্তরীণ নেটওয়ার্কের — নিরাপত্তার কারণে চেক করা হয়নি।', kb(nav()))
    if st == 'error' and len(chain) == 1:
        return say(chat_id, '⚠️ লিংকটিতে পৌঁছানো যায়নি (সাইট বন্ধ বা ভুল ঠিকানা হতে পারে)।', kb(nav()))
    lines = ['🔎 <b>লিংক Expand</b>\n' + LINE]
    for i, u in enumerate(chain[:8], 1):
        lines.append('%d. <code>%s</code>' % (i, esc(u[:150])))
    final = chain[-1]
    lines.append('\n🎯 <b>গন্তব্য:</b> %s' % esc(domain_of(final)))
    if len(chain) == 1:
        lines.append('ℹ️ এই লিংকে কোনো রিডাইরেক্ট নেই।')
    if final.lower().startswith('http://'):
        lines.append('⚠️ গন্তব্য HTTPS নয় — পাসওয়ার্ড/পেমেন্ট তথ্য দেবেন না।')
    if re.fullmatch(r'[\d.]+', domain_of(final)) or 'xn--' in domain_of(final):
        lines.append('⚠️ সন্দেহজনক ডোমেইন (IP বা punycode) — সতর্ক থাকুন।')
    if st == 'many':
        lines.append('⚠️ অনেকগুলো রিডাইরেক্ট — শেষ পর্যন্ত যাওয়া হয়নি।')
    say(chat_id, '\n'.join(lines), kb([btn('✂️ শর্ট করুন', 'us'), btn('🏠 Home', 'home')]))


# ---------------------------------------------------------------- commands & messages
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    clear_state(m.from_user.id)
    t, k = home_view(m.from_user.id)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()))


@bot.message_handler(commands=['short'])
def short_cmd(m):
    parts = cmd_arg(m).split()
    url = norm_url(parts[0]) if parts else None
    if not url:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/short https://example.com mylink</code>', kb(nav()))
    alias = parts[1] if len(parts) > 1 else None
    if alias:
        if not ALIAS_RE.match(alias):
            return say(m.chat.id, '⚠️ কাস্টম নাম ৫–৩০ অক্ষরের হতে হবে; শুধু a-z A-Z 0-9 _')
        ok, res, prov = shorten(url, ucfg(m.from_user.id)['prov'], alias)
        if not ok:
            return say(m.chat.id, '⚠️ কাস্টম নামে বানানো যায়নি: <i>%s</i>' % esc(res))
        lid = add_hist(m.from_user.id, url, res, prov)
        return say(m.chat.id, card(find(m.from_user.id, lid)), card_kb(find(m.from_user.id, lid)))
    do_shorten(m.chat.id, m.from_user.id, [url])


@bot.message_handler(commands=['expand'])
def expand_cmd(m):
    arg = cmd_arg(m)
    if not arg:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/expand https://is.gd/abc</code>')
    send_expand(m.chat.id, arg.split()[0])


@bot.message_handler(commands=['mylinks'])
def mylinks_cmd(m):
    u = ucfg(m.from_user.id)
    if not u['hist']:
        return say(m.chat.id, '🕘 এখনো কোনো লিংক বানাননি।', kb([btn('✂️ লিংক শর্ট করুন', 'us')], nav()))
    lines = ['🕘 <b>আপনার শেষ লিংকগুলো</b>\n' + LINE]
    for h in u['hist'][:10]:
        lines.append('• <code>%s</code> — %s' % (esc(h['short']), esc(domain_of(h['long']))))
    say(m.chat.id, '\n'.join(lines), kb([btn('🕘 সব দেখুন', 'uh')], nav()))


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = m.text.strip()
    if s and s['k'] == 'alias':
        h = find(uid, s['d']['lid'])
        clear_state(uid)
        if not h:
            return say(m.chat.id, '⚠️ লিংকটি আর পাওয়া যাচ্ছে না।', kb(nav()))
        if not ALIAS_RE.match(txt):
            set_state(uid, 'alias', lid=h['id'])
            return say(m.chat.id, '⚠️ ৫–৩০ অক্ষর; শুধু <code>a-z A-Z 0-9 _</code> — আবার লিখুন (বাতিল: /cancel)')
        if not rate_ok(uid, 'short', 1.5):
            return say(m.chat.id, '⏳ একটু অপেক্ষা করুন।')
        prov = h['prov'] if h['prov'] in ('isgd', 'vgd') else 'isgd'
        ok, res, p = shorten(h['long'], prov, txt)
        if not ok:
            set_state(uid, 'alias', lid=h['id'])
            return say(m.chat.id, '⚠️ এই নামে হয়নি: <i>%s</i>\nঅন্য নাম লিখুন (বাতিল: /cancel)' % esc(res))
        lid = add_hist(uid, h['long'], res, p)
        return say(m.chat.id, card(find(uid, lid)), card_kb(find(uid, lid)))
    if s and s['k'] == 'expand':
        clear_state(uid)
        return send_expand(m.chat.id, txt.split()[0])
    urls = []
    for raw in URL_RE.findall(txt):
        n = norm_url(raw)
        if n and n not in urls:
            urls.append(n)
    if not urls and len(txt.split()) == 1:
        n = norm_url(txt)
        tld = (urllib.parse.urlparse(n).hostname or '').rsplit('.', 1)[-1].lower() if n else ''
        if n and ((s and s['k'] == 'url') or tld in COMMON_TLD):
            urls.append(n)
    if urls:
        clear_state(uid)
        if not rate_ok(uid, 'short', 1.5):
            return say(m.chat.id, '⏳ একটু অপেক্ষা করুন।')
        return do_shorten(m.chat.id, uid, urls)
    t, k = home_view(uid)
    say(m.chat.id, '⚠️ কোনো সঠিক লিংক পাইনি। লিংক (https://...) পাঠান।\n\n' + t, k)


run(commands=[('start', 'মেনু খুলুন'), ('short', 'লিংক শর্ট করুন'), ('expand', 'শর্ট লিংক Expand'),
              ('mylinks', 'আপনার লিংক'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
    "ai_chatbot": {
        "emoji": '🧠',
        "name": 'AI Chatbot (Gemini / OpenAI)',
        "proj_name": 'AI Chatbot',
        "category": '🧠 AI & Automation',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~40 sec',
        "features": ['Gemini (ফ্রি) বা OpenAI Key — মডেল অটো-সিলেক্ট', '🎭 Persona (শিক্ষক, কোডার, অনুবাদক, লেখক, কোচ...) ও 📏 উত্তরের ধরন', '📷 ছবি, 🎤 ভয়েস, 📄 PDF/টেক্সট ফাইল বিশ্লেষণ (Gemini-তে ভয়েস/PDF)', '🔄 প্রতিটি উত্তরের নিচে Regenerate / ছোট / বিস্তারিত বাটন + চ্যাট এক্সপোর্ট', 'শেষ ১০ প্রশ্ন মনে রাখে; গ্রুপে mention/reply-তে উত্তর', 'Owner: মডেল, দৈনিক লিমিট, ইউজার ব্লক, ব্রডকাস্ট, Key চেক'],
        "commands": '/ask প্রশ্ন · /clear · Owner: /model · /system · /stats',
        "about": 'AI সহকারী — ছবি/ভয়েস/ফাইল বোঝে, Persona ও উত্তরের ধরন বদলানো যায়, Regenerate বাটনসহ; Owner প্যানেলে লিমিট ও মডেল কন্ট্রোল।',
        "steps": ['আপনার AI API Key দিন (Gemini ফ্রি / OpenAI)', 'বট ডিপ্লয় হলে /start দিন — আপনি Owner হবেন', 'সরাসরি লিখুন, ছবি/ভয়েস/ফাইল পাঠান', '🎭 Persona ও 📏 উত্তরের ধরন বেছে নিন', 'Owner প্যানেলে দৈনিক লিমিট/মডেল সেট করুন'],
        "need": 'বট টোকেন + Gemini (ফ্রি) অথবা OpenAI API Key',
        "preview": '👤 বাংলাদেশের রাজধানী কোথায়?\n🤖 বাংলাদেশের রাজধানী ঢাকা।\n👤 /system তুমি একজন বিনয়ী সেলস অ্যাসিস্ট্যান্ট\n🤖 ✅ নতুন নির্দেশনা সেট হয়েছে।',
        "inputs": [{'placeholder': '__AI_KEY__', 'pattern': '^(AIza[0-9A-Za-z_\\-]{30,60}|sk-[0-9A-Za-z_\\-]{20,250})$', 'prompt': '🔑 <b>এখন আপনার AI API Key দিন:</b>\n\n• <b>Gemini (ফ্রি):</b> aistudio.google.com/apikey থেকে নিন — <code>AIza...</code> দিয়ে শুরু\n• <b>OpenAI:</b> platform.openai.com/api-keys থেকে নিন — <code>sk-...</code> দিয়ে শুরু\n\n🔒 Key পাঠানোর সাথে সাথে আমি মেসেজটি চ্যাট থেকে মুছে ফেলব। Key শুধু আপনার বটের কোডেই থাকবে।', 'error': '❌ <b>Key-টি সঠিক ফরম্যাটে নেই।</b>\nGemini Key <code>AIza...</code> এবং OpenAI Key <code>sk-...</code> দিয়ে শুরু হয়। আবার পাঠান।'}],
        "code": r'''BOT_TOKEN = '__TOKEN__'
AI_KEY = '__AI_KEY__'
PARSE_MODE = None
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)

import base64

DB = Store('ai_data.json', {'owner': None, 'model': '', 'system': '', 'users': {}, 'messages': 0, 'limit': 0, 'groups': True})
PROVIDER = 'gemini' if AI_KEY.startswith('AIza') else 'openai'
GEMINI_PREF = ['gemini-3.5-flash', 'gemini-3.1-flash-lite', 'gemini-3-flash-preview', 'gemini-2.5-flash']
OPENAI_PREF = ['gpt-5-mini', 'gpt-4.1-mini', 'gpt-4o-mini']
BAD_WORDS = ('image', 'tts', 'live', 'audio', 'embedding', 'vision', 'realtime', 'transcribe', 'search', 'computer', 'robotics', 'moderation', 'instruct')
DEFAULT_SYSTEM = ('You are a friendly, helpful AI assistant inside Telegram. Always reply in the same language the user writes in '
                  '(Bangla, English, Hindi etc.). Keep answers clear and reasonably short unless the user asks for detail.')
PERSONAS = {
    'default': ('🤖 সাধারণ সহকারী', None),
    'teacher': ('👨‍🏫 শিক্ষক', 'You are a patient teacher. Explain step by step with simple examples and a short recap. Always reply in the same language the user writes in.'),
    'coder': ('💻 কোডার', 'You are an expert programmer. Give correct, minimal, well-commented code in markdown code blocks plus a brief explanation. Always reply in the same language the user writes in.'),
    'translator': ('🌐 অনুবাদক', 'You are a translator. If the user writes Bangla, translate to English; if the text is English or another language, translate to Bangla. Output only the translation unless asked otherwise.'),
    'writer': ('✍️ লেখক / এডিটর', 'You are a skilled writer and editor. Improve clarity, grammar and tone while keeping the meaning. Always reply in the same language the user writes in.'),
    'funny': ('😄 মজার বন্ধু', 'You are a witty, friendly buddy. Keep replies light with a touch of humor, but still genuinely helpful. Always reply in the same language the user writes in.'),
    'career': ('💼 ক্যারিয়ার কোচ', 'You are a career coach. Give practical, honest, actionable advice on jobs, CVs and interviews. Always reply in the same language the user writes in.'),
    'summ': ('📝 সামারাইজার', 'You summarize any text into concise bullet points with the key takeaways. Always reply in the same language as the text.'),
}
LENGTHS = {'short': ('✂️ সংক্ষিপ্ত', 'Answer in at most 3 short sentences.'), 'normal': ('⚖️ স্বাভাবিক', ''), 'detailed': ('📖 বিস্তারিত', 'Give a thorough, well-structured, detailed answer.')}
LIMITS = [0, 10, 20, 50, 100]
TEXT_EXT = ('.txt', '.md', '.py', '.js', '.json', '.csv', '.html', '.css', '.log', '.xml', '.yml', '.yaml', '.ini', '.c', '.cpp', '.java', '.sql', '.sh', '.php', '.ts')
MAX_TURNS, COOLDOWN = 10, 3
HIST, MODEL_CACHE, MODELS, LASTMSG = {}, {}, [], {}


class AIError(Exception):
    pass


def uinfo(uid, name=None):
    u = DB.d['users'].setdefault(str(uid), {'name': '', 'n': 0, 'day': '', 'today': 0, 'persona': 'default', 'len': 'normal', 'blocked': False})
    if name:
        u['name'] = name[:40]
    return u


def is_owner(uid):
    return DB.d.get('owner') == uid


# ---------------------------------------------------------------- AI backend
def list_models():
    if PROVIDER == 'gemini':
        st, j = http_json('https://generativelanguage.googleapis.com/v1beta/models?pageSize=200', headers={'x-goog-api-key': AI_KEY}, timeout=20)
        if st != 200 or not j:
            raise AIError(explain(st))
        return [m['name'].split('/', 1)[-1] for m in j.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
    st, j = http_json('https://api.openai.com/v1/models', headers={'Authorization': 'Bearer ' + AI_KEY}, timeout=20)
    if st != 200 or not j:
        raise AIError(explain(st))
    return [m['id'] for m in j.get('data', [])]


def auto_model():
    if 'm' in MODEL_CACHE:
        return MODEL_CACHE['m']
    try:
        avail = list_models()
    except Exception:
        return None
    pref = GEMINI_PREF if PROVIDER == 'gemini' else OPENAI_PREF
    for p in pref:
        if p in avail:
            MODEL_CACHE['m'] = p
            return p
    key = 'flash' if PROVIDER == 'gemini' else 'mini'
    cands = sorted(x for x in avail if key in x and x.startswith('gemini' if PROVIDER == 'gemini' else 'gpt') and not any(b in x for b in BAD_WORDS))
    if cands:
        MODEL_CACHE['m'] = cands[-1]
        return cands[-1]
    return None


def current_model():
    return DB.d.get('model') or auto_model()


def explain(code):
    if code in (400, 401, 403):
        return '🔑 API Key সঠিক নয় বা এই কাজের পারমিশন নেই। Owner-কে নতুন Key দিয়ে বটটি আবার ডেপ্লয় করতে বলুন।'
    if code == 429:
        return '⏳ API-এর ফ্রি লিমিট/কোটা শেষ। কিছুক্ষণ পর আবার চেষ্টা করুন।'
    if isinstance(code, int) and code >= 500:
        return '🛠️ AI সার্ভার এখন ব্যস্ত। একটু পর আবার চেষ্টা করুন।'
    return '❌ AI থেকে উত্তর আনা যায়নি (code %s)।' % code


def call_ai(history, system, media=None, retry=True):
    model = current_model()
    if not model:
        raise AIError('❌ কোনো উপযুক্ত AI মডেল পাওয়া যায়নি। Owner মডেল সেট করুন।')
    media = media or []
    if PROVIDER == 'gemini':
        contents = []
        for i, h in enumerate(history):
            parts = []
            if media and i == len(history) - 1 and h['role'] == 'user':
                parts += [{'inline_data': {'mime_type': mm['mime'], 'data': mm['b64']}} for mm in media]
            parts.append({'text': h['text']})
            contents.append({'role': 'user' if h['role'] == 'user' else 'model', 'parts': parts})
        st, j = http_json('https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % model, method='POST',
                          headers={'x-goog-api-key': AI_KEY, 'Content-Type': 'application/json'},
                          data=json.dumps({'systemInstruction': {'parts': [{'text': system}]}, 'contents': contents}).encode(), timeout=90)
    else:
        msgs = [{'role': 'system', 'content': system}]
        for i, h in enumerate(history):
            role = 'user' if h['role'] == 'user' else 'assistant'
            if media and i == len(history) - 1 and role == 'user':
                content = [{'type': 'text', 'text': h['text']}] + [{'type': 'image_url', 'image_url': {'url': 'data:%s;base64,%s' % (mm['mime'], mm['b64'])}} for mm in media]
                msgs.append({'role': role, 'content': content})
            else:
                msgs.append({'role': role, 'content': h['text']})
        st, j = http_json('https://api.openai.com/v1/chat/completions', method='POST', headers={'Authorization': 'Bearer ' + AI_KEY, 'Content-Type': 'application/json'},
                          data=json.dumps({'model': model, 'messages': msgs}).encode(), timeout=90)
    if st == 404 and retry and not DB.d.get('model'):
        MODEL_CACHE.clear()
        return call_ai(history, system, media, retry=False)
    if st != 200 or not isinstance(j, dict):
        raise AIError(explain(st))
    if PROVIDER == 'gemini':
        cands = j.get('candidates') or []
        if not cands:
            raise AIError('⚠️ এই প্রশ্নের উত্তর AI দিতে পারেনি (সেফটি ফিল্টার)। অন্যভাবে লিখে দেখুন।')
        parts = cands[0].get('content', {}).get('parts', [])
        text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
    else:
        ch = j.get('choices') or []
        text = (ch[0].get('message', {}).get('content') or '') if ch else ''
    text = text.strip()
    if not text:
        raise AIError('⚠️ AI খালি উত্তর দিয়েছে। আবার চেষ্টা করুন।')
    return text


def system_for(uid):
    u = uinfo(uid)
    base = PERSONAS[u['persona']][1] if u['persona'] != 'default' and u['persona'] in PERSONAS else (DB.d.get('system') or DEFAULT_SYSTEM)
    extra = LENGTHS.get(u['len'], LENGTHS['normal'])[1]
    return base + ((' ' + extra) if extra else '')


# ---------------------------------------------------------------- formatting
def md_to_html(t):
    blocks, inl = [], []

    def kb_(m_):
        blocks.append(m_.group(2))
        return '\x00B%d\x00' % (len(blocks) - 1)

    def ki_(m_):
        inl.append(m_.group(1))
        return '\x00I%d\x00' % (len(inl) - 1)
    t = re.sub(r'```(\w*)\n?(.*?)```', kb_, t, flags=re.S)
    t = re.sub(r'`([^`\n]+)`', ki_, t)
    t = esc(t)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t, flags=re.S)
    t = re.sub(r'(?m)^#{1,6}\s*(.+)$', r'<b>\1</b>', t)
    t = re.sub(r'(?m)^(\s*)[\*\-]\s+', r'\1• ', t)
    t = re.sub(r'\[([^\]\n]+)\]\((https?://[^)\s"]+)\)', r'<a href="\2">\1</a>', t)
    t = re.sub(r'\x00I(\d+)\x00', lambda m_: '<code>%s</code>' % esc(inl[int(m_.group(1))]), t)
    t = re.sub(r'\x00B(\d+)\x00', lambda m_: '<pre>%s</pre>' % esc(blocks[int(m_.group(1))].strip('\n')), t)
    return t


def split_text(text, n=3500):
    chunks, cur = [], ''
    for para in text.split('\n'):
        while len(para) > n:
            if cur:
                chunks.append(cur)
                cur = ''
            chunks.append(para[:n])
            para = para[n:]
        if len(cur) + len(para) + 1 > n:
            chunks.append(cur)
            cur = para
        else:
            cur = (cur + '\n' + para) if cur else para
    if cur:
        chunks.append(cur)
    out, carry = [], False
    for ch in chunks:
        if carry:
            ch = '```\n' + ch
            carry = False
        if ch.count('```') % 2:
            ch += '\n```'
            carry = True
        out.append(ch)
    return out or ['...']


def answer_kb():
    return kb([btn('🔄 Regenerate', 'ar:re'), btn('✂️ ছোট', 'ar:sh'), btn('📖 বিস্তারিত', 'ar:de')], [btn('🧹 নতুন চ্যাট', 'ac'), btn('📜 Export', 'ax')])


def send_ai(chat_id, text, reply_to=None, markup=None):
    chunks = split_text(text)
    for i, ch in enumerate(chunks):
        last = i == len(chunks) - 1
        res = None
        try:
            kw = {'reply_markup': markup if last else None, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
            if reply_to and i == 0:
                kw['reply_to_message_id'] = reply_to
            res = _call(bot.send_message, chat_id, md_to_html(ch)[:4096], **kw)
        except Exception:
            res = None
        if res is None:
            try:
                bot.send_message(chat_id, ch[:4096], reply_markup=markup if last else None)
            except Exception as e:
                log('send_ai', str(e)[:150])


# ---------------------------------------------------------------- answering
def check_user(m):
    uid = m.from_user.id
    u = uinfo(uid, uname_of(m.from_user))
    if u['blocked']:
        say(m.chat.id, '🚫 আপনি এই বট ব্যবহার করতে পারছেন না।')
        return None
    now_t = time.time()
    if now_t - LASTMSG.get(uid, 0) < COOLDOWN:
        say(m.chat.id, '⏳ একটু অপেক্ষা করুন, আগের উত্তর আসছে...', reply_to=m.message_id)
        return None
    lim = DB.d.get('limit', 0)
    today = fmt_ts(now_t, 6.0, '%Y-%m-%d')
    if u['day'] != today:
        u['day'], u['today'] = today, 0
    if lim and not is_owner(uid) and u['today'] >= lim:
        say(m.chat.id, '📊 আজকের লিমিট (%d টি প্রশ্ন) শেষ হয়েছে। কাল আবার চেষ্টা করুন।' % lim)
        return None
    LASTMSG[uid] = now_t
    return u


def run_ai(chat_id, uid, reply_to, hist, media=None):
    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass
    try:
        reply = call_ai(hist, system_for(uid), media)
    except AIError as e:
        return False, str(e)
    except Exception as e:
        log('ai network', repr(e)[:150])
        return False, '❌ নেটওয়ার্ক সমস্যা হয়েছে, আবার চেষ্টা করুন।'
    return True, reply


def answer(m, question, media=None, label=None):
    u = check_user(m)
    if u is None:
        return
    uid = m.from_user.id
    if media and PROVIDER != 'gemini' and any(not mm['mime'].startswith('image/') for mm in media):
        return say(m.chat.id, '⚠️ এই ধরনের ফাইল (PDF/অডিও) শুধু <b>Gemini Key</b>-তে সাপোর্ট করে। ছবি বা টেক্সট ফাইল পাঠান।')
    with DB.lock:
        u['n'] += 1
        u['today'] += 1
        DB.d['messages'] += 1
        DB.save()
    key = (m.chat.id, uid)
    hist = HIST.setdefault(key, [])
    hist.append({'role': 'user', 'text': (question or label or '')[:12000]})
    del hist[:-MAX_TURNS * 2]
    if len(HIST) > 2000:
        HIST.pop(next(iter(HIST)))
    ok, res = run_ai(m.chat.id, uid, m.message_id, hist, media)
    if not ok:
        hist.pop()
        return say(m.chat.id, res, reply_to=m.message_id)
    if label:
        hist[-1]['text'] = label[:600]
    hist.append({'role': 'model', 'text': res})
    del hist[:-MAX_TURNS * 2]
    send_ai(m.chat.id, res, m.message_id, answer_kb())


@route('ar')
def cb_regen(c, a):
    uid, chat = c.from_user.id, c.message.chat.id
    hist = HIST.get((chat, uid))
    if not hist:
        return ack(c, 'চ্যাট হিস্ট্রি নেই — নতুন প্রশ্ন করুন', True)
    if uinfo(uid)['blocked']:
        return ack(c, '🚫', True)
    if not rate_ok(uid, 'ai', COOLDOWN):
        return ack(c, '⏳ একটু অপেক্ষা করুন', True)
    act = a[0] if a else 're'
    if act == 're':
        if hist and hist[-1]['role'] == 'model':
            hist.pop()
    else:
        instr = 'Rewrite your previous answer much shorter (2-3 sentences).' if act == 'sh' else 'Expand your previous answer with more detail and structure.'
        hist.append({'role': 'user', 'text': instr})
    if not hist or hist[-1]['role'] != 'user':
        return ack(c, 'আগে একটি প্রশ্ন করুন', True)
    ack(c, '⏳ উত্তর তৈরি হচ্ছে...')
    ok, res = run_ai(chat, uid, None, hist)
    if not ok:
        if act != 're':
            hist.pop()
        return say(chat, res)
    hist.append({'role': 'model', 'text': res})
    del hist[:-MAX_TURNS * 2]
    send_ai(chat, res, None, answer_kb())


@route('ac')
def cb_clear(c, a):
    HIST.pop((c.message.chat.id, c.from_user.id), None)
    ack(c, '🧹 নতুন চ্যাট শুরু')
    say(c.message.chat.id, '🧹 <b>চ্যাট হিস্ট্রি মুছে ফেলা হয়েছে।</b> নতুন প্রশ্ন করুন!')


@route('ax')
def cb_export(c, a):
    hist = HIST.get((c.message.chat.id, c.from_user.id))
    if not hist:
        return ack(c, 'এক্সপোর্ট করার মতো চ্যাট নেই', True)
    ack(c)
    lines = ['AI Chat Export — %s\n' % fmt_ts(time.time(), 6.0, '%Y-%m-%d %H:%M')]
    for h in hist:
        lines.append(('You: ' if h['role'] == 'user' else 'AI: ') + h['text'] + '\n')
    bio = io.BytesIO('\n'.join(lines).encode('utf-8'))
    bio.name = 'chat.txt'
    _call(bot.send_document, c.message.chat.id, bio, caption='📜 আপনার চ্যাটের কপি')


# ---------------------------------------------------------------- user menus
def home_view(uid):
    u = uinfo(uid)
    d = DB.d
    text = ('🤖 <b>AI Assistant</b>\n' + LINE + '\nযেকোনো প্রশ্ন লিখুন — আমি উত্তর দেব। ছবি 📷, ভয়েস 🎤' + (', PDF 📄' if PROVIDER == 'gemini' else '') + ' বা টেক্সট ফাইলও পাঠাতে পারেন!\n\n'
            '🎭 Persona: <b>%s</b>\n📏 উত্তর: <b>%s</b>%s' % (PERSONAS[u['persona']][0], LENGTHS[u['len']][0], ('\n📊 দৈনিক লিমিট: <b>%d</b> (আজ ব্যবহার: %d)' % (d['limit'], u['today'])) if d.get('limit') and not is_owner(uid) else '') + FOOTER)
    rows = [[btn('🎭 Persona', 'ap'), btn('📏 উত্তরের ধরন', 'al')], [btn('🧹 নতুন চ্যাট', 'ac'), btn('📜 Export', 'ax')]]
    if is_owner(uid):
        rows.append([btn('🛠 Owner Panel', 'aow')])
    rows.append([btn('❓ Help', 'ahelp')])
    return text, kb(*rows)


HELP = ('❓ <b>AI Assistant — সাহায্য</b>\n' + LINE + '\n'
        '💬 সরাসরি লিখুন — আগের কথাও মনে রাখে (শেষ ১০ প্রশ্ন)\n'
        '📷 <b>ছবি</b> পাঠিয়ে ক্যাপশনে প্রশ্ন করুন (যেমন "এটা কী?", "এই লেখাটা অনুবাদ করো")\n'
        '🎤 <b>ভয়েস</b> পাঠান (Gemini Key-তে) • 📄 <b>PDF</b> (Gemini) • টেক্সট/কোড ফাইল (.txt .py .json ...)\n'
        '🎭 <b>Persona:</b> শিক্ষক, কোডার, অনুবাদক, লেখক, ক্যারিয়ার কোচ, সামারাইজার...\n'
        '📏 <b>উত্তরের ধরন:</b> সংক্ষিপ্ত / স্বাভাবিক / বিস্তারিত\n'
        '🔄 প্রতিটি উত্তরের নিচে Regenerate / ছোট / বিস্তারিত বাটন\n'
        '👥 গ্রুপে: বটকে @mention করুন বা তার মেসেজে রিপ্লাই দিন\n\n'
        '⌨️ /ask প্রশ্ন · /clear · Owner: /model · /system · /stats')


@route('ahelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


@route('ap')
def cb_persona(c, a):
    uid = c.from_user.id
    u = uinfo(uid)
    if a and a[0] in PERSONAS:
        u['persona'] = a[0]
        DB.save()
        HIST.pop((c.message.chat.id, uid), None)
        ack(c, '✅ %s' % PERSONAS[a[0]][0])
    else:
        ack(c)
    rows = [[btn(('✅ ' if u['persona'] == k else '') + v[0], 'ap:' + k)] for k, v in PERSONAS.items()]
    rows.append(nav())
    edit(c, '🎭 <b>Persona বেছে নিন</b>\n' + LINE + '\nবট কোন ভূমিকায় উত্তর দেবে তা ঠিক করুন। বদলালে নতুন চ্যাট শুরু হয়।', kb(*rows))


@route('al')
def cb_length(c, a):
    uid = c.from_user.id
    u = uinfo(uid)
    if a and a[0] in LENGTHS:
        u['len'] = a[0]
        DB.save()
        ack(c, '✅ %s' % LENGTHS[a[0]][0])
    else:
        ack(c)
    rows = [[btn(('✅ ' if u['len'] == k else '') + v[0], 'al:' + k)] for k, v in LENGTHS.items()]
    rows.append(nav())
    edit(c, '📏 <b>উত্তরের ধরন</b>\n' + LINE + '\nউত্তর কত বড় হবে বেছে নিন:', kb(*rows))


# ---------------------------------------------------------------- owner panel
def guard(c):
    if not is_owner(c.from_user.id):
        ack(c, '🚫 শুধু Owner', True)
        return False
    return True


def owner_view():
    d = DB.d
    text = ('🛠 <b>Owner Panel</b>\n' + LINE + '\n🤖 Provider: <b>%s</b>\n🧠 মডেল: <b>%s</b>\n🎭 গ্লোবাল নির্দেশনা: %s\n🚦 দৈনিক লিমিট: <b>%s</b>\n👥 গ্রুপে উত্তর: <b>%s</b>\n🚫 ব্লকড: <b>%d</b>'
            % (PROVIDER.upper(), esc(d['model'] or ('auto → %s' % (MODEL_CACHE.get('m') or 'N/A'))), 'কাস্টম' if d['system'] else 'ডিফল্ট', d['limit'] or 'আনলিমিটেড', 'ON' if d['groups'] else 'OFF',
               len([u for u in d['users'].values() if u['blocked']])))
    return text, kb([btn('🧠 Model', 'am'), btn('🎭 নির্দেশনা', 'asy')], [btn('🚦 লিমিট: %s' % (d['limit'] or '∞'), 'adl'), btn('👥 Groups: %s' % ('ON' if d['groups'] else 'OFF'), 'agr')],
                    [btn('🚫 Blocklist', 'abl'), btn('📢 Broadcast', 'abc')], [btn('🔑 Key Test', 'akt'), btn('📊 Stats', 'ast')], nav())


@route('aow')
def cb_owner(c, a):
    if not guard(c):
        return
    ack(c)
    clear_state(c.from_user.id)
    t, k = owner_view()
    edit(c, t, k)


@route('ast')
def cb_stats(c, a):
    if not guard(c):
        return
    ack(c)
    d = DB.d
    edit(c, '📊 <b>পরিসংখ্যান</b>\n' + LINE + '\n👥 মোট ইউজার: <b>%d</b>\n💬 মোট প্রশ্ন: <b>%d</b>\n🤖 Provider: %s\n🧠 মডেল: %s' % (len(d['users']), d['messages'], PROVIDER.upper(), esc(d['model'] or ('auto → %s' % (auto_model() or 'N/A')))), kb(nav('aow')))


@route('am')
def cb_model(c, a):
    if not guard(c):
        return
    d = DB.d
    if a and a[0] == 'auto':
        d['model'] = ''
        MODEL_CACHE.clear()
        DB.save()
        ack(c, '✅ অটো-সিলেক্ট')
    elif len(a) >= 2 and a[0] == 's':
        i = to_int(a[1], -1)
        if 0 <= i < len(MODELS):
            d['model'] = MODELS[i][:80]
            DB.save()
            ack(c, '✅ %s' % MODELS[i][:40])
    else:
        ack(c, '⏳ মডেল তালিকা আনা হচ্ছে...')
        try:
            names = list_models()
            MODELS[:] = sorted(n for n in names if not any(b in n for b in BAD_WORDS))[:60]
        except Exception as e:
            MODELS[:] = []
            return edit(c, '⚠️ মডেল তালিকা আনা যায়নি।\n%s' % esc(str(e)), kb([btn('🔄 আবার', 'am')], nav('aow')))
    page = to_int(a[2], 0) if len(a) > 2 and a[0] == 'p' else 0
    if a and a[0] == 'p':
        page = to_int(a[1], 0)
    chunk, page, pages = paginate(list(enumerate(MODELS)), page, 8)
    rows = [[btn(('✅ ' if d['model'] == n else '') + n[:40], 'am:s:%d' % i)] for i, n in chunk]
    pg = pager('am:p', page, pages)
    if pg:
        rows.append(pg)
    rows.append([btn('♻️ Auto', 'am:auto')])
    rows.append(nav('aow'))
    edit(c, '🧠 <b>মডেল</b>\nবর্তমান: <b>%s</b>' % esc(d['model'] or ('auto → %s' % (MODEL_CACHE.get('m') or auto_model() or 'N/A'))), kb(*rows))


@route('asy')
def cb_system(c, a):
    if not guard(c):
        return
    if a and a[0] == 'reset':
        DB.d['system'] = ''
        DB.save()
        HIST.clear()
        ack(c, '♻️ ডিফল্টে ফেরানো হয়েছে')
    elif a and a[0] == 'set':
        ack(c)
        set_state(c.from_user.id, 'system')
        return edit(c, '✏️ <b>নতুন নির্দেশনা লিখুন</b> (বটের ব্যক্তিত্ব/নিয়ম, সর্বোচ্চ ১৫০০ অক্ষর):', kb(nav('asy')))
    else:
        ack(c)
    edit(c, '🎭 <b>গ্লোবাল নির্দেশনা</b>\n' + LINE + '\n%s\n\n<i>(Persona বাছা ইউজারদের জন্য Persona-র নির্দেশনা প্রযোজ্য)</i>' % esc(DB.d['system'] or DEFAULT_SYSTEM), kb([btn('✏️ বদলান', 'asy:set'), btn('♻️ রিসেট', 'asy:reset')], nav('aow')))


@route('adl')
def cb_limit(c, a):
    if not guard(c):
        return
    cur = DB.d['limit']
    DB.d['limit'] = LIMITS[(LIMITS.index(cur) + 1) % len(LIMITS)] if cur in LIMITS else 0
    DB.save()
    ack(c, 'দৈনিক লিমিট: %s' % (DB.d['limit'] or 'আনলিমিটেড'))
    t, k = owner_view()
    edit(c, t, k)


@route('agr')
def cb_groups(c, a):
    if not guard(c):
        return
    DB.d['groups'] = not DB.d['groups']
    DB.save()
    ack(c, 'গ্রুপ সাপোর্ট %s' % ('চালু' if DB.d['groups'] else 'বন্ধ'))
    t, k = owner_view()
    edit(c, t, k)


@route('abl')
def cb_block(c, a):
    if not guard(c):
        return
    if len(a) >= 2 and a[0] == 'x':
        u = DB.d['users'].get(a[1])
        if u:
            u['blocked'] = False
            DB.save()
        ack(c, '♻️ আনব্লক')
    elif a and a[0] == 'add':
        ack(c)
        set_state(c.from_user.id, 'block_add')
        return edit(c, '🚫 <b>ব্লক করতে ইউজার আইডি লিখুন</b> (সংখ্যা):', kb(nav('abl')))
    else:
        ack(c)
    bl = [(k, u) for k, u in DB.d['users'].items() if u['blocked']]
    rows = [[btn('♻️ %s (%s)' % (u['name'] or k, k), 'abl:x:%s' % k)] for k, u in bl[:20]]
    rows.append([btn('➕ আইডি ব্লক করুন', 'abl:add')])
    rows.append(nav('aow'))
    edit(c, '🚫 <b>Blocklist (%d)</b>\nআনব্লক করতে চাপুন।' % len(bl), kb(*rows))


@route('akt')
def cb_keytest(c, a):
    if not guard(c):
        return
    ack(c, '⏳ পরীক্ষা হচ্ছে...')
    try:
        names = list_models()
        txt = '✅ <b>API Key কাজ করছে!</b>\n🤖 Provider: %s\n🧠 উপলব্ধ মডেল: %d টি\n⭐ বর্তমান: %s' % (PROVIDER.upper(), len(names), esc(current_model() or 'N/A'))
    except AIError as e:
        txt = '❌ <b>Key পরীক্ষা ব্যর্থ</b>\n%s' % esc(str(e))
    except Exception as e:
        txt = '❌ নেটওয়ার্ক সমস্যা: %s' % esc(type(e).__name__)
    edit(c, txt, kb([btn('🔄 আবার', 'akt')], nav('aow')))


@route('abc')
def cb_broadcast(c, a):
    if not guard(c):
        return
    ack(c)
    set_state(c.from_user.id, 'bc')
    edit(c, '📢 <b>Broadcast</b>\n\nসবাইকে পাঠানোর মেসেজ (টেক্সট/ছবি) এখানে পাঠান।\n👥 প্রাপক: <b>%d</b>' % len([u for u in DB.d['users'] if int(u) != c.from_user.id]), kb(nav('aow')))


def bc_worker(chat_id, mid, owner):
    ok = fail = 0
    for k, u in list(DB.d['users'].items()):
        uid = int(k)
        if uid == owner or u['blocked'] or u.get('gone'):
            continue
        try:
            bot.copy_message(uid, chat_id, mid)
            ok += 1
        except Exception as e:
            fail += 1
            if '403' in str(e) or 'blocked' in str(e).lower() or 'deactivated' in str(e).lower():
                u['gone'] = True
        time.sleep(0.05)
    DB.save()
    say(owner, '📢 <b>Broadcast শেষ</b>\n✅ পৌঁছেছে: %d\n❌ ব্যর্থ: %d' % (ok, fail), kb(nav('aow')))


@route('abs')
def cb_bc_send(c, a):
    if not guard(c):
        return
    s = get_state(c.from_user.id)
    if not s or s['k'] != 'bc_ok':
        return ack(c, 'সেশন শেষ', True)
    d = s['d']
    clear_state(c.from_user.id)
    ack(c, '📢 শুরু হয়েছে')
    edit(c, '📢 Broadcast চলছে... শেষ হলে জানাব।', None)
    t = threading.Thread(target=bc_worker, args=(d['chat'], d['mid'], c.from_user.id), daemon=True)
    t.start()


# ---------------------------------------------------------------- commands
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    uid = m.from_user.id
    clear_state(uid)
    uinfo(uid, uname_of(m.from_user))
    first = False
    if DB.d['owner'] is None and m.chat.type == 'private':
        DB.d['owner'] = uid
        first = True
    DB.save()
    t, k = home_view(uid)
    if first:
        t = '👑 <b>আপনি এই AI বটের Owner হয়ে গেছেন!</b>\n🛠 Owner Panel থেকে মডেল, নির্দেশনা, দৈনিক লিমিট ও Broadcast ম্যানেজ করুন।\n\n' + t
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()) if m.chat.type == 'private' else None)


@bot.message_handler(commands=['clear', 'new'])
def clear_cmd(m):
    HIST.pop((m.chat.id, m.from_user.id), None)
    say(m.chat.id, '🧹 চ্যাট হিস্ট্রি মুছে ফেলা হয়েছে।')


@bot.message_handler(commands=['stats'])
def stats_cmd(m):
    if not is_owner(m.from_user.id):
        return
    d = DB.d
    say(m.chat.id, '📊 মোট ইউজার: %d\n💬 মোট প্রশ্ন: %d\n🤖 Provider: %s\n🧠 মডেল: %s' % (len(d['users']), d['messages'], PROVIDER.upper(), d['model'] or ('auto → %s' % (auto_model() or 'N/A'))))


@bot.message_handler(commands=['model'])
def model_cmd(m):
    if not is_owner(m.from_user.id):
        return
    arg = cmd_arg(m)
    if not arg:
        return say(m.chat.id, '🧠 বর্তমান মডেল: <b>%s</b>\nবদলাতে: <code>/model নাম</code> · অটো: <code>/model auto</code>' % esc(DB.d['model'] or ('auto → %s' % (auto_model() or 'N/A'))), kb([btn('🧠 তালিকা', 'am')]))
    if arg.lower() == 'auto':
        DB.d['model'] = ''
        MODEL_CACHE.clear()
        DB.save()
        return say(m.chat.id, '✅ মডেল এখন অটো-সিলেক্ট হবে।')
    DB.d['model'] = arg[:80]
    DB.save()
    say(m.chat.id, '✅ মডেল সেট হয়েছে: %s' % esc(arg[:80]))


@bot.message_handler(commands=['system'])
def system_cmd(m):
    if not is_owner(m.from_user.id):
        return
    arg = cmd_arg(m)
    if not arg:
        return say(m.chat.id, '🎭 বর্তমান নির্দেশনা:\n%s\n\nবদলাতে: <code>/system আপনার নির্দেশনা</code> · রিসেট: <code>/system reset</code>' % esc(DB.d['system'] or DEFAULT_SYSTEM))
    DB.d['system'] = '' if arg.lower() == 'reset' else arg[:1500]
    DB.save()
    HIST.clear()
    say(m.chat.id, '✅ ডিফল্ট নির্দেশনায় ফিরে গেছে।' if arg.lower() == 'reset' else '✅ নতুন নির্দেশনা সেট হয়েছে।')


@bot.message_handler(commands=['ask'])
def ask_cmd(m):
    arg = cmd_arg(m)
    if not arg:
        return say(m.chat.id, 'ব্যবহার: <code>/ask আপনার প্রশ্ন</code>')
    if m.chat.type != 'private' and not DB.d['groups']:
        return
    answer(m, arg)


# ---------------------------------------------------------------- media & text
def addressed(m):
    if m.chat.type == 'private':
        return True
    if not DB.d['groups']:
        return False
    uname = ('@' + (me().username or '')).lower()
    txt = (m.text or m.caption or '').lower()
    replied = bool(m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id == me().id)
    return uname in txt or replied


def strip_mention(m, text):
    if m.chat.type != 'private':
        return re.sub(re.escape('@' + (me().username or '')), '', text, flags=re.I).strip() or text
    return text


def download(file_id, limit):
    info = bot.get_file(file_id)
    if getattr(info, 'file_size', 0) and info.file_size > limit:
        raise AIError('⚠️ ফাইলটি অনেক বড় (সর্বোচ্চ %s)।' % human_size(limit))
    return bot.download_file(info.file_path)


@bot.message_handler(content_types=['photo', 'voice', 'document'])
def on_media(m):
    uid = m.from_user.id
    if not addressed(m):
        return
    s = get_state(uid)
    if s and s['k'] == 'bc' and is_owner(uid):
        return owner_state(m, s)
    cap = (m.caption or '').strip()
    try:
        if m.photo:
            data = download(m.photo[-1].file_id, 10 * 1024 * 1024)
            return answer(m, strip_mention(m, cap) or 'এই ছবিটি সম্পর্কে বলো এবং এতে কী আছে বর্ণনা করো।', [{'mime': 'image/jpeg', 'b64': base64.b64encode(data).decode()}], '[ছবি] ' + (cap or ''))
        if m.voice:
            if PROVIDER != 'gemini':
                return say(m.chat.id, '🎤 ভয়েস মেসেজ শুধু <b>Gemini Key</b>-তে সাপোর্ট করে। টেক্সটে লিখুন।')
            data = download(m.voice.file_id, 10 * 1024 * 1024)
            return answer(m, 'This is a voice message from the user. Understand it and reply to it directly in the same language the user spoke.', [{'mime': 'audio/ogg', 'b64': base64.b64encode(data).decode()}], '[ভয়েস মেসেজ]')
        if m.document:
            name = (m.document.file_name or 'file').lower()
            mime = (m.document.mime_type or '').lower()
            if name.endswith('.pdf') or mime == 'application/pdf':
                if PROVIDER != 'gemini':
                    return say(m.chat.id, '📄 PDF শুধু <b>Gemini Key</b>-তে সাপোর্ট করে।')
                data = download(m.document.file_id, 15 * 1024 * 1024)
                return answer(m, strip_mention(m, cap) or 'এই PDF-টি সংক্ষেপে বুঝিয়ে দাও।', [{'mime': 'application/pdf', 'b64': base64.b64encode(data).decode()}], '[PDF] ' + name)
            if name.endswith(TEXT_EXT) or mime.startswith('text/'):
                data = download(m.document.file_id, 200 * 1024)
                text = data.decode('utf-8', 'replace')[:30000]
                q = (strip_mention(m, cap) or 'এই ফাইলটি সংক্ষেপে বুঝিয়ে দাও।') + '\n\n--- FILE: %s ---\n%s' % (m.document.file_name, text)
                return answer(m, q, None, '[ফাইল] %s — %s' % (m.document.file_name, cap[:200]))
            if m.chat.type == 'private':
                return say(m.chat.id, '⚠️ এই ফাইল ধরন সাপোর্ট করি না। ছবি, ভয়েস, PDF (Gemini) বা টেক্সট/কোড ফাইল পাঠান।')
    except AIError as e:
        return say(m.chat.id, str(e))
    except Exception as e:
        log('media', repr(e)[:150])
        return say(m.chat.id, '❌ ফাইলটি প্রসেস করা যায়নি, আবার চেষ্টা করুন।')


def owner_state(m, s):
    uid = m.from_user.id
    key, d = s['k'], s['d']
    txt = (m.text or '').strip()
    if key == 'bc':
        s['k'] = 'bc_ok'
        d['chat'], d['mid'] = m.chat.id, m.message_id
        return say(m.chat.id, '📢 <b>উপরের মেসেজটি %d জনকে পাঠাবেন?</b>' % len([u for u in DB.d['users'] if int(u) != uid]), kb([btn('✅ পাঠান', 'abs'), btn('✖ বাতিল', 'aow')]), reply_to=m.message_id)
    if key == 'system' and txt:
        DB.d['system'] = txt[:1500]
        DB.save()
        HIST.clear()
        clear_state(uid)
        return say(m.chat.id, '✅ নতুন নির্দেশনা সেট হয়েছে।', kb([btn('🎭 দেখুন', 'asy'), btn('🛠 Panel', 'aow')]))
    if key == 'block_add' and txt:
        n = to_int(txt, None)
        if not n or n == uid:
            return say(m.chat.id, '⚠️ সঠিক ইউজার আইডি (সংখ্যা) লিখুন:')
        uinfo(n)['blocked'] = True
        DB.save()
        clear_state(uid)
        return say(m.chat.id, '🚫 <code>%d</code> ব্লক হয়েছে।' % n, kb([btn('🚫 Blocklist', 'abl'), btn('🛠 Panel', 'aow')]))


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def chat(m):
    uid = m.from_user.id
    if m.chat.type == 'private':
        s = get_state(uid)
        if s and is_owner(uid) and s['k'] in ('bc', 'system', 'block_add'):
            return owner_state(m, s)
    if not addressed(m):
        return
    answer(m, strip_mention(m, m.text))


run(commands=[('start', 'মেনু খুলুন'), ('ask', 'AI-কে প্রশ্ন করুন'), ('clear', 'নতুন চ্যাট শুরু'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')],
    allowed=['message', 'callback_query'])
''',
    },
    "join_approver": {
        "emoji": '✅',
        "name": 'Auto Join Request Approver',
        "proj_name": 'Auto Approver',
        "category": '🛡️ Channel Tools',
        "level": '⭐⭐ Intermediate',
        "setup": '~30 sec',
        "features": ['৩ মোড: 🤖 Auto, 🛡 DM Verification ("আমি রোবট নই" বাটন), 👤 Manual Review', '⏱ অ্যাপ্রুভের আগে অপেক্ষা (০–৬০ সেকেন্ড) ও ⏸ Pause/Resume (রিকোয়েস্ট জমা থাকে)', '🧹 Filters — বট/ডিলিটেড অ্যাকাউন্ট/স্প্যাম নাম অটো ডিক্লাইন + 🚫 Blocklist', '👋 Welcome DM — {name} {chat}, লিংক-বাটন, প্রতি চ্যাটে আলাদা মেসেজ', '📋 My Chats — প্রতি চ্যাট চালু/বন্ধ ও পরিসংখ্যান', '📥 Pending — জমা ও রিভিউ রিকোয়েস্টে Approve/Decline all'],
        "commands": '/status · /pause · /resume · /delay সেকেন্ড · /welcome টেক্সট|off',
        "about": 'চ্যানেল/গ্রুপের জয়েন-রিকোয়েস্ট অটো/ভেরিফাই/ম্যানুয়ালি অ্যাপ্রুভ — ফিল্টার, ব্লকলিস্ট ও Welcome DM সহ।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', 'আপনি /start দিন — Owner হবেন', 'বটকে চ্যানেল/গ্রুপে Admin করুন (Invite Users পারমিশনসহ) ও Invite Link-এ Request Admin Approval অন করুন', 'প্যানেল থেকে মোড, ফিল্টার ও Welcome সেট করুন'],
        "need": 'শুধু বট টোকেন + চ্যানেল/গ্রুপে Admin পারমিশন',
        "preview": '👤 Karim জয়েন রিকোয়েস্ট পাঠাল (Invite Link)\n🤖 ✅ অটো অ্যাপ্রুভ\n🤖 → Karim: স্বাগতম Karim! চ্যানেলে আপনাকে ধন্যবাদ।',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('join_data.json', {'owner': None, 'paused': False, 'delay': 0, 'welcome': '', 'wbtn': '', 'mode': 'auto', 'f_bots': False,
                              'f_deleted': False, 'f_spam': False, 'block': [], 'approved': 0, 'declined': 0, 'chats': {}, 'queue': [], 'pending': {}})
DELAYS = [0, 5, 10, 30, 60]
MODES = [('auto', '🤖 Auto'), ('captcha', '🧩 Captcha DM'), ('manual', '👤 Manual')]
MODE_NAME = dict(MODES)
MODE_HELP = {'auto': 'রিকোয়েস্ট এলেই অটো অ্যাপ্রুভ', 'captcha': 'জয়েনারকে DM-এ বাটন চাপতে হবে (৫ মিনিটের মধ্যে), নাহলে ডিক্লাইন', 'manual': 'প্রতিটি রিকোয়েস্ট আপনি বাটনে অ্যাপ্রুভ/ডিক্লাইন করবেন'}
QUEUE_CAP, PENDING_CAP, CAPTCHA_SECS = 5000, 2000, 290
SPAM_RE = re.compile(r'(https?://|t\.me/|www\.|@\w{3,}|\.(com|net|xyz|top|ru)\b|joinchat)', re.I)


def is_owner(uid):
    return DB.d.get('owner') == uid


def chat_rec(chat_id, title=None):
    c = DB.d['chats'].setdefault(str(chat_id), {'title': title or str(chat_id), 'approved': 0, 'declined': 0, 'enabled': True, 'welcome': ''})
    if title:
        c['title'] = title
    c.setdefault('declined', 0)
    c.setdefault('enabled', True)
    c.setdefault('welcome', '')
    return c


def approve(chat_id, user_id):
    for _ in range(3):
        try:
            bot.approve_chat_join_request(chat_id, user_id)
            return True
        except Exception as e:
            msg = str(e)
            if 'retry after' in msg.lower():
                time.sleep(min(_retry_secs(msg.lower()), 30))
                continue
            if 'USER_ALREADY_PARTICIPANT' in msg or 'HIDE_REQUESTER_MISSING' in msg:
                return True
            log('approve', msg[:150])
            return False
    return False


def decline(chat_id, user_id):
    for _ in range(3):
        try:
            bot.decline_chat_join_request(chat_id, user_id)
            return True
        except Exception as e:
            msg = str(e)
            if 'retry after' in msg.lower():
                time.sleep(min(_retry_secs(msg.lower()), 30))
                continue
            return 'HIDE_REQUESTER_MISSING' in msg or 'USER_ALREADY_PARTICIPANT' in msg
    return False


def welcome_markup():
    b = DB.d.get('wbtn') or ''
    if '|' in b:
        label, _, url = b.partition('|')
        if url.strip().startswith(('http://', 'https://', 'tg://')) and label.strip():
            return kb([btn(label.strip()[:30], url=url.strip())])
    return None


def send_welcome(chat_id, user, user_chat, title):
    text = chat_rec(chat_id)['welcome'] or DB.d['welcome']
    if not text:
        return
    name = esc(user.first_name or 'Friend') if hasattr(user, 'first_name') else 'Friend'
    say(user_chat or user.id, text.replace('{name}', name).replace('{chat}', esc(title or '')), welcome_markup())


def count(chat_id, title, ok=True):
    with DB.lock:
        c = chat_rec(chat_id, title)
        if ok:
            DB.d['approved'] += 1
            c['approved'] += 1
        else:
            DB.d['declined'] += 1
            c['declined'] += 1
        DB.save()


def process(chat_id, title, user, user_chat):
    if not approve(chat_id, user.id):
        return False
    count(chat_id, title)
    send_welcome(chat_id, user, user_chat, title)
    return True


def reject_reason(user):
    if user.id in DB.d['block']:
        return 'blocklist'
    if DB.d['f_bots'] and getattr(user, 'is_bot', False):
        return 'bot'
    if DB.d['f_deleted'] and (user.first_name or '') in ('Deleted Account', ''):
        return 'deleted'
    if DB.d['f_spam'] and SPAM_RE.search('%s %s %s' % (user.first_name or '', getattr(user, 'last_name', '') or '', getattr(user, 'username', '') or '')):
        return 'spam'
    return None


@bot.chat_join_request_handler()
def on_join(req):
    chat, user = req.chat, req.from_user
    uchat = getattr(req, 'user_chat_id', None)
    rec = chat_rec(chat.id, chat.title)
    if not rec['enabled']:
        return
    why = reject_reason(user)
    if why:
        if decline(chat.id, user.id):
            count(chat.id, chat.title, False)
        return
    if DB.d['paused']:
        with DB.lock:
            if len(DB.d['queue']) < QUEUE_CAP:
                DB.d['queue'].append([chat.id, chat.title or '', user.id, uchat or user.id, user.first_name or ''])
                DB.save()
        return
    mode = DB.d['mode']
    key = '%s:%s' % (chat.id, user.id)
    if mode == 'auto':
        args = (chat.id, chat.title, user, uchat)
        if DB.d['delay'] > 0:
            tm = threading.Timer(DB.d['delay'], process, args=args)
            tm.daemon = True
            tm.start()
        else:
            process(*args)
        return
    with DB.lock:
        if len(DB.d['pending']) >= PENDING_CAP:
            return
        DB.d['pending'][key] = {'chat': chat.id, 'uid': user.id, 'uchat': uchat or user.id, 'name': (user.first_name or 'User')[:40], 'ts': time.time(), 'mode': mode, 'title': chat.title or ''}
        DB.save()
    if mode == 'captcha':
        say(uchat or user.id, '🤖 <b>পরিচয় যাচাই</b>\n\n<b>%s</b>-এ জয়েন করতে নিচের বাটনটি চাপুন (৫ মিনিটের মধ্যে)।' % esc(chat.title or 'চ্যাট'), kb([btn('✅ আমি রোবট নই', 'jv:%s' % key)]))
    elif DB.d['owner']:
        say(DB.d['owner'], '🆕 <b>জয়েন রিকোয়েস্ট</b>\n📌 %s\n👤 %s (<code>%s</code>)' % (esc(chat.title or chat.id), esc(user.first_name or 'User'), user.id),
            kb([btn('✅ Approve', 'jm:a:%s' % key), btn('❌ Decline', 'jm:d:%s' % key)], [btn('🚫 Block & Decline', 'jm:b:%s' % key)]))


@route('jv')
def cb_verify(c, a):
    key = ':'.join(a)
    p = DB.d['pending'].get(key)
    if not p or p['mode'] != 'captcha':
        return ack(c, '⌛ সময় শেষ বা রিকোয়েস্ট পাওয়া যায়নি', True)
    if c.from_user.id != p['uid']:
        return ack(c, 'এই বাটন আপনার জন্য নয়', True)
    DB.d['pending'].pop(key, None)
    ok = process(p['chat'], p['title'], c.from_user, p['uchat'])
    DB.save()
    ack(c, '✅ যাচাই সম্পন্ন!' if ok else '⚠️ অ্যাপ্রুভ করা যায়নি', not ok)
    edit(c, '✅ <b>যাচাই সম্পন্ন!</b> আপনি এখন <b>%s</b>-এর সদস্য।' % esc(p['title']) if ok else '⚠️ অ্যাপ্রুভ করা যায়নি, আবার জয়েন রিকোয়েস্ট দিন।', None)


@route('jm')
def cb_manual(c, a):
    if not is_owner(c.from_user.id) or len(a) < 3:
        return ack(c, '🚫 শুধু Owner', True)
    act, key = a[0], ':'.join(a[1:])
    p = DB.d['pending'].pop(key, None)
    if not p:
        return ack(c, 'রিকোয়েস্টটি আগেই প্রসেস হয়েছে', True)
    if act == 'a':
        ok = approve(p['chat'], p['uid'])
        if ok:
            count(p['chat'], p['title'])
            send_welcome(p['chat'], Obj_user(p), p['uchat'], p['title'])
        res = '✅ Approved' if ok else '⚠️ অ্যাপ্রুভ করা যায়নি'
    else:
        if act == 'b' and p['uid'] not in DB.d['block']:
            DB.d['block'].append(p['uid'])
        ok = decline(p['chat'], p['uid'])
        if ok:
            count(p['chat'], p['title'], False)
        res = '🚫 Blocked & Declined' if act == 'b' else '❌ Declined'
    DB.save()
    ack(c, res)
    edit(c, '%s\n👤 %s (<code>%s</code>) — %s' % (res, esc(p['name']), p['uid'], esc(p['title'])), None)


class Obj_user:
    def __init__(self, p):
        self.id, self.first_name = p['uid'], p['name']


def expire_tick():
    now_t = time.time()
    dead = [(k, p) for k, p in DB.d['pending'].items() if p['mode'] == 'captcha' and now_t - p['ts'] > CAPTCHA_SECS]
    for k, p in dead:
        DB.d['pending'].pop(k, None)
        if decline(p['chat'], p['uid']):
            count(p['chat'], p['title'], False)
    if dead:
        DB.save()


@bot.my_chat_member_handler()
def on_added(upd):
    try:
        if upd.chat.type in ('channel', 'supergroup', 'group') and upd.new_chat_member.status == 'administrator':
            chat_rec(upd.chat.id, upd.chat.title)
            DB.save()
            if DB.d['owner']:
                can = getattr(upd.new_chat_member, 'can_invite_users', None)
                note = '' if can else '\n⚠️ বটকে <b>Invite Users / Add Subscribers</b> পারমিশন দিন, নাহলে অ্যাপ্রুভ করতে পারবে না।'
                say(DB.d['owner'], '✅ বট <b>%s</b>-এ অ্যাডমিন হয়েছে।%s' % (esc(upd.chat.title or upd.chat.id), note), kb([btn('📋 Chats', 'jc')]))
    except Exception as e:
        log('my_chat_member', repr(e))


# ---------------------------------------------------------------- owner home
def status_text():
    d = DB.d
    return ('📊 <b>Auto Approver</b>\n' + LINE + '\nঅবস্থা: %s\n🛡 মোড: <b>%s</b>\n⏱ অপেক্ষা: <b>%d সেকেন্ড</b>\n👋 Welcome DM: %s\n✅ মোট অ্যাপ্রুভ: <b>%d</b>   ❌ ডিক্লাইন: <b>%d</b>\n📥 অপেক্ষমাণ: <b>%d</b>   🚫 ব্লক: <b>%d</b>'
            % ('⏸ Paused' if d['paused'] else '🟢 Active', MODE_NAME[d['mode']], d['delay'], '✅ চালু' if d['welcome'] else '❌ বন্ধ', d['approved'], d['declined'],
               len(d['queue']) + len(d['pending']), len(d['block'])) + '\n<i>%s</i>' % MODE_HELP[d['mode']])


def home_view(uid):
    d = DB.d
    if not is_owner(uid):
        return '🤖 <b>অটো জয়েন-রিকোয়েস্ট অ্যাপ্রুভার</b>\nএই বটটি চ্যানেল/গ্রুপের জয়েন রিকোয়েস্ট স্বয়ংক্রিয়ভাবে প্রসেস করে।' + FOOTER, kb()
    return status_text() + FOOTER, kb(
        [btn('▶️ Resume' if d['paused'] else '⏸ Pause', 'jp'), btn('⏱ Delay: %ds' % d['delay'], 'jd')],
        [btn('🛡 Mode: %s' % MODE_NAME[d['mode']], 'jmo'), btn('🧹 Filters', 'jf')],
        [btn('👋 Welcome DM', 'jw'), btn('📋 Chats (%d)' % len(d['chats']), 'jc')],
        [btn('📥 Pending (%d)' % (len(d['queue']) + len(d['pending'])), 'jq'), btn('🚫 Blocklist (%d)' % len(d['block']), 'jb')],
        [btn('🔄 Refresh', 'home'), btn('❓ Help', 'jhelp')])


HELP = ('❓ <b>Auto Approver — সাহায্য</b>\n' + LINE + '\n'
        '<b>সেটআপ:</b>\n1️⃣ বটকে চ্যানেল/গ্রুপে Admin করুন (<b>Invite Users</b> পারমিশনসহ)\n2️⃣ Invite Link বানিয়ে <b>Request Admin Approval</b> অন করুন\n3️⃣ ওই লিংকে রিকোয়েস্ট এলেই বট প্রসেস করবে\n\n'
        '🛡 <b>মোড:</b> Auto (সাথে সাথে) • Captcha DM (জয়েনার বাটন চাপলে) • Manual (আপনি বাটনে)\n'
        '🧹 <b>ফিল্টার:</b> বট, ডিলিটেড অ্যাকাউন্ট ও স্প্যাম-নাম অটো ডিক্লাইন; Blocklist\n'
        '👋 <b>Welcome DM:</b> <code>{name}</code> ও <code>{chat}</code> ব্যবহার করুন; চাইলে একটি লিংক-বাটনও\n'
        '📋 <b>Chats:</b> প্রতিটি চ্যানেল/গ্রুপ আলাদা চালু/বন্ধ ও আলাদা Welcome\n'
        '⏸ Pause করলে রিকোয়েস্ট জমা থাকে, Resume বা Pending থেকে একসাথে অ্যাপ্রুভ\n\n'
        '⌨️ /status · /pause · /resume · /delay সেকেন্ড · /welcome টেক্সট (বন্ধ: /welcome off)')


@route('jhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


def guard(c):
    if not is_owner(c.from_user.id):
        ack(c, '🚫 শুধু Owner', True)
        return False
    return True


def refresh_home(c):
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@route('jp')
def cb_pause(c, a):
    if not guard(c):
        return
    DB.d['paused'] = not DB.d['paused']
    DB.save()
    if not DB.d['paused'] and DB.d['queue']:
        ack(c, '▶️ চালু — অপেক্ষমাণগুলো অ্যাপ্রুভ হচ্ছে')
        start_drain(c.from_user.id)
    else:
        ack(c, '⏸ থামানো হয়েছে' if DB.d['paused'] else '▶️ চালু হয়েছে')
    refresh_home(c)


@route('jd')
def cb_delay(c, a):
    if not guard(c):
        return
    DB.d['delay'] = DELAYS[(DELAYS.index(DB.d['delay']) + 1) % len(DELAYS)] if DB.d['delay'] in DELAYS else 0
    DB.save()
    ack(c, '⏱ %d সেকেন্ড' % DB.d['delay'])
    refresh_home(c)


@route('jmo')
def cb_mode(c, a):
    if not guard(c):
        return
    keys = [k for k, _ in MODES]
    DB.d['mode'] = keys[(keys.index(DB.d['mode']) + 1) % len(keys)]
    DB.save()
    ack(c, MODE_NAME[DB.d['mode']])
    refresh_home(c)


# ---------------------------------------------------------------- filters / blocklist / welcome
def filters_view():
    d = DB.d
    t = lambda k: '✅ ON' if d[k] else '❌ OFF'
    return ('🧹 <b>Filters</b>\n' + LINE + '\nচালু করা ফিল্টারে ধরা পড়লে রিকোয়েস্ট অটো ডিক্লাইন হবে।\n\n🤖 বট অ্যাকাউন্ট: %s\n👻 ডিলিটেড অ্যাকাউন্ট: %s\n🔗 স্প্যাম নাম (লিংক/@/ডোমেইন): %s' % (t('f_bots'), t('f_deleted'), t('f_spam'))), \
        kb([btn('🤖 Bots: %s' % t('f_bots'), 'jf:f_bots')], [btn('👻 Deleted: %s' % t('f_deleted'), 'jf:f_deleted')], [btn('🔗 Spam name: %s' % t('f_spam'), 'jf:f_spam')], nav())


@route('jf')
def cb_filters(c, a):
    if not guard(c):
        return
    if a and a[0] in ('f_bots', 'f_deleted', 'f_spam'):
        DB.d[a[0]] = not DB.d[a[0]]
        DB.save()
    ack(c)
    t, k = filters_view()
    edit(c, t, k)


@route('jb')
def cb_block(c, a):
    if not guard(c):
        return
    if len(a) >= 2 and a[0] == 'x':
        u = to_int(a[1], 0)
        if u in DB.d['block']:
            DB.d['block'].remove(u)
            DB.save()
        ack(c, '♻️ আনব্লক হয়েছে')
    elif a and a[0] == 'add':
        ack(c)
        set_state(c.from_user.id, 'blk_add')
        return edit(c, '🚫 <b>ব্লক করতে ইউজার আইডি লিখুন</b> (সংখ্যা):', kb(nav('jb')))
    else:
        ack(c)
    rows = [[btn('♻️ %s' % u, 'jb:x:%s' % u)] for u in DB.d['block'][:20]]
    rows.append([btn('➕ আইডি ব্লক করুন', 'jb:add')])
    rows.append(nav())
    edit(c, '🚫 <b>Blocklist (%d)</b>\nএদের রিকোয়েস্ট অটো ডিক্লাইন হয়। আনব্লক করতে আইডিতে চাপুন।' % len(DB.d['block']), kb(*rows))


def welcome_view():
    d = DB.d
    prev = esc(d['welcome']) if d['welcome'] else '— বন্ধ —'
    btxt = d['wbtn'].split('|')[0] if '|' in d['wbtn'] else '—'
    return ('👋 <b>Welcome DM</b>\n' + LINE + '\nঅ্যাপ্রুভের পর জয়েনারকে এই মেসেজ যাবে।\n\n%s\n\n🔘 বাটন: <b>%s</b>\n<i>প্লেসহোল্ডার: {name}, {chat}</i>' % (prev, esc(btxt))), \
        kb([btn('✏️ মেসেজ সেট', 'jw:set'), btn('👁 প্রিভিউ', 'jw:pv')], [btn('🔘 বাটন সেট', 'jw:btn'), btn('🗑 বন্ধ/মুছুন', 'jw:off')], nav())


@route('jw')
def cb_welcome(c, a):
    if not guard(c):
        return
    uid = c.from_user.id
    act = a[0] if a else ''
    if act == 'set':
        ack(c)
        set_state(uid, 'welcome')
        return edit(c, '✏️ <b>Welcome মেসেজ লিখুন</b>\nHTML চলবে (<b>bold</b>, <i>italic</i>)। যেমন:\n<code>স্বাগতম {name}! {chat}-এ আপনাকে পেয়ে খুশি 🎉</code>', kb(nav('jw')))
    if act == 'btn':
        ack(c)
        set_state(uid, 'wbtn')
        return edit(c, '🔘 <b>বাটন সেট করুন</b>\nফরম্যাট: <code>বাটনের লেখা | https://লিংক</code>\nযেমন: <code>📢 চ্যানেল দেখুন | https://t.me/example</code>', kb(nav('jw')))
    if act == 'off':
        DB.d['welcome'], DB.d['wbtn'] = '', ''
        DB.save()
        ack(c, '🗑 বন্ধ করা হয়েছে')
    elif act == 'pv':
        if not DB.d['welcome']:
            return ack(c, 'Welcome মেসেজ সেট করা নেই', True)
        ack(c)
        say(c.message.chat.id, '👁 <b>প্রিভিউ:</b>\n\n' + DB.d['welcome'].replace('{name}', esc(c.from_user.first_name or 'Friend')).replace('{chat}', 'আপনার চ্যানেল'), welcome_markup())
        return
    else:
        ack(c)
    t, k = welcome_view()
    edit(c, t, k)


# ---------------------------------------------------------------- chats
@route('jc')
def cb_chats(c, a):
    if not guard(c):
        return
    ack(c)
    items = sorted(DB.d['chats'].items(), key=lambda kv: -kv[1]['approved'])
    if not items:
        return edit(c, '📋 <b>Chats</b>\n\nএখনো কোনো চ্যাট নেই। বটকে চ্যানেল/গ্রুপে অ্যাডমিন করুন।', kb(nav()))
    chunk, page, pages = paginate(items, to_int(a[0], 0) if a else 0, 8)
    rows = [[btn('%s %s • %d' % ('🟢' if v['enabled'] else '⏸', v['title'][:24], v['approved']), 'jcv:%s' % k)] for k, v in chunk]
    pg = pager('jc', page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav())
    edit(c, '📋 <b>Chats (%d)</b>\nচাপ দিয়ে সেটিংস দেখুন।' % len(items), kb(*rows))


def chat_view(cid):
    v = DB.d['chats'].get(cid)
    if not v:
        return '⚠️ চ্যাট পাওয়া যায়নি।', kb(nav('jc'))
    txt = ('📌 <b>%s</b>\n' % esc(v['title']) + LINE + '\n🆔 <code>%s</code>\nঅবস্থা: %s\n✅ অ্যাপ্রুভ: <b>%d</b>   ❌ ডিক্লাইন: <b>%d</b>\n👋 আলাদা Welcome: %s'
           % (cid, '🟢 চালু' if v['enabled'] else '⏸ বন্ধ (রিকোয়েস্ট ঝুলে থাকবে)', v['approved'], v['declined'], 'আছে' if v['welcome'] else 'নেই (গ্লোবাল ব্যবহার হয়)'))
    return txt, kb([btn('⏸ বন্ধ করুন' if v['enabled'] else '▶️ চালু করুন', 'jcv:%s:t' % cid)], [btn('👋 Welcome সেট', 'jcv:%s:w' % cid), btn('🧹 Welcome মুছুন', 'jcv:%s:wx' % cid)],
                   [btn('🗑 তালিকা থেকে সরান', 'jcv:%s:rm' % cid)], nav('jc'))


@route('jcv')
def cb_chat_view(c, a):
    if not guard(c) or not a:
        return
    cid = a[0]
    v = DB.d['chats'].get(cid)
    if not v:
        return ack(c, 'চ্যাট পাওয়া যায়নি', True)
    act = a[1] if len(a) > 1 else ''
    if act == 't':
        v['enabled'] = not v['enabled']
    elif act == 'w':
        ack(c)
        set_state(c.from_user.id, 'cwelcome', cid=cid)
        return edit(c, '👋 <b>%s</b>-এর জন্য আলাদা Welcome লিখুন ({name}, {chat} চলবে):' % esc(v['title']), kb(nav('jcv:%s' % cid)))
    elif act == 'wx':
        v['welcome'] = ''
    elif act == 'rm':
        DB.d['chats'].pop(cid, None)
        DB.save()
        ack(c, '🗑 সরানো হয়েছে')
        return cb_chats(c, [])
    DB.save()
    ack(c)
    t, k = chat_view(cid)
    edit(c, t, k)


# ---------------------------------------------------------------- pending / queue
def pending_view():
    d = DB.d
    caps = len([p for p in d['pending'].values() if p['mode'] == 'captcha'])
    man = len(d['pending']) - caps
    txt = ('📥 <b>Pending</b>\n' + LINE + '\n⏸ Pause-এ জমা: <b>%d</b>\n🧩 Captcha অপেক্ষায়: <b>%d</b>\n👤 Manual অপেক্ষায়: <b>%d</b>' % (len(d['queue']), caps, man))
    return txt, kb([btn('✅ জমা সব অ্যাপ্রুভ', 'jq:qa'), btn('❌ জমা সব ডিক্লাইন', 'jq:qd')],
                   [btn('✅ Manual সব অ্যাপ্রুভ', 'jq:pa'), btn('❌ Manual সব ডিক্লাইন', 'jq:pd')], [btn('🔄 Refresh', 'jq')], nav())


@route('jq')
def cb_pending(c, a):
    if not guard(c):
        return
    act = a[0] if a else ''
    if act in ('qa', 'qd', 'pa', 'pd'):
        n = len(DB.d['queue']) if act.startswith('q') else len([p for p in DB.d['pending'].values() if p['mode'] == 'manual'])
        if not n:
            return ack(c, 'কিছু নেই', True)
        ack(c, '⏳ প্রসেস হচ্ছে (%d)...' % n)
        t = threading.Thread(target=bulk_worker, args=(act, c.from_user.id), daemon=True)
        t.start()
    else:
        ack(c)
    t, k = pending_view()
    edit(c, t, k)


def bulk_worker(act, owner):
    ok = fail = 0
    if act.startswith('q'):
        with DB.lock:
            q, DB.d['queue'] = DB.d['queue'], []
            DB.save()
        for item in q:
            chat_id, title, uid, uchat = item[0], item[1], item[2], item[3] if len(item) > 3 else item[2]
            name = item[4] if len(item) > 4 else ''
            if act == 'qa':
                good = approve(chat_id, uid)
                if good:
                    count(chat_id, title)
                    send_welcome(chat_id, Obj_user({'uid': uid, 'name': name or 'Friend'}), uchat, title)
            else:
                good = decline(chat_id, uid)
                if good:
                    count(chat_id, title, False)
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            time.sleep(0.05)
    else:
        items = [(k, p) for k, p in list(DB.d['pending'].items()) if p['mode'] == 'manual']
        for k, p in items:
            DB.d['pending'].pop(k, None)
            if act == 'pa':
                good = approve(p['chat'], p['uid'])
                if good:
                    count(p['chat'], p['title'])
                    send_welcome(p['chat'], Obj_user(p), p['uchat'], p['title'])
            else:
                good = decline(p['chat'], p['uid'])
                if good:
                    count(p['chat'], p['title'], False)
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            time.sleep(0.05)
        DB.save()
    say(owner, '📥 <b>প্রসেস শেষ</b>\n✅ সফল: %d\n⚠️ ব্যর্থ: %d' % (ok, fail), kb([btn('📊 Status', 'home')]))


def start_drain(owner):
    t = threading.Thread(target=bulk_worker, args=('qa', owner), daemon=True)
    t.start()


# ---------------------------------------------------------------- commands & text
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    if m.chat.type != 'private':
        return
    uid = m.from_user.id
    clear_state(uid)
    if DB.d['owner'] is None:
        DB.d['owner'] = uid
        DB.save()
        t, k = home_view(uid)
        return say(m.chat.id, '👑 <b>আপনি এই বটের Owner হয়ে গেছেন!</b>\n\n<b>সেটআপ:</b>\n1️⃣ বটকে চ্যানেল/গ্রুপে Admin করুন (<b>Invite Users</b> পারমিশনসহ)\n2️⃣ Invite Link-এ <b>Request Admin Approval</b> অন করুন\n3️⃣ ওই লিংকে রিকোয়েস্ট এলেই অটো প্রসেস হবে ✅\n\n' + t, k)
    t, k = home_view(uid)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP if is_owner(m.from_user.id) else '🤖 এটি একটি অটো জয়েন-রিকোয়েস্ট অ্যাপ্রুভার বট।', kb(nav()) if is_owner(m.from_user.id) else None)


@bot.message_handler(commands=['status', 'stats'])
def status_cmd(m):
    if not is_owner(m.from_user.id):
        return
    lines = [status_text()]
    if DB.d['chats']:
        lines.append('\n<b>চ্যাট অনুযায়ী:</b>')
        for cid, c in sorted(DB.d['chats'].items(), key=lambda x: -x[1]['approved'])[:15]:
            lines.append('• %s — %d' % (esc(c['title']), c['approved']))
    say(m.chat.id, '\n'.join(lines), kb([btn('🏠 Panel', 'home')]))


@bot.message_handler(commands=['pause'])
def pause_cmd(m):
    if not is_owner(m.from_user.id):
        return
    DB.d['paused'] = True
    DB.save()
    say(m.chat.id, '⏸️ অ্যাপ্রুভ থামানো হয়েছে। নতুন রিকোয়েস্ট জমা থাকবে — /resume দিলে একসাথে অ্যাপ্রুভ হবে।')


@bot.message_handler(commands=['resume'])
def resume_cmd(m):
    if not is_owner(m.from_user.id):
        return
    DB.d['paused'] = False
    DB.save()
    n = len(DB.d['queue'])
    say(m.chat.id, '▶️ চালু হয়েছে।' + (' জমা %d টি রিকোয়েস্ট প্রসেস হচ্ছে...' % n if n else ''))
    if n:
        start_drain(m.from_user.id)


@bot.message_handler(commands=['delay'])
def delay_cmd(m):
    if not is_owner(m.from_user.id):
        return
    v = to_int(cmd_arg(m), None)
    if v is None or not 0 <= v <= 60:
        return say(m.chat.id, '⚠️ ব্যবহার: <code>/delay 0-60</code> (সেকেন্ড)')
    DB.d['delay'] = v
    DB.save()
    say(m.chat.id, '⏱ অপেক্ষা সেট: %d সেকেন্ড' % v)


@bot.message_handler(commands=['welcome'])
def welcome_cmd(m):
    if not is_owner(m.from_user.id):
        return
    arg = cmd_arg(m)
    if not arg:
        t, k = welcome_view()
        return say(m.chat.id, t, k)
    DB.d['welcome'] = '' if arg.lower() == 'off' else arg[:1500]
    DB.save()
    say(m.chat.id, '✅ Welcome DM বন্ধ করা হয়েছে।' if arg.lower() == 'off' else '✅ Welcome DM সেট হয়েছে।')


@bot.message_handler(func=lambda m: m.chat.type == 'private' and bool(m.text) and not m.text.startswith('/'))
def on_text(m):
    uid = m.from_user.id
    if not is_owner(uid):
        return
    s = get_state(uid)
    txt = m.text.strip()
    if not s:
        t, k = home_view(uid)
        return say(m.chat.id, t, k)
    key, d = s['k'], s['d']
    clear_state(uid)
    if key == 'welcome':
        DB.d['welcome'] = txt[:1500]
        DB.save()
        t, k = welcome_view()
        return say(m.chat.id, '✅ Welcome মেসেজ সেট হয়েছে।\n\n' + t, k)
    if key == 'wbtn':
        label, _, url = txt.partition('|')
        if not label.strip() or not url.strip().startswith(('http://', 'https://', 'tg://')):
            set_state(uid, 'wbtn')
            return say(m.chat.id, '⚠️ ফরম্যাট: <code>লেখা | https://লিংক</code> — আবার লিখুন:')
        DB.d['wbtn'] = '%s|%s' % (label.strip()[:30], url.strip())
        DB.save()
        t, k = welcome_view()
        return say(m.chat.id, '✅ বাটন সেট হয়েছে।\n\n' + t, k)
    if key == 'cwelcome':
        v = DB.d['chats'].get(d['cid'])
        if v:
            v['welcome'] = txt[:1500]
            DB.save()
        t, k = chat_view(d['cid'])
        return say(m.chat.id, '✅ সেট হয়েছে।\n\n' + t, k)
    if key == 'blk_add':
        u = to_int(txt, None)
        if not u or u == uid:
            set_state(uid, 'blk_add')
            return say(m.chat.id, '⚠️ সঠিক ইউজার আইডি (সংখ্যা) লিখুন:')
        if u not in DB.d['block']:
            DB.d['block'].append(u)
            DB.save()
        return say(m.chat.id, '🚫 <code>%d</code> ব্লক হয়েছে।' % u, kb([btn('🚫 Blocklist', 'jb'), btn('🏠 Home', 'home')]))


start_worker(expire_tick, 20)
run(commands=[('start', 'প্যানেল খুলুন'), ('status', 'অবস্থা ও পরিসংখ্যান'), ('pause', 'অ্যাপ্রুভ থামান'), ('resume', 'অ্যাপ্রুভ চালু'),
              ('delay', 'অ্যাপ্রুভের আগে অপেক্ষা'), ('welcome', 'Welcome DM'), ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')],
    allowed=['message', 'callback_query', 'chat_join_request', 'my_chat_member'])
''',
    },
    "file_link": {
        "emoji": '🔗',
        "name": 'File to Link Bot',
        "proj_name": 'File To Link',
        "category": '📎 File Tools',
        "level": '⭐⭐⭐ Advanced',
        "setup": '~30 sec',
        "features": ['ফাইল পাঠালেই শেয়ারযোগ্য লিংক — আর 📁 Collection (একাধিক ফাইল এক লিংকে)', '🔒 পাসওয়ার্ড, ⏳ মেয়াদ, 🔢 ওপেন-লিমিট, 🧨 অটো-ডিলিট, 🚫 ফরওয়ার্ড ব্লক — লিংক প্রতি আলাদা', '🔔 Force Join — চ্যানেলে জয়েন না করলে ফাইল নয়', '🌐 Public Mode — অন্যরাও আপলোড করতে পারবে (দৈনিক লিমিটসহ)', '📂 My Links — ম্যানেজ, ♻️ লিংক রিসেট, 📷 QR', '👥 ইউনিক ওপেন কাউন্ট'],
        "commands": '/mylinks · /del কোড · /public on|off · /stats',
        "about": 'ফাইল-টু-লিংক বট — পাসওয়ার্ড/মেয়াদ/লিমিট, কালেকশন, ফোর্স-জয়েন ও পাবলিক আপলোড মোডসহ।',
        "steps": ['বট সাথে সাথে ডিপ্লয় ও লাইভ হবে', 'আপনি /start দিন — Owner হবেন', 'যেকোনো ফাইল পাঠান — লিংক পাবেন; 📁 Collection-এ একাধিক ফাইল', 'লিংকের কার্ড থেকে পাসওয়ার্ড/মেয়াদ/লিমিট সেট করুন', 'Force Join-এ চ্যানেল যোগ করুন (বটকে চ্যানেলে Admin করুন)'],
        "need": 'শুধু বট টোকেন',
        "preview": '👤 [movie.mp4 পাঠাল]\n🤖 ✅ লিংক তৈরি হয়েছে!\n🔗 https://t.me/YourBot?start=f_Ab3xK9\n👤 (বন্ধু লিংকে ক্লিক করল) 🤖 [ভিডিও ▶️]',
        "code": r'''BOT_TOKEN = '__TOKEN__'
import os, sys, io, re, json, time, html, shutil, random, string, hashlib, secrets, threading, datetime, traceback
import urllib.request, urllib.parse, urllib.error
import telebot
from telebot import types

try:
    _PM = PARSE_MODE
except NameError:
    _PM = 'HTML'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=_PM)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTER = '\n\n⚡ Powered by @PyHost_CloudBot'
LINE = '━━━━━━━━━━━━━━━━━━'
UA = 'Mozilla/5.0 (compatible; PyHostBot/2.0)'


def log(*a):
    try:
        print('[bot]', *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers
def esc(s):
    return html.escape(str(s if s is not None else ''), quote=False)


def escq(s):
    return html.escape(str(s if s is not None else ''), quote=True)


_BN = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def norm_digits(s):
    return str(s if s is not None else '').translate(_BN)


def to_int(s, default=None):
    try:
        return int(norm_digits(s).strip())
    except Exception:
        return default


def to_float(s, default=None):
    try:
        return float(norm_digits(s).strip().replace(',', ''))
    except Exception:
        return default


def uname_of(u):
    if u is None:
        return 'User'
    n = (getattr(u, 'first_name', None) or '').strip()
    if not n:
        n = getattr(u, 'username', None) or str(getattr(u, 'id', ''))
    return n


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u.id, esc(uname_of(u)))


def cmd_arg(m):
    parts = (m.text or m.caption or '').split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ''


def human_size(n):
    if not n:
        return 'N/A'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u == 'B' else '%.1f %s') % (n, u)
        n /= 1024


def parse_duration(s):
    s = norm_digits(s).strip().lower().replace(' ', '')
    if not s:
        return None
    pos, total = 0, 0.0
    for mt in re.finditer(r'(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)', s):
        if mt.start() != pos:
            return None
        total += float(mt.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[mt.group(2)[0]]
        pos = mt.end()
    if pos != len(s) or total <= 0:
        return None
    return int(total)


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d সেকেন্ড' % sec
    parts = []
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi = r // 60
    if d:
        parts.append('%d দিন' % d)
    if h:
        parts.append('%d ঘণ্টা' % h)
    if mi and not d:
        parts.append('%d মিনিট' % mi)
    return ' '.join(parts) or '1 মিনিট'


def tz_label(off):
    sign = '+' if off >= 0 else '-'
    a = abs(off)
    h = int(a)
    mi = int(round((a - h) * 60))
    return 'UTC%s%d' % (sign, h) + (':%02d' % mi if mi else '')


def local_dt(ts, off):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=off)
    return d.replace(tzinfo=None)


def local_to_ts(dt, off):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds() - off * 3600


def fmt_ts(ts, off, fmt='%d %b %Y, %H:%M'):
    return local_dt(ts, off).strftime(fmt)


def parse_tz(s):
    s = norm_digits(s).strip().lower().replace('utc', '').replace('gmt', '').replace(' ', '')
    mt = re.fullmatch(r'([+-]?)(\d{1,2})(?::?(\d{2}))?', s)
    if not mt:
        return None
    off = int(mt.group(2)) + (int(mt.group(3)) / 60.0 if mt.group(3) else 0)
    if mt.group(1) == '-':
        off = -off
    if -12 <= off <= 14:
        return off
    return None


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, filename, defaults=None):
        self.path = os.path.join(BASE_DIR, filename)
        self.defaults = defaults or {}
        self.lock = threading.RLock()
        self.d = self._load()

    def _load(self):
        data = None
        for p in (self.path, self.path + '.bak'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except Exception:
                data = None
        if data is None:
            data = {}
        for k, v in self.defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data

    def save(self):
        with self.lock:
            s = None
            for _ in range(3):
                try:
                    s = json.dumps(self.d, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.02)
            if s is None:
                return
            try:
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(s)
                if os.path.exists(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + '.bak')
                    except Exception:
                        pass
                os.replace(tmp, self.path)
            except Exception as e:
                log('save failed', repr(e))


class TTL:
    def __init__(self, ttl, cap=500):
        self.ttl, self.cap, self.d = ttl, cap, {}

    def get(self, k, default=None):
        v = self.d.get(k)
        if v is not None and time.time() - v[0] < self.ttl:
            return v[1]
        self.d.pop(k, None)
        return default

    def peek(self, k, default=None):
        v = self.d.get(k)
        return v[1] if v is not None else default

    def set(self, k, val):
        if len(self.d) >= self.cap:
            self.d.pop(next(iter(self.d)), None)
        self.d[k] = (time.time(), val)

    def pop(self, k):
        self.d.pop(k, None)


# ---------------------------------------------------------------- user input state
STATE = {}


def set_state(uid, key, **data):
    STATE[uid] = {'k': key, 'd': data, 't': time.time()}
    if len(STATE) > 5000:
        STATE.pop(next(iter(STATE)), None)


def get_state(uid, ttl=900):
    s = STATE.get(uid)
    if s and time.time() - s['t'] > ttl:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


_RATE = {}


def rate_ok(uid, bucket='m', gap=1.0):
    t = time.time()
    k = (uid, bucket)
    if t - _RATE.get(k, 0) < gap:
        return False
    _RATE[k] = t
    if len(_RATE) > 8000:
        _RATE.clear()
    return True


# ---------------------------------------------------------------- telegram helpers
def _retry_secs(s):
    mt = re.search(r'retry after (\d+)', s)
    return int(mt.group(1)) + 1 if mt else 3


def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return fn(*a, **kw)
        except TypeError as e:
            mt = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if mt and mt.group(1) in kw:
                kw.pop(mt.group(1))
                continue
            raise
        except Exception as e:
            s = str(e).lower()
            if 'reply message not found' in s or 'message to be replied not found' in s:
                kw.pop('reply_to_message_id', None)
                kw.pop('reply_parameters', None)
                continue
            if 'retry after' in s:
                time.sleep(min(_retry_secs(s), 20))
                continue
            log(getattr(fn, '__name__', 'call'), str(e)[:200])
            return None
    return None


def btn(text, cb=None, url=None):
    if url:
        return types.InlineKeyboardButton(text, url=url)
    return types.InlineKeyboardButton(text, callback_data=cb)


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for r in rows:
        if r:
            m.row(*r)
    return m


def nav(back=None, home='home'):
    row = []
    if back:
        row.append(btn('◀️ Back', back))
    if home:
        row.append(btn('🏠 Home', home))
    return row


def say(chat_id, text, markup=None, reply_to=None, **kw):
    args = dict(reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    if reply_to:
        args['reply_to_message_id'] = reply_to
    args.update(kw)
    return _call(bot.send_message, chat_id, str(text)[:4096], **args)


def edit(target, text, markup=None):
    msg = getattr(target, 'message', None) or target
    try:
        bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup,
                              parse_mode='HTML', disable_web_page_preview=True)
        return True
    except TypeError:
        try:
            bot.edit_message_text(str(text)[:4096], msg.chat.id, msg.message_id, reply_markup=markup, parse_mode='HTML')
            return True
        except Exception as e:
            if 'not modified' in str(e).lower():
                return True
    except Exception as e:
        if 'not modified' in str(e).lower():
            return True
    return say(msg.chat.id, text, markup)


def ack(c, text=None, alert=False):
    try:
        bot.answer_callback_query(c.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        c._acked = True
    except Exception:
        pass


def delete_msg(chat_id, mid):
    try:
        bot.delete_message(chat_id, mid)
    except Exception:
        pass


def delete_later(chat_id, mids, secs):
    def run():
        for mid in mids:
            delete_msg(chat_id, mid)
    t = threading.Timer(secs, run)
    t.daemon = True
    t.start()


_ME = {}


def me():
    if 'u' not in _ME:
        _ME['u'] = bot.get_me()
    return _ME['u']


def bot_link(start=None):
    return 'https://t.me/%s%s' % (me().username, ('?start=' + start) if start else '')


_ADM = TTL(60, 3000)


def is_chat_admin(chat_id, uid, fresh=False):
    k = (chat_id, uid)
    if not fresh:
        v = _ADM.get(k)
        if v is not None:
            return v
    try:
        r = bot.get_chat_member(chat_id, uid).status in ('administrator', 'creator')
    except Exception:
        r = False
    _ADM.set(k, r)
    return r


def extract_media(m):
    if m.video:
        o = m.video
        return 'video', o.file_id, getattr(o, 'file_name', None) or 'video.mp4', o.file_size
    if m.animation:
        o = m.animation
        return 'animation', o.file_id, getattr(o, 'file_name', None) or 'animation.mp4', o.file_size
    if m.document:
        o = m.document
        return 'document', o.file_id, o.file_name or 'file', o.file_size
    if m.audio:
        o = m.audio
        return 'audio', o.file_id, o.file_name or o.title or 'audio', o.file_size
    if m.voice:
        return 'voice', m.voice.file_id, 'voice message', m.voice.file_size
    if m.video_note:
        return 'video_note', m.video_note.file_id, 'video note', m.video_note.file_size
    if m.photo:
        o = m.photo[-1]
        return 'photo', o.file_id, 'photo.jpg', o.file_size
    return None


MEDIA_TYPES = ['document', 'video', 'audio', 'voice', 'animation', 'video_note', 'photo']


def send_media(chat_id, kind, fid, caption=None, protect=False, markup=None):
    fns = {'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice,
           'animation': bot.send_animation, 'video_note': bot.send_video_note}
    fn = fns.get(kind, bot.send_document)
    kw = {}
    if caption and kind != 'video_note':
        kw['caption'] = caption
        kw['parse_mode'] = 'HTML'
    if protect:
        kw['protect_content'] = True
    if markup:
        kw['reply_markup'] = markup
    if kind == 'video':
        kw['supports_streaming'] = True
    return _call(fn, chat_id, fid, **kw)


# ---------------------------------------------------------------- callbacks & pagination
ROUTES = []


def route(prefix):
    def deco(fn):
        ROUTES.append((prefix, fn))
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def _on_callback(c):
    data = c.data or ''
    for prefix, fn in ROUTES:
        if data == prefix or data.startswith(prefix + ':'):
            try:
                fn(c, data.split(':')[1:])
            except Exception as e:
                log('callback error', data, repr(e), traceback.format_exc()[-400:])
                ack(c, '⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।')
            if not getattr(c, '_acked', False):
                ack(c)
            return
    ack(c, 'ℹ️ এই বাটনটি পুরনো হয়ে গেছে। /start দিন।', True)


def paginate(items, page, per=8):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def pager(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn('◀️', '%s:%d' % (prefix, page - 1)))
    row.append(btn('%d/%d' % (page + 1, pages), 'noop'))
    if page < pages - 1:
        row.append(btn('▶️', '%s:%d' % (prefix, page + 1)))
    return row


@route('noop')
def _noop(c, a):
    ack(c)


@route('home')
def _home(c, a):
    clear_state(c.from_user.id)
    ack(c)
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@bot.message_handler(commands=['cancel'])
def _cancel(m):
    clear_state(m.from_user.id)
    say(m.chat.id, '✖️ বাতিল করা হয়েছে।')
    if m.chat.type == 'private':
        t, k = home_view(m.from_user.id)
        say(m.chat.id, t, k)


# ---------------------------------------------------------------- network helpers
def http(url, data=None, headers=None, timeout=15, method=None, params=None, limit=6000000):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    h = {'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000)
        except Exception:
            body = b''
        return e.code, body


def http_json(url, **kw):
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode('utf-8', 'replace'))
    except Exception:
        return status, None


def start_worker(fn, every):
    def loop():
        while True:
            time.sleep(every)
            try:
                fn()
            except Exception as e:
                log('worker error', repr(e), traceback.format_exc()[-300:])
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run(commands=None, allowed=None):
    if commands:
        try:
            bot.set_my_commands([types.BotCommand(c, d) for c, d in commands])
        except Exception as e:
            log('set_my_commands', repr(e))
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log('bot started')
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=allowed)
        except Exception as e:
            log('polling crashed', repr(e))
            time.sleep(5)


DB = Store('file_links.json', {'owner': None, 'public': False, 'daily_limit': 10, 'daily': {}, 'files': {}, 'force': []})
EXPIRY = [(0, '♾ নেই'), (3600, '1 ঘণ্টা'), (86400, '24 ঘণ্টা'), (604800, '7 দিন'), (2592000, '30 দিন')]
MAXOPEN = [(0, '♾ আনলিমিটেড'), (1, '1 বার'), (10, '10 বার'), (100, '100 বার'), (1000, '1000 বার')]
AUTODEL = [(0, 'বন্ধ'), (60, '1 মিনিট'), (300, '5 মিনিট'), (600, '10 মিনিট')]
DAILY = [5, 10, 20, 50, 100]
EXP_NAME, MAX_NAME, AD_NAME = dict(EXPIRY), dict(MAXOPEN), dict(AUTODEL)
KIND_ICON = {'document': '📄', 'photo': '🖼', 'video': '🎬', 'audio': '🎵', 'voice': '🎤', 'animation': '🎞', 'video_note': '⭕'}
MAX_BATCH = 20
PWFAILS = {}


def is_owner(uid):
    return DB.d.get('owner') == uid


def can_upload(uid):
    return is_owner(uid) or DB.d['public']


def new_code():
    while True:
        code = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        if code not in DB.d['files']:
            return code


def link_for(code):
    return bot_link('f_%s' % code)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode(), 20000).hex()


def my_links(uid):
    return sorted([(k, v) for k, v in DB.d['files'].items() if v['by'] == uid], key=lambda kv: -kv[1]['at'])


def make_record(uid, items, title, kind):
    code = new_code()
    DB.d['files'][code] = {'kind': kind, 'items': items, 'title': title[:60], 'by': uid, 'at': time.time(), 'opens': 0, 'uniq': [], 'pw': '', 'salt': '',
                           'exp': None, 'max': 0, 'protect': False, 'autodel': 0}
    DB.save()
    return code


def daily_ok(uid):
    if is_owner(uid):
        return True
    today = fmt_ts(time.time(), 6.0, '%Y-%m-%d')
    for k in list(DB.d['daily'].keys()):
        if k != today:
            DB.d['daily'].pop(k, None)
    day = DB.d['daily'].setdefault(today, {})
    if day.get(str(uid), 0) >= DB.d['daily_limit']:
        return False
    day[str(uid)] = day.get(str(uid), 0) + 1
    DB.save()
    return True


def total_size(items):
    return sum((i.get('s') or 0) for i in items)


def flags(r):
    out = []
    if r['pw']:
        out.append('🔒')
    if r['exp']:
        out.append('⏳')
    if r['max']:
        out.append('🔢')
    if r['autodel']:
        out.append('🧨')
    if r['protect']:
        out.append('🚫')
    return ''.join(out)


def card(code):
    r = DB.d['files'][code]
    left = ''
    if r['exp']:
        left = '\n⏳ মেয়াদ বাকি: <b>%s</b>' % (fmt_dur(r['exp'] - time.time()) if r['exp'] > time.time() else 'শেষ')
    return ('%s <b>%s</b>\n' % ('📁' if r['kind'] == 'collection' else KIND_ICON.get(r['items'][0]['k'], '📄'), esc(r['title'])) + LINE +
            '\n📦 %d টি ফাইল • %s\n👁 খোলা হয়েছে: <b>%d</b> বার (👥 %d জন)\n🔒 পাসওয়ার্ড: %s\n🔢 সর্বোচ্চ ওপেন: %s%s\n🧨 অটো-ডিলিট: %s   🚫 ফরওয়ার্ড-ব্লক: %s\n\n🔗 <code>%s</code>'
            % (len(r['items']), human_size(total_size(r['items'])), r['opens'], len(r['uniq']), 'আছে' if r['pw'] else 'নেই', MAX_NAME.get(r['max'], '%d বার' % r['max']), left, AD_NAME.get(r['autodel'], '%d সেকেন্ড' % r['autodel']),
               'হ্যাঁ' if r['protect'] else 'না', link_for(code)))


def card_kb(code, back='ml'):
    r = DB.d['files'][code]
    return kb([btn('📤 Share', url='https://t.me/share/url?url=' + urllib.parse.quote(link_for(code))), btn('📷 QR', 'lx:qr:%s' % code)],
              [btn('🔒 Password' if not r['pw'] else '🔓 Password সরান', 'lx:pw:%s' % code if not r['pw'] else 'lx:pwx:%s' % code), btn('⏳ মেয়াদ: %s' % (fmt_dur(r['exp'] - time.time()) if r['exp'] and r['exp'] > time.time() else 'নেই'), 'lx:e:%s' % code)],
              [btn('🔢 %s' % MAX_NAME.get(r['max'], '%d বার' % r['max']), 'lx:m:%s' % code), btn('🧨 %s' % AD_NAME.get(r['autodel'], '%ds' % r['autodel']), 'lx:a:%s' % code)],
              [btn('🚫 Protect: %s' % ('ON' if r['protect'] else 'OFF'), 'lx:p:%s' % code), btn('✏️ Title', 'lx:t:%s' % code)],
              [btn('♻️ লিংক রিসেট', 'lx:rs:%s' % code), btn('🗑 ডিলিট', 'lx:d:%s' % code)], nav(back))


# ---------------------------------------------------------------- home
def home_view(uid):
    d = DB.d
    if not can_upload(uid):
        return '📁 <b>File to Link</b>\n' + LINE + '\nএই বটে শেয়ার করা লিংক দিয়ে ফাইল পেতে পারবেন।\nলিংকে ট্যাপ করুন, ফাইল চলে আসবে।' + FOOTER, kb()
    n = len(my_links(uid))
    text = ('📁 <b>File to Link</b>\n' + LINE + '\nযেকোনো ফাইল পাঠান — সাথে সাথে শেয়ারযোগ্য লিংক পাবেন।\n'
            '🔒 পাসওয়ার্ড • ⏳ মেয়াদ • 🔢 ওপেন-লিমিট • 🧨 অটো-ডিলিট • 🚫 ফরওয়ার্ড-ব্লক\n\n📋 আপনার লিংক: <b>%d</b>' % n)
    if is_owner(uid):
        text += '\n🌐 পাবলিক মোড: <b>%s</b>   🔔 Force Join: <b>%d</b>' % ('চালু' if d['public'] else 'বন্ধ', len(d['force']))
    rows = [[btn('📤 ফাইল আপলোড', 'lu'), btn('📁 Collection', 'lcs')], [btn('📋 My Links (%d)' % n, 'ml')]]
    if is_owner(uid):
        rows.append([btn('🔔 Force Join (%d)' % len(d['force']), 'fjm'), btn('🌐 Public: %s' % ('ON' if d['public'] else 'OFF'), 'pub')])
        if d['public']:
            rows.append([btn('📊 দৈনিক লিমিট: %d' % d['daily_limit'], 'dl')])
    rows.append([btn('❓ Help', 'lhelp')])
    return text + FOOTER, kb(*rows)


HELP = ('❓ <b>File to Link — সাহায্য</b>\n' + LINE + '\n'
        '📤 <b>আপলোড:</b> যেকোনো ফাইল/ছবি/ভিডিও/অডিও পাঠান → লিংক রেডি\n'
        '📁 <b>Collection:</b> একাধিক ফাইল এক লিংকে — বাটনে শুরু করে ফাইল পাঠান, তারপর ✅ Finish\n'
        '🔒 পাসওয়ার্ড • ⏳ মেয়াদ (1h–30d) • 🔢 ওপেন-লিমিট • 🧨 পাওয়ার পর চ্যাট থেকে অটো-ডিলিট • 🚫 ফরওয়ার্ড/সেভ ব্লক\n'
        '🔔 <b>Force Join:</b> ফাইল পেতে আপনার চ্যানেলে জয়েন বাধ্যতামূলক (বটকে চ্যানেলে অ্যাডমিন করুন)\n'
        '🌐 <b>Public Mode:</b> অন্যরাও আপলোড করতে পারবে (দৈনিক লিমিটসহ)\n'
        '♻️ লিংক রিসেট করলে পুরনো লিংক অচল হয়ে যায়\n\n'
        '⌨️ /mylinks · /del কোড · /public on|off · /stats')


@route('lhelp')
def cb_help(c, a):
    edit(c, HELP, kb(nav()))


@route('lu')
def cb_upload(c, a):
    if not can_upload(c.from_user.id):
        return ack(c, '🚫 আপলোড বন্ধ', True)
    ack(c)
    edit(c, '📤 <b>ফাইল আপলোড</b>\n\nযেকোনো ফাইল, ছবি, ভিডিও বা অডিও এখানে পাঠান — সাথে সাথে লিংক পাবেন।\n<i>একাধিক ফাইল এক লিংকে চাইলে 📁 Collection ব্যবহার করুন।</i>', kb([btn('📁 Collection', 'lcs')], nav()))


@route('pub')
def cb_public(c, a):
    if not is_owner(c.from_user.id):
        return ack(c, '🚫 শুধু Owner', True)
    DB.d['public'] = not DB.d['public']
    DB.save()
    ack(c, '🌐 পাবলিক মোড %s' % ('চালু' if DB.d['public'] else 'বন্ধ'))
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


@route('dl')
def cb_daily(c, a):
    if not is_owner(c.from_user.id):
        return ack(c, '🚫 শুধু Owner', True)
    cur = DB.d['daily_limit']
    DB.d['daily_limit'] = DAILY[(DAILY.index(cur) + 1) % len(DAILY)] if cur in DAILY else 10
    DB.save()
    ack(c, 'দৈনিক লিমিট: %d' % DB.d['daily_limit'])
    t, k = home_view(c.from_user.id)
    edit(c, t, k)


# ---------------------------------------------------------------- collections
def batch_view(d):
    lines = ['📁 <b>Collection তৈরি</b>\n' + LINE + '\nএকে একে ফাইল পাঠান। শেষ হলে ✅ Finish চাপুন।\n']
    for i, it in enumerate(d['items'], 1):
        lines.append('%d. %s %s' % (i, KIND_ICON.get(it['k'], '📄'), esc(it['n'][:36])))
    lines.append('\n📦 এখন: <b>%d/%d</b> • %s' % (len(d['items']), MAX_BATCH, human_size(total_size(d['items']))))
    rows = []
    if d['items']:
        rows.append([btn('✅ Finish (%d)' % len(d['items']), 'lcd'), btn('↩️ শেষটি বাদ', 'lcu')])
    rows.append([btn('✖ Cancel', 'home')])
    return '\n'.join(lines), kb(*rows)


@route('lcs')
def cb_batch_start(c, a):
    if not can_upload(c.from_user.id):
        return ack(c, '🚫 আপলোড বন্ধ', True)
    ack(c)
    set_state(c.from_user.id, 'batch', items=[])
    t, k = batch_view(STATE[c.from_user.id]['d'])
    edit(c, t, k)


@route('lcu')
def cb_batch_undo(c, a):
    s = get_state(c.from_user.id)
    if not s or s['k'] != 'batch':
        return ack(c, 'সেশন শেষ', True)
    if s['d']['items']:
        s['d']['items'].pop()
    ack(c)
    t, k = batch_view(s['d'])
    edit(c, t, k)


@route('lcd')
def cb_batch_done(c, a):
    uid = c.from_user.id
    s = get_state(uid)
    if not s or s['k'] != 'batch' or not s['d']['items']:
        return ack(c, 'আগে ফাইল পাঠান', True)
    if not daily_ok(uid):
        return ack(c, 'আজকের আপলোড লিমিট শেষ', True)
    items = s['d']['items']
    clear_state(uid)
    code = make_record(uid, items, 'Collection (%d ফাইল)' % len(items), 'collection')
    ack(c, '✅ লিংক তৈরি হয়েছে')
    edit(c, '✅ <b>Collection লিংক তৈরি!</b>\n\n' + card(code), card_kb(code))


# ---------------------------------------------------------------- my links & manage
@route('ml')
def cb_list(c, a):
    uid = c.from_user.id
    ack(c)
    clear_state(uid)
    ls = my_links(uid)
    if not ls:
        return edit(c, '📋 <b>My Links</b>\n\nএখনো কোনো লিংক নেই। ফাইল পাঠান!', kb([btn('📤 আপলোড', 'lu')], nav()))
    chunk, page, pages = paginate(ls, to_int(a[0], 0) if a else 0, 6)
    rows = [[btn('%s %s • 👁%d %s' % ('📁' if v['kind'] == 'collection' else '🔗', v['title'][:22], v['opens'], flags(v)), 'lv:%s:%d' % (k, page))] for k, v in chunk]
    pg = pager('ml', page, pages)
    if pg:
        rows.append(pg)
    rows.append(nav())
    edit(c, '📋 <b>My Links (%d)</b>\n<i>🔒 পাসওয়ার্ড • ⏳ মেয়াদ • 🔢 লিমিট • 🧨 অটো-ডিলিট • 🚫 প্রটেক্ট</i>' % len(ls), kb(*rows))


@route('lv')
def cb_view(c, a):
    code = a[0] if a else ''
    r = DB.d['files'].get(code)
    if not r or (r['by'] != c.from_user.id and not is_owner(c.from_user.id)):
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    ack(c)
    edit(c, card(code), card_kb(code, 'ml:%s' % (a[1] if len(a) > 1 else '0')))


def cycle(vals, cur):
    return vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]


@route('lx')
def cb_manage(c, a):
    uid = c.from_user.id
    if len(a) < 2:
        return ack(c)
    act, code = a[0], a[1]
    r = DB.d['files'].get(code)
    if not r or (r['by'] != uid and not is_owner(uid)):
        return ack(c, 'লিংকটি পাওয়া যায়নি', True)
    if act == 'e':
        vals = [v for v, _ in EXPIRY]
        cur = 0
        if r['exp']:
            left = r['exp'] - time.time()
            cur = min([v for v in vals if v], key=lambda v: abs(v - left)) if left > 0 else 0
        nxt = cycle(vals, cur)
        r['exp'] = time.time() + nxt if nxt else None
    elif act == 'm':
        r['max'] = cycle([v for v, _ in MAXOPEN], r['max'])
    elif act == 'a':
        r['autodel'] = cycle([v for v, _ in AUTODEL], r['autodel'])
    elif act == 'p':
        r['protect'] = not r['protect']
    elif act == 'pw':
        ack(c)
        set_state(uid, 'setpw', code=code)
        return say(c.message.chat.id, '🔒 <b>পাসওয়ার্ড লিখুন</b> (কমপক্ষে ৩ অক্ষর)।\n<i>মেসেজটি পাঠানোর পর মুছে ফেলা হবে।</i>', kb(nav('lv:%s' % code)))
    elif act == 'pwx':
        r['pw'], r['salt'] = '', ''
    elif act == 't':
        ack(c)
        set_state(uid, 'title', code=code)
        return say(c.message.chat.id, '✏️ <b>নতুন টাইটেল</b> লিখুন:', kb(nav('lv:%s' % code)))
    elif act == 'qr':
        ack(c, '⏳ QR বানানো হচ্ছে...')
        try:
            st, body = http('https://api.qrserver.com/v1/create-qr-code/', params={'size': '500x500', 'margin': 10, 'data': link_for(code)}, timeout=20)
        except Exception:
            st, body = 0, b''
        if st != 200 or not body:
            return say(c.message.chat.id, '⚠️ QR বানানো যায়নি, পরে চেষ্টা করুন।')
        bio = io.BytesIO(body)
        bio.name = 'qr.png'
        return _call(bot.send_photo, c.message.chat.id, bio, caption='📷 <b>%s</b>\n<code>%s</code>' % (esc(r['title']), link_for(code)), parse_mode='HTML')
    elif act == 'rs':
        return edit(c, '♻️ <b>লিংক রিসেট করবেন?</b>\nনতুন লিংক তৈরি হবে এবং পুরনো লিংক আর কাজ করবে না।', kb([btn('✅ হ্যাঁ', 'lx:rsy:%s' % code), btn('✖ না', 'lv:%s' % code)]))
    elif act == 'rsy':
        DB.d['files'][new_code_for(code)] = DB.d['files'].pop(code)
        DB.save()
        ack(c, '♻️ নতুন লিংক তৈরি হয়েছে')
        newc = [k for k, v in DB.d['files'].items() if v is r][0]
        return edit(c, card(newc), card_kb(newc))
    elif act == 'd':
        return edit(c, '🗑 <b>"%s" ডিলিট করবেন?</b>\nলিংক আর কাজ করবে না।' % esc(r['title']), kb([btn('✅ হ্যাঁ, ডিলিট', 'lx:dy:%s' % code), btn('✖ না', 'lv:%s' % code)]))
    elif act == 'dy':
        DB.d['files'].pop(code, None)
        DB.save()
        ack(c, '🗑 ডিলিট হয়েছে')
        return cb_list(c, [])
    DB.save()
    ack(c, '✅ আপডেট হয়েছে')
    edit(c, card(code), card_kb(code))


def new_code_for(old):
    return new_code()


# ---------------------------------------------------------------- force join
def fj_view():
    rows = [[btn('🗑 %s' % f['title'][:28], 'fjm:x:%d' % i)] for i, f in enumerate(DB.d['force'])]
    if len(DB.d['force']) < 3:
        rows.append([btn('➕ চ্যানেল যোগ করুন', 'fjm:add')])
    rows.append(nav())
    return ('🔔 <b>Force Join</b>\n' + LINE + '\nলিংক থেকে ফাইল পাওয়ার আগে এই চ্যানেলগুলোতে জয়েন করতে হবে (সর্বোচ্চ ৩টি)।\n<i>বটকে সেই চ্যানেলে অ্যাডমিন করতে হবে এবং চ্যানেলের পাবলিক @username থাকতে হবে।</i>\n\n' +
            ('\n'.join('• %s' % esc(f['title']) for f in DB.d['force']) or 'কোনো চ্যানেল সেট করা নেই।')), kb(*rows)


@route('fjm')
def cb_force(c, a):
    if not is_owner(c.from_user.id):
        return ack(c, '🚫 শুধু Owner', True)
    if a and a[0] == 'add':
        ack(c)
        set_state(c.from_user.id, 'fj_add')
        return edit(c, '🔔 <b>চ্যানেল যোগ</b>\n\nচ্যানেলের <code>@username</code> পাঠান অথবা সেখান থেকে একটি পোস্ট এখানে ফরওয়ার্ড করুন।', kb(nav('fjm')))
    if len(a) > 1 and a[0] == 'x':
        i = to_int(a[1], -1)
        if 0 <= i < len(DB.d['force']):
            DB.d['force'].pop(i)
            DB.save()
        ack(c, '🗑 সরানো হয়েছে')
    else:
        ack(c)
    t, k = fj_view()
    edit(c, t, k)


def joined(uid):
    missing = []
    for f in DB.d['force']:
        try:
            st = bot.get_chat_member(f['id'], uid).status
        except Exception:
            st = 'member'    # cannot verify -> do not block users
        if st not in ('member', 'administrator', 'creator', 'restricted'):
            missing.append(f)
    return missing


@route('fj')
def cb_recheck(c, a):
    code = a[0] if a else ''
    if joined(c.from_user.id):
        return ack(c, '❌ এখনো সব চ্যানেলে জয়েন করেননি', True)
    ack(c, '✅ ধন্যবাদ!')
    gate(c.message.chat.id, c.from_user, code)


# ---------------------------------------------------------------- delivery
def gate(chat_id, user, code):
    uid = user.id
    r = DB.d['files'].get(code)
    if not r:
        return say(chat_id, '❌ <b>লিংকটি পাওয়া যায়নি</b> বা মুছে ফেলা হয়েছে।', kb(nav()))
    if r['exp'] and time.time() > r['exp']:
        DB.d['files'].pop(code, None)
        DB.save()
        return say(chat_id, '⌛ এই লিংকের মেয়াদ শেষ হয়ে গেছে।', kb(nav()))
    if r['max'] and r['opens'] >= r['max'] and not is_owner(uid):
        return say(chat_id, '⚠️ এই লিংকের ওপেন-লিমিট শেষ হয়ে গেছে।', kb(nav()))
    if DB.d['force'] and not is_owner(uid):
        missing = joined(uid)
        if missing:
            rows = [[btn('📢 %s' % f['title'][:26], url=f['link'])] for f in missing]
            rows.append([btn('✅ জয়েন করেছি', 'fj:%s' % code)])
            return say(chat_id, '🔔 <b>ফাইল পেতে আগে নিচের চ্যানেলে জয়েন করুন</b>, তারপর "✅ জয়েন করেছি" চাপুন।', kb(*rows))
    if r['pw'] and uid != r['by']:
        fl = PWFAILS.get((uid, code), [0, 0])
        if fl[1] > time.time():
            return say(chat_id, '⛔ অনেকবার ভুল পাসওয়ার্ড। %s পরে চেষ্টা করুন।' % fmt_dur(fl[1] - time.time()))
        set_state(uid, 'fpw', code=code)
        return say(chat_id, '🔒 <b>এই ফাইলটি পাসওয়ার্ড-সুরক্ষিত</b>\nপাসওয়ার্ড লিখে পাঠান:', kb(nav()))
    deliver(chat_id, user, code)


def deliver(chat_id, user, code):
    r = DB.d['files'].get(code)
    if not r:
        return
    uid = user.id
    r['opens'] += 1
    if uid not in r['uniq'] and len(r['uniq']) < 3000:
        r['uniq'].append(uid)
    DB.save()
    sent = []
    n = len(r['items'])
    for i, it in enumerate(r['items']):
        cap = ('📁 <b>%s</b>' % esc(r['title'])) if (n == 1 or i == 0) and r['title'] != it['n'] else None
        res = send_media(chat_id, it['k'], it['f'], cap, r['protect'])
        if res is not None:
            sent.append(res.message_id)
        if n > 3:
            time.sleep(0.3)
    info = []
    if r['autodel'] and sent:
        delete_later(chat_id, sent, r['autodel'])
        info.append('🧨 ফাইলগুলো %s পরে এই চ্যাট থেকে মুছে যাবে — এখনই সেভ করে নিন।' % AD_NAME.get(r['autodel'], fmt_dur(r['autodel'])))
    if r['max']:
        info.append('🔢 বাকি ওপেন: %d' % max(0, r['max'] - r['opens']))
    if info:
        say(chat_id, '\n'.join(info))


def cleanup_tick():
    now_t = time.time()
    dead = [k for k, v in DB.d['files'].items() if v['exp'] and now_t > v['exp']]
    for k in dead:
        DB.d['files'].pop(k, None)
    if dead:
        DB.save()


# ---------------------------------------------------------------- commands
@bot.message_handler(commands=['start', 'menu'])
def start(m):
    uid = m.from_user.id
    clear_state(uid)
    if m.chat.type != 'private':
        return
    arg = cmd_arg(m)
    if DB.d['owner'] is None and not arg.startswith('f_'):
        DB.d['owner'] = uid
        DB.save()
        t, k = home_view(uid)
        return say(m.chat.id, '👑 <b>আপনি এই বটের Owner হয়ে গেছেন!</b>\nফাইল পাঠান — সাথে সাথে শেয়ারযোগ্য লিংক পাবেন।\n\n' + t, k)
    if arg.startswith('f_'):
        return gate(m.chat.id, m.from_user, arg[2:])
    t, k = home_view(uid)
    say(m.chat.id, t, k)


@bot.message_handler(commands=['help'])
def help_cmd(m):
    say(m.chat.id, HELP, kb(nav()))


@bot.message_handler(commands=['mylinks'])
def mylinks_cmd(m):
    if not can_upload(m.from_user.id):
        return
    ls = my_links(m.from_user.id)
    if not ls:
        return say(m.chat.id, 'এখনো কোনো লিংক নেই। ফাইল পাঠান!')
    lines = ['📋 <b>আপনার লিংক</b>\n']
    for k, v in ls[:15]:
        lines.append('• %s — 👁%d\n  <code>%s</code>' % (esc(v['title'][:30]), v['opens'], link_for(k)))
    say(m.chat.id, '\n'.join(lines), kb([btn('📋 ম্যানেজ করুন', 'ml')]))


@bot.message_handler(commands=['del'])
def del_cmd(m):
    arg = cmd_arg(m).strip()
    code = arg[2:] if arg.startswith('f_') else arg
    r = DB.d['files'].get(code)
    if not code or not r or (r['by'] != m.from_user.id and not is_owner(m.from_user.id)):
        return say(m.chat.id, 'ব্যবহার: <code>/del কোড</code> (লিংকের শেষের f_ এর পরের অংশ)')
    DB.d['files'].pop(code, None)
    DB.save()
    say(m.chat.id, '🗑 লিংক মুছে ফেলা হয়েছে।')


@bot.message_handler(commands=['public'])
def public_cmd(m):
    if not is_owner(m.from_user.id):
        return
    arg = cmd_arg(m).lower()
    if arg not in ('on', 'off'):
        return say(m.chat.id, 'ব্যবহার: <code>/public on</code> বা <code>/public off</code>\nবর্তমান: %s' % ('চালু' if DB.d['public'] else 'বন্ধ'))
    DB.d['public'] = arg == 'on'
    DB.save()
    say(m.chat.id, ('🌐 পাবলিক মোড চালু — যে কেউ আপলোড করতে পারবে (দিনে %d টি পর্যন্ত)।' % DB.d['daily_limit']) if DB.d['public'] else '🔒 পাবলিক মোড বন্ধ — শুধু আপনি আপলোড করতে পারবেন।')


@bot.message_handler(commands=['stats'])
def stats_cmd(m):
    if not is_owner(m.from_user.id):
        return
    total = sum(v['opens'] for v in DB.d['files'].values())
    say(m.chat.id, '📊 <b>পরিসংখ্যান</b>\n━━━━━━━━━━━━━━\n🔗 মোট লিংক: <b>%d</b>\n👁 মোট ওপেন: <b>%d</b>\n🌐 পাবলিক মোড: %s' % (len(DB.d['files']), total, 'চালু' if DB.d['public'] else 'বন্ধ'))


# ---------------------------------------------------------------- uploads & text
def forwarded_chat(m):
    fo = getattr(m, 'forward_origin', None)
    ch = getattr(fo, 'chat', None) if fo else None
    return ch or getattr(m, 'forward_from_chat', None)


@bot.message_handler(func=lambda m: m.chat.type == 'private', content_types=MEDIA_TYPES + ['text'])
def on_any(m):
    uid = m.from_user.id
    s = get_state(uid)
    txt = (m.text or '').strip()
    if txt.startswith('/'):
        return
    if s and s['k'] == 'fj_add' and is_owner(uid):
        fc = forwarded_chat(m)
        ref = fc.id if fc is not None else (txt if txt.startswith('@') else ('@' + txt if txt else None))
        if ref is None:
            return
        try:
            ch = bot.get_chat(ref)
            bst = bot.get_chat_member(ch.id, me().id).status
        except Exception:
            return say(m.chat.id, '⚠️ চ্যানেলটি খুঁজে পাইনি বা বট সেখানে নেই। বটকে অ্যাডমিন করে আবার পাঠান।')
        if bst not in ('administrator', 'creator'):
            return say(m.chat.id, '⚠️ বট এই চ্যানেলে অ্যাডমিন নয়।')
        uname = getattr(ch, 'username', None)
        if not uname:
            return say(m.chat.id, '⚠️ চ্যানেলের পাবলিক @username নেই — জয়েন বাটন বানানো যাবে না। আগে @username সেট করুন।')
        if not any(f['id'] == ch.id for f in DB.d['force']) and len(DB.d['force']) < 3:
            DB.d['force'].append({'id': ch.id, 'title': (ch.title or uname)[:40], 'link': 'https://t.me/%s' % uname})
            DB.save()
        clear_state(uid)
        t, k = fj_view()
        return say(m.chat.id, '✅ যোগ হয়েছে।\n\n' + t, k)
    if txt and s:
        key, d = s['k'], s['d']
        if key == 'fpw':
            delete_msg(m.chat.id, m.message_id)
            clear_state(uid)
            r = DB.d['files'].get(d['code'])
            if not r:
                return say(m.chat.id, '❌ লিংকটি পাওয়া যায়নি।')
            fk = (uid, d['code'])
            fl = PWFAILS.get(fk, [0, 0])
            if fl[1] > time.time():
                return say(m.chat.id, '⛔ অনেকবার ভুল পাসওয়ার্ড। %s পরে চেষ্টা করুন।' % fmt_dur(fl[1] - time.time()))
            if hash_pw(txt, r['salt']) != r['pw']:
                fl[0] += 1
                if fl[0] >= 5:
                    fl = [0, time.time() + 600]
                PWFAILS[fk] = fl
                return say(m.chat.id, '❌ ভুল পাসওয়ার্ড।' + (' ⛔ ১০ মিনিটের জন্য ব্লক।' if fl[1] > time.time() else ' আবার চেষ্টা করুন:'))
            PWFAILS.pop(fk, None)
            return deliver(m.chat.id, m.from_user, d['code'])
        if key == 'setpw':
            delete_msg(m.chat.id, m.message_id)
            r = DB.d['files'].get(d['code'])
            if len(txt) < 3:
                return say(m.chat.id, '⚠️ কমপক্ষে ৩ অক্ষর। আবার লিখুন:')
            clear_state(uid)
            if not r or (r['by'] != uid and not is_owner(uid)):
                return say(m.chat.id, '⚠️ লিংকটি পাওয়া যায়নি।')
            r['salt'] = secrets.token_hex(8)
            r['pw'] = hash_pw(txt, r['salt'])
            DB.save()
            return say(m.chat.id, '🔒 পাসওয়ার্ড সেট হয়েছে (আপনার মেসেজটি মুছে দেওয়া হয়েছে)।', kb([btn('🔗 লিংক দেখুন', 'lv:%s' % d['code'])]))
        if key == 'title':
            r = DB.d['files'].get(d['code'])
            clear_state(uid)
            if r and (r['by'] == uid or is_owner(uid)):
                r['title'] = txt[:60]
                DB.save()
            return say(m.chat.id, '✅ টাইটেল বদলানো হয়েছে।', kb([btn('🔗 লিংক দেখুন', 'lv:%s' % d['code'])]))
    media = extract_media(m)
    if not media:
        if DB.d['owner'] is None or not txt:
            return
        t, k = home_view(uid)
        return say(m.chat.id, '💡 ফাইল পাঠান, অথবা বাটন ব্যবহার করুন:\n\n' + t, k)
    if not can_upload(uid):
        return say(m.chat.id, '🔒 শুধু Owner ফাইল আপলোড করতে পারবেন।')
    kind, fid, name, size = media
    item = {'k': kind, 'f': fid, 'n': (name or 'file')[:60], 's': size or 0}
    if s and s['k'] == 'batch':
        if len(s['d']['items']) >= MAX_BATCH:
            return say(m.chat.id, '⚠️ সর্বোচ্চ %d টি ফাইল। ✅ Finish চাপুন।' % MAX_BATCH)
        s['d']['items'].append(item)
        t, k = batch_view(s['d'])
        return say(m.chat.id, t, k)
    if not daily_ok(uid):
        return say(m.chat.id, '⚠️ আজকের আপলোড লিমিট (%d) শেষ। কাল আবার চেষ্টা করুন।' % DB.d['daily_limit'])
    code = make_record(uid, [item], (m.caption or name or 'file')[:60], 'file')
    say(m.chat.id, '✅ <b>লিংক তৈরি হয়েছে!</b>\n\n' + card(code), card_kb(code))


start_worker(cleanup_tick, 60)
run(commands=[('start', 'মেনু খুলুন'), ('mylinks', 'আপনার লিংক'), ('del', 'লিংক মুছুন'), ('public', 'পাবলিক আপলোড মোড'), ('stats', 'পরিসংখ্যান'),
              ('help', 'সাহায্য'), ('cancel', 'চলমান ধাপ বাতিল')])
''',
    },
}

MARKETPLACE_INTRO = (
    "🛒 <b>PyHost Bot Store</b>\n━━━━━━━━━━━━━━━━━━\n"
    "কোনো কোডিং ছাড়াই ১-ক্লিকে রেডিমেড বট ডেপ্লয় করুন!\n"
    "নিচের যেকোনো বট বেছে নিন:"
)

def build_marketplace_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [make_inline_button(f"{t['emoji']} {t['name']}", callback_data=f"mp_view_{key}", style="primary") for key, t in MARKETPLACE_TEMPLATES.items()]
    markup.add(*buttons)
    markup.add(
        make_inline_button("🌐 Community Bots", callback_data="cm_list_0", style="success"),
        make_inline_button("📤 Submit Your Bot", callback_data="cm_submit", style="success")
    )
    add_home_button(markup)
    return markup

def build_marketplace_card(key):
    t = MARKETPLACE_TEMPLATES[key]
    steps = "\n".join(f"{i}️⃣ {s}" for i, s in enumerate(t["steps"], 1))
    feats = "\n".join(f"  ✅ {f}" for f in t.get("features", []))
    msg = (
        f"{t['emoji']} <b>{t['name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🗂️ <b>Category:</b> {t.get('category', '🤖 Bot')}\n"
        f"🎚️ <b>Level:</b> {t.get('level', '⭐ Beginner')}   ⏱️ <b>Setup:</b> {t.get('setup', '~30 sec')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 {t['about']}\n\n"
        + (f"✨ <b>Key Features:</b>\n{feats}\n\n" if feats else "")
        + (f"⌨️ <b>Commands:</b> <code>{html.escape(t['commands'])}</code>\n\n" if t.get("commands") else "")
        + f"🚀 <b>টোকেন দিলে যা হবে:</b>\n{steps}\n\n"
        f"🎬 <b>Demo Preview:</b>\n<pre>{html.escape(t['preview'])}</pre>\n"
        f"📌 <b>প্রয়োজন:</b> {t['need']}\n"
        "🔄 <i>ডিপ্লয়ের পর কোড আপনার File Manager থেকে এডিট করা যাবে।</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(make_inline_button("🚀 Deploy Now", callback_data=f"deploy_tpl_{key}", style="success"))
    markup.add(
        make_inline_button("🔑 Get Bot Token", callback_data="faq_get_token", style="primary"),
        make_inline_button("◀️ Marketplace", callback_data="open_mp_menu", style="primary")
    )
    return msg, markup

# ======================== 🌐 COMMUNITY MARKETPLACE + মার্কেটপ্লেস ডিপ্লয় হেল্পার ========================
def get_marketplace_template(key: str):
    """বিল্ট-ইন টেমপ্লেট (key) অথবা অ্যাপ্রুভড কমিউনিটি টেমপ্লেট (cm_<id>) রিটার্ন করে।"""
    if key in MARKETPLACE_TEMPLATES:
        return MARKETPLACE_TEMPLATES[key]
    if key.startswith("cm_") and key[3:].isdigit():
        t = admin.get_community_template(int(key[3:]))
        if t and t["status"] == "APPROVED":
            return {"name": t["name"], "proj_name": t["name"][:30], "code": t["code"], "inputs": []}
        return None
    return MARKETPLACE_TEMPLATES.get("echo")

def _mp_slot_ok(chat_id: int, user_id: int) -> bool:
    """মার্কেটপ্লেস ডিপ্লয়ের আগে স্লট-লিমিট চেক। পূর্ণ থাকলে ইউজারকে জানিয়ে False দেয়।"""
    u_data = admin.get_user(user_id)
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    projects = admin.get_user_projects(user_id)
    if len(projects) >= max_bots:
        bot.send_message(
            chat_id,
            "⚠️ <b>আপনার বটের স্লট লিমিট পূর্ণ হয়ে গেছে!</b>\n"
            "মার্কেটপ্লেস থেকে বট ডিপ্লয় করতেও ফাঁকা স্লট লাগবে — 💎 <b>Upgrade Plan</b> থেকে "
            "প্ল্যান আপগ্রেড করুন অথবা কোনো একটি বট ডিলিট করে জায়গা খালি করুন।",
            parse_mode="HTML"
        )
        return False
    # সব ১০টা স্লট (হার্ড ক্যাপ) পূর্ণ থাকলে slot_number=None দিয়ে "ভুতুড়ে" প্রজেক্ট তৈরি হয়ে যেত
    if admin.get_next_free_slot(user_id) is None:
        bot.send_message(chat_id, "⚠️ <b>আপনার সবগুলো বট স্লট (সর্বোচ্চ ১০টি) পূর্ণ!</b> নতুন বট ডিপ্লয় করার আগে একটি ডিলিট করুন।", parse_mode="HTML")
        return False
    return True

def finish_marketplace_deploy(message, user_id: int, template_type: str, bot_token: str, extras: dict = None):
    """টোকেন (ও দরকার হলে এক্সট্রা ইনপুট যেমন API Key) পাওয়ার পর টেমপ্লেট ফাইল বানিয়ে বট রান করে।"""
    extras = extras or {}
    if not _mp_slot_ok(message.chat.id, user_id):
        return

    tpl_def = get_marketplace_template(template_type)
    if tpl_def is None:
        bot.send_message(message.chat.id, "❌ <b>এই টেমপ্লেটটি আর পাওয়া যাচ্ছে না।</b> Marketplace থেকে অন্য একটি বেছে নিন।", parse_mode="HTML")
        return

    status_msg = bot.send_message(message.chat.id, "⏳ <b>বটের ফাইল তৈরি ও লাইভ পরিবেশ সেটআপ হচ্ছে...</b>", parse_mode="HTML")

    proj_name = tpl_def["proj_name"]
    bot_code = tpl_def["code"]
    for placeholder, value in extras.items():
        bot_code = bot_code.replace(placeholder, value)
    bot_code = bot_code.replace("__TOKEN__", bot_token)

    pid = admin.create_user_project(user_id, proj_name)
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    main_path = os.path.join(p_dir, "main.py")
    with open(main_path, "w", encoding="utf-8") as f:
        f.write(bot_code)

    # ⭐ কিছু অ্যাডভান্স টেমপ্লেটে telebot ছাড়াও বাড়তি লাইব্রেরি লাগে (যেমন Weather/AI Bot-এ
    # requests) — তাই রান করার আগে ইমপোর্ট স্ক্যান করে দরকারি প্যাকেজ ইনস্টল করে নেওয়া হয়।
    detected = scan_python_imports(main_path)
    if detected:
        bot.edit_message_text("📦 <b>দরকারি প্যাকেজ ইনস্টল হচ্ছে...</b>", message.chat.id, status_msg.message_id, parse_mode="HTML")
        install_missing_packages_sync(detected)

    ok_run, run_msg = execute_project_script(user_id, pid, "main.py")
    if not ok_run:
        admin.delete_user_project(pid)
        bot.edit_message_text(
            f"{run_msg}\n\n(কোনো স্লট নষ্ট হয়নি।)",
            message.chat.id, status_msg.message_id, parse_mode="HTML"
        )
        return

    # 🌐 কমিউনিটি টেমপ্লেট সফলভাবে চালু হলে প্রস্তুতকারককে কমিশন (ইউনিক ডেপ্লয়ার প্রতি একবার)
    if template_type.startswith("cm_") and template_type[3:].isdigit():
        try:
            creator_id, commission = admin.record_community_deploy(int(template_type[3:]), user_id)
            if creator_id and commission > 0:
                bot.send_message(
                    creator_id,
                    f"🎉 <b>আপনার কমিউনিটি বট ডেপ্লয় হয়েছে!</b>\n"
                    f"🧩 <b>{html.escape(tpl_def['name'])}</b>\n"
                    f"💰 কমিশন: <b>+{commission:g} Credits</b> (My Account-এ দেখুন)",
                    parse_mode="HTML"
                )
        except Exception:
            pass

    bot.edit_message_text(
        f"🎉 <b>অভিনন্দন! আপনার '{html.escape(proj_name)}' সফলভাবে ডেপ্লয় হয়েছে!</b>\n\n"
        f"🚀 বটটি এখন লাইভ চলছে। 🎛️ <b>Control Panel</b> থেকে এটি পরিচালনা করতে পারবেন।",
        message.chat.id,
        status_msg.message_id,
        parse_mode="HTML"
    )

# ---------- কমিউনিটি: ইউজার UI বিল্ডার ----------
CM_PAGE_SIZE = 6

def _cm_creator_label(t: dict) -> str:
    n = (t.get("creator_name") or "").strip()
    return f"@{html.escape(n)}" if n and n != "Anonymous" else "Community Dev"

def build_community_list(page: int = 0):
    rows, total = admin.list_community_templates("APPROVED", CM_PAGE_SIZE, page * CM_PAGE_SIZE)
    pages = max(1, (total + CM_PAGE_SIZE - 1) // CM_PAGE_SIZE)
    text = (
        "🌐 <b>Community Bots</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "অন্য ডেভেলপারদের বানানো বট — অ্যাডমিন রিভিউ করে অ্যাপ্রুভ করা।\n"
        "টোকেন দিলেই ১-ক্লিকে ডেপ্লয়।"
    )
    if not rows:
        text += "\n\n<i>এখনো কোনো কমিউনিটি বট নেই। আপনার বট সাবমিট করে প্রথম হয়ে যান!</i>"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        markup.add(make_inline_button(f"🧩 {r['name'][:26]} • 🚀 {r['deploys']}", callback_data=f"cm_view_{r['id']}", style="primary"))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(make_inline_button("◀️ Prev", callback_data=f"cm_list_{page - 1}", style="primary"))
        nav.append(make_inline_button(f"{page + 1}/{pages}", callback_data="cm_list_%d" % page, style="primary"))
        if page < pages - 1:
            nav.append(make_inline_button("Next ▶️", callback_data=f"cm_list_{page + 1}", style="primary"))
        markup.row(*nav)
    markup.add(make_inline_button("📤 Submit Your Bot", callback_data="cm_submit", style="success"))
    markup.add(make_inline_button("◀️ Marketplace", callback_data="open_mp_menu", style="primary"))
    return text, markup

def build_community_card(tid: int):
    t = admin.get_community_template(tid)
    if not t or t["status"] != "APPROVED":
        return None, None
    msg = (
        f"🧩 <b>{html.escape(t['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Developer:</b> {_cm_creator_label(t)}\n"
        f"🚀 <b>Deploys:</b> {t['deploys']}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 {html.escape(t['description'] or '')}\n\n"
        "📌 <b>প্রয়োজন:</b> বট টোকেন (@BotFather থেকে)\n"
        "🛡️ <i>কমিউনিটি বট — অ্যাডমিন রিভিউ করেছেন, তবুও ডেপ্লয়ের পর File Manager থেকে কোড দেখে নিতে পারেন।</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(make_inline_button("🚀 Deploy Now", callback_data=f"cm_deploy_{tid}", style="success"))
    markup.add(
        make_inline_button("🔑 Get Bot Token", callback_data="faq_get_token", style="primary"),
        make_inline_button("◀️ Community", callback_data="cm_list_0", style="primary")
    )
    return msg, markup

def build_community_submit_info(user_id: int):
    commission = admin.SYSTEM_CONFIG.get("community_commission", 1.0)
    mine = admin.list_user_templates(user_id)
    earned = sum(r["earned"] for r in mine)
    text = (
        "📤 <b>আপনার বট Marketplace-এ সাবমিট করুন</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"অন্য কেউ আপনার বট ডেপ্লয় করলে আপনি পাবেন <b>{commission:g} Credits</b> কমিশন "
        "(প্রতিটি আলাদা ইউজারের প্রথম ডেপ্লয়ে একবার)।\n\n"
        "<b>নিয়ম:</b>\n"
        "1️⃣ একটাই ফাইল (<code>main.py</code>) — .py ফাইল আপলোড অথবা কোড পেস্ট করুন (সর্বোচ্চ ১০০ KB)\n"
        "2️⃣ বটের টোকেনের জায়গায় লিখুন <code>BOT_TOKEN = '__TOKEN__'</code> — আসল টোকেন কোডে রাখবেন না\n"
        "3️⃣ দরকারি লাইব্রেরি ইমপোর্ট থেকে অটো ইনস্টল হবে\n"
        "4️⃣ অ্যাডমিন কোড রিভিউ করে অ্যাপ্রুভ করলে লাইভ হবে; ক্ষতিকর/সন্দেহজনক কোড রিজেক্ট হবে\n"
        f"5️⃣ একসাথে সর্বোচ্চ {admin.COMMUNITY_MAX_PENDING_PER_USER}টি রিভিউয়ের অপেক্ষায় থাকতে পারবে\n"
    )
    if mine:
        text += f"\n📊 আপনার সাবমিশন: <b>{len(mine)}</b>টি • মোট আয়: <b>{earned:g} Credits</b>"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("✍️ নতুন বট সাবমিট করুন", callback_data="cm_submit_go", style="success"))
    if mine:
        markup.add(make_inline_button("📋 My Submissions", callback_data="cm_mine", style="primary"))
    markup.add(make_inline_button("◀️ Community Bots", callback_data="cm_list_0", style="primary"))
    return text, markup

CM_STATUS_ICON = {"PENDING": "⏳ Pending", "APPROVED": "✅ Live", "REJECTED": "❌ Rejected"}

def build_my_submissions(user_id: int):
    rows = admin.list_user_templates(user_id)
    lines = ["📋 <b>My Submissions</b>", "━━━━━━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("<i>এখনো কিছু সাবমিট করেননি।</i>")
    for r in rows:
        line = f"• <b>{html.escape(r['name'])}</b> — {CM_STATUS_ICON.get(r['status'], r['status'])}"
        if r["status"] == "APPROVED":
            line += f"\n   🚀 {r['deploys']} deploys • 💰 {r['earned']:g} Credits"
        elif r["status"] == "REJECTED" and r.get("reject_reason"):
            line += f"\n   📝 {html.escape(r['reject_reason'])}"
        lines.append(line)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("◀️ Back", callback_data="cm_submit", style="primary"))
    return "\n".join(lines), markup

def validate_community_code(code: str):
    """সাবমিট করা কোড বেসিক যাচাই। রিটার্ন (ok, error_message)।"""
    if not code or not code.strip():
        return False, "কোড খালি।"
    if len(code.encode("utf-8")) > admin.COMMUNITY_MAX_CODE_BYTES:
        return False, "কোড ১০০ KB-র বেশি বড়।"
    if "__TOKEN__" not in code:
        return False, "কোডে <code>'__TOKEN__'</code> প্লেসহোল্ডার নেই — যেমন: <code>BOT_TOKEN = '__TOKEN__'</code>"
    if re.search(r"\d{8,10}:[a-zA-Z0-9_-]{35}", code):
        return False, "কোডে আসল বট টোকেন পাওয়া গেছে! এটি সরিয়ে <code>'__TOKEN__'</code> ব্যবহার করুন (নাহলে আপনার টোকেন লিক হবে)।"
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"সিনট্যাক্স এরর (লাইন {e.lineno}): <code>{html.escape(str(e.msg))}</code>"
    return True, ""

def submit_community_code(chat_id: int, from_user, state: dict, code: str) -> bool:
    """কোড যাচাই → DB-তে জমা → অ্যাডমিনকে নোটিফাই। সফল হলে True (তখন স্টেট মুছে ফেলা যায়)।"""
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\n?", "", code)
        code = re.sub(r"\n?```\s*$", "", code)
    ok, err = validate_community_code(code)
    if not ok:
        bot.send_message(chat_id, f"❌ <b>কোড গ্রহণযোগ্য নয়:</b> {err}\n\nঠিক করে আবার পাঠান (বাতিল করতে যেকোনো মেনু বাটন চাপুন)।", parse_mode="HTML")
        return False
    if admin.count_user_pending_templates(from_user.id) >= admin.COMMUNITY_MAX_PENDING_PER_USER:
        bot.send_message(chat_id, f"⚠️ আপনার {admin.COMMUNITY_MAX_PENDING_PER_USER}টি সাবমিশন ইতিমধ্যে রিভিউয়ের অপেক্ষায় আছে। রিভিউ শেষ হলে আবার সাবমিট করুন।", parse_mode="HTML")
        return True
    tid = admin.create_community_template(from_user.id, from_user.username or "", state["name"], state["desc"], code)
    bot.send_message(
        chat_id,
        f"✅ <b>সাবমিট হয়েছে!</b> (#{tid})\n🧩 {html.escape(state['name'])}\n\n"
        "অ্যাডমিন রিভিউ করলে আপনাকে জানানো হবে। অ্যাপ্রুভ হলে সবাই এটি ডেপ্লয় করতে পারবে এবং আপনি কমিশন পাবেন। "
        "স্ট্যাটাস দেখুন: Marketplace → Community Bots → Submit → My Submissions",
        parse_mode="HTML"
    )
    flags = admin.scan_community_code(code)
    for aid in admin.ADMIN_IDS:
        try:
            m = types.InlineKeyboardMarkup()
            m.add(make_inline_button("🔍 Review", callback_data=f"adm_cm_view_{tid}", style="primary"))
            bot.send_message(
                aid,
                f"🌐 <b>নতুন কমিউনিটি টেমপ্লেট!</b> (#{tid})\n🧩 {html.escape(state['name'])}\n"
                f"👤 @{html.escape(from_user.username or str(from_user.id))} (<code>{from_user.id}</code>)\n"
                f"🛡️ স্ক্যান: {'⚠️ ' + html.escape(', '.join(flags)) if flags else '✅ কোনো সন্দেহজনক প্যাটার্ন নেই'}",
                reply_markup=m, parse_mode="HTML"
            )
        except Exception:
            pass
    return True

# ---------- কমিউনিটি: অ্যাডমিন রিভিউ প্যানেল ----------
def build_admin_community_panel():
    pending, ptotal = admin.list_community_templates("PENDING", 10, 0)
    _, atotal = admin.list_community_templates("APPROVED", 1, 0)
    cur = float(admin.SYSTEM_CONFIG.get("community_commission", 1.0) or 0.0)
    text = (
        "🌐 <b>COMMUNITY REVIEW</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ রিভিউয়ের অপেক্ষায়: <b>{ptotal}</b>\n"
        f"✅ লাইভ টেমপ্লেট: <b>{atotal}</b>\n"
        f"💰 কমিশন: <b>{cur:g} Credits</b> / ইউনিক ডেপ্লয়\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ অ্যাপ্রুভের আগে অবশ্যই কোড পড়ে দেখুন — এই কোড অন্য ইউজারদের টোকেন নিয়ে চলবে।"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in pending:
        markup.add(make_inline_button(f"🔍 #{p['id']} {p['name'][:28]}", callback_data=f"adm_cm_view_{p['id']}", style="primary"))
    markup.add(make_inline_button("📋 লাইভ টেমপ্লেট (Remove)", callback_data="adm_cm_live", style="primary"))
    comm_row = []
    for v in (0, 0.5, 1, 2, 5):
        label = f"{v:g}" + (" ✓" if abs(cur - v) < 1e-9 else "")
        comm_row.append(make_inline_button(label, callback_data=f"adm_cm_comm_{int(v * 10)}", style="primary"))
    markup.row(*comm_row)
    return text, markup


def _renew_markup():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(make_inline_button("💎 Renew Now", callback_data="open_upgrade_menu", style="success"))
    m.add(make_inline_button("🔙 Back to Home", callback_data="go_home", style="primary"))
    return m


# ----------------- বাটন হেল্পারস ও কিবোর্ড জেনারেটরস (All Buttons in English) -----------------
def make_reply_button(text, style="primary"):
    try:
        return make_kb(text, style=style)
    except Exception:
        return make_kb(text)

def make_inline_button(text, callback_data=None, url=None, style="primary"):
    text = bold_font(text)
    try:
        if url:
            return types.InlineKeyboardButton(text, url=url, style=style)
        return types.InlineKeyboardButton(text, callback_data=callback_data, style=style)
    except Exception:
        if url:
            return types.InlineKeyboardButton(text, url=url)
        return types.InlineKeyboardButton(text, callback_data=callback_data)

def add_home_button(markup, extra_text="🏠 Home Menu", extra_callback="go_home"):
    """যেকোনো ইনলাইন মেনুর নিচে একটা কুইক 'Home' বাটন জুড়ে দেয়, যাতে যেকোনো স্ক্রিন থেকে
    এক ট্যাপেই মূল হাব-এ ফিরে আসা যায় (নেভিগেশন সহজ করার জন্য, কোনো কিছু রিমুভ হয় না)।"""
    markup.add(make_inline_button(extra_text, callback_data=extra_callback, style="primary"))
    return markup

_BN_DIGIT_MAP = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
def to_bn_num(n) -> str:
    """সংখ্যাকে বাংলা সংখ্যায় (০-৯) রূপান্তর করে — যেমন 60 → ৬০।"""
    return str(n).translate(_BN_DIGIT_MAP)

def build_deploy_success_card(project_id: int, file_name: str, verify_token: bool = True):
    """ডিপ্লয়মেন্ট সফল কার্ড — আগে ৩ জায়গায় কপি-পেস্ট করা ছিল, এখন একটাই ফাংশনে।
    verify_token=False দিলে বট টোকেন Telegram-এ যাচাই করার নেটওয়ার্ক কলটা (যেটা কয়েক
    সেকেন্ড সময় নিতে পারে) স্কিপ করে সাথে সাথে কার্ড রিটার্ন করে — যাতে বট রান হওয়ার পর
    ইউজার সঙ্গে সঙ্গেই "Live" কার্ড দেখতে পায়। এরপর ব্যাকগ্রাউন্ডে যাচাই করে কার্ডটা
    আপডেট করে দেওয়া হয় (দেখুন refresh_deploy_card_async)।"""
    proj = admin.get_project_by_id(project_id)
    proj_name = proj["project_name"] if proj else file_name
    slot_no = proj.get("slot_number") if proj else None
    slot_label = f"Slot #{slot_no}" if slot_no else "Unassigned"

    # বট হ্যান্ডল (@username) — ডিপ্লয় করা ফাইলে টোকেন লেখা থাকলে দ্রুত (৪ সেকেন্ড টাইমআউট)
    # যাচাই করে দেখানো হয়, যাতে ধীর নেটওয়ার্কেও ইউজারকে বেশিক্ষণ অপেক্ষা করতে না হয়।
    bot_handle_line = "🔄 Connecting..."
    packages_line = "None Needed"
    session_line = "24/7 Unlimited Run"
    try:
        if proj:
            p_dir = os.path.join(PROJECTS_DIR, f"{proj['user_id']}_{project_id}")
            file_path = os.path.join(p_dir, file_name)
            if file_name.endswith(".py") and os.path.exists(file_path):
                if verify_token:
                    with open(file_path, "rb") as f:
                        code_bytes = f.read()
                    token = extract_bot_token_from_code(code_bytes)
                    if token:
                        is_valid, uname_or_err = validate_bot_token(token, timeout=4)
                        bot_handle_line = f"{uname_or_err} 🟢" if is_valid else "⚠️ Unknown (couldn't verify)"
                pkgs = scan_python_imports(file_path)
                packages_line = "Auto-Installed (OK)" if pkgs else "None Needed"

            u_data = admin.get_user(proj["user_id"])
            slot_status = admin.get_effective_slot_status(u_data, proj)
            if slot_status["is_24_7"]:
                session_line = "24/7 Unlimited Run"
            else:
                session_line = f"🆓 {admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)} Hours Free Session"
    except Exception:
        pass

    ram_mb = admin.SYSTEM_CONFIG.get("max_ram_mb_per_process", 256)

    text = (
        "╭━━〔 🚀 DEPLOYMENT SUCCESS 〕━━╮\n"
        "│ ⚡ Status: 🟢 Live & Running\n"
        f"│ 🏷️ Bot Name: {html.escape(proj_name)}\n"
        f"│ 🔢 Active Slot: {slot_label}\n"
        f"│ 🤖 Bot Handle: {bot_handle_line}\n"
        f"│ 📄 Main File: <code>{html.escape(file_name)}</code>\n"
        f"│ 📦 Libraries: {packages_line}\n"
        f"│ 🧠 RAM Limit: {ram_mb} MB\n"
        f"│ ⏱️ Session: {session_line}\n"
        "│ 🛡️ Watchdog: Crash-Protection Active\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n"
        "💡 কোনো এরর হলে ➜ View Logs চেক করুন\n\n"
        "👇 বটটি পরিচালনা করতে নিচের\n"
        f"🎛️ {bold_font('Control Panel')} ব্যবহার করুন:"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("🎛️ Control Panel", callback_data=f"manage_bot_{project_id}", style="primary"),
        make_inline_button("📜 View Logs", callback_data=f"bot_logs_{project_id}", style="primary")
    )
    markup.add(make_inline_button("⏹️ Stop Bot", callback_data=f"bot_stop_{project_id}", style="danger"))
    markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return text, markup

def refresh_deploy_card_async(chat_id, message_id, project_id: int, file_name: str):
    """ব্যাকগ্রাউন্ড থ্রেডে বট টোকেন যাচাই করে ডিপ্লয়মেন্ট কার্ডের 'Bot Handle' লাইনটা
    পরে আপডেট করে দেয় — যাতে টোকেন যাচাইয়ের নেটওয়ার্ক কলের জন্য ইউজারকে বট রান হওয়ার পর
    বাড়তি সময় অপেক্ষা করতে না হয়।"""
    def _worker():
        try:
            text, markup = build_deploy_success_card(project_id, file_name, verify_token=True)
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

# ----------------- GitHub Deploy (VIP-exclusive) -----------------
def sync_github_repo(user_id: int, pid: int, chat_id, status_message_id, token: str = None):
    """ইতিমধ্যে GitHub থেকে ডিপ্লয় করা একটি প্রজেক্টকে রিপোর লেটেস্ট কমিট দিয়ে আপডেট করে।
    পুরনো ফাইলগুলো রাখা হয় না — রিপোর নতুন ভার্সন দিয়ে সম্পূর্ণ রিপ্লেস হয়।
    বট চলমান থাকলে প্রথমে বন্ধ করা হয়, sync শেষে আবার চালু করা হয়।"""

    def _edit(txt):
        try:
            bot.edit_message_text(txt, chat_id, status_message_id, parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(chat_id, txt, parse_mode="HTML")
            except Exception:
                pass

    gh_info = admin.get_project_github_info(pid)
    if not gh_info:
        _edit("❌ <b>এই বটের GitHub তথ্য পাওয়া যায়নি!</b> শুধু GitHub Deploy দিয়ে তৈরি বটেই Sync কাজ করে।")
        return

    owner, repo = gh_info["owner"], gh_info["repo"]
    is_private = gh_info["is_private"]
    project = admin.get_project_by_id(pid)
    if not project:
        _edit("❌ <b>প্রজেক্ট খুঁজে পাওয়া যায়নি।</b>")
        return

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    p_key = f"{user_id}_{pid}"

    # চলমান বট বন্ধ করা
    if p_key in active_processes:
        try:
            active_processes[p_key]["process"].terminate()
            active_processes[p_key]["log_handle"].close()
        except Exception:
            pass
        del active_processes[p_key]
        admin.update_project_status(pid, "STOPPED")

    _edit(f"🔄 <b>GitHub Sync শুরু হচ্ছে...</b>\n📦 <code>{html.escape(owner)}/{html.escape(repo)}</code> থেকে লেটেস্ট ভার্সন নামানো হচ্ছে...")

    # GitHub API headers
    headers = {"User-Agent": "PyHostCloud-Bot", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    # Default branch বের করা
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            repo_info = json.loads(resp.read().decode("utf-8"))
        default_branch = repo_info.get("default_branch") or "main"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _edit("❌ <b>GitHub অ্যাক্সেস প্রত্যাখ্যাত!</b> Private repo হলে নতুন Token দিয়ে আবার Sync করুন।")
        elif e.code == 404:
            _edit("❌ <b>রিপো পাওয়া যায়নি!</b> হয়তো রিপোটি ডিলিট/private করা হয়েছে।")
        else:
            _edit(f"❌ <b>GitHub API এরর:</b> HTTP {e.code}")
        return
    except Exception as ex:
        _edit(f"❌ <b>সংযোগ সমস্যা:</b> <code>{html.escape(str(ex)[:150])}</code>")
        return

    # Zipball ডাউনলোড
    try:
        zip_req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/zipball/{default_branch}",
            headers=headers
        )
        with urllib.request.urlopen(zip_req, timeout=90) as resp:
            zip_bytes = resp.read()
    except Exception as ex:
        _edit(f"❌ <b>ডাউনলোড ব্যর্থ:</b> <code>{html.escape(str(ex)[:150])}</code>")
        return

    _edit("📦 <b>ডাউনলোড সম্পন্ন! পুরনো ফাইল রিপ্লেস হচ্ছে...</b>")

    # পুরনো ফাইল পরিষ্কার করা (requirements.txt ও .env রেখে বাকি সব রিপ্লেস)
    keep_files = {".env", ".env.local", "local_config.py"}
    saved = {}
    for kf in keep_files:
        kp = os.path.join(p_dir, kf)
        if os.path.exists(kp):
            with open(kp, "rb") as f:
                saved[kf] = f.read()

    # পুরনো ফোল্ডার পরিষ্কার
    if os.path.exists(p_dir):
        shutil.rmtree(p_dir, ignore_errors=True)
    os.makedirs(p_dir, exist_ok=True)

    # সংরক্ষিত ফাইল ফিরিয়ে দেওয়া
    for kf, kdata in saved.items():
        with open(os.path.join(p_dir, kf), "wb") as f:
            f.write(kdata)

    # নতুন ফাইল এক্সট্র্যাক্ট
    tmp_zip = os.path.join(p_dir, "__sync__.zip")
    try:
        with open(tmp_zip, "wb") as f:
            f.write(zip_bytes)
        safe_extract_zip(tmp_zip, p_dir)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    # GitHub-এর wrapper ফোল্ডার flatten করা
    entries = [e for e in os.listdir(p_dir) if e not in keep_files]
    if len(entries) == 1 and os.path.isdir(os.path.join(p_dir, entries[0])):
        wrapper_dir = os.path.join(p_dir, entries[0])
        for item in os.listdir(wrapper_dir):
            dest = os.path.join(p_dir, item)
            if os.path.exists(dest):
                if os.path.isdir(dest):
                    shutil.rmtree(dest)
                else:
                    os.remove(dest)
            shutil.move(os.path.join(wrapper_dir, item), dest)
        shutil.rmtree(wrapper_dir, ignore_errors=True)

    py_files = [f for f in os.listdir(p_dir) if f.endswith(".py")]
    if not py_files:
        _edit("❌ <b>রিপোতে কোনো .py ফাইল পাওয়া যায়নি!</b> Sync বাতিল — আগের ফাইল হারিয়ে গেছে। নতুন করে Deploy করুন।")
        return

    req_file = os.path.join(p_dir, "requirements.txt")
    if os.path.exists(req_file):
        _edit("📦 <b>requirements.txt থেকে নতুন লাইব্রেরি ইনস্টল হচ্ছে...</b>")
        pip_install_requirements(req_file)

    main_f = project["main_file"]
    if main_f not in py_files:
        main_f = pick_entry_point(p_dir, py_files) or py_files[0]
        admin.update_project_main_file(pid, main_f)

    detected = scan_python_imports(os.path.join(p_dir, main_f))
    if detected:
        install_missing_packages_sync(detected)

    ok_run, run_msg = execute_project_script(user_id, pid, main_f)
    # GitHub তথ্য আপডেট (token পরিবর্তন হলে)
    admin.set_project_github_info(pid, owner, repo, is_private)

    if not ok_run:
        _edit(f"⚠️ <b>কোড সিঙ্ক হয়েছে কিন্তু বট চালু করা যায়নি!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{run_msg}")
        return

    _edit(
        f"✅ <b>GitHub Sync সফল হয়েছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Repo:</b> <code>{html.escape(owner)}/{html.escape(repo)}</code>\n"
        f"🌿 <b>Branch:</b> <code>{default_branch}</code>\n"
        f"📄 <b>Main File:</b> <code>{main_f}</code>\n"
        "🟢 <b>বট চালু হয়েছে!</b>"
    )


def deploy_github_repo(user_id: int, chat_id, status_message_id, owner: str, repo: str, token: str = None, is_private: bool = False):
    """ব্যাকগ্রাউন্ড থ্রেডে চলে — GitHub রিপো (Public/Private) ডাউনলোড, নিরাপদে এক্সট্র্যাক্ট ও
    ডিপ্লয় করে। কোনো ধাপে সমস্যা হলে ইউজারকে স্পষ্ট বাংলা মেসেজে জানানো হয় এবং আংশিক তৈরি হওয়া
    প্রজেক্ট থাকলে সেটা ক্লিনআপ করে দেওয়া হয় (যাতে "ভুতুড়ে" খালি স্লট না থেকে যায়)। সফল হলে
    সব অ্যাডমিনকে একটা সাইলেন্ট নোটিফিকেশন পাঠানো হয় (টোকেন ছাড়া)।"""

    def _edit(txt, markup=None):
        try:
            bot.edit_message_text(txt, chat_id, status_message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(chat_id, txt, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

    headers = {"User-Agent": "PyHostCloud-Bot", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    # ১. রিপো ভেরিফাই ও ডিফল্ট ব্রাঞ্চ বের করা
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            repo_info = json.loads(resp.read().decode("utf-8"))
        default_branch = repo_info.get("default_branch") or "main"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _edit("❌ <b>রিপো পাওয়া যায়নি!</b>\nলিংকটা চেক করুন — অথবা এটা Private হলে সঠিক GitHub Token দিয়ে আবার চেষ্টা করুন।")
        elif e.code in (401, 403):
            _edit("❌ <b>অ্যাক্সেস প্রত্যাখ্যাত!</b>\nটোকেন ভুল/মেয়াদোত্তীর্ণ, অথবা GitHub rate-limit এ পড়েছে। একটু পর আবার চেষ্টা করুন।")
        else:
            _edit(f"❌ <b>GitHub এরর:</b> HTTP {e.code}")
        return
    except Exception as e:
        _edit(f"❌ <b>রিপো খুঁজে পেতে সমস্যা হয়েছে:</b> <code>{html.escape(str(e)[:200])}</code>")
        return

    # ২. জিপবল ডাউনলোড
    try:
        zip_req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}/zipball/{default_branch}", headers=headers)
        with urllib.request.urlopen(zip_req, timeout=90) as resp:
            zip_bytes = resp.read()
    except Exception as e:
        _edit(f"❌ <b>রিপো ডাউনলোড ব্যর্থ হয়েছে:</b> <code>{html.escape(str(e)[:200])}</code>")
        return

    _edit(f"📦 <b>'{html.escape(repo)}' ডাউনলোড সম্পন্ন! আনপ্যাক ও এনভায়রনমেন্ট সেটআপ হচ্ছে...</b>")

    # ৩. প্রজেক্ট স্লট রিজার্ভ ও এক্সট্র্যাক্ট (নিরাপদ zip-slip-protected এক্সট্র্যাক্টর রিইউজ করা হচ্ছে)
    pid = admin.create_user_project(user_id, repo[:30])
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    os.makedirs(p_dir, exist_ok=True)

    tmp_zip_path = os.path.join(p_dir, "__gh_download__.zip")
    try:
        with open(tmp_zip_path, "wb") as f:
            f.write(zip_bytes)
        safe_extract_zip(tmp_zip_path, p_dir)
    finally:
        if os.path.exists(tmp_zip_path):
            os.remove(tmp_zip_path)

    # GitHub zipball সবসময় একটা একক টপ-লেভেল ফোল্ডারে (owner-repo-<sha>) ফাইলগুলো দেয় —
    # সেটা ফ্ল্যাটেন করে প্রজেক্ট ফোল্ডারের রুটে নিয়ে আসা হচ্ছে
    entries = os.listdir(p_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(p_dir, entries[0])):
        wrapper_dir = os.path.join(p_dir, entries[0])
        for item in os.listdir(wrapper_dir):
            shutil.move(os.path.join(wrapper_dir, item), os.path.join(p_dir, item))
        shutil.rmtree(wrapper_dir, ignore_errors=True)

    # ৪. .py ফাইল না থাকলে ডিপ্লয় বাতিল ও ক্লিনআপ (ভুতুড়ে খালি স্লট এড়াতে)
    py_files = [f for f in os.listdir(p_dir) if f.endswith(".py")]
    if not py_files:
        admin.delete_user_project(pid)
        _edit("❌ <b>এই রিপোতে কোনো .py ফাইল পাওয়া যায়নি!</b> ডিপ্লয় বাতিল করা হয়েছে, কোনো স্লট নষ্ট হয়নি।")
        return

    req_file = os.path.join(p_dir, "requirements.txt")
    if os.path.exists(req_file):
        _edit("📦 <b>requirements.txt থেকে লাইব্রেরি ইনস্টল হচ্ছে...</b>")
        pip_install_requirements(req_file)

    main_f = pick_entry_point(p_dir, py_files) or py_files[0]
    detected = scan_python_imports(os.path.join(p_dir, main_f))
    if detected:
        install_missing_packages_sync(detected)

    ok_run, run_msg = execute_project_script(user_id, pid, main_f)
    if not ok_run:
        admin.delete_user_project(pid)   # ফ্রি স্লট নষ্ট না হয় সেজন্য বাতিল করা হলো
        _edit(f"{run_msg}\n\n(কোনো স্লট নষ্ট হয়নি — রিপো ডাউনলোড হয়েছিল কিন্তু ডিপ্লয় বাতিল করা হয়েছে।)")
        return

    # GitHub রিপো তথ্য সেভ করা হচ্ছে — পরে "🔄 GitHub Sync" বাটনে কাজে লাগবে
    admin.set_project_github_info(pid, owner, repo, is_private)
    success_card, markup = build_deploy_success_card(pid, main_f, verify_token=False)
    _edit(success_card, markup)
    refresh_deploy_card_async(chat_id, status_message_id, pid, main_f)

    # ৫. অ্যাডমিনদের সাইলেন্ট নোটিফিকেশন (টোকেন কখনোই এতে থাকবে না)
    u_data = admin.get_user(user_id)
    notify_text = (
        "🐙 <b>New GitHub Deploy!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📦 <b>Repo:</b> <code>{html.escape(owner)}/{html.escape(repo)}</code> ({'🔒 Private' if is_private else '🔓 Public'})\n"
        f"🏷️ <b>Plan:</b> <code>{u_data.get('plan', 'FREE')}</code>\n"
        f"🕒 <b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    for aid in admin.ADMIN_IDS:
        try:
            bot.send_message(aid, notify_text, parse_mode="HTML")
        except Exception:
            pass

def get_user_keyboard(user_id=None):
    """মূল ইউজার মেনু — মূল বাটনগুলো (Control Panel, File Manager, My Account, Upgrade Plan,
    GitHub Deploy, Documentation, Support Hub) সামনে থাকবে। Marketplace এখন 🐙 GitHub Deploy
    স্ক্রিনের ভিতরে, আর Redeem Code এখন 💎 Upgrade Plan স্ক্রিনের ভিতরে (ও /redeem কমান্ডে)।"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    b1 = make_reply_button("🎛️ Control Panel", style="success")
    b2 = make_reply_button("📁 File Manager", style="primary")
    b3 = make_reply_button("👤 My Account", style="primary")
    b4 = make_reply_button("💎 Upgrade Plan", style="primary")
    b6_gh = make_reply_button("🐙 GitHub Deploy", style="success")
    b6 = make_reply_button("📖 Documentation", style="primary")
    b7 = make_reply_button("📡 Support Hub", style="primary")

    # GitHub Deploy সবসময় নিচের সারির ডান পাশে থাকবে
    markup.row(b1, b2)
    markup.row(b3, b4)
    if user_id and admin.is_admin(user_id):
        b_admin = make_reply_button("👑 Admin Panel", style="danger")
        markup.row(b6, b7)
        markup.row(b_admin, b6_gh)
    else:
        markup.row(b7)
        markup.row(b6, b6_gh)
    return markup

def get_admin_reply_keyboard():
    """অ্যাডমিন মেইন কিবোর্ড — ৪টা ক্যাটাগরি বাটন, প্রতিটার ভেতরে সাব-মেনু (Analytics এখন মনিটরিং-এর ভেতরে)"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("📊 Monitoring"),
        make_kb("👥 User Manager"),
    )
    markup.add(
        make_kb("💰 Billing & Plans"),
        make_kb("⚙️ System"),
    )
    markup.add(make_kb("🔙 User Menu"))
    return markup

def get_admin_monitoring_keyboard():
    """মনিটরিং সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("📊 Live Telemetry"),
        make_kb("🤖 Process Tracker"),
    )
    markup.add(
        make_kb("🔄 Restart All Bots"),
        make_kb("⏹ Stop All Bots"),
    )
    markup.add(make_kb("📈 Analytics"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup

def get_admin_user_keyboard():
    """ইউজার ম্যানেজ সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("👥 User Directory"),
        make_kb("👤 User Management"),
    )
    markup.add(
        make_kb("📢 Global Broadcast"),
        make_kb("🚫 Ban / Security"),
    )
    markup.add(
        make_kb("🗓️ Schedule Broadcast"),
        make_kb("📋 Pending Requests"),
    )
    markup.add(make_kb("🎫 Support Tickets"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup

def get_admin_billing_keyboard():
    """বিলিং সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("💎 Grant VIP (Manual)"),
        make_kb("🎟️ Coupon Manager"),
    )
    markup.add(make_kb("🎁 Redeem Codes"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup

def get_admin_system_keyboard():
    """সিস্টেম সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("💾 Database Backup"),
        make_kb("🧹 Server Cleanup"),
    )
    markup.add(
        make_kb("⚙️ Maintenance Mode"),
        make_kb("📤 Bot Export"),
    )
    markup.add(make_kb("🆓 Free Hosting (12h)"), make_kb("🔓 VIP All Slot Unlock"))
    markup.add(make_kb("🌐 Community Review"), make_kb("💾 Data Backup"))
    markup.add(make_kb("🧹 Channel Clean"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup


def get_multi_bot_control_panel(user_id: int):
    projects = admin.get_user_projects(user_id)
    u_data = admin.get_user(user_id)
    plan = u_data.get("plan", "FREE").upper()
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    free_hosting_on = admin.SYSTEM_CONFIG.get("free_hosting_enabled", True)
    free_run_label = f"{admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)} Hours/Run"
    unlock_dt = admin.get_vip_unlock_until()
    if unlock_dt:
        free_run_label = f"🎉 VIP Unlock • 24/7 • {admin.format_unlock_remaining(unlock_dt)} বাকি"
        free_hosting_on = True

    slot_map = {}
    for p in projects:
        sn = p.get("slot_number")
        if sn:
            slot_map[sn] = p

    running_count = 0
    for p in projects:
        p_key = f"{user_id}_{p['project_id']}"
        if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
            running_count += 1

    total_count = len(projects)
    stopped_count = total_count - running_count

    text = (
        "🎛️ <b>PYHOST MULTI-BOT CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>প্ল্যান:</b> <code>{plan}</code> ({'24/7 Unlimited' if plan != 'FREE' else (free_run_label if free_hosting_on else '🔒 Locked (Off)')})\n"
        f"📦 <b>মোট বট স্লট:</b> <code>{total_count}/{max_bots}</code> টি\n"
        f"├ 🟢 <b>সক্রিয় বট:</b> <code>{running_count}</code> টি\n"
        f"└ 🔴 <b>বন্ধ বট:</b> <code>{stopped_count}</code> টি\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>যেকোনো স্লটে ট্যাপ করে ম্যানেজ, আপডেট অথবা মেয়াদ বৃদ্ধি করুন:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    slot_buttons = []
    for slot_no in range(1, admin.MAX_BOT_SLOTS + 1):
        p = slot_map.get(slot_no)
        if p:
            pid = p["project_id"]
            p_key = f"{user_id}_{pid}"
            is_run = p_key in active_processes and active_processes[p_key]["process"].poll() is None
            slot_status = admin.get_effective_slot_status(u_data, p)
            is_expired = slot_status["is_expired"]
            is_covered = slot_status["is_24_7"]
            pinfo = admin.get_plan_display_info(u_data)

            if is_expired:
                icon = "⛔"
                state_label = "Expired"
            elif is_covered and slot_status["source"] == "PLAN":
                icon = pinfo["icon"]
                state_label = f"{pinfo['display_name']} • {'Running' if is_run else 'Stopped'}"
            elif is_covered and slot_status["source"] == "EVENT":
                icon = "🎉"
                state_label = f"VIP Unlock • {'Running' if is_run else 'Stopped'}"
            elif is_covered:
                icon = "💎"
                state_label = "Extended • Running" if is_run else "Extended • Stopped"
            elif not free_hosting_on:
                icon = "🔒"
                state_label = "Locked (Free Off)"
            elif is_run:
                icon = "🟢"
                state_label = "Running"
            else:
                icon = "🔴"
                state_label = "Stopped"

            btn_text = f"{icon} Slot {slot_no} ({state_label})"
            slot_buttons.append(make_inline_button(btn_text, callback_data=f"manage_bot_{pid}", style="primary"))
        elif slot_no <= max_bots:
            if unlock_dt:
                slot_buttons.append(make_inline_button(f"🎉 Slot {slot_no} (Unlocked)", callback_data=f"slot_new_{slot_no}", style="success"))
            elif admin.is_vip_active(u_data):
                slot_buttons.append(make_inline_button(f"💎 Slot {slot_no} (VIP)", callback_data=f"slot_new_{slot_no}", style="success"))
            else:
                slot_buttons.append(make_inline_button(f"🆓 Slot {slot_no} (Free)", callback_data=f"slot_new_{slot_no}", style="success"))
        else:
            slot_buttons.append(make_inline_button(f"🔒 Slot {slot_no} (Locked)", callback_data="slot_locked", style="danger"))

    for i in range(0, len(slot_buttons), 2):
        pair = slot_buttons[i:i + 2]
        markup.add(*pair)

    add_home_button(markup)
    return text, markup

def get_single_bot_management_markup(user_id: int, project_id: int):
    project = admin.get_project_by_id(project_id)
    u_data = admin.get_user(user_id)
    if project and is_slot_locked(u_data, project):
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("💎 Upgrade Plan (আনলক করুন)", callback_data="open_upgrade_menu", style="success"))
        markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
        return markup

    p_key = f"{user_id}_{project_id}"
    is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None
    # GitHub Sync বাটন শুধু তখনই দেখাবে যখন বটটি GitHub থেকে ডিপ্লয় করা হয়েছে
    gh_info = admin.get_project_github_info(project_id)

    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.add(
            make_inline_button("⏹️ Stop Bot", callback_data=f"bot_stop_{project_id}", style="danger"),
            make_inline_button("🔄 Restart Bot", callback_data=f"bot_restart_{project_id}", style="primary")
        )
    else:
        markup.add(
            make_inline_button("▶️ Start Bot", callback_data=f"bot_start_{project_id}", style="success")
        )

    markup.add(
        make_inline_button("📜 View Logs", callback_data=f"bot_logs_{project_id}", style="primary"),
        make_inline_button("📤 Update / Upload File", callback_data=f"bot_update_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("✏️ Rename Main File", callback_data=f"bot_rename_{project_id}", style="primary")
    )
    # GitHub Sync — শুধু GitHub Deploy করা বটে দেখাবে
    if gh_info:
        markup.add(
            make_inline_button(
                f"🐙 GitHub Sync ({html.escape(gh_info['repo'])})",
                callback_data=f"gh_sync_{project_id}",
                style="success"
            )
        )
    markup.add(
        make_inline_button("🏥 Health Check", callback_data=f"bot_health_{project_id}", style="primary"),
        make_inline_button("⏳ Extend Validity", callback_data=f"bot_extend_{project_id}", style="success")
    )
    # ⭐ FIX: Download Backup, .env Manager, Version History, Clone Bot, Change Token ও
    # Scheduled Start/Stop — এই ৬টি বাটন আলাদা আলাদা না দেখিয়ে একটি "🧰 More Tools" বাটনের
    # ভিতরে একত্র করা হয়েছে (স্ক্রিনশটে গোল দাগ দেওয়া বাটনগুলো)। এতে মূল ড্যাশবোর্ড ছোট ও
    # পরিষ্কার থাকবে, ট্যাপ করলে সাব-মেনুতে সবগুলো অপশন দেখা যাবে।
    markup.add(
        make_inline_button("🧰 More Tools", callback_data=f"bot_more_tools_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("🗑️ Delete Bot", callback_data=f"bot_delete_confirm_{project_id}", style="danger")
    )
    markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return markup

def get_bot_more_tools_markup(project_id: int):
    """⭐ FIX: Download Backup, .env Manager, Version History, Clone Bot, Change Token,
    Scheduled Start/Stop — এই ৬টি বাটনের সাব-মেনু (স্ক্রিনশটে গোল দাগ দেওয়া বাটনগুলো একত্রে)।"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("📦 Download Backup", callback_data=f"fm_zip_{project_id}", style="primary"),
        make_inline_button("🔧 .env Manager", callback_data=f"env_mgr_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("📚 Version History", callback_data=f"ver_hist_{project_id}", style="primary"),
        make_inline_button("📋 Clone Bot", callback_data=f"bot_clone_{project_id}", style="success")
    )
    markup.add(
        make_inline_button("🔑 Change Token", callback_data=f"bot_token_{project_id}", style="primary"),
        make_inline_button("⏰ Scheduled Start/Stop", callback_data=f"bot_schedule_{project_id}", style="primary")
    )
    markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{project_id}", style="primary"))
    return markup

def get_file_manager_dashboard(user_id: int, project_id: int):
    project = admin.get_project_by_id(project_id)
    files, total_size = admin.get_project_files_info(user_id, project_id)
    
    file_lines = []
    if files:
        for f in files[:15]:
            sz_kb = max(1, f["size"] // 1024)
            icon = "⭐" if f["name"] == project.get("main_file", "main.py") else "📄"
            file_lines.append(f"{icon} <code>{f['name']}</code> ({sz_kb} KB)")
        if len(files) > 15:
            file_lines.append(f"<i>...এবং আরও {len(files)-15} টি ফাইল</i>")
        files_str = "\n".join(file_lines)
    else:
        files_str = "<i>কোনো ফাইল নেই (ফোল্ডার খালি)</i>"

    total_kb = total_size // 1024
    msg = (
        f"📁 <b>ফাইল ম্যানেজার: {project['project_name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>মেইন স্ক্রিপ্ট:</b> <code>{project['main_file']}</code>\n"
        f"💾 <b>মোট ফাইল সাইজ:</b> <code>{total_kb} KB</code> ({len(files)} টি ফাইল)\n\n"
        f"📋 <b>ফাইল তালিকা:</b>\n{files_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>ফাইল অ্যাকশন নির্বাচন করুন:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("📤 Upload / Replace File", callback_data=f"bot_update_{project_id}", style="primary"),
        make_inline_button("📥 Download Backup (.zip)", callback_data=f"fm_zip_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("🗑️ Delete File", callback_data=f"fm_delfile_{project_id}", style="danger"),
        make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{project_id}", style="primary")
    )
    return msg, markup

def get_my_account_card(user_id: int, user_obj):
    u_data = admin.get_user(user_id)
    projects = admin.get_user_projects(user_id)
    active_instances = 0
    for p in projects:
        p_key = f"{user_id}_{p['project_id']}"
        if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
            active_instances += 1

    pinfo = admin.get_plan_display_info(u_data)
    plan = pinfo["plan_key"]
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    time_rem = pinfo["expiry_str"] if pinfo["is_active"] else ("— (Free Tier)" if plan == "FREE" else pinfo["expiry_str"])
    plan_display = f"{pinfo['icon']} {pinfo['display_name']}"
    ref_link = f"https://t.me/{bot.get_me().username}?start=ref_{user_id}"

    card_text = (
        "╭━━━[ 👤 <b>ACCOUNT OVERVIEW</b> ]━━━╮\n"
        f"┣ 🏷 <b>Name:</b> {user_obj.first_name}\n"
        f"┣ 🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"┣ 👑 <b>Plan:</b> <code>{plan_display}</code>\n"
        f"┣ 🕐 <b>Time Remaining:</b> <code>{time_rem}</code>\n"
        "┣━━━[ 💳 <b>BILLING & REWARDS</b> ]━━━━┫\n"
        f"┣ ☁️ <b>Credits:</b> <code>{u_data.get('credits', 0.0)}</code>\n"
        f"┣ 🎁 <b>Referral Credits:</b> <code>{u_data.get('referral_credits', 0.0)}</code>\n"
        f"┣ 👥 <b>Total Referrals:</b> <code>{u_data.get('referral_count', 0)}</code> Users\n"
        "┣━━━[ 📊 <b>RESOURCE USAGE</b> ]━━━━━┫\n"
        f"┣ 🚀 <b>Active Instances:</b> <code>{active_instances}/{max_bots}</code> Bots\n"
        f"┣ ⏱️ <b>Total Uptime:</b> <code>{format_uptime_seconds(u_data.get('total_uptime_seconds', 0))}</code>\n"
        f"┣ 🎖️ <b>Badges Earned:</b> <code>{len(admin.get_user_badges(user_id))}/{len(admin.ACHIEVEMENTS)}</code>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔗 <b>আপনার রেফারেল লিংক:</b>\n"
        f"<code>{ref_link}</code>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"),
        make_inline_button("📢 Share Referral", url=f"https://t.me/share/url?url={ref_link}&text=PyHost%20এ%20ফ্রিতে%20Python%20বট%20হোস্ট%20করুন!", style="primary")
    )
    markup.add(make_inline_button("🔄 Refresh Stats", callback_data="refresh_my_account", style="primary"))
    add_home_button(markup)
    return card_text, markup

def format_uptime_seconds(total_seconds) -> str:
    total_seconds = int(total_seconds or 0)
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes}m"

# ⭐ NEW: "🔴 Live Mode" লগ ভিউয়ের কনফিগ ও হেল্পার — ⏳ FIX অংশে বলা "Live Terminal" ফিচারটা
# আসলে বাস্তবায়ন করে (আগে "View Logs" শুধু একবারের স্ন্যাপশট নিত, সত্যিকারের লাইভ ছিল না)।
LIVE_LOG_MAX_DURATION = 90   # সেকেন্ড — এত সময় পর অটো বন্ধ হয়ে যাবে (সার্ভার রিসোর্স বাঁচাতে)
LIVE_LOG_INTERVAL = 4        # সেকেন্ড — প্রতি এত সেকেন্ড পরপর মেসেজ রিফ্রেশ হবে

def render_log_view(user_id: int, pid: int, n: int = 25, live: bool = False, remaining: int = None) -> str:
    """একটি বটের শেষ n লাইনের লগ HTML-এস্কেপ করে রেন্ডার করে (আগে raw টেক্সট escape ছাড়াই
    <pre> ট্যাগে বসানো হতো — ট্রেসব্যাকে '<'/'>' থাকলে HTML পার্সিং ভেঙে যেতে পারত)।"""
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    log_path = os.path.join(p_dir, "app.log")
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            logs = f.readlines()
        last_logs = "".join(logs[-n:]) if logs else "লগ ফাইল খালি।"
    else:
        last_logs = "কোনো লগ পাওয়া যায়নি।"

    if live:
        ts = datetime.now().strftime("%H:%M:%S")
        header = f"🔴 <b>লাইভ টার্মিনাল লগ</b> (শেষ {n} লাইন) — আপডেট: <code>{ts}</code>"
        if remaining is not None:
            header += f"\n⏳ <i>{remaining} সেকেন্ড পর স্বয়ংক্রিয়ভাবে বন্ধ হবে</i>"
    else:
        header = f"📜 <b>টার্মিনাল লগ</b> (শেষ {n} লাইন):"

    return f"{header}\n<pre>{html.escape(last_logs)}</pre>"

def _live_log_loop(chat_id, message_id, user_id, pid, session_key):
    """ব্যাকগ্রাউন্ড থ্রেড — LIVE_LOG_INTERVAL সেকেন্ড পরপর একই মেসেজ এডিট করে লগ রিফ্রেশ করে,
    LIVE_LOG_MAX_DURATION সেকেন্ড পার হলে বা ইউজার '⏹ Stop' চাপলে থেমে যায়।"""
    start_time = time.time()
    last_text = None
    stopped_manually = False

    while time.time() - start_time < LIVE_LOG_MAX_DURATION:
        if session_key not in live_log_sessions:
            stopped_manually = True
            break
        remaining = max(0, int(LIVE_LOG_MAX_DURATION - (time.time() - start_time)))
        text = render_log_view(user_id, pid, live=True, remaining=remaining)
        if text != last_text:
            try:
                stop_markup = types.InlineKeyboardMarkup(row_width=1)
                stop_markup.add(make_inline_button("⏹ Live Mode বন্ধ করুন", callback_data=f"bot_logs_stoplive_{pid}", style="danger"))
                stop_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
                bot.edit_message_text(text, chat_id, message_id, reply_markup=stop_markup, parse_mode="HTML")
                last_text = text
            except Exception:
                pass
        time.sleep(LIVE_LOG_INTERVAL)

    if not stopped_manually and live_log_sessions.pop(session_key, None) is not None:
        try:
            static_markup = types.InlineKeyboardMarkup(row_width=1)
            static_markup.add(make_inline_button("🔴 Live Mode আবার চালু করুন", callback_data=f"bot_logs_live_{pid}", style="danger"))
            static_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
            bot.edit_message_text(
                render_log_view(user_id, pid) + "\n\n⏱️ <i>Live mode সময় শেষ হওয়ায় স্বয়ংক্রিয়ভাবে বন্ধ হয়েছে।</i>",
                chat_id, message_id, reply_markup=static_markup, parse_mode="HTML"
            )
        except Exception:
            pass

def render_bot_dashboard(chat_id, message_id, user_id: int, pid: int):
    """একক বটের ড্যাশবোর্ড মেসেজ রেন্ডার/রিফ্রেশ করে। আগে Start/Stop/Restart-এর পর
    একটা fake callback object বানিয়ে হ্যান্ডলার নিজেকে কল করত — এখন সরাসরি এই ফাংশন কল হবে,
    যা বেশি স্থিতিশীল ও রক্ষণাবেক্ষণযোগ্য।"""
    project = admin.get_project_by_id(pid)
    if not project:
        return

    p_key = f"{user_id}_{pid}"
    is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None
    status_str = "🟢 Running (Active)" if is_running else "🔴 Stopped (Inactive)"

    runtime_str = "0h 0m"
    mem_str = "0 MB"
    cpu_str = "0.0%"
    if is_running:
        elapsed = int(time.time() - active_processes[p_key]["start_time"])
        runtime_str = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
        mem, cpu = admin.get_bot_resource_usage(active_processes[p_key]["process"].pid)
        mem_str = f"{mem} MB"
        cpu_str = f"{cpu}%"

    # প্ল্যান + স্লট এক্সটেনশন — দুটো সোর্স মিলিয়ে একটাই effective স্ট্যাটাস, Control Panel ও
    # Extend Validity স্ক্রিনের সাথে ঠিক একই তথ্য এখানেও দেখানো হবে
    u_data = admin.get_user(user_id)
    slot_status = admin.get_effective_slot_status(u_data, project)
    pinfo = admin.get_plan_display_info(u_data)
    is_expired = slot_status["is_expired"]
    slot_display = project.get("slot_number") or "-"
    # Badge — প্ল্যান কিনলে সেই প্ল্যানের সুন্দর নামটাই দেখাবে (BUSINESS → Business Boss)
    if slot_status["is_24_7"] and slot_status["source"] == "PLAN":
        vip_badge = f" • {pinfo['icon']} {pinfo['display_name']}"
    elif slot_status["is_24_7"] and slot_status["source"] == "SLOT":
        vip_badge = " • 💎 Extended"
    elif slot_status["is_24_7"] and slot_status["source"] == "EVENT":
        vip_badge = " • 🎉 VIP Unlock"
    else:
        vip_badge = " • 🆓 Free"

    # তারিখ সুন্দরভাবে ফরম্যাট করা ("18 Sep 2026 • 19:00:44")
    try:
        created_dt = datetime.strptime(project['created_at'], "%Y-%m-%d %H:%M:%S")
        created_str = created_dt.strftime("%d %b %Y • %H:%M:%S")
    except Exception:
        created_str = project['created_at']

    # ⭐ NEW: প্ল্যান/মেয়াদ ব্লকটি এখন একটা আলাদা সুন্দর বক্সে দেখানো হয়, আর সার্ভিস স্ট্যাটাস
    # লাইনের নিচে বটটি এখন সক্রিয়/বন্ধ/মেয়াদোত্তীর্ণ কিনা তার একটা স্পষ্ট সামারি লাইন যোগ হয়েছে।
    free_hours_now = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
    locked_now = is_slot_locked(u_data, project)
    if slot_status["expiry_dt"] is None:
        service_line = "⚡ সার্ভিস: 24/7 Active (Lifetime)" if slot_status["is_24_7"] else f"⚡ সার্ভিস: 🆓 Free ({free_hours_now}h/Session)"
        validity_lines = f"│ {service_line}"
    else:
        days_left = slot_status["days_left"]
        expiry_str = slot_status["expiry_dt"].strftime("%d %b %Y • %H:%M")
        service_line = "⚡ সার্ভিস: 24/7 Active" if slot_status["is_24_7"] else f"⚡ সার্ভিস: 🆓 Free ({free_hours_now}h/Session)"
        validity_lines = (
            f"│ {service_line}\n"
            f"│ 📆 মেয়াদ বাকি: {days_left} দিন\n"
            f"│ ⏳ মেয়াদ শেষ: {expiry_str}"
        )

    plan_box = (
        "╭─────────────────────\n"
        f"│ 👑 প্ল্যান: {pinfo['icon']} {pinfo['display_name']}\n"
        f"{validity_lines}\n"
        "╰─────────────────────"
    )

    if locked_now:
        footer_line = "🔒 <b>ফ্রি হোস্টিং অ্যাডমিন থেকে সাময়িক বন্ধ — এই স্লট লকড</b>"
    elif is_expired:
        footer_line = "⛔ <b>মেয়াদ শেষ — বট বন্ধ আছে</b>"
    elif is_running:
        footer_line = "🟢 <b>বট বর্তমানে সক্রিয় ও চলছে</b>"
    else:
        footer_line = "🔴 <b>বট বর্তমানে বন্ধ আছে</b>"

    msg = (
        "🤖 <b>বট ড্যাশবোর্ড</b>\n"
        f"{html.escape(project['project_name'])}{vip_badge}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 <b>Slot:</b> <code>{slot_display}</code>\n"
        f"⚡ <b>স্ট্যাটাস:</b> <code>{status_str}</code>\n"
        f"📄 <b>মেইন ফাইল:</b> <code>{project['main_file']}</code>\n"
        f"🧠 <b>RAM Usage:</b> <code>{mem_str} / 256 MB</code>\n"
        f"💻 <b>CPU Load:</b> <code>{cpu_str}</code>\n"
        f"⏰ <b>রানিং টাইম:</b> <code>{runtime_str}</code>\n"
        f"📅 <b>তৈরি:</b> <code>{created_str}</code>\n\n"
        f"<pre>{plan_box}</pre>\n\n"
        f"{footer_line}"
    )
    try:
        bot.edit_message_text(
            msg, chat_id, message_id,
            reply_markup=get_single_bot_management_markup(user_id, pid),
            parse_mode="HTML"
        )
    except Exception:
        pass


def _doc_pages():
    """📖 Documentation-এর পাতাগুলো প্রতিবার ফ্রেশ বানানো হয় যাতে ফ্রি হোস্টিংয়ের ঘণ্টা সবসময় সঠিক থাকে।"""
    hours = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
    return [
        ("🚀 নতুন বট তৈরি ও ডেপ্লয়",
         "├ 1️⃣ 🎛️ <b>Control Panel</b> থেকে <code>➕ Add New Bot</code> চাপুন।\n"
         "├ 2️⃣ আপনার বটের নাম দিন (যেমন: <i>My Support Bot</i>)।\n"
         "├ 3️⃣ <code>📤 Update / Upload File</code> থেকে <code>.py</code> বা <code>.zip</code> স্ক্রিপ্ট আপলোড করুন (বা সরাসরি কোড চ্যাটে পেস্ট করুন)।\n"
         "└ 4️⃣ ফাইল আপলোড হলে সিস্টেম স্বয়ংক্রিয়ভাবে প্যাকেজ ইনস্টল করে বট চালু করবে!"),
        ("🛒 নো-কোড মার্কেটপ্লেস (১-ক্লিক ডেপ্লয়)",
         "├ কোনো কোডিং ছাড়াই বট চালাতে 🤖 <b>Marketplace</b>-এ যান (🐙 GitHub Deploy স্ক্রিনের ভিতরে পাবেন)।\n"
         "├ পছন্দের বট সিলেক্ট করে @BotFather থেকে পাওয়া <code>BOT_TOKEN</code> দিন।\n"
         "└ সিস্টেম স্বয়ংক্রিয়ভাবে পুরো বট কনফিগার করে লাইভ করে দেবে।"),
        ("📁 ফাইল ও লাইব্রেরি রুলস",
         "├ 📄 <b>মেইন ফাইল:</b> আপনার প্রধান ফাইলের নাম <code>main.py</code> রাখা উত্তম।\n"
         "├ 📦 <b>জিপ ফাইল:</b> <code>.zip</code> আপলোড করলে তা নিজে থেকেই আনপ্যাক হয়ে যাবে।\n"
         "└ ⚙️ <b>ডিপেন্ডেন্সি:</b> কোড স্ক্যান করে সব লাইব্রেরি সিস্টেম অটো ইনস্টল করে নেয়।"),
        ("🎛️ ড্যাশবোর্ড ও ফাইল ম্যানেজার",
         "├ ▶️ <b>Start / Stop:</b> বট চালু বা বন্ধ রাখা।\n"
         "├ 🔄 <b>Restart:</b> কোড আপডেটের পর রিফ্রেশ করা।\n"
         "├ 📜 <b>View Logs:</b> কোনো এরর হলে লাইভ লগ চেক করা।\n"
         "└ 📁 <b>File Manager:</b> ফাইলের সাইজ দেখা, জিপ ব্যাকআপ ও নির্দিষ্ট ফাইল ডিলিট।"),
        ("⏳ প্ল্যান পলিসি ও প্যাকেজসমূহ",
         f"├ 🟢 <b>FREE Plan:</b> প্রতি রান {hours} ঘণ্টা ({hours} ঘণ্টা পর CP থেকে আবার চালু করা যাবে)।\n"
         "├ ⚡ <b>Pro Plan:</b> 24/7 লাইভ • 1টি বট • ফাস্ট বুট।\n"
         "├ 🌟 <b>Premium Plan:</b> 24/7 লাইভ • 3টি বট • অটো-রিস্টার্ট • ব্যাকআপ।\n"
         "└ 👑 <b>Business Boss:</b> 24/7 লাইভ • 10টি বট • ম্যাক্সিমাম RAM • VIP সাপোর্ট।\n"
         "🎁 <b>Redeem Code:</b> 💎 Upgrade Plan পেজ থেকে অথবা <code>/redeem CODE</code> দিয়ে সাথে সাথে প্ল্যান চালু করুন।"),
        ("🛠️ সাপোর্ট হাব (Support Hub)",
         "├ 💼 <b>Dev Studio:</b> কাস্টম বট বানাতে আমাদের ডেভেলপার টিমকে রিকোয়ারমেন্ট পাঠান।\n"
         "└ 👨‍💼 <b>Contact Admin:</b> পেমেন্ট বা টেকনিক্যাল সহায়তার জন্য সরাসরি যোগাযোগ করুন।"),
    ]

def build_doc_page(idx: int):
    pages = _doc_pages()
    idx = max(0, min(idx, len(pages) - 1))
    title, body = pages[idx]
    text = (
        f"📖 <b>PYHOST CLOUD গাইড</b> — পাতা {idx + 1}/{len(pages)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
        f"<b>{title}</b>\n"
        f"{body}\n\n"
        "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    markup = types.InlineKeyboardMarkup(row_width=3)
    nav = []
    if idx > 0:
        nav.append(make_inline_button("◀️ Prev", callback_data=f"doc_page_{idx - 1}", style="primary"))
    nav.append(make_inline_button(f"{idx + 1}/{len(pages)}", callback_data="doc_noop", style="primary"))
    if idx < len(pages) - 1:
        nav.append(make_inline_button("Next ▶️", callback_data=f"doc_page_{idx + 1}", style="success"))
    markup.row(*nav)
    add_home_button(markup)
    return text, markup

def get_upload_guide_card():
    text = (
        "╭──────────────╮\n"
        "│ 📤 ফাইল / কোড পাঠান\n"
        "│\n"
        "│ • 🐍 \".py\" → main.py / bot.py\n"
        "│ • 📄 \".txt\" \".json\" \".env\"\n"
        "│ • 📦 \".zip\" → সব ফাইল\n"
        "│ • 💻 সরাসরি Python কোড\n"
        "╰──────────────╯\n"
        "╭━━━━━━━━━━━━━━━━━━╮\n"
        "┃ 📥 এখন ফাইল বা কোডটি সেন্ড করুন\n"
        "╰━━━━━━━━━━━━━━━━━━╯"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("◀️ Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return text, markup

# ৩-টিয়ার প্যাকেজ সিলেকশন ভিউ
def get_upgrade_plan_view(user_id: int):
    u_data = admin.get_user(user_id)
    pinfo = admin.get_plan_display_info(u_data)
    curr_tier_display = f"{pinfo['icon']} {pinfo['display_name']}"
    exp_display = pinfo["expiry_str"] if pinfo["is_active"] else "—"

    def _tag(tdata):
        t = " | Pro" if tdata.get("pro") else ""
        if tdata.get("sold") is not None:
            t += f" | Sold: {tdata['sold']}/{tdata.get('sold_total', tdata['sold'])}"
        return t

    pro = admin.TIER_PLANS["PRO"]
    prem = admin.TIER_PLANS["PREMIUM"]
    biz = admin.TIER_PLANS["BUSINESS"]
    biz6 = admin.TIER_PLANS["BUSINESS6M"]
    yr = admin.TIER_PLANS["YEARLY"]

    text = (
        "🚀 <b>PyHost Cloud — Premium Plans</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Current Tier:</b> <code>{curr_tier_display}</code> ({exp_display})\n\n"
        "⚡ <b>Pro:</b> 24/7 • Ad-Free • 1 Bot • Auto-Renew\n"
        "🌟 <b>Premium:</b> 24/7 • 3 Bots • GitHub Sync • Backup • DDoS Shield\n"
        "👑🔥 <b>Business Boss:</b> 24/7 • 10 Bots • Live Terminal • Max RAM • VIP Support\n"
        "🏆 <b>Business Yearly:</b> Business-এর সব ফিচার + সারা বছরের জন্য বিশাল ছাড়\n\n"
        "👇 <b>Plan select:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    b1 = make_inline_button(f"⚡ {pro['period_bn']} — {pro['price']}{_tag(pro)}", callback_data="select_tier_PRO", style="primary")
    b2 = make_inline_button(f"🌟 {prem['period_bn']} — {prem['price']}{_tag(prem)}", callback_data="select_tier_PREMIUM", style="success")
    b3 = make_inline_button(f"👑 {biz['period_bn']} — {biz['price']}{_tag(biz)}", callback_data="select_tier_BUSINESS", style="primary")
    b4 = make_inline_button(f"🔥 {biz6['period_bn']} — {biz6['price']}{_tag(biz6)}", callback_data="select_tier_BUSINESS6M", style="success")
    b5 = make_inline_button(f"🏆 {yr['period_bn']} — {yr['price']}{_tag(yr)}", callback_data="select_tier_YEARLY", style="success")
    markup.add(b1, b2, b3, b4, b5)
    markup.add(make_inline_button("🎁 Redeem Code আছে?", callback_data="user_redeem_start", style="success"))
    add_home_button(markup)
    return text, markup

# নতুন অর্ডার সামারি কার্ড ও পেমেন্ট গেটওয়ে বাটনস (স্ক্রিনশটের হুবহু ডিজাইন)
def get_order_summary_view(tier_key: str, discount_percent: int = 0):
    tdata = admin.TIER_PLANS.get(tier_key, admin.TIER_PLANS["PREMIUM"])
    
    price_val = tdata["price_num"]
    if discount_percent > 0:
        price_val = int(price_val * (1 - discount_percent / 100))
        price_text = f"<s>{tdata['price_num']} BDT</s> <b>{price_val} BDT</b> ({discount_percent}% OFF)"
    else:
        price_text = f"{price_val} BDT"

    text = (
        "🛒 <b>Order Summary</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>{tdata['title']}</b> | ⏱️ <b>{tdata['days']} Days</b>" + (" | Pro" if tdata.get("pro") else "") + "\n"
        f"💰 <b>{price_text}</b>" + (f" | Sold: {tdata['sold']}/{tdata.get('sold_total', tdata['sold'])}" if tdata.get("sold") is not None else "") + "\n\n"
        f"✨ <b>Features:</b>\n{tdata['features']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "পেমেন্ট মেথড বেছে নিন:"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = make_inline_button("💳 bKash", callback_data=f"paygw_{tier_key}_bkash_{price_val}", style="primary")
    b2 = make_inline_button("💳 Nagad", callback_data=f"paygw_{tier_key}_nagad_{price_val}", style="primary")
    b3 = make_inline_button("🪙 Crypto Pay (USDT/TRX)", callback_data=f"paygw_{tier_key}_crypto_{price_val}", style="primary")
    b4 = make_inline_button("🎁 Coupon Code", callback_data=f"apply_coupon_{tier_key}", style="success")
    b5 = make_inline_button("◀️ Back", callback_data="open_upgrade_menu", style="danger")
    
    markup.add(b1, b2)
    markup.add(b3)
    markup.add(b4)
    markup.add(b5)
    return text, markup

def get_support_hub_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = make_inline_button("❓ FAQ", callback_data="sup_faq", style="primary")
    b2 = make_inline_button("📢 Official Channel ↗️", url="https://t.me/+6eODSVuNdqUxMTNl", style="primary")
    b3 = make_inline_button("👥 Community ↗️", url="https://t.me/+6eODSVuNdqUxMTNl", style="primary")
    b4 = make_inline_button("💼 Dev Studio", callback_data="sup_devstudio", style="primary")
    # ⭐ NEW: "👨‍💼 Contact Admin" আগে সরাসরি অ্যাডমিনের ব্যক্তিগত DM-এ URL লিংক ছিল — কোনো
    # ট্র্যাকিং ছিল না। এখন "🎫 My Tickets" দিয়ে বটের ভিতরেই একটা ট্র্যাকযোগ্য টিকিট
    # সিস্টেমে যাওয়া যায় (status + মেসেজ থ্রেড সহ)।
    b6 = make_inline_button("🎫 My Tickets", callback_data="my_tickets_page_0", style="success")
    b5 = make_inline_button("👨‍💼 Contact Admin ↗️", url="https://t.me/forhadkhandakar", style="success")
    markup.add(b1, b2, b3, b4)
    markup.add(b6)
    markup.add(b5)
    add_home_button(markup, "🔙 Back to Home")
    return markup

# ======================== 💼 Dev Studio (কাস্টম বট ডেভেলপমেন্ট রিকোয়েস্ট) ========================
def get_dev_studio_card():
    """Support Hub-এর 💼 Dev Studio বাটনের স্ক্রিন — রিকোয়ারমেন্ট পাঠালে সাপোর্ট টিকিট হিসেবে অ্যাডমিনের কাছে যায়।"""
    text = (
        "💼 <b>Dev Studio — কাস্টম বট ডেভেলপমেন্ট</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "নিজের আইডিয়া অনুযায়ী কাস্টম টেলিগ্রাম বট বানাতে চান? আমাদের ডেভেলপার টিমকে আপনার "
        "রিকোয়ারমেন্ট পাঠান — টিম রিভিউ করে সরাসরি আপনাকে রিপ্লাই দেবে।\n\n"
        "📝 <b>রিকোয়ারমেন্টে যা লিখলে দ্রুত কাজ শুরু হবে:</b>\n"
        "├ 1️⃣ বটের ধরন (যেমন: স্টোর, সাপোর্ট, গ্রুপ ম্যানেজার)\n"
        "├ 2️⃣ কী কী ফিচার চান\n"
        "├ 3️⃣ আপনার বাজেট\n"
        "└ 4️⃣ কবে নাগাদ দরকার (ডেডলাইন)\n\n"
        "🎫 রিকোয়ারমেন্ট <b>সাপোর্ট টিকিট</b> হিসেবে জমা হবে — <b>My Tickets</b> থেকে রিপ্লাই ও স্ট্যাটাস দেখতে পারবেন।\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        make_inline_button("📝 রিকোয়ারমেন্ট পাঠান", callback_data="devstudio_request", style="success"),
        make_inline_button("🎫 My Tickets", callback_data="my_tickets_page_0", style="primary"),
        make_inline_button("🔙 Back", callback_data="back_to_support", style="danger"),
    )
    return text, markup

# ======================== 🎁 Redeem Code — অ্যাডমিন ড্যাশবোর্ড + ইউজার ফ্লো ========================
def _edit_or_send(call, text, markup=None):
    """মেসেজ এডিট করে; কোনো কারণে না পারলে (মেসেজ পুরনো/মুছে গেছে) নতুন মেসেজ পাঠায়।"""
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

def _rc_pack_label(tier_key: str, days: int = None) -> str:
    t = admin.TIER_PLANS.get(tier_key, {})
    d = days if days is not None else t.get("days", 0)
    return f"{t.get('icon', '🎁')} {t.get('title', tier_key)} • {d} দিন"

def _rc_status(c: dict) -> tuple:
    if not c["is_active"]:
        return "🔴", "বাতিল"
    if c["max_uses"] > 0 and c["used_count"] >= c["max_uses"]:
        return "✅", "ব্যবহৃত"
    return "🟢", "অ্যাভেইলেবল"

def _rc_uses_label(c: dict) -> str:
    return f"{c['used_count']}/{c['max_uses']}" if c["max_uses"] > 0 else f"{c['used_count']}/∞"

def build_redeem_dashboard():
    st = admin.get_redeem_stats()
    recent = admin.list_redeem_codes(limit=5)
    if recent:
        lines = [f"{_rc_status(c)[0]} <code>{c['code']}</code> — {_rc_pack_label(c['tier_key'], c['days'])} | {_rc_uses_label(c)}" for c in recent]
        recent_txt = "\n".join(lines)
    else:
        recent_txt = "<i>এখনো কোনো কোড তৈরি হয়নি।</i>"
    text = (
        "🎁 <b>Redeem Code Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "প্যাক বেছে কোড বানান → ইউজারকে কোড দিন → ইউজার <b>🎁 Redeem Code</b> দিয়ে রিডিম করলেই "
        "প্ল্যান সরাসরি চালু হয়ে যাবে।\n\n"
        f"📦 মোট কোড: <b>{st['total']}</b>\n"
        f"🟢 ব্যবহারযোগ্য: <b>{st['available']}</b>\n"
        f"✅ মোট রিডিম: <b>{st['redeemed']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🕒 <b>সর্বশেষ ৫টি কোড:</b>\n" + recent_txt
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(make_inline_button("➕ Create New Code", callback_data="rc_new", style="success"))
    markup.add(
        make_inline_button("📋 All Codes", callback_data="rc_list_0", style="primary"),
        make_inline_button("🔄 Refresh", callback_data="rc_home", style="primary"),
    )
    return text, markup

def build_redeem_tier_picker():
    text = (
        "📦 <b>প্যাক বেছে নিন</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "কোন প্ল্যানের কোড বানাতে চান? (বট লিমিট প্ল্যান অনুযায়ী স্বয়ংক্রিয়ভাবে সেট হবে)"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, t in admin.TIER_PLANS.items():
        markup.add(make_inline_button(
            f"{t['icon']} {t['title']} • {t['period_bn'].replace('দিন','Days').replace('মাস','Months').replace('বছর','Year')} • {t['bot_limit']} Bots",
            callback_data=f"rc_tier_{key}", style="primary"))
    markup.add(make_inline_button("🔙 Back", callback_data="rc_home", style="danger"))
    return text, markup

def build_redeem_days_picker(tier_key: str):
    t = admin.TIER_PLANS[tier_key]
    text = (
        f"⏳ <b>মেয়াদ বেছে নিন</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 প্যাক: <b>{t['icon']} {t['title']}</b>\n"
        f"ডিফল্ট মেয়াদ: <b>{t['days']} দিন</b> — চাইলে অন্য মেয়াদও দিতে পারেন।"
    )
    markup = types.InlineKeyboardMarkup(row_width=4)
    markup.add(make_inline_button(f"✅ Default ({t['days']} Days)", callback_data=f"rc_days_{tier_key}_{t['days']}", style="success"))
    quick = [d for d in (7, 15, 30, 90) if d != t["days"]]
    markup.row(*[make_inline_button(f"{d} Days", callback_data=f"rc_days_{tier_key}_{d}", style="primary") for d in quick])
    markup.add(make_inline_button("✏️ Custom Days", callback_data=f"rc_daysc_{tier_key}", style="primary"))
    markup.add(make_inline_button("🔙 Back", callback_data="rc_new", style="danger"))
    return text, markup

def build_redeem_qty_picker(tier_key: str, days: int):
    text = (
        "🔢 <b>কতটি কোড বানাবেন?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 প্যাক: <b>{_rc_pack_label(tier_key, days)}</b>\n"
        "প্রতিটি কোড শুধু <b>১ জন</b> ইউজার রিডিম করতে পারবে।\n"
        "নিজের পছন্দের নামের কোড বা একাধিক ইউজারের কোড চাইলে ✍️ কাস্টম কোড বেছে নিন।"
    )
    markup = types.InlineKeyboardMarkup(row_width=5)
    markup.row(*[make_inline_button(str(q), callback_data=f"rc_qty_{tier_key}_{days}_{q}", style="success") for q in (1, 5, 10, 25, 50)])
    markup.add(make_inline_button("✍️ Custom Code (Own Name / Multi-User)", callback_data=f"rc_custom_{tier_key}_{days}", style="primary"))
    markup.add(make_inline_button("🔙 Back", callback_data=f"rc_tier_{tier_key}", style="danger"))
    return text, markup

def build_redeem_codes_result(tier_key: str, days: int, codes: list, max_uses: int):
    uses_label = "আনলিমিটেড ইউজার" if max_uses == 0 else f"{max_uses} জন ইউজার"
    header = (
        f"✅ <b>{len(codes)}টি কোড তৈরি হয়েছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 প্যাক: <b>{_rc_pack_label(tier_key, days)}</b>\n"
        f"👥 প্রতি কোড: <b>{uses_label}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    footer = "\n━━━━━━━━━━━━━━━━━━━━━━\n👆 কোডে ট্যাপ করলে কপি হবে — ইউজারকে পাঠিয়ে দিন।"
    # লম্বা লিস্ট হলে ৪০৯৬ ক্যারেক্টার লিমিট এড়াতে ভেঙে পাঠানো হয়
    chunks, cur = [], []
    cur_len = len(header) + len(footer)
    for c in codes:
        line = f"<code>{c}</code>"
        if cur_len + len(line) + 1 > 3800:
            chunks.append(cur); cur = []; cur_len = len(footer) + 20
        cur.append(line); cur_len += len(line) + 1
    chunks.append(cur)
    messages = []
    for i, ch in enumerate(chunks):
        body = "\n".join(ch)
        messages.append((header if i == 0 else "🎁 <b>(পরের অংশ)</b>\n") + body + footer)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("➕ Create More", callback_data="rc_new", style="success"),
        make_inline_button("🎁 Redeem Home", callback_data="rc_home", style="primary"),
    )
    return messages, markup

def build_redeem_list_view(page: int = 0):
    codes = admin.list_redeem_codes(limit=200)
    markup = types.InlineKeyboardMarkup(row_width=1)
    if not codes:
        markup.add(make_inline_button("🔙 Back", callback_data="rc_home", style="danger"))
        return "📋 <b>কোড লিস্ট</b>\n━━━━━━━━━━━━━━━━━━━━━━\n<i>এখনো কোনো কোড তৈরি হয়নি।</i>", markup
    items, page, total_pages = admin.paginate(codes, page, per_page=8)
    text = (
        f"📋 <b>সব Redeem Code</b> — মোট <code>{len(codes)}</code>টি (পেজ {page+1}/{total_pages})\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n🟢 ব্যবহারযোগ্য  ✅ ব্যবহৃত  🔴 বাতিল\n👇 ডিটেইলস দেখতে কোডে ট্যাপ করুন:"
    )
    for c in items:
        icon, _ = _rc_status(c)
        markup.add(make_inline_button(f"{icon} {c['code']} • {c['days']}d • {_rc_uses_label(c)}", callback_data=f"rc_view_{c['code']}", style="primary"))
    nav = []
    if page > 0:
        nav.append(make_inline_button("◀️ Prev", callback_data=f"rc_list_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav.append(make_inline_button("Next ▶️", callback_data=f"rc_list_{page+1}", style="primary"))
    if nav:
        markup.row(*nav)
    markup.add(make_inline_button("🔙 Back", callback_data="rc_home", style="danger"))
    return text, markup

def build_redeem_detail_view(code: str):
    c = admin.get_redeem_code(code)
    if not c:
        return None, None
    icon, label = _rc_status(c)
    uses = admin.get_redeem_uses(c["code"], limit=15)
    if uses:
        used_lines = "\n".join(f"• @{html.escape(u['username'] or 'Anonymous')} (<code>{u['user_id']}</code>) — {u['used_at']}" for u in uses)
    else:
        used_lines = "<i>এখনো কেউ রিডিম করেনি।</i>"
    text = (
        f"🎁 <b>Redeem Code</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 কোড: <code>{c['code']}</code>\n"
        f"📦 প্যাক: <b>{_rc_pack_label(c['tier_key'], c['days'])}</b>\n"
        f"📊 স্ট্যাটাস: {icon} <b>{label}</b>\n"
        f"👥 ব্যবহার: <b>{_rc_uses_label(c)}</b>\n"
        f"🕒 তৈরি: <code>{c['created_at']}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>রিডিম করেছেন:</b>\n{used_lines}"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    if c["is_active"]:
        markup.add(make_inline_button("⛔ Revoke Code", callback_data=f"rc_off_{c['code']}", style="danger"))
    else:
        markup.add(make_inline_button("♻️ Re-Activate", callback_data=f"rc_on_{c['code']}", style="success"))
    markup.add(make_inline_button("🔙 Code List", callback_data="rc_list_0", style="primary"))
    return text, markup

def _handle_redeem_admin_callback(call):
    """অ্যাডমিন প্যানেলের সব rc_* কলব্যাক এখানে। (ডিসপ্যাচারে অ্যাডমিন চেক আগেই হয়ে গেছে)"""
    uid = call.from_user.id
    parts = call.data.split("_")
    action = parts[1] if len(parts) > 1 else ""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if action == "home":
        user_states.pop(uid, None)
        text, markup = build_redeem_dashboard()
        _edit_or_send(call, text, markup)

    elif action == "new":
        user_states.pop(uid, None)
        text, markup = build_redeem_tier_picker()
        _edit_or_send(call, text, markup)

    elif action == "tier" and len(parts) >= 3 and parts[2] in admin.TIER_PLANS:
        text, markup = build_redeem_days_picker(parts[2])
        _edit_or_send(call, text, markup)

    elif action == "daysc" and len(parts) >= 3 and parts[2] in admin.TIER_PLANS:
        user_states[uid] = {"state": "awaiting_redeem_days", "tier": parts[2]}
        bot.send_message(
            call.message.chat.id,
            "✏️ <b>কাস্টম মেয়াদ</b>\nকত দিনের প্যাক? শুধু সংখ্যা লিখে পাঠান (১ – ৩৬৫০)।\nউদাহরণ: <code>45</code>",
            parse_mode="HTML"
        )

    elif action == "days" and len(parts) >= 4 and parts[2] in admin.TIER_PLANS and parts[3].isdigit():
        text, markup = build_redeem_qty_picker(parts[2], int(parts[3]))
        _edit_or_send(call, text, markup)

    elif action == "qty" and len(parts) >= 5 and parts[2] in admin.TIER_PLANS and parts[3].isdigit() and parts[4].isdigit():
        tier_key, days, qty = parts[2], int(parts[3]), int(parts[4])
        ok, res = admin.create_redeem_codes(tier_key, days, qty, 1, uid)
        if not ok:
            bot.send_message(call.message.chat.id, f"❌ {html.escape(str(res))}", parse_mode="HTML")
            return
        messages, markup = build_redeem_codes_result(tier_key, days, res, 1)
        for i, m in enumerate(messages):
            bot.send_message(call.message.chat.id, m, reply_markup=markup if i == len(messages) - 1 else None, parse_mode="HTML")

    elif action == "custom" and len(parts) >= 4 and parts[2] in admin.TIER_PLANS and parts[3].isdigit():
        user_states[uid] = {"state": "awaiting_redeem_custom", "tier": parts[2], "days": int(parts[3])}
        bot.send_message(
            call.message.chat.id,
            "✍️ <b>কাস্টম কোড</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 প্যাক: <b>{_rc_pack_label(parts[2], int(parts[3]))}</b>\n\n"
            "এই ফরম্যাটে পাঠান:\n<code>CODE MAX_USES</code>\n\n"
            "উদাহরণ: <code>EID2026 50</code>\n→ 'EID2026' কোডটি সর্বোচ্চ ৫০ জন ইউজার রিডিম করতে পারবে\n\n"
            "• MAX_USES না দিলে ১ জন\n• MAX_USES = 0 দিলে আনলিমিটেড\n"
            "• কোডে শুধু ইংরেজি লেটার/সংখ্যা/ড্যাশ (৪-৩২ অক্ষর)",
            parse_mode="HTML"
        )

    elif action == "list":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        text, markup = build_redeem_list_view(page)
        _edit_or_send(call, text, markup)

    elif action in ("view", "off", "on") and len(parts) >= 3:
        code = "_".join(parts[2:])
        if action == "off":
            admin.set_redeem_code_active(code, False)
        elif action == "on":
            admin.set_redeem_code_active(code, True)
        text, markup = build_redeem_detail_view(code)
        if not text:
            bot.send_message(call.message.chat.id, "❌ কোডটি পাওয়া যায়নি।")
            return
        _edit_or_send(call, text, markup)

def process_user_redeem(chat_id: int, user_id: int, username: str, raw_code: str):
    """ইউজারের দেওয়া কোড রিডিম করে সাকসেস/এরর কার্ড পাঠায় (বাটন, /redeem কমান্ড ও টেক্সট — তিন পথেই এটা ব্যবহার হয়)।"""
    ok, res = admin.redeem_code(user_id, username, raw_code)
    if not ok:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("🔁 আবার চেষ্টা করুন", callback_data="user_redeem_start", style="success"),
            make_inline_button("🎫 সাপোর্টে যোগাযোগ", callback_data="back_to_support", style="primary"),
        )
        bot.send_message(chat_id, f"❌ <b>রিডিম ব্যর্থ!</b>\n{html.escape(str(res))}", reply_markup=markup, parse_mode="HTML")
        return

    _award_first_upgrade_badge(user_id)
    u_data = admin.get_user(user_id)
    try:
        pinfo = admin.get_plan_display_info(u_data)
        plan_line = f"👑 <b>বর্তমান প্ল্যান:</b> {pinfo['icon']} {html.escape(str(pinfo['display_name']))}\n⏳ <b>মেয়াদ শেষ:</b> <code>{pinfo['expiry_str']}</code>\n"
    except Exception:
        plan_line = ""
    try:
        slots_line = f"🤖 <b>বট লিমিট:</b> {admin.get_max_bot_limit_for_user(u_data)}টি\n"
    except Exception:
        slots_line = ""
    card = (
        "🎉 <b>রিডিম সফল!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 <b>কোড:</b> <code>{res['code']}</code>\n"
        f"📦 <b>প্যাক:</b> {_rc_pack_label(res['tier_key'], res['days'])}\n"
        f"{plan_line}{slots_line}"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 এখন আপনার বট ২৪/৭ চালাতে পারবেন!"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        make_inline_button("🎛️ Control Panel", callback_data="back_to_cp", style="success"),
        make_inline_button("🏠 Home Menu", callback_data="go_home", style="primary"),
    )
    bot.send_message(chat_id, card, reply_markup=markup, parse_mode="HTML")

    admin_alert = (
        "🎁 <b>Redeem Code ব্যবহৃত হয়েছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 @{html.escape(username or 'Anonymous')} (ID: <code>{user_id}</code>)\n"
        f"🔑 <code>{res['code']}</code>\n"
        f"📦 {_rc_pack_label(res['tier_key'], res['days'])}"
    )
    for aid in admin.ADMIN_IDS:
        try:
            bot.send_message(aid, admin_alert, parse_mode="HTML")
        except Exception:
            pass

def send_redeem_prompt(chat_id: int, user_id: int):
    """ইউজারকে কোড লেখার প্রম্পট দেখায় এবং স্টেট সেট করে।"""
    user_states[user_id] = {"state": "awaiting_redeem_input"}
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("❌ বাতিল", callback_data="user_redeem_cancel", style="danger"))
    bot.send_message(
        chat_id,
        "🎁 <b>Redeem Code</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "আপনার রিডিম কোডটি লিখে পাঠান — কোড সঠিক হলে প্ল্যান সাথে সাথে চালু হয়ে যাবে।\n\n"
        "উদাহরণ: <code>PYH-ABCD-EFGH-JKLM</code>\n"
        "💡 কোড না থাকলে <b>💎 Upgrade Plan</b> থেকে প্ল্যান কিনতে পারেন।",
        reply_markup=markup, parse_mode="HTML"
    )

# যে স্টেটগুলোতে মেনুর বাটন চাপলে ইনপুট-মোড বাতিল হয়ে বাটনটি স্বাভাবিকভাবে কাজ করবে
# (আগে এই অবস্থায় বাটনের লেখাটাই টিকিট/কোড হিসেবে ধরা হতো)
_NAV_CANCELLABLE_STATES = {
    "awaiting_redeem_input", "awaiting_devstudio_request", "awaiting_ticket_message",
    "awaiting_redeem_days", "awaiting_redeem_custom", "awaiting_vip_unlock_custom",
    "awaiting_deploy_token", "awaiting_deploy_extra", "awaiting_cm_name", "awaiting_cm_desc",
    "awaiting_cm_code", "awaiting_cm_reject_reason",
}
_NAV_BUTTON_TEXTS = {
    "🎛️ Control Panel", "📁 File Manager", "👤 My Account", "💎 Upgrade Plan", "🤖 Marketplace",
    "🐙 GitHub Deploy", "📖 Documentation", "📡 Support Hub", "🎁 Redeem Code", "👑 Admin Panel",
    "🔙 User Menu", "🔙 Admin Menu", "📊 Monitoring", "👥 User Manager", "💰 Billing & Plans",
    "⚙️ System", "📈 Analytics", "🎁 Redeem Codes", "🎟️ Coupon Manager", "💎 Grant VIP (Manual)",
    "🆓 Free Hosting (12h)", "🔓 VIP All Slot Unlock", "🌐 Community Review", "💾 Data Backup", "🧹 Channel Clean",
}

# ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — ভিউ বিল্ডার ফাংশনসমূহ
TICKET_STATUS_ICON = {"OPEN": "🟢", "REPLIED": "🟡", "CLOSED": "🔒"}
TICKET_STATUS_LABEL = {"OPEN": "Open", "REPLIED": "Replied", "CLOSED": "Closed"}

def build_user_tickets_view(user_id: int, page: int = 0):
    """ইউজারের নিজের সব টিকিটের লিস্ট — নতুন টিকিট খোলার বাটনসহ।"""
    tickets = admin.get_user_tickets(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("📝 নতুন টিকিট খুলুন", callback_data="new_ticket", style="success"))

    if not tickets:
        add_home_button(markup)
        return "🎫 <b>My Tickets</b>\n━━━━━━━━━━━━━━━━━━━━━━\n<i>আপনার কোনো টিকিট নেই।</i>", markup

    items, page, total_pages = admin.paginate(tickets, page, per_page=6)
    text = f"🎫 <b>My Tickets</b> — মোট <code>{len(tickets)}</code> টি (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"
    for t in items:
        icon = TICKET_STATUS_ICON.get(t["status"], "⚪")
        label = f"{icon} #{t['ticket_id']} • {t['subject']} ({TICKET_STATUS_LABEL.get(t['status'], t['status'])})"
        markup.add(make_inline_button(label, callback_data=f"ticket_view_{t['ticket_id']}", style="primary"))

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"my_tickets_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"my_tickets_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)
    add_home_button(markup)
    return text, markup

def build_ticket_thread_view(ticket_id: int, is_admin: bool = False):
    """একটা টিকিটের পুরো মেসেজ থ্রেড রেন্ডার করে — ইউজার ও অ্যাডমিন দুই ভিউতেই ব্যবহার হয়,
    শুধু বাটন ও ব্যাক-নেভিগেশন আলাদা।"""
    ticket = admin.get_ticket(ticket_id)
    if not ticket:
        return "❌ টিকিটটি পাওয়া যায়নি।", None

    msgs = admin.get_ticket_messages(ticket_id)
    icon = TICKET_STATUS_ICON.get(ticket["status"], "⚪")
    header = (
        f"🎫 <b>Ticket #{ticket_id}</b> — {icon} {TICKET_STATUS_LABEL.get(ticket['status'], ticket['status'])}\n"
        f"👤 <code>{ticket['username'] or ticket['user_id']}</code> (ID: <code>{ticket['user_id']}</code>)\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    lines = [header]
    for m in msgs:
        sender_label = "👤 You" if (m["sender"] == "user" and not is_admin) else ("👨‍💼 Admin" if m["sender"] == "admin" else "👤 User")
        lines.append(f"<b>{sender_label}</b> <i>({m['created_at']})</i>\n{html.escape(m['message'])}")
    body = "\n\n".join(lines)

    markup = types.InlineKeyboardMarkup(row_width=1)
    if ticket["status"] != "CLOSED":
        if is_admin:
            markup.add(make_inline_button("💬 Reply", callback_data=f"adm_ticket_reply_{ticket_id}", style="primary"))
            markup.add(make_inline_button("🔒 Close Ticket", callback_data=f"adm_ticket_close_{ticket_id}", style="danger"))
        else:
            markup.add(make_inline_button("💬 রিপ্লাই দিন", callback_data=f"ticket_reply_{ticket_id}", style="primary"))
            markup.add(make_inline_button("🔒 টিকিট বন্ধ করুন", callback_data=f"ticket_close_{ticket_id}", style="danger"))

    if is_admin:
        markup.add(make_inline_button("🔙 Support Tickets", callback_data="adm_tickets_page_0", style="primary"))
    else:
        markup.add(make_inline_button("🔙 My Tickets", callback_data="my_tickets_page_0", style="primary"))

    return body, markup

def build_admin_tickets_view(page: int = 0):
    """অ্যাডমিনের সাপোর্ট টিকিট লিস্ট — OPEN/REPLIED (নজর দরকার) আগে, CLOSED পরে।"""
    open_tickets = admin.get_all_tickets(status="OPEN") + admin.get_all_tickets(status="REPLIED")
    closed_tickets = admin.get_all_tickets(status="CLOSED", limit=30)
    all_tickets = sorted(open_tickets, key=lambda t: t["updated_at"], reverse=True) + closed_tickets

    if not all_tickets:
        return "✅ <b>কোনো সাপোর্ট টিকিট নেই।</b>", None

    items, page, total_pages = admin.paginate(all_tickets, page, per_page=8)
    open_count = len(open_tickets)
    text = f"🎫 <b>Support Tickets</b> — <code>{open_count}</code> টি নজর দরকার (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for t in items:
        icon = TICKET_STATUS_ICON.get(t["status"], "⚪")
        label = f"{icon} #{t['ticket_id']} • @{t['username'] or 'N/A'} • {t['subject']}"
        markup.add(make_inline_button(label, callback_data=f"adm_ticket_view_{t['ticket_id']}", style="primary"))

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_tickets_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_tickets_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)
    return text, markup

# ----------------- অ্যাডমিন: পেজিনেটেড পেন্ডিং রিকোয়েস্ট ভিউ (স্প্যাম কমাতে ও সহজে ব্রাউজ করতে) -----------------
def build_pending_requests_view(page: int = 0):
    pending = admin.get_pending_vip_requests()
    if not pending:
        return "✅ <b>কোনো পেন্ডিং সাবস্ক্রিপশন রিকোয়েস্ট নেই।</b>", None

    items, page, total_pages = admin.paginate(pending, page, per_page=3)

    text_parts = [f"📋 <b>পেন্ডিং রিকোয়েস্ট</b> — মোট <code>{len(pending)}</code> টি (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"]
    markup = types.InlineKeyboardMarkup(row_width=2)
    for req in items:
        req_id = req["request_id"]
        text_parts.append(
            f"📦 <b>Req #{req_id}</b> | UID: <code>{req['user_id']}</code> (@{req['username']})\n"
            f"Plan: <code>{req['plan_name']}</code> ({req.get('gateway','bKash')})\n"
            f"Price: <code>{req['price']}</code> | TrxID: <code>{req['trx_id']}</code>"
        )
        markup.add(
            make_inline_button(f"✅ Approve #{req_id}", callback_data=f"adm_app_vip_{req_id}", style="success"),
            make_inline_button(f"❌ Reject #{req_id}", callback_data=f"adm_rej_vip_{req_id}", style="danger")
        )

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_reqs_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_reqs_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)

    return "\n\n".join(text_parts), markup

# ----------------- অ্যাডমিন: পেজিনেটেড ও সার্চযোগ্য ইউজার ডাইরেক্টরি -----------------
def build_user_directory_view(page: int = 0, query: str = ""):
    users = admin.search_users(query) if query else admin.get_all_users()
    total_all = len(admin.get_all_users())

    if not users:
        return f"👥 <b>কোনো ইউজার পাওয়া যায়নি</b> (মোট রেজিস্টার্ড: <code>{total_all}</code>)।", None

    items, page, total_pages = admin.paginate(users, page, per_page=8)

    header = f"👥 <b>ইউজার ডাইরেক্টরি</b> — মোট <code>{total_all}</code> ক্লায়েন্ট"
    if query:
        header += f"\n🔎 সার্চ ফলাফল: <code>{query}</code> ({len(users)} মিলেছে)"
    header += f"\n(পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"

    markup = types.InlineKeyboardMarkup(row_width=1)
    for u in items:
        ban_icon = "🚫" if u.get("is_banned") else "🟢"
        label = f"{ban_icon} @{u.get('username') or 'N/A'} • {u.get('plan','FREE')} • {u['user_id']}"
        markup.add(make_inline_button(label, callback_data=f"adm_view_user_{u['user_id']}", style="primary"))

    nav_row = []
    page_prefix = f"adm_users_page_{{}}_" + (query or "-")
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_users_page_{page-1}_{query or '-'}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_users_page_{page+1}_{query or '-'}", style="primary"))
    if nav_row:
        markup.add(*nav_row)

    tip = "\n\n💡 নির্দিষ্ট ইউজার খুঁজতে: <code>/find username_or_id</code>"
    return header + tip, markup

def build_admin_user_profile_card(target_uid: int):
    """অ্যাডমিন ক্লায়েন্ট প্রোফাইল কার্ড — /user কমান্ড ও ইউজার ডাইরেক্টরি দুই জায়গা থেকেই ব্যবহার হয়।"""
    u = admin.get_user(target_uid)
    projects = admin.get_user_projects(target_uid)
    is_ban = bool(u.get("is_banned", 0))

    card = (
        "👤 <b>ADMIN CLIENT PROFILE CARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"👤 <b>Username:</b> @{u.get('username', 'None')}\n"
        f"💎 <b>Plan:</b> <code>{u.get('plan', 'FREE')}</code> (Exp: <code>{u.get('plan_expiry', 'LIFETIME')}</code>)\n"
        f"📦 <b>Total Bots:</b> <code>{len(projects)}</code>\n"
        f"👥 <b>Referrals:</b> <code>{u.get('referral_count', 0)}</code> Clients\n"
        f"🚫 <b>Status:</b> <code>{'🔴 BANNED' if is_ban else '🟢 ACTIVE'}</code>\n"
        f"📅 <b>Joined:</b> <code>{u.get('joined_date', 'Unknown')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("💎 Grant Pro (7d)", callback_data=f"adm_grant_pro_{target_uid}", style="success"),
        make_inline_button("💎 Grant Prem (30d)", callback_data=f"adm_grant_vip_{target_uid}", style="success")
    )
    markup.add(
        make_inline_button("🗂 Manage Plan", callback_data=f"adm_plan_menu_{target_uid}", style="primary"),
        make_inline_button("📦 User's Bots", callback_data=f"adm_userbots_{target_uid}", style="primary")
    )
    markup.add(
        make_inline_button("📜 Audit Log", callback_data=f"adm_auditlog_{target_uid}", style="primary"),
        make_inline_button("📩 Send DM", callback_data=f"adm_dm_{target_uid}", style="primary")
    )
    markup.add(
        make_inline_button("🚫 Unban" if is_ban else "🚫 Ban", callback_data=f"adm_ban_confirm_{target_uid}", style="danger")
    )
    markup.add(make_inline_button("🔙 User Directory", callback_data="adm_users_page_0_-", style="primary"))
    return card, markup

# ----------------- ইনস্ট্যান্ট স্টার্ট ও কমান্ড হ্যান্ডলারস (< 0.2s Response) -----------------

def send_onboarding_tutorial(chat_id, user_id):
    """নতুন ইউজারের জন্য ধাপে ধাপে ইন্টারেক্টিভ Tutorial — স্বয়ংক্রিয়ভাবে Welcome-এর পরে আসে।"""
    steps = [
        (
            "🎓 <b>PyHost Tutorial — ধাপ ১/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>বট স্লট কী?</b>\n"
            "প্রতিটা বট স্লট = একটা আলাদা বট চালানোর জায়গা।\n"
            f"Free প্ল্যানে ২টা স্লট পাবেন — প্রতিটা দিনে সর্বোচ্চ {to_bn_num(admin.SYSTEM_CONFIG.get('free_hosting_hours', 12))} ঘণ্টা চলবে।\n\n"
            "👉 <b>পরবর্তী ধাপে যেতে নিচের বাটন চাপুন:</b>",
            "tut_step_2"
        ),
        (
            "🎓 <b>PyHost Tutorial — ধাপ ২/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📤 <b>বট কীভাবে আপলোড করবেন?</b>\n"
            "① <b>🎛️ Control Panel</b> খুলুন\n"
            "② <b>➕ New Bot</b> বা <b>Slot (Free)</b> ট্যাপ করুন\n"
            "③ বটের একটা নাম দিন\n"
            "④ আপনার Python ফাইল (.py) বা .zip পাঠান\n"
            "⑤ বট অটো স্টার্ট হয়ে যাবে! 🚀\n\n"
            "💡 <b>Tip:</b> main.py বা bot.py নামের ফাইল অটো-ডিটেক্ট হয়।",
            "tut_step_3"
        ),
        (
            "🎓 <b>PyHost Tutorial — ধাপ ৩/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🐙 <b>GitHub থেকে ডিপ্লয় (VIP)</b>\n"
            "পেইড প্ল্যানে GitHub repo লিংক দিলেই বট অটো-ডিপ্লয় হবে।\n"
            "পরে repo আপডেট করলে <b>GitHub Sync</b> বাটনে একক ক্লিকেই আপডেট!\n\n"
            "🛒 <b>Marketplace</b> — কোনো কোড ছাড়াই রেডিমেড বট ১ ক্লিকে চালু করুন।\n\n"
            "💎 <b>প্ল্যান আপগ্রেড করলে:</b>\n"
            "• 24/7 বট চালু থাকবে\n"
            "• বেশি স্লট পাবেন\n"
            "• GitHub Deploy ও আরো ফিচার",
            "tut_step_4"
        ),
        (
            "🎓 <b>PyHost Tutorial — ধাপ ৪/৪ (শেষ!)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>আপনার করণীয় Checklist:</b>\n"
            "⬜ প্রথম বট তৈরি করুন (Control Panel → New Bot)\n"
            "⬜ একটি .py ফাইল আপলোড করুন\n"
            "⬜ বট রান হতে দেখুন (Bot Dashboard)\n"
            "⬜ বন্ধুকে রেফার করুন (My Account → Share Referral)\n\n"
            "❓ <b>সমস্যা হলে:</b> নিচের মেনু থেকে <b>📖 Documentation</b> বা <b>📡 Support Hub</b> ব্যবহার করুন।\n\n"
            "🎉 সব ঠিকঠাক! শুরু করুন →",
            "tut_done"
        ),
    ]
    # প্রথম ধাপ পাঠাই
    markup = types.InlineKeyboardMarkup()
    markup.add(make_inline_button("▶️ পরের ধাপ →", callback_data=steps[0][1], style="success"))
    markup.add(make_inline_button("⏭️ Tutorial Skip করুন", callback_data="tut_skip", style="primary"))
    bot.send_message(chat_id, steps[0][0], reply_markup=markup, parse_mode="HTML")

ONBOARDING_STEPS = [
    ("tut_step_2", 1),
    ("tut_step_3", 2),
    ("tut_step_4", 3),
    ("tut_done", None),
]

def get_onboarding_step_content(step_key):
    texts = {
        "tut_step_2": (
            "🎓 <b>PyHost Tutorial — ধাপ ২/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📤 <b>বট কীভাবে আপলোড করবেন?</b>\n"
            "① <b>🎛️ Control Panel</b> খুলুন\n"
            "② <b>➕ New Bot</b> বা যেকোনো <b>Slot (Free)</b> ট্যাপ করুন\n"
            "③ বটের একটা নাম দিন\n"
            "④ আপনার Python ফাইল (.py) বা .zip পাঠান\n"
            "⑤ বট অটো স্টার্ট হয়ে যাবে! 🚀\n\n"
            "💡 main.py বা bot.py অটো-ডিটেক্ট হয়।"
        ),
        "tut_step_3": (
            "🎓 <b>PyHost Tutorial — ধাপ ৩/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🐙 <b>GitHub Deploy (VIP)</b> — repo লিংক দিলেই ডিপ্লয়!\n"
            "🛒 <b>Marketplace</b> — কোড ছাড়াই রেডিমেড বট চালু!\n\n"
            "💎 <b>পেইড প্ল্যানে:</b> 24/7, বেশি স্লট, GitHub Sync"
        ),
        "tut_step_4": (
            "🎓 <b>PyHost Tutorial — ধাপ ৪/৪ (শেষ!)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>আপনার Checklist:</b>\n"
            "⬜ Control Panel → New Bot → ফাইল আপলোড\n"
            "⬜ বট Dashboard থেকে লগ দেখুন\n"
            "⬜ বন্ধুকে Refer করুন (My Account)\n\n"
            "❓ সমস্যায় → 📖 Documentation / 📡 Support Hub\n"
            "🎉 শুরু করুন!"
        ),
    }
    next_map = {"tut_step_2": "tut_step_3", "tut_step_3": "tut_step_4", "tut_step_4": "tut_done"}
    return texts.get(step_key, ""), next_map.get(step_key)

@bot.message_handler(commands=['start'])

def cmd_start(message):
    user_id = message.from_user.id
    referrer_id = None
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].replace("ref_", ""))
        except Exception:
            pass

    welcome_text = (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "┃ ☁️ PyHost Multi-Bot Cloud 🚀\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "⚡ <b>মাল্টি-বট হোস্টিং</b>\n"
        "একই অ্যাকাউন্টে একাধিক বট চালান।\n\n"
        "📦 <b>স্মার্ট ফাইল আপডেট</b>\n"
        "ফাইল রিপ্লেস, ZIP Backup ও Realtime Logs।\n\n"
        "🛒 <b>বট মার্কেটপ্লেস</b>\n"
        "কোডিং ছাড়াই রেডিমেড বট চালু করুন।\n\n"
        "🎁 <b>রেফারেল বোনাস</b>\n"
        "৩ জন বন্ধুকে Invite করলে ৩ দিন VIP ফ্রি।\n\n"
        "🔐 <b>নিরাপদ হোস্টিং</b>\n"
        "আপনার তথ্য, কোড ও আপলোড করা ফাইল নিরাপদে সংরক্ষিত থাকবে।\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 শুরু করতে নিচের বাটন ব্যবহার করুন।\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_user_keyboard(user_id), parse_mode="HTML")

    def _async_reg():
        is_new_user, bonus_ref_id = admin.register_user(user_id, message.from_user.username, referrer_id)

        # নতুন ইউজার জয়েন-নোটিফিকেশন — সম্পূর্ণ সাইলেন্ট, ইউজার কিছু বুঝবে না
        if is_new_user:
            uname = message.from_user.username
            full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])) or "-"
            join_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ref_line = f"🔗 <b>Referred by:</b> <code>{referrer_id}</code>\n" if referrer_id else ""
            notify_text = (
                "🆕 <b>NEW USER JOINED!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                f"👤 <b>Username:</b> @{uname or 'Anonymous'}\n"
                f"📛 <b>Name:</b> {html.escape(full_name)}\n"
                f"🕒 <b>Joined:</b> <code>{join_time}</code>\n"
                f"{ref_line}"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, notify_text, parse_mode="HTML")
                except Exception:
                    pass
            # Tutorial শুধু নতুন ইউজারদের জন্য — ৩ সেকেন্ড পর পাঠানো হয়
            import time as _t
            _t.sleep(3)
            try:
                send_onboarding_tutorial(message.chat.id, user_id)
            except Exception:
                pass

        if bonus_ref_id:
            try:
                bot.send_message(
                    bonus_ref_id,
                    f"🎉 <b>অভিনন্দন!</b> ৩ জন সফল রেফারে আপনি <b>{admin.SYSTEM_CONFIG['referral_bonus_days']} দিনের ফ্রি Premium VIP প্ল্যান</b> বোনাস পেয়েছেন! 🚀",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        # রেফারেল অ্যাচিভমেন্ট ব্যাজ (৫ জন / ১০ জন)
        if referrer_id:
            try:
                ref_user = admin.get_user(referrer_id)
                rc = ref_user.get("referral_count") or 0
                for threshold, badge_key in ((5, "REFER_5"), (10, "REFER_10")):
                    if rc >= threshold and admin.award_badge(referrer_id, badge_key):
                        b = admin.ACHIEVEMENTS[badge_key]
                        bot.send_message(
                            referrer_id,
                            f"{b['emoji']} <b>নতুন ব্যাজ অর্জিত: {b['title']}!</b>\n{b['desc']} 🎉",
                            parse_mode="HTML"
                        )
            except Exception:
                pass
    threading.Thread(target=_async_reg, daemon=True).start()

@bot.message_handler(commands=['user'])
def cmd_user_admin_search(message):
    user_id = message.from_user.id
    if not admin.is_admin(user_id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ <b>Usage:</b> <code>/user &lt;user_id&gt;</code>")
        return

    try:
        target_uid = int(args[1].strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid User ID!")
        return

    card, markup = build_admin_user_profile_card(target_uid)
    bot.send_message(message.chat.id, card, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['find'])
def cmd_find_users(message):
    """অ্যাডমিন ইউজারনেইম বা আইডি দিয়ে সার্চ করতে পারবে (পেজিনেশন সহ)।"""
    user_id = message.from_user.id
    if not admin.is_admin(user_id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ <b>Usage:</b> <code>/find &lt;username বা user_idর অংশ&gt;</code>")
        return

    query = args[1].strip()
    text, markup = build_user_directory_view(page=0, query=query)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['redeem'])
def cmd_redeem(message):
    """/redeem            -> কোড লেখার প্রম্পট
    /redeem PYH-XXXX-...  -> সরাসরি রিডিম"""
    user_id = message.from_user.id
    if not check_rate_limit(user_id):
        return
    if admin.get_user(user_id).get("is_banned"):
        bot.send_message(message.chat.id, "🚫 <b>আপনার একাউন্টটি ব্যান করা হয়েছে।</b>")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        send_redeem_prompt(message.chat.id, user_id)
    else:
        user_states.pop(user_id, None)
        process_user_redeem(message.chat.id, user_id, message.from_user.username, args[1])

# ----------------- টেক্সট ও স্টেট হ্যান্ডলারস -----------------
@bot.message_handler(func=lambda msg: True)
def handle_all_text_navigation(message):
    user_id = message.from_user.id
    if not check_rate_limit(user_id):
        return   # সাইলেন্টলি ইগনোর — স্প্যাম প্রতিরোধ

    message.text = normalize_button_text(message.text)
    text = message.text

    u_data = admin.get_user(user_id)
    if u_data.get("is_banned"):
        bot.send_message(message.chat.id, "🚫 <b>আপনার একাউন্টটি ব্যান করা হয়েছে।</b>")
        return

    # স্টেট হ্যান্ডলারস
    if user_id in user_states:
        state_data = user_states[user_id]
        state_name = state_data if isinstance(state_data, str) else state_data.get("state")

        # ইনপুট-মোডে থাকা অবস্থায় মেনুর বাটন চাপলে ইনপুট-মোড বাতিল হয়ে বাটনটিই কাজ করবে
        if state_name in _NAV_CANCELLABLE_STATES and text in _NAV_BUTTON_TEXTS:
            del user_states[user_id]
            state_name = None

        # ১. অ্যাডমিন গ্লোবাল ব্রডকাস্ট
        if state_name == "awaiting_broadcast" and admin.is_admin(user_id):
            del user_states[user_id]
            users = admin.get_all_users()
            total = len(users)
            
            prog = bot.send_message(message.chat.id, f"📢 <b>Broadcast শুরু হচ্ছে ({total} ইউজার)...</b>", parse_mode="HTML")
            
            def _run_broadcast():
                success = 0
                failed = 0
                for u in users:
                    try:
                        bot.send_message(u["user_id"], f"📢 <b>PYHOST OFFICIAL ANNOUNCEMENT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
                        success += 1
                    except Exception:
                        failed += 1
                    time.sleep(0.04)
                bot.edit_message_text(f"✅ <b>Broadcast সম্পন্ন!</b>\nসফল: <code>{success}</code> | ব্যর্থ: <code>{failed}</code>", message.chat.id, prog.message_id, parse_mode="HTML")

            threading.Thread(target=_run_broadcast, daemon=True).start()
            return

        # ২. অ্যাডমিন ডাইরেক্ট ডিএম
        elif state_name == "awaiting_admin_dm" and admin.is_admin(user_id):
            target_uid = state_data.get("target_uid")
            del user_states[user_id]
            try:
                bot.send_message(target_uid, f"📩 <b>PyHost Support Message from Admin:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_uid}</code>-কে মেসেজ পাঠানো হয়েছে!")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ মেসেজ পাঠানো যায়নি: {e}")
            return

        # ৩. অ্যাডমিন ম্যানুয়াল ভিআইপি আইডি
        elif state_name == "awaiting_vip_id" and admin.is_admin(user_id):
            del user_states[user_id]
            try:
                target_id = int(text.strip())
                admin.set_user_plan(target_id, "PREMIUM", 30)
                _award_first_upgrade_badge(target_id)
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_id}</code>-কে Premium (30 Days) দেওয়া হয়েছে!")
                try:
                    bot.send_message(target_id, "🎉 <b>অভিনন্দন! আপনার অ্যাকাউন্টটি Premium প্ল্যানে আপগ্রেড করা হয়েছে।</b> (24/7 Unlimited Run)")
                except Exception:
                    pass
            except ValueError:
                bot.send_message(message.chat.id, "❌ অনুগ্রহ করে সঠিক Telegram User ID দিন।")
            return

        # ৪. অ্যাডমিন ব্যান আইডি
        elif state_name == "awaiting_ban_id" and admin.is_admin(user_id):
            del user_states[user_id]
            try:
                target_id = int(text.strip())
                u_info = admin.get_user(target_id)
                new_status = not bool(u_info.get("is_banned", 0))
                admin.set_user_ban(target_id, new_status)
                status_str = "BANNED" if new_status else "UNBANNED"
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_id}</code> স্ট্যাটাস: <b>{status_str}</b>!")
            except ValueError:
                bot.send_message(message.chat.id, "❌ অনুগ্রহ করে সঠিক Telegram User ID দিন।")
            return

        elif state_name == "awaiting_new_coupon" and admin.is_admin(user_id):
            del user_states[user_id]
            parts = text.strip().split()
            if len(parts) != 3:
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট! এভাবে দিন: <code>CODE DISCOUNT MAX_USES</code>\nউদাহরণ: <code>EID50 50 100</code>", parse_mode="HTML")
                return
            code_in, disc_in, uses_in = parts
            try:
                disc_int = int(disc_in)
                uses_int = int(uses_in)
            except ValueError:
                bot.send_message(message.chat.id, "❌ DISCOUNT ও MAX_USES অবশ্যই সংখ্যা হতে হবে।")
                return
            success, msg = admin.create_coupon(code_in, disc_int, uses_int, user_id)
            bot.send_message(message.chat.id, msg, parse_mode="HTML")
            return

        elif state_name == "awaiting_deactivate_coupon" and admin.is_admin(user_id):
            del user_states[user_id]
            code_in = text.strip().upper()
            if admin.deactivate_coupon(code_in):
                bot.send_message(message.chat.id, f"✅ কুপন <code>{html.escape(code_in)}</code> বাতিল করা হয়েছে।", parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, f"❌ <code>{html.escape(code_in)}</code> নামে কোনো কুপন পাওয়া যায়নি।", parse_mode="HTML")
            return

        # ৫. কুপন কোড ইনপুট হ্যান্ডলার
        elif state_name == "awaiting_coupon_code":
            tier_key = state_data["tier_key"]
            coupon_input = text.strip().upper()
            del user_states[user_id]

            # DB-backed কুপন চেক — আগে hardcoded dict ছিল, এখন use_coupon() দিয়ে
            # একই কুপন একই ইউজার বারবার ব্যবহার করতে পারবে না, ম্যাক্স ইউজ লিমিটও চেক হয়
            success, result = admin.use_coupon(coupon_input, user_id)
            if success:
                disc = result  # disc = discount percentage (int)
                bot.send_message(
                    message.chat.id,
                    f"🎉 <b>কুপন সফল!</b> আপনি <b>{disc}%</b> ডিসকাউন্ট পেয়েছেন!",
                    parse_mode="HTML"
                )
                sum_text, sum_markup = get_order_summary_view(tier_key, discount_percent=disc)
                bot.send_message(message.chat.id, sum_text, reply_markup=sum_markup, parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, f"❌ <b>কুপন ব্যর্থ:</b> {html.escape(str(result))}", parse_mode="HTML")
                sum_text, sum_markup = get_order_summary_view(tier_key, discount_percent=0)
                bot.send_message(message.chat.id, sum_text, reply_markup=sum_markup, parse_mode="HTML")
            return


        # ৬. নতুন বটের নাম (স্লট চাপার পর)
        # ⭐ FIX: আগে এখানে যা-ই লেখা হোক (এমনকি কোড/এলোমেলো টেক্সট) সাথে সাথে একটা প্রজেক্ট (স্লট) তৈরি হয়ে যেত — ফাইল না এলেও স্লট আটকে থাকত।
        # এখন: কোড পেস্ট করলে সেটাই ডেপ্লয় হয়; নাম হলে শুধু নামটা মনে রাখা হয় — স্লট/প্রজেক্ট তৈরি হয় ফাইল বা কোড আসার পরে।
        elif state_name == "awaiting_bot_name":
            target_slot = state_data.get("slot_number") if isinstance(state_data, dict) else None
            raw_text = text or ""
            if _looks_like_code(raw_text):
                pid, _err = _materialize_pending_project(user_id, {"pending_name": "My Bot", "slot_number": target_slot}, u_data)
                if pid is None:
                    user_states.pop(user_id, None)
                    _slots_full_notice(message.chat.id, user_id, u_data)
                    return
                _deploy_pasted_code(message, user_id, pid, raw_text)
                return
            bot_name = " ".join(raw_text.split())[:30]
            if not bot_name or bot_name.startswith("/"):
                bot.send_message(message.chat.id, "✏️ <b>বটের একটা নাম লিখুন</b> (যেমন: <i>My Support Bot</i>) — অথবা সরাসরি <b>.py / .zip ফাইল</b> পাঠান।", parse_mode="HTML")
                return
            safe_bot_name = html.escape(bot_name)  # HTML পার্সিং ভাঙা এড়াতে ইউজার-দেওয়া নাম এসকেপ করা হচ্ছে
            user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": None, "pending_name": bot_name,
                                    "slot_number": target_slot, "ts": time.time()}
            card_text, card_markup = get_upload_guide_card()
            created_box = (
                "╭━━━━━━━━━━━━━━━━━━╮\n"
                f"┃  ✅ নাম সেট: <b>{safe_bot_name}</b>\n"
                "╰━━━━━━━━━━━━━━━━━━╯\n"
                "📎 এখন <b>.py / .zip ফাইল</b> পাঠান বা কোড পেস্ট করুন — ফাইল আসার পর স্লট তৈরি হবে।\n\n"
            )
            bot.send_message(message.chat.id, created_box + card_text, reply_markup=card_markup, parse_mode="HTML")
            return

        # ৭. সরাসরি কোড পেস্ট করে ডেপ্লয় (প্রজেক্ট আগে থেকে তৈরি থাকলে, অথবা স্লট চাপার পর নাম দিয়ে ফাইল-অপেক্ষায় থাকলে এখানেই তৈরি হবে)
        elif state_name == "awaiting_file_for_bot" and ("import " in text or "def " in text or "bot =" in text or "print(" in text) \
                and time.time() - state_data.get("ts", time.time()) <= UPLOAD_EXPLICIT_TTL \
                and (state_data.get("pending_name") is not None
                     or (admin.get_project_by_id(state_data.get("project_id")) or {}).get("user_id") == user_id):
            pid = state_data.get("project_id")
            if pid is None:
                pid, _err = _materialize_pending_project(user_id, state_data, u_data)
                if pid is None:
                    user_states.pop(user_id, None)
                    _slots_full_notice(message.chat.id, user_id, u_data)
                    return
            _deploy_pasted_code(message, user_id, pid, text)
            return

        # ৮. মার্কেটপ্লেস অটো-ডেপ্লয় টোকেন
        elif state_name == "awaiting_deploy_token":
            bot_token = text.strip()
            template_type = state_data["template"]
            del user_states[user_id]

            if not re.match(r"^\d{8,10}:[a-zA-Z0-9_-]{35}$", bot_token):
                bot.send_message(message.chat.id, "❌ <b>ভুল বট টোকেন ফরম্যাট!</b> @BotFather থেকে সঠিক টোকেন এনে আবার চেষ্টা করুন।", parse_mode="HTML")
                return

            # টোকেন ভ্যালিডেশন — Telegram API-তে চেক করে আসল/কাজের টোকেন কিনা
            # এটা না থাকলে ভুল টোকেন দিয়ে স্লট নষ্ট হয়ে যেতে পারে
            v_msg = bot.send_message(message.chat.id, "🔍 <b>টোকেন যাচাই করা হচ্ছে...</b>", parse_mode="HTML")
            is_valid, val_info = validate_bot_token(bot_token)
            try:
                bot.delete_message(message.chat.id, v_msg.message_id)
            except Exception:
                pass
            if not is_valid:
                bot.send_message(
                    message.chat.id,
                    f"❌ <b>টোকেন ভ্যালিড নয়!</b>\n{html.escape(val_info)}\n\n"
                    "@BotFather থেকে সঠিক টোকেন এনে আবার চেষ্টা করুন।",
                    parse_mode="HTML"
                )
                return


            # স্লট-লিমিট আগেই চেক — যাতে স্লট ফাঁকা না থাকলে ইউজারকে এক্সট্রা ইনপুট (API Key) দিতে না হয়
            if not _mp_slot_ok(message.chat.id, user_id):
                return

            tpl_def = get_marketplace_template(template_type)
            if tpl_def is None:
                bot.send_message(message.chat.id, "❌ <b>এই টেমপ্লেটটি আর পাওয়া যাচ্ছে না।</b> Marketplace থেকে অন্য একটি বেছে নিন।", parse_mode="HTML")
                return
            extra_inputs = tpl_def.get("inputs") or []
            if extra_inputs:
                # যেসব টেমপ্লেটে টোকেন ছাড়াও বাড়তি তথ্য লাগে (যেমন AI Chatbot-এর API Key)
                user_states[user_id] = {"state": "awaiting_deploy_extra", "template": template_type,
                                        "token": bot_token, "idx": 0, "extras": {}}
                bot.send_message(message.chat.id, extra_inputs[0]["prompt"], parse_mode="HTML", disable_web_page_preview=True)
                return

            finish_marketplace_deploy(message, user_id, template_type, bot_token, {})
            return

        # ৯. পেমেন্ট TrxID সাবমিশন
        elif state_name == "awaiting_vip_trx":
            del user_states[user_id]
            trx_id = text.strip()
            plan_tier = state_data["plan_tier"]
            gateway = state_data.get("gateway", "bKash")
            price_text = state_data.get("price", "30 BDT")
            tier_info = admin.TIER_PLANS.get(plan_tier, admin.TIER_PLANS["PREMIUM"])
            days = tier_info["days"]
            plan_name = tier_info["name"]

            req_id = admin.create_vip_request(user_id, message.from_user.username, plan_name, days, price_text, gateway, trx_id, tier_key=plan_tier)

            bot.send_message(
                message.chat.id,
                "✅ <b>আপনার TrxID সফলভাবে জমা হয়েছে!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>প্যাকেজ:</b> {plan_name}\n"
                f"💳 <b>মেথড:</b> {gateway}\n"
                f"💵 <b>মূল্য:</b> {price_text}\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏳ <i>অ্যাডমিন ভেরিফাই করে অ্যাপ্রুভ করলেই আপনার প্ল্যান সক্রিয় হয়ে যাবে।</i>",
                parse_mode="HTML"
            )

            admin_markup = types.InlineKeyboardMarkup(row_width=2)
            admin_markup.add(
                make_inline_button("✅ Approve", callback_data=f"adm_app_vip_{req_id}", style="success"),
                make_inline_button("❌ Reject", callback_data=f"adm_rej_vip_{req_id}", style="danger")
            )

            admin_alert = (
                f"🔔 <b>NEW SUBSCRIPTION ORDER!</b> (#REQ-{req_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Client:</b> @{message.from_user.username or 'Anonymous'} (ID: <code>{user_id}</code>)\n"
                f"📦 <b>Tier:</b> <code>{plan_name}</code> ({days} Days)\n"
                f"💳 <b>Gateway:</b> <code>{gateway}</code>\n"
                f"💵 <b>Amount:</b> <code>{price_text}</code>\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )

            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        # স্লট মেয়াদ বৃদ্ধি (Extend Validity) পেমেন্ট TrxID সাবমিশন
        elif state_name == "awaiting_ext_trx":
            del user_states[user_id]
            trx_id = text.strip()
            pid = state_data["project_id"]
            plan_key = state_data["plan_key"]
            plan_d = admin.SLOT_EXTEND_PLANS.get(plan_key, admin.SLOT_EXTEND_PLANS["30D"])
            project = admin.get_project_by_id(pid)
            proj_name = project["project_name"] if project else f"Bot #{pid}"

            req_id = admin.create_renewal_request(user_id, message.from_user.username, pid, proj_name, plan_d["days"], plan_d["price"], "Manual", trx_id)

            bot.send_message(
                message.chat.id,
                "✅ <b>আপনার TrxID সফলভাবে জমা হয়েছে!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 <b>বট:</b> {proj_name}\n"
                f"📦 <b>প্যাকেজ:</b> {plan_d['label']}\n"
                f"💵 <b>মূল্য:</b> {plan_d['price']}\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏳ <i>অ্যাডমিন ভেরিফাই করে অ্যাপ্রুভ করলেই মেয়াদ যোগ হয়ে যাবে।</i>",
                parse_mode="HTML"
            )

            admin_markup = types.InlineKeyboardMarkup(row_width=2)
            admin_markup.add(
                make_inline_button("✅ Approve", callback_data=f"adm_app_ext_{req_id}", style="success"),
                make_inline_button("❌ Reject", callback_data=f"adm_rej_ext_{req_id}", style="danger")
            )

            admin_alert = (
                f"🔔 <b>NEW SLOT RENEWAL REQUEST!</b> (#EXT-{req_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Client:</b> @{message.from_user.username or 'Anonymous'} (ID: <code>{user_id}</code>)\n"
                f"🤖 <b>Bot:</b> <code>{proj_name}</code> (PID: {pid})\n"
                f"📦 <b>Package:</b> <code>{plan_d['label']}</code>\n"
                f"💵 <b>Amount:</b> <code>{plan_d['price']}</code>\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )

            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        elif state_name == "awaiting_github_url":
            raw_url = text.strip()
            # github.com/owner/repo বা https://github.com/owner/repo দুটো ফরম্যাটই সাপোর্ট করা হচ্ছে
            m = re.match(r"(?:https?://)?github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git|/.*)?$", raw_url)
            if not m:
                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(make_inline_button("❌ বাতিল করুন", callback_data="gh_cancel", style="danger"))
                bot.send_message(
                    message.chat.id,
                    "❌ <b>ভুল ফরম্যাট!</b> GitHub রিপোর লিংক এভাবে দিন:\n"
                    "<code>https://github.com/owner/repo</code>\n\n"
                    "🔄 আবার সঠিক লিংক পাঠান বা নিচের বাটনে বাতিল করুন।",
                    reply_markup=markup, parse_mode="HTML"
                )
                # state মুছে দেওয়া হচ্ছে না — ইউজার retry করতে পারবে
                return
            owner, repo = m.group(1), m.group(2)
            user_states[user_id] = {"state": "awaiting_github_visibility", "owner": owner, "repo": repo}
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                make_inline_button("🔓 Public Repo", callback_data="gh_repo_public", style="success"),
                make_inline_button("🔒 Private Repo", callback_data="gh_repo_private", style="primary")
            )
            markup.add(make_inline_button("🔙 Back / Cancel", callback_data="gh_cancel", style="danger"))
            bot.send_message(
                message.chat.id,
                f"🐙 <b>{html.escape(owner)}/{html.escape(repo)}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "রিপোটি Public নাকি Private?",
                reply_markup=markup, parse_mode="HTML"
            )
            return

        elif state_name == "awaiting_github_token":
            token = text.strip()
            owner, repo = state_data["owner"], state_data["repo"]
            del user_states[user_id]
            # টোকেন মেসেজটা সাথে সাথে ডিলিট করে দেওয়া হচ্ছে (নিরাপত্তার জন্য)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            status_msg = bot.send_message(
                message.chat.id,
                f"⏳ <b>GitHub থেকে '{html.escape(owner)}/{html.escape(repo)}' ডাউনলোড হচ্ছে...</b>",
                parse_mode="HTML"
            )
            threading.Thread(
                target=deploy_github_repo,
                args=(user_id, message.chat.id, status_msg.message_id, owner, repo, token, True),
                daemon=True
            ).start()
            return
        elif state_name == "awaiting_bot_notes":
            pid = state_data["pid"]
            del user_states[user_id]
            notes_text = text.strip()[:300]
            admin.set_bot_notes(pid, notes_text)
            admin.set_checklist_flag(user_id, "added_notes")
            bot.send_message(message.chat.id, f"📝 <b>নোট সেভ হয়েছে:</b> {html.escape(notes_text)}", parse_mode="HTML")
            return

        elif state_name == "awaiting_sync_token":
            token = text.strip()
            pid = state_data["pid"]
            del user_states[user_id]
            # টোকেন মেসেজ সাথে সাথে ডিলিট (নিরাপত্তা)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            project = admin.get_project_by_id(pid)
            proj_name = project["project_name"] if project else f"Bot #{pid}"
            status_msg = bot.send_message(
                message.chat.id,
                f"🔄 <b>GitHub Sync শুরু হচ্ছে...</b>\n📦 <code>{html.escape(proj_name)}</code>",
                parse_mode="HTML"
            )
            threading.Thread(
                target=sync_github_repo,
                args=(user_id, pid, message.chat.id, status_msg.message_id, token),
                daemon=True
            ).start()
            return


        elif state_name == "awaiting_env_var":
            pid = state_data["pid"]
            del user_states[user_id]
            # মেসেজ সাথে সাথে ডিলিট করা (value-তে sensitive info থাকতে পারে)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            raw = text.strip()
            if "=" not in raw:
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট। <code>KEY=VALUE</code> আকারে পাঠান।", parse_mode="HTML")
                return
            key, _, value = raw.partition("=")
            key = key.strip().upper()
            if not key:
                bot.send_message(message.chat.id, "❌ KEY খালি হতে পারবে না।")
                return
            admin.set_env_var(pid, key, value.strip())
            admin.set_checklist_flag(user_id, "env_var_set")
            bot.send_message(
                message.chat.id,
                f"✅ <b>Env Variable সেভ হয়েছে!</b>\n"
                f"🔑 <code>{html.escape(key)}</code> = <code>{'*' * min(len(value), 8)}</code>\n"
                "বট পরের বার রান হওয়ার সময় এটা .env ফাইলে লেখা হবে।",
                parse_mode="HTML"
            )
            return

        elif state_name == "awaiting_env_delete":
            pid = state_data["pid"]
            del user_states[user_id]
            key = text.strip().upper()
            if admin.delete_env_var(pid, key):
                bot.send_message(message.chat.id, f"✅ <code>{html.escape(key)}</code> মুছে ফেলা হয়েছে।", parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, f"❌ <code>{html.escape(key)}</code> নামে কোনো variable নেই।", parse_mode="HTML")
            return

        elif state_name == "awaiting_daily_report_time":
            del user_states[user_id]
            import re as _re
            time_input = text.strip()
            if not _re.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", time_input):
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট। HH:MM আকারে দিন, যেমন <code>08:00</code>", parse_mode="HTML")
                return
            # HH:MM নর্মালাইজ
            h, m = time_input.split(":")
            normalized = f"{int(h):02d}:{int(m):02d}"
            admin.set_daily_report_time(user_id, normalized)
            bot.send_message(
                message.chat.id,
                f"✅ <b>Daily Report সেট হয়েছে!</b>\n"
                f"⏰ প্রতিদিন <b>{normalized}</b> (Bangladesh Time)-এ রিপোর্ট পাবেন।",
                parse_mode="HTML"
            )
            return

        # ======================== ব্যাচ ৩ — Feedback Comment ========================
        elif state_name == "awaiting_feedback_comment":
            rating = state_data.get("rating", 5)
            star_str = "⭐" * rating
            del user_states[user_id]
            comment = "" if text.strip().lower() == "skip" else text.strip()[:300]
            admin.add_feedback(user_id, message.from_user.username or "Anonymous", rating, comment)
            bot.send_message(
                message.chat.id,
                f"{star_str} <b>\u09a7\u09a8\u09cd\u09af\u09ac\u09be\u09a6! \u0986\u09aa\u09a8\u09be\u09b0 \u09ab\u09bf\u09a1\u09ac\u09cd\u09af\u09be\u0995 \u09b8\u09c7\u09ad \u0995\u09b0\u09be \u09b9\u09df\u09c7\u099b\u09c7\u0964</b>",
                parse_mode="HTML"
            )
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, f"\u2b50 <b>New Feedback</b> ({rating}/5) from <code>{user_id}</code>:\n{html.escape(comment) if comment else '<i>No comment</i>'}", parse_mode="HTML")
                except Exception:
                    pass
            return

        # ======================== \u09ac\u099f Token Change ========================
        elif state_name == "awaiting_new_token":
            pid = state_data.get("pid")
            del user_states[user_id]
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            new_token = text.strip()
            status_msg = bot.send_message(message.chat.id, "\u23f3 \u099f\u09cb\u0995\u09c7\u09a8 \u09af\u09be\u099a\u09be\u0987 \u0995\u09b0\u09be \u09b9\u099a\u09cd\u099b\u09c7...")
            is_valid, info = validate_bot_token(new_token)
            if not is_valid:
                bot.edit_message_text(f"\u274c \u099f\u09cb\u0995\u09c7\u09a8 \u0985\u09ac\u09c8\u09a7: {info}", message.chat.id, status_msg.message_id)
                return
            admin.set_env_var(pid, "BOT_TOKEN", new_token)
            project = admin.get_project_by_id(pid)
            p_key = f"{user_id}_{pid}"
            was_running = p_key in active_processes
            restart_ok, restart_msg = (True, None)
            if was_running:
                restart_ok, restart_msg = execute_project_script(user_id, pid, preserve_restarts=True)
            if was_running and restart_ok:
                extra_line = "\U0001F504 \u09ac\u099f \u09b0\u09bf\u09b8\u09cd\u099f\u09be\u09b0\u09cd\u099f \u0995\u09b0\u09be \u09b9\u09df\u09c7\u099b\u09c7 \u09a8\u09a4\u09c1\u09a8 \u099f\u09cb\u0995\u09c7\u09a8 \u09b8\u09b9\u0964"
            elif was_running and not restart_ok:
                extra_line = f"\u26a0\ufe0f {restart_msg}"
            else:
                extra_line = "\u09a8\u09c7\u0995\u09cd\u09b8\u099f \u09ac\u09be\u09b0 \u09ac\u099f Start \u0995\u09b0\u09b2\u09c7 \u09a8\u09a4\u09c1\u09a8 \u099f\u09cb\u0995\u09c7\u09a8 \u09ac\u09cd\u09af\u09ac\u09b9\u09c3\u09a4 \u09b9\u09ac\u09c7\u0964"
            bot.edit_message_text(
                f"\u2705 <b>\u099f\u09cb\u0995\u09c7\u09a8 \u0986\u09aa\u09a1\u09c7\u099f \u09b9\u09df\u09c7\u099b\u09c7!</b>\n"
                f"\U0001F916 \u09ac\u099f: <code>{html.escape(project['project_name']) if project else pid}</code>\n"
                f"\u2705 \u09a8\u09a4\u09c1\u09a8 \u099f\u09cb\u0995\u09c7\u09a8: {info}\n"
                + extra_line,
                message.chat.id, status_msg.message_id, parse_mode="HTML"
            )
            return

        # ======================== Bot Clone \u2014 \u09a8\u09be\u09ae \u0987\u09a8\u09aa\u09c1\u099f ========================
        elif state_name == "awaiting_clone_name":
            source_pid = state_data.get("source_pid")
            del user_states[user_id]
            new_name = text.strip()[:40] or "Cloned Bot"
            new_pid, slot = admin.clone_project_record(user_id, source_pid, new_name)
            if not new_pid:
                bot.send_message(message.chat.id, "\u274c \u0995\u09cd\u09b2\u09cb\u09a8 \u09ac\u09cd\u09af\u09b0\u09cd\u09a5: \u0995\u09cb\u09a8\u09cb \u0996\u09be\u09b2\u09bf \u09b8\u09cd\u09b2\u099f \u09a8\u09c7\u0987 \u0985\u09a5\u09ac\u09be \u09ae\u09c2\u09b2 \u09ac\u099f\u099f\u09bf \u0996\u09c1\u0981\u099c\u09c7 \u09aa\u09be\u0993\u09df\u09be \u09af\u09be\u09df\u09a8\u09bf\u0964", parse_mode="HTML")
                return
            # সোর্স প্রজেক্টের ফাইল ফোল্ডার কপি
            src_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{source_pid}")
            dst_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{new_pid}")
            try:
                if os.path.exists(src_dir):
                    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                else:
                    os.makedirs(dst_dir, exist_ok=True)
            except Exception:
                pass
            bot.send_message(
                message.chat.id,
                f"\u2705 <b>\u09ac\u099f \u0995\u09cd\u09b2\u09cb\u09a8 \u09b8\u09ab\u09b2 \u09b9\u09df\u09c7\u099b\u09c7!</b>\n"
                f"\U0001F195 \u09a8\u09a4\u09c1\u09a8 \u09ac\u099f: <code>{html.escape(new_name)}</code> (Slot {slot})\n"
                "\U0001F4C1 \u09b8\u09ac \u09ab\u09be\u0987\u09b2 \u0993 env variable \u0995\u09aa\u09bf \u09b9\u09df\u09c7\u099b\u09c7\u0964 \U0001F3ae Control Panel \u09a5\u09c7\u0995\u09c7 \u0928\u09a4\u09c1\u09a8 \u09b8\u09cd\u09b2\u099f Start \u0995\u09b0\u09c1\u09a8\u0964",
                parse_mode="HTML"
            )
            return

        # ======================== Scheduled Start/Stop \u2014 \u09b8\u09ae\u09df \u0987\u09a8\u09aa\u09c1\u099f ========================
        elif state_name in ("awaiting_sched_start", "awaiting_sched_stop"):
            pid = state_data.get("pid")
            del user_states[user_id]
            import re as _re2
            time_input = text.strip()
            if not _re2.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", time_input):
                bot.send_message(message.chat.id, "\u274c \u09ad\u09c1\u09b2 \u09ab\u09b0\u09ae\u09cd\u09af\u09be\u099f\u0964 HH:MM \u0986\u0995\u09be\u09b0\u09c7 \u09a6\u09bf\u09a8, \u09af\u09c7\u09ae\u09a8 <code>22:00</code>", parse_mode="HTML")
                return
            h, m = time_input.split(":")
            normalized = f"{int(h):02d}:{int(m):02d}"
            project = admin.get_project_by_id(pid)
            cur_start = project.get("sched_start_time") if project else None
            cur_stop = project.get("sched_stop_time") if project else None
            if state_name == "awaiting_sched_start":
                admin.set_bot_schedule(pid, normalized, cur_stop, enabled=True)
                label = "Start"
            else:
                admin.set_bot_schedule(pid, cur_start, normalized, enabled=True)
                label = "Stop"
            bot.send_message(
                message.chat.id,
                f"\u2705 <b>Scheduled {label} Time \u09b8\u09c7\u099f \u09b9\u09df\u09c7\u099b\u09c7!</b>\n\u23f0 \u09aa\u09cd\u09b0\u09a4\u09bf\u09a6\u09bf\u09a8 <b>{normalized}</b> (Bangladesh Time)-\u098f \u098f\u0987 \u0985\u09cd\u09af\u09be\u0995\u09b6\u09a8 \u09b9\u09ac\u09c7\u0964",
                parse_mode="HTML"
            )
            return

        # ======================== Auto-Broadcast \u09b6\u09bf\u09a1\u09bf\u0989\u09b2 \u2014 \u09ae\u09c7\u09b8\u09c7\u099c \u09a7\u09be\u09aa \u0993 \u09b8\u09ae\u09df ========================
        elif state_name == "awaiting_sched_broadcast_msg" and admin.is_admin(user_id):
            user_states[user_id] = {"state": "awaiting_sched_broadcast_time", "msg": text}
            bot.send_message(
                message.chat.id,
                "\u23f0 <b>\u0995\u0996\u09a8 \u09aa\u09be\u09a0\u09be\u09a4\u09c7 \u099a\u09be\u09a8?</b>\n\u098f\u0987 \u09ab\u09b0\u09ae\u09cd\u09af\u09be\u099f\u09c7 \u09a6\u09bf\u09a8: <code>YYYY-MM-DD HH:MM</code>\n\u0989\u09a6\u09be\u09b9\u09b0\u09a3: <code>2026-09-20 09:00</code>",
                parse_mode="HTML"
            )
            return

        elif state_name == "awaiting_sched_broadcast_time" and admin.is_admin(user_id):
            msg_text = state_data.get("msg", "")
            del user_states[user_id]
            send_at = text.strip()
            try:
                datetime.strptime(send_at, "%Y-%m-%d %H:%M")
            except Exception:
                bot.send_message(message.chat.id, "\u274c \u09ad\u09c1\u09b2 \u09ab\u09b0\u09ae\u09cd\u09af\u09be\u099f\u0964 \u0989\u09a6\u09be\u09b9\u09b0\u09a3: <code>2026-09-20 09:00</code>", parse_mode="HTML")
                return
            admin.create_scheduled_broadcast(msg_text, send_at, user_id)
            bot.send_message(
                message.chat.id,
                f"✅ <b>Broadcast শিডিউল হয়েছে!</b>\n⏰ পাঠানো হবে: <code>{send_at}</code>\n📝 মেসেজ: {html.escape(msg_text[:200])}",
                parse_mode="HTML"
            )
            return

        # 💼 Dev Studio — ইউজারের কাস্টম বট রিকোয়ারমেন্ট → সাপোর্ট টিকিট + অ্যাডমিন অ্যালার্ট
        elif state_name == "awaiting_devstudio_request":
            body = (text or "").strip()
            if len(body) < 15:
                bot.send_message(
                    message.chat.id,
                    "⚠️ <b>রিকোয়ারমেন্ট খুব ছোট!</b> বটের ধরন, ফিচার, বাজেট ও ডেডলাইন সহ একটু বিস্তারিত লিখে আবার পাঠান।",
                    parse_mode="HTML"
                )
                return
            del user_states[user_id]
            ticket_id = admin.create_support_ticket(user_id, message.from_user.username, "💼 [Dev Studio Request]\n" + body[:3500])
            ok_markup = types.InlineKeyboardMarkup(row_width=1)
            ok_markup.add(
                make_inline_button("🎫 My Tickets", callback_data="my_tickets_page_0", style="primary"),
                make_inline_button("🏠 Home Menu", callback_data="go_home", style="success"),
            )
            bot.send_message(
                message.chat.id,
                f"✅ <b>রিকোয়ারমেন্ট জমা হয়েছে!</b> (টিকিট #{ticket_id})\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "💼 আমাদের ডেভেলপার টিম রিভিউ করে শীঘ্রই আপনাকে রিপ্লাই দেবে।\n"
                "রিপ্লাই ও স্ট্যাটাস দেখতে <b>🎫 My Tickets</b> চেক করুন।",
                reply_markup=ok_markup, parse_mode="HTML"
            )
            admin_alert = (
                f"💼 <b>নতুন Dev Studio রিকোয়ারমেন্ট!</b> (#{ticket_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>ইউজার:</b> @{html.escape(message.from_user.username or 'Anonymous')} (ID: <code>{user_id}</code>)\n"
                f"📝 <b>রিকোয়ারমেন্ট:</b> {html.escape(body[:600])}"
            )
            admin_markup = types.InlineKeyboardMarkup()
            admin_markup.add(make_inline_button("💬 View / Reply", callback_data=f"adm_ticket_view_{ticket_id}", style="primary"))
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        # 🎁 ইউজার রিডিম কোড লিখেছে
        elif state_name == "awaiting_redeem_input":
            del user_states[user_id]
            process_user_redeem(message.chat.id, user_id, message.from_user.username, text or "")
            return

        # 🌐 কমিউনিটি: ইউজার বটের নাম লিখছে
        elif state_name == "awaiting_cm_name":
            name = (text or "").strip()
            if not (3 <= len(name) <= 40) or "\n" in name:
                bot.send_message(message.chat.id, "❌ নাম ৩–৪০ অক্ষরের এক লাইনে হতে হবে। আবার লিখুন।")
                return
            user_states[user_id] = {"state": "awaiting_cm_desc", "name": name}
            bot.send_message(message.chat.id, "✍️ <b>ধাপ ২/৩ — বিবরণ</b>\nবটটি কী করে, সংক্ষেপে লিখুন (১০–৩০০ অক্ষর)।", parse_mode="HTML")
            return

        # 🌐 কমিউনিটি: বিবরণ
        elif state_name == "awaiting_cm_desc":
            desc = (text or "").strip()
            if not (10 <= len(desc) <= 300):
                bot.send_message(message.chat.id, "❌ বিবরণ ১০–৩০০ অক্ষরের মধ্যে হতে হবে। আবার লিখুন।")
                return
            user_states[user_id] = {"state": "awaiting_cm_code", "name": state_data["name"], "desc": desc}
            bot.send_message(
                message.chat.id,
                "✍️ <b>ধাপ ৩/৩ — কোড</b>\n<code>main.py</code> ফাইলটি <b>.py ডকুমেন্ট হিসেবে আপলোড</b> করুন অথবা কোড এই চ্যাটে পেস্ট করুন।\n"
                "মনে রাখবেন: টোকেনের জায়গায় <code>'__TOKEN__'</code> থাকতে হবে।",
                parse_mode="HTML"
            )
            return

        # 🌐 কমিউনিটি: কোড পেস্ট করেছে
        elif state_name == "awaiting_cm_code":
            if submit_community_code(message.chat.id, message.from_user, state_data, text or ""):
                user_states.pop(user_id, None)
            return

        # 🌐 অ্যাডমিন: রিজেক্ট/রিমুভের কারণ
        elif state_name == "awaiting_cm_reject_reason" and admin.is_admin(user_id):
            reason = (text or "").strip()
            reason = "" if reason == "-" else reason[:200]
            tid = state_data.get("tid")
            del user_states[user_id]
            t = admin.review_community_template(tid, False, reason)
            if not t:
                bot.send_message(message.chat.id, "❌ টেমপ্লেট পাওয়া যায়নি।")
                return
            bot.send_message(message.chat.id, f"✅ Template #{tid} রিজেক্ট/রিমুভ করা হয়েছে।")
            try:
                bot.send_message(
                    t["creator_id"],
                    f"❌ <b>আপনার কমিউনিটি বট লাইভ নেই</b>\n🧩 {html.escape(t['name'])}"
                    + (f"\n📝 কারণ: {html.escape(reason)}" if reason else "")
                    + "\n\nঠিক করে আবার সাবমিট করতে পারেন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # ✅ মার্কেটপ্লেস: টেমপ্লেটের এক্সট্রা ইনপুট (যেমন AI API Key)
        elif state_name == "awaiting_deploy_extra":
            template_type = state_data["template"]
            tpl_def = get_marketplace_template(template_type) or {}
            inputs = tpl_def.get("inputs") or []
            idx = state_data.get("idx", 0)
            if idx >= len(inputs):
                del user_states[user_id]
                return
            value = (text or "").strip()
            try:
                bot.delete_message(message.chat.id, message.message_id)   # সিক্রেট কী চ্যাটে ফেলে রাখা হয় না
            except Exception:
                pass
            spec = inputs[idx]
            if not re.match(spec["pattern"], value):
                bot.send_message(message.chat.id, spec.get("error", "❌ ভুল ফরম্যাট! আবার পাঠান।"), parse_mode="HTML")
                return
            state_data["extras"][spec["placeholder"]] = value
            if idx + 1 < len(inputs):
                state_data["idx"] = idx + 1
                bot.send_message(message.chat.id, inputs[idx + 1]["prompt"], parse_mode="HTML", disable_web_page_preview=True)
                return
            del user_states[user_id]
            finish_marketplace_deploy(message, user_id, template_type, state_data["token"], state_data["extras"])
            return

        # 🔓 অ্যাডমিন: VIP Unlock কাস্টম মেয়াদ (দিন/ঘণ্টা) লিখেছে
        elif state_name == "awaiting_vip_unlock_custom" and admin.is_admin(user_id):
            raw = (text or "").strip().lower()
            mt = re.fullmatch(r"(\d{1,4})\s*([dh]?)", raw)
            hours = None
            if mt:
                n, unit = int(mt.group(1)), (mt.group(2) or "d")
                hours = n * 24 if unit == "d" else n
            if not hours or hours > 365 * 24:
                bot.send_message(message.chat.id, "❌ সঠিক মেয়াদ লিখুন (১–৩৬৫ দিন)। উদাহরণ: <code>10</code> অথবা <code>36h</code>", parse_mode="HTML")
                return
            del user_states[user_id]
            until = admin.set_vip_unlock(hours)
            label = f"{hours // 24} দিন" if hours % 24 == 0 else f"{hours} ঘণ্টা"
            bot.send_message(
                message.chat.id,
                "🔓 <b>VIP All Slot Unlock চালু হয়েছে!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱️ মেয়াদ: <b>{label}</b>\n"
                f"🕒 শেষ হবে: <code>{until.strftime('%d %b %Y • %H:%M')}</code>\n"
                f"প্রতি ইউজারের <b>{admin.get_vip_unlock_slots()}টি স্লট</b> (Slot 1–{admin.get_vip_unlock_slots()}) এখন আনলক ও 24/7।",
                parse_mode="HTML"
            )
            return

        # 🎁 অ্যাডমিন: কাস্টম মেয়াদ (দিন) লিখেছে
        elif state_name == "awaiting_redeem_days" and admin.is_admin(user_id):
            tier_key = state_data.get("tier")
            raw = (text or "").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= 3650):
                bot.send_message(message.chat.id, "❌ ১ থেকে ৩৬৫০-এর মধ্যে একটি সংখ্যা লিখুন। উদাহরণ: <code>45</code>", parse_mode="HTML")
                return
            del user_states[user_id]
            q_text, q_markup = build_redeem_qty_picker(tier_key, int(raw))
            bot.send_message(message.chat.id, q_text, reply_markup=q_markup, parse_mode="HTML")
            return

        # 🎁 অ্যাডমিন: কাস্টম কোড (CODE MAX_USES)
        elif state_name == "awaiting_redeem_custom" and admin.is_admin(user_id):
            parts = (text or "").strip().split()
            if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট! এভাবে দিন: <code>CODE MAX_USES</code>\nউদাহরণ: <code>EID2026 50</code>", parse_mode="HTML")
                return
            max_uses = int(parts[1]) if len(parts) == 2 else 1
            tier_key, days = state_data.get("tier"), state_data.get("days")
            ok, res = admin.create_redeem_codes(tier_key, days, 1, max_uses, user_id, custom_code=parts[0])
            if not ok:
                bot.send_message(message.chat.id, f"❌ {html.escape(str(res))}\nআবার চেষ্টা করুন বা অন্য কোড দিন।", parse_mode="HTML")
                return
            del user_states[user_id]
            messages, r_markup = build_redeem_codes_result(tier_key, days, res, max_uses)
            for i, m in enumerate(messages):
                bot.send_message(message.chat.id, m, reply_markup=r_markup if i == len(messages) - 1 else None, parse_mode="HTML")
            return

        # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — ইউজার নতুন টিকিট খুলছে
        elif state_name == "awaiting_ticket_message":
            del user_states[user_id]
            msg_body = text.strip()
            ticket_id = admin.create_support_ticket(user_id, message.from_user.username, msg_body)
            bot.send_message(
                message.chat.id,
                f"✅ <b>টিকিট #{ticket_id} তৈরি হয়েছে!</b>\nআমাদের টিম শীঘ্রই আপনাকে রিপ্লাই দেবে। "
                "আপডেট পেতে <b>🎫 My Tickets</b> চেক করুন।",
                parse_mode="HTML"
            )
            admin_alert = (
                f"🎫 <b>নতুন সাপোর্ট টিকিট!</b> (#{ticket_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>ইউজার:</b> @{message.from_user.username or 'Anonymous'} (ID: <code>{user_id}</code>)\n"
                f"📝 <b>মেসেজ:</b> {html.escape(msg_body[:300])}"
            )
            admin_markup = types.InlineKeyboardMarkup()
            admin_markup.add(make_inline_button("💬 Reply", callback_data=f"adm_ticket_view_{ticket_id}", style="primary"))
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        # ⭐ NEW: ইউজার একটা ওপেন টিকিটে রিপ্লাই দিচ্ছে
        elif state_name == "awaiting_ticket_reply":
            del user_states[user_id]
            ticket_id = state_data["ticket_id"]
            ticket = admin.get_ticket(ticket_id)
            if not ticket or ticket["user_id"] != user_id:
                bot.send_message(message.chat.id, "❌ টিকিটটি পাওয়া যায়নি।")
                return
            admin.add_ticket_message(ticket_id, "user", text.strip())
            thread_text, thread_markup = build_ticket_thread_view(ticket_id, is_admin=False)
            bot.send_message(message.chat.id, thread_text, reply_markup=thread_markup, parse_mode="HTML")
            for aid in admin.ADMIN_IDS:
                try:
                    _mk = types.InlineKeyboardMarkup()
                    _mk.add(make_inline_button("💬 View / Reply", callback_data=f"adm_ticket_view_{ticket_id}", style="primary"))
                    bot.send_message(
                        aid,
                        f"🔔 <b>টিকিট #{ticket_id}-এ ইউজার রিপ্লাই দিয়েছে!</b>\n{html.escape(text.strip()[:300])}",
                        reply_markup=_mk,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            return

        # ⭐ NEW: অ্যাডমিন একটা টিকিটে রিপ্লাই দিচ্ছেন
        elif state_name == "awaiting_admin_ticket_reply" and admin.is_admin(user_id):
            del user_states[user_id]
            ticket_id = state_data["ticket_id"]
            ticket = admin.get_ticket(ticket_id)
            if not ticket:
                bot.send_message(message.chat.id, "❌ টিকিটটি পাওয়া যায়নি।")
                return
            admin.add_ticket_message(ticket_id, "admin", text.strip())
            thread_text, thread_markup = build_ticket_thread_view(ticket_id, is_admin=True)
            bot.send_message(message.chat.id, thread_text, reply_markup=thread_markup, parse_mode="HTML")
            try:
                _mk2 = types.InlineKeyboardMarkup()
                _mk2.add(make_inline_button("👁 টিকিট দেখুন", callback_data=f"ticket_view_{ticket_id}", style="primary"))
                bot.send_message(
                    ticket["user_id"],
                    f"💬 <b>সাপোর্ট থেকে রিপ্লাই এসেছে!</b> (টিকিট #{ticket_id})\n{html.escape(text.strip()[:500])}",
                    reply_markup=_mk2,
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

    # --- অ্যাডমিন বাটন অ্যাকশনস ---
    if text == "👑 Admin Panel" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        bot.send_message(message.chat.id, stats, reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")

    elif text == "🔙 User Menu":
        bot.send_message(message.chat.id, "🔙 <b>ইউজার মেনুতে ফিরে আসা হয়েছে:</b>", reply_markup=get_user_keyboard(user_id), parse_mode="HTML")

    # ======================== অ্যাডমিন সাব-মেনু নেভিগেশন ========================
    elif text == "📊 Monitoring" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "📊 <b>মনিটরিং মেনু</b>\nসার্ভার ও বট স্ট্যাটাস দেখুন:", reply_markup=get_admin_monitoring_keyboard(), parse_mode="HTML")

    elif text == "👥 User Manager" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "👥 <b>ইউজার ম্যানেজমেন্ট</b>\nইউজার সংক্রান্ত সব কার্যক্রম:", reply_markup=get_admin_user_keyboard(), parse_mode="HTML")

    elif text == "💰 Billing & Plans" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "💰 <b>বিলিং ও প্ল্যান</b>\nVIP প্রদান, কুপন ও রিডিম কোড ম্যানেজ:", reply_markup=get_admin_billing_keyboard(), parse_mode="HTML")

    elif text == "⚙️ System" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "⚙️ <b>সিস্টেম</b>\nসার্ভার ম্যানেজমেন্ট:", reply_markup=get_admin_system_keyboard(), parse_mode="HTML")

    elif text == "📈 Analytics" and admin.is_admin(user_id):
        # Analytics Dashboard directly
        all_users = admin.get_all_users()
        total = len(all_users)
        free_c = sum(1 for u in all_users if u.get("plan","FREE")=="FREE")
        pro_c = sum(1 for u in all_users if u.get("plan")=="PRO")
        prem_c = sum(1 for u in all_users if u.get("plan")=="PREMIUM")
        biz_c = sum(1 for u in all_users if u.get("plan") in ("BUSINESS","VIP"))
        paid_total = pro_c+prem_c+biz_c
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(1 for u in all_users if str(u.get("created_at","")).startswith(today_str))
        active_bot_count = len([k for k,v in active_processes.items() if v["process"].poll() is None])
        conn = admin.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM projects"); total_bots = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vip_requests WHERE status='APPROVED'"); approved_vip = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupon_uses"); coupon_uses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupons WHERE is_active=1"); active_coupons = cur.fetchone()[0]
        conn.close()
        conv = f"{(paid_total/total*100):.1f}%" if total>0 else "0%"
        dash = (
            "📈 <b>Analytics Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 মোট ইউজার: <b>{total}</b> | আজ নতুন: <b>{today_new}</b>\n"
            f"💰 পেইড: <b>{paid_total}</b> | ফ্রি: <b>{free_c}</b> | Conversion: <b>{conv}</b>\n"
            f"⚡ Pro: <b>{pro_c}</b> | 🌟 Premium: <b>{prem_c}</b> | 👑 Business: <b>{biz_c}</b>\n"
            f"🤖 বট চলছে: <b>{active_bot_count}</b> | মোট: <b>{total_bots}</b>\n"
            f"✅ VIP Approved: <b>{approved_vip}</b>\n"
            f"🎟️ কুপন একটিভ: <b>{active_coupons}</b> | ব্যবহার: <b>{coupon_uses}</b>\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="adm_refresh_analytics", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(message.chat.id, dash, reply_markup=markup, parse_mode="HTML")

    elif text == "⏹ Stop All Bots" and admin.is_admin(user_id):
        stopped = 0
        for p_key in list(active_processes.keys()):
            try:
                uid, pid = map(int, p_key.split("_", 1))
                active_processes[p_key]["process"].terminate()
                active_processes[p_key]["log_handle"].close()
                del active_processes[p_key]
                admin.update_project_status(pid, "STOPPED")
                stopped += 1
            except Exception:
                pass
        bot.send_message(message.chat.id, f"⏹️ <b>{stopped}টি বট বন্ধ করা হয়েছে।</b>", parse_mode="HTML")

    elif text == "📤 Bot Export" and admin.is_admin(user_id):
        # সব ইউজারের বট সামারি CSV হিসেবে দেওয়া
        all_users = admin.get_all_users()
        lines_csv = ["User ID,Username,Plan,Total Bots,Active Bots"]
        for u in all_users:
            uid = u["user_id"]
            uname = u.get("username","")
            plan = u.get("plan","FREE")
            projs = admin.get_user_projects(uid)
            active_c = sum(1 for p in projs if f"{uid}_{p['project_id']}" in active_processes and active_processes[f"{uid}_{p['project_id']}"]['process'].poll() is None)
            lines_csv.append(f"{uid},{uname},{plan},{len(projs)},{active_c}")
        csv_data = "\n".join(lines_csv).encode("utf-8")
        import io as _io
        bio = _io.BytesIO(csv_data)
        bio.name = f"pyhost_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        bot.send_document(message.chat.id, bio, caption="📤 <b>All Users Bot Export</b>", parse_mode="HTML")

    elif text == "🔙 Admin Menu" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        bot.send_message(message.chat.id, stats, reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")

    elif text == "📊 Live Telemetry" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes, footer=False)
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh Telemetry", callback_data="adm_refresh_telem", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(message.chat.id, stats, reply_markup=markup, parse_mode="HTML")

    elif text == "🤖 Process Tracker" and admin.is_admin(user_id):
        if not active_processes:
            bot.send_message(message.chat.id, "🤖 No active bot processes running.")
        else:
            lines = []
            for p_key, d in active_processes.items():
                elapsed = int(time.time() - d["start_time"])
                mem, cpu = admin.get_bot_resource_usage(d["process"].pid)
                lines.append(f"• <b>Bot:</b> <code>{d['project_name']}</code> (UID: <code>{d['user_id']}</code>)\n  └ 🧠 {mem} MB | 💻 {cpu}% | ⏱️ {elapsed//3600}h {(elapsed%3600)//60}m")
            bot.send_message(message.chat.id, "🤖 <b>Active Bots Process Directory:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines), parse_mode="HTML")

    elif text == "📋 Pending Requests" and admin.is_admin(user_id):
        req_text, req_markup = build_pending_requests_view(page=0)
        bot.send_message(message.chat.id, req_text, reply_markup=req_markup, parse_mode="HTML")

    elif text == "🎫 Support Tickets" and admin.is_admin(user_id):
        t_text, t_markup = build_admin_tickets_view(page=0)
        bot.send_message(message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")

    elif text == "🔄 Restart All Bots" and admin.is_admin(user_id):
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Yes, Restart", callback_data="adm_confirm_restart_all", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>নিশ্চিত করুন!</b>\nবর্তমানে সক্রিয় <code>{len(active_processes)}</code> টি বট রিস্টার্ট হবে। চালিয়ে যেতে চান?",
            reply_markup=markup, parse_mode="HTML"
        )

    elif text == "👥 User Directory" and admin.is_admin(user_id):
        dir_text, dir_markup = build_user_directory_view(page=0)
        bot.send_message(message.chat.id, dir_text, reply_markup=dir_markup, parse_mode="HTML")

    elif text == "👤 User Management" and admin.is_admin(user_id):
        dir_text, dir_markup = build_user_directory_view(page=0)
        intro = (
            "👤 <b>USER MANAGEMENT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "নিচের লিস্ট থেকে ইউজার বেছে নিন — প্রোফাইলে ট্যাপ করলে প্ল্যান পরিবর্তন, "
            "ব্যান/আনব্যান, বট ম্যানেজমেন্ট ও অ্যাক্টিভিটি লগসহ সব কন্ট্রোল এক জায়গায় পাবেন।\n\n"
        )
        bot.send_message(message.chat.id, intro + dir_text, reply_markup=dir_markup, parse_mode="HTML")

    elif text == "📢 Global Broadcast" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_broadcast"
        bot.send_message(message.chat.id, "📢 <b>Broadcast Notice:</b> Type the announcement message to broadcast to all clients:")

    elif text == "🗓️ Schedule Broadcast" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_sched_broadcast_msg"
        bot.send_message(message.chat.id, "🗓️ <b>Schedule Broadcast:</b> যে মেসেজটি নির্দিষ্ট সময়ে সব ইউজারকে পাঠাতে চান সেটা লিখুন:", parse_mode="HTML")

    elif text == "💎 Grant VIP (Manual)" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_vip_id"
        bot.send_message(message.chat.id, "💎 <b>Grant VIP:</b> Enter the Telegram User ID to upgrade:")

    elif text == "🚫 Ban / Security" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_ban_id"
        bot.send_message(message.chat.id, "🚫 <b>Ban/Unban:</b> Enter the Telegram User ID to toggle ban status:")

    elif text == "💾 Database Backup" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "⏳ Generating system & database backup...")
        b_path = admin.generate_system_backup()
        with open(b_path, "rb") as bf:
            bot.send_document(message.chat.id, bf, caption="💾 <b>PyHost Full Database Backup</b>")

    elif text == "🧹 Server Cleanup" and admin.is_admin(user_id):
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Yes, Clean Up", callback_data="adm_confirm_cleanup", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        bot.send_message(message.chat.id, "⚠️ <b>নিশ্চিত করুন!</b>\nপুরনো লগ ও ক্যাশ পরিষ্কার করা হবে। চালিয়ে যেতে চান?", reply_markup=markup, parse_mode="HTML")

    elif text == "💾 Data Backup" and admin.is_admin(user_id):
        g_text, g_markup = build_gh_backup_panel()
        bot.send_message(message.chat.id, g_text, reply_markup=g_markup, parse_mode="HTML")

    elif text == "🧹 Channel Clean" and admin.is_admin(user_id):
        c_text, c_markup = build_channel_clean_panel()
        bot.send_message(message.chat.id, c_text, reply_markup=c_markup, parse_mode="HTML")

    elif text == "🌐 Community Review" and admin.is_admin(user_id):
        c_text, c_markup = build_admin_community_panel()
        bot.send_message(message.chat.id, c_text, reply_markup=c_markup, parse_mode="HTML")

    elif text == "🔓 VIP All Slot Unlock" and admin.is_admin(user_id):
        panel_text, panel_markup = build_vip_unlock_panel()
        bot.send_message(message.chat.id, panel_text, reply_markup=panel_markup, parse_mode="HTML")

    elif text == "🆓 Free Hosting (12h)" and admin.is_admin(user_id):
        will_be = not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True)
        current_hours = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Confirm" if will_be else "🔒 Disable (Confirm)", callback_data="adm_confirm_free_hosting", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        markup.add(
            make_inline_button("3 Hours" + (" ✓" if current_hours == 3 else ""), callback_data="adm_set_free_hours_3", style="primary"),
            make_inline_button("6 Hours" + (" ✓" if current_hours == 6 else ""), callback_data="adm_set_free_hours_6", style="primary"),
        )
        markup.add(
            make_inline_button("9 Hours" + (" ✓" if current_hours == 9 else ""), callback_data="adm_set_free_hours_9", style="primary"),
            make_inline_button("12 Hours" + (" ✓" if current_hours == 12 else ""), callback_data="adm_set_free_hours_12", style="primary"),
        )
        target_label = "🟢 ON (ফ্রি ইউজাররা আগের মতোই বট চালাতে পারবে)" if will_be else "🔴 OFF (সব ফ্রি স্লট লক হয়ে যাবে, এখন চলতে থাকা ফ্রি বটও বন্ধ হয়ে যাবে)"
        current_label = "🟢 বর্তমানে ON আছে" if admin.SYSTEM_CONFIG.get("free_hosting_enabled", True) else "🔴 বর্তমানে OFF আছে"
        bot.send_message(
            message.chat.id,
            f"🆓 <b>ফ্রি হোস্টিং সিস্টেম</b>\n{current_label} • সেশন মেয়াদ: <b>{current_hours} Hours</b>\n\n"
            f"পরিবর্তন করে <b>{target_label}</b> করা হবে।\n"
            "💎 পেইড প্ল্যান (24/7) কোনোভাবেই প্রভাবিত হবে না — শুধু FREE প্ল্যান আটকাবে।\n\n"
            "⏱️ নিচ থেকে ফ্রি সেশনের মেয়াদ (ঘণ্টা) বদলাতে পারেন — এখনই এফেক্ট হবে, Control Panel-এও সাথে সাথে আপডেট হয়ে দেখাবে।",
            reply_markup=markup, parse_mode="HTML"
        )

    elif text == "⚙️ Maintenance Mode" and admin.is_admin(user_id):
        will_be = not admin.SYSTEM_CONFIG["maintenance_mode"]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Confirm", callback_data="adm_confirm_maintenance", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        target_label = "🔴 ON (সব নতুন ডিপ্লয়মেন্ট বন্ধ থাকবে)" if will_be else "🟢 OFF"
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>নিশ্চিত করুন!</b>\nMaintenance Mode <b>{target_label}</b> করা হবে — এটি সব ইউজারকে প্রভাবিত করবে। চালিয়ে যেতে চান?",
            reply_markup=markup, parse_mode="HTML"
        )


    elif text == "📈 Analytics Dashboard" and admin.is_admin(user_id):
        all_users = admin.get_all_users()
        total = len(all_users)
        free_c = sum(1 for u in all_users if u.get("plan", "FREE") == "FREE")
        pro_c = sum(1 for u in all_users if u.get("plan") == "PRO")
        prem_c = sum(1 for u in all_users if u.get("plan") == "PREMIUM")
        biz_c = sum(1 for u in all_users if u.get("plan") in ("BUSINESS", "VIP"))
        paid_total = pro_c + prem_c + biz_c
        banned_c = sum(1 for u in all_users if u.get("is_banned") == 1)
        # আজকের নতুন ইউজার
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(1 for u in all_users if str(u.get("created_at", "")).startswith(today_str))
        # Active bots
        active_bot_count = len([k for k, v in active_processes.items() if v["process"].poll() is None])
        # Total bots in DB
        import sqlite3
        conn = admin.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM projects")
        total_bots = cur.fetchone()[0]
        # Revenue estimate (approved requests)
        cur.execute("SELECT COUNT(*) FROM vip_requests WHERE status = 'APPROVED'")
        approved_vip = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM renewal_requests WHERE status = 'APPROVED'")
        approved_ext = cur.fetchone()[0]
        # Coupon usage
        cur.execute("SELECT COUNT(*) FROM coupon_uses")
        total_coupon_uses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupons WHERE is_active = 1")
        active_coupons = cur.fetchone()[0]
        conn.close()

        # Conversion rate
        conv_rate = f"{(paid_total/total*100):.1f}%" if total > 0 else "0%"

        dashboard = (
            "📈 <b>PyHost Analytics Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>ইউজার সামারি:</b>\n"
            f"├ 📊 মোট রেজিস্টার্ড: <b>{total}</b> জন\n"
            f"├ 🆕 আজকের নতুন: <b>{today_new}</b> জন\n"
            f"├ 💰 পেইড সদস্য: <b>{paid_total}</b> জন\n"
            f"├ 🆓 ফ্রি ইউজার: <b>{free_c}</b> জন\n"
            f"└ 🚫 ব্যান্ড: <b>{banned_c}</b> জন\n\n"
            "📦 <b>প্ল্যান বিভাজন:</b>\n"
            f"├ ⚡ Pro: <b>{pro_c}</b> জন\n"
            f"├ 🌟 Premium: <b>{prem_c}</b> জন\n"
            f"├ 👑 Business/VIP: <b>{biz_c}</b> জন\n"
            f"└ 📈 Conversion Rate: <b>{conv_rate}</b>\n\n"
            "🤖 <b>বট স্ট্যাটাস:</b>\n"
            f"├ 🟢 এখন চলছে: <b>{active_bot_count}</b> টি\n"
            f"└ 📦 মোট তৈরি: <b>{total_bots}</b> টি\n\n"
            "💰 <b>পেমেন্ট সামারি:</b>\n"
            f"├ ✅ অ্যাপ্রুভড প্ল্যান রিকোয়েস্ট: <b>{approved_vip}</b>\n"
            f"└ ✅ অ্যাপ্রুভড রিনিউয়াল: <b>{approved_ext}</b>\n\n"
            "🎟️ <b>কুপন সামারি:</b>\n"
            f"├ 🟢 একটিভ কুপন: <b>{active_coupons}</b> টি\n"
            f"└ 📊 মোট ব্যবহার: <b>{total_coupon_uses}</b> বার\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <i>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="adm_refresh_analytics", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(message.chat.id, dashboard, reply_markup=markup, parse_mode="HTML")

    elif text == "🎟️ Coupon Manager" and admin.is_admin(user_id):
        coupons = admin.list_coupons()
        if coupons:
            lines_text = []
            for c in coupons[:15]:
                status = "🟢" if c["is_active"] else "🔴"
                uses = f"{c['used_count']}/{c['max_uses']}" if c["max_uses"] > 0 else f"{c['used_count']}/∞"
                lines_text.append(f"{status} <code>{c['code']}</code> — {c['discount_percent']}% ছাড় | ব্যবহার: {uses}")
            coupon_list = "\n".join(lines_text)
        else:
            coupon_list = "<i>এখনো কোনো কুপন নেই।</i>"

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("➕ Create New Coupon", callback_data="adm_coupon_create", style="success"),
            make_inline_button("❌ Deactivate Coupon", callback_data="adm_coupon_deactivate", style="danger")
        )
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(
            message.chat.id,
            f"🎟️ <b>Coupon Manager</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{coupon_list}\n━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=markup, parse_mode="HTML"
        )

    # 🎁 অ্যাডমিন: Redeem Code ম্যানেজার
    elif text == "🎁 Redeem Codes" and admin.is_admin(user_id):
        rc_text, rc_markup = build_redeem_dashboard()
        bot.send_message(message.chat.id, rc_text, reply_markup=rc_markup, parse_mode="HTML")

    # --- রেগুলার ইউজার বাটনস ---
    elif text == "🎁 Redeem Code":
        send_redeem_prompt(message.chat.id, user_id)

    elif text == "🎛️ Control Panel":
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.send_message(message.chat.id, cp_text, reply_markup=cp_markup, parse_mode="HTML")

    elif text == "📁 File Manager":
        projects = admin.get_user_projects(user_id)
        if not projects:
            markup = types.InlineKeyboardMarkup()
            markup.add(make_inline_button("➕ Create Bot & Upload File", callback_data="add_new_bot", style="success"))
            markup.add(make_inline_button("🔙 Back to Home", callback_data="go_home", style="primary"))
            bot.send_message(
                message.chat.id,
                "⚠️ <b>আপনার কোনো সক্রিয় বট প্রজেক্ট পাওয়া যায়নি!</b>\n\nফাইল আপলোড ও হোস্ট করতে নিচের বাটনে চাপ দিন:",
                reply_markup=markup,
                parse_mode="HTML"
            )
        else:
            markup = types.InlineKeyboardMarkup(row_width=1)
            for p in projects:
                markup.add(make_inline_button(f"📁 {p['project_name']}", callback_data=f"fm_view_{p['project_id']}", style="primary"))
            markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
            bot.send_message(
                message.chat.id,
                "📂 <b>স্মার্ট ফাইল ম্যানেজার</b>\n━━━━━━━━━━━━━━━━━━━━━━\nযে বটের ফাইল দেখতে, ব্যাকআপ নিতে বা আপডেট করতে চান তা নিচে নির্বাচন করুন:",
                reply_markup=markup,
                parse_mode="HTML"
            )

    elif text == "👤 My Account":
        card_text, markup = get_my_account_card(user_id, message.from_user)
        bot.send_message(message.chat.id, card_text, reply_markup=markup, parse_mode="HTML")

    elif text == "💎 Upgrade Plan":
        plan_text, plan_markup = get_upgrade_plan_view(user_id)
        bot.send_message(message.chat.id, plan_text, reply_markup=plan_markup, parse_mode="HTML")

    elif text == "🐙 GitHub Deploy":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("🐙 GitHub Deploy", callback_data="gh_deploy_start", style="success"),
            make_inline_button("🤖 Marketplace", callback_data="open_mp_menu", style="primary")
        )
        add_home_button(markup)
        bot.send_message(
            message.chat.id,
            "🐙 <b>GitHub Deploy</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "GitHub রিপো থেকে সরাসরি বট ডিপ্লয় করতে নিচের <b>🐙 GitHub Deploy</b> বাটনে চাপুন (VIP প্ল্যান প্রয়োজন)।\n"
            "কোডিং জানা না থাকলে <b>🤖 Marketplace</b> থেকে রেডিমেড বট বেছে নিন।",
            reply_markup=markup, parse_mode="HTML"
        )

    elif text == "🤖 Marketplace":
        bot.send_message(message.chat.id, MARKETPLACE_INTRO, reply_markup=build_marketplace_markup(), parse_mode="HTML")

    elif text == "📡 Support Hub":
        msg = (
            "🛠️ <b>Support Centre</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "যেকোনো সহায়তার জন্য বা কাস্টম বট বানানোর জন্য নিচের অপশনগুলো বেছে নিন:"
        )
        support_markup = get_support_hub_markup()
        add_home_button(support_markup)
        bot.send_message(message.chat.id, msg, reply_markup=support_markup, parse_mode="HTML")

    elif text == "📖 Documentation":
        _doc_text0, _doc_markup0 = build_doc_page(0)
        bot.send_message(message.chat.id, _doc_text0, reply_markup=_doc_markup0, parse_mode="HTML")

# ----------------- ফাইল আপলোড ও লাইভ ডেপ্লয়মেন্ট হ্যান্ডলার -----------------
# ⭐ FIX: ফাইল আপলোডের সময় একটার পর একটা এলোমেলো এক-লাইনের টেক্সট দেখানোর বদলে ধাপে-ধাপে
# চেকলিস্ট দেখানো হবে (সম্পন্ন/চলমান/বাকি ধাপ) — একই মেসেজ এডিট হয়ে আপডেট হবে, বট সফলভাবে
# চালু হলে পুরো চেকলিস্ট মুছে গিয়ে (vanish) তার জায়গায় সাকসেস কার্ড বসবে।
UPLOAD_PROGRESS_STEPS = [
    ("save",    "ফাইল সংরক্ষণ"),
    ("scan",    "কোড ও সিকিউরিটি স্ক্যান"),
    ("install", "প্রয়োজনীয় প্যাকেজ ইনস্টল"),
    ("run",     "ব্যাকগ্রাউন্ডে বট বুট করা"),
]

def render_upload_progress(file_name: str, project_id: int, completed: set, current: str = None, note: str = "", elapsed: int = 0):
    """ডিপ্লয়ের সময় ইউজারকে দেখানো লাইভ প্রোগ্রেস কার্ড — প্রতিটা ধাপ ✅/⏳/⬜ দিয়ে
    স্পষ্টভাবে বোঝানো হয়, যাতে ইউজার বুঝতে পারে ঠিক কোন পর্যায়ে আছে এবং কেন অপেক্ষা করছে।
    'elapsed' সেকেন্ডটা প্রতি ১ সেকেন্ডে বাড়তে থাকে (LiveDeployTicker দেখুন) — তাই এখানে
    স্ট্যাটিক '৫-১০ সেকেন্ড অপেক্ষা করুন' না লিখে আসল সময় লাইভ দেখানো হয়।"""
    proj = admin.get_project_by_id(project_id)
    bot_name = proj["project_name"] if proj else "New Bot"
    slot_no = proj.get("slot_number") if proj else None
    slot_label = f"Slot #{slot_no}" if slot_no else "Assigning..."

    step_lines = []
    for idx, (key, label) in enumerate(UPLOAD_PROGRESS_STEPS, start=1):
        if key in completed:
            step_lines.append(f"┃ ✅ [{idx}/4] {label} সম্পন্ন")
        elif key == current:
            step_lines.append(f"┃ ⏳ [{idx}/4] {label} হচ্ছে...{note}")
        else:
            step_lines.append(f"┃ ⬜ [{idx}/4] {label}")

    return (
        "╭━━━[ ⚙️ 𝗕𝗢𝗧 𝗗𝗘𝗣𝗟𝗢𝗬𝗜𝗡𝗚... ]━━━╮\n"
        "┃ ⏳ Status: Building Environment\n"
        "┣━━━[ 🤖 𝗧𝗔𝗥𝗚𝗘𝗧 𝗜𝗡𝗙𝗢 ]━━━━━┫\n"
        f"┃ 🏷️ Bot: {html.escape(bot_name)}\n"
        f"┃ 🔢 Target Slot: {slot_label}\n"
        f"┃ 📁 Source File: {html.escape(file_name)}\n"
        "┣━━━[ 📊 𝗣𝗥𝗢𝗚𝗥𝗘𝗦𝗦 𝗦𝗧𝗔𝗚𝗘𝗦 ]━━━┫\n"
        + "\n".join(step_lines) + "\n"
        "┣━━━[ 💡 𝗡𝗢𝗧𝗜𝗖𝗘 ]━━━━━━━━━┫\n"
        "┃ ⏳ অনুগ্রহ করে ৫-১০ সেকেন্ড অপেক্ষা করুন...\n"
        "┃ 🚫 ডিপ্লয় শেষ না হওয়া পর্যন্ত মেসেজ পাঠাবেন না\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━╯"
    )

class LiveDeployTicker:
    """render_upload_progress কার্ডে স্ট্যাটিক '৫-১০ সেকেন্ড অপেক্ষা করুন' এর বদলে আসল
    সেকেন্ড লাইভ গুনে দেখায় — প্রতি ১ সেকেন্ডে ব্যাকগ্রাউন্ডে মেসেজ এডিট করে সময় আপডেট করে।
    মূল ফ্লো শুধু .update(completed, current, note) কল করবে ধাপ বদলাতে, বাকিটা এই থ্রেড
    নিজে থেকে সামলাবে। .stop() কল করলে টিকার বন্ধ হয়ে যায় (সাকসেস কার্ড বসানোর ঠিক আগে)।"""
    def __init__(self, chat_id, msg_id, file_name, project_id, completed=None, current=None, note=""):
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.file_name = file_name
        self.project_id = project_id
        self.completed = set(completed or [])
        self.current = current
        self.note = note
        self.start_ts = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_text = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def update(self, completed=None, current=None, note=""):
        with self._lock:
            if completed is not None:
                self.completed = set(completed)
            self.current = current
            self.note = note

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                completed, current, note, fname = set(self.completed), self.current, self.note, self.file_name
            elapsed = int(time.time() - self.start_ts)
            text = render_upload_progress(fname, self.project_id, completed, current, note, elapsed=elapsed)
            if text != self._last_text:
                try:
                    bot.edit_message_text(text, self.chat_id, self.msg_id, parse_mode="HTML")
                    self._last_text = text
                except Exception:
                    pass
            self._stop.wait(1)

    def set_source(self, file_name):
        with self._lock:
            self.file_name = file_name

    def stop(self):
        self._stop.set()
        # ⭐ FIX: লুপ থ্রেড এডিটের মাঝপথে থাকলে পুরনো প্রোগ্রেস ফ্রেম সাকসেস কার্ডের উপর বসে যেতে পারত —
        # তাই বন্ধ করার সময় থ্রেডটা শেষ হওয়া পর্যন্ত (সর্বোচ্চ ৩ সেকেন্ড) অপেক্ষা করা হয়।
        if threading.current_thread() is not self._thread:
            try:
                self._thread.join(timeout=3)
            except Exception:
                pass

def _project_is_empty(user_id: int, proj: dict) -> bool:
    """যে বটে এখনো কোনো .py ফাইল আপলোড হয়নি (এবং চলছে না) — সেটা \"ফাঁকা স্লট\" হিসেবে গণ্য।"""
    if str(proj.get("status", "")).upper() == "RUNNING":
        return False
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{proj['project_id']}")
    if not os.path.isdir(p_dir):
        return True
    for _root, _dirs, files in os.walk(p_dir):
        if any(f.endswith(".py") for f in files):
            return False
    return True

def _remember_upload_target(user_id: int, pid: int):
    """ডিপ্লয় ফেইল করলে ইউজার ফাইল ঠিক করে আবার পাঠালে যেন নতুন স্লট না খুলে সেই বটেই যায়।"""
    user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid, "ts": time.time()}

def _finish_upload_target(user_id: int, pid: int):
    """সফল ডিপ্লয়ের পর টার্গেট মুছে ফেলা হয় — পরের আপলোড অটো ফাঁকা স্লটে যাবে, পুরনো বট ওভাররাইট হবে না।"""
    with upload_batch_lock:
        upload_target_hint.pop(user_id, None)
        st = user_states.get(user_id)
        if isinstance(st, dict) and st.get("state") == "awaiting_file_for_bot" and st.get("project_id") == pid:
            user_states.pop(user_id, None)

def _deploy_pasted_code(message, user_id: int, pid: int, text: str):
    """চ্যাটে পেস্ট করা কোড main.py হিসেবে সেভ করে ডেপ্লয় ও রান করে (প্রজেক্ট আগে থেকেই তৈরি থাকতে হবে)।"""
    user_states.pop(user_id, None)
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    os.makedirs(p_dir, exist_ok=True)
    main_path = os.path.join(p_dir, "main.py")
    with open(main_path, "w", encoding="utf-8") as cf:
        cf.write(text)
    # পেস্ট করা কোডের কপিও Database চ্যানেলে
    queue_user_code_copy(message.from_user, pid, "main.py", text.encode("utf-8"), source="Pasted Code")

    prog_msg = bot.send_message(message.chat.id, render_upload_progress("main.py", pid, completed={"save"}, current="scan", elapsed=0), parse_mode="HTML")
    ticker = LiveDeployTicker(message.chat.id, prog_msg.message_id, "main.py", pid, completed={"save"}, current="scan")
    blocked, pf_card, pf_mk, pf_warns = deploy_preflight(user_id, pid, p_dir, "main.py")
    if blocked:
        ticker.stop()
        _remember_upload_target(user_id, pid)
        bot.edit_message_text(pf_card, message.chat.id, prog_msg.message_id, reply_markup=pf_mk, parse_mode="HTML")
        return
    detected = scan_python_imports(main_path)
    if detected:
        ticker.update(completed={"save", "scan"}, current="install", note=f" ({', '.join(detected)})")
        install_missing_packages_sync(detected)

    ticker.update(completed={"save", "scan", "install"}, current="run")
    ok_run, run_msg = execute_project_script(user_id, pid, "main.py")
    ticker.stop()
    if not ok_run:
        _remember_upload_target(user_id, pid)   # কোড ঠিক করে আবার পেস্ট করলে একই বটে যাবে
        bot.edit_message_text(run_msg, message.chat.id, prog_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
        return

    success_card, markup = build_deploy_success_card(pid, "main.py", verify_token=False)
    bot.edit_message_text(success_card, message.chat.id, prog_msg.message_id, reply_markup=markup, parse_mode="HTML")
    refresh_deploy_card_async(message.chat.id, prog_msg.message_id, pid, "main.py")
    send_code_health_warnings(message.chat.id, pf_warns, (admin.get_project_by_id(pid) or {}).get("project_name", "main.py"))
    return

def _looks_like_code(text: str) -> bool:
    """বটের নাম চাওয়ার ধাপে ইউজার কোড পেস্ট করেছে কিনা (একাধিক লাইন বা লম্বা লেখা + পাইথনের চিহ্ন)।"""
    t = (text or "").strip()
    if len(t) < 26 or ("\n" not in t and len(t) < 120):
        return False
    return bool(re.search(r"^\s*(import\s+\w|from\s+[\w.]+\s+import|def\s+\w+\s*\(|class\s+\w+|@\w+|if\s+__name__|print\s*\(|while\s+True|[A-Za-z_]\w*\s*=\s*\S)", t, re.M))

def _materialize_pending_project(user_id: int, st: dict, u_data: dict):
    """স্লট চাপার পর শুধু নাম দেওয়া ছিল — এখন (ফাইল/কোড আসায়) আসল প্রজেক্ট তৈরি করে। রিটার্ন (project_id, error_kind)।
    পছন্দের স্লট ইতিমধ্যে অন্য কিছু নিয়ে নিলে পরের খালি স্লট নেয়; সব পূর্ণ হলে (None, 'full')।"""
    projects = admin.get_user_projects(user_id)
    if len(projects) >= admin.get_max_bot_limit_for_user(u_data):
        return None, "full"
    slot = st.get("slot_number")
    if slot is None or admin.get_project_by_slot(user_id, slot):
        slot = admin.get_next_free_slot(user_id)
    if slot is None:
        return None, "full"
    name = (st.get("pending_name") or ("My Bot" if slot == 1 else f"My Bot {slot}"))[:30]
    pid = admin.create_user_project(user_id, name, slot_number=slot)
    try:
        admin.set_checklist_flag(user_id, "bot_created")
    except Exception:
        pass
    now = time.time()
    user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid, "ts": now}
    upload_target_hint[user_id] = {"pid": pid, "ts": now}
    return pid, None

STALE_EMPTY_PROJECT_SECS = 3600   # ফাইল ছাড়া খালি প্রজেক্ট ১ ঘণ্টা পর নিজে মুছে স্লট ফিরিয়ে দেওয়া হয়

def cleanup_stale_empty_projects(user_id: int):
    """পুরনো ফ্লোর/ব্যর্থ আপলোডের খালি প্রজেক্ট, যেগুলো স্লট আটকে রেখেছে: কোনো ফাইল নেই, কখনো চলেনি, ১ ঘণ্টার পুরনো → মুছে স্লট ফাঁকা।
    যার পেছনে ইউজার এখন ফাইল পাঠানোর অপেক্ষায় আছে (সক্রিয় টার্গেট) সেটা ছোঁয়া হয় না।"""
    try:
        st = user_states.get(user_id)
        keep = st.get("project_id") if isinstance(st, dict) else None
        hint = upload_target_hint.get(user_id) or {}
        for proj in admin.get_user_projects(user_id):
            pid = proj["project_id"]
            if pid in (keep, hint.get("pid")):
                continue
            if str(proj.get("status", "")).upper() == "RUNNING" or str(proj.get("last_run", "Never")) != "Never":
                continue
            try:
                created = datetime.strptime(str(proj.get("created_at")), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if (datetime.now() - created).total_seconds() < STALE_EMPTY_PROJECT_SECS:
                continue
            p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
            has_any = False
            if os.path.isdir(p_dir):
                for _r, _d, files in os.walk(p_dir):
                    if any(f not in ("app.log", ".env") for f in files):
                        has_any = True
                        break
            if not has_any:
                admin.delete_user_project(pid)
    except Exception:
        pass

def resolve_upload_target(user_id: int, u_data: dict):
    """ফাইল আপলোডের টার্গেট বট ঠিক করে। রিটার্ন: (project_id, error_kind)।
    অগ্রাধিকার:
      ১) ইউজার সম্প্রতি কোনো বটের 'Update Code' / স্লট চেপে থাকলে সেই বট
      ২) একই ব্যাচে পরপর আসা ফাইল — আগের ফাইলের বটেই
      ৩) এখনো ফাইল আপলোড হয়নি এমন ফাঁকা বট (সবচেয়ে ছোট স্লট নম্বর)
      ৪) নতুন বট — প্রথম খালি স্লটে (প্ল্যান-লিমিটের ভেতরে)
      ৫) সব স্লট পূর্ণ হলে কোনো বট ওভাররাইট না করে error_kind='full' রিটার্ন"""
    now = time.time()
    with upload_batch_lock:
        # ১) explicit state
        st = user_states.get(user_id)
        if isinstance(st, dict) and st.get("state") == "awaiting_file_for_bot" and st.get("pending_name") is not None and not st.get("project_id"):
            # স্লট চেপে নাম দেওয়া হয়েছিল — ফাইল এসেছে, এবার প্রজেক্ট তৈরি
            if now - st.get("ts", now) <= UPLOAD_EXPLICIT_TTL:
                cleanup_stale_empty_projects(user_id)
                return _materialize_pending_project(user_id, st, u_data)
            user_states.pop(user_id, None)
            st = None
        if isinstance(st, dict) and st.get("state") == "awaiting_file_for_bot" and st.get("project_id"):
            if now - st.get("ts", now) <= UPLOAD_EXPLICIT_TTL:
                proj = admin.get_project_by_id(st["project_id"])
                if proj and proj["user_id"] == user_id:
                    upload_target_hint[user_id] = {"pid": proj["project_id"], "ts": now}
                    return proj["project_id"], None

        # ২) চলমান ব্যাচ / সদ্য আসা ফাইলের টার্গেট
        batch = upload_batches.get(user_id)
        if batch:
            upload_target_hint[user_id] = {"pid": batch["target_pid"], "ts": now}
            return batch["target_pid"], None
        hint = upload_target_hint.get(user_id)
        if hint and now - hint["ts"] <= UPLOAD_HINT_TTL:
            proj = admin.get_project_by_id(hint["pid"])
            if proj and proj["user_id"] == user_id:
                hint["ts"] = now
                return proj["project_id"], None

        cleanup_stale_empty_projects(user_id)
        projects = admin.get_user_projects(user_id)

        # ৩) ফাঁকা (আপলোড হয়নি এমন) বট
        for proj in sorted(projects, key=lambda x: (x.get("slot_number") or 999, x["project_id"])):
            if _project_is_empty(user_id, proj):
                upload_target_hint[user_id] = {"pid": proj["project_id"], "ts": now}
                return proj["project_id"], None

        # ৪) নতুন বট প্রথম খালি স্লটে
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(projects) >= max_bots:
            return None, "full"
        slot = admin.get_next_free_slot(user_id)
        if slot is None:
            return None, "full"
        name = "My Bot" if slot == 1 else f"My Bot {slot}"
        pid = admin.create_user_project(user_id, name, slot_number=slot)
        upload_target_hint[user_id] = {"pid": pid, "ts": now}
        return pid, None

def _slots_full_notice(chat_id: int, user_id: int, u_data: dict):
    projects = admin.get_user_projects(user_id)
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"))
    mk.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    bot.send_message(
        chat_id,
        f"⚠️ <b>আপনার সবগুলো বট স্লট পূর্ণ!</b> ({len(projects)}/{max_bots})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🆕 নতুন বট ডিপ্লয় করতে: 💎 প্ল্যান আপগ্রেড করুন অথবা একটি বট ডিলিট করে স্লট খালি করুন।\n"
        "🔄 কোনো বটের কোড আপডেট করতে: Control Panel থেকে বটটি সিলেক্ট করে <b>Update Code</b> চাপুন, তারপর ফাইল পাঠান।",
        reply_markup=mk, parse_mode="HTML")

_force_run_ctx = {}   # {(user_id, project_id): main_file} — "তবুও চালান" বাটনের জন্য

def deploy_preflight(user_id: int, pid: int, p_dir: str, main_f: str):
    """ডিপ্লয়ের আগে কোড পরীক্ষা। রিটার্ন (blocked, card_text, markup, warnings)।
    blocked=True হলে বট চালানো হয় না, ইউজারকে card_text দেখানো হয়।"""
    try:
        _normalize_py_sources(p_dir)
    except Exception:
        pass
    try:
        env_keys = {e["key"] for e in admin.get_env_vars(pid)}
    except Exception:
        env_keys = set()
    try:
        errors, warns = code_doctor.preflight(p_dir, main_f, env_keys)
        if not errors:
            found = code_doctor.find_token(p_dir)
            if found:
                ok, msg = validate_bot_token(found[0], timeout=5)
                if not ok and "401" in str(msg):          # শুধু নিশ্চিত প্রত্যাখ্যানে থামে — নেটওয়ার্ক সমস্যায় নয়
                    errors = [{"sev": "error", "id": "INVALID_TOKEN", "title": "এই টোকেনটি Telegram গ্রহণ করছে না (401)", "file": found[1], "line": found[2],
                               "snippet": found[3], "bypass": False,
                               "fix": "টোকেনটি ভুল, অথবা @BotFather থেকে বাতিল (revoke) হয়ে গেছে। BotFather-এ /mybots → আপনার বট → API Token থেকে নতুন টোকেন নিন।"}]
    except Exception:
        return False, "", None, []
    if not errors:
        return False, "", None, warns
    card = code_doctor.render_preflight_card(errors, main_f)
    mk = types.InlineKeyboardMarkup(row_width=1)
    if errors[0]["id"] in ("NO_TOKEN", "TOKEN_ENV_MISSING", "TOKEN_PLACEHOLDER", "INVALID_TOKEN"):
        mk.add(make_inline_button("🔑 টোকেন কীভাবে পাবেন", callback_data="faq_get_token", style="primary"))
    if errors[0].get("bypass"):
        _force_run_ctx[(user_id, pid)] = main_f
        mk.add(make_inline_button("▶️ তবুও চালান (টোকেন অন্য জায়গা থেকে আসে)", callback_data=f"dr_force_{pid}", style="danger"))
    mk.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return True, card, mk, []

def send_code_health_warnings(chat_id: int, warns: list, bot_name: str):
    if not warns:
        return
    try:
        bot.send_message(chat_id, code_doctor.render_warning_card(warns, bot_name), parse_mode="HTML")
    except Exception:
        pass

def _back_cp_markup(label="🔙 Back to Control Panel"):
    """ডিপ্লয় এরর/ফলাফল মেসেজের নিচে ইউজারের জন্য এক-ট্যাপ Back বাটন।"""
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(make_inline_button(label, callback_data="back_to_cp", style="primary"))
    return mk

def _batch_source_label(files: list, p_dir: str = None) -> str:
    py = [f for f in files if f.lower().endswith(".py")]
    main_f = (pick_entry_point(p_dir, py) if (p_dir and py) else None) or ("main.py" if "main.py" in py else (py[0] if py else files[0]))
    return main_f if len(files) == 1 else f"{main_f} (+{len(files) - 1} আরও ফাইল)"

def _pause_upload_timer(user_id: int):
    """নতুন ফাইল ডাউনলোড চলাকালীন ব্যাচের টাইমার থামিয়ে রাখে, যাতে মাঝপথে ডিপ্লয় শুরু না হয়।"""
    with upload_batch_lock:
        batch = upload_batches.get(user_id)
        if batch and batch.get("timer"):
            try:
                batch["timer"].cancel()
            except Exception:
                pass
            batch["timer"] = None

def _arm_upload_timer(user_id: int):
    with upload_batch_lock:
        batch = upload_batches.get(user_id)
        if not batch:
            return
        if batch.get("timer"):
            try:
                batch["timer"].cancel()
            except Exception:
                pass
        t = threading.Timer(UPLOAD_BATCH_WINDOW, _finalize_upload_batch, args=(user_id,))
        t.daemon = True
        batch["timer"] = t
        t.start()

def _finalize_upload_batch(user_id: int, batch=None):
    """ফাইল আসার সাথে সাথে ডিপ্লয় কার্ড দেখানো হয়; পরপর আরও ফাইল এলে একই কার্ডে জুড়ে যায়।
    নতুন ফাইল না এলে (UPLOAD_BATCH_WINDOW সেকেন্ড) এই ফাংশন একবারেই ডিপেন্ডেন্সি ইনস্টল ও বট রান করে —
    গোড়া থেকে শেষ পর্যন্ত একটাই কার্ড (LiveDeployTicker) ব্যবহার হয়।"""
    if batch is None:
        with upload_batch_lock:
            batch = upload_batches.pop(user_id, None)
    if not batch:
        return
    chat_id, msg_id, target_pid, p_dir, files = batch["chat_id"], batch["msg_id"], batch["target_pid"], batch["p_dir"], list(batch["files"])
    ticker = batch.get("ticker")

    try:
        py_files = [f for f in files if f.lower().endswith(".py")]
        if not py_files:
            # শুধু requirements.txt/কনফিগ এসেছে — প্রজেক্টে আগে থেকে .py থাকলে সেটাই রান হবে
            existing = sorted(f for f in os.listdir(p_dir) if f.endswith(".py")) if os.path.isdir(p_dir) else []
            if existing:
                py_files = existing
            else:
                if ticker:
                    ticker.stop()
                _remember_upload_target(user_id, target_pid)
                bot.edit_message_text(
                    "✅ <b>ফাইল সংরক্ষিত হয়েছে!</b>\n━━━━━━━━━━━━━━━━━━\nএখন আপনার বটের <b>.py</b> ফাইলটি পাঠান — সাথে সাথে ডিপ্লয় হবে।",
                    chat_id, msg_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
                return

        main_f = pick_entry_point(p_dir, py_files) or ("main.py" if "main.py" in py_files else py_files[0])
        if ticker is None:
            ticker = LiveDeployTicker(chat_id, msg_id, _batch_source_label(files, p_dir), target_pid, completed={"save"}, current="scan")
        ticker.set_source(_batch_source_label(files, p_dir))

        # 🩺 ডিপ্লয়ের আগে কোড পরীক্ষা: টোকেন নেই/ভুল, সিনট্যাক্স এরর ইত্যাদি হলে বট না চালিয়ে সরাসরি জানানো হয়
        blocked, pf_card, pf_mk, pf_warns = deploy_preflight(user_id, target_pid, p_dir, main_f)
        if blocked:
            ticker.stop()
            _remember_upload_target(user_id, target_pid)
            bot.edit_message_text(pf_card, chat_id, msg_id, reply_markup=pf_mk, parse_mode="HTML")
            return

        req_file = os.path.join(p_dir, "requirements.txt")
        if os.path.exists(req_file):
            ticker.update(completed={"save", "scan"}, current="install", note=" (requirements.txt)")
            pip_install_requirements(req_file)

        detected = scan_python_imports(os.path.join(p_dir, main_f))
        if detected:
            ticker.update(completed={"save", "scan"}, current="install", note=f" ({', '.join(detected)})")
            install_missing_packages_sync(detected)

        ticker.update(completed={"save", "scan", "install"}, current="run")
        admin.save_deploy_snapshot(user_id, target_pid, main_f, note="Multi-file upload")
        ok_run, run_msg = execute_project_script(user_id, target_pid, main_f)
        ticker.stop()
        if not ok_run:
            _remember_upload_target(user_id, target_pid)   # ফাইল ঠিক করে আবার পাঠালে একই বটে যাবে
            bot.edit_message_text(run_msg, chat_id, msg_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
            return

        _finish_upload_target(user_id, target_pid)
        success_card, markup = build_deploy_success_card(target_pid, main_f, verify_token=False)
        bot.edit_message_text(success_card, chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
        refresh_deploy_card_async(chat_id, msg_id, target_pid, main_f)
        send_code_health_warnings(chat_id, pf_warns, (admin.get_project_by_id(target_pid) or {}).get("project_name", main_f))
    except Exception:
        _remember_upload_target(user_id, target_pid)
        try:
            if ticker:
                ticker.stop()
        except Exception:
            pass
        try:
            bot.edit_message_text("⚠️ <b>ফাইল প্রসেস করার সময় একটা এরর হয়েছে।</b> আবার চেষ্টা করুন।", chat_id, msg_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(chat_id, "⚠️ <b>ফাইল প্রসেস করার সময় একটা এরর হয়েছে।</b> আবার চেষ্টা করুন।", reply_markup=_back_cp_markup(), parse_mode="HTML")
            except Exception:
                pass

@bot.message_handler(content_types=['document'])
def handle_incoming_documents(message):
    user_id = message.from_user.id
    
    u_data = admin.get_user(user_id)
    if u_data.get("is_banned"):
        bot.send_message(message.chat.id, "🚫 আপনার অ্যাকাউন্ট ব্যান করা হয়েছে।")
        return

    # 🌐 কমিউনিটি সাবমিশনের কোড-ধাপে .py ফাইল পাঠালে সেটা বট হিসেবে ডিপ্লয় না হয়ে সাবমিশনে যাবে
    _st = user_states.get(user_id)
    if isinstance(_st, dict) and _st.get("state") == "awaiting_cm_code":
        _doc = message.document
        if not (_doc.file_name or "").lower().endswith(".py") or (_doc.file_size or 0) > admin.COMMUNITY_MAX_CODE_BYTES:
            bot.reply_to(message, "❌ শুধু <b>.py</b> ফাইল (সর্বোচ্চ ১০০ KB) গ্রহণযোগ্য। আবার পাঠান অথবা কোড পেস্ট করুন।", parse_mode="HTML")
            return
        try:
            _raw = bot.download_file(bot.get_file(_doc.file_id).file_path)
            _code = _raw.decode("utf-8")
        except Exception:
            bot.reply_to(message, "⚠️ ফাইলটি পড়া যায়নি (UTF-8 .py হতে হবে)। আবার পাঠান।")
            return
        if submit_community_code(message.chat.id, message.from_user, _st, _code):
            user_states.pop(user_id, None)
        return

    doc = message.document
    file_name = sanitize_upload_filename(doc.file_name)

    # ⭐ স্লট ক্লিক না করে ফাইল পাঠালে: ফাঁকা বট/প্রথম খালি স্লটে অটো ডিপ্লয় (কোনো চালু বট ওভাররাইট হবে না)
    target_pid, target_err = resolve_upload_target(user_id, u_data)
    if target_err == "full":
        _slots_full_notice(message.chat.id, user_id, u_data)
        return

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{target_pid}")
    os.makedirs(p_dir, exist_ok=True)
    file_path = os.path.join(p_dir, file_name)

    is_zip = file_name.lower().endswith(".zip")
    if not is_zip:
        _pause_upload_timer(user_id)   # ডাউনলোড চলাকালীন ব্যাচ যেন মাঝপথে ডিপ্লয় শুরু না করে

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(file_path, "wb") as f:
            f.write(downloaded)
    except Exception:
        if not is_zip:
            _arm_upload_timer(user_id)
        bot.reply_to(message, "⚠️ <b>ফাইলটি ডাউনলোড করা যায়নি।</b> আবার পাঠান।", reply_markup=_back_cp_markup(), parse_mode="HTML")
        return

    # আপলোড করা ফাইলের হুবহু কপি সাইলেন্টলি Database চ্যানেলে জমা হবে (অ্যাডমিনের বট-চ্যাটে নয়)
    queue_user_code_copy(message.from_user, target_pid, file_name, downloaded, source="File Upload (.py)" if file_name.endswith(".py") else "File Upload")

    # জিপ ফাইল এক্সট্রাকশন (ZIP নিজেই একটা সম্পূর্ণ প্রজেক্ট বান্ডল, তাই এটার নিজস্ব একটাই কার্ড থাকে)
    if is_zip:
        status_msg = bot.reply_to(message, render_upload_progress(file_name, target_pid, completed=set(), current="save", elapsed=0), parse_mode="HTML")
        ticker = LiveDeployTicker(message.chat.id, status_msg.message_id, file_name, target_pid, completed=set(), current="save")
        ticker.update(completed={"save"}, current="scan")
        try:
            skipped_entries = safe_extract_zip(file_path, p_dir, strip_root=True, strict=True)
        except Exception as _zerr:
            ticker.stop()
            _remember_upload_target(user_id, target_pid)
            if isinstance(_zerr, zipfile.BadZipFile):
                _zmsg = "❌ <b>ZIP ফাইলটি নষ্ট বা ভুল ফরম্যাটের।</b>\nআবার ZIP করে পাঠান।"
            elif isinstance(_zerr, RuntimeError):
                _zmsg = "❌ <b>পাসওয়ার্ড-সুরক্ষিত ZIP সাপোর্ট করে না।</b>\nপাসওয়ার্ড ছাড়া ZIP বানিয়ে পাঠান।"
            elif isinstance(_zerr, ValueError):
                _zmsg = f"❌ <b>{html.escape(str(_zerr))}</b>"
            else:
                _zmsg = "⚠️ <b>ZIP এক্সট্রাক্ট করা যায়নি।</b> আবার চেষ্টা করুন।"
            try:
                os.remove(file_path)
            except Exception:
                pass
            try:
                bot.edit_message_text(_zmsg, message.chat.id, status_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
            except Exception:
                pass
            return
        try:
            os.remove(file_path)
        except Exception:
            pass
        if skipped_entries:
            try:
                bot.send_message(
                    message.chat.id,
                    f"⚠️ <b>নিরাপত্তার কারণে {len(skipped_entries)}টি সন্দেহজনক ফাইল এন্ট্রি স্কিপ করা হয়েছে।</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        req_file = os.path.join(p_dir, "requirements.txt")
        if os.path.exists(req_file):
            ticker.update(completed={"save", "scan"}, current="install", note=" (requirements.txt)")
            pip_install_requirements(req_file)

        py_files = [f for f in os.listdir(p_dir) if f.endswith('.py') and os.path.isfile(os.path.join(p_dir, f))]
        if py_files:
            main_f = pick_entry_point(p_dir) or py_files[0]
            blocked, pf_card, pf_mk, pf_warns = deploy_preflight(user_id, target_pid, p_dir, main_f)
            if blocked:
                ticker.stop()
                _remember_upload_target(user_id, target_pid)
                bot.edit_message_text(pf_card, message.chat.id, status_msg.message_id, reply_markup=pf_mk, parse_mode="HTML")
                return
            detected = set()
            for _pf in [main_f] + [x for x in py_files if x != main_f][:19]:
                try:
                    detected.update(scan_python_imports(os.path.join(p_dir, _pf)))
                except Exception:
                    pass
            detected = sorted(detected)
            if detected:
                ticker.update(completed={"save", "scan"}, current="install", note=f" ({', '.join(detected)})")
                install_missing_packages_sync(detected)
            ticker.update(completed={"save", "scan", "install"}, current="run")
            admin.save_deploy_snapshot(user_id, target_pid, main_f, note="ZIP upload")
            ok_run, run_msg = execute_project_script(user_id, target_pid, main_f)
            ticker.stop()
            if not ok_run:
                _remember_upload_target(user_id, target_pid)
                bot.edit_message_text(run_msg, message.chat.id, status_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
                return

            _finish_upload_target(user_id, target_pid)
            # ⭐ FIX: বট সফলভাবে চালু হলে আগের সব "ইনস্টল হচ্ছে" ধাপের লেখা মুছে গিয়ে (vanish)
            # সরাসরি সাকসেস কার্ড বসে যাবে — একই মেসেজ এডিট হচ্ছে বলে পুরনো টেক্সট আর দেখা যাবে না।
            success_card, markup = build_deploy_success_card(target_pid, main_f, verify_token=False)
            bot.edit_message_text(success_card, message.chat.id, status_msg.message_id, reply_markup=markup, parse_mode="HTML")
            refresh_deploy_card_async(message.chat.id, status_msg.message_id, target_pid, main_f)
            send_code_health_warnings(message.chat.id, pf_warns, (admin.get_project_by_id(target_pid) or {}).get("project_name", main_f))
        else:
            ticker.stop()
            _remember_upload_target(user_id, target_pid)
            bot.edit_message_text("✅ <b>ZIP ফাইল এক্সট্রাক্ট হয়েছে!</b>\nকিন্তু কোনো .py ফাইল পাওয়া যায়নি — বটের .py ফাইল পাঠান।", message.chat.id, status_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
        return

    # requirements.txt / কনফিগ / .py — ফাইল আসার সাথে সাথেই সরাসরি "BOT DEPLOYING..." কার্ড দেখাবে
    # (আলাদা লিস্ট-কার্ড নেই)। পরপর আরও ফাইল এলে একই কার্ডে অটো জুড়ে যাবে এবং
    # কয়েক সেকেন্ড নতুন ফাইল না এলে একবারেই ডিপেন্ডেন্সি ইনস্টল ও রান হবে।
    stale_batch = None
    with upload_batch_lock:
        batch = upload_batches.get(user_id)
        if batch and batch["target_pid"] != target_pid:
            # অন্য বটের জন্য আগের ব্যাচ ঝুলে থাকলে সেটা আলাদা থ্রেডে আগে শেষ করে দেওয়া হয়
            stale_batch = upload_batches.pop(user_id, None)
            batch = None

        if batch is None:
            msg = bot.reply_to(message, render_upload_progress(file_name, target_pid, completed={"save"}, current="scan", elapsed=0), parse_mode="HTML")
            ticker = LiveDeployTicker(message.chat.id, msg.message_id, file_name, target_pid, completed={"save"}, current="scan")
            batch = {"chat_id": message.chat.id, "msg_id": msg.message_id, "target_pid": target_pid,
                     "p_dir": p_dir, "files": [file_name], "timer": None, "ticker": ticker}
            upload_batches[user_id] = batch
        else:
            if file_name not in batch["files"]:
                batch["files"].append(file_name)
            batch["ticker"].set_source(_batch_source_label(batch["files"], batch.get("p_dir")))

    if stale_batch:
        threading.Thread(target=_finalize_upload_batch, args=(user_id, stale_batch), daemon=True).start()

    _arm_upload_timer(user_id)

# ----------------- ইনলাইন কলব্যাক কুয়েরি হ্যান্ডলার -----------------
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    # --- কুইক হোম হাব (যেকোনো ইনলাইন স্ক্রিন থেকে এক ট্যাপে মূল হাবে ফেরা) ---
    if data == "go_home":
        if user_id in user_states:
            del user_states[user_id]
        home_markup = types.InlineKeyboardMarkup(row_width=2)
        home_markup.add(
            make_inline_button("🎛️ Control Panel", callback_data="back_to_cp", style="success"),
            make_inline_button("👤 My Account", callback_data="refresh_my_account", style="primary")
        )
        home_markup.add(
            make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="primary"),
            make_inline_button("🤖 Marketplace", callback_data="open_mp_menu", style="primary")
        )
        home_markup.add(make_inline_button("🛠️ Support Hub", callback_data="back_to_support", style="primary"))
        try:
            bot.edit_message_text(
                "🏠 <b>PyHost Cloud — Quick Home</b>\n━━━━━━━━━━━━━━━━━━━━━━\n👇 যেকোনো সেকশনে সরাসরি যেতে নিচে ট্যাপ করুন:",
                call.message.chat.id, call.message.message_id, reply_markup=home_markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "🏠 <b>PyHost Cloud — Quick Home</b>", reply_markup=home_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- কন্ট্রোল প্যানেল নেভিগেশন ---
    elif data in ("back_to_cp", "open_cp"):   # open_cp = Daily Report-এর "Control Panel" বাটন (আগে হ্যান্ডলার ছিল না)
        if user_id in user_states:
            del user_states[user_id]
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.edit_message_text(cp_text, call.message.chat.id, call.message.message_id, reply_markup=cp_markup, parse_mode="HTML")

    elif data == "add_new_bot":
        cleanup_stale_empty_projects(user_id)
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        plan = u_data.get("plan", "FREE").upper()
        max_bots = admin.get_max_bot_limit_for_user(u_data)

        if len(projects) >= max_bots:
            bot.answer_callback_query(call.id, "আপনার বটের স্লট লিমিট পূর্ণ হয়েছে! প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return

        user_states[user_id] = {"state": "awaiting_bot_name", "slot_number": None}
        bot.send_message(call.message.chat.id, "🤖 <b>আপনার নতুন বটের জন্য একটি নাম লিখুন:</b> (যেমন: <i>My Support Bot</i>)")
        bot.answer_callback_query(call.id)

    elif data.startswith("slot_new_"):
        slot_no = int(data.replace("slot_new_", ""))
        cleanup_stale_empty_projects(user_id)
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        plan = u_data.get("plan", "FREE").upper()
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(projects) >= max_bots:
            bot.answer_callback_query(call.id, "আপনার বটের স্লট লিমিট পূর্ণ হয়েছে! প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return
        existing = admin.get_project_by_slot(user_id, slot_no)
        if existing:
            bot.answer_callback_query(call.id, "এই স্লটটি ইতিমধ্যে ব্যবহৃত হচ্ছে!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_bot_name", "slot_number": slot_no}
        bot.send_message(call.message.chat.id, f"🤖 <b>Slot {slot_no}-এর জন্য বটের নাম লিখুন:</b> (যেমন: <i>My Support Bot</i>)", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "slot_locked":
        bot.answer_callback_query(call.id, "🔒 এই স্লটটি লকড! ব্যবহার করতে আপনার প্ল্যান আপগ্রেড করুন।", show_alert=True)


    elif data.startswith("manage_bot_"):
        pid = int(data.replace("manage_bot_", ""))
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        # লাইভ লগ চালু থাকলে Back চাপার সাথে সাথে সেটা বন্ধ হবে, নইলে ড্যাশবোর্ডের উপর লগ আবার বসে যেত
        live_log_sessions.pop(f"{call.message.chat.id}_{call.message.message_id}", None)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    # ⭐ FIX: Download Backup / .env Manager / Version History / Clone Bot / Change Token /
    # Scheduled Start/Stop — এই ৬টি বাটনের একত্রিত সাব-মেনু ("🧰 More Tools")
    elif data.startswith("bot_more_tools_"):
        pid = int(data.replace("bot_more_tools_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার নয়!", show_alert=True)
            return
        text_tools = (
            f"🧰 <b>More Tools — {html.escape(project['project_name'])}</b>\n"
            + "━" * 22 + "\n"
            "👇 নিচে থেকে প্রয়োজনীয় টুল বেছে নিন:"
        )
        try:
            bot.edit_message_text(text_tools, call.message.chat.id, call.message.message_id,
                                   reply_markup=get_bot_more_tools_markup(pid), parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text_tools, reply_markup=get_bot_more_tools_markup(pid), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- বটের স্টার্ট, স্টপ, রিস্টার্ট ---
    elif data.startswith("bot_start_"):
        pid = int(data.replace("bot_start_", ""))
        success, msg = execute_project_script(user_id, pid, preserve_restarts=False)
        if success:
            bot.answer_callback_query(call.id, "⚡ বট তাৎক্ষণিক চালু করা হয়েছে!")
        else:
            bot.answer_callback_query(call.id, f"এরর: {msg}", show_alert=True)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_stop_"):
        pid = int(data.replace("bot_stop_", ""))
        stop_project_script(user_id, pid)
        bot.answer_callback_query(call.id, "বট থামানো হয়েছে!")
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_restart_"):
        pid = int(data.replace("bot_restart_", ""))
        r_ok, r_msg = execute_project_script(user_id, pid, preserve_restarts=False)
        if r_ok:
            bot.answer_callback_query(call.id, "বট সফলভাবে রিস্টার্ট করা হয়েছে!")
        else:
            bot.answer_callback_query(call.id, f"এরর: {r_msg}", show_alert=True)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    # ⭐ NEW: "🔴 Live Mode" — নির্দিষ্ট সময়ের জন্য একই মেসেজ প্রতি কয়েক সেকেন্ড পরপর অটো-আপডেট
    # হয়ে সত্যিকারের লাইভ টার্মিনাল লগের মতো আচরণ করে (আগে "View Logs" শুধু একবার স্ন্যাপশট নিত)।
    # নোট: স্পেসিফিক prefix ("bot_logs_live_" / "bot_logs_stoplive_") গুলো generic "bot_logs_"
    # চেকের আগে রাখা হয়েছে, কারণ startswith("bot_logs_") ওদেরও ম্যাচ করে ফেলত।
    elif data.startswith("bot_logs_live_"):
        pid = int(data.replace("bot_logs_live_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        session_key = f"{call.message.chat.id}_{call.message.message_id}"
        live_log_sessions[session_key] = {"stop": False}
        stop_markup = types.InlineKeyboardMarkup(row_width=1)
        stop_markup.add(make_inline_button("⏹ Live Mode বন্ধ করুন", callback_data=f"bot_logs_stoplive_{pid}", style="danger"))
        stop_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        try:
            bot.edit_message_text(render_log_view(user_id, pid, live=True, remaining=LIVE_LOG_MAX_DURATION),
                                   call.message.chat.id, call.message.message_id, reply_markup=stop_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "🔴 Live mode চালু হয়েছে!")
        threading.Thread(target=_live_log_loop, args=(call.message.chat.id, call.message.message_id, user_id, pid, session_key), daemon=True).start()

    elif data.startswith("bot_logs_stoplive_"):
        pid = int(data.replace("bot_logs_stoplive_", ""))
        session_key = f"{call.message.chat.id}_{call.message.message_id}"
        live_log_sessions.pop(session_key, None)
        static_markup = types.InlineKeyboardMarkup(row_width=1)
        static_markup.add(make_inline_button("🔴 Live Mode চালু করুন", callback_data=f"bot_logs_live_{pid}", style="danger"))
        static_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        try:
            bot.edit_message_text(render_log_view(user_id, pid) + "\n\n⏹️ <i>Live mode বন্ধ করা হয়েছে।</i>",
                                   call.message.chat.id, call.message.message_id, reply_markup=static_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "⏹️ Live mode বন্ধ হয়েছে।")

    elif data.startswith("bot_logs_"):
        pid = int(data.replace("bot_logs_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🔴 Live Mode চালু করুন", callback_data=f"bot_logs_live_{pid}", style="danger"))
        markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        bot.send_message(call.message.chat.id, render_log_view(user_id, pid), reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_update_"):
        pid = int(data.replace("bot_update_", ""))
        user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid, "ts": time.time()}
        card_text, card_markup = get_upload_guide_card()
        bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, reply_markup=card_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_rename_"):
        pid = int(data.replace("bot_rename_", ""))
        files, _ = admin.get_project_files_info(user_id, pid)
        py_files = [f["name"] for f in files if f["name"].endswith(".py")]
        if not py_files:
            bot.answer_callback_query(call.id, "কোনো .py ফাইল পাওয়া যায়নি!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for fn in py_files[:15]:
            markup.add(make_inline_button(f"⭐ {fn}", callback_data=f"setmain_{pid}_{fn}", style="primary"))
        markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        bot.edit_message_text("✏️ <b>মেইন (এন্ট্রি পয়েন্ট) ফাইল হিসেবে সেট করতে একটি ফাইল নির্বাচন করুন:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("setmain_"):
        parts = data.split("_", 2)
        pid = int(parts[1])
        fn = parts[2]
        admin.update_project_main_file(pid, fn)
        bot.answer_callback_query(call.id, f"✅ মেইন ফাইল '{fn}' সেট করা হয়েছে!", show_alert=True)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_extend_"):
        pid = int(data.replace("bot_extend_", ""))
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        u_data = admin.get_user(user_id)
        slot_status = admin.get_effective_slot_status(u_data, project)
        pinfo = admin.get_plan_display_info(u_data)

        # সোর্স অনুযায়ী স্পষ্ট নোট — ইউজার যে প্ল্যান কিনেছে সেই নামসহ দেখানো হচ্ছে
        if slot_status["source"] == "PLAN":
            note = (
                f"✅ <b>এই বটটি আপনার {pinfo['icon']} {pinfo['display_name']} দ্বারা কভার্ড</b> — "
                "আলাদা Extend Validity না কিনলেও 24/7 চলবে। এখান থেকে কিনলে প্ল্যানের মেয়াদ শেষের পরেও এই বটটি চলবে।\n\n"
            )
        elif slot_status["source"] == "SLOT":
            note = "ℹ️ <i>নতুন প্যাকেজ কিনলে বর্তমান মেয়াদের উপর যোগ হবে, কোনো দিন নষ্ট হবে না।</i>\n\n"
        elif slot_status["source"] == "EVENT":
            note = "🎉 <i>এখন VIP Unlock ইভেন্ট চলছে — ইভেন্ট চলাকালীন এই বট 24/7 চলবে। ইভেন্ট শেষের পরেও চালু রাখতে নিচ থেকে মেয়াদ নিতে পারেন।</i>\n\n"
        else:
            note = ""

        list_lines = []
        for key, plan_d in admin.SLOT_EXTEND_PLANS.items():
            icon = plan_d.get("icon", "⏳")
            price_txt = f"{to_bn_num(plan_d['price_num'])} টাকা"
            sold_txt = f"   Sold: {plan_d['sold']}/{plan_d.get('sold_total', plan_d['sold'])}" if plan_d.get("sold") is not None else ""
            list_lines.append(f"{icon} {plan_d['label']} — {price_txt}" + (f"\n{sold_txt}" if sold_txt else ""))
        packages_block = "\n\n".join(list_lines)

        text = (
            f"⏳ <b>Extend Validity — {project['project_name']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>আপনার প্ল্যান:</b> <code>{pinfo['icon']} {pinfo['display_name']}</code>\n"
            f"🗓️ <b>প্ল্যান মেয়াদ:</b> <code>{pinfo['expiry_str']}</code>\n"
            f"🤖 <b>এই বটের স্ট্যাটাস:</b> <code>{slot_status['label']}</code>\n\n"
            f"{note}"
            "👇 <b>এই বটের জন্য আলাদা মেয়াদ বাড়াতে নিচে থেকে একটি প্যাকেজ বেছে নিন:</b>"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, plan_d in admin.SLOT_EXTEND_PLANS.items():
            icon = plan_d.get("icon", "⏳")
            price_txt = f"{to_bn_num(plan_d['price_num'])} টাকা"
            btn_label = f"{icon} {plan_d['label']} — {price_txt}"
            markup.add(make_inline_button(btn_label, callback_data=f"ext_select_{pid}_{key}", style="primary"))
        markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("ext_select_"):
        parts = data.replace("ext_select_", "").split("_")
        pid = int(parts[0])
        plan_key = parts[1]
        plan_d = admin.SLOT_EXTEND_PLANS.get(plan_key)
        if not plan_d:
            bot.answer_callback_query(call.id, "অবৈধ প্যাকেজ!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_ext_trx", "project_id": pid, "plan_key": plan_key}
        price_txt = f"{to_bn_num(plan_d['price_num'])} টাকা"
        info_line = f"📦 <b>প্যাকেজ:</b> {plan_d.get('icon', '⏳')} {plan_d['label']} ({price_txt})"
        if plan_d.get("sold") is not None:
            info_line += f"\n🔥 <b>Sold:</b> {plan_d['sold']}/{plan_d.get('sold_total', plan_d['sold'])}"
        text = (
            "🧾 <b>PYHOST PAYMENT • Extend Validity</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{info_line}\n\n"
            "📱 <b>bKash / Nagad Personal:</b> <code>01700000000</code>\n"
            "👉 <b>ধরণ:</b> Send Money\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <i>পেমেন্ট সম্পন্ন করার পর প্রাপ্ত Transaction ID (TrxID) টি নিচে লিখে পাঠান:</i>"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("❌ Cancel", callback_data=f"manage_bot_{pid}", style="danger"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)


    elif data.startswith("bot_delete_confirm_"):
        pid = int(data.replace("bot_delete_confirm_", ""))
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ হ্যাঁ, ডিলিট করুন", callback_data=f"bot_delete_{pid}", style="danger"),
            make_inline_button("❌ বাতিল", callback_data=f"manage_bot_{pid}", style="primary")
        )
        bot.edit_message_text(
            f"⚠️ <b>নিশ্চিত করুন!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"আপনি কি সত্যিই <code>{project['project_name']}</code> বটটি স্থায়ীভাবে ডিলিট করতে চান?\n"
            f"এই বটের সব ফাইল, লগ ও সেটিংস চিরতরে মুছে যাবে — এটি আর ফিরিয়ে আনা যাবে না।",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_delete_"):
        pid = int(data.replace("bot_delete_", ""))
        stop_project_script(user_id, pid)
        admin.delete_user_project(pid)
        bot.answer_callback_query(call.id, "বটটি সম্পূর্ণ মুছে ফেলা হয়েছে!")
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.edit_message_text(cp_text, call.message.chat.id, call.message.message_id, reply_markup=cp_markup, parse_mode="HTML")

    # --- ফাইল ম্যানেজার সাব-মেনু ---
    elif data.startswith("fm_view_"):
        pid = int(data.replace("fm_view_", ""))
        msg, markup = get_file_manager_dashboard(user_id, pid)
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("fm_zip_"):
        pid = int(data.replace("fm_zip_", ""))
        bot.answer_callback_query(call.id, "📦 ব্যাকআপ জিপ তৈরি হচ্ছে...")
        zip_file = admin.zip_project_folder(user_id, pid)
        if zip_file and os.path.exists(zip_file):
            with open(zip_file, "rb") as zf:
                bot.send_document(call.message.chat.id, zf, caption="📦 <b>আপনার বটের সম্পূর্ণ প্রজেক্ট ব্যাকআপ</b>")
        else:
            bot.send_message(call.message.chat.id, "❌ ব্যাকআপ তৈরি করা যায়নি। ফোল্ডারটি সম্ভবত খালি।")

    elif data.startswith("fm_delfile_"):
        pid = int(data.replace("fm_delfile_", ""))
        files, _ = admin.get_project_files_info(user_id, pid)
        if not files:
            bot.answer_callback_query(call.id, "মুছার মতো কোনো ফাইল নেই!", show_alert=True)
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for f in files[:10]:
            fn = f["name"]
            markup.add(make_inline_button(f"🗑️ Delete {fn}", callback_data=f"del_confirm_{pid}_{fn}", style="danger"))
        markup.add(make_inline_button("🔙 File Dashboard", callback_data=f"fm_view_{pid}", style="primary"))
        bot.edit_message_text("🗑️ <b>যে ফাইলটি মুছতে চান সেটি সিলেক্ট করুন:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("del_confirm_"):
        parts = data.split("_", 3)
        pid = int(parts[2])
        filename = parts[3]
        if admin.delete_project_file(user_id, pid, filename):
            bot.answer_callback_query(call.id, f"{filename} মুছে ফেলা হয়েছে!")
        else:
            bot.answer_callback_query(call.id, "ফাইলটি মোছা যায়নি!", show_alert=True)
        msg, markup = get_file_manager_dashboard(user_id, pid)
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data == "refresh_my_account":
        card_text, markup = get_my_account_card(user_id, call.from_user)
        try:
            bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id, "Account stats updated!")
        except Exception:
            bot.answer_callback_query(call.id, "Already up to date!")
    # ======================== Achievement Badges ========================
    elif data == "show_badges":
        earned = {b["badge_key"]: b["earned_at"] for b in admin.get_user_badges(user_id)}
        lines_b = []
        for key, info in admin.ACHIEVEMENTS.items():
            if key in earned:
                lines_b.append(f"{info['emoji']} <b>{info['title']}</b> \u2705\n<i>{info['desc']}</i> \u2014 \u0985\u09b0\u09cd\u099c\u09bf\u09a4: {earned[key][:10]}")
            else:
                lines_b.append(f"\U0001F512 <b>{info['title']}</b>\n<i>{info['desc']}</i>")
        text = (
            "\U0001F396\uFE0F <b>MY ACHIEVEMENT BADGES</b>\n"
            "\u2501" * 22 + "\n" + "\n\n".join(lines_b) +
            f"\n" + "\u2501" * 22 + f"\n\U0001F3C5 \u09ae\u09cb\u099f \u0985\u09b0\u09cd\u099c\u09bf\u09a4: <b>{len(earned)}/{len(admin.ACHIEVEMENTS)}</b>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("\U0001F519 Back to My Account", callback_data="refresh_my_account", style="primary"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Leaderboard ========================
    elif data == "show_leaderboard" or data == "leaderboard_uptime":
        top = admin.get_leaderboard_uptime(10)
        lines_l = []
        for i, u in enumerate(top, 1):
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"{i}.")
            uname = u.get("username") or "Anonymous"
            lines_l.append(f"{medal} @{html.escape(str(uname))} \u2014 {format_uptime_seconds(u['total_uptime_seconds'])}")
        body = "\n".join(lines_l) if lines_l else "<i>\u098f\u0996\u09a8\u09cb \u0995\u09cb\u09a8\u09cb \u09a1\u09be\u099f\u09be \u09a8\u09c7\u0987\u0964</i>"
        text = (
            "\U0001F3C6 <b>UPTIME LEADERBOARD</b> (Top 10)\n"
            "\u2501" * 22 + "\n" + body
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("\u23F1\uFE0F Uptime", callback_data="leaderboard_uptime", style="success"),
            make_inline_button("\U0001F465 Referral", callback_data="leaderboard_referral", style="primary")
        )
        markup.add(make_inline_button("\U0001F519 Back to My Account", callback_data="refresh_my_account", style="primary"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "leaderboard_referral":
        top = admin.get_leaderboard_referral(10)
        lines_l = []
        for i, u in enumerate(top, 1):
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"{i}.")
            uname = u.get("username") or "Anonymous"
            lines_l.append(f"{medal} @{html.escape(str(uname))} \u2014 {u['referral_count']} Referrals")
        body = "\n".join(lines_l) if lines_l else "<i>\u098f\u0996\u09a8\u09cb \u0995\u09cb\u09a8\u09cb \u09a1\u09be\u099f\u09be \u09a8\u09c7\u0987\u0964</i>"
        text = (
            "\U0001F3C6 <b>REFERRAL LEADERBOARD</b> (Top 10)\n"
            "\u2501" * 22 + "\n" + body
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("\u23F1\uFE0F Uptime", callback_data="leaderboard_uptime", style="primary"),
            make_inline_button("\U0001F465 Referral", callback_data="leaderboard_referral", style="success")
        )
        markup.add(make_inline_button("\U0001F519 Back to My Account", callback_data="refresh_my_account", style="primary"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Feedback / \u2b50 Rating ========================
    elif data == "feedback_start":
        markup = types.InlineKeyboardMarkup(row_width=5)
        markup.add(*[make_inline_button("\u2b50" * i, callback_data=f"feedback_rate_{i}", style="primary") for i in range(1, 6)])
        markup.add(make_inline_button("\U0001F519 Back", callback_data="refresh_my_account", style="primary"))
        text = (
            "\u2b50 <b>\u0986\u09ae\u09be\u09a6\u09c7\u09b0 \u09b0\u09c7\u099f\u09bf\u0982 \u09a6\u09bf\u09a8!</b>\n"
            + "\u2501" * 22 + "\n"
            "PyHost Cloud \u0986\u09aa\u09a8\u09be\u09b0 \u0995\u09c7\u09ae\u09a8 \u09b2\u09be\u0997\u099b\u09c7? \u09a8\u09bf\u099a\u09c7 \u09a4\u09be\u09b0\u0995\u09be (Star) \u09b8\u0982\u0996\u09cd\u09af\u09be \u09ac\u09c7\u099b\u09c7 \u09a8\u09bf\u09a8:"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("feedback_rate_"):
        rating = int(data.replace("feedback_rate_", ""))
        star_str = "⭐" * rating
        user_states[user_id] = {"state": "awaiting_feedback_comment", "rating": rating}
        bot.answer_callback_query(call.id, f"{star_str} \u09a8\u09bf\u09b0\u09cd\u09ac\u09be\u099a\u09bf\u09a4!")
        bot.send_message(
            call.message.chat.id,
            f"{star_str}\n\n\u099a\u09be\u0987\u09b2\u09c7 \u098f\u0995\u099f\u09be \u09b8\u0982\u0995\u09cd\u09b7\u09bf\u09aa\u09cd\u09a4 \u09ae\u09a8\u09cd\u09a4\u09ac\u09cd\u09af \u09b2\u09bf\u0996\u09c7 \u09aa\u09be\u09a0\u09be\u09a8 (\u0990\u099a\u09cd\u099b\u09bf\u0995), \u0985\u09a5\u09ac\u09be <b>Skip</b> \u09b2\u09bf\u0996\u09c1\u09a8\u0964",
            parse_mode="HTML"
        )

    # ======================== Language Toggle ========================
    elif data == "toggle_language":
        current = admin.get_user_language(user_id)
        new_lang = "en" if current == "bn" else "bn"
        admin.set_user_language(user_id, new_lang)
        msg = "\u2705 Language set to English! (\u09ac\u09c7\u09b6\u09bf\u09b0\u09ad\u09be\u0997 \u09ae\u09c7\u09b8\u09c7\u099c \u098f\u0996\u09a8\u09cb \u09ac\u09be\u0982\u09b2\u09be\u09df \u09a5\u09be\u0995\u09ac\u09c7 \u2014 \u09ab\u09c1\u09b2 \u0985\u09a8\u09c1\u09ac\u09be\u09a6 \u09b6\u09c0\u0998\u09cd\u09b0\u0987 \u0986\u09b8\u099b\u09c7)" if new_lang == "en" else "\u2705 \u09ad\u09be\u09b7\u09be \u09ac\u09be\u0982\u09b2\u09be \u0995\u09b0\u09be \u09b9\u09af\u09bc\u09c7\u099b\u09c7!"
        bot.answer_callback_query(call.id, msg, show_alert=True)


    # --- ৩-টিয়ার প্যাকেজ ও চেকআউট ফ্লো ---
    elif data == "open_upgrade_menu":
        p_text, p_markup = get_upgrade_plan_view(user_id)
        bot.edit_message_text(p_text, call.message.chat.id, call.message.message_id, reply_markup=p_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("select_tier_"):
        tier = data.replace("select_tier_", "")
        sum_text, sum_markup = get_order_summary_view(tier, discount_percent=0)
        bot.edit_message_text(sum_text, call.message.chat.id, call.message.message_id, reply_markup=sum_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("apply_coupon_"):
        tier = data.replace("apply_coupon_", "")
        user_states[user_id] = {"state": "awaiting_coupon_code", "tier_key": tier}
        bot.send_message(call.message.chat.id, "🎁 <b>আপনার ডিসকাউন্ট কুপন কোডটি লিখুন:</b> (যেমন: <code>PYHOST20</code>)")
        bot.answer_callback_query(call.id)

    elif data.startswith("paygw_"):
        parts = data.split("_")
        tier = parts[1]
        gateway = parts[2]
        price_val = parts[3]
        price_str = f"{price_val} BDT"

        user_states[user_id] = {
            "state": "awaiting_vip_trx",
            "plan_tier": tier,
            "gateway": gateway.upper(),
            "price": price_str
        }

        if gateway == "bkash":
            gw_info = (
                "📱 <b>bKash Personal:</b> <code>01700000000</code>\n"
                "👉 <b>ধরণ:</b> Send Money (ট্যাপ করলেই কপি হবে)\n"
                f"💵 <b>প্রদেয় পরিমাণ:</b> <code>{price_str}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>টাকা পাঠানোর পর প্রাপ্ত Transaction ID (TrxID) টি নিচে লিখে পাঠান:</i>"
            )
        elif gateway == "nagad":
            gw_info = (
                "📱 <b>Nagad Personal:</b> <code>01700000000</code>\n"
                "👉 <b>ধরণ:</b> Send Money (ট্যাপ করলেই কপি হবে)\n"
                f"💵 <b>প্রদেয় পরিমাণ:</b> <code>{price_str}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>টাকা পাঠানোর পর প্রাপ্ত Transaction ID (TrxID) টি নিচে লিখে পাঠান:</i>"
            )
        else:
            gw_info = (
                "🪙 <b>Binance Pay ID:</b> <code>123456789</code>\n"
                "🌐 <b>USDT (TRC20):</b> <code>TYDzsxd8Vp4...EXAMPLE...TRC20</code>\n"
                f"💵 <b>প্রদেয় পরিমাণ:</b> <code>{price_str} (~${int(price_val)/120:.2f})</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>পেমেন্ট সম্পন্ন করার পর প্রাপ্ত Order ID / TxHash টি নিচে লিখে পাঠান:</i>"
            )

        pay_msg = (
            f"🧾 <b>PYHOST PAYMENT GATEWAY • {gateway.upper()}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>প্যাকেজ:</b> {admin.TIER_PLANS[tier]['name']}\n\n"
            f"{gw_info}"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("❌ Cancel Order", callback_data="cancel_vip_order", style="danger"))
        bot.send_message(call.message.chat.id, pay_msg, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "cancel_vip_order":
        if user_id in user_states:
            del user_states[user_id]
        bot.edit_message_text("❌ <b>পেমেন্ট অর্ডারটি বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Order cancelled")

    # --- অ্যাডমিন ১-ক্লিক ভিআইপি অ্যাপ্রুভাল ---
    elif data.startswith("adm_app_vip_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_app_vip_", ""))
        req = admin.get_vip_request(req_id)
        if req and req["status"] == "PENDING":
            target_uid = req["user_id"]
            days = req["days"]

            # ⭐ FIX: আগে শুধু plan_name স্ট্রিং-এ "Business" শব্দ খুঁজে assigned_plan ঠিক করা
            # হতো — এতে 2 মাস (BUSINESS) / 6 মাস (BUSINESS6M) / 12 মাস (YEARLY) তিনটা প্যাকেজই
            # একই "BUSINESS" ধরে নিয়ে একই (১০টি) বট-স্লট লিমিট পেত। এখন request-এ সেভ থাকা
            # আসল tier_key ব্যবহার করে সঠিক প্যাকেজ শনাক্ত করা হচ্ছে।
            tier_key = req.get("tier_key")
            plan_name_raw = req["plan_name"]

            if not tier_key:
                # পুরনো (মাইগ্রেশনের আগে তৈরি) রিকোয়েস্টের জন্য fallback — নাম মিলিয়ে অনুমান
                if "Pro" in plan_name_raw:
                    tier_key = "PRO"
                elif "Business Yearly" in plan_name_raw or "🏆" in plan_name_raw:
                    tier_key = "YEARLY"
                elif "6M" in plan_name_raw or "🔥" in plan_name_raw:
                    tier_key = "BUSINESS6M"
                elif "Business" in plan_name_raw:
                    tier_key = "BUSINESS"
                else:
                    tier_key = "PREMIUM"

            tier_key = tier_key.upper()
            if tier_key == "PRO":
                assigned_plan = "PRO"
            elif tier_key in ("BUSINESS", "BUSINESS6M", "YEARLY"):
                assigned_plan = "BUSINESS"
            else:
                assigned_plan = "PREMIUM"

            admin.set_user_plan(target_uid, assigned_plan, days, tier_key=tier_key)
            _award_first_upgrade_badge(target_uid)
            admin.update_vip_request_status(req_id, "APPROVED")
            
            bot.edit_message_text(
                f"{call.message.text}\n\n✅ <b>APPROVED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    target_uid,
                    f"🎉 <b>অভিনন্দন! আপনার পেমেন্ট ভেরিফাই হয়েছে।</b>\n"
                    f"💎 <b>{req['plan_name']}</b> সফলভাবে সক্রিয় করা হয়েছে! (24/7 Unlimited Run)",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    elif data.startswith("adm_rej_vip_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_rej_vip_", ""))
        req = admin.get_vip_request(req_id)
        if req and req["status"] == "PENDING":
            admin.update_vip_request_status(req_id, "REJECTED")
            bot.edit_message_text(
                f"{call.message.text}\n\n❌ <b>REJECTED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    req["user_id"],
                    "❌ <b>দুঃখিত! আপনার পেমেন্ট রিকোয়েস্টটি বাতিল করা হয়েছে।</b>\nসঠিক TrxID দিয়ে পুনরায় চেষ্টা করুন বা সাপোর্টে যোগাযোগ করুন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    elif data.startswith("adm_app_ext_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_app_ext_", ""))
        req = admin.get_renewal_request(req_id)
        if req and req["status"] == "PENDING":
            new_exp = admin.extend_project_expiry(req["project_id"], req["days"])
            admin.update_renewal_request_status(req_id, "APPROVED")
            bot.edit_message_text(
                f"{call.message.text}\n\n✅ <b>APPROVED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    req["user_id"],
                    f"🎉 <b>মেয়াদ বৃদ্ধি সফল হয়েছে!</b>\n🤖 <b>বট:</b> {req['project_name']}\n🗓️ <b>নতুন মেয়াদ:</b> <code>{new_exp}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    elif data.startswith("adm_rej_ext_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_rej_ext_", ""))
        req = admin.get_renewal_request(req_id)
        if req and req["status"] == "PENDING":
            admin.update_renewal_request_status(req_id, "REJECTED")
            bot.edit_message_text(
                f"{call.message.text}\n\n❌ <b>REJECTED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    req["user_id"],
                    "❌ <b>দুঃখিত! আপনার মেয়াদ বৃদ্ধি রিকোয়েস্টটি বাতিল করা হয়েছে।</b>\nসঠিক TrxID দিয়ে আবার চেষ্টা করুন বা সাপোর্টে যোগাযোগ করুন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    # --- অ্যাডমিন টেলিমেট্রি ও কন্ট্রোল ---
    elif data == "adm_back_menu" and admin.is_admin(user_id):
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "👑 <b>Admin Panel</b>\n👇 অ্যাডমিন মেনু:", reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "adm_refresh_telem" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes, footer=False)
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh Telemetry", callback_data="adm_refresh_telem", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        try:
            bot.edit_message_text(stats, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id, "Telemetry refreshed!")
        except Exception:
            bot.answer_callback_query(call.id, "Already refreshed!")

    elif data.startswith("adm_grant_pro_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_grant_pro_", ""))
        admin.set_user_plan(target_uid, "PRO", 7)
        _award_first_upgrade_badge(target_uid)
        bot.answer_callback_query(call.id, f"Pro Plan (7d) granted to {target_uid}!", show_alert=True)
        try:
            bot.send_message(target_uid, "🎉 <b>অভিনন্দন! অ্যাডমিন আপনার একাউন্ট Pro Plan (7 Days) প্ল্যানে আপগ্রেড করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass

    elif data.startswith("adm_grant_vip_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_grant_vip_", ""))
        admin.set_user_plan(target_uid, "PREMIUM", 30)
        _award_first_upgrade_badge(target_uid)
        bot.answer_callback_query(call.id, f"Premium Plan (30d) granted to {target_uid}!", show_alert=True)
        try:
            bot.send_message(target_uid, "🎉 <b>অভিনন্দন! অ্যাডমিন আপনার একাউন্ট Premium Plan (30 Days) প্ল্যানে আপগ্রেড করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass

    elif data.startswith("adm_ban_confirm_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_ban_confirm_", ""))
        u_info = admin.get_user(target_uid)
        will_ban = not bool(u_info.get("is_banned", 0))
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Confirm", callback_data=f"adm_toggle_ban_{target_uid}", style="danger"),
            make_inline_button("❌ Cancel", callback_data=f"adm_view_user_{target_uid}", style="primary")
        )
        action_label = "🚫 BAN" if will_ban else "✅ UNBAN"
        bot.send_message(
            call.message.chat.id,
            f"⚠️ <b>নিশ্চিত করুন!</b>\nইউজার <code>{target_uid}</code>-কে <b>{action_label}</b> করা হবে। চালিয়ে যেতে চান?",
            reply_markup=markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_toggle_ban_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_toggle_ban_", ""))
        u_info = admin.get_user(target_uid)
        new_status = not bool(u_info.get("is_banned", 0))
        admin.set_user_ban(target_uid, new_status)
        bot.answer_callback_query(call.id, f"User status updated: {'BANNED' if new_status else 'UNBANNED'}!", show_alert=True)
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("adm_dm_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_dm_", ""))
        user_states[user_id] = {"state": "awaiting_admin_dm", "target_uid": target_uid}
        bot.send_message(call.message.chat.id, f"📩 <b>ইউজার <code>{target_uid}</code>-কে মেসেজ লিখুন:</b>")
        bot.answer_callback_query(call.id)

    # --- অ্যাডমিন: পেজিনেটেড রিকোয়েস্ট ও ইউজার ডাইরেক্টরি নেভিগেশন ---
    elif data.startswith("adm_reqs_page_") and admin.is_admin(user_id):
        page = int(data.replace("adm_reqs_page_", ""))
        req_text, req_markup = build_pending_requests_view(page=page)
        try:
            bot.edit_message_text(req_text, call.message.chat.id, call.message.message_id, reply_markup=req_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — ইউজার সাইড কলব্যাকসমূহ
    elif data.startswith("my_tickets_page_"):
        page = int(data.replace("my_tickets_page_", ""))
        t_text, t_markup = build_user_tickets_view(user_id, page=page)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "new_ticket":
        user_states[user_id] = "awaiting_ticket_message"
        bot.send_message(
            call.message.chat.id,
            "📝 <b>নতুন টিকিট:</b>\nআপনার সমস্যাটি বিস্তারিত লিখে একটি মেসেজে পাঠান — আমাদের টিম শীঘ্রই রিপ্লাই দেবে।",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("ticket_view_"):
        tid = int(data.replace("ticket_view_", ""))
        ticket = admin.get_ticket(tid)
        if not ticket or ticket["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই টিকিটটি আপনার না!", show_alert=True)
            return
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=False)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("ticket_reply_"):
        tid = int(data.replace("ticket_reply_", ""))
        ticket = admin.get_ticket(tid)
        if not ticket or ticket["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই টিকিটটি আপনার না!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_ticket_reply", "ticket_id": tid}
        bot.send_message(call.message.chat.id, f"💬 <b>টিকিট #{tid}-এ আপনার রিপ্লাই লিখুন:</b>", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("ticket_close_"):
        tid = int(data.replace("ticket_close_", ""))
        ticket = admin.get_ticket(tid)
        if not ticket or ticket["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই টিকিটটি আপনার না!", show_alert=True)
            return
        admin.update_ticket_status(tid, "CLOSED")
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=False)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, "🔒 টিকিট বন্ধ করা হয়েছে।")

    # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — অ্যাডমিন সাইড কলব্যাকসমূহ
    elif data.startswith("adm_tickets_page_") and admin.is_admin(user_id):
        page = int(data.replace("adm_tickets_page_", ""))
        t_text, t_markup = build_admin_tickets_view(page=page)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_ticket_view_") and admin.is_admin(user_id):
        tid = int(data.replace("adm_ticket_view_", ""))
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=True)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_ticket_reply_") and admin.is_admin(user_id):
        tid = int(data.replace("adm_ticket_reply_", ""))
        user_states[user_id] = {"state": "awaiting_admin_ticket_reply", "ticket_id": tid}
        bot.send_message(call.message.chat.id, f"💬 <b>টিকিট #{tid}-এ রিপ্লাই লিখুন:</b>", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_ticket_close_") and admin.is_admin(user_id):
        tid = int(data.replace("adm_ticket_close_", ""))
        ticket = admin.get_ticket(tid)
        admin.update_ticket_status(tid, "CLOSED")
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=True)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        if ticket:
            try:
                bot.send_message(ticket["user_id"], f"🔒 <b>আপনার টিকিট #{tid} বন্ধ করা হয়েছে।</b>", parse_mode="HTML")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "🔒 টিকিট বন্ধ করা হয়েছে।")

    elif data.startswith("adm_users_page_") and admin.is_admin(user_id):
        rest = data.replace("adm_users_page_", "")
        page_str, _, query = rest.partition("_")
        page = int(page_str)
        query = "" if query == "-" else query
        dir_text, dir_markup = build_user_directory_view(page=page, query=query)
        try:
            bot.edit_message_text(dir_text, call.message.chat.id, call.message.message_id, reply_markup=dir_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_view_user_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_view_user_", ""))
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_plan_menu_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_plan_menu_", ""))
        text2 = f"🗂 <b>Manage Plan — User <code>{target_uid}</code></b>\n━━━━━━━━━━━━━━━━━━━━━━\n👇 নতুন প্ল্যান নির্বাচন করুন:"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🆓 Set FREE", callback_data=f"adm_setplan_{target_uid}_FREE_0", style="danger"))
        markup.add(make_inline_button("⚡ Pro — 7 Days", callback_data=f"adm_setplan_{target_uid}_PRO_7", style="primary"))
        markup.add(make_inline_button("⚡ Pro — 30 Days", callback_data=f"adm_setplan_{target_uid}_PRO_30", style="primary"))
        markup.add(make_inline_button("🌟 Premium — 30 Days", callback_data=f"adm_setplan_{target_uid}_PREMIUM_30", style="success"))
        markup.add(make_inline_button("🌟 Premium — 90 Days", callback_data=f"adm_setplan_{target_uid}_PREMIUM_90", style="success"))
        markup.add(make_inline_button("👑 Business — 30 Days", callback_data=f"adm_setplan_{target_uid}_BUSINESS_30", style="success"))
        markup.add(make_inline_button("👑 Business — 365 Days", callback_data=f"adm_setplan_{target_uid}_BUSINESS_365", style="success"))
        markup.add(make_inline_button("🔙 Back to Profile", callback_data=f"adm_view_user_{target_uid}", style="primary"))
        bot.edit_message_text(text2, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_setplan_") and admin.is_admin(user_id):
        parts = data.replace("adm_setplan_", "").split("_")
        target_uid = int(parts[0])
        plan_name = parts[1]
        days = int(parts[2])
        admin.set_user_plan(target_uid, plan_name, days)
        _award_first_upgrade_badge(target_uid)
        bot.answer_callback_query(call.id, f"✅ Plan updated to {plan_name} for {days} days!", show_alert=True)
        try:
            bot.send_message(target_uid, f"🎉 <b>অভিনন্দন! অ্যাডমিন আপনার একাউন্ট {plan_name} প্ল্যানে সেট করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("adm_userbots_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_userbots_", ""))
        projects = admin.get_user_projects(target_uid)
        if not projects:
            bot.answer_callback_query(call.id, "এই ইউজারের কোনো বট নেই!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for p in projects:
            pid = p["project_id"]
            p_key = f"{target_uid}_{pid}"
            is_run = p_key in active_processes and active_processes[p_key]["process"].poll() is None
            icon = "🟢" if is_run else "🔴"
            slot_lbl = p.get("slot_number") or "-"
            markup.add(make_inline_button(f"{icon} Slot {slot_lbl} — {p['project_name']}", callback_data=f"adm_bot_view_{target_uid}_{pid}", style="primary"))
        markup.add(make_inline_button("🔙 Back to Profile", callback_data=f"adm_view_user_{target_uid}", style="primary"))
        bot.edit_message_text(f"📦 <b>User <code>{target_uid}</code> এর সব বট:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_bot_view_") and admin.is_admin(user_id):
        parts = data.replace("adm_bot_view_", "").split("_")
        target_uid = int(parts[0])
        pid = int(parts[1])
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        owner_data = admin.get_user(target_uid)
        slot_status = admin.get_effective_slot_status(owner_data, project)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🗑️ Force Delete Bot", callback_data=f"adm_bot_del_confirm_{target_uid}_{pid}", style="danger"))
        markup.add(make_inline_button("🔙 Back to Bots List", callback_data=f"adm_userbots_{target_uid}", style="primary"))
        info = (
            f"🤖 <b>{project['project_name']}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Slot: <code>{project.get('slot_number') or '-'}</code>\n"
            f"📄 Main File: <code>{project['main_file']}</code>\n"
            f"📅 Created: <code>{project['created_at']}</code>\n"
            f"⏳ Status: <code>{slot_status['label']}</code>"
        )
        bot.edit_message_text(info, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_bot_del_confirm_") and admin.is_admin(user_id):
        parts = data.replace("adm_bot_del_confirm_", "").split("_")
        target_uid = int(parts[0])
        pid = int(parts[1])
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Yes, Delete", callback_data=f"adm_bot_del_{target_uid}_{pid}", style="danger"),
            make_inline_button("❌ Cancel", callback_data=f"adm_bot_view_{target_uid}_{pid}", style="primary")
        )
        bot.edit_message_text("⚠️ <b>নিশ্চিত করুন! এই বটটি স্থায়ীভাবে ডিলিট করা হবে।</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_bot_del_") and admin.is_admin(user_id):
        parts = data.replace("adm_bot_del_", "").split("_")
        target_uid = int(parts[0])
        pid = int(parts[1])
        stop_project_script(target_uid, pid)
        admin.delete_user_project(pid)
        bot.answer_callback_query(call.id, "বটটি ফোর্স ডিলিট করা হয়েছে!", show_alert=True)
        try:
            bot.send_message(target_uid, "⚠️ <b>অ্যাডমিন আপনার একটি বট ডিলিট করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("adm_auditlog_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_auditlog_", ""))
        logs = admin.get_user_audit_log(target_uid, limit=15)
        if not logs:
            bot.answer_callback_query(call.id, "কোনো অ্যাক্টিভিটি লগ পাওয়া যায়নি!", show_alert=True)
            return
        lines = [f"• <code>{l.get('timestamp', '')}</code> — <b>{l.get('action', '')}</b>\n  └ {l.get('details', '')}" for l in logs]
        text3 = f"📜 <b>Audit Log — User <code>{target_uid}</code></b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🔙 Back to Profile", callback_data=f"adm_view_user_{target_uid}", style="primary"))
        bot.edit_message_text(text3[:4000], call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- অ্যাডমিন: ঝুঁকিপূর্ণ অ্যাকশনের কনফার্মেশন ডায়ালগ ---
    elif data == "adm_coupon_create" and admin.is_admin(user_id):
        user_states[user_id] = {"state": "awaiting_new_coupon"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🎟️ <b>নতুন কুপন তৈরি করুন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "এই ফরম্যাটে পাঠান:\n"
            "<code>CODE DISCOUNT MAX_USES</code>\n\n"
            "উদাহরণ: <code>EID50 50 100</code>\n"
            "→ 'EID50' কোডে ৫০% ছাড়, সর্বোচ্চ ১০০ বার ব্যবহার\n\n"
            "⚠️ MAX_USES = 0 দিলে আনলিমিটেড।",
            parse_mode="HTML"
        )

    elif data == "adm_coupon_deactivate" and admin.is_admin(user_id):
        user_states[user_id] = {"state": "awaiting_deactivate_coupon"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "❌ <b>কুপন বাতিল করুন</b>\n"
            "বাতিল করতে চান এমন কুপনের CODE পাঠান।\n"
            "উদাহরণ: <code>EID50</code>",
            parse_mode="HTML"
        )

    elif data == "adm_refresh_analytics" and admin.is_admin(user_id):
        # Analytics refresh — same data, re-render
        all_users = admin.get_all_users()
        total = len(all_users)
        free_c = sum(1 for u in all_users if u.get("plan", "FREE") == "FREE")
        pro_c = sum(1 for u in all_users if u.get("plan") == "PRO")
        prem_c = sum(1 for u in all_users if u.get("plan") == "PREMIUM")
        biz_c = sum(1 for u in all_users if u.get("plan") in ("BUSINESS", "VIP"))
        paid_total = pro_c + prem_c + biz_c
        banned_c = sum(1 for u in all_users if u.get("is_banned") == 1)
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(1 for u in all_users if str(u.get("created_at", "")).startswith(today_str))
        active_bot_count = len([k for k, v in active_processes.items() if v["process"].poll() is None])
        conn = admin.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM projects"); total_bots = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vip_requests WHERE status = 'APPROVED'"); approved_vip = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM renewal_requests WHERE status = 'APPROVED'"); approved_ext = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupon_uses"); total_coupon_uses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupons WHERE is_active = 1"); active_coupons = cur.fetchone()[0]
        conn.close()
        conv_rate = f"{(paid_total/total*100):.1f}%" if total > 0 else "0%"
        dashboard = (
            "📈 <b>PyHost Analytics Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>ইউজার সামারি:</b>\n"
            f"├ 📊 মোট রেজিস্টার্ড: <b>{total}</b> জন\n"
            f"├ 🆕 আজকের নতুন: <b>{today_new}</b> জন\n"
            f"├ 💰 পেইড সদস্য: <b>{paid_total}</b> জন\n"
            f"├ 🆓 ফ্রি ইউজার: <b>{free_c}</b> জন\n"
            f"└ 🚫 ব্যান্ড: <b>{banned_c}</b> জন\n\n"
            "📦 <b>প্ল্যান বিভাজন:</b>\n"
            f"├ ⚡ Pro: <b>{pro_c}</b> জন\n"
            f"├ 🌟 Premium: <b>{prem_c}</b> জন\n"
            f"├ 👑 Business/VIP: <b>{biz_c}</b> জন\n"
            f"└ 📈 Conversion Rate: <b>{conv_rate}</b>\n\n"
            "🤖 <b>বট স্ট্যাটাস:</b>\n"
            f"├ 🟢 এখন চলছে: <b>{active_bot_count}</b> টি\n"
            f"└ 📦 মোট তৈরি: <b>{total_bots}</b> টি\n\n"
            "💰 <b>পেমেন্ট সামারি:</b>\n"
            f"├ ✅ অ্যাপ্রুভড প্ল্যান: <b>{approved_vip}</b>\n"
            f"└ ✅ অ্যাপ্রুভড রিনিউয়াল: <b>{approved_ext}</b>\n\n"
            "🎟️ <b>কুপন সামারি:</b>\n"
            f"├ 🟢 একটিভ কুপন: <b>{active_coupons}</b> টি\n"
            f"└ 📊 মোট ব্যবহার: <b>{total_coupon_uses}</b> বার\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <i>Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="adm_refresh_analytics", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        try:
            bot.edit_message_text(dashboard, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "✅ Refreshed!")

    elif data == "adm_cancel_action" and admin.is_admin(user_id):
        try:
            bot.edit_message_text("❌ <b>অ্যাকশনটি বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Cancelled")

    elif data == "adm_confirm_restart_all" and admin.is_admin(user_id):
        count = restart_all_active_bots()
        try:
            bot.edit_message_text(f"🔄 Successfully reloaded and restarted <b>{count}</b> active bot instances!", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, f"🔄 Successfully reloaded and restarted <b>{count}</b> active bot instances!", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "adm_confirm_cleanup" and admin.is_admin(user_id):
        kb = admin.execute_server_cleanup()
        try:
            bot.edit_message_text(f"🧹 Cleanup finished! <code>{kb} KB</code> of logs & cache purged.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, f"🧹 Cleanup finished! <code>{kb} KB</code> of logs & cache purged.", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("doc_page_"):
        idx = int(data.replace("doc_page_", ""))
        d_text, d_markup = build_doc_page(idx)
        try:
            bot.edit_message_text(d_text, call.message.chat.id, call.message.message_id, reply_markup=d_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif data == "doc_noop":
        bot.answer_callback_query(call.id)

    elif data == "adm_confirm_maintenance" and admin.is_admin(user_id):
        admin.SYSTEM_CONFIG["maintenance_mode"] = not admin.SYSTEM_CONFIG["maintenance_mode"]
        status = "🔴 ACTIVE (ON)" if admin.SYSTEM_CONFIG["maintenance_mode"] else "🟢 INACTIVE (OFF)"
        try:
            bot.edit_message_text(f"⚙️ Maintenance Mode is now: <b>{status}</b>.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, f"⚙️ Maintenance Mode is now: <b>{status}</b>.", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "adm_confirm_free_hosting" and admin.is_admin(user_id):
        admin.set_system_setting("free_hosting_enabled", not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True))
        status = "🟢 ON" if admin.SYSTEM_CONFIG["free_hosting_enabled"] else "🔴 OFF"
        result_text = f"🆓 ফ্রি হোস্টিং এখন: <b>{status}</b>।"
        if not admin.SYSTEM_CONFIG["free_hosting_enabled"]:
            stopped_n = lock_all_free_bots()
            result_text += f"\n🔒 <b>{stopped_n}</b>টি চলতে থাকা ফ্রি বট বন্ধ করে সব ফ্রি স্লট লক করা হয়েছে।"
        try:
            bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_vipu_set_") and admin.is_admin(user_id):
        try:
            hours = int(data.replace("adm_vipu_set_", ""))
        except ValueError:
            bot.answer_callback_query(call.id, "ভুল ভ্যালু!", show_alert=True)
            return
        until = admin.set_vip_unlock(hours)
        label = f"{hours // 24} দিন" if hours % 24 == 0 else f"{hours} ঘণ্টা"
        result_text = (
            "🔓 <b>VIP All Slot Unlock চালু হয়েছে!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ মেয়াদ: <b>{label}</b>\n"
        )
        result_text += (
            f"🕒 শেষ হবে: <code>{until.strftime('%d %b %Y • %H:%M')}</code>\n"
            f"প্রতি ইউজারের <b>{admin.get_vip_unlock_slots()}টি স্লট</b> (Slot 1–{admin.get_vip_unlock_slots()}) এখন আনলক ও 24/7।"
        )
        try:
            bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")
        bot.answer_callback_query(call.id, "✅ VIP Unlock চালু!")

    elif data.startswith("adm_vipu_slots_") and admin.is_admin(user_id):
        try:
            n = admin.set_vip_unlock_slots(int(data.replace("adm_vipu_slots_", "")))
        except ValueError:
            bot.answer_callback_query(call.id, "ভুল ভ্যালু!", show_alert=True)
            return
        cut = enforce_vip_slot_limits() if admin.is_vip_unlock_active() else 0
        panel_text, panel_markup = build_vip_unlock_panel()
        if cut:
            panel_text += f"\n\n🔒 সীমার বাইরের <b>{cut}</b>টি বট বন্ধ করা হয়েছে।"
        try:
            bot.edit_message_text(panel_text, call.message.chat.id, call.message.message_id, reply_markup=panel_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"✅ {n}টি স্লট সেট হয়েছে")

    elif data == "adm_vipu_custom" and admin.is_admin(user_id):
        user_states[user_id] = {"state": "awaiting_vip_unlock_custom"}
        bot.send_message(
            call.message.chat.id,
            "✏️ <b>কাস্টম VIP Unlock মেয়াদ</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "কত দিনের জন্য? শুধু সংখ্যা লিখে পাঠান (১ – ৩৬৫)।\n"
            "• দিন: <code>10</code> বা <code>10d</code>\n"
            "• ঘণ্টা: <code>36h</code>\n"
            "বাতিল করতে যেকোনো মেনু বাটন চাপুন।",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data == "adm_vipu_off" and admin.is_admin(user_id):
        stopped_n = end_vip_unlock()
        result_text = "🛑 <b>VIP All Slot Unlock বন্ধ করা হয়েছে।</b>"
        if stopped_n:
            result_text += f"\n🔒 স্লট-লিমিটের বাইরে চলা <b>{stopped_n}</b>টি বট বন্ধ করা হয়েছে।"
        try:
            bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")
        bot.answer_callback_query(call.id, "বন্ধ হয়েছে")

    elif data.startswith("adm_set_free_hours_") and admin.is_admin(user_id):
        hours = int(data.replace("adm_set_free_hours_", ""))
        admin.set_system_setting("free_hosting_hours", hours)
        result_text = f"🆓 ফ্রি হোস্টিং সেশনের মেয়াদ এখন: <b>{hours} Hours</b> per session। Control Panel-এও এখন থেকে এটাই দেখাবে।"
        try:
            bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")
        bot.answer_callback_query(call.id, f"সেট হয়েছে: {hours} Hours")

    # --- মার্কেটপ্লেস হ্যান্ডলারস ---
    elif data.startswith("mp_view_") and data[len("mp_view_"):] in MARKETPLACE_TEMPLATES:
        msg, markup = build_marketplace_card(data[len("mp_view_"):])
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "mp_view_github":
        u_data = admin.get_user(user_id)
        if not admin.is_vip_active(u_data):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"),
                make_inline_button("◀️ Back to Marketplace", callback_data="open_mp_menu", style="primary")
            )
            bot.edit_message_text(
                "🔒 <b>GitHub Deploy — VIP Exclusive Feature</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "এই ফিচারটি শুধুমাত্র পেইড প্ল্যান (Pro / Premium / Business / Business Yearly) ইউজারদের জন্য উন্মুক্ত।\n\n"
                "👉 GitHub থেকে সরাসরি রিপো ডিপ্লয় করতে প্ল্যান আপগ্রেড করুন।",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
            return
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(admin.get_user_projects(user_id)) >= max_bots:
            bot.answer_callback_query(call.id, "⚠️ স্লট লিমিট পূর্ণ! একটি বট ডিলিট করুন বা প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return
        if admin.get_next_free_slot(user_id) is None:
            bot.answer_callback_query(call.id, "⚠️ সবগুলো স্লট পূর্ণ! নতুন ডিপ্লয়ের আগে একটি ডিলিট করুন।", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_github_url"}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("◀️ Back to Marketplace", callback_data="open_mp_menu", style="primary"))
        bot.edit_message_text(
            "🐙 <b>Deploy from GitHub</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "আপনার GitHub রিপোর লিংক পাঠান।\n"
            "যেমন: <code>https://github.com/owner/repo</code>\n\n"
            "✅ requirements.txt থাকলে অটো-ইনস্টল হবে\n"
            "✅ main.py / bot.py অটো-ডিটেক্ট হয়ে রান হবে",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("gh_sync_"):
        pid = int(data.replace("gh_sync_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        gh_info = admin.get_project_github_info(pid)
        if not gh_info:
            bot.answer_callback_query(call.id, "এই বটের কোনো GitHub তথ্য নেই!", show_alert=True)
            return
        bot.answer_callback_query(call.id)

        # Private repo হলে নতুন token চাওয়া হবে, Public হলে সরাসরি sync
        if gh_info["is_private"]:
            user_states[user_id] = {"state": "awaiting_sync_token", "pid": pid}
            bot.edit_message_text(
                f"🔒 <b>Private Repo Sync — Token দরকার</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>Repo:</b> <code>{html.escape(gh_info['owner'])}/{html.escape(gh_info['repo'])}</code>\n\n"
                "আপনার GitHub Personal Access Token পাঠান।\n"
                "⚠️ <i>Token কোথাও সেভ হবে না — Sync শেষে ডিসকার্ড হবে। পাঠানো মেসেজও ডিলিট হবে।</i>",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
        else:
            status_msg = bot.edit_message_text(
                f"🔄 <b>GitHub Sync শুরু হচ্ছে...</b>\n"
                f"📦 <code>{html.escape(gh_info['owner'])}/{html.escape(gh_info['repo'])}</code>",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
            threading.Thread(
                target=sync_github_repo,
                args=(user_id, pid, call.message.chat.id, status_msg.message_id, None),
                daemon=True
            ).start()

    # ======================== Tutorial Callbacks ========================
    elif data in ("tut_step_2", "tut_step_3", "tut_step_4"):
        content, next_key = get_onboarding_step_content(data)
        markup = types.InlineKeyboardMarkup()
        if next_key and next_key != "tut_done":
            markup.add(make_inline_button("▶️ পরের ধাপ →", callback_data=next_key, style="success"))
        elif next_key == "tut_done":
            markup.add(make_inline_button("✅ শুরু করি!", callback_data="tut_done", style="success"))
        _prev = {"tut_step_3": "tut_step_2", "tut_step_4": "tut_step_3"}.get(data)
        if _prev:
            markup.add(make_inline_button("🔙 Back", callback_data=_prev, style="primary"))
        markup.add(make_inline_button("⏭️ Skip", callback_data="tut_skip", style="primary"))
        try:
            bot.edit_message_text(content, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, content, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "tut_done":
        admin.mark_onboarding_done(user_id)
        admin.set_checklist_flag(user_id, "tutorial_done")
        # ⭐ Tutorial শেষ হলে মেসেজটা চ্যাটে পড়ে না থেকে অটো-ভ্যানিশ (ডিলিট) হয়ে যাবে —
        # নিচের answer_callback_query টোস্টেই কনফার্মেশন দেখানো হচ্ছে, চ্যাটে বাড়তি লাইন থাকবে না।
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text(
                    "✅ <b>Tutorial সম্পন্ন!</b>\n🎉 এখন Control Panel থেকে প্রথম বট তৈরি করুন!",
                    call.message.chat.id, call.message.message_id, parse_mode="HTML"
                )
            except Exception:
                pass
        bot.answer_callback_query(call.id, "টিউটোরিয়াল শেষ! শুরু করুন 🚀")

    elif data == "tut_skip":
        admin.mark_onboarding_done(user_id)
        # ⭐ Skip করলেও মেসেজটা অটো-ভ্যানিশ (ডিলিট) হয়ে যাবে, চ্যাটে পড়ে থাকবে না
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text("⏭️ Tutorial skip করা হয়েছে। যেকোনো সময় Help থেকে দেখতে পারবেন।", call.message.chat.id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "Tutorial skip করা হয়েছে। Help থেকে যেকোনো সময় দেখতে পারবেন।")

    # ======================== Onboarding Checklist ========================
    elif data == "show_checklist":
        flags = admin.get_checklist_flags(user_id)
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        pinfo = admin.get_plan_display_info(u_data)
        has_bot = len(projects) > 0
        has_run = any(f"{user_id}_{p['project_id']}" in active_processes for p in projects)
        has_upgrade = pinfo["is_active"]
        has_refer = (u_data.get("referral_count") or 0) > 0
        def chk(done): return "✅" if done else "⬜"
        checklist = (
            "📋 <b>Onboarding Checklist</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{chk(flags.get('tutorial_done'))} Tutorial দেখুন\n"
            f"{chk(has_bot)} প্রথম বট তৈরি করুন\n"
            f"{chk(has_run)} বট রান করুন\n"
            f"{chk(has_upgrade)} প্ল্যান আপগ্রেড করুন (24/7)\n"
            f"{chk(has_refer)} ১ জন বন্ধুকে Refer করুন\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
        )
        done_count = sum([bool(flags.get("tutorial_done")), has_bot, has_run, has_upgrade, has_refer])
        checklist += f"🏆 সম্পন্ন: <b>{done_count}/5</b>"
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="show_checklist", style="primary"))
        markup.add(make_inline_button("🔙 Back to Home", callback_data="go_home", style="primary"))
        try:
            bot.edit_message_text(checklist, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, checklist, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Bot Health Check ========================
    elif data.startswith("bot_health_"):
        pid = int(data.replace("bot_health_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        p_key = f"{user_id}_{pid}"
        is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None

        # লগ থেকে এরর ডিটেক্ট
        errors = admin.scan_logs_for_errors(user_id, pid, PROJECTS_DIR)

        if is_running:
            proc_data = active_processes.get(p_key, {})
            proc = proc_data.get("process")
            try:
                mem = psutil.Process(proc.pid).memory_info().rss // (1024*1024)
            except Exception:
                mem = 0
            status_icon = "🟢"
            status_text = f"চলছে ✅ | RAM: {mem}MB"
        else:
            status_icon = "🔴"
            status_text = "বন্ধ"

        health_text = (
            f"🏥 <b>Bot Health Check — {html.escape(project['project_name'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_icon} <b>স্ট্যাটাস:</b> {status_text}\n"
            f"📄 <b>Main File:</b> <code>{project['main_file']}</code>\n"
            f"🔢 <b>Slot:</b> {project.get('slot_number','-')}\n"
            f"📅 <b>Created:</b> {project.get('created_at','-')[:10]}\n"
        )
        if errors:
            health_text += "\n⚠️ <b>পাওয়া সমস্যা:</b>\n" + "\n".join(f"• {e}" for e in errors)
        else:
            health_text += "\n✅ <b>কোনো সমস্যা পাওয়া যায়নি!</b>"
        gh_info = admin.get_project_github_info(pid)
        if gh_info:
            health_text += f"\n\n🐙 <b>GitHub:</b> <code>{html.escape(gh_info['owner'])}/{html.escape(gh_info['repo'])}</code>"
        notes = admin.get_bot_notes(pid)
        if notes:
            health_text += f"\n\n📝 <b>নোট:</b> {html.escape(notes)}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("🔄 Refresh", callback_data=f"bot_health_{pid}", style="primary"),
            make_inline_button("📝 নোট এডিট", callback_data=f"bot_edit_notes_{pid}", style="primary")
        )
        markup.add(make_inline_button("🔙 Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        try:
            bot.edit_message_text(health_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, health_text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_edit_notes_"):
        pid = int(data.replace("bot_edit_notes_", ""))
        user_states[user_id] = {"state": "awaiting_bot_notes", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📝 <b>বটের নোট লিখুন</b> (সর্বোচ্চ ৩০০ অক্ষর)\n"
            "যেমন: 'এটা আমার শপিং বট', 'admin: @username'\n"
            "বাতিল করতে /start দিন।",
            parse_mode="HTML"
        )

    # ======================== Quick Actions ========================
    # ======================== .env Manager ========================
    elif data.startswith("env_mgr_"):
        pid = int(data.replace("env_mgr_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        env_vars = admin.get_env_vars(pid)
        if env_vars:
            lines_text = []
            for ev in env_vars:
                val_masked = ev["value"][:2] + "*" * max(0, len(ev["value"]) - 4) + ev["value"][-2:] if len(ev["value"]) > 4 else "****"
                lines_text.append(f"🔑 <code>{ev['key']}</code> = <code>{val_masked}</code>")
            env_text = "\n".join(lines_text)
        else:
            env_text = "<i>কোনো env variable নেই। যোগ করুন।</i>"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("➕ Variable যোগ করুন", callback_data=f"env_add_{pid}", style="success"),
            make_inline_button("🗑️ Variable মুছুন", callback_data=f"env_del_{pid}", style="danger"),
            make_inline_button("📄 .env ফাইল দেখুন (Full)", callback_data=f"env_show_full_{pid}", style="primary"),
            make_inline_button("🔙 Back", callback_data=f"bot_more_tools_{pid}", style="primary")
        )
        try:
            bot.edit_message_text(
                f"🔧 <b>.env Manager — {html.escape(project['project_name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{env_text}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>Values মাস্ক করে দেখানো হচ্ছে। বট রান হওয়ার সময় .env ফাইলে লেখা হয়।</i>",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id,
                f"🔧 <b>.env Manager</b>\n{env_text}", reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Bot Clone ========================
    elif data.startswith("bot_clone_"):
        pid = int(data.replace("bot_clone_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "\u098f\u0987 \u09ac\u099f\u099f\u09bf \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be!", show_alert=True)
            return
        if admin.get_next_free_slot(user_id) is None:
            bot.answer_callback_query(call.id, "\u0995\u09cb\u09a8\u09cb \u0996\u09be\u09b2\u09bf \u09b8\u09cd\u09b2\u099f \u09a8\u09c7\u0987! \u09aa\u09cd\u09b0\u09a5\u09ae\u09c7 \u098f\u0995\u099f\u09be \u09ac\u099f \u09ae\u09c1\u099b\u09c1\u09a8 \u0985\u09a5\u09ac\u09be Upgrade Plan \u0995\u09b0\u09c1\u09a8\u0964", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_clone_name", "source_pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"\U0001F4cb <b>Clone Bot \u2014 {html.escape(project['project_name'])}</b>\n"
            "\u2501" * 22 + "\n"
            "\u09a8\u09a4\u09c1\u09a8 \u0995\u09cd\u09b2\u09cb\u09a8 \u0995\u09b0\u09be \u09ac\u099f\u099f\u09bf\u09b0 \u09a8\u09be\u09ae \u09b2\u09bf\u0996\u09c1\u09a8:\n"
            "\U0001F4c1 \u09b8\u09ac \u09ab\u09be\u0987\u09b2 \u0993 env variable \u0985\u099f\u09cb\u09ae\u09c7\u099f\u09bf\u0995 \u0995\u09aa\u09bf \u09b9\u09ac\u09c7\u0964",
            parse_mode="HTML"
        )

    # ======================== Bot Token Change ========================
    elif data.startswith("bot_token_"):
        pid = int(data.replace("bot_token_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "\u098f\u0987 \u09ac\u099f\u099f\u09bf \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_new_token", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"\U0001F511 <b>Change Bot Token \u2014 {html.escape(project['project_name'])}</b>\n"
            "\u2501" * 22 + "\n"
            "\u09a8\u09a4\u09c1\u09a8 Telegram Bot Token \u09aa\u09be\u09a0\u09be\u09a8:\n\n"
            "\u26a0\ufe0f \u099f\u09cb\u0995\u09c7\u09a8 \u09af\u09be\u099a\u09be\u0987 \u0995\u09b0\u09c7 BOT_TOKEN env variable \u0986\u09aa\u09a1\u09c7\u099f \u09b9\u09ac\u09c7 \u098f\u09ac\u0982 \u09ac\u099f\u099f\u09bf \u099a\u09b2\u09ae\u09be\u09a8 \u09a5\u09be\u0995\u09b2\u09c7 \u0985\u099f\u09cb-\u09b0\u09bf\u09b8\u09cd\u099f\u09be\u09b0\u09cd\u099f \u09b9\u09ac\u09c7\u0964\n"
            "\u09ae\u09c7\u09b8\u09c7\u099c\u099f\u09bf \u09aa\u09be\u09a0\u09be\u09a8\u09cb\u09b0 \u09aa\u09b0 \u09b8\u09be\u09a5\u09c7 \u09b8\u09be\u09a5\u09c7 \u09a1\u09bf\u09b2\u09bf\u099f \u09b9\u09df\u09c7 \u09af\u09be\u09ac\u09c7\u0964",
            parse_mode="HTML"
        )

    # ======================== Scheduled Start/Stop \u09ae\u09c7\u09a8\u09c1 ========================
    elif data.startswith("bot_schedule_"):
        pid = int(data.replace("bot_schedule_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "\u098f\u0987 \u09ac\u099f\u099f\u09bf \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be!", show_alert=True)
            return
        start_t = project.get("sched_start_time") or "\u09b8\u09c7\u099f \u0995\u09b0\u09be \u09a8\u09c7\u0987"
        stop_t = project.get("sched_stop_time") or "\u09b8\u09c7\u099f \u0995\u09b0\u09be \u09a8\u09c7\u0987"
        enabled = "\u2705 \u09b8\u099a\u09b2" if project.get("sched_enabled") else "\u274c \u09ac\u09a8\u09cd\u09a7"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("\u25b6\ufe0f Set Start Time", callback_data=f"sched_set_start_{pid}", style="success"),
            make_inline_button("\u23f9\ufe0f Set Stop Time", callback_data=f"sched_set_stop_{pid}", style="danger")
        )
        markup.add(make_inline_button("\U0001F515 Disable Schedule", callback_data=f"sched_disable_{pid}", style="primary"))
        markup.add(make_inline_button("\U0001F519 Back", callback_data=f"bot_more_tools_{pid}", style="primary"))
        text_sched = (
            f"\u23f0 <b>Scheduled Start/Stop \u2014 {html.escape(project['project_name'])}</b>\n"
            + "\u2501" * 22 + "\n"
            f"\u25b6\ufe0f <b>Start Time:</b> <code>{start_t}</code>\n"
            f"\u23f9\ufe0f <b>Stop Time:</b> <code>{stop_t}</code>\n"
            f"\U0001F4cc <b>Status:</b> {enabled}\n\n"
            "\u09b8\u09ae\u09df \u09b8\u09c7\u099f \u0995\u09b0\u09b2\u09c7 \u09aa\u09cd\u09b0\u09a4\u09bf\u09a6\u09bf\u09a8 \u0989\u0995\u09cd\u09a4 \u09b8\u09ae\u09df\u09c7 \u09ac\u099f\u099f\u09bf \u09b8\u09cd\u09ac\u09df\u0982\u0995\u09cd\u09b0\u09bf\u09df\u09ad\u09be\u09ac\u09c7 \u099a\u09be\u09b2\u09c1/\u09ac\u09a8\u09cd\u09a7 \u09b9\u09ac\u09c7 (Bangladesh Time)\u0964"
        )
        try:
            bot.edit_message_text(text_sched, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text_sched, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("sched_set_start_"):
        pid = int(data.replace("sched_set_start_", ""))
        user_states[user_id] = {"state": "awaiting_sched_start", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "\u25b6\ufe0f \u0995\u09a4 \u09b8\u09ae\u09df\u09c7 \u09ac\u099f\u099f\u09bf \u099a\u09be\u09b2\u09c1 \u09b9\u09ac\u09c7? HH:MM \u0986\u0995\u09be\u09b0\u09c7 \u09aa\u09be\u09a0\u09be\u09a8 (\u09af\u09c7\u09ae\u09a8 <code>08:00</code>):", parse_mode="HTML")

    elif data.startswith("sched_set_stop_"):
        pid = int(data.replace("sched_set_stop_", ""))
        user_states[user_id] = {"state": "awaiting_sched_stop", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "\u23f9\ufe0f \u0995\u09a4 \u09b8\u09ae\u09df\u09c7 \u09ac\u099f\u099f\u09bf \u09ac\u09a8\u09cd\u09a7 \u09b9\u09ac\u09c7? HH:MM \u0986\u0995\u09be\u09b0\u09c7 \u09aa\u09be\u09a0\u09be\u09a8 (\u09af\u09c7\u09ae\u09a8 <code>00:00</code>):", parse_mode="HTML")

    elif data.startswith("sched_disable_"):
        pid = int(data.replace("sched_disable_", ""))
        admin.clear_bot_schedule(pid)
        bot.answer_callback_query(call.id, "\u2705 Schedule \u09ac\u09a8\u09cd\u09a7 \u0995\u09b0\u09be \u09b9\u09df\u09c7\u099b\u09c7!", show_alert=True)

    elif data.startswith("env_add_"):
        pid = int(data.replace("env_add_", ""))
        user_states[user_id] = {"state": "awaiting_env_var", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🔧 <b>নতুন Env Variable যোগ করুন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "এই ফরম্যাটে পাঠান:\n"
            "<code>KEY=VALUE</code>\n\n"
            "উদাহরণ:\n"
            "<code>BOT_TOKEN=123456:ABC...</code>\n"
            "<code>ADMIN_ID=987654321</code>\n\n"
            "⚠️ মেসেজটি পাঠানোর পর সাথে সাথে ডিলিট হয়ে যাবে।",
            parse_mode="HTML"
        )

    elif data.startswith("env_del_"):
        pid = int(data.replace("env_del_", ""))
        env_vars = admin.get_env_vars(pid)
        if not env_vars:
            bot.answer_callback_query(call.id, "কোনো variable নেই!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_env_delete", "pid": pid}
        bot.answer_callback_query(call.id)
        keys_text = "\n".join(f"• <code>{ev['key']}</code>" for ev in env_vars)
        bot.send_message(
            call.message.chat.id,
            f"🗑️ <b>কোন KEY মুছবেন?</b>\nKey-এর নাম পাঠান:\n{keys_text}",
            parse_mode="HTML"
        )

    elif data.startswith("env_show_full_"):
        pid = int(data.replace("env_show_full_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        env_vars = admin.get_env_vars(pid)
        if not env_vars:
            bot.answer_callback_query(call.id, "কোনো env variable নেই!", show_alert=True)
            return
        lines_text = [f"{ev['key']}={ev['value']}" for ev in env_vars]
        env_content = "\n".join(lines_text)
        # ফাইল হিসেবে পাঠানো (নিরাপদ — শুধু ইউজার নিজে দেখবে)
        bio = io.BytesIO(env_content.encode("utf-8"))
        bio.name = ".env"
        bot.send_document(call.message.chat.id, bio, caption=f"🔧 <b>{html.escape(project['project_name'])}</b> — .env file", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Version History ========================
    elif data.startswith("ver_hist_"):
        pid = int(data.replace("ver_hist_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        history = admin.get_deploy_history(pid)
        if not history:
            bot.answer_callback_query(call.id, "এখনো কোনো ভার্সন হিস্ট্রি নেই!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for h in history[:10]:
            note = f" ({h['note']})" if h.get('note') and not h['note'].startswith('[') else ""
            label = f"v{h['version']} — {h['created_at'][:16]}{note}"
            markup.add(make_inline_button(f"↩️ {label}", callback_data=f"rollback_{pid}_{h['id']}", style="primary"))
        markup.add(make_inline_button("🔙 Back", callback_data=f"bot_more_tools_{pid}", style="primary"))
        try:
            bot.edit_message_text(
                f"📚 <b>Version History — {html.escape(project['project_name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "যেকোনো ভার্সনে Rollback করতে বাটন চাপুন:\n"
                "<i>(Rollback করার আগে বর্তমান ভার্সন অটো-সেভ হবে)</i>",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "📚 <b>Version History</b>", reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("rollback_"):
        parts = data.replace("rollback_", "").split("_")
        pid, hist_id = int(parts[0]), int(parts[1])
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Rollback হচ্ছে...")
        # চলমান বট বন্ধ
        p_key = f"{user_id}_{pid}"
        if p_key in active_processes:
            try:
                active_processes[p_key]["process"].terminate()
                active_processes[p_key]["log_handle"].close()
                del active_processes[p_key]
            except Exception:
                pass
        success, main_f_or_err = admin.restore_deploy_version(user_id, pid, hist_id)
        if success:
            ok_run, run_msg = execute_project_script(user_id, pid, main_f_or_err)
            rollback_line = ("🟢 বট আবার চালু হয়েছে।" if ok_run else f"⚠️ {run_msg}")

            try:
                bot.edit_message_text(
                    f"✅ <b>Rollback সফল হয়েছে!</b>\n"
                    f"📄 Main file: <code>{main_f_or_err}</code>\n"
                    + rollback_line,
                    call.message.chat.id, call.message.message_id, parse_mode="HTML"
                )
            except Exception:
                bot.send_message(call.message.chat.id, f"✅ Rollback সফল! বট রিস্টার্ট হয়েছে।")
        else:
            try:
                bot.edit_message_text(f"❌ <b>Rollback ব্যর্থ:</b> {html.escape(main_f_or_err)}", call.message.chat.id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass

    # ======================== Daily Report Settings ========================
    elif data == "daily_report_settings":
        u_data = admin.get_user(user_id)
        current_time = u_data.get("daily_report_time") or "বন্ধ"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("⏰ সময় সেট করুন", callback_data="daily_report_set", style="success"),
            make_inline_button("🔕 বন্ধ করুন", callback_data="daily_report_off", style="danger")
        )
        markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
        try:
            bot.edit_message_text(
                f"📊 <b>Daily Report Settings</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ <b>বর্তমান সময়:</b> <code>{current_time}</code> (Bangladesh Time)\n\n"
                "প্রতিদিন নির্দিষ্ট সময়ে আপনার বটের সারসংক্ষেপ পাবেন:\n"
                "• কতটা বট চলছে\n"
                "• কোনো ক্র্যাশ হয়েছে কিনা\n"
                "• মোট আপটাইম",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "📊 Daily Report Settings", reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "daily_report_set":
        user_states[user_id] = {"state": "awaiting_daily_report_time"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⏰ <b>Daily Report সময় সেট করুন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "HH:MM ফরম্যাটে সময় পাঠান (Bangladesh Time):\n"
            "উদাহরণ: <code>08:00</code> বা <code>20:30</code>",
            parse_mode="HTML"
        )

    elif data == "daily_report_off":
        admin.set_daily_report_time(user_id, None)
        bot.answer_callback_query(call.id, "✅ Daily Report বন্ধ করা হয়েছে।")
        try:
            bot.edit_message_text("🔕 <b>Daily Report বন্ধ করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass

    elif data == "adm_quick_stop_all" and admin.is_admin(user_id):
        stopped = 0
        for p_key in list(active_processes.keys()):
            try:
                uid_p, pid_p = p_key.split("_", 1)
                active_processes[p_key]["process"].terminate()
                active_processes[p_key]["log_handle"].close()
                del active_processes[p_key]
                admin.update_project_status(int(pid_p), "STOPPED")
                stopped += 1
            except Exception:
                pass
        bot.answer_callback_query(call.id, f"✅ {stopped}টি বট বন্ধ করা হয়েছে!")
        try:
            bot.edit_message_text(f"⏹️ <b>{stopped}টি বট বন্ধ করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass

    elif data == "gh_deploy_start":
        u_data = admin.get_user(user_id)
        if not admin.is_vip_active(u_data):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"))
            add_home_button(markup)
            lock_text = (
                "🔒 <b>GitHub Deploy — VIP Exclusive</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "এই ফিচারটি শুধুমাত্র পেইড প্ল্যান ইউজারদের জন্য।\n"
                "👉 ব্যবহার করতে নিচের বাটন থেকে যেকোনো পেইড প্ল্যান নিন।"
            )
            try:
                bot.edit_message_text(lock_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
            except Exception:
                bot.send_message(call.message.chat.id, lock_text, reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id)
            return
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(admin.get_user_projects(user_id)) >= max_bots:
            bot.answer_callback_query(call.id, "⚠️ স্লট লিমিট পূর্ণ! একটি বট ডিলিট করুন বা প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return
        if admin.get_next_free_slot(user_id) is None:
            bot.answer_callback_query(call.id, "⚠️ সবগুলো স্লট পূর্ণ! নতুন ডিপ্লয়ের আগে একটি ডিলিট করুন।", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_github_url"}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("❌ বাতিল করুন", callback_data="gh_cancel", style="danger"))
        prompt_text = (
            "🐙 <b>Deploy from GitHub</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "আপনার GitHub রিপোর লিংক পাঠান।\n"
            "যেমন: <code>https://github.com/owner/repo</code>\n\n"
            "✅ requirements.txt থাকলে অটো-ইনস্টল হবে\n"
            "✅ main.py / bot.py অটো-ডিটেক্ট হয়ে রান হবে"
        )
        try:
            bot.edit_message_text(prompt_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, prompt_text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "gh_cancel":
        if user_id in user_states:
            del user_states[user_id]
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে।")
        try:
            bot.edit_message_text(
                "❌ <b>GitHub Deploy বাতিল করা হয়েছে।</b>\n"
                "মূল মেনুতে ফিরে যেতে /start দিন।",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "❌ বাতিল করা হয়েছে।")

    elif data == "gh_repo_public":
        st = user_states.get(user_id)
        if not st or not isinstance(st, dict) or st.get("state") != "awaiting_github_visibility":
            bot.answer_callback_query(call.id, "সেশনের মেয়াদ শেষ, আবার শুরু করুন।", show_alert=True)
            return
        owner, repo = st["owner"], st["repo"]
        del user_states[user_id]
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏳ <b>GitHub থেকে '{html.escape(owner)}/{html.escape(repo)}' ডাউনলোড হচ্ছে...</b>",
            call.message.chat.id, call.message.message_id, parse_mode="HTML"
        )
        threading.Thread(
            target=deploy_github_repo,
            args=(user_id, call.message.chat.id, call.message.message_id, owner, repo, None, False),
            daemon=True
        ).start()

    elif data == "gh_repo_private":
        st = user_states.get(user_id)
        if not st or not isinstance(st, dict) or st.get("state") != "awaiting_github_visibility":
            bot.answer_callback_query(call.id, "সেশনের মেয়াদ শেষ, আবার শুরু করুন।", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_github_token", "owner": st["owner"], "repo": st["repo"]}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "🔒 <b>Private Repo — GitHub Token দিন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "আপনার GitHub Personal Access Token (PAT) পাঠান।\n\n"
            "⚠️ <i>টোকেনটি শুধু এই ডাউনলোডের জন্য একবারই ব্যবহার হবে — কোথাও সংরক্ষিত করা হয় না।"
            " পাঠানো মেসেজটাও নিরাপত্তার জন্য সাথে সাথে ডিলিট হয়ে যাবে।</i>",
            call.message.chat.id, call.message.message_id, parse_mode="HTML"
        )

    # --- 🌐 কমিউনিটি মার্কেটপ্লেস (ইউজার সাইড) ---
    elif data.startswith("cm_list_") and data[len("cm_list_"):].isdigit():
        c_text, c_markup = build_community_list(int(data[len("cm_list_"):]))
        _edit_or_send(call, c_text, c_markup)
        bot.answer_callback_query(call.id)

    elif data.startswith("cm_view_") and data[len("cm_view_"):].isdigit():
        c_text, c_markup = build_community_card(int(data[len("cm_view_"):]))
        if not c_text:
            bot.answer_callback_query(call.id, "এই বটটি আর পাওয়া যাচ্ছে না।", show_alert=True)
        else:
            _edit_or_send(call, c_text, c_markup)
            bot.answer_callback_query(call.id)

    elif data.startswith("cm_deploy_") and data[len("cm_deploy_"):].isdigit():
        tid = int(data[len("cm_deploy_"):])
        t = admin.get_community_template(tid)
        if not t or t["status"] != "APPROVED":
            bot.answer_callback_query(call.id, "এই বটটি আর পাওয়া যাচ্ছে না।", show_alert=True)
        else:
            user_states[user_id] = {"state": "awaiting_deploy_token", "template": f"cm_{tid}"}
            bot.send_message(
                call.message.chat.id,
                "🤖 <b>আপনার বটের টোকেন দিন:</b>\n\n"
                "👉 @BotFather থেকে পাওয়া <code>BOT_TOKEN</code> টি মেসেজে সেন্ড করুন:",
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)

    elif data == "cm_submit":
        user_states.pop(user_id, None)
        c_text, c_markup = build_community_submit_info(user_id)
        _edit_or_send(call, c_text, c_markup)
        bot.answer_callback_query(call.id)

    elif data == "cm_mine":
        c_text, c_markup = build_my_submissions(user_id)
        _edit_or_send(call, c_text, c_markup)
        bot.answer_callback_query(call.id)

    elif data == "cm_submit_go":
        if admin.count_user_pending_templates(user_id) >= admin.COMMUNITY_MAX_PENDING_PER_USER:
            bot.answer_callback_query(call.id, f"আপনার {admin.COMMUNITY_MAX_PENDING_PER_USER}টি সাবমিশন রিভিউয়ের অপেক্ষায় আছে।", show_alert=True)
        else:
            user_states[user_id] = {"state": "awaiting_cm_name"}
            bot.send_message(
                call.message.chat.id,
                "✍️ <b>ধাপ ১/৩ — বটের নাম</b>\nএকটি ছোট নাম লিখুন (৩–৪০ অক্ষর)। উদাহরণ: <code>Giveaway Manager</code>\n\n"
                "বাতিল করতে যেকোনো মেনু বাটন চাপুন।",
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)

    # --- 🌐 কমিউনিটি মার্কেটপ্লেস (অ্যাডমিন রিভিউ) ---
    elif data == "adm_ghb_status" and admin.is_admin(user_id):
        g_text, g_markup = build_gh_backup_panel()
        _edit_or_send(call, g_text, g_markup)
        bot.answer_callback_query(call.id, "✅ Refreshed")

    elif data.startswith("dr_force_") and data[len("dr_force_"):].isdigit():
        f_pid = int(data[len("dr_force_"):])
        f_proj = admin.get_project_by_id(f_pid)
        f_main = _force_run_ctx.pop((user_id, f_pid), None)
        if not f_proj or f_proj["user_id"] != user_id:
            bot.answer_callback_query(call.id, "প্রজেক্ট পাওয়া যায়নি।", show_alert=True)
        elif not f_main:
            bot.answer_callback_query(call.id, "সেশন শেষ — ফাইলটি আবার পাঠান।", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "বট চালু করা হচ্ছে...")
            f_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{f_pid}")
            try:
                f_det = scan_python_imports(os.path.join(f_dir, f_main))
                if f_det:
                    install_missing_packages_sync(f_det)
            except Exception:
                pass
            f_ok, f_msg = execute_project_script(user_id, f_pid, f_main)
            if not f_ok:
                bot.send_message(call.message.chat.id, f_msg, reply_markup=_back_cp_markup(), parse_mode="HTML")
            else:
                f_card, f_mk = build_deploy_success_card(f_pid, f_main, verify_token=False)
                try:
                    bot.edit_message_text(f_card, call.message.chat.id, call.message.message_id, reply_markup=f_mk, parse_mode="HTML")
                except Exception:
                    bot.send_message(call.message.chat.id, f_card, reply_markup=f_mk, parse_mode="HTML")

    elif data == "adm_chc_panel" and admin.is_admin(user_id):
        c_text, c_markup = build_channel_clean_panel()
        _edit_or_send(call, c_text, c_markup)
        bot.answer_callback_query(call.id)

    elif data in ("adm_chc_ask_old", "adm_chc_ask_reset") and admin.is_admin(user_id):
        kind = data.replace("adm_chc_ask_", "")
        if kind == "old":
            t = "⚠️ <b>নিশ্চিত করুন</b>\nচ্যানেলের পুরনো DB-ব্যাকআপ জেনারেশনগুলো মোছা হবে। বর্তমান PIN করা ব্যাকআপ ও ইউজারদের কোড-কপি অক্ষত থাকবে।"
        else:
            t = ("⚠️ <b>DB-ব্যাকআপ রিসেট</b>\nচ্যানেলের সব DB-ব্যাকআপ (বর্তমানটাসহ) মুছে এখনকার ডাটাবেস থেকে নতুন ব্যাকআপ তৈরি হবে। "
                 "ইউজারদের কোড-কপি মোছা হবে না।\n⛔ নতুন ব্যাকআপ ব্যর্থ হলে সার্ভার রিসেটে ডাটা ফেরত আসবে না — তাই সার্ভার ঠিকমতো চললে তবেই করুন।")
        mk = types.InlineKeyboardMarkup(row_width=2)
        mk.add(make_inline_button("🗑 হ্যাঁ", callback_data=f"adm_chc_do_{kind}", style="danger"),
               make_inline_button("❌ বাতিল", callback_data="adm_chc_panel", style="primary"))
        _edit_or_send(call, t, mk)
        bot.answer_callback_query(call.id)

    elif data in ("adm_chc_do_old", "adm_chc_do_reset") and admin.is_admin(user_id):
        if _chc_lock.locked():
            bot.answer_callback_query(call.id, "⏳ আরেকটি ক্লিন চলছে", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "🧹 শুরু হচ্ছে...")
            _edit_or_send(call, "🧹 <b>পরিষ্কার চলছে...</b>", None)
            threading.Thread(target=_run_channel_clean,
                             args=(call.message.chat.id, call.message.message_id, data.replace("adm_chc_do_", "")),
                             daemon=True).start()

    elif data == "adm_chb_now" and admin.is_admin(user_id):
        bot.answer_callback_query(call.id, "চ্যানেল ব্যাকআপ শুরু হচ্ছে...")
        _chat_id, _msg_id = call.message.chat.id, call.message.message_id
        def _run_ch_backup():
            ok, info = channel_backup.push(bot, DB_CHANNEL_ID)
            g_text, g_markup = build_gh_backup_panel()
            head = ("✅ <b>চ্যানেল ব্যাকআপ সম্পন্ন:</b> " if ok else "❌ <b>ব্যর্থ:</b> ") + html.escape(info) + "\n\n"
            try:
                bot.edit_message_text(head + g_text, _chat_id, _msg_id, reply_markup=g_markup, parse_mode="HTML")
            except Exception:
                bot.send_message(_chat_id, head + g_text, reply_markup=g_markup, parse_mode="HTML")
        threading.Thread(target=_run_ch_backup, daemon=True).start()

    elif data == "adm_cm_panel" and admin.is_admin(user_id):
        c_text, c_markup = build_admin_community_panel()
        _edit_or_send(call, c_text, c_markup)
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_cm_view_") and data[len("adm_cm_view_"):].isdigit() and admin.is_admin(user_id):
        tid = int(data[len("adm_cm_view_"):])
        t = admin.get_community_template(tid)
        if not t:
            bot.answer_callback_query(call.id, "পাওয়া যায়নি।", show_alert=True)
        else:
            flags = admin.scan_community_code(t["code"])
            info = (
                f"🔍 <b>Template #{t['id']} — {html.escape(t['name'])}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 @{html.escape(t['creator_name'] or str(t['creator_id']))} (<code>{t['creator_id']}</code>)\n"
                f"📌 Status: <b>{t['status']}</b>\n"
                f"📝 {html.escape(t['description'] or '')}\n"
                f"📏 Size: {len(t['code'].encode('utf-8')) / 1024:.1f} KB\n"
                f"🛡️ স্ক্যান: {'⚠️ ' + html.escape(', '.join(flags)) if flags else '✅ কোনো সন্দেহজনক প্যাটার্ন নেই'}\n\n"
                "<i>নিচের ফাইলটি পড়ে তারপর সিদ্ধান্ত নিন।</i>"
            )
            m = types.InlineKeyboardMarkup(row_width=2)
            if t["status"] == "PENDING":
                m.add(
                    make_inline_button("✅ Approve", callback_data=f"adm_cm_ok_{tid}", style="success"),
                    make_inline_button("❌ Reject", callback_data=f"adm_cm_no_{tid}", style="danger")
                )
            elif t["status"] == "APPROVED":
                m.add(make_inline_button("🗑️ Remove from Marketplace", callback_data=f"adm_cm_no_{tid}", style="danger"))
            m.add(make_inline_button("◀️ Community Review", callback_data="adm_cm_panel", style="primary"))
            bot.send_message(call.message.chat.id, info, reply_markup=m, parse_mode="HTML")
            try:
                buf = io.BytesIO(t["code"].encode("utf-8"))
                buf.name = f"community_{tid}.py"
                bot.send_document(call.message.chat.id, buf, caption=f"💾 Template #{tid} source")
            except Exception:
                pass
            bot.answer_callback_query(call.id)

    elif data.startswith("adm_cm_ok_") and data[len("adm_cm_ok_"):].isdigit() and admin.is_admin(user_id):
        tid = int(data[len("adm_cm_ok_"):])
        t = admin.review_community_template(tid, True)
        if not t:
            bot.answer_callback_query(call.id, "পাওয়া যায়নি।", show_alert=True)
        else:
            try:
                bot.edit_message_text(f"✅ <b>Template #{tid} অ্যাপ্রুভ হয়েছে</b> — এখন Marketplace-এ লাইভ।", call.message.chat.id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass
            try:
                bot.send_message(
                    t["creator_id"],
                    f"🎉 <b>আপনার বট অ্যাপ্রুভ হয়েছে!</b>\n🧩 {html.escape(t['name'])}\n"
                    f"এখন সবাই এটি ডেপ্লয় করতে পারবে — প্রতিটি ইউনিক ডেপ্লয়ে আপনি পাবেন {admin.SYSTEM_CONFIG.get('community_commission', 1.0):g} Credits। 💰",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id, "✅ Approved")

    elif data.startswith("adm_cm_no_") and data[len("adm_cm_no_"):].isdigit() and admin.is_admin(user_id):
        tid = int(data[len("adm_cm_no_"):])
        t = admin.get_community_template(tid)
        if not t:
            bot.answer_callback_query(call.id, "পাওয়া যায়নি।", show_alert=True)
        else:
            user_states[user_id] = {"state": "awaiting_cm_reject_reason", "tid": tid}
            bot.send_message(
                call.message.chat.id,
                f"❌ <b>Template #{tid} রিজেক্ট/রিমুভ</b>\nকারণ লিখে পাঠান (ইউজার দেখতে পাবে)। কারণ ছাড়া করতে <code>-</code> লিখুন।",
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)

    elif data == "adm_cm_live" and admin.is_admin(user_id):
        rows, total = admin.list_community_templates("APPROVED", 20, 0)
        m = types.InlineKeyboardMarkup(row_width=1)
        for r in rows:
            m.add(make_inline_button(f"🔍 #{r['id']} {r['name'][:24]} • 🚀{r['deploys']}", callback_data=f"adm_cm_view_{r['id']}", style="primary"))
        m.add(make_inline_button("◀️ Community Review", callback_data="adm_cm_panel", style="primary"))
        _edit_or_send(call, f"📋 <b>লাইভ কমিউনিটি টেমপ্লেট ({total})</b>\nসিলেক্ট করে রিভিউ/রিমুভ করুন।", m)
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_cm_comm_") and data[len("adm_cm_comm_"):].isdigit() and admin.is_admin(user_id):
        val = int(data[len("adm_cm_comm_"):]) / 10.0
        admin.set_system_setting("community_commission", val)
        c_text, c_markup = build_admin_community_panel()
        _edit_or_send(call, c_text, c_markup)
        bot.answer_callback_query(call.id, f"কমিশন: {val:g} Credits")

    elif data == "open_mp_menu":
        bot.edit_message_text(MARKETPLACE_INTRO, call.message.chat.id, call.message.message_id, reply_markup=build_marketplace_markup(), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ⭐ FIX: এই ব্লকের "elif data.startswith("deploy_tpl_")" লাইনটি হারিয়ে গিয়ে কোডটি ভুলে open_mp_menu-এর
    # ভেতরে ঢুকে গিয়েছিল — ফলে Marketplace খুললেই টোকেন-ইনপুট মোড চালু হয়ে যেত আর "🚀 Deploy করুন"
    # বাটন কাজই করত না। এখন আলাদা ব্লকে ঠিক করা হয়েছে।
    elif data.startswith("deploy_tpl_"):
        tpl = data.replace("deploy_tpl_", "")
        user_states[user_id] = {"state": "awaiting_deploy_token", "template": tpl}
        bot.send_message(
            call.message.chat.id,
            "🤖 <b>আপনার বটের টোকেন দিন:</b>\n\n"
            "👉 @BotFather থেকে পাওয়া <code>BOT_TOKEN</code> টি মেসেজে সেন্ড করুন:"
        )
        bot.answer_callback_query(call.id)


    # 💼 Dev Studio (⭐ FIX: বাটনটির কোনো কলব্যাক হ্যান্ডলারই ছিল না, তাই কিছুই হতো না)
    elif data == "sup_devstudio":
        ds_text, ds_markup = get_dev_studio_card()
        try:
            bot.edit_message_text(ds_text, call.message.chat.id, call.message.message_id, reply_markup=ds_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, ds_text, reply_markup=ds_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "devstudio_request":
        user_states[user_id] = {"state": "awaiting_devstudio_request"}
        cancel_markup = types.InlineKeyboardMarkup()
        cancel_markup.add(make_inline_button("❌ বাতিল", callback_data="devstudio_cancel", style="danger"))
        bot.send_message(
            call.message.chat.id,
            "📝 <b>আপনার রিকোয়ারমেন্ট লিখুন</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "একটি মেসেজে লিখে পাঠান:\n"
            "1️⃣ বটের ধরন\n2️⃣ কী কী ফিচার চান\n3️⃣ বাজেট\n4️⃣ ডেডলাইন\n\n"
            "💡 যত বিস্তারিত লিখবেন, তত দ্রুত ও নির্ভুল কোটেশন পাবেন।",
            reply_markup=cancel_markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data == "devstudio_cancel":
        user_states.pop(user_id, None)
        try:
            bot.edit_message_text("❌ <b>Dev Studio রিকোয়েস্ট বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে")

    # 🎁 ইউজার সাইড রিডিম
    elif data == "user_redeem_start":
        send_redeem_prompt(call.message.chat.id, user_id)
        bot.answer_callback_query(call.id)

    elif data == "user_redeem_cancel":
        user_states.pop(user_id, None)
        try:
            bot.edit_message_text("❌ <b>রিডিম বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে")

    # 🎁 অ্যাডমিন সাইড Redeem Code ম্যানেজার (rc_*)
    elif data.startswith("rc_"):
        if not admin.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ শুধু অ্যাডমিনদের জন্য।", show_alert=True)
            return
        _handle_redeem_admin_callback(call)

    elif data == "sup_faq":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("❓ বট চলছে না কেন?", callback_data="faq_bot_not_running", style="primary"),
            make_inline_button("🔑 টোকেন কোথায় পাবো?", callback_data="faq_get_token", style="primary"),
            make_inline_button("📦 requirements.txt কী?", callback_data="faq_requirements", style="primary"),
            make_inline_button("💎 প্ল্যান না কিনলে কী হয়?", callback_data="faq_free_plan", style="primary"),
            make_inline_button("🐙 GitHub Deploy কীভাবে?", callback_data="faq_github", style="primary"),
            make_inline_button("🔙 Back", callback_data="back_to_support", style="primary")
        )
        bot.edit_message_text(
            "❓ <b>FAQ — সাধারণ প্রশ্নোত্তর</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "নিচের যেকোনো প্রশ্নে ট্যাপ করুন:",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )

    elif data in ("faq_bot_not_running","faq_get_token","faq_requirements","faq_free_plan","faq_github"):
        answers = {
            "faq_bot_not_running": (
                "❓ <b>বট চলছে না কেন?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "১. বট টোকেন ভুল আছে কিনা চেক করুন (@BotFather → /mybots)\n"
                "২. কোডে SyntaxError আছে কিনা চেক করুন (View Logs)\n"
                "৩. requirements.txt-এ সব লাইব্রেরি আছে কিনা দেখুন\n"
                "৪. 🏥 Health Check বাটন চাপুন — স্বয়ংক্রিয়ভাবে সমস্যা ধরবে\n"
                f"৫. Free প্ল্যানে বট দিনে {to_bn_num(admin.SYSTEM_CONFIG.get('free_hosting_hours', 12))} ঘণ্টার বেশি চলে না"
            ),
            "faq_get_token": (
                "🔑 <b>বট টোকেন কোথায় পাবেন?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "① Telegram-এ @BotFather-কে মেসেজ দিন\n"
                "② /newbot কমান্ড দিন\n"
                "③ বটের নাম ও username দিন (username শেষে Bot থাকতে হবে)\n"
                "④ টোকেন পাবেন — এটা কপি করে PyHost-এ দিন\n\n"
                "⚠️ টোকেন কখনো শেয়ার করবেন না!"
            ),
            "faq_requirements": (
                "📦 <b>requirements.txt কী?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "আপনার Python বট যে লাইব্রেরিগুলো ব্যবহার করে, সেগুলোর লিস্ট।\n\n"
                "উদাহরণ:\n<code>pyTelegramBotAPI\nrequests\npython-dotenv</code>\n\n"
                "এই ফাইল থাকলে PyHost স্বয়ংক্রিয়ভাবে সব ইনস্টল করে নেয়।"
            ),
            "faq_free_plan": (
                "💎 <b>Free প্ল্যানে কী পাবেন?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ ২টি বট স্লট | ✅ দিনে {to_bn_num(admin.SYSTEM_CONFIG.get('free_hosting_hours', 12))} ঘণ্টা | ✅ ফাইল আপলোড\n"
                "❌ 24/7 চলবে না | ❌ GitHub Deploy নেই\n\n"
                "💡 ৩ জনকে Refer করলে ৩ দিন ফ্রি VIP!"
            ),
            "faq_github": (
                "🐙 <b>GitHub Deploy কীভাবে?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "① পেইড প্ল্যান থাকতে হবে\n"
                "② 🐙 GitHub Deploy বাটন চাপুন\n"
                "③ রিপো লিংক দিন: https://github.com/owner/repo\n"
                "④ Public/Private বেছে নিন → বট ডিপ্লয়!\n"
                "🔄 আপডেট হলে → Bot Dashboard → GitHub Sync"
            ),
        }
        answer = answers.get(data, "উত্তর পাওয়া যায়নি।")
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔙 FAQ তালিকায় ফিরুন", callback_data="sup_faq", style="primary"))
        try:
            bot.edit_message_text(answer, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, answer, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "back_to_support":
        msg = "🛠️ <b>Support Centre</b>\n━━━━━━━━━━━━━━━━━━\nযেকোনো সহায়তার জন্য বা কাস্টম বট বানানোর জন্য নিচের অপশনগুলো বেছে নিন:"
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=get_support_hub_markup(), parse_mode="HTML")

# ----------------- রিকানেকশন ও পোলিং লুপ -----------------
if __name__ == "__main__":
    print("PyHost Cloud 24/7 Multi-Bot Engine successfully initiated...")
    
    try:
        bot.remove_webhook()
        bot.delete_my_commands()
    except Exception:
        pass

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print("⚠️ Warning: Conflict Error (409)! Another instance is active. Retrying in 5s...")
                time.sleep(5)
            else:
                time.sleep(3)
        except Exception:
            time.sleep(3)