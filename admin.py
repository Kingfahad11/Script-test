import os
import re
import sys
import time
import json
import psutil
import shutil
import sqlite3
import hashlib
import zipfile
import threading
import subprocess
from datetime import datetime, timedelta
from cryptography.fernet import Fernet, InvalidToken

# ======================== ডিরেক্টরি ও পাথ কনফিগারেশন ========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pyhost_system.db")
PROJECTS_DIR = os.path.join(BASE_DIR, "user_projects")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)

# অ্যাডমিন টেলিগ্রাম আইডি তালিকা
# ENV VAR সাপোর্ট: চাইলে ".env"-এ ADMIN_IDS="5504272381,111111111" দিয়ে একাধিক অ্যাডমিন যোগ করা যাবে।
# ENV সেট না থাকলে আগের মতোই ডিফল্ট আইডি ব্যবহার হবে (কিছু ভাঙবে না)।
_DEFAULT_ADMIN_IDS = [5504272381]
_env_admin_ids = os.environ.get("ADMIN_IDS", "").strip()
if _env_admin_ids:
    try:
        ADMIN_IDS = [int(x.strip()) for x in _env_admin_ids.split(",") if x.strip()]
    except Exception:
        ADMIN_IDS = _DEFAULT_ADMIN_IDS
else:
    ADMIN_IDS = _DEFAULT_ADMIN_IDS

# পার্মানেন্ট এনক্রিপশন কী (Fernet)
# ENV VAR সাপোর্ট: ENCRYPTION_KEY সেট থাকলে সেটি ব্যবহার হবে, নাহলে আগের স্ট্যাটিক কী (ব্যাকওয়ার্ড কম্প্যাটিবল)।
STATIC_KEY = os.environ.get("ENCRYPTION_KEY", "a8StDhnfpthgsr89cJ4l6tuLgOmZY0IIPDqWFkkRbHA=").encode("utf-8")
try:
    cipher_suite = Fernet(STATIC_KEY)
except Exception:
    cipher_suite = Fernet(Fernet.generate_key())

# সিস্টেম সেটিংস ও টিয়ার লিমিট
SYSTEM_CONFIG = {
    "maintenance_mode": False,
    "free_hosting_enabled": True,          # 🆓 অ্যাডমিন এখান থেকে ফ্রি হোস্টিং সিস্টেম অফ/অন করতে পারবেন
    "free_hosting_hours": 12,              # 🆓 প্রতি ফ্রি সেশনের মেয়াদ (ঘণ্টা) — অ্যাডমিন প্যানেল থেকে 3/6/9/12 সেট করা যায়
    "community_commission": 1.0,           # 🌐 কমিউনিটি বট অন্য কেউ ডেপ্লয় করলে প্রস্তুতকারক যত Credits পাবে (প্রতি ইউনিক ডেপ্লয়ারে)
    "boot_log": "",                        # 🛡 শেষ কয়েকটা সার্ভার-স্টার্টের সময় (ক্র্যাশ-লুপ ধরার জন্য)
    "last_boot_notice_ts": 0,              # 🛡 অ্যাডমিনকে 'রিস্টোর/রিস্টার্ট' মেসেজ শেষ কখন গেছে (স্প্যাম ঠেকাতে)
    "last_ram_notice_ts": 0,               # 🛡 'RAM ভরে গেছে' অ্যালার্ট শেষ কখন গেছে
    "vip_unlock_slots": 10,                # 🔓 VIP Unlock চলাকালে কয়টি স্লট (১–MAX_BOT_SLOTS) আনলক থাকবে — অ্যাডমিন ঠিক করে
    "vip_unlock_until": "",                # 🔓 VIP All Slot Unlock ইভেন্ট কখন শেষ হবে ("YYYY-MM-DD HH:MM:SS"; খালি = বন্ধ)
    "max_cpu_per_process": 80.0,
    "max_ram_mb_per_process": 256,
    "allow_new_deployments": True,
    "free_bot_limit": 2,                   # ফ্রি ইউজার সর্বোচ্চ ২টি বট
    "pro_bot_limit": 1,                    # প্রো প্ল্যান ১টি বট
    "premium_bot_limit": 3,                # প্রিমিয়াম প্ল্যান ৩টি বট
    "business_2m_bot_limit": 4,            # ⭐ Business Boss — 2 মাস (60D) প্যাকেজে ৪টি বট
    "business_6m_bot_limit": 7,            # ⭐ Business Boss — 6 মাস (180D) প্যাকেজে ৭টি বট
    "business_yearly_bot_limit": 10,       # ⭐ Business Yearly — 12 মাস (365D) প্যাকেজে ১০টি বট
    "business_bot_limit": 10,              # লিগ্যাসি/ম্যানুয়াল অ্যাডমিন BUSINESS সেট (fallback) ১০টি বট
    "referral_target": 3,                  # কতটি রেফারে বোনাস
    "referral_bonus_days": 3               # কত দিনের VIP বোনাস
}

# 💎 Upgrade Plan — ৫-টিয়ার প্যাকেজ বিস্তারিত (মেয়াদ অনুযায়ী: 7 দিন / 30 দিন / 2 মাস / 6 মাস / 12 মাস)
# দাম/Sold পাল্টাতে চাইলে সরাসরি নিচের সংখ্যাগুলো এডিট করে বট রিস্টার্ট করলেই হবে।
TIER_PLANS = {
    "PRO": {
        "name": "⚡ Pro Plan",
        "title": "Pro",
        "period_bn": "7 দিন",
        "days": 7,
        "price_num": 80,
        "price": "80 BDT",
        "bot_limit": SYSTEM_CONFIG["pro_bot_limit"],
        "sold": 2,
        "sold_total": 10,
        "pro": False,
        "icon": "⚡",
        "features": "• 1 Bot Active\n• 24/7 Running • Fast Boot\n• 100% Ad-Free • Auto-Restart"
    },
    "PREMIUM": {
        "name": "🌟 Premium Plan",
        "title": "Premium",
        "period_bn": "30 দিন",
        "days": 30,
        "price_num": 150,
        "price": "150 BDT",
        "bot_limit": SYSTEM_CONFIG["premium_bot_limit"],
        "sold": 4,
        "sold_total": 10,
        "pro": False,
        "icon": "🌟",
        "features": "• 3 Bots + Automation\n• 24/7 Non-Stop • Auto-Restart Watchdog\n• 1-Click Backup Shield • Fast NVMe"
    },
    "BUSINESS": {
        "name": "👑 Business Boss",
        "title": "Business Boss",
        "period_bn": "2 মাস",
        "days": 60,
        "price_num": 220,
        "price": "220 BDT",
        "bot_limit": SYSTEM_CONFIG["business_2m_bot_limit"],
        "sold": 3,
        "sold_total": 10,
        "pro": True,
        "icon": "👑",
        "features": "• 4 Bots + Full Automation\n• 24/7 Non-Stop • Max RAM Resource\n• Live Logs • VIP Priority Support"
    },
    "BUSINESS6M": {
        "name": "🔥 Business Boss",
        "title": "Business Boss (6M)",
        "period_bn": "6 মাস",
        "days": 180,
        "price_num": 350,
        "price": "350 BDT",
        "bot_limit": SYSTEM_CONFIG["business_6m_bot_limit"],
        "sold": 7,
        "sold_total": 10,
        "pro": True,
        "icon": "🔥",
        "features": (
            "• 7 Bots + Full Automation\n• 24/7 Non-Stop • Max RAM Resource\n"
            "• Live Logs • VIP Priority Support\n"
            "• 🎁 ৬ মাসের জন্য বাড়তি ছাড়"
        )
    },
    "YEARLY": {
        "name": "🏆 Business Yearly",
        "title": "Business Yearly",
        "period_bn": "12 মাস",
        "days": 365,
        "price_num": 600,
        "price": "600 BDT",
        "bot_limit": SYSTEM_CONFIG["business_yearly_bot_limit"],
        "sold": 10,
        "sold_total": 10,
        "pro": True,
        "icon": "🏆",
        "features": (
            "• 10 Bots + Full Automation\n• 24/7 Non-Stop • Max RAM Resource\n"
            "• Live Logs • VIP Priority Support\n"
            "• 🎁 সারা বছরের জন্য বিশাল ছাড়\n"
            "• 🏆 Yearly Exclusive Badge"
        )
    }
}

# মোট ফিক্সড বট স্লট সংখ্যা (Control Panel-এ সবসময় ১-১০ পর্যন্ত দেখানো হবে)
MAX_BOT_SLOTS = 10

# প্রতিটি বট স্লট আলাদাভাবে মেয়াদ বাড়ানোর (Extend Validity) প্যাকেজসমূহ
# "sold" আর "sold_total" মিলিয়ে "Sold: X/Y" ফরম্যাটে দেখানো হয় — দাম/sold পাল্টাতে চাইলে
# সরাসরি নিচের সংখ্যাগুলো এডিট করে বট রিস্টার্ট করলেই হবে।
SLOT_EXTEND_PLANS = {
    "7D":   {"label": "7 দিন",   "days": 7,   "price_num": 60,  "price": "60 BDT",  "sold": 2,  "sold_total": 10, "pro": False, "icon": "⚡"},
    "30D":  {"label": "30 দিন",  "days": 30,  "price_num": 120, "price": "120 BDT", "sold": 4,  "sold_total": 10, "pro": False, "icon": "🌟"},
    "60D":  {"label": "2 মাস",   "days": 60,  "price_num": 180, "price": "180 BDT", "sold": 3,  "sold_total": 10, "pro": False, "icon": "👑"},
    "180D": {"label": "6 মাস",   "days": 180, "price_num": 300, "price": "300 BDT", "sold": 10, "sold_total": 10, "pro": True,  "icon": "🔥"},
    "365D": {"label": "12 মাস",  "days": 365, "price_num": 500, "price": "500 BDT", "sold": 10, "sold_total": 10, "pro": True,  "icon": "🏆"},
}


# পেমেন্ট মেথড অ্যাকাউন্ট তথ্য
PAYMENT_GATEWAYS = {
    "bkash": {
        "name": "bKash (Personal)",
        "number": "01700000000",
        "type": "Send Money"
    },
    "nagad": {
        "name": "Nagad (Personal)",
        "number": "01700000000",
        "type": "Send Money"
    },
    "crypto": {
        "name": "Crypto Pay",
        "binance_id": "123456789",
        "usdt_trc20": "TYDzsxd8Vp4...EXAMPLE...TRC20"
    }
}

# অ্যাক্টিভ ডিসকাউন্ট কুপনস
COUPONS = {
    "PYHOST20": 20,    # ২০% ডিসকাউন্ট
    "WELCOME50": 50,   # ৫০% ডিসকাউন্ট
    "SPECIAL10": 10    # ১০% ডিসকাউন্ট
}

# ======================== ডাটাবেজ অপ্টিমাইজেশন (WAL Mode) ========================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_admin_database():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # ইউজার টেবিল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            plan TEXT DEFAULT 'FREE',
            plan_expiry TEXT DEFAULT 'LIFETIME',
            joined_date TEXT,
            last_active TEXT,
            total_deploys INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            referred_by INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0,
            referral_claimed INTEGER DEFAULT 0,
            credits REAL DEFAULT 0.0,
            referral_credits REAL DEFAULT 0.0,
            expiry_alert_24h INTEGER DEFAULT 0,
            expiry_alert_2h INTEGER DEFAULT 0,
            expiry_alert_3d INTEGER DEFAULT 0
        )
    """)
    
    cur.execute("PRAGMA table_info(users)")
    cols = [col[1] for col in cur.fetchall()]
    if "referred_by" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT 0")
    if "referral_count" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0")
    if "referral_claimed" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN referral_claimed INTEGER DEFAULT 0")
    if "credits" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN credits REAL DEFAULT 0.0")
    if "referral_credits" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN referral_credits REAL DEFAULT 0.0")
    if "expiry_alert_24h" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN expiry_alert_24h INTEGER DEFAULT 0")
    if "expiry_alert_2h" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN expiry_alert_2h INTEGER DEFAULT 0")
    if "expiry_alert_3d" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN expiry_alert_3d INTEGER DEFAULT 0")
    if "plan_tier" not in cols:
        # ⭐ FIX: শুধু 'plan' (PRO/PREMIUM/BUSINESS/VIP) কলাম দিয়ে বট-স্লট লিমিট ঠিক করা হলে
        # 2 মাস (BUSINESS) / 6 মাস (BUSINESS6M) / 12 মাস (YEARLY) — তিনটা আলাদা প্যাকেজই
        # একই "BUSINESS" ভ্যালুতে সেভ হয়ে যেত, ফলে সবগুলো একই (business_bot_limit=10) স্লট
        # লিমিট পেত। এই নতুন কলামে ঠিক কোন TIER_PLANS-কী (PRO/PREMIUM/BUSINESS/BUSINESS6M/YEARLY)
        # কেনা হয়েছে তা আলাদাভাবে সেভ থাকে, যাতে সঠিক bot_limit (4 / 7 / 10) হিসাব করা যায়।
        cur.execute("ALTER TABLE users ADD COLUMN plan_tier TEXT")
    
    # মাল্টি-প্রজেক্ট টেবিল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            project_name TEXT,
            main_file TEXT DEFAULT 'main.py',
            status TEXT DEFAULT 'STOPPED',
            created_at TEXT,
            last_run TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # প্রজেক্ট টেবিলে স্লট নম্বর ও মেয়াদ (expiry) কলাম যোগ (ব্যাকওয়ার্ড কম্প্যাটিবল মাইগ্রেশন)
    cur.execute("PRAGMA table_info(projects)")
    proj_cols = [col[1] for col in cur.fetchall()]
    if "slot_number" not in proj_cols:
        cur.execute("ALTER TABLE projects ADD COLUMN slot_number INTEGER")
    if "expiry_date" not in proj_cols:
        cur.execute("ALTER TABLE projects ADD COLUMN expiry_date TEXT DEFAULT 'LIFETIME'")
    if "github_owner" not in proj_cols:
        cur.execute("ALTER TABLE projects ADD COLUMN github_owner TEXT DEFAULT NULL")
    if "github_repo" not in proj_cols:
        cur.execute("ALTER TABLE projects ADD COLUMN github_repo TEXT DEFAULT NULL")
    if "github_is_private" not in proj_cols:
        cur.execute("ALTER TABLE projects ADD COLUMN github_is_private INTEGER DEFAULT 0")
    if "bot_notes" not in proj_cols:
        cur.execute("ALTER TABLE projects ADD COLUMN bot_notes TEXT DEFAULT NULL")

    # ইউজার টেবিলে onboarding ও checklist ট্র্যাকিং কলাম
    cur.execute("PRAGMA table_info(users)")
    user_cols = [col[1] for col in cur.fetchall()]
    if "onboarding_done" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN onboarding_done INTEGER DEFAULT 0")
    if "checklist_flags" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN checklist_flags TEXT DEFAULT '{}'"  )
    if "daily_report_time" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN daily_report_time TEXT DEFAULT NULL")
    if "language" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'bn'")
    if "total_uptime_seconds" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN total_uptime_seconds INTEGER DEFAULT 0")

    # প্রজেক্ট টেবিলে শিডিউল্ড স্টার্ট/স্টপ ও টাইমজোন কলাম
    cur.execute("PRAGMA table_info(projects)")
    proj_cols2 = [col[1] for col in cur.fetchall()]
    if "sched_start_time" not in proj_cols2:
        cur.execute("ALTER TABLE projects ADD COLUMN sched_start_time TEXT DEFAULT NULL")
    if "sched_stop_time" not in proj_cols2:
        cur.execute("ALTER TABLE projects ADD COLUMN sched_stop_time TEXT DEFAULT NULL")
    if "sched_enabled" not in proj_cols2:
        cur.execute("ALTER TABLE projects ADD COLUMN sched_enabled INTEGER DEFAULT 0")
    if "sched_last_action" not in proj_cols2:
        cur.execute("ALTER TABLE projects ADD COLUMN sched_last_action TEXT DEFAULT NULL")

    # ======================== ব্যাচ ৩ — নতুন ফিচারের টেবিল ========================
    # অ্যাচিভমেন্ট ব্যাজ
    cur.execute("""
        CREATE TABLE IF NOT EXISTS badges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            badge_key TEXT NOT NULL,
            earned_at TEXT,
            UNIQUE(user_id, badge_key)
        )
    """)
    # ফিডব্যাক / রেটিং
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT
        )
    """)
    # আপটাইম / রেফারেল মাইলস্টোন অ্যালার্ট — একবারই পাঠানো নিশ্চিত করতে
    cur.execute("""
        CREATE TABLE IF NOT EXISTS milestone_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            milestone TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE(project_id, milestone)
        )
    """)
    # অটো-ব্রডকাস্ট শিডিউল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            send_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT,
            sent_count INTEGER DEFAULT 0
        )
    """)

    # পুরনো প্রজেক্টগুলোর জন্য স্লট নম্বর ব্যাকফিল (যাদের slot_number এখনো খালি)
    cur.execute("SELECT DISTINCT user_id FROM projects WHERE slot_number IS NULL")
    users_needing_slots = [r["user_id"] for r in cur.fetchall()]
    for uid in users_needing_slots:
        cur.execute("SELECT project_id FROM projects WHERE user_id = ? ORDER BY project_id ASC", (uid,))
        rows = cur.fetchall()
        cur.execute("SELECT slot_number FROM projects WHERE user_id = ? AND slot_number IS NOT NULL", (uid,))
        used_slots = {r["slot_number"] for r in cur.fetchall()}
        next_slot = 1
        for row in rows:
            pid = row["project_id"]
            cur.execute("SELECT slot_number FROM projects WHERE project_id = ?", (pid,))
            existing = cur.fetchone()["slot_number"]
            if existing:
                continue
            while next_slot in used_slots and next_slot <= MAX_BOT_SLOTS:
                next_slot += 1
            if next_slot > MAX_BOT_SLOTS:
                break
            cur.execute("UPDATE projects SET slot_number = ? WHERE project_id = ?", (next_slot, pid))
            used_slots.add(next_slot)
            next_slot += 1

    # ভিআইপি পেমেন্ট রিকোয়েস্ট টেবিল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            plan_name TEXT,
            days INTEGER,
            price TEXT,
            gateway TEXT,
            trx_id TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT
        )
    """)
    
    # স্কিমা চেক
    cur.execute("PRAGMA table_info(vip_requests)")
    req_cols = [col[1] for col in cur.fetchall()]
    if "gateway" not in req_cols:
        cur.execute("ALTER TABLE vip_requests ADD COLUMN gateway TEXT DEFAULT 'bKash'")
    if "tier_key" not in req_cols:
        # ⭐ FIX: আগে অ্যাডমিন অ্যাপ্রুভ করার সময় শুধু plan_name (যেমন "🔥 Business Boss")
        # স্ট্রিং-এর ভিতর "Business" শব্দ খুঁজে প্ল্যান বসানো হতো — এতে 2 মাস (BUSINESS),
        # 6 মাস (BUSINESS6M) ও 12 মাস (YEARLY) তিনটা প্যাকেজই একরকম ধরা হতো, ফলে ভুল
        # bot_limit সেট হতো। এখন আসল TIER_PLANS-কী সরাসরি সেভ রাখা হচ্ছে যাতে অ্যাপ্রুভের
        # সময় সঠিক প্যাকেজ শনাক্ত করা যায়।
        cur.execute("ALTER TABLE vip_requests ADD COLUMN tier_key TEXT")

    # বট স্লট মেয়াদ বৃদ্ধি (Extend Validity) রিকোয়েস্ট টেবিল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS renewal_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            project_id INTEGER,
            project_name TEXT,
            days INTEGER,
            price TEXT,
            gateway TEXT,
            trx_id TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT
        )
    """)

    # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — আগে "Contact Admin" শুধু অ্যাডমিনের ব্যক্তিগত DM-এ সরাসরি
    # লিংক দিত (কোনো ট্র্যাকিং/স্ট্যাটাস ছিল না)। এখন প্রতিটি সমস্যা একটা ট্র্যাকযোগ্য টিকিটে
    # পরিণত হয়, যার একটা স্ট্যাটাস (OPEN/REPLIED/CLOSED) ও মেসেজ থ্রেড থাকে — ইউজার ও
    # অ্যাডমিন দুজনেই বটের ভিতর থেকেই পুরো কথোপকথনের ইতিহাস দেখতে পারে।
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            subject TEXT,
            status TEXT DEFAULT 'OPEN',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            sender TEXT,
            message TEXT,
            created_at TEXT
        )
    """)

    # অ্যাডমিন অডিট লগ টেবিল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            timestamp TEXT
        )
    """)

    # ব্ল্যাকলিস্টেড টোকেন টেবিল
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blacklisted_tokens (
            token_hash TEXT PRIMARY KEY,
            added_at TEXT,
            reason TEXT
        )
    """)

    # Deploy History — প্রতি আপলোডে স্বয়ংক্রিয় স্ন্যাপশট, সর্বোচ্চ ১০ ভার্সন
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deploy_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            snapshot_dir TEXT NOT NULL,
            main_file TEXT,
            note TEXT,
            created_at TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(project_id)
        )
    """)
    # Env Vars — প্রতি প্রজেক্টের .env ভেরিয়েবল এনক্রিপ্টেড সেভ
    cur.execute("""
        CREATE TABLE IF NOT EXISTS env_vars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value_encrypted TEXT NOT NULL,
            created_at TEXT,
            UNIQUE(project_id, key),
            FOREIGN KEY (project_id) REFERENCES projects(project_id)
        )
    """)

    # কুপন টেবিল — আগে hardcoded dict ছিল, এখন DB-তে; অ্যাডমিন প্যানেল থেকে যোগ/বাতিল করা যাবে
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coupons (
            code TEXT PRIMARY KEY,
            discount_percent INTEGER NOT NULL,
            max_uses INTEGER DEFAULT 0,
            used_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coupon_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            used_at TEXT,
            UNIQUE(code, user_id)
        )
    """)
    # পুরনো hardcoded কুপনগুলো DB-তে মাইগ্রেট করা (যদি না থাকে)
    default_coupons = [("PYHOST20", 20), ("WELCOME50", 50), ("SPECIAL10", 10)]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for code, disc in default_coupons:
        cur.execute("INSERT OR IGNORE INTO coupons (code, discount_percent, max_uses, created_at) VALUES (?, ?, 0, ?)", (code, disc, now_str))

    # 🎁 Redeem Code সিস্টেম — অ্যাডমিন প্যাক (প্ল্যান + মেয়াদ) বানিয়ে কোড জেনারেট করবে,
    # ইউজার সেই কোড রিডিম করলে সরাসরি প্ল্যান অ্যাক্টিভ হবে (ডিসকাউন্ট কুপন থেকে সম্পূর্ণ আলাদা)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            tier_key TEXT NOT NULL,
            plan TEXT NOT NULL,
            days INTEGER NOT NULL,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS redeem_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            used_at TEXT,
            UNIQUE(code, user_id)
        )
    """)

    # ⚙️ সিস্টেম টগল (যেমন: ফ্রি হোস্টিং অন/অফ) — রিস্টার্টের পরও মনে রাখার জন্য DB-তে সেভ থাকে
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # 📡 চ্যানেলে পাঠানো কোড-কপির ইনডেক্স (সার্ভার রিসেটের পর ফাইল ফেরত আনার জন্য)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS code_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            project_name TEXT,
            file_name TEXT NOT NULL,
            size INTEGER DEFAULT 0,
            file_id TEXT NOT NULL,
            msg_id INTEGER,
            source TEXT,
            created_at TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_code_backups_proj ON code_backups (project_id, file_name)")
    try:    # অডিট-কার্ড আলাদা মেসেজে গেলে তার আইডি (Channel Clean-এ কার্ডসহ মুছতে)
        cur.execute("ALTER TABLE code_backups ADD COLUMN card_msg_id INTEGER")
    except Exception:
        pass

    # 🌐 কমিউনিটি মার্কেটপ্লেস — ইউজারদের জমা দেওয়া বট টেমপ্লেট (অ্যাডমিন রিভিউ করে অ্যাপ্রুভ করলে লাইভ হয়)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS community_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            creator_name TEXT,
            name TEXT NOT NULL,
            description TEXT,
            code TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            reject_reason TEXT,
            deploys INTEGER DEFAULT 0,
            earned REAL DEFAULT 0.0,
            created_at TEXT,
            reviewed_at TEXT
        )
    """)
    # প্রতিটি (টেমপ্লেট, ডেপ্লয়ার) জোড়ায় কমিশন মাত্র একবার — ডিলিট করে বারবার ডেপ্লয় করে কমিশন ফার্ম করা আটকাতে
    cur.execute("""
        CREATE TABLE IF NOT EXISTS community_deploys (
            template_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            deployed_at TEXT,
            PRIMARY KEY (template_id, user_id)
        )
    """)

    conn.commit()
    conn.close()

def _load_persisted_system_settings():
    """বট রিস্টার্ট হলে DB থেকে সেভ করা SYSTEM_CONFIG টগলগুলো (bool/int) ফিরিয়ে আনে।"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM system_settings")
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            key, value = row["key"], row["value"]
            if key in SYSTEM_CONFIG:
                if isinstance(SYSTEM_CONFIG[key], bool):
                    SYSTEM_CONFIG[key] = (value == "1")
                elif isinstance(SYSTEM_CONFIG[key], int):
                    SYSTEM_CONFIG[key] = int(value)
                elif isinstance(SYSTEM_CONFIG[key], float):
                    SYSTEM_CONFIG[key] = float(value)
                else:
                    SYSTEM_CONFIG[key] = value
    except Exception:
        pass

def set_system_setting(key: str, value):
    """SYSTEM_CONFIG-এর কোনো টগল মেমোরিতে ও DB-তে (persist) একসাথে সেট করে।"""
    SYSTEM_CONFIG[key] = value
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        db_value = "1" if value is True else ("0" if value is False else str(value))
        cur.execute("INSERT INTO system_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, db_value))
        conn.commit()
        conn.close()
    except Exception:
        pass

init_admin_database()
_load_persisted_system_settings()

# ======================== 🔓 VIP ALL SLOT UNLOCK (অ্যাডমিন ইভেন্ট) ========================
# অ্যাডমিন নির্দিষ্ট সময়ের জন্য (২/৭/১৫ দিন বা কাস্টম) চালু করলে ওই সময়ে সব ইউজারের সবগুলো
# স্লট (১–MAX_BOT_SLOTS) আনলক হয় এবং ১২ ঘণ্টার ফ্রি-সেশন লিমিট ছাড়াই 24/7 চলে।
# মেয়াদ শেষে অটো বন্ধ হয় (ওয়াচডগ main.py-তে)। পেইড প্ল্যান ও Extend Validity আগের মতোই থাকে।
_UNLOCK_FMT = "%Y-%m-%d %H:%M:%S"

def get_vip_unlock_until():
    """ইভেন্ট এখন চালু থাকলে শেষ হওয়ার datetime, না থাকলে None।"""
    raw = SYSTEM_CONFIG.get("vip_unlock_until") or ""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, _UNLOCK_FMT)
    except Exception:
        return None
    return dt if dt > datetime.now() else None

def is_vip_unlock_active() -> bool:
    return get_vip_unlock_until() is not None

def get_vip_unlock_slots() -> int:
    """VIP Unlock চলাকালে কয়টি স্লট আনলক থাকবে (১–MAX_BOT_SLOTS)। ডিফল্ট: সবগুলো।"""
    try:
        n = int(SYSTEM_CONFIG.get("vip_unlock_slots", MAX_BOT_SLOTS))
    except Exception:
        n = MAX_BOT_SLOTS
    return max(1, min(MAX_BOT_SLOTS, n))

def set_vip_unlock_slots(n: int) -> int:
    """অ্যাডমিন যত স্লট আনলক করতে চায় সেটা সেভ করে (ইভেন্ট চালু থাকলে সাথে সাথে কার্যকর)।"""
    n = max(1, min(MAX_BOT_SLOTS, int(n)))
    set_system_setting("vip_unlock_slots", n)
    log_audit_event(0, "VIP_UNLOCK_SLOTS", f"VIP Unlock slots = {n}")
    return n

def set_vip_unlock(hours: float, slots: int = None):
    """এখন থেকে `hours` ঘণ্টার জন্য ইভেন্ট চালু করে (আগে চালু থাকলে নতুন সময়ে রিসেট হয়)। শেষ সময় রিটার্ন করে।
    slots দিলে সেটাই আনলক-স্লট সংখ্যা হিসেবে সেভ হয়; না দিলে আগে সেট করা সংখ্যা (ডিফল্ট: সব স্লট)।"""
    if slots is not None:
        set_vip_unlock_slots(slots)
    until = datetime.now() + timedelta(hours=hours)
    set_system_setting("vip_unlock_until", until.strftime(_UNLOCK_FMT))
    log_audit_event(0, "VIP_UNLOCK_ON", f"VIP Unlock until {until.strftime(_UNLOCK_FMT)} • {get_vip_unlock_slots()} slots")
    return until

def clear_vip_unlock():
    set_system_setting("vip_unlock_until", "")
    log_audit_event(0, "VIP_UNLOCK_OFF", "VIP All Slot Unlock ended")

# ======================== 🌐 কমিউনিটি মার্কেটপ্লেস (DB-backed) ========================
COMMUNITY_MAX_PENDING_PER_USER = 3
COMMUNITY_MAX_CODE_BYTES = 100 * 1024

CODE_BACKUP_KEEP_VERSIONS = 5

def add_code_backup(user_id: int, project_id: int, project_name: str, file_name: str, size: int,
                    file_id: str, msg_id: int, source: str = "", card_msg_id: int = None):
    """চ্যানেলে কোড-কপি পাঠানো সফল হলে ইনডেক্স রাখে। প্রতি (প্রজেক্ট, ফাইল)-এ শেষ ৫ ভার্সন রাখে।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO code_backups (user_id, project_id, project_name, file_name, size, file_id, msg_id, source, created_at, card_msg_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, project_id, project_name or "", file_name, size, file_id, msg_id, source or "",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), card_msg_id))
    cur.execute(
        "DELETE FROM code_backups WHERE project_id = ? AND file_name = ? AND id NOT IN "
        "(SELECT id FROM code_backups WHERE project_id = ? AND file_name = ? ORDER BY id DESC LIMIT ?)",
        (project_id, file_name, project_id, file_name, CODE_BACKUP_KEEP_VERSIONS))
    conn.commit()
    conn.close()

def get_latest_code_backups():
    """যেসব প্রজেক্ট এখনো আছে তাদের প্রতিটি ফাইলের সর্বশেষ কপি (file_id সহ)।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT cb.* FROM code_backups cb "
        "JOIN projects p ON p.project_id = cb.project_id AND p.user_id = cb.user_id "
        "WHERE cb.id = (SELECT MAX(c2.id) FROM code_backups c2 WHERE c2.project_id = cb.project_id AND c2.file_name = cb.file_name) "
        "ORDER BY cb.id ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def create_community_template(creator_id: int, creator_name: str, name: str, description: str, code: str) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO community_templates (creator_id, creator_name, name, description, code, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
        (creator_id, creator_name or "", name, description, code, datetime.now().strftime(_UNLOCK_FMT)))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    log_audit_event(creator_id, "COMMUNITY_SUBMIT", f"Template #{tid}: {name}")
    return tid

def count_user_pending_templates(user_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM community_templates WHERE creator_id = ? AND status = 'PENDING'", (user_id,))
    n = cur.fetchone()["c"]
    conn.close()
    return n

def get_community_template(tid: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM community_templates WHERE id = ?", (tid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def list_community_templates(status: str = "APPROVED", limit: int = 6, offset: int = 0):
    """রিটার্ন: (rows_without_code, total)। কোড বাদ দিয়ে হালকা লিস্ট দেয়।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM community_templates WHERE status = ?", (status,))
    total = cur.fetchone()["c"]
    cur.execute(
        "SELECT id, creator_id, creator_name, name, description, status, deploys, earned, created_at "
        "FROM community_templates WHERE status = ? ORDER BY deploys DESC, id DESC LIMIT ? OFFSET ?",
        (status, limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows, total

def list_user_templates(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, status, reject_reason, deploys, earned, created_at FROM community_templates "
        "WHERE creator_id = ? ORDER BY id DESC LIMIT 20", (user_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def review_community_template(tid: int, approve: bool, reason: str = ""):
    """PENDING (বা APPROVED→REMOVE) টেমপ্লেটের স্ট্যাটাস বদলায়। আপডেটেড রো রিটার্ন করে (না পেলে None)।"""
    t = get_community_template(tid)
    if not t:
        return None
    new_status = "APPROVED" if approve else "REJECTED"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE community_templates SET status = ?, reject_reason = ?, reviewed_at = ? WHERE id = ?",
                (new_status, "" if approve else reason[:200], datetime.now().strftime(_UNLOCK_FMT), tid))
    conn.commit()
    conn.close()
    log_audit_event(0, "COMMUNITY_REVIEW", f"Template #{tid} -> {new_status}")
    t["status"] = new_status
    t["reject_reason"] = "" if approve else reason[:200]
    return t

def record_community_deploy(template_id: int, deployer_id: int):
    """সফল ডেপ্লয়ের পর কল হয়। নিজের টেমপ্লেট ডেপ্লয় করলে বা একই ইউজার আবার ডেপ্লয় করলে কমিশন নেই।
    রিটার্ন: (creator_id, commission) — কমিশন দেওয়া হলে; না হলে (None, 0.0)।"""
    t = get_community_template(template_id)
    if not t or t["status"] != "APPROVED" or t["creator_id"] == deployer_id:
        return None, 0.0
    commission = float(SYSTEM_CONFIG.get("community_commission", 1.0) or 0.0)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO community_deploys (template_id, user_id, deployed_at) VALUES (?, ?, ?)",
                (template_id, deployer_id, datetime.now().strftime(_UNLOCK_FMT)))
    if cur.rowcount == 0:
        conn.close()
        return None, 0.0
    cur.execute("UPDATE community_templates SET deploys = deploys + 1, earned = earned + ? WHERE id = ?", (commission, template_id))
    if commission > 0:
        cur.execute("UPDATE users SET credits = COALESCE(credits, 0.0) + ? WHERE user_id = ?", (commission, t["creator_id"]))
    conn.commit()
    conn.close()
    log_audit_event(t["creator_id"], "COMMUNITY_COMMISSION", f"Template #{template_id} deployed by {deployer_id}: +{commission}")
    return t["creator_id"], commission

def scan_community_code(code: str):
    """অ্যাডমিন রিভিউয়ের জন্য হালকা হিউরিস্টিক স্ক্যান — সন্দেহজনক প্যাটার্নের তালিকা রিটার্ন করে
    (এটা ম্যালওয়্যার ডিটেকশন নয়; অ্যাডমিনকে কোডটা নিজে পড়ে দেখতেই হবে)।"""
    import re as _re
    checks = [
        (r'os\.system\s*\(', "os.system()"),
        (r'subprocess', "subprocess"),
        (r'\beval\s*\(|\bexec\s*\(', "eval/exec"),
        (r'base64|marshal|pickle', "base64/marshal/pickle (obfuscation?)"),
        (r'socket\.|paramiko|ftplib|smtplib', "raw network (socket/ssh/ftp/smtp)"),
        (r'os\.environ|getenv', "environment variables"),
        (r'\.\./|/etc/|/proc/|expanduser|shutil\.rmtree|os\.remove|os\.rmdir', "file system access outside bot dir"),
        (r'discord\.com/api/webhooks|pastebin|ngrok|webhook\.site', "external webhook/paste endpoint"),
    ]
    found = [label for pat, label in checks if _re.search(pat, code)]
    # বাইরের ডোমেইনে HTTP কল (api.telegram.org ও জানা AI-এন্ডপয়েন্ট বাদে)
    urls = set(_re.findall(r'https?://([A-Za-z0-9\.\-]+)', code))
    safe = ("api.telegram.org", "t.me", "telegram.org", "generativelanguage.googleapis.com", "api.openai.com")
    ext = sorted(u for u in urls if not any(u == s or u.endswith("." + s) for s in safe))
    if ext:
        found.append("external domains: " + ", ".join(ext[:6]))
    return found

def format_unlock_remaining(until) -> str:
    """'6d 4h' / '5h 20m' ধরনের ছোট ফরম্যাট।"""
    secs = max(0, int((until - datetime.now()).total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"

# ======================== কুপন ম্যানেজমেন্ট (DB-backed) ========================
def get_coupon(code: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coupons WHERE code = ? AND is_active = 1", (code.upper().strip(),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def use_coupon(code: str, user_id: int) -> tuple:
    """কুপন ব্যবহার করে। রিটার্ন (success: bool, discount_percent: int | error_msg: str)"""
    code = code.upper().strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coupons WHERE code = ? AND is_active = 1", (code,))
    coupon = cur.fetchone()
    if not coupon:
        conn.close()
        return False, "কুপন কোডটি বৈধ নয় বা মেয়াদ শেষ হয়ে গেছে।"
    coupon = dict(coupon)
    cur.execute("SELECT id FROM coupon_uses WHERE code = ? AND user_id = ?", (code, user_id))
    if cur.fetchone():
        conn.close()
        return False, "আপনি এই কুপনটি আগেই ব্যবহার করেছেন।"
    if coupon["max_uses"] > 0 and coupon["used_count"] >= coupon["max_uses"]:
        conn.close()
        return False, "এই কুপনের ব্যবহার সীমা শেষ হয়ে গেছে।"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO coupon_uses (code, user_id, used_at) VALUES (?, ?, ?)", (code, user_id, now_str))
    cur.execute("UPDATE coupons SET used_count = used_count + 1 WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    return True, coupon["discount_percent"]

def create_coupon(code: str, discount: int, max_uses: int, admin_id: int) -> tuple:
    code = code.upper().strip()
    if not code or not code.isalnum():
        return False, "কুপন কোড শুধু letter/number দিয়ে হবে।"
    if not (1 <= discount <= 100):
        return False, "ডিসকাউন্ট ১ থেকে ১০০ এর মধ্যে হতে হবে।"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("INSERT INTO coupons (code, discount_percent, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                    (code, discount, max_uses, admin_id, now_str))
        conn.commit()
        conn.close()
        uses_label = "আনলিমিটেড" if max_uses == 0 else f"{max_uses}বার"
        return True, f"✅ কুপন <code>{code}</code> তৈরি হয়েছে — {discount}% ছাড়, {uses_label} ব্যবহারযোগ্য।"
    except Exception:
        conn.close()
        return False, f"কুপন কোড <code>{code}</code> আগে থেকেই আছে।"

def deactivate_coupon(code: str) -> bool:
    code = code.upper().strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE coupons SET is_active = 0 WHERE code = ?", (code,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def list_coupons() -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ======================== 🎁 Redeem Code ম্যানেজমেন্ট (DB-backed) ========================
# একটা "প্যাক" = প্ল্যান টিয়ার + মেয়াদ (দিন)। অ্যাডমিন প্যাক বেছে কোড জেনারেট করে, ইউজার কোডটি
# রিডিম করলে সরাসরি প্ল্যান অ্যাক্টিভ হয়ে যায় (পেমেন্ট অ্যাপ্রুভাল ছাড়াই)।
import secrets
import sqlite3 as _sqlite3

_REDEEM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 0/O, 1/I বাদ — পড়তে ভুল হবে না
REDEEM_PREFIX = "PYH"
REDEEM_MAX_FAILS = 5            # এতবার ভুল কোড দিলে সাময়িক লক
REDEEM_LOCK_SECONDS = 600       # লকের সময় (১০ মিনিট)
_redeem_fail_log = {}           # {user_id: [failed_attempt_timestamps]}
_redeem_fail_lock = threading.Lock()

def get_free_session_seconds() -> int:
    """অ্যাডমিন প্যানেল থেকে সেট করা ফ্রি-সেশনের মেয়াদ (ঘণ্টা) সেকেন্ডে রূপান্তর করে।"""
    return int(SYSTEM_CONFIG.get("free_hosting_hours", 12) * 3600)

def get_free_warning_seconds() -> int:
    """মেয়াদ শেষ হওয়ার ৩০ মিনিট আগে ইউজারকে অ্যালার্ট পাঠানোর সময় (সেকেন্ডে)।"""
    return max(get_free_session_seconds() - 1800, 0)

def tier_to_plan(tier_key: str) -> str:
    """TIER_PLANS-এর কী থেকে DB-র 'plan' ভ্যালু বের করে (approve ফ্লো-র সাথে হুবহু এক নিয়ম)।"""
    tier_key = (tier_key or "").upper()
    if tier_key == "PRO":
        return "PRO"
    if tier_key in ("BUSINESS", "BUSINESS6M", "YEARLY"):
        return "BUSINESS"
    return "PREMIUM"

def generate_redeem_code() -> str:
    """র‍্যান্ডম কোড — ফরম্যাট: PYH-XXXX-XXXX-XXXX (cryptographically secure)।"""
    groups = ["".join(secrets.choice(_REDEEM_ALPHABET) for _ in range(4)) for _ in range(3)]
    return f"{REDEEM_PREFIX}-" + "-".join(groups)

def _normalize_redeem_code(raw: str) -> str:
    code = re.sub(r"\s+", "", (raw or "")).upper()
    # ড্যাশ ছাড়া PYHXXXXXXXXXXXX পেস্ট করলেও যেন কাজ করে
    if "-" not in code and code.startswith(REDEEM_PREFIX) and len(code) == 15:
        code = f"{code[:3]}-{code[3:7]}-{code[7:11]}-{code[11:15]}"
    return code

def create_redeem_codes(tier_key: str, days: int, count: int, max_uses: int, admin_id: int, custom_code: str = None) -> tuple:
    """একসাথে একাধিক কোড বানায়। রিটার্ন (True, [codes]) অথবা (False, error_msg)।
    max_uses = প্রতিটি কোড কতজন ইউজার রিডিম করতে পারবে (0 = আনলিমিটেড)।"""
    tier_key = (tier_key or "").upper()
    if tier_key not in TIER_PLANS:
        return False, "প্যাক টিয়ার সঠিক নয়।"
    if not (1 <= int(days) <= 3650):
        return False, "মেয়াদ ১ থেকে ৩৬৫০ দিনের মধ্যে হতে হবে।"
    if not (1 <= int(count) <= 100):
        return False, "একসাথে ১ থেকে ১০০টি কোড বানানো যাবে।"
    if max_uses < 0:
        return False, "MAX_USES ঋণাত্মক হতে পারে না।"

    plan = tier_to_plan(tier_key)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    cur = conn.cursor()
    created = []
    try:
        if custom_code:
            code = re.sub(r"\s+", "", custom_code).upper()
            if not re.fullmatch(r"[A-Z0-9\-]{4,32}", code):
                conn.close()
                return False, "কাস্টম কোডে শুধু ইংরেজি লেটার, সংখ্যা ও ড্যাশ (৪-৩২ অক্ষর) ব্যবহার করুন।"
            codes_to_make = [code]
        else:
            codes_to_make = [generate_redeem_code() for _ in range(int(count))]

        for code in codes_to_make:
            try:
                cur.execute(
                    "INSERT INTO redeem_codes (code, tier_key, plan, days, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (code, tier_key, plan, int(days), int(max_uses), admin_id, now_str)
                )
                created.append(code)
            except _sqlite3.IntegrityError:
                if custom_code:
                    conn.rollback()
                    conn.close()
                    return False, f"কোড {code} আগে থেকেই আছে।"
                # র‍্যান্ডম কোড কলিশন (প্রায় অসম্ভব) — একবার নতুন করে চেষ্টা
                retry = generate_redeem_code()
                cur.execute(
                    "INSERT INTO redeem_codes (code, tier_key, plan, days, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (retry, tier_key, plan, int(days), int(max_uses), admin_id, now_str)
                )
                created.append(retry)
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return False, f"কোড তৈরি ব্যর্থ: {str(e)[:80]}"
    conn.close()
    log_audit_event(admin_id, "REDEEM_CODES_CREATED", f"{len(created)}x {tier_key} {days}D (max_uses={max_uses})")
    return True, created

def _redeem_is_locked(user_id: int) -> int:
    """লক থাকলে বাকি সেকেন্ড রিটার্ন করে, নাহলে 0।"""
    now = time.time()
    with _redeem_fail_lock:
        recent = [t for t in _redeem_fail_log.get(user_id, []) if now - t < REDEEM_LOCK_SECONDS]
        _redeem_fail_log[user_id] = recent
        if len(recent) >= REDEEM_MAX_FAILS:
            return int(REDEEM_LOCK_SECONDS - (now - recent[0])) + 1
    return 0

def _redeem_register_fail(user_id: int):
    with _redeem_fail_lock:
        _redeem_fail_log.setdefault(user_id, []).append(time.time())

def redeem_code(user_id: int, username: str, raw_code: str) -> tuple:
    """ইউজারের কোড রিডিম করে প্ল্যান অ্যাক্টিভ করে।
    রিটার্ন (True, {tier_key, plan, days, code}) অথবা (False, বাংলা এরর মেসেজ)।
    ব্রুট-ফোর্স ঠেকাতে ৫ বার ভুল করলে ১০ মিনিট লক; একই কোড একই ইউজার দুইবার নিতে পারে না;
    আপডেটটি এক ট্রানজেকশনে হয় তাই একসাথে দুইজন একই সিঙ্গেল-ইউজ কোড নিতে পারবে না।"""
    locked = _redeem_is_locked(user_id)
    if locked:
        return False, f"অনেকবার ভুল কোড দেওয়া হয়েছে। অনুগ্রহ করে {max(1, locked // 60)} মিনিট পর আবার চেষ্টা করুন।"

    code = _normalize_redeem_code(raw_code)
    if not code or len(code) > 40:
        _redeem_register_fail(user_id)
        return False, "কোডটি সঠিক নয়। আবার চেক করে দিন।"

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT * FROM redeem_codes WHERE code = ?", (code,))
        row = cur.fetchone()
        if not row:
            conn.rollback(); conn.close()
            _redeem_register_fail(user_id)
            return False, "কোডটি সঠিক নয়। আবার চেক করে দিন।"
        rc = dict(row)
        if not rc["is_active"]:
            conn.rollback(); conn.close()
            return False, "এই কোডটি বাতিল করা হয়েছে।"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            cur.execute("INSERT INTO redeem_uses (code, user_id, username, used_at) VALUES (?, ?, ?, ?)",
                        (code, user_id, username or "Anonymous", now_str))
        except _sqlite3.IntegrityError:
            conn.rollback(); conn.close()
            return False, "আপনি এই কোডটি আগেই ব্যবহার করেছেন।"
        cur.execute(
            "UPDATE redeem_codes SET used_count = used_count + 1 "
            "WHERE code = ? AND is_active = 1 AND (max_uses = 0 OR used_count < max_uses)",
            (code,)
        )
        if cur.rowcount == 0:
            conn.rollback(); conn.close()
            return False, "এই কোডটি ইতিমধ্যে ব্যবহার হয়ে গেছে।"
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.rollback(); conn.close()
        except Exception:
            pass
        return False, f"রিডিম করা যায়নি: {str(e)[:60]}"

    # কোড কনজিউম হয়েছে — এবার প্ল্যান অ্যাক্টিভ; ব্যর্থ হলে কোডটি ফেরত দেওয়া হয় (ইউজারের ক্ষতি না হয়)
    try:
        set_user_plan(user_id, rc["plan"], rc["days"], tier_key=rc["tier_key"])
    except Exception as e:
        c2 = get_db_connection()
        c2.execute("DELETE FROM redeem_uses WHERE code = ? AND user_id = ?", (code, user_id))
        c2.execute("UPDATE redeem_codes SET used_count = MAX(used_count - 1, 0) WHERE code = ?", (code,))
        c2.commit(); c2.close()
        return False, "প্ল্যান অ্যাক্টিভ করতে সমস্যা হয়েছে, কোডটি নষ্ট হয়নি — কিছুক্ষণ পর আবার চেষ্টা করুন।"

    with _redeem_fail_lock:
        _redeem_fail_log.pop(user_id, None)
    log_audit_event(user_id, "REDEEM_CODE_USED", f"{code} -> {rc['tier_key']} {rc['days']}D")
    return True, {"code": code, "tier_key": rc["tier_key"], "plan": rc["plan"], "days": rc["days"]}

def get_redeem_code(code: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM redeem_codes WHERE code = ?", (_normalize_redeem_code(code),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def list_redeem_codes(limit: int = 200) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_redeem_uses(code: str, limit: int = 20) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM redeem_uses WHERE code = ? ORDER BY id DESC LIMIT ?", (code, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def set_redeem_code_active(code: str, active: bool) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE redeem_codes SET is_active = ? WHERE code = ?", (1 if active else 0, code))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_redeem_stats() -> dict:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM redeem_codes"); total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM redeem_codes WHERE is_active = 1 AND (max_uses = 0 OR used_count < max_uses)")
    available = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM redeem_uses"); redeemed = cur.fetchone()[0]
    conn.close()
    return {"total": total, "available": available, "redeemed": redeemed}


# ======================== সিকিউরিটি ও হেল্পার ========================
def encrypt_data(plain_text: str) -> str:
    if not plain_text:
        return ""
    try:
        return cipher_suite.encrypt(plain_text.encode('utf-8')).decode('utf-8')
    except Exception:
        return plain_text

def decrypt_data(encrypted_text: str) -> str:
    if not encrypted_text:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_text.encode('utf-8')).decode('utf-8')
    except (InvalidToken, Exception):
        return "[Decryption Error: Invalid Key]"

def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode('utf-8')).hexdigest()

def is_token_blacklisted(token: str) -> bool:
    thash = hash_token(token)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM blacklisted_tokens WHERE token_hash = ?", (thash,))
    row = cur.fetchone()
    conn.close()
    return bool(row)

def add_token_to_blacklist(token: str, reason: str = "Malicious Activity"):
    thash = hash_token(token)
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT OR REPLACE INTO blacklisted_tokens (token_hash, added_at, reason) VALUES (?, ?, ?)",
                (thash, now, reason))
    conn.commit()
    conn.close()

def log_audit_event(user_id: int, action: str, details: str):
    def _async_log():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("INSERT INTO audit_logs (user_id, action, details, timestamp) VALUES (?, ?, ?, ?)",
                        (user_id, action, details, now))
            conn.commit()
            conn.close()
        except Exception:
            pass
    threading.Thread(target=_async_log, daemon=True).start()

def get_user_audit_log(user_id: int, limit: int = 15):
    """নির্দিষ্ট ইউজারের সাম্প্রতিক অ্যাক্টিভিটি লগ (User Management প্যানেলের জন্য)।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM audit_logs WHERE user_id = ? ORDER BY log_id DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ======================== ইউজার ও রেফারেল ইঞ্জিন ========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_max_bot_limit(plan: str, tier_key: str = None) -> int:
    """
    ⭐ FIX: আগে এই ফাংশন শুধু 'plan' (PRO/PREMIUM/BUSINESS/VIP) দেখে বট-স্লট লিমিট রিটার্ন
    করত। এতে Business পরিবারের 2 মাস (BUSINESS) / 6 মাস (BUSINESS6M) / 12 মাস (YEARLY) —
    তিনটা প্যাকেজই "BUSINESS" প্ল্যানে সেভ হয়ে একই (business_bot_limit) লিমিট পেত (৬ মাসের
    জন্য ৭-এর বদলে ১০, ২ মাসের জন্য ৪-এর বদলে ভুল ফলাফল)।
    এখন tier_key (TIER_PLANS-এর আসল কী, যেমন "BUSINESS6M") দেওয়া থাকলে সরাসরি সেই
    প্যাকেজের bot_limit ব্যবহার করা হয় — নইলে পুরোনো (legacy/ম্যানুয়াল) হিসাবে ফলব্যাক করে।
    """
    plan = (plan or "FREE").upper()
    if tier_key:
        tier_key = tier_key.upper()
        tier_data = TIER_PLANS.get(tier_key)
        if tier_data:
            return tier_data["bot_limit"]
    if plan == "PRO":
        return SYSTEM_CONFIG["pro_bot_limit"]
    elif plan == "PREMIUM":
        return SYSTEM_CONFIG["premium_bot_limit"]
    elif plan in ("BUSINESS", "VIP"):
        return SYSTEM_CONFIG["business_bot_limit"]
    return SYSTEM_CONFIG["free_bot_limit"]


def get_max_bot_limit_for_user(user: dict) -> int:
    """user dict (users টেবিলের রো) থেকে plan + plan_tier দুটোই পড়ে সঠিক বট-স্লট
    লিমিট বের করে। যেকোনো জায়গায় ইউজারের max_bots বের করতে get_max_bot_limit(plan)-এর
    বদলে এটি ব্যবহার করা উচিত, কারণ এটিই Business-পরিবারের প্যাকেজগুলোর (2/6/12 মাস)
    মধ্যে সঠিকভাবে পার্থক্য করতে পারে।"""
    if not user:
        base = SYSTEM_CONFIG["free_bot_limit"]
    else:
        plan = (user.get("plan") or "FREE").upper()
        tier_key = user.get("plan_tier")
        base = get_max_bot_limit(plan, tier_key=tier_key)
    # 🔓 VIP All Slot Unlock ইভেন্ট চালু থাকলে সবার জন্য সব স্লট খোলা
    if is_vip_unlock_active():
        return max(base, get_vip_unlock_slots())
    return base

def is_vip_active(user: dict) -> bool:
    """ইউজারের কোনো পেইড প্ল্যান (Pro/Premium/Business — Business Yearly-ও 'BUSINESS' হিসেবেই
    সেভ হয়) বর্তমানে একটিভ কিনা চেক করে। GitHub Deploy-এর মতো VIP-exclusive ফিচার আনলক করতে
    ব্যবহার হয় — FREE ইউজার বা মেয়াদ-শেষ পেইড ইউজারের জন্য False রিটার্ন করবে।"""
    plan = (user.get("plan") or "FREE").upper()
    if plan not in ("PRO", "PREMIUM", "BUSINESS", "VIP"):
        return False
    expiry_raw = user.get("plan_expiry")
    if not expiry_raw or expiry_raw == "LIFETIME":
        return True
    try:
        return datetime.strptime(expiry_raw, "%Y-%m-%d %H:%M:%S") > datetime.now()
    except Exception:
        return False

def register_user(user_id: int, username: str, referrer_id: int = None):
    """ইউজার রেজিস্টার করে। রিটার্ন করে (is_new_user, referral_bonus_user_id) — is_new_user
    দিয়ে বোঝা যাবে এই ইউজারটি একদম প্রথমবার জয়েন করল কিনা (join-notification পাঠানোর জন্য দরকার),
    আর referral_bonus_user_id দিয়ে বোঝা যাবে কাউকে রেফারেল বোনাস দেওয়া লাগবে কিনা।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reward_user_id = None
    is_new_user = not row

    if not row:
        ref_id = referrer_id if (referrer_id and referrer_id != user_id) else 0
        cur.execute("""
            INSERT INTO users (user_id, username, plan, plan_expiry, joined_date, last_active, total_deploys, is_banned, ban_reason, referred_by, referral_count, referral_claimed, credits, referral_credits)
            VALUES (?, ?, 'FREE', 'LIFETIME', ?, ?, 0, 0, '', ?, 0, 0, 0.0, 0.0)
        """, (user_id, username or "Anonymous", now, now, ref_id))
        log_audit_event(user_id, "USER_REGISTER", f"New user registered: @{username}")

        if ref_id:
            cur.execute("SELECT user_id, referral_count, referral_claimed FROM users WHERE user_id = ?", (ref_id,))
            ref_data = cur.fetchone()
            if ref_data:
                new_count = ref_data["referral_count"] + 1
                cur.execute("UPDATE users SET referral_count = ? WHERE user_id = ?", (new_count, ref_id))
                
                target = SYSTEM_CONFIG["referral_target"]
                if new_count >= (ref_data["referral_claimed"] + 1) * target:
                    cur.execute("UPDATE users SET referral_claimed = referral_claimed + 1 WHERE user_id = ?", (ref_id,))
                    reward_user_id = ref_id
    else:
        cur.execute("UPDATE users SET username = ?, last_active = ? WHERE user_id = ?",
                    (username or "Anonymous", now, user_id))
    
    conn.commit()
    conn.close()

    if reward_user_id:
        set_user_plan(reward_user_id, "PREMIUM", SYSTEM_CONFIG["referral_bonus_days"])

    return is_new_user, reward_user_id

def get_user(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "user_id": user_id,
        "username": "Unknown",
        "plan": "FREE",
        "plan_expiry": "LIFETIME",
        "total_deploys": 0,
        "is_banned": 0,
        "ban_reason": "",
        "referred_by": 0,
        "referral_count": 0,
        "referral_claimed": 0,
        "credits": 0.0,
        "referral_credits": 0.0
    }

def get_all_users():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY joined_date DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_users(query: str, limit: int = 50):
    """
    ইউজার সার্চ করে ইউজার আইডি বা ইউজারনেমের অংশ দিয়ে (অ্যাডমিন ইউজার ডাইরেক্টরির জন্য)।
    সংখ্যা দিলে exact user_id ম্যাচ + username-এ substring উভয়ই খোঁজা হয়।
    """
    conn = get_db_connection()
    cur = conn.cursor()
    q = (query or "").strip()
    like = f"%{q}%"
    if q.isdigit():
        cur.execute(
            "SELECT * FROM users WHERE user_id = ? OR username LIKE ? ORDER BY joined_date DESC LIMIT ?",
            (int(q), like, limit)
        )
    else:
        cur.execute(
            "SELECT * FROM users WHERE username LIKE ? ORDER BY joined_date DESC LIMIT ?",
            (like, limit)
        )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def paginate(items: list, page: int, per_page: int = 6):
    """
    সাধারণ পেজিনেশন হেল্পার — যেকোনো লিস্ট (ইউজার, রিকোয়েস্ট, বট) কে পেজে ভাগ করে দেয়।
    রিটার্ন করে: (page_items, current_page, total_pages)
    """
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    return items[start:end], page, total_pages

def set_user_plan(user_id: int, plan: str, days: int = 30, tier_key: str = None):
    """plan -> DB-র 'plan' কলামে সেভ হওয়া কোয়ার্স ভ্যালু (PRO/PREMIUM/BUSINESS/VIP), এক্সপায়ারি ও
    is_vip_active ইত্যাদি চেকে ব্যবহৃত হয়।
    tier_key -> ⭐ FIX: TIER_PLANS-এর আসল কী (PRO/PREMIUM/BUSINESS/BUSINESS6M/YEARLY) — ঠিক
    কোন মেয়াদের প্যাকেজ কেনা হয়েছে তা আলাদাভাবে সেভ রাখে, যাতে get_max_bot_limit_for_user()
    Business পরিবারের ভিন্ন ভিন্ন মেয়াদের (2/6/12 মাস) জন্য সঠিক bot_limit (4/7/10) দিতে পারে।
    না দিলে plan-কেই tier_key হিসেবে সেভ করা হয় (আগের ম্যানুয়াল/legacy আচরণ অক্ষুণ্ণ থাকে)।"""
    conn = get_db_connection()
    cur = conn.cursor()
    plan = plan.upper()
    # PRO/PREMIUM-এর মেয়াদ একটাই ধরনের, তাই tier_key না দিলেও plan থেকেই নিরাপদে বসানো যায়।
    # কিন্তু BUSINESS/VIP-এর একাধিক মেয়াদভিত্তিক ভ্যারিয়েন্ট (2/6/12 মাস) আছে, তাই সেখানে
    # tier_key স্পষ্টভাবে না দিলে None-ই রাখা হয় — যাতে ম্যানুয়াল/legacy অ্যাডমিন অ্যাকশনে
    # (যেমন "Business — 30 Days" কুইক বাটন) ভুলবশত 2-মাসের (4-বট) লিমিট বসে না যায়, বরং
    # আগের মতোই legacy business_bot_limit (১০টি) ব্যবহার হয়।
    if tier_key:
        tier_key = tier_key.upper()
    elif plan in ("PRO", "PREMIUM"):
        tier_key = plan
    else:
        tier_key = None
    if plan in ("PRO", "PREMIUM", "BUSINESS", "VIP"):
        cur.execute("SELECT plan, plan_expiry FROM users WHERE user_id = ?", (user_id,))
        u = cur.fetchone()
        now = datetime.now()
        if u and u["plan"] in ("PRO", "PREMIUM", "BUSINESS", "VIP") and u["plan_expiry"] not in ("LIFETIME", None, ""):
            try:
                curr_exp = datetime.strptime(u["plan_expiry"], "%Y-%m-%d %H:%M:%S")
                if curr_exp > now:
                    expiry = (curr_exp + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    expiry = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                expiry = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            expiry = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        expiry = "LIFETIME"
        
    cur.execute("""
        UPDATE users 
        SET plan = ?, plan_expiry = ?, plan_tier = ?, expiry_alert_24h = 0, expiry_alert_2h = 0, expiry_alert_3d = 0
        WHERE user_id = ?
    """, (plan, expiry, tier_key, user_id))
    conn.commit()
    conn.close()
    log_audit_event(user_id, "PLAN_UPGRADE", f"Plan set to {plan} (Tier: {tier_key}, Expiry: {expiry})")

def set_user_ban(user_id: int, ban: bool, reason: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned = ?, ban_reason = ? WHERE user_id = ?",
                (1 if ban else 0, reason, user_id))
    conn.commit()
    conn.close()
    log_audit_event(user_id, "BAN_STATUS_CHANGE", f"Banned: {ban}, Reason: {reason}")

def increment_deploy_count(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET total_deploys = total_deploys + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ======================== মাল্টি-প্রজেক্ট ইঞ্জিন ========================
def get_user_projects(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE user_id = ? ORDER BY project_id ASC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_project_by_id(project_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_next_free_slot(user_id: int):
    """ইউজারের ১-১০ স্লটের মধ্যে প্রথম খালি স্লট নম্বর বের করে।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT slot_number FROM projects WHERE user_id = ? AND slot_number IS NOT NULL", (user_id,))
    used = {row["slot_number"] for row in cur.fetchall()}
    conn.close()
    for slot in range(1, MAX_BOT_SLOTS + 1):
        if slot not in used:
            return slot
    return None

def create_user_project(user_id: int, project_name: str, main_file: str = "main.py", slot_number: int = None):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if slot_number is None:
        slot_number = get_next_free_slot(user_id)

    cur.execute("""
        INSERT INTO projects (user_id, project_name, main_file, status, created_at, last_run, slot_number, expiry_date)
        VALUES (?, ?, ?, 'STOPPED', ?, ?, ?, 'LIFETIME')
    """, (user_id, project_name, main_file, now, "Never", slot_number))
    pid = cur.lastrowid
    conn.commit()
    conn.close()

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    os.makedirs(p_dir, exist_ok=True)
    return pid

def get_project_by_slot(user_id: int, slot_number: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE user_id = ? AND slot_number = ?", (user_id, slot_number))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_user_project(project_id: int):
    p = get_project_by_id(project_id)
    if not p:
        return
    user_id = p["user_id"]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    conn.commit()
    conn.close()

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if os.path.exists(p_dir):
        try:
            shutil.rmtree(p_dir)
        except Exception:
            pass

def update_project_status(project_id: int, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE projects SET status = ?, last_run = ? WHERE project_id = ?", (status, now, project_id))
    conn.commit()
    conn.close()

def update_project_main_file(project_id: int, main_file: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET main_file = ? WHERE project_id = ?", (main_file, project_id))
    conn.commit()
    conn.close()

def get_project_expiry_status(project: dict):
    """রিটার্ন করে (is_expired: bool, label: str) — নির্দিষ্ট বট স্লটের মেয়াদ শেষ কিনা এবং দেখানোর টেক্সট।"""
    exp_str = project.get("expiry_date") if project else None
    if not exp_str or exp_str == "LIFETIME":
        return False, "Lifetime (Free Slot)"
    try:
        exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
        if exp_dt <= datetime.now():
            return True, f"Expired ({exp_str})"
        return False, exp_str
    except Exception:
        return False, exp_str

def get_effective_slot_status(user: dict, project: dict):
    """
    একটি নির্দিষ্ট বট স্লটের 'প্রকৃত' (effective) হোস্টিং স্ট্যাটাস বের করে।

    কেন এই ফাংশনটি দরকার হলো:
    আগে অ্যাকাউন্ট-লেভেল প্ল্যান (users.plan / plan_expiry — "My Account"-এ দেখানো হয়)
    আর প্রতিটি বট স্লটের নিজস্ব Extend Validity মেয়াদ (projects.expiry_date — Control
    Panel ও Extend Validity স্ক্রিনে দেখানো হয়) — এই দুটো সম্পূর্ণ আলাদাভাবে হিসাব হতো।
    ফলে একজন ইউজার Business Plan কিনলেও প্রতিটি বট স্লটে "Lifetime (Free Slot)" দেখাত
    এবং Extend Validity মেনু আবার টাকা দিয়ে কিনতে বলত — যদিও ইউজার ইতিমধ্যে টাকা
    দিয়ে একটিভ প্ল্যান কিনে ফেলেছে। উল্টো দিকে, কোনো স্লট আলাদাভাবে Extend Validity
    দিয়ে কেনা থাকলে সেটার মেয়াদ শেষ হয়ে গেলে বটটি জোরপূর্বক বন্ধ হয়ে যেত, এমনকি
    ইউজারের অ্যাকাউন্ট প্ল্যান তখনও সচল থাকলেও।

    এই ফাংশন এখন দুটো সোর্সকেই (প্ল্যান + স্লট) একত্র করে একটাই ফলাফল দেয়, যেটা পুরো
    বট জুড়ে (Control Panel, Bot Dashboard, Extend Validity, ওয়াচডগ) সবখানে ব্যবহার
    করা হয় — তাই কোথাও ভিন্ন/ভুল তথ্য দেখানোর বা ভুলভাবে বট বন্ধ হওয়ার সুযোগ থাকে না।

    রিটার্ন করে dict:
        is_24_7    -> bool, এই স্লটটি ১২ ঘণ্টা ফ্রি-সেশন লিমিট থেকে মুক্ত কিনা
        source     -> "PLAN" (অ্যাকাউন্ট প্ল্যান কভার করছে) | "SLOT" (স্লট আলাদাভাবে কেনা) | "NONE"
        label      -> রেডি-টু-শো টেক্সট (সব স্ক্রিনে একই ফরম্যাট ব্যবহার করা উচিত)
        expiry_dt  -> datetime | None (None = কোনো নির্দিষ্ট মেয়াদ নেই অর্থাৎ লাইফটাইম কভারেজ)
        days_left  -> int | None
        is_expired -> bool, এই স্লটটি জোরপূর্বক বন্ধ করা উচিত কিনা (কোনো কভারেজ নেই এবং
                      স্লটের নিজের কেনা মেয়াদ আগে সেট ছিল কিন্তু এখন শেষ হয়ে গেছে)
    """
    now = datetime.now()
    user = user or {}
    project = project or {}

    # ১. অ্যাকাউন্ট-লেভেল প্ল্যান কভারেজ চেক
    plan = (user.get("plan") or "FREE").upper()
    plan_expiry_raw = user.get("plan_expiry")
    plan_active = False
    plan_expiry_dt = None
    if plan in ("PRO", "PREMIUM", "BUSINESS", "VIP"):
        if not plan_expiry_raw or plan_expiry_raw == "LIFETIME":
            plan_active = True  # অ্যাডমিন কর্তৃক ম্যানুয়াল লাইফটাইম প্ল্যান
        else:
            try:
                plan_expiry_dt = datetime.strptime(plan_expiry_raw, "%Y-%m-%d %H:%M:%S")
                plan_active = plan_expiry_dt > now
            except Exception:
                plan_active = False

    # এই স্লট নম্বরটি বর্তমান প্ল্যানের বট-লিমিটের মধ্যে পড়ে কিনা
    # ⭐ FIX: আগে এখানে get_max_bot_limit(plan) কল হতো, যেটা tier_key ছাড়া সবসময় Business
    # পরিবারের জন্য পুরনো ফ্ল্যাট business_bot_limit (১০) রিটার্ন করত — ফলে ২ মাস (৪ স্লট) বা
    # ৬ মাস (৭ স্লট) প্ল্যানেও ভুলভাবে ১০টি স্লটকে "কভার্ড" ধরে নিত। এখন ইউজারের আসল plan_tier
    # অনুযায়ী সঠিক bot_limit ব্যবহার হচ্ছে।
    slot_no = project.get("slot_number") or 0
    bot_limit = get_max_bot_limit_for_user(user) if plan_active else 0
    plan_covers_slot = plan_active and (slot_no == 0 or slot_no <= bot_limit)

    # ২. এই স্লটের নিজস্ব Extend Validity ক্রয় চেক
    slot_exp_raw = project.get("expiry_date")
    slot_was_purchased = bool(slot_exp_raw and slot_exp_raw != "LIFETIME")
    slot_active = False
    slot_expiry_dt = None
    if slot_was_purchased:
        try:
            slot_expiry_dt = datetime.strptime(slot_exp_raw, "%Y-%m-%d %H:%M:%S")
            slot_active = slot_expiry_dt > now
        except Exception:
            slot_active = False

    candidates = []
    if plan_covers_slot:
        candidates.append(("PLAN", plan_expiry_dt))
    if slot_active:
        candidates.append(("SLOT", slot_expiry_dt))
    # 🔓 অ্যাডমিন VIP All Slot Unlock ইভেন্ট — সব ইউজারের সব স্লট 24/7 কভার্ড
    unlock_dt = get_vip_unlock_until()
    if unlock_dt and (slot_no == 0 or slot_no <= get_vip_unlock_slots()):
        candidates.append(("EVENT", unlock_dt))

    if not candidates:
        # কোনো কভারেজ নেই। যদি আগে স্লট নিজে কেনা ছিল আর এখন শেষ হয়ে গেছে -> সত্যিকারের এক্সপায়ার্ড
        is_expired = slot_was_purchased and not slot_active
        return {
            "is_24_7": False,
            "source": "NONE",
            "label": f"⛔ Expired ({slot_exp_raw})" if is_expired else f"🆓 Free Slot ({SYSTEM_CONFIG.get('free_hosting_hours', 12)}h/Session)",
            "expiry_dt": None,
            "days_left": None,
            "is_expired": is_expired
        }

    # লাইফটাইম কভারেজ (প্ল্যান বা স্লট, যেটাই লাইফটাইম হোক) সবচেয়ে ভালো — কোনো এক্সপায়ারি নেই
    lifetime_hit = next((c for c in candidates if c[1] is None), None)
    if lifetime_hit:
        src = lifetime_hit[0]
        pinfo = get_plan_display_info(user)
        if src == "PLAN":
            label = f"{pinfo['icon']} {pinfo['display_name']} • 24/7 Active • Lifetime"
        else:
            label = "💎 Extended (24/7 • Lifetime)"
        return {"is_24_7": True, "source": src, "label": label, "expiry_dt": None, "days_left": None, "is_expired": False}

    # দুটো সোর্সের মধ্যে যেটার মেয়াদ পরে শেষ হবে (ইউজারের জন্য বেশি সুবিধাজনক) সেটাই দেখানো হবে
    best_source, best_dt = max(candidates, key=lambda c: c[1])
    days_left = max(0, (best_dt - now).days)
    pinfo = get_plan_display_info(user)
    if best_source == "PLAN":
        label = f"{pinfo['icon']} {pinfo['display_name']} • 24/7 Active • {days_left}d left (until {best_dt.strftime('%Y-%m-%d %H:%M')})"
    elif best_source == "EVENT":
        label = f"🎉 VIP Unlock • 24/7 Active • {format_unlock_remaining(best_dt)} left (until {best_dt.strftime('%Y-%m-%d %H:%M')})"
    else:
        label = f"💎 Extended (24/7) — {days_left}d left (until {best_dt.strftime('%Y-%m-%d %H:%M')})"

    return {
        "is_24_7": True,
        "source": best_source,
        "label": label,
        "expiry_dt": best_dt,
        "days_left": days_left,
        "is_expired": False
    }

def get_plan_display_info(user: dict) -> dict:
    """
    ইউজারের প্ল্যান সম্পর্কিত সব display তথ্য এক জায়গা থেকে দেয় — যাতে
    My Account, Upgrade Plan, Bot Dashboard, Control Panel, Extend Validity
    সব স্ক্রিনে একই নাম ও ফরম্যাটে দেখানো যায়।

    রিটার্ন করে:
        plan_key     -> DB-তে সেভ থাকা raw key (BUSINESS/PRO/PREMIUM/FREE)
        display_name -> ইউজারকে দেখানোর জন্য সুন্দর নাম (Business Boss / Pro Plan...)
        icon         -> প্ল্যানের ইমোজি আইকন
        is_active    -> bool, প্ল্যান এখন সত্যিই কার্যকর কিনা
        expiry_str   -> মেয়াদ শেষের তারিখ (YYYY-MM-DD HH:MM) বা "Lifetime" বা "—"
        days_left    -> int | None, বাকি দিন সংখ্যা
        badge        -> Control Panel/Bot Dashboard-এ দেখানোর ছোট badge টেক্সট
    """
    plan_key = (user.get("plan") or "FREE").upper()
    expiry_raw = user.get("plan_expiry") or "LIFETIME"
    now = datetime.now()

    # প্ল্যান নাম ও আইকন
    plan_meta = {
        "PRO":      ("⚡", "Pro Plan"),
        "PREMIUM":  ("🌟", "Premium Plan"),
        "BUSINESS": ("👑", "Business Boss"),
        "VIP":      ("💎", "VIP Plan"),
        "FREE":     ("🆓", "Free Tier"),
    }
    icon, display_name = plan_meta.get(plan_key, ("🆓", plan_key))

    # মেয়াদ ও active চেক
    is_active = False
    expiry_str = "—"
    days_left = None

    if plan_key in ("PRO", "PREMIUM", "BUSINESS", "VIP"):
        if not expiry_raw or expiry_raw == "LIFETIME":
            is_active = True
            expiry_str = "Lifetime"
            days_left = None
        else:
            try:
                exp_dt = datetime.strptime(expiry_raw, "%Y-%m-%d %H:%M:%S")
                is_active = exp_dt > now
                days_left = max(0, (exp_dt - now).days)
                expiry_str = f"{exp_dt.strftime('%Y-%m-%d %H:%M')} ({days_left}d left)"
                if not is_active:
                    expiry_str = f"⛔ Expired ({exp_dt.strftime('%Y-%m-%d')})"
            except Exception:
                is_active = False
                expiry_str = expiry_raw

    # Badge — Control Panel ও Bot Dashboard-এ compact ফরম্যাটে
    if not is_active:
        badge = "🆓 Free"
    elif expiry_str == "Lifetime":
        badge = f"{icon} {display_name} • Lifetime"
    else:
        badge = f"{icon} {display_name} • {days_left}d left"

    return {
        "plan_key": plan_key,
        "display_name": display_name,
        "icon": icon,
        "is_active": is_active,
        "expiry_str": expiry_str,
        "days_left": days_left,
        "badge": badge,
    }


def set_project_github_info(project_id: int, owner: str, repo: str, is_private: bool = False):
    """GitHub থেকে ডিপ্লয় হওয়া প্রজেক্টের রিপো তথ্য সেভ করে — পরে GitHub Sync করার সময় লাগবে।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE projects SET github_owner=?, github_repo=?, github_is_private=? WHERE project_id=?",
        (owner, repo, 1 if is_private else 0, project_id)
    )
    conn.commit()
    conn.close()

def get_project_github_info(project_id: int) -> dict | None:
    """প্রজেক্টের GitHub রিপো তথ্য রিটার্ন করে — না থাকলে None।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT github_owner, github_repo, github_is_private FROM projects WHERE project_id=?",
        (project_id,)
    )
    row = cur.fetchone()
    conn.close()
    if row and row["github_owner"]:
        return {"owner": row["github_owner"], "repo": row["github_repo"], "is_private": bool(row["github_is_private"])}
    return None


# ======================== বট নোটস ========================
def set_bot_notes(project_id: int, notes: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET bot_notes=? WHERE project_id=?", (notes[:300], project_id))
    conn.commit()
    conn.close()

def get_bot_notes(project_id: int) -> str:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT bot_notes FROM projects WHERE project_id=?", (project_id,))
    row = cur.fetchone()
    conn.close()
    return (row["bot_notes"] or "") if row else ""

# ======================== Onboarding ========================
def mark_onboarding_done(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET onboarding_done=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def is_onboarding_done(user_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT onboarding_done FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row["onboarding_done"]) if row else False

def get_checklist_flags(user_id: int) -> dict:
    import json as _json
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT checklist_flags FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    try:
        return _json.loads(row["checklist_flags"] or "{}") if row else {}
    except Exception:
        return {}

def set_checklist_flag(user_id: int, flag: str):
    import json as _json
    flags = get_checklist_flags(user_id)
    flags[flag] = True
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET checklist_flags=? WHERE user_id=?", (_json.dumps(flags), user_id))
    conn.commit()
    conn.close()

def scan_logs_for_errors(user_id: int, project_id: int, projects_dir: str) -> list:
    """বটের লগ ফাইল স্ক্যান করে কমন এরর ধরে বাংলায় ব্যাখ্যা দেয়।"""
    import os as _os
    log_path = _os.path.join(projects_dir, f"{user_id}_{project_id}", "bot_output.log")
    if not _os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            log = f.read()[-8000:]  # শেষ ৮০০০ অক্ষর
    except Exception:
        return []

    error_patterns = [
        ("Traceback (most recent call last)", "🔴 Python এরর পাওয়া গেছে। কোডে কোনো Exception হয়েছে।"),
        ("ModuleNotFoundError", "📦 কোনো library ইনস্টল নেই। requirements.txt চেক করুন বা আবার আপলোড করুন।"),
        ("ImportError", "📦 Import এরর — কোনো লাইব্রেরি খুঁজে পাচ্ছে না।"),
        ("SyntaxError", "✏️ কোডে ভুল লেখা আছে (SyntaxError)। ফাইলটা এডিট করুন।"),
        ("ConnectionRefusedError", "🌐 Internet সংযোগ সমস্যা বা পোর্ট বন্ধ।"),
        ("Unauthorized", "🔑 বট টোকেন ভুল বা বাতিল হয়ে গেছে।"),
        ("Invalid token", "🔑 বট টোকেন সঠিক নয়। @BotFather থেকে নতুন টোকেন নিন।"),
        ("Conflict: terminated by other getUpdates", "⚠️ একই টোকেন দিয়ে অন্য জায়গায় বট চালু আছে।"),
        ("MemoryError", "💾 RAM শেষ হয়ে গেছে। প্ল্যান আপগ্রেড করুন।"),
        ("KeyError", "🔑 কোডে কোনো Key খুঁজে পাচ্ছে না — config বা .env চেক করুন।"),
        ("FileNotFoundError", "📄 কোনো ফাইল খুঁজে পাচ্ছে না। সব ফাইল ঠিকমতো আপলোড করুন।"),
        ("TimeoutError", "⏱️ সংযোগ Timeout — নেটওয়ার্ক সমস্যা বা বাইরের API ধীর।"),
        ("PermissionError", "🔒 ফাইল লেখার অনুমতি নেই।"),
        ("rate limit", "🚦 Telegram rate limit — বট খুব দ্রুত মেসেজ পাঠাচ্ছে, একটু ধীর করুন।"),
        ("webhook", "🌐 Webhook সমস্যা — polling mode ব্যবহার করুন।"),
    ]
    found = []
    for pattern, explanation in error_patterns:
        if pattern.lower() in log.lower():
            found.append(explanation)
    return list(dict.fromkeys(found))[:5]  # সর্বোচ্চ ৫টা unique এরর


# ======================== Deploy History ========================
MAX_DEPLOY_HISTORY = 10

def save_deploy_snapshot(user_id: int, project_id: int, main_file: str, note: str = "") -> int:
    """ফাইল আপলোড/আপডেটের সময় বর্তমান প্রজেক্টের স্ন্যাপশট সেভ করে।
    সর্বোচ্চ MAX_DEPLOY_HISTORY ভার্সন রাখে — পুরনোটা স্বয়ংক্রিয় মুছে দেয়।
    রিটার্ন করে নতুন version নম্বর।"""
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if not os.path.exists(p_dir):
        return 0

    conn = get_db_connection()
    cur = conn.cursor()

    # পরের ভার্সন নম্বর বের করা
    cur.execute("SELECT MAX(version) FROM deploy_history WHERE project_id=?", (project_id,))
    row = cur.fetchone()
    next_ver = (row[0] or 0) + 1

    # স্ন্যাপশট ডিরেক্টরি
    snap_base = os.path.join(PROJECTS_DIR, f"_hist_{user_id}_{project_id}")
    os.makedirs(snap_base, exist_ok=True)
    snap_dir = os.path.join(snap_base, f"v{next_ver}")

    # বর্তমান ফাইলগুলো কপি (history ও hidden ফোল্ডার বাদে)
    try:
        if os.path.exists(snap_dir):
            shutil.rmtree(snap_dir)
        os.makedirs(snap_dir)
        for item in os.listdir(p_dir):
            if item.startswith("_") or item.startswith("."):
                continue
            src = os.path.join(p_dir, item)
            dst = os.path.join(snap_dir, item)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
    except Exception:
        conn.close()
        return 0

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO deploy_history (project_id, user_id, version, snapshot_dir, main_file, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, user_id, next_ver, snap_dir, main_file, note, now_str)
    )

    # পুরনো ভার্সন ছাঁটাই — MAX_DEPLOY_HISTORY-এর বেশি থাকলে সবচেয়ে পুরনোটা মুছে দেওয়া
    cur.execute(
        "SELECT id, snapshot_dir FROM deploy_history WHERE project_id=? ORDER BY version ASC",
        (project_id,)
    )
    all_rows = cur.fetchall()
    while len(all_rows) > MAX_DEPLOY_HISTORY:
        old_id, old_snap = all_rows.pop(0)
        cur.execute("DELETE FROM deploy_history WHERE id=?", (old_id,))
        try:
            if os.path.exists(old_snap):
                shutil.rmtree(old_snap)
        except Exception:
            pass

    conn.commit()
    conn.close()
    return next_ver

def get_deploy_history(project_id: int) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, version, main_file, note, created_at FROM deploy_history WHERE project_id=? ORDER BY version DESC",
        (project_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def restore_deploy_version(user_id: int, project_id: int, history_id: int) -> tuple:
    """নির্দিষ্ট ভার্সনে রোলব্যাক করে। রিটার্ন (success: bool, main_file: str | error: str)"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM deploy_history WHERE id=? AND project_id=?", (history_id, project_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False, "ভার্সনটি পাওয়া যায়নি।"
    row = dict(row)
    snap_dir = row["snapshot_dir"]
    if not os.path.exists(snap_dir):
        return False, "স্ন্যাপশট ফাইল মুছে গেছে।"

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    # রোলব্যাকের আগে বর্তমান ভার্সন সেভ করা
    save_deploy_snapshot(user_id, project_id, row["main_file"] or "main.py", note="[auto-save before rollback]")

    # পুরনো ফাইল পরিষ্কার (underscore ফাইল রেখে)
    for item in os.listdir(p_dir):
        if not item.startswith("_"):
            fp = os.path.join(p_dir, item)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass

    # স্ন্যাপশট থেকে রিস্টোর
    for item in os.listdir(snap_dir):
        src = os.path.join(snap_dir, item)
        dst = os.path.join(p_dir, item)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    main_f = row["main_file"] or "main.py"
    update_project_main_file(project_id, main_f)
    return True, main_f

# ======================== Env Vars (.env Manager) ========================
def set_env_var(project_id: int, key: str, value: str):
    """KEY=VALUE এনক্রিপ্ট করে DB-তে সেভ করে। বট রান হওয়ার আগে .env ফাইলে লেখা হয়।"""
    key = key.strip().upper()
    encrypted = encrypt_data(value)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO env_vars (project_id, key, value_encrypted, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(project_id, key) DO UPDATE SET value_encrypted=excluded.value_encrypted, created_at=excluded.created_at",
        (project_id, key, encrypted, now_str)
    )
    conn.commit()
    conn.close()

def get_env_vars(project_id: int) -> list:
    """প্রজেক্টের সব env var রিটার্ন করে — value ডিক্রিপ্ট করা।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT key, value_encrypted, created_at FROM env_vars WHERE project_id=? ORDER BY key", (project_id,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for row in rows:
        try:
            val = decrypt_data(row["value_encrypted"])
        except Exception:
            val = "***"
        result.append({"key": row["key"], "value": val, "created_at": row["created_at"]})
    return result

def delete_env_var(project_id: int, key: str) -> bool:
    key = key.strip().upper()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM env_vars WHERE project_id=? AND key=?", (project_id, key))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def write_env_file(user_id: int, project_id: int):
    """.env ফাইল ডিস্কে লেখে — বট রান হওয়ার ঠিক আগে কল করতে হবে।"""
    env_vars = get_env_vars(project_id)
    if not env_vars:
        return
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    env_path = os.path.join(p_dir, ".env")
    lines = [f'{v["key"]}={v["value"]}' for v in env_vars]
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

# ======================== Daily Report ========================
def set_daily_report_time(user_id: int, time_str: str | None):
    """HH:MM ফরম্যাটে সময় সেভ করে। None দিলে daily report বন্ধ হয়।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET daily_report_time=? WHERE user_id=?", (time_str, user_id))
    conn.commit()
    conn.close()

def get_users_with_daily_report() -> list:
    """daily_report_time সেট আছে এমন সব ইউজার রিটার্ন করে।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, daily_report_time FROM users WHERE daily_report_time IS NOT NULL")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def extend_project_expiry(project_id: int, days: int):
    """একটি বট স্লটের মেয়াদ বাড়ায় — চলমান মেয়াদ থাকলে তার উপর যোগ হবে, মেয়াদ শেষ/খালি থাকলে আজ থেকে গণনা হবে।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT expiry_date FROM projects WHERE project_id = ?", (project_id,))
    row = cur.fetchone()
    now = datetime.now()
    current_exp = row["expiry_date"] if row else None

    if current_exp and current_exp != "LIFETIME":
        try:
            curr_dt = datetime.strptime(current_exp, "%Y-%m-%d %H:%M:%S")
            base = curr_dt if curr_dt > now else now
        except Exception:
            base = now
    else:
        base = now

    new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE projects SET expiry_date = ? WHERE project_id = ?", (new_exp, project_id))
    conn.commit()
    conn.close()
    return new_exp

# ======================== ফাইল ম্যানেজার হেল্পারস ========================
def get_project_files_info(user_id: int, project_id: int):
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if not os.path.exists(p_dir):
        return [], 0
    
    file_list = []
    total_size = 0
    for root, _, files in os.walk(p_dir):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, p_dir)
            size = os.path.getsize(full_path)
            total_size += size
            file_list.append({"name": rel_path, "size": size})
    
    return file_list, total_size

def zip_project_folder(user_id: int, project_id: int):
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if not os.path.exists(p_dir):
        return None
    
    project = get_project_by_id(project_id)
    proj_name = re.sub(r'[^a-zA-Z0-9_]', '_', project["project_name"]) if project else f"bot_{project_id}"
    zip_path = os.path.join(BACKUP_DIR, f"{proj_name}_{project_id}_backup.zip")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(p_dir):
            for file in files:
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, p_dir)
                zf.write(full_p, arcname=rel_p)
                
    return zip_path

def delete_project_file(user_id: int, project_id: int, filename: str):
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    target_path = os.path.abspath(os.path.join(p_dir, filename))
    if target_path.startswith(os.path.abspath(p_dir) + os.sep) and os.path.exists(target_path):
        if os.path.isfile(target_path):
            os.remove(target_path)
            return True
    return False

# ======================== ভিআইপি রিকোয়েস্ট ইঞ্জিন ========================
def create_vip_request(user_id: int, username: str, plan_name: str, days: int, price: str, gateway: str, trx_id: str, tier_key: str = None):
    """tier_key -> ⭐ FIX: TIER_PLANS-এর আসল কী (PRO/PREMIUM/BUSINESS/BUSINESS6M/YEARLY) সেভ
    রাখা হয়, যাতে অ্যাডমিন অ্যাপ্রুভ করার সময় plan_name স্ট্রিং থেকে আন্দাজ (substring match)
    না করে সরাসরি সঠিক প্যাকেজ শনাক্ত করে সঠিক bot_limit (4/7/10) সেট করা যায়।"""
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO vip_requests (user_id, username, plan_name, days, price, gateway, trx_id, status, created_at, tier_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
    """, (user_id, username or "Anonymous", plan_name, days, price, gateway, trx_id, now, tier_key))
    req_id = cur.lastrowid
    conn.commit()
    conn.close()
    return req_id

def get_vip_request(req_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM vip_requests WHERE request_id = ?", (req_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_pending_vip_requests():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM vip_requests WHERE status = 'PENDING' ORDER BY request_id DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_vip_requests(status: str = None, limit: int = 200):
    """সব রিকোয়েস্ট (বা নির্দিষ্ট status) হিস্টোরি প্যানেলের জন্য, নতুন থেকে পুরাতন।"""
    conn = get_db_connection()
    cur = conn.cursor()
    if status:
        cur.execute("SELECT * FROM vip_requests WHERE status = ? ORDER BY request_id DESC LIMIT ?", (status, limit))
    else:
        cur.execute("SELECT * FROM vip_requests ORDER BY request_id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_vip_request_status(req_id: int, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE vip_requests SET status = ? WHERE request_id = ?", (status, req_id))
    conn.commit()
    conn.close()

# ======================== সাপোর্ট টিকিট সিস্টেম ========================
def create_support_ticket(user_id: int, username: str, first_message: str) -> int:
    """নতুন টিকিট তৈরি করে এবং ইউজারের প্রথম মেসেজটাও থ্রেডে যোগ করে দেয়।
    subject হিসেবে প্রথম মেসেজের প্রথম ৪০ ক্যারেক্টার ব্যবহার হয়।"""
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = (first_message or "").strip().replace("\n", " ")[:40]
    if len(first_message or "") > 40:
        subject += "..."
    cur.execute("""
        INSERT INTO support_tickets (user_id, username, subject, status, created_at, updated_at)
        VALUES (?, ?, ?, 'OPEN', ?, ?)
    """, (user_id, username or "Anonymous", subject, now, now))
    ticket_id = cur.lastrowid
    cur.execute("""
        INSERT INTO ticket_messages (ticket_id, sender, message, created_at)
        VALUES (?, 'user', ?, ?)
    """, (ticket_id, first_message, now))
    conn.commit()
    conn.close()
    return ticket_id

def add_ticket_message(ticket_id: int, sender: str, message: str):
    """sender: 'user' বা 'admin'। মেসেজ যোগ করার সাথে সাথে টিকিটের status ও updated_at আপডেট হয়:
    ইউজার মেসেজ পাঠালে -> OPEN (রিওপেন হয় যদি আগে CLOSED-ও থাকে), অ্যাডমিন রিপ্লাই দিলে -> REPLIED।"""
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO ticket_messages (ticket_id, sender, message, created_at)
        VALUES (?, ?, ?, ?)
    """, (ticket_id, sender, message, now))
    new_status = "REPLIED" if sender == "admin" else "OPEN"
    cur.execute("UPDATE support_tickets SET status = ?, updated_at = ? WHERE ticket_id = ?", (new_status, now, ticket_id))
    conn.commit()
    conn.close()

def get_ticket(ticket_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_ticket_messages(ticket_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY msg_id ASC", (ticket_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_user_tickets(user_id: int, limit: int = 50):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_tickets WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_tickets(status: str = None, limit: int = 200):
    """অ্যাডমিন প্যানেলের জন্য — status দিলে ফিল্টার করে, নইলে সব। OPEN/REPLIED আগে দেখানোর
    জন্য status='OPEN'/'REPLIED' অগ্রাধিকার অনুযায়ী সর্ট করা (নতুন-আপডেট আগে)।"""
    conn = get_db_connection()
    cur = conn.cursor()
    if status:
        cur.execute("SELECT * FROM support_tickets WHERE status = ? ORDER BY updated_at DESC LIMIT ?", (status, limit))
    else:
        cur.execute("SELECT * FROM support_tickets ORDER BY updated_at DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def count_open_tickets() -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM support_tickets WHERE status IN ('OPEN', 'REPLIED')")
    count = cur.fetchone()[0]
    conn.close()
    return count

def update_ticket_status(ticket_id: int, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE support_tickets SET status = ?, updated_at = ? WHERE ticket_id = ?", (status, now, ticket_id))
    conn.commit()
    conn.close()

# ======================== বট স্লট মেয়াদ বৃদ্ধি (Extend Validity) রিকোয়েস্ট ইঞ্জিন ========================
def create_renewal_request(user_id: int, username: str, project_id: int, project_name: str, days: int, price: str, gateway: str, trx_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO renewal_requests (user_id, username, project_id, project_name, days, price, gateway, trx_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
    """, (user_id, username or "Anonymous", project_id, project_name, days, price, gateway, trx_id, now))
    req_id = cur.lastrowid
    conn.commit()
    conn.close()
    return req_id

def get_renewal_request(req_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM renewal_requests WHERE request_id = ?", (req_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_pending_renewal_requests():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM renewal_requests WHERE status = 'PENDING' ORDER BY request_id DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_renewal_request_status(req_id: int, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE renewal_requests SET status = ? WHERE request_id = ?", (status, req_id))
    conn.commit()
    conn.close()

# ======================== সার্ভার টেলিম্যাট্রি ও মেট্রিক্স ========================
def get_progress_bar(percent: float, length: int = 10) -> str:
    filled = int(length * (percent / 100))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.1f}%"

def get_bot_resource_usage(pid_num: int):
    try:
        p = psutil.Process(pid_num)
        mem_mb = p.memory_info().rss // (1024 * 1024)
        cpu = p.cpu_percent(interval=0.05)
        return mem_mb, cpu
    except Exception:
        return 0, 0.0

_api_check_cache = {"ts": 0.0, "ok": True}

def _telegram_api_online() -> bool:
    """api.telegram.org:443 পৌঁছানো যায় কিনা (৬০ সেকেন্ড ক্যাশ, ১.৫ সেকেন্ড টাইমআউট)।"""
    import socket
    now = time.time()
    if now - _api_check_cache["ts"] < 60:
        return _api_check_cache["ok"]
    try:
        with socket.create_connection(("api.telegram.org", 443), timeout=1.5):
            ok = True
    except Exception:
        ok = False
    _api_check_cache.update(ts=now, ok=ok)
    return ok

def _database_online() -> bool:
    try:
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False

def get_comprehensive_server_stats(active_processes: dict, footer: bool = True) -> str:
    cpu_percent = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net_io = psutil.net_io_counters()
    boot_dt = datetime.fromtimestamp(psutil.boot_time())

    users = get_all_users()
    total_users = len(users)
    paid_users = sum(1 for u in users if u.get("plan") in ("PRO", "PREMIUM", "BUSINESS", "VIP"))
    banned_users = sum(1 for u in users if u.get("is_banned") == 1)

    total_bot_ram_mb = 0
    live_owners = set()
    for key, pdata in active_processes.items():
        try:
            mem, _ = get_bot_resource_usage(pdata["process"].pid)
            total_bot_ram_mb += mem
        except Exception:
            pass
        try:
            uid = pdata.get("user_id")
            if uid is None and isinstance(key, tuple):
                uid = key[0]
            live_owners.add(uid if uid is not None else key)
        except Exception:
            live_owners.add(key)

    gb = 1024 ** 3
    filled = max(0, min(10, round(cpu_percent / 10)))
    cpu_bar = "▰" * filled + "▱" * (10 - filled)
    on, off = "🟢", "🔴"
    maint = bool(SYSTEM_CONFIG.get("maintenance_mode"))

    L = [
        "╭━━━〔 👑 PYHOST CLOUD 〕━━━╮",
        "┃     ⚡ ADVANCED TELEMETRY",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 🖥️ CPU    {cpu_percent:.1f}%  {cpu_bar}",
        f"┃ 🧠 RAM    {ram.percent:.1f}%  {ram.used / gb:.1f}/{ram.total / gb:.1f}GB",
        f"┃ 💾 DISK   {disk.percent:.1f}%  {disk.used // gb}/{disk.total // gb}GB",
        f"┃ 🤖 BOTS   {len(active_processes):02d}  • {total_bot_ram_mb}MB RAM",
        f"┃ 🌐 NET    ↑{net_io.bytes_sent // (1024*1024)}MB • ↓{net_io.bytes_recv // (1024*1024)}MB",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 👥 USERS  {total_users:02d}  │ 💎 PAID {paid_users:02d}",
        f"┃ 🚫 BANNED {banned_users:02d}  │ 🟢 LIVE {len(live_owners):02d}",
        "┃ ⚙️ PROCESS  ACTIVE",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ 📦 DATABASE   {on + ' ONLINE' if _database_online() else off + ' OFFLINE'}",
        f"┃ 🔌 API        {on + ' ONLINE' if _telegram_api_online() else off + ' OFFLINE'}",
        "┃ 🛡️ SECURITY   🟢 ACTIVE",
        f"┃ ⚡ MAINTENANCE {'🟡 ON' if maint else off + ' OFF'}",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ ⏱️ BOOT   {boot_dt.strftime('%d %b %Y • %H:%M')}",
        "┃ 📡 ENGINE MULTI-TENANT",
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━╯",
    ]
    if footer:
        L.append("        ⬇️ 𝗔𝗗𝗠𝗜𝗡 𝗠𝗘𝗡𝗨")
    return "\n".join(L)

# ======================== ব্যাকআপ ও ক্লিনআপ ইঞ্জিন ========================
def generate_system_backup():
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"pyhost_backup_{now_str}.zip"
    backup_filepath = os.path.join(BACKUP_DIR, backup_filename)

    with zipfile.ZipFile(backup_filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DB_PATH):
            zf.write(DB_PATH, arcname="pyhost_system.db")
        if os.path.exists(PROJECTS_DIR):
            for root, _, files in os.walk(PROJECTS_DIR):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, BASE_DIR)
                    zf.write(full_path, arcname=rel_path)

    return backup_filepath

def execute_server_cleanup():
    deleted_bytes = 0
    for root, _, files in os.walk(PROJECTS_DIR):
        for f in files:
            if f.endswith(".log"):
                lp = os.path.join(root, f)
                try:
                    size = os.path.getsize(lp)
                    if size > 2 * 1024 * 1024:
                        with open(lp, "w") as lf:
                            lf.write(f"--- Log rotated at {datetime.now()} ---\n")
                        deleted_bytes += size
                except Exception:
                    pass

    for root, dirs, _ in os.walk(BASE_DIR):
        for d in dirs:
            if d == "__pycache__":
                cp = os.path.join(root, d)
                try:
                    shutil.rmtree(cp)
                except Exception:
                    pass

    return deleted_bytes // 1024

# ======================== ব্যাচ ৩ — Achievement Badges ========================
ACHIEVEMENTS = {
    "FIRST_BOT":  {"emoji": "🎉", "title": "First Bot Deployed",    "desc": "আপনার প্রথম বট ডিপ্লয় করেছেন"},
    "UPTIME_7D":  {"emoji": "🔥", "title": "7 Day Uptime Warrior",  "desc": "একটি বট একটানা ৭ দিন সচল রেখেছেন"},
    "UPTIME_30D": {"emoji": "🏆", "title": "30 Day Uptime Legend",  "desc": "একটি বট একটানা ৩০ দিন সচল রেখেছেন"},
    "REFER_5":    {"emoji": "👥", "title": "5 Referrals Champion",  "desc": "৫ জনকে রেফার করেছেন"},
    "REFER_10":   {"emoji": "🌟", "title": "10 Referrals Legend",   "desc": "১০ জনকে রেফার করেছেন"},
    "FIRST_UPGRADE": {"emoji": "💎", "title": "VIP Member",         "desc": "প্রথমবার প্ল্যান আপগ্রেড করেছেন"},
}

def award_badge(user_id: int, badge_key: str) -> bool:
    """ব্যাজ দেয় (আগে থেকে থাকলে কিছু হয় না)। নতুন করে অ্যাওয়ার্ড হলে True রিটার্ন করে।"""
    if badge_key not in ACHIEVEMENTS:
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur.execute("INSERT INTO badges (user_id, badge_key, earned_at) VALUES (?, ?, ?)", (user_id, badge_key, now))
        conn.commit()
        newly_awarded = True
    except sqlite3.IntegrityError:
        newly_awarded = False
    conn.close()
    return newly_awarded

def get_user_badges(user_id: int) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT badge_key, earned_at FROM badges WHERE user_id = ? ORDER BY earned_at ASC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ======================== Leaderboard ========================
def add_uptime_seconds(user_id: int, seconds: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET total_uptime_seconds = COALESCE(total_uptime_seconds, 0) + ? WHERE user_id = ?", (seconds, user_id))
    conn.commit()
    conn.close()

def get_leaderboard_uptime(limit: int = 10) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, total_uptime_seconds FROM users WHERE total_uptime_seconds > 0 ORDER BY total_uptime_seconds DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_leaderboard_referral(limit: int = 10) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, referral_count FROM users WHERE referral_count > 0 ORDER BY referral_count DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ======================== Feedback / Rating ========================
def add_feedback(user_id: int, username: str, rating: int, comment: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO feedback (user_id, username, rating, comment, created_at) VALUES (?,?,?,?,?)",
                (user_id, username, rating, comment, now))
    conn.commit()
    conn.close()

def get_feedback_stats() -> dict:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt, AVG(rating) as avg_rating FROM feedback")
    row = cur.fetchone()
    conn.close()
    return {"count": row["cnt"] or 0, "avg_rating": round(row["avg_rating"], 2) if row["avg_rating"] else 0.0}

def get_recent_feedback(limit: int = 15) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ======================== Bot Clone ========================
def clone_project_record(user_id: int, source_project_id: int, new_name: str):
    """সোর্স প্রজেক্টের DB রেকর্ড কপি করে একটা নতুন খালি স্লটে বসায়। রিটার্ন করে (new_project_id, slot) অথবা (None, None)।"""
    src = get_project_by_id(source_project_id)
    if not src or src["user_id"] != user_id:
        return None, None
    slot = get_next_free_slot(user_id)
    if slot is None:
        return None, None
    new_pid = create_user_project(user_id, new_name, main_file=src["main_file"], slot_number=slot)
    # env vars কপি
    for ev in get_env_vars(source_project_id):
        set_env_var(new_pid, ev["key"], ev["value"])
    return new_pid, slot

# ======================== Scheduled Start/Stop ========================
def set_bot_schedule(project_id: int, start_time: str | None, stop_time: str | None, enabled: bool = True):
    """start_time/stop_time ফরম্যাট 'HH:MM' (Bangladesh সময়), None দিলে সেটা বাদ যাবে।"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE projects SET sched_start_time = ?, sched_stop_time = ?, sched_enabled = ? WHERE project_id = ?",
        (start_time, stop_time, 1 if enabled else 0, project_id)
    )
    conn.commit()
    conn.close()

def clear_bot_schedule(project_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET sched_start_time = NULL, sched_stop_time = NULL, sched_enabled = 0 WHERE project_id = ?", (project_id,))
    conn.commit()
    conn.close()

def get_scheduled_projects() -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE sched_enabled = 1 AND (sched_start_time IS NOT NULL OR sched_stop_time IS NOT NULL)")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_schedule_action(project_id: int, action_tag: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET sched_last_action = ? WHERE project_id = ?", (action_tag, project_id))
    conn.commit()
    conn.close()

# ======================== Language Toggle ========================
def get_user_language(user_id: int) -> str:
    u = get_user(user_id)
    return (u.get("language") or "bn") if u else "bn"

def set_user_language(user_id: int, lang: str):
    lang = "en" if lang == "en" else "bn"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

# ======================== Milestone Alerts (আপটাইম/রেফারেল) ========================
def has_milestone_sent(project_id: int, milestone: str) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM milestone_alerts WHERE project_id = ? AND milestone = ?", (project_id, milestone))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_milestone_sent(project_id: int, milestone: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur.execute("INSERT INTO milestone_alerts (project_id, milestone, sent_at) VALUES (?,?,?)", (project_id, milestone, now))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

# ======================== Auto-Broadcast (Scheduled) ========================
def create_scheduled_broadcast(message: str, send_at: str, created_by: int):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO broadcasts (message, send_at, created_by, created_at) VALUES (?,?,?,?)",
                (message, send_at, created_by, now))
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    return bid

def get_due_broadcasts():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM broadcasts WHERE sent = 0 AND send_at <= ?", (now,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_pending_broadcasts():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM broadcasts WHERE sent = 0 ORDER BY send_at ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_broadcast_sent(broadcast_id: int, sent_count: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE broadcasts SET sent = 1, sent_count = ? WHERE id = ?", (sent_count, broadcast_id))
    conn.commit()
    conn.close()

def cancel_broadcast(broadcast_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM broadcasts WHERE id = ? AND sent = 0", (broadcast_id,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0