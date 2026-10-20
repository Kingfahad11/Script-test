"""
📡 Channel Backup — আপনার প্রাইভেট DB-চ্যানেলে ডাটাবেসের স্ন্যাপশট রাখা ও সার্ভার রিসেটের পর সেখান থেকেই ফিরিয়ে আনা

কীভাবে কাজ করে:
  • ডাটাবেসের (ছোট) কম্প্রেসড কপি চ্যানেলে একটা ডকুমেন্ট মেসেজ হিসেবে থাকে এবং সেটা PIN করা থাকে।
    (বট চ্যানেলের পুরনো মেসেজ পড়তে পারে না — কিন্তু getChat দিয়ে PIN করা মেসেজটা পায়। তাই ঠিক ১টা মেসেজ, এডিট হয়ে আপডেট হয়।)
  • DB বদলালে প্রতি ~২ মিনিটে, আর কোড আপলোডের ~৩০ সেকেন্ড পরে স্ন্যাপশট আপডেট হয়।
  • রিসেটের পর লোকাল DB না থাকলে/ফাঁকা থাকলে চালুর সময় এই PIN করা মেসেজ থেকে DB নামিয়ে বসায়।
  • এরপর main.py ইউজারদের কোড-কপি (চ্যানেলে যে ফাইলগুলো আগে থেকেই যায়) নামিয়ে প্রতিটি বটের ফোল্ডারে ফিরিয়ে দেয়।

দরকার: বট চ্যানেলের Admin হতে হবে — "Post Messages", "Edit Messages" ও "Pin Messages" পারমিশনসহ।
সতর্কতা: ওই চ্যানেলে অন্য কোনো মেসেজ PIN করবেন না (রিস্টোর PIN করা মেসেজ দিয়েই ব্যাকআপ খোঁজে)।
সীমা: বট সর্বোচ্চ ২০ MB ফাইল নামাতে পারে — তাই ব্যাকআপ ~১৮ MB-র কাছে গেলে নিজে থেকেই ভাগ হয়ে নতুন ফাইল পিন হয় (ম্যানিফেস্ট-কার্ড), আগের ফাইলগুলো চ্যানেলে থেকে যায়।
"""
import os
import io
import json
import time
import zipfile
import sqlite3
import tempfile
import threading
import shutil
import urllib.request
import urllib.parse
import urllib.error
import re
import random
import hashlib
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pyhost_system.db")

def _db_counts(path):
    try:
        con = sqlite3.connect(path, timeout=10)
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        try:
            projects = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        except Exception:
            projects = 0
        con.close()
        return users, projects
    except Exception:
        return None, None

def _local_needs_restore() -> bool:
    """লোকাল DB না থাকলে বা ইউজার ০ হলে রিস্টোর লাগবে (FORCE_RESTORE=1 হলে সবসময়)।"""
    if os.environ.get("FORCE_RESTORE", "").strip() in ("1", "true", "yes"):
        return True
    if not os.path.exists(DB_PATH):
        return True
    users, _ = _db_counts(DB_PATH)
    return not users

MARKER = "PYHOST_DB_BACKUP"
FILE_NAME = "pyhost_db_backup.zip"
MANIFEST_NAME = "pyhost_db_backup.manifest.json"
# ⭐ বট ২০ MB-র বেশি ফাইল নামাতে পারে না। ব্যাকআপ (কম্প্রেসড) PART_BYTES-এর কাছে গেলে একটা ফাইলে না ধরিয়ে ভাগ করে নতুন ফাইল/পার্ট হিসেবে পাঠানো হয়
# এবং সেটাই নতুন করে PIN হয়; আগের ফাইলগুলো চ্যানেলে থেকে যায় (কতগুলো থাকবে — CH_BACKUP_KEEP, ডিফল্ট ২ জেনারেশন, ০ = সব)।
PART_BYTES = 18 * 1024 * 1024
MAX_PARTS = 30
MULTI_MIN_INTERVAL = 1800    # মাল্টি-ফাইল মোডে (বড় আপলোড) দুই পুশের মধ্যে ন্যূনতম বিরতি (সেকেন্ড)
CHECK_EVERY = 120            # সেকেন্ড: DB বদলেছে কিনা দেখার ব্যবধান (প্ল্যান কেনা/ক্রেডিট বদল ২ মিনিটের মধ্যেই ব্যাকআপে ঢোকে)

status = {"last_ok": None, "last_error": "", "last_size": 0, "last_users": None, "last_projects": None,
          "pin_warning": False, "restored": None, "restore_failed": False, "last_error_kind": "",
          "last_parts": 1, "last_seq": 0, "last_gen": 0}
_push_lock = threading.Lock()
_timer_lock = threading.Lock()
_pending_timer = [None]
_last_push_mtime = [0.0]


# ------------------------------------------------------------------ Bot API (stdlib/requests) — রিস্টোর ও পুশ দুটোর জন্য
# ⭐ FIX: আগে পুশ telebot দিয়ে একবারই চেষ্টা করত; নেটওয়ার্ক একটু হোঁচট খেলেই "ConnectionError: HTTPSConnectionPool(host='api.telegram.org'...)"
# দেখিয়ে ব্যর্থ হতো, আর এরর মেসেজ ১২০ অক্ষরে কাটা পড়ায় (টোকেনসহ URL-এই শেষ) আসল কারণ বোঝা যেত না।
# এখন: নিজস্ব রিট্রাই (ব্যাকঅফসহ), পরিষ্কার টাইমআউট, আসল কারণ দেখানো ও টোকেন মাস্ক করা।
TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_\-]{20,}")
CONNECT_TIMEOUT = 15
UPLOAD_TIMEOUT = 120

class TgApiError(RuntimeError):
    def __init__(self, method, code, desc, retry_after=None):
        self.method, self.code, self.desc, self.retry_after = method, code, str(desc or ""), retry_after
        super().__init__(f"{method}: {code} {self.desc[:100]}")

def _api_base():
    return os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")

def _mask(text) -> str:
    """এরর টেক্সট থেকে বট টোকেন মুছে দেয় (লগ/অ্যাডমিন মেসেজে টোকেন যেন না যায়)।"""
    t = str(text)
    t = re.sub(r"/bot[^/\s'\"]+", "/bot<token>", t)
    return TOKEN_RE.sub("<token>", t)

def _root_cause(e) -> str:
    """requests/urllib3 এররের গভীর থেকে আসল কারণ (DNS, Timeout, Reset...) বের করে ছোট বাংলা লেবেলে দেয়।"""
    seen, cur, parts = set(), e, []
    while cur is not None and id(cur) not in seen and len(parts) < 8:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__} {cur}")
        cur = cur.__cause__ or cur.__context__
        if cur is None and getattr(e, "args", None):
            for a in e.args:
                if isinstance(a, BaseException) and id(a) not in seen:
                    cur = a
                    break
    blob = " ".join(parts).lower()
    if "nameresolution" in blob or "name or service not known" in blob or "temporary failure in name" in blob or "getaddrinfo" in blob:
        return "DNS সমস্যা (api.telegram.org চিনতে পারছে না)"
    if "timed out" in blob or "timeout" in blob:
        return "টাইমআউট (সার্ভার সময়মতো উত্তর দেয়নি)"
    if "reset by peer" in blob or "connection reset" in blob or "connectionreset" in blob:
        return "কানেকশন রিসেট হয়েছে"
    if "refused" in blob:
        return "কানেকশন প্রত্যাখ্যাত হয়েছে"
    if "unreachable" in blob or "no route" in blob:
        return "নেটওয়ার্ক পৌঁছানো যাচ্ছে না"
    if "ssl" in blob or "certificate" in blob:
        return "SSL/সার্টিফিকেট সমস্যা"
    if "remotedisconnected" in blob or "closed connection" in blob or "connection aborted" in blob:
        return "সার্ভার কানেকশন মাঝপথে বন্ধ করেছে"
    return ""

def _is_network_error(e) -> bool:
    """নেটওয়ার্ক/টাইমআউট জাতীয় অস্থায়ী সমস্যা (যেগুলোতে আবার চেষ্টা করা অর্থপূর্ণ)।"""
    if isinstance(e, TgApiError):
        return e.code in (429, 500, 502, 503, 504) or e.code == 0
    if isinstance(e, (OSError, TimeoutError, ConnectionError)):
        return True
    n = type(e).__name__.lower()
    if any(k in n for k in ("connection", "timeout", "ssl", "protocol", "chunkedencoding", "remotedisconnected")):
        return True
    b = str(e).lower()
    return any(k in b for k in ("max retries", "connection", "timed out", "temporary failure", "name or service", "network is unreachable"))

def _clean_err(e, limit=220) -> str:
    """ব্যবহারকারী/অ্যাডমিনকে দেখানোর মতো পরিষ্কার এরর: ধরন + আসল কারণ, টোকেন ছাড়া।"""
    if isinstance(e, TgApiError):
        return _mask(f"{e.method}: Telegram {e.code} — {e.desc}")[:limit]
    cause = _root_cause(e)
    base = type(e).__name__
    if cause:
        return f"{base} — {cause}"[:limit]
    return _mask(f"{base}: {e}")[:limit]

def _post(token, method, data=None, files=None, timeout=(CONNECT_TIMEOUT, 60)):
    """Telegram Bot API POST (requests দিয়ে; না থাকলে urllib ফলব্যাক শুধু ফাইল ছাড়া কলে)।"""
    url = f"{_api_base()}/bot{token}/{method}"
    try:
        import requests
        r = requests.post(url, data=data or {}, files=files, timeout=timeout)
        try:
            j = r.json()
        except Exception:
            raise TgApiError(method, r.status_code or 0, "অপ্রত্যাশিত উত্তর")
        if not j.get("ok"):
            ra = (j.get("parameters") or {}).get("retry_after")
            raise TgApiError(method, j.get("error_code", r.status_code), j.get("description", ""), ra)
        return j["result"]
    except ImportError:
        if files:
            raise
        body = urllib.parse.urlencode(data or {}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout[1]) as rr:
                j = json.loads(rr.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            raise TgApiError(method, he.code, "HTTP error") from None
        if not j.get("ok"):
            raise TgApiError(method, j.get("error_code", 0), j.get("description", ""))
        return j["result"]

def _retry(fn, tries=4, base=2.0, label=""):
    """নেটওয়ার্ক-জাতীয় এররে ব্যাকঅফসহ আবার চেষ্টা। Telegram-এর স্পষ্ট প্রত্যাখ্যান (400/403...) হলে সাথে সাথে ছেড়ে দেয়।"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if not _is_network_error(e) or i == tries - 1:
                raise
            wait = base * (2 ** i) + random.uniform(0, 0.8)
            if isinstance(e, TgApiError) and e.retry_after:
                wait = min(float(e.retry_after) + 1, 30)
            print(f"[CH-BACKUP] ⚠️ {label or 'কল'} ব্যর্থ ({_clean_err(e, 90)}) — {wait:.0f}s পরে আবার চেষ্টা ({i + 1}/{tries - 1})")
            time.sleep(wait)
    raise last

def _call(token, method, params, timeout=30, tries=4):
    """রিস্টোরের GET-জাতীয় কল (রিট্রাইসহ)। বট অবজেক্ট তখনো নেই।"""
    return _retry(lambda: _post(token, method, data=params, timeout=(CONNECT_TIMEOUT, timeout)), tries=tries, base=2.0, label=method)

def _download(token, file_path, timeout=120, tries=4):
    url = f"{_api_base()}/file/bot{token}/{file_path}"
    def _once():
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise TgApiError("download", e.code, "ফাইল নামানো যায়নি") from None
    return _retry(_once, tries=tries, base=2.0, label="download")


# ------------------------------------------------------------------ কার্ড (PIN করা মেসেজের ক্যাপশন) ও হেল্পার
def _fmt_size(n) -> str:
    n = float(n or 0)
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"

def _bar(pct, width=10) -> str:
    f = max(0, min(width, int(round(pct / 100 * width))))
    return "▓" * f + "░" * (width - f)

def _delta(cur, prev) -> str:
    if cur is None or prev is None or cur == prev:
        return ""
    d = cur - prev
    return f" ({'+' if d > 0 else ''}{d})"

def _parse_card(caption) -> dict:
    """আগের কার্ড থেকে ব্যাকআপ নম্বর ও আগের সংখ্যা পড়ে (বদল দেখানো ও নম্বর চালু রাখার জন্য — সার্ভার রিসেট হলেও চলে)।"""
    out = {}
    c = caption or ""
    for key, pat in (("seq", r"ব্যাকআপ #(\d+)"), ("users", r"ইউজার:\s*(\d+)"), ("projects", r"বট:\s*(\d+)")):
        m = re.search(pat, c)
        if m:
            out[key] = int(m.group(1))
    return out

def _build_card(stats, size, last_part_size, n_parts, seq, sha, prev, gen=0) -> str:
    users, projects = stats.get("users"), stats.get("projects")
    paid, banned = stats.get("paid"), stats.get("banned")
    running = stats.get("running")
    free = (users - paid) if (users is not None and paid is not None) else None
    stopped = (projects - running) if (projects is not None and running is not None) else None
    pct = min(100, last_part_size * 100 / PART_BYTES)
    lines = [MARKER, f"🗄 ডাটাবেস ব্যাকআপ #{seq}", "━━━━━━━━━━━━━━━━━━",
             f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"👥 ইউজার: {users}{_delta(users, prev.get('users'))}"]
    if paid is not None:
        lines.append(f"   💎 পেইড: {paid} • 🆓 ফ্রি: {free} • 🚫 ব্যান: {banned if banned is not None else 0}")
    lines.append(f"🤖 বট: {projects}{_delta(projects, prev.get('projects'))}")
    if running is not None:
        lines.append(f"   🟢 চলছে: {running} • 🔴 বন্ধ: {stopped}")
    if n_parts <= 1:
        lines.append(f"📦 সাইজ: {_fmt_size(size)} (ZIP) • 📌 একক ফাইল")
    else:
        lines.append(f"📦 সাইজ: {_fmt_size(size)} (ZIP) • 🧩 {n_parts}টি ফাইল • জেনারেশন #{gen}")
    lines.append(f"💾 ফাইল ভর্তি: {_bar(pct)} {pct:.0f}% (~{PART_BYTES // (1024 * 1024)} MB-তে নতুন ফাইল পিন হয়)")
    lines.append(f"🔐 SHA256: {sha[:16]}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("♻️ রিসেটের পর বট এই PIN করা মেসেজ থেকেই সব ফিরিয়ে আনে — এটা মুছবেন না/আনপিন করবেন না")
    text = "\n".join(lines)
    return text[:1020]

def _db_stats(path) -> dict:
    st = {}
    try:
        con = sqlite3.connect(path, timeout=10)
    except Exception:
        return st
    def one(q):
        try:
            return con.execute(q).fetchone()[0]
        except Exception:
            return None
    st["users"] = one("SELECT COUNT(*) FROM users")
    st["projects"] = one("SELECT COUNT(*) FROM projects")
    st["paid"] = one("SELECT COUNT(*) FROM users WHERE UPPER(COALESCE(plan,'FREE')) != 'FREE'")
    st["banned"] = one("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    st["running"] = one("SELECT COUNT(*) FROM projects WHERE UPPER(COALESCE(status,'')) = 'RUNNING'")
    try:
        con.close()
    except Exception:
        pass
    return st

def _keep_gens() -> int:
    try:
        return max(0, int(os.environ.get("CH_BACKUP_KEEP", "2")))
    except Exception:
        return 2


# ------------------------------------------------------------------ রিস্টোর (import admin-এর আগে)
def _apply_zip(raw: bytes) -> bool:
    zf = zipfile.ZipFile(io.BytesIO(raw))
    if "pyhost_system.db" not in zf.namelist():
        print("[CH-BACKUP] ব্যাকআপ ফাইলে DB নেই — বাতিল।")
        status["restore_failed"] = True
        return False
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    with zf.open("pyhost_system.db") as src, open(DB_PATH, "wb") as out:
        shutil.copyfileobj(src, out)
    users, projects = _db_counts(DB_PATH)
    status["restored"] = {"users": users, "projects": projects}
    print(f"[CH-BACKUP] ✅ DB রিস্টোর সম্পন্ন: {users} ইউজার, {projects} বট।")
    return True

def _fetch_file(token, file_id, tries=6) -> bytes:
    info = _call(token, "getFile", {"file_id": file_id}, tries=tries)
    return _download(token, info["file_path"], tries=tries)

def _assemble_parts(token, channel_id, man) -> bytes:
    """ম্যানিফেস্টের সব পার্ট নামিয়ে জোড়া লাগায় ও checksum মেলায়। file_id কাজ না করলে (যেমন নতুন বট) মেসেজ-আইডি দিয়ে ফরওয়ার্ড করে নেয়।"""
    buf = bytearray()
    for part in sorted(man["parts"], key=lambda x: x["i"]):
        try:
            raw = _fetch_file(token, part["file_id"])
        except Exception:
            fw = _call(token, "forwardMessage", {"chat_id": channel_id, "from_chat_id": channel_id, "message_id": part["message_id"]}, tries=3)
            try:
                raw = _fetch_file(token, (fw.get("document") or {})["file_id"])
            finally:
                try:
                    _post(token, "deleteMessage", {"chat_id": channel_id, "message_id": fw["message_id"]})
                except Exception:
                    pass
        if hashlib.sha256(raw).hexdigest() != part["sha"]:
            raise ValueError(f"পার্ট {part['i']} checksum মেলেনি")
        buf += raw
    if hashlib.sha256(bytes(buf)).hexdigest() != man["sha256"]:
        raise ValueError("মোট ব্যাকআপের checksum মেলেনি")
    return bytes(buf)

def restore_db_if_needed(token, channel_id) -> bool:
    try:
        if not token or not channel_id:
            return False
        if not _local_needs_restore():
            return False
        print("[CH-BACKUP] লোকাল ডাটা নেই/ফাঁকা — চ্যানেল থেকে রিস্টোর চেষ্টা হচ্ছে...")
        chat = _call(token, "getChat", {"chat_id": channel_id}, tries=6)
        pm = chat.get("pinned_message") or {}
        doc = pm.get("document") or {}
        fname = doc.get("file_name")
        if fname == FILE_NAME:
            raw = _fetch_file(token, doc["file_id"])
        elif fname == MANIFEST_NAME:
            man = json.loads(_fetch_file(token, doc["file_id"]).decode("utf-8"))
            raw = _assemble_parts(token, channel_id, man)
        else:
            print("[CH-BACKUP] চ্যানেলে PIN করা ব্যাকআপ পাওয়া যায়নি — নতুন করে শুরু হচ্ছে।")
            return False
        return _apply_zip(raw)
    except Exception as e:
        status["restore_failed"] = True      # ওভাররাইট-সুরক্ষা: ব্যর্থ রিস্টোরের পর খালি DB দিয়ে ভালো ব্যাকআপ মুছে ফেলা আটকায়
        status["last_error"] = _clean_err(e)
        print(f"[CH-BACKUP] ❌ রিস্টোর ব্যর্থ: {_clean_err(e)}")
        return False


# ------------------------------------------------------------------ স্ন্যাপশট ও পুশ
def _make_zip():
    """রিটার্ন (zip_bytes, users, projects, stats)।"""
    tmp = tempfile.mkdtemp(prefix="pyhost_chsnap_")
    try:
        snap = os.path.join(tmp, "pyhost_system.db")
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        users, projects = _db_counts(snap)
        stats = _db_stats(snap)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(snap, arcname="pyhost_system.db")
        return buf.getvalue(), users, projects, stats
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _multi_throttle_left() -> float:
    """মাল্টি-ফাইল মোডে (বড় ব্যাকআপ) আবার পুশ করতে আর কত সেকেন্ড অপেক্ষা করা উচিত (০ = এখনই চলবে)।"""
    if (status.get("last_parts") or 1) <= 1 or not status.get("last_ok"):
        return 0
    return max(0.0, MULTI_MIN_INTERVAL - (datetime.now() - status["last_ok"]).total_seconds())

def _archive_note(token, channel_id, mid, old_caption, why, timeout=(CONNECT_TIMEOUT, 20)):
    """পুরনো PIN করা মেসেজটা মুছে না — শুধু ক্যাপশনে 'আর্কাইভ' লিখে রাখে (best-effort)।"""
    try:
        cap = f"🗄 আর্কাইভ — {why}\n" + (old_caption or "")
        _post(token, "editMessageCaption", {"chat_id": channel_id, "message_id": mid, "caption": cap[:1020]}, timeout=timeout)
    except Exception:
        pass

def push(bot, channel_id, quick=False):
    """DB স্ন্যাপশট চ্যানেলে পাঠায়। ছোট হলে PIN করা একটা মেসেজই এডিট হয়ে আপডেট হয়;
    ~১৮ MB ছাড়ালে ভাগ হয়ে নতুন ফাইল/পার্ট আপলোড হয় ও নতুন PIN করা ম্যানিফেস্ট-কার্ড সেগুলো ধরে রাখে — আগের ফাইল থেকে যায়।
    রিটার্ন (ok, message)। quick=True: ডেপ্লয়/শাটডাউনের ঠিক আগে — কম রিট্রাই, ছোট টাইমআউট।"""
    if status["restore_failed"]:
        status["last_error_kind"] = "restore"
        return False, "আগের রিস্টোর ব্যর্থ হয়েছিল — ভালো ব্যাকআপ ওভাররাইট হওয়া ঠেকাতে ব্যাকআপ বন্ধ। বট রিস্টার্ট দিয়ে আবার চেষ্টা করুন।"
    if not _push_lock.acquire(blocking=False):
        return False, "আরেকটি ব্যাকআপ চলছে"
    new_ids = []          # এই পুশে নতুন পাঠানো পার্ট — মাঝপথে ব্যর্থ হলে মুছে ফেলার জন্য
    token = None
    try:
        if not os.path.exists(DB_PATH):
            return False, "DB ফাইল নেই"
        token = getattr(bot, "token", None)
        if not token:
            return False, "বট টোকেন পাওয়া যায়নি"
        mtime_before = _db_mtime()
        data, users, projects, stats = _make_zip()
        if not users:
            return False, "DB-তে এখনো কোনো ইউজার নেই — খালি ব্যাকআপ রাখা হয়নি"
        sha = hashlib.sha256(data).hexdigest()
        chunks = [data[i:i + PART_BYTES] for i in range(0, len(data), PART_BYTES)] or [data]
        n = len(chunks)
        if n > MAX_PARTS:
            msg = f"ব্যাকআপ {_fmt_size(len(data))} — সর্বোচ্চ {MAX_PARTS}টি ফাইল ({MAX_PARTS * PART_BYTES // (1024 * 1024)} MB) সীমা ছাড়িয়ে গেছে"
            status["last_error"] = msg
            status["last_error_kind"] = "size"
            return False, msg

        tries = 2 if quick else 4
        up_to = (6, 12) if quick else (CONNECT_TIMEOUT, UPLOAD_TIMEOUT)
        gc_to = (6, 10) if quick else (CONNECT_TIMEOUT, 30)

        def _send(fname, blob, caption, mime="application/zip", label="sendDocument"):
            return _retry(lambda: _post(token, "sendDocument", {"chat_id": channel_id, "caption": caption, "disable_notification": "true"},
                                        files={"document": (fname, blob, mime)}, timeout=up_to), tries=tries, base=2.0, label=label)

        def _edit(mid, fname, blob, caption, mime="application/zip"):
            def _once():
                media = json.dumps({"type": "document", "media": "attach://f", "caption": caption})
                return _post(token, "editMessageMedia", {"chat_id": channel_id, "message_id": mid, "media": media},
                             files={"f": (fname, blob, mime)}, timeout=up_to)
            return _retry(_once, tries=tries, base=2.0, label="editMessageMedia")

        def _pin(mid):
            try:
                _retry(lambda: _post(token, "pinChatMessage", {"chat_id": channel_id, "message_id": mid, "disable_notification": "true"}, timeout=gc_to),
                       tries=2, base=1.5, label="pinChatMessage")
                status["pin_warning"] = False
            except Exception:
                status["pin_warning"] = True

        # ১) বর্তমান PIN করা মেসেজ — নেটওয়ার্ক সমস্যায় রিট্রাই; তবুও না হলে নতুন মেসেজ পাঠানো হয় না (ডুপ্লিকেট এড়াতে) — পুরো পুশ ব্যর্থ ধরা হয়
        chat = _retry(lambda: _post(token, "getChat", {"chat_id": channel_id}, timeout=gc_to), tries=tries, base=2.0, label="getChat")
        pm = (chat or {}).get("pinned_message") or {}
        doc = pm.get("document") or {}
        fname_old = doc.get("file_name")
        pin_mid = pm.get("message_id")
        old_caption = pm.get("caption") or ""
        prev = _parse_card(old_caption)
        seq = (prev.get("seq") or status.get("last_seq") or 0) + 1
        old_single = bool(pin_mid) and fname_old == FILE_NAME
        old_manifest = bool(pin_mid) and fname_old == MANIFEST_NAME

        gen = 0
        if n == 1:
            # ================= একক ফাইল মোড =================
            card = _build_card(stats, len(data), len(data), 1, seq, sha, prev)
            done = False
            if old_single:
                try:
                    _edit(pin_mid, FILE_NAME, data, card)
                    done = True
                except TgApiError as e:
                    if "not modified" in e.desc.lower():
                        done = True
                    elif _is_network_error(e):
                        raise
                    # অন্য Telegram এরর (যেমন মেসেজ মুছে গেছে) → নিচে নতুন করে পাঠানো হবে
            if not done:
                sent = _send(FILE_NAME, data, card)
                _pin(sent["message_id"])
                if old_manifest:
                    _archive_note(token, channel_id, pin_mid, old_caption, "ডেটা ছোট হওয়ায় আবার একক ফাইল মোডে ফেরা হয়েছে", gc_to)
        else:
            # ================= মাল্টি-ফাইল মোড (~১৮ MB ছাড়ালে) =================
            old_gen, old_ids, old_hist = 0, [], []
            if old_manifest:
                try:
                    man_old = json.loads(_fetch_file(token, doc["file_id"], tries=2).decode("utf-8"))
                    old_gen = int(man_old.get("gen") or 0)
                    old_ids = [p["message_id"] for p in man_old.get("parts", [])]
                    old_hist = list(man_old.get("history") or [])
                except Exception:
                    pass
            gen = max(old_gen, status.get("last_gen") or 0) + 1
            part_meta = []
            for i, ch in enumerate(chunks, 1):
                cap = f"{MARKER}\n🧩 পার্ট {i}/{n} • জেনারেশন #{gen} • {_fmt_size(len(ch))}"
                sent = _send(f"{FILE_NAME}.part{i:03d}", ch, cap, "application/octet-stream", label=f"part {i}/{n}")
                new_ids.append(sent["message_id"])
                part_meta.append({"i": i, "message_id": sent["message_id"], "file_id": (sent.get("document") or {}).get("file_id", ""),
                                  "size": len(ch), "sha": hashlib.sha256(ch).hexdigest()})
            # পুরনো জেনারেশন থেকে কতগুলো রাখব (বাকিগুলো মোছা হবে)
            hist = ([{"gen": old_gen, "ids": old_ids}] if old_ids else []) + old_hist
            keep = _keep_gens()
            to_delete = []
            if keep > 0:
                for h in hist[max(0, keep - 1):]:
                    to_delete += list(h.get("ids") or [])
                hist = hist[:max(0, keep - 1)]
            else:
                hist = hist[:50]
            manifest = {"v": 2, "gen": gen, "seq": seq, "time": datetime.now().isoformat(timespec="seconds"),
                        "sha256": sha, "size": len(data), "parts": part_meta, "history": hist}
            mbytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            card = _build_card(stats, len(data), len(chunks[-1]), n, seq, sha, prev, gen)
            if old_manifest:
                # ম্যানিফেস্ট একটাই মেসেজে এডিট হয় — মিডিয়া+ক্যাপশন একসাথে বদলায়, তাই সব পার্ট আপলোডের পরেই এটা বদলায় (ক্র্যাশ-সেফ)
                _edit(pin_mid, MANIFEST_NAME, mbytes, card, "application/json")
            else:
                sent = _send(MANIFEST_NAME, mbytes, card, "application/json", label="manifest")
                new_ids.append(sent["message_id"])
                _pin(sent["message_id"])
                if old_single:
                    _archive_note(token, channel_id, pin_mid, old_caption, "ডেটা বড় হওয়ায় নতুন মাল্টি-ফাইল ব্যাকআপ চালু হয়েছে (এটা আগের ফাইল, মুছা হয়নি)", gc_to)
            new_ids = []      # সফল — ক্লিনআপ লাগবে না
            for mid_old in to_delete:
                try:
                    _post(token, "deleteMessage", {"chat_id": channel_id, "message_id": mid_old}, timeout=(CONNECT_TIMEOUT, 15))
                except Exception:
                    pass

        status.update(last_ok=datetime.now(), last_error="", last_error_kind="", last_size=len(data), last_users=users,
                      last_projects=projects, last_parts=n, last_seq=seq, last_gen=gen)
        _last_push_mtime[0] = mtime_before
        warn = " ⚠️ PIN করা যায়নি — বটকে চ্যানেলে 'Pin Messages' পারমিশন দিন, নাহলে রিস্টোর কাজ করবে না" if status["pin_warning"] else ""
        parts_txt = f" • {n}টি ফাইল" if n > 1 else ""
        return True, f"{users} ইউজার • {projects} বট • {_fmt_size(len(data))}{parts_txt}{warn}"
    except Exception as e:
        status["last_error"] = _clean_err(e)
        status["last_error_kind"] = "net" if _is_network_error(e) else "api"
        # মাল্টি-ফাইল পুশ মাঝপথে ব্যর্থ হলে এই পুশের অর্ধেক পাঠানো পার্টগুলো মুছে দেওয়া হয় — চালু ব্যাকআপ (পুরনো ম্যানিফেস্ট) অক্ষত থাকে
        if new_ids and token:
            for mid_new in new_ids:
                try:
                    _post(token, "deleteMessage", {"chat_id": channel_id, "message_id": mid_new}, timeout=(CONNECT_TIMEOUT, 15))
                except Exception:
                    pass
        return False, status["last_error"]
    finally:
        _push_lock.release()

def _db_mtime():
    m = 0.0
    for suf in ("", "-wal"):
        try:
            m = max(m, os.path.getmtime(DB_PATH + suf))
        except OSError:
            pass
    return m

def schedule_push(bot, channel_id, delay=30):
    """কোড আপলোডের পর কল হয়। অনেকগুলো আপলোড একসাথে এলে ১টা পুশেই সামলায়।"""
    with _timer_lock:
        t = _pending_timer[0]
        if t is not None and t.is_alive():
            return
        t = threading.Timer(delay, lambda: (push(bot, channel_id) if _multi_throttle_left() <= 0 else None))
        t.daemon = True
        _pending_timer[0] = t
        t.start()

def start_periodic(bot, channel_id, on_failure=None):
    """প্রথম পুশ ৬০ সেকেন্ড পর; এরপর প্রতি ২ মিনিটে DB বদলালে পুশ।
    অ্যাডমিনকে জানানো: নেটওয়ার্ক-জাতীয় অস্থায়ী ব্যর্থতায় টানা ৩ বার (~৬ মিনিট) ফেইল করলে তবেই (রিট্রাইয়ের পরও); পারমিশন/অন্য এরর হলে সাথে সাথে।
    আর কখনোই স্প্যাম নয় — সফল হয়ে আবার ব্যর্থ হলে নতুন করে একবার।"""
    def loop():
        time.sleep(60)
        first = True
        fails = 0
        alerted = False
        while True:
            try:
                if first or (_db_mtime() > _last_push_mtime[0] and _multi_throttle_left() <= 0):
                    ok, msg = push(bot, channel_id)
                    first = False
                    print(f"[CH-BACKUP] {'✅' if ok else '❌'} {msg}")
                    if ok:
                        fails, alerted = 0, False
                    elif "খালি ব্যাকআপ" in msg or "আরেকটি ব্যাকআপ" in msg:
                        pass
                    else:
                        fails += 1
                        transient = status.get("last_error_kind") == "net"
                        if on_failure and not alerted and (fails >= 3 or not transient):
                            on_failure(False, msg)
                            alerted = True
            except Exception:
                pass
            time.sleep(CHECK_EVERY)
    t = threading.Thread(target=loop, daemon=True, name="ch-backup")
    t.start()
    return t


# ------------------------------------------------------------------ 🧹 অ্যাডমিন: DB-ব্যাকআপ পরিষ্কার (ইউজারদের কোড-কপি কখনো ছোঁয়া হয় না)
def _pinned(bot, channel_id):
    token = getattr(bot, "token", None)
    chat = _call(token, "getChat", {"chat_id": channel_id}, tries=3)
    pm = (chat or {}).get("pinned_message") or {}
    return token, pm, pm.get("document") or {}

def _del_msg(token, channel_id, mid) -> bool:
    try:
        _post(token, "deleteMessage", {"chat_id": channel_id, "message_id": mid}, timeout=(CONNECT_TIMEOUT, 15))
        return True
    except TgApiError as e:
        return "not found" in e.desc.lower() or "invalid" in e.desc.lower()
    except Exception:
        return False

def inspect(bot, channel_id) -> dict:
    """চ্যানেলে এখন কী ধরনের DB-ব্যাকআপ আছে (পড়া-মাত্র, কিছু বদলায় না)।"""
    token, pm, doc = _pinned(bot, channel_id)
    fname = doc.get("file_name")
    if fname == FILE_NAME:
        return {"pinned": True, "mode": "single", "size": doc.get("file_size") or 0, "parts": 1, "old_msgs": 0}
    if fname == MANIFEST_NAME:
        man = json.loads(_fetch_file(token, doc["file_id"], tries=2).decode("utf-8"))
        old = sum(len(h.get("ids") or []) for h in (man.get("history") or []))
        return {"pinned": True, "mode": "multi", "size": man.get("size") or 0, "parts": len(man.get("parts") or []), "old_msgs": old}
    return {"pinned": False}

def clean(bot, channel_id, kind: str):
    """kind='old'  : পুরনো জেনারেশনের মেসেজ মোছে, বর্তমান PIN করা ব্যাকআপ অক্ষত।
       kind='reset': সব DB-ব্যাকআপ (বর্তমানটাসহ) মুছে এখনকার DB থেকে নতুন ব্যাকআপ বানায়।
    রিটার্ন (ok, বার্তা)।"""
    if not _push_lock.acquire(blocking=False):
        return False, "আরেকটি ব্যাকআপ চলছে — একটু পরে চেষ্টা করুন"
    try:
        token, pm, doc = _pinned(bot, channel_id)
        fname, pin_mid = doc.get("file_name"), pm.get("message_id")
        man = None
        if fname == MANIFEST_NAME:
            man = json.loads(_fetch_file(token, doc["file_id"], tries=3).decode("utf-8"))
        old_ids = [i for h in ((man or {}).get("history") or []) for i in (h.get("ids") or [])]
        if kind == "old":
            if not man or not old_ids:
                return True, "মোছার মতো পুরনো DB-ব্যাকআপ নেই"
            n = sum(1 for i in old_ids if _del_msg(token, channel_id, i))
            if n == len(old_ids):
                man["history"] = []        # সব মুছেছে; আংশিক হলে ইনডেক্স যেমন ছিল তেমনই থাকে
            media = json.dumps({"type": "document", "media": "attach://f", "caption": (pm.get("caption") or "")[:1020]})
            _retry(lambda: _post(token, "editMessageMedia", {"chat_id": channel_id, "message_id": pin_mid, "media": media},
                                 files={"f": (MANIFEST_NAME, json.dumps(man, ensure_ascii=False).encode("utf-8"), "application/json")},
                                 timeout=(CONNECT_TIMEOUT, UPLOAD_TIMEOUT)), tries=3, label="manifest")
            return True, f"{n}/{len(old_ids)}টি পুরনো ব্যাকআপ-মেসেজ মোছা হয়েছে"
        # reset
        ids = list(old_ids)
        if man:
            ids += [p["message_id"] for p in man.get("parts", [])]
        if pin_mid:
            ids.append(pin_mid)
        n = sum(1 for i in ids if _del_msg(token, channel_id, i))
        status.update(last_seq=0, last_gen=0, last_parts=1)
    finally:
        _push_lock.release()
    ok, msg = push(bot, channel_id)
    if ok:
        return True, f"{n}টি পুরনো মেসেজ মুছে নতুন ব্যাকআপ তৈরি হয়েছে — {msg}"
    return False, f"পুরনো মোছা হয়েছে কিন্তু নতুন ব্যাকআপ তৈরি হয়নি ({msg}) — এখনই 📡 Backup Now চাপুন"
