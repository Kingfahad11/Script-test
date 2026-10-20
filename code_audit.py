"""
🔍 CODE AUDIT — আপলোড/পেস্ট করা কোড ও ZIP-এর বিস্তারিত বিশ্লেষণ + প্রাইভেট চ্যানেলের কার্ড

সব বিশ্লেষণ নিয়ম-ভিত্তিক (regex/AST) — ম্যালওয়্যার ডিটেকশন বা সত্যিকারের AI নয়; অ্যাডমিনের দ্রুত রিভিউয়ের জন্য ইঙ্গিত দেয়।
টেলিগ্রামের ক্যাপশন-সীমা (১০২৪) এড়াতে কার্ডটা ডকুমেন্টের রিপ্লাই মেসেজ হিসেবে যায় (সীমা ৪০৯৬)।
"""
import re
import io
import ast
import html
import zipfile
from datetime import datetime

MAX_PY_READ = 600 * 1024          # প্রতিটি ফাইল থেকে সর্বোচ্চ কত বাইট পড়া হবে
MAX_TOTAL_READ = 4 * 1024 * 1024
MAX_PY_FILES = 60
ZIP_MAX_ENTRIES = 3000
ZIP_MAX_UNPACKED = 200 * 1024 * 1024
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024

# (নাম, regex, ঝুঁকি-পয়েন্ট)
_THREATS = [
    ("os.system()", r"\bos\.system\s*\(", 15),
    ("subprocess shell=True", r"subprocess\.(?:Popen|call|run|check_output)\s*\([^)]{0,200}shell\s*=\s*True", 20),
    ("eval()", r"(?<![\w.])eval\s*\(", 15),
    ("exec()", r"(?<![\w.])exec\s*\(", 15),
    ("rm -rf /", r"rm\s+-rf\s+/", 40),
    ("base64 decode", r"base64\.b(?:64|32|16)decode\s*\(", 10),
    ("pickle/marshal load", r"(?:pickle|marshal)\.loads?\s*\(", 10),
    ("dynamic __import__", r"__import__\s*\(", 10),
    ("ctypes", r"\bimport\s+ctypes|\bfrom\s+ctypes\b", 10),
    ("reverse-shell pattern", r"pty\.spawn|/bin/(?:ba)?sh\s+-i|socket\.[^\n]{0,80}\n?[^\n]{0,80}dup2", 40),
    ("crypto-miner marker", r"stratum\+tcp|xmrig|minerd|cryptonight", 50),
]
_SECRET_KEYS = [
    ("Google/Gemini", r"AIza[0-9A-Za-z_\-]{30,}"),
    ("OpenAI-style", r"\bsk-[A-Za-z0-9_\-]{20,}"),
    ("GitHub", r"\bgh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    ("AWS", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Stripe", r"\b[sr]k_live_[0-9A-Za-z]{16,}"),
    ("Generic api_key", r"""(?i)\bapi[_\-]?key\s*[=:]\s*['"][A-Za-z0-9_\-]{16,}['"]"""),
]
_CREDENTIALS = [
    r"""(?i)\b(?:password|passwd|secret|client_secret)\s*[=:]\s*['"][^'"\s]{6,}['"]""",
    r'"type"\s*:\s*"service_account"',
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
    r"(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql)://[^/\s:@]+:[^@\s]+@",
]
_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")
_SAFE_HOSTS = ("api.telegram.org", "t.me", "telegram.org")


# ------------------------------------------------------------------ ইনপুট পড়া
def _is_suspicious_name(n: str) -> bool:
    n2 = n.replace("\\", "/")
    return n2.startswith("/") or ".." in n2.split("/") or "\\" in n or (len(n2) > 1 and n2[1] == ":")

def load_sources(file_name: str, data: bytes):
    """রিটার্ন (sources[(path, text)], meta dict)। ZIP হলে meta-তে ZIP-এর তথ্য থাকে।"""
    lower = file_name.lower()
    meta = {"kind": "zip" if lower.endswith(".zip") else ("py" if lower.endswith(".py") else "other")}
    if meta["kind"] != "zip":
        text = data[:MAX_PY_READ * 2].decode("utf-8", errors="ignore")
        return [(file_name, text)], meta

    meta.update(valid=True, entries=0, folders=0, py_count=0, unpacked=0, ratio=None, top=[], skipped=0,
                main=None, req_packages=None, too_big=False, truncated=False, error="")
    sources = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        meta.update(valid=False, error="ZIP পড়া যায়নি (ভাঙা/পাসওয়ার্ড দেওয়া)")
        return sources, meta
    infos = zf.infolist()
    files = [i for i in infos if not i.is_dir()]
    meta["entries"] = len(files)
    meta["folders"] = sum(1 for i in infos if i.is_dir()) or len({i.filename.rsplit("/", 1)[0] for i in files if "/" in i.filename})
    meta["unpacked"] = sum(i.file_size for i in files)
    if data:
        meta["ratio"] = round(meta["unpacked"] / max(1, len(data)), 1)
    meta["skipped"] = sum(1 for i in infos if _is_suspicious_name(i.filename))
    if len(infos) > ZIP_MAX_ENTRIES or meta["unpacked"] > ZIP_MAX_UNPACKED:
        meta["too_big"] = True            # বট এক্সট্রাকশন নিজেই এটা ফেরত দেয়; আমরাও ভেতরে পড়ছি না
        return sources, meta
    tops = []
    for i in infos:
        top = i.filename.replace("\\", "/").strip("/").split("/")[0]
        if top and top not in tops:
            tops.append(top + ("/" if ("/" in i.filename.strip("/") or i.is_dir()) else ""))
    meta["top"] = tops
    py_infos = [i for i in files if i.filename.lower().endswith(".py") and not _is_suspicious_name(i.filename)]
    meta["py_count"] = len(py_infos)
    names = {i.filename for i in py_infos}
    base_names = [i.filename.rsplit("/", 1)[-1] for i in py_infos]
    meta["main"] = "main.py" if "main.py" in base_names else (base_names[0] if base_names else None)
    py_infos.sort(key=lambda i: (i.filename.rsplit("/", 1)[-1] != "main.py", i.filename.count("/"), i.filename))
    total = 0
    for i in py_infos[:MAX_PY_FILES]:
        if total >= MAX_TOTAL_READ:
            meta["truncated"] = True
            break
        try:
            raw = zf.open(i).read(MAX_PY_READ)
        except Exception:
            continue
        total += len(raw)
        sources.append((i.filename, raw.decode("utf-8", errors="ignore")))
    if len(py_infos) > MAX_PY_FILES:
        meta["truncated"] = True
    for i in files:
        if i.filename.rsplit("/", 1)[-1].lower() == "requirements.txt" and i.file_size < 200 * 1024:
            try:
                req = zf.open(i).read(200 * 1024).decode("utf-8", errors="ignore")
                meta["req_packages"] = sum(1 for l in req.splitlines() if l.strip() and not l.strip().startswith("#"))
            except Exception:
                pass
            break
    return sources, meta


# ------------------------------------------------------------------ বিশ্লেষণ
def analyze(file_name: str, data: bytes, extract_pkgs=None):
    sources, meta = load_sources(file_name, data)
    joined = "\n".join(t for _, t in sources)
    info = {"meta": meta, "size": len(data), "file_name": file_name, "sources": len(sources)}

    # টোকেন (main.py অগ্রাধিকার — sources আগে থেকেই main প্রথমে সাজানো)
    token, tokens = None, set()
    for _, t in sources:
        for m in _TOKEN_RE.finditer(t):
            tokens.add(m.group(0))
            token = token or m.group(0)
    info["token"], info["token_count"] = token, len(tokens)

    # প্যাকেজ
    pkgs = []
    if extract_pkgs:
        local = {p.rsplit("/", 1)[-1][:-3] for p, _ in sources if p.endswith(".py")} | {p.split("/")[0] for p, _ in sources if "/" in p}
        seen = set()
        for _, t in sources:
            try:
                for p in extract_pkgs(t, local):
                    if p not in seen:
                        seen.add(p); pkgs.append(p)
            except Exception:
                pass
    info["packages"] = pkgs

    # সিনট্যাক্স ও গঠন (.py হলে)
    info["syntax_error"] = None
    info["functions"] = info["classes"] = info["lines"] = 0
    for path, t in sources:
        info["lines"] += t.count("\n") + 1
        try:
            tree = ast.parse(t)
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)): info["functions"] += 1
                elif isinstance(n, ast.ClassDef): info["classes"] += 1
        except SyntaxError as e:
            if info["syntax_error"] is None:
                info["syntax_error"] = f"{path.rsplit('/', 1)[-1]}:{e.lineno}"
        except Exception:
            pass

    # নিরাপত্তা স্ক্যান
    hits, score = [], 0
    for name, pat, pts in _THREATS:
        if re.search(pat, joined):
            hits.append(name); score += pts
    info["threats"], info["risk"] = hits, min(100, score)

    # সিক্রেট
    keys = [name for name, pat in _SECRET_KEYS if re.search(pat, joined)]
    info["api_key_count"] = len({m.group(0) for _, pat in _SECRET_KEYS for m in re.finditer(pat, joined)})
    creds = sum(1 for pat in _CREDENTIALS if re.search(pat, joined))
    info["api_keys"], info["credentials"] = keys, creds

    # ফায়ারবেস
    info["fb_admin"] = bool(re.search(r"\bfirebase_admin\b|firebase-adminsdk", joined))
    info["fb_any"] = info["fb_admin"] or bool(re.search(r"firebaseio\.com|\bpyrebase\b|FIREBASE_", joined, re.I))
    db_kinds = []
    if re.search(r"firestore", joined, re.I): db_kinds.append("Firestore")
    if re.search(r"db\.reference|firebaseio\.com|\bdatabase\.reference", joined): db_kinds.append("Realtime DB")
    info["fb_db"] = db_kinds
    info["fb_service"] = bool(re.search(r"credentials\.Certificate|service_?account|serviceAccount", joined))

    # নেটওয়ার্ক
    info["http"] = bool(re.search(r"\brequests\.(?:get|post|put|delete|patch|request|Session)\b|\bhttpx\b|\baiohttp\b|urllib\.request|\burlopen\s*\(|http\.client", joined))
    hosts = []
    for h in re.findall(r"https?://([A-Za-z0-9.\-]+)", joined):
        h = h.lower().rstrip(".")
        if h and not any(h == s or h.endswith("." + s) for s in _SAFE_HOSTS) and h not in hosts:
            hosts.append(h)
    info["hosts"] = hosts
    raw_net = []
    if re.search(r"\bimport\s+socket\b|\bsocket\.socket\s*\(", joined): raw_net.append("socket")
    if re.search(r"\bparamiko\b|\bftplib\b", joined): raw_net.append("ssh/ftp")
    if re.search(r"\bsmtplib\b", joined): raw_net.append("smtp")
    info["raw_net"] = raw_net

    # পারফরম্যান্স ইঙ্গিত
    busy = bool(re.search(r"while\s+True\s*:", joined)) and not re.search(r"time\.sleep|asyncio\.sleep|polling|run_forever|\.start\(\)|idle\(\)|run_until_complete|serve_forever", joined)
    info["perf"] = "⚠️ Heavy (busy loop?)" if busy else ("⚠️ Large file" if len(data) > 1024 * 1024 else "Good")
    return info


# ------------------------------------------------------------------ কার্ড
def mask_token(tok: str) -> str:
    return f"{tok[:4]}{'•' * 13}{tok[-4:]}" if tok and len(tok) > 10 else "—"

def _e(s) -> str:
    return html.escape(str(s))

def _fmt_size(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.2f} MB"

def _status(risk: int):
    if risk == 0: return "🟢 CLEAN", "SAFE"
    if risk < 30: return "🟡 REVIEW", "REVIEW"
    if risk < 60: return "🟠 SUSPICIOUS", "CAUTION"
    return "🔴 DANGEROUS", "UNSAFE"

def build_card(info: dict, ctx: dict) -> str:
    """প্রাইভেট চ্যানেলের কমপ্যাক্ট অডিট কার্ড।
    ctx: name, user_id, username, plan_line, project, slot, bot_user (str|None), bot_live (bool|None)।
    বট টোকেন এখানে সম্পূর্ণ দেখানো হয় (শুধু অ্যাডমিনের প্রাইভেট চ্যানেলে যায়)।"""
    meta = info["meta"]
    kind = meta["kind"]
    ftype = {"py": "Python 3", "zip": "ZIP Project"}.get(kind, "Other")
    status, verdict = _status(info["risk"])
    verdict_icon = status.split(" ")[0]
    if info["syntax_error"]:
        verdict_icon, verdict = "🟡", "CHECK (syntax error)"

    # বট স্ট্যাটাস
    if not info["token"]:
        bot_state = "❔ NO TOKEN"
    elif ctx.get("bot_user"):
        bot_state = "🟢 LIVE" if ctx.get("bot_live") else "🔴 INVALID"
    else:
        bot_state = "⚠️ UNVERIFIED"

    slot = ctx.get("slot") or "Unassigned"
    slot = slot if str(slot).lower().startswith(("slot", "unassigned")) else f"Slot {slot}"

    n_pk = len(info["packages"])
    n_fn, n_cl = info["functions"], info["classes"]
    n_keys = info.get("api_key_count", len(info["api_keys"]))
    n_urls = len(info["hosts"])
    net = "HTTP/API" if info["http"] else "No HTTP"
    if info["raw_net"]:
        net = "⚠️ Raw " + "/".join(info["raw_net"])

    L = [
        "╭━━[ 🔍 𝗖𝗢𝗗𝗘 𝗔𝗨𝗗𝗜𝗧 ]━━╮",
        f"┃ 📅 {datetime.now().strftime('%d %b %Y • %I:%M %p')}",
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 👤 {_e(ctx['name'])}",
        f"┃ 🆔 <code>{ctx['user_id']}</code> • @{_e(ctx.get('username') or 'N/A')}",
        f"┃ 👑 {_e(ctx['plan_line'])}",
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 🤖 {_e(ctx['project'])} • {_e(slot)} • {bot_state}",
    ]
    if info["token"]:
        if ctx.get("bot_live") and ctx.get("bot_user"):
            L.append(f"┃ {_e(ctx['bot_user'])}")
        L.append(f"┃ 🔐 Token: <code>{_e(info['token'])}</code>" + (f" (+{info['token_count'] - 1} more)" if info["token_count"] > 1 else ""))
    else:
        L.append("┃ 🔐 Token: ❌ Not found")
    L += [
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 📄 <code>{_e(info['file_name'])}</code> • {_fmt_size(info['size'])} • {ftype}",
    ]
    if kind == "zip":
        if not meta["valid"]:
            L.append(f"┃ ❌ {_e(meta['error'])}")
        elif meta["too_big"]:
            L.append("┃ ⚠️ ZIP অনেক বড় (সীমা ২০০MB/৩০০০ ফাইল) — স্ক্যান হয়নি")
        else:
            L.append(f"┃ 🗂️ {meta['entries']} Files • {meta['folders']} Folders • {meta['py_count']} .py"
                     + (" (partial scan)" if meta["truncated"] else ""))
            L.append(f"┃ 🎯 Main: {_e(meta['main'] or 'None')} • Req: "
                     + (f"{meta['req_packages']} pkgs" if meta["req_packages"] is not None else "None"))
        if info["size"] > TG_DOWNLOAD_LIMIT:
            L.append("┃ ⚠️ ২০MB+ ZIP — অটো-রিস্টোর সম্ভব নয়")
    L.append(f"┃ 🧮 {info['lines']} Lines • {n_fn} Function{'s' if n_fn != 1 else ''} • {n_cl} Class{'es' if n_cl != 1 else ''}")
    L.append(f"┃ 📦 {n_pk} Package{'s' if n_pk != 1 else ''}")
    if info["syntax_error"]:
        L.append(f"┃ ❗ Syntax error: <code>{_e(info['syntax_error'])}</code>")
    L += [
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 🛡️ {status.split(' ', 1)[1]} • Threats {len(info['threats'])} • Risk {info['risk']}/100",
    ]
    if info["threats"]:
        L.append(f"┃ ⚠️ {_e(', '.join(info['threats'][:4]))}")
    L += [
        f"┃ 🔑 API Keys {n_keys if n_keys else 'None'} • Firebase {'Detected' if info['fb_any'] else 'None'}",
        f"┃ 🔐 Credentials {str(info['credentials']) if info['credentials'] else 'None'} • Token {'Found' if info['token'] else 'None'}",
        f"┃ 🌐 {n_urls} URL{'s' if n_urls != 1 else ''} • {_e(net)} • Performance {_e(info['perf'])}",
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 📋 Verdict: {verdict_icon} {verdict}",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
    ]
    return "\n".join(L)[:4000]
