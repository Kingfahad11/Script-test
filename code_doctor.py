"""
🩺 CODE DOCTOR — ডিপ্লয়ের আগে কোড পরীক্ষা + ক্র্যাশের পর ঠিক কোথায় সমস্যা তা দেখানো

১) preflight(...)  : টোকেন আছে কিনা, সিনট্যাক্স এরর (লাইন নম্বর সহ), প্লেসহোল্ডার টোকেন, পোলিং লুপ নেই, input() ইত্যাদি
২) diagnose_traceback(...) : ক্র্যাশ লগের Traceback থেকে ফাইল, লাইন, কোডের লাইনটা ও বাংলায় কারণ-সমাধান
সবই নিয়ম-ভিত্তিক (regex/AST) — ভুল ইঙ্গিত দিতে পারে, তাই টোকেন-না-পাওয়ার কার্ডে "তবুও চালান" অপশন থাকে।
"""
import os
import re
import ast
import html

TG_LIBS = ("telebot", "telegram", "aiogram", "pyrogram", "telethon", "pytelegrambotapi", "tgcrypto", "hydrogram", "pyrogram")
TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")
_TEXT_EXT = (".env", ".json", ".txt", ".ini", ".cfg", ".yml", ".yaml", ".toml", ".conf")
MAX_FILE = 300 * 1024


def _e(s) -> str:
    return html.escape(str(s))


# ------------------------------------------------------------------ প্রজেক্ট লোড
def load_project(p_dir: str):
    """রিটার্ন (py_sources{rel: text}, other_text{rel: text})।"""
    py, other = {}, {}
    if not os.path.isdir(p_dir):
        return py, other
    count = 0
    for root, dirs, files in os.walk(p_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "venv", ".venv", "node_modules", "site-packages")]
        for fn in files:
            if count > 80:
                break
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, p_dir).replace(os.sep, "/")
            low = fn.lower()
            try:
                if low.endswith(".py"):
                    with open(full, "rb") as f:
                        py[rel] = f.read(MAX_FILE).decode("utf-8", errors="replace")
                    count += 1
                elif low.endswith(_TEXT_EXT) or low == ".env":
                    with open(full, "rb") as f:
                        other[rel] = f.read(100 * 1024).decode("utf-8", errors="replace")
                    count += 1
            except Exception:
                pass
    return py, other


# ------------------------------------------------------------------ সিনট্যাক্স
def _syntax_hint(msg: str, line_text: str) -> str:
    m = (msg or "").lower()
    lt = (line_text or "").strip()
    if "unterminated string" in m or "eol while scanning" in m:
        return "কোটেশন চিহ্ন (' বা \") খোলা হয়েছে কিন্তু বন্ধ করা হয়নি। লাইনের শেষে ঠিক কোটেশন দিন।"
    if "was never closed" in m or "unexpected eof" in m or "unmatched" in m:
        return "বন্ধনী ( [ { খোলা হয়েছে কিন্তু বন্ধ হয়নি (বা বাড়তি বন্ধনী আছে)। জোড়া মিলিয়ে দেখুন — সমস্যাটা এই লাইনে বা তার আগের লাইনে থাকতে পারে।"
    if "expected an indented block" in m:
        return "if/for/def/class-এর পরের লাইনটা ভেতরে সরিয়ে (৪টা স্পেস) লিখতে হবে।"
    if "unexpected indent" in m or "unindent does not match" in m or "inconsistent use of tabs" in m:
        return "ইন্ডেন্টেশন (শুরুর স্পেস) মিলছে না। ট্যাব আর স্পেস মেশাবেন না — সব জায়গায় ৪টা স্পেস দিন।"
    if "missing parentheses in call to 'print'" in m:
        return "এটা Python 2-এর কোড। Python 3-এ লিখুন: print(\"...\")"
    if "non-printable" in m or "invalid character" in m or "invalid non-printable" in m:
        return "কপি-পেস্টের সময় অদৃশ্য/অন্য ধরনের চিহ্ন (যেমন “ ” স্মার্ট কোট) ঢুকে গেছে। সাধারণ \" বা ' দিয়ে বদলান।"
    if "perhaps you forgot a comma" in m:
        return "দুটো মানের মাঝে কমা (,) বাদ পড়েছে।"
    if "cannot assign" in m or "expression" in m and "assign" in m:
        return "এখানে = এর বাঁ-পাশে ভেরিয়েবল থাকতে হবে। তুলনার জন্য == ব্যবহার করুন।"
    if re.match(r"^(if|elif|else|for|while|def|class|try|except|finally|with|async)\b", lt) and not lt.endswith(":") and not lt.endswith(("(", ",", "\\", "[", "{")):
        return "এই লাইনের শেষে কোলন ( : ) নেই।"
    if lt.startswith(("!pip", "%pip", "!apt", "%matplotlib")):
        return "এটা Jupyter/Colab-এর কমান্ড। .py ফাইলে চলবে না — প্যাকেজ requirements.txt-এ লিখুন।"
    return "এই লাইনের (বা ঠিক আগের লাইনের) লেখায় Python-এর ব্যাকরণ ভুল আছে।"

def _snippet(text: str, lineno: int, col=None, ctx: int = 1):
    lines = text.splitlines()
    out = []
    for n in range(max(1, lineno - ctx), min(len(lines), lineno + ctx) + 1):
        raw = lines[n - 1].replace("\t", "    ")
        out.append((n, raw[:110], n == lineno))
    return out

def _check_syntax(py: dict):
    issues = []
    order = sorted(py, key=lambda p: (p.rsplit("/", 1)[-1] != "main.py", p.count("/"), p))
    for path in order:
        if len(issues) >= 3:
            break
        try:
            ast.parse(py[path])
        except SyntaxError as e:
            lines = py[path].splitlines()
            lt = lines[e.lineno - 1] if e.lineno and 0 < e.lineno <= len(lines) else ""
            issues.append({
                "sev": "error", "id": "SYNTAX", "title": f"{e.__class__.__name__}: {e.msg}", "file": path, "line": e.lineno,
                "snippet": _snippet(py[path], e.lineno or 1) if e.lineno else None, "fix": _syntax_hint(e.msg, lt),
            })
        except Exception:
            pass
    return issues


# ------------------------------------------------------------------ টোকেন
def _uses_telegram(py: dict) -> bool:
    pat = re.compile(r"^\s*(?:import|from)\s+(?:[^\n#]*[\s,])?(" + "|".join(TG_LIBS) + r")\b", re.M | re.I)
    return any(pat.search(t) for t in py.values())

def _env_names_used(py: dict):
    names = set()
    for t in py.values():
        for m in re.finditer(r"""(?:os\.environ\s*\[\s*|os\.environ\.get\s*\(\s*|os\.getenv\s*\(\s*|environ\.get\s*\(\s*|getenv\s*\(\s*)['"]([A-Za-z0-9_]+)['"]""", t):
            names.add(m.group(1))
    return names

def _uses_env(py: dict) -> bool:
    return any(re.search(r"os\.environ|os\.getenv|\bgetenv\(|load_dotenv|dotenv_values", t) for t in py.values())

_TOKEN_VARS = {"token", "bot_token", "bottoken", "api_token", "apitoken", "telegram_token", "tg_token", "tgtoken",
               "bot_api_token", "telegram_bot_token", "bot_key", "bot_api"}

def _placeholder_token(py: dict):
    """টোকেন-নামের ভেরিয়েবলে ভুয়া/ফাঁকা মান (YOUR_TOKEN, 123456:ABC...) থাকলে (ফাইল, লাইন, মান)।"""
    pat = re.compile(r"""(?im)^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*['"]([^'"\n]{0,80})['"]""")
    for path, t in py.items():
        for m in pat.finditer(t):
            if m.group(1).lower() not in _TOKEN_VARS:
                continue
            val = m.group(2).strip()
            if TOKEN_RE.fullmatch(val):
                continue
            if val == "" or re.search(r"your|token|here|paste|xxxx|123456|abc|example|insert|enter|put|api[_ ]?key", val, re.I) or (":" not in val and len(val) < 25):
                return path, t.count("\n", 0, m.start()) + 1, (val or "(খালি)")
    return None

def mask_token(tok: str) -> str:
    return f"{tok[:4]}{'•' * 13}{tok[-4:]}" if tok and len(tok) > 10 else "•••"

def find_token(p_dir: str):
    """প্রজেক্টে প্রথম আসল-ধরনের টোকেন। রিটার্ন (token, file, line, snippet[টোকেন ঢাকা]) বা None।"""
    py, other = load_project(p_dir)
    order = sorted(py, key=lambda p: (p.rsplit("/", 1)[-1] != "main.py", p.count("/"), p))
    for path in order + sorted(other):
        t = py.get(path) or other.get(path)
        m = TOKEN_RE.search(t)
        if m:
            ln = t.count("\n", 0, m.start()) + 1
            snip = [(n, TOKEN_RE.sub(lambda mm: mask_token(mm.group(0)), txt), hit) for n, txt, hit in _snippet(t, ln, ctx=0)]
            return m.group(0), path, ln, snip
    return None

def check_token(py: dict, other: dict, env_keys: set, main_file: str):
    """রিটার্ন issue dict বা None।"""
    if not _uses_telegram(py):
        return None
    for t in list(py.values()) + list(other.values()):
        if TOKEN_RE.search(t):
            return None                                   # কোথাও না কোথাও আসল-ধরনের টোকেন আছে
    ph = _placeholder_token(py)
    checked = ", ".join(sorted({p.rsplit("/", 1)[-1] for p in py})[:5]) + (", .env/কনফিগ" if other else "") + ", Env Variables"
    if ph:
        path, line, val = ph
        return {"sev": "error", "id": "TOKEN_PLACEHOLDER", "title": "টোকেনের জায়গায় নমুনা/ফাঁকা লেখা আছে", "file": path, "line": line,
                "snippet": _snippet(py[path], line), "checked": checked, "bypass": False,
                "fix": f"এখানে আপনার আসল বট টোকেন বসান (@BotFather থেকে)। এখন লেখা আছে: {val}"}
    if _uses_env(py):
        names = sorted(_env_names_used(py))
        token_like = [n for n in names if re.search(r"token|bot|api|key", n, re.I)] or names
        set_keys = {k.upper() for k in env_keys}
        env_has_token = any(TOKEN_RE.search(v) for v in other.values())
        if any(n.upper() in set_keys for n in token_like) or env_has_token:
            return None
        return {"sev": "error", "id": "TOKEN_ENV_MISSING", "title": "টোকেন Environment Variable থেকে পড়ছে, কিন্তু সেট করা নেই",
                "file": None, "line": None, "snippet": None, "checked": checked, "bypass": True,
                "env_names": token_like[:4],
                "fix": "বটের 🔑 Env Variables-এ এই নামে টোকেন সেট করুন: " + (", ".join(token_like[:4]) or "BOT_TOKEN") + " — অথবা কোডেই টোকেন লিখুন।"}
    return {"sev": "error", "id": "NO_TOKEN", "title": "আপনার কোডে বট টোকেন পাওয়া যায়নি", "file": None, "line": None, "snippet": None,
            "checked": checked, "bypass": True,
            "fix": "টোকেন ছাড়া বট Telegram-এর সাথে যুক্ত হতে পারে না। কোডে বসান: BOT_TOKEN = \"123456789:ABC...\""}


# ------------------------------------------------------------------ সতর্কতা (বট চালু হলেও)
def _line_of(text: str, m) -> int:
    return text.count("\n", 0, m.start()) + 1

def check_warnings(py: dict, main_file: str):
    warns = []
    tg = _uses_telegram(py)
    joined = "\n".join(py.values())
    if tg:
        has_bot_obj = re.search(r"TeleBot\s*\(|Bot\s*\(|Application\.builder|ApplicationBuilder|Dispatcher\s*\(|Client\s*\(|TelegramClient\s*\(|Updater\s*\(", joined)
        has_loop = re.search(r"infinity_polling|\.polling\s*\(|run_polling|start_polling|executor\.start|run_until_complete|asyncio\.run|\.run_forever|\.idle\s*\(|\bidle\s*\(|serve_forever|\.run\s*\(|\.start\s*\(", joined)
        if has_bot_obj and not has_loop:
            warns.append({"sev": "warn", "id": "NO_POLLING", "title": "বট চালু রাখার লাইন (polling) পাওয়া যায়নি", "file": main_file, "line": None, "snippet": None,
                          "fix": "ফাইলের একদম শেষে বট চালু রাখার লাইন যোগ করুন, যেমন: bot.infinity_polling() — নাহলে বট চালু হয়েই বন্ধ হয়ে যাবে।"})
    for path, t in py.items():
        for m in re.finditer(r"(?m)^[^#\n]*(?<![\w.])input\s*\(", t):
            warns.append({"sev": "warn", "id": "INPUT_CALL", "title": "input() ব্যবহার করা হয়েছে", "file": path, "line": _line_of(t, m),
                          "snippet": _snippet(t, _line_of(t, m), ctx=0),
                          "fix": "সার্ভারে কিবোর্ড নেই, তাই input() এ বট আটকে যাবে/EOFError দেবে। মান কোডে বা Env Variables-এ রাখুন।"})
            break
        for m in re.finditer(r"(?m)^\s*(?:import|from)\s+(tkinter|pyautogui|pygame|PyQt\d|PySide\d|kivy|turtle|pynput)\b", t):
            warns.append({"sev": "warn", "id": "GUI_LIB", "title": f"ডেস্কটপ/GUI লাইব্রেরি ({m.group(1)})", "file": path, "line": _line_of(t, m),
                          "snippet": _snippet(t, _line_of(t, m), ctx=0),
                          "fix": "সার্ভারে স্ক্রিন/মাউস নেই, তাই এই লাইব্রেরি কাজ করবে না।"})
            break
        for m in re.finditer(r"""['"]r?[A-Za-z]:[\\/]{1,2}(?:Users|Program|Windows|Documents|Desktop)[^'"]*['"]""", t):
            warns.append({"sev": "warn", "id": "WIN_PATH", "title": "Windows ফাইল-পাথ আছে", "file": path, "line": _line_of(t, m),
                          "snippet": _snippet(t, _line_of(t, m), ctx=0),
                          "fix": "সার্ভারে C:\\... পাথ নেই। ফাইলটা প্রজেক্টে আপলোড করে শুধু নাম (যেমন \"data.json\") ব্যবহার করুন।"})
            break
        if re.search(r"google\.colab|from\s+IPython|get_ipython\(", t):
            warns.append({"sev": "warn", "id": "COLAB", "title": "Colab/Jupyter-এর কোড আছে", "file": path, "line": None, "snippet": None,
                          "fix": "google.colab ও IPython সার্ভারে নেই — ওই অংশ বাদ দিন।"})
            break
    return warns[:5]


def _local_imports_of(text: str):
    """একটা ফাইলের ইম্পোর্ট করা মডিউলের রুট নামগুলো।"""
    names = set()
    try:
        tree = ast.parse(text)
    except Exception:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            elif node.level:
                for a in node.names:
                    names.add(a.name.split(".")[0])
    return names

def _is_relevant_file(py: dict, main_file: str, path) -> bool:
    """এই ফাইলের সিনট্যাক্স এরর বট চালু হওয়া আটকাবে কিনা (মূল ফাইল, অথবা মূল ফাইল/তার ইম্পোর্টের চেইনে থাকা লোকাল মডিউল)।"""
    if not path:
        return True
    base = os.path.basename(main_file or "")
    if os.path.basename(path) == base or path == main_file:
        return True
    if main_file not in py and base not in {os.path.basename(k) for k in py}:
        return True                       # মূল ফাইল চেনা যাচ্ছে না — সাবধানে সবই ধরা হয়
    seen, stack = set(), [main_file if main_file in py else next((k for k in py if os.path.basename(k) == base), None)]
    reachable = set()
    while stack:
        cur = stack.pop()
        if not cur or cur in seen or cur not in py:
            continue
        seen.add(cur)
        for nm in _local_imports_of(py[cur]):
            for k in py:
                stem = k[:-3].replace("/", ".")
                if k == cur:
                    continue
                if stem == nm or stem.startswith(nm + ".") or os.path.basename(k)[:-3] == nm or k.split("/")[0] == nm:
                    reachable.add(k)
                    stack.append(k)
    return path in reachable

def preflight(p_dir: str, main_file: str, env_keys=()):
    """রিটার্ন (errors[], warnings[])। errors থাকলে ডিপ্লয় থামানো হয়।"""
    _extra_warns = []
    py, other = load_project(p_dir)
    if main_file not in py:
        # মেইন ফাইল প্রজেক্টের সাবফোল্ডারে/নেই — যা আছে তাই দিয়ে চলবে
        pass
    errors = _check_syntax(py)
    if errors:
        # ⭐ যেকোনো Py ফাইল চালানোর জন্য: যে ফাইলে সিনট্যাক্স এরর সেটা মূল ফাইল না হলে আর মূল ফাইল সেটা ইম্পোর্টও না করলে
        # (যেমন পুরনো/অব্যবহৃত ব্যাকআপ ফাইল) বট আটকানো হয় না — শুধু সতর্কতা।
        keep = [e for e in errors if _is_relevant_file(py, main_file, e.get("file"))]
        extra = [e for e in errors if e not in keep]
        errors = keep
        for e in extra:
            e2 = dict(e); e2["sev"] = "warn"; e2["id"] = "SYNTAX_UNUSED"
            e2["title"] = "অব্যবহৃত ফাইলে সিনট্যাক্স এরর (বট চলবে): " + str(e.get("title", ""))
            e2["fix"] = "মূল ফাইল এটা ইম্পোর্ট করে না, তাই বট চালানো হয়েছে। ফাইলটা দরকার হলে ঠিক করে নিন।"
            _extra_warns.append(e2)
    if not errors:
        tok = check_token(py, other, set(env_keys), main_file)
        if tok:
            errors.append(tok)
    warnings = (check_warnings(py, main_file) + _extra_warns) if not errors else []
    return errors, warnings


# ------------------------------------------------------------------ কার্ড রেন্ডার
def _render_snippet(snip) -> str:
    if not snip:
        return ""
    rows = []
    for n, txt, hit in snip:
        rows.append(f"{'➜' if hit else ' '} {n:>4} │ {txt}")
    return "<pre>" + _e("\n".join(rows)) + "</pre>"

def render_preflight_card(errors: list, file_name: str) -> str:
    first = errors[0]
    if first["id"] in ("NO_TOKEN", "TOKEN_ENV_MISSING", "TOKEN_PLACEHOLDER"):
        head = "╭━━〔 🔑 TOKEN PROBLEM 〕━━╮"
    elif first["id"] == "INVALID_TOKEN":
        head = "╭━━〔 🚫 INVALID TOKEN 〕━━╮"
    else:
        head = "╭━━〔 🩺 CODE DOCTOR 〕━━╮"
    out = [head, f"│ 📄 ফাইল: <code>{_e(file_name)}</code>", f"│ ⛔ {len(errors)}টি সমস্যা — বট চালানো হয়নি", "╰━━━━━━━━━━━━━━━━━━━━╯"]
    for i, it in enumerate(errors[:3], 1):
        out.append("")
        out.append(f"<b>{i}️⃣ ❌ {_e(it['title'])}</b>")
        if it.get("file"):
            out.append(f"📍 <code>{_e(it['file'])}</code>" + (f" • লাইন <b>{it['line']}</b>" if it.get("line") else ""))
        if it.get("snippet"):
            out.append(_render_snippet(it["snippet"]))
        if it.get("checked"):
            out.append(f"🔍 যা যা দেখা হয়েছে: <i>{_e(it['checked'])}</i>")
        out.append(f"💡 <b>সমাধান:</b> {_e(it['fix'])}")
    if first["id"] in ("NO_TOKEN",):
        out += ["", "🪪 <b>টোকেন কীভাবে পাবেন?</b>", "@BotFather → <code>/newbot</code> → নাম দিন → যে টোকেন দেবে সেটা কপি করুন।"]
    out += ["", "✏️ ঠিক করে ফাইলটি <b>আবার পাঠান</b> — একই স্লটে ডিপ্লয় হবে।"]
    return "\n".join(out)[:4000]

def render_warning_card(warns: list, bot_name: str) -> str:
    out = ["╭━━〔 ⚠️ CODE HEALTH 〕━━╮", f"│ 🤖 {_e(bot_name)}", "│ বট চালু হয়েছে, তবে কিছু বিষয় দেখুন", "╰━━━━━━━━━━━━━━━━━━━━╯"]
    for it in warns[:4]:
        out.append("")
        out.append(f"⚠️ <b>{_e(it['title'])}</b>")
        if it.get("file") and it.get("line"):
            out.append(f"📍 <code>{_e(it['file'])}</code> • লাইন {it['line']}")
        if it.get("snippet"):
            out.append(_render_snippet(it["snippet"]))
        out.append(f"💡 {_e(it['fix'])}")
    return "\n".join(out)[:4000]


# ------------------------------------------------------------------ ক্র্যাশ ডায়াগনোসিস (Traceback)
_ERR_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)|[A-Za-z_][\w.]*\.[A-Za-z_]*Exception)\s*(?::\s*(.*))?$")
_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)(?:, in (.+))?$')

_HINTS = [
    (r"ModuleNotFoundError|ImportError: No module", "একটি প্যাকেজ ইনস্টল নেই বা নামে ভুল আছে। requirements.txt ফাইলে প্যাকেজের নাম লিখে সাথে পাঠান।"),
    (r"cannot import name", "যে নামটা ইমপোর্ট করছেন সেটা ওই লাইব্রেরির এই ভার্সনে নেই — ভার্সন মিলছে না বা নামে ভুল আছে।"),
    (r"401|Unauthorized", "বট টোকেন ভুল বা @BotFather থেকে বাতিল (revoke) করা হয়েছে। নতুন টোকেন দিন।"),
    (r"409|Conflict|terminated by other getUpdates", "একই টোকেনে বটটা আরেক জায়গায়ও চলছে। অন্য জায়গার বটটা বন্ধ করুন।"),
    (r"403|Forbidden|bot was blocked", "ইউজার বটটাকে ব্লক করেছে বা বটকে এই চ্যাটে পারমিশন নেই। এরর ধরার জন্য try/except ব্যবহার করুন।"),
    (r"chat not found|user not found", "চ্যাট আইডি ভুল, অথবা ইউজার আগে বটকে /start দেয়নি।"),
    (r"429|Too Many Requests|retry after", "Telegram-এ অনেক বেশি দ্রুত মেসেজ পাঠানো হচ্ছে। মেসেজের মাঝে time.sleep দিন।"),
    (r"message is too long", "মেসেজ ৪০৯৬ অক্ষরের বেশি — ভেঙে ভেঙে পাঠান।"),
    (r"can't parse entities", "HTML/Markdown ফরম্যাটে ভুল (< > & বা _ * বন্ধ হয়নি)। parse_mode সরান বা html.escape করুন।"),
    (r"NameError", "এমন একটা নাম ব্যবহার হয়েছে যা আগে তৈরি/ইমপোর্ট করা হয়নি — বানান বা import দেখুন।"),
    (r"UnboundLocalError", "ফাংশনের ভেতরে ভেরিয়েবলে মান বসানোর আগেই সেটা ব্যবহার হয়েছে।"),
    (r"AttributeError.*NoneType", "কিছু একটা None এসেছে (যেমন ডাটা পাওয়া যায়নি) আর তার ওপর কাজ করা হচ্ছে। আগে if x: দিয়ে চেক করুন।"),
    (r"AttributeError", "ওই অবজেক্টে এই নামে কিছু নেই — বানান বা লাইব্রেরির ভার্সন মিলিয়ে দেখুন।"),
    (r"KeyError", "ডিকশনারিতে এই key নেই। .get(\"key\") ব্যবহার করুন বা আগে চেক করুন।"),
    (r"IndexError", "লিস্টের যতগুলো আইটেম আছে তার বাইরের নম্বর চাওয়া হয়েছে।"),
    (r"TypeError", "ভুল ধরনের মান দেওয়া হয়েছে (যেমন সংখ্যা + লেখা) বা ফাংশনে আর্গুমেন্ট কমবেশি।"),
    (r"ValueError", "মানটা প্রত্যাশিত ফরম্যাটে নেই (যেমন লেখাকে int() করা)।"),
    (r"ZeroDivisionError", "০ দিয়ে ভাগ করা হয়েছে।"),
    (r"FileNotFoundError|No such file", "কোড যে ফাইলটা খুঁজছে সেটা প্রজেক্টে নেই। ফাইলটা আপলোড করুন এবং শুধু নাম (পাথ ছাড়া) ব্যবহার করুন।"),
    (r"PermissionError", "এই ফাইল/ফোল্ডারে লেখার অনুমতি নেই — প্রজেক্টের নিজের ফোল্ডারের ভেতরে লিখুন।"),
    (r"JSONDecodeError", "JSON ফাইল বা ডাটা ভাঙা/খালি।"),
    (r"sqlite3\.|OperationalError", "ডাটাবেস সমস্যা — টেবিল তৈরি হয়নি, বা ফাইল লক/ভাঙা।"),
    (r"EOFError", "input() ব্যবহার হয়েছে, সার্ভারে কিবোর্ড নেই। input() বাদ দিন।"),
    (r"UnicodeDecodeError|UnicodeEncodeError", "ফাইল/লেখার এনকোডিং সমস্যা — open(..., encoding=\"utf-8\") ব্যবহার করুন।"),
    (r"RecursionError", "ফাংশন নিজেকে অসীমবার ডাকছে (শেষ হচ্ছে না)।"),
    (r"MemoryError|Killed", "RAM লিমিট পেরিয়ে গেছে। বড় ডাটা একসাথে লোড না করে অংশে অংশে নিন।"),
    (r"ConnectionError|TimeoutError|ReadTimeout|ConnectTimeout|SSLError|NewConnectionError|gaierror|Max retries", "ইন্টারনেট/সার্ভার সংযোগে সমস্যা। requests-এ timeout দিন এবং try/except দিয়ে আবার চেষ্টা করান।"),
    (r"IndentationError|TabError", "ইন্ডেন্টেশন (শুরুর স্পেস) ঠিক নেই।"),
    (r"SyntaxError", "কোডের ব্যাকরণ ভুল।"),
]

def diagnose_traceback(tail: str, p_dir: str = None):
    """লগের শেষ অংশ থেকে ডায়াগনোসিস। কিছু না পেলে None।"""
    if not tail:
        return None
    lines = [l.rstrip() for l in tail.splitlines()]
    err_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _ERR_LINE.match(lines[i].strip()):
            err_idx = i
            break
    if err_idx is None:
        return None
    m = _ERR_LINE.match(lines[err_idx].strip())
    etype, emsg = m.group(1), (m.group(2) or "").strip()
    # চেইন করা এরর ("During handling...") হলে শেষের এররটাই আসল
    frames = []
    for l in lines[:err_idx]:
        fm = _FRAME.match(l)
        if fm:
            frames.append((fm.group(1), int(fm.group(2)), fm.group(3) or ""))
    chosen = None
    if p_dir:
        ap = os.path.abspath(p_dir)
        for fr in reversed(frames):
            if os.path.abspath(fr[0]).startswith(ap + os.sep):
                chosen = fr
                break
    if not chosen and frames:
        chosen = frames[-1]
    diag = {"type": etype, "message": emsg[:300], "file": None, "line": None, "func": "", "snippet": None, "in_project": False}
    if chosen:
        path, ln, func = chosen
        diag.update(line=ln, func=func)
        in_proj = bool(p_dir and os.path.abspath(path).startswith(os.path.abspath(p_dir) + os.sep))
        diag["in_project"] = in_proj
        diag["file"] = os.path.relpath(path, p_dir).replace(os.sep, "/") if in_proj else os.path.basename(path) + " (লাইব্রেরি)"
        if in_proj:
            try:
                with open(path, "rb") as f:
                    text = f.read(MAX_FILE * 2).decode("utf-8", errors="replace")
                diag["snippet"] = _snippet(text, ln)
            except Exception:
                pass
    full = f"{etype}: {emsg}"
    diag["hint"] = next((h for pat, h in _HINTS if re.search(pat, full)), "লগের এরর মেসেজটা দেখে কোডের ওই লাইনটা ঠিক করুন।")
    return diag

def render_crash_card(diag: dict, bot_name: str, attempt: int = None, final: bool = False, exit_code=None) -> str:
    head = "╭━━〔 🚨 BOT STOPPED 〕━━╮" if final else "╭━━〔 🩺 CODE DOCTOR 〕━━╮"
    out = [head, f"│ 🤖 {_e(bot_name)}"]
    if final:
        out.append("│ ❌ ৩ বার অটো-রিস্টার্টের পরও চালু থাকেনি" + (f" (exit {exit_code})" if exit_code is not None else ""))
    elif attempt:
        out.append(f"│ 🔁 বারবার বন্ধ হচ্ছে — অটো-রিস্টার্ট {attempt}/3")
    out.append("╰━━━━━━━━━━━━━━━━━━━━╯")
    out.append("")
    out.append(f"❌ <b>{_e(diag['type'])}</b>" + (f"\n<code>{_e(diag['message'])}</code>" if diag["message"] else ""))
    if diag["file"]:
        where = f"📍 <code>{_e(diag['file'])}</code>" + (f" • লাইন <b>{diag['line']}</b>" if diag["line"] else "") + (f" • <code>{_e(diag['func'])}</code>" if diag["func"] and diag["func"] != "<module>" else "")
        out.append(where)
    if diag["snippet"]:
        out.append(_render_snippet(diag["snippet"]))
    elif diag["file"] and not diag["in_project"]:
        out.append("<i>সমস্যাটা লাইব্রেরির ভেতরে ধরা পড়েছে — সাধারণত আপনার কোড ভুল ডাটা/আর্গুমেন্ট দিলে এমন হয়।</i>")
    out.append(f"💡 <b>কারণ ও সমাধান:</b> {_e(diag['hint'])}")
    out.append("")
    out.append("👉 ঠিক করে ফাইল আবার পাঠান, অথবা <b>🎛️ Control Panel</b> থেকে Start দিন।" if final else "ℹ️ বট নিজে থেকে আবার চেষ্টা করছে।")
    return "\n".join(out)[:4000]
