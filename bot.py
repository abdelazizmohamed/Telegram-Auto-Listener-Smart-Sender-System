
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
bot.py — Listen/Live Only + Sender with:
- LIVE هو المصدر الوحيد لليوزرات (لا يوجد iter_chat_members).
- Permanent-failure -> move to errors/<flow>_dead.txt + حذف من ملف الـ flow.
- Temporary-failure -> mark sender temp-failed & skip to next (لا يوقف التسلسل العام).
- No duplicate send of same message to same user within N days from config (cooldown_days).
- سجلات في logs/send_log.csv + logs/sent_history.csv.
- Listen scope & keyword filter with SQLite sink for viewer bot, with source_tag per uni/group/kw.
- لو ما فيش @username: نخزن رابط الرسالة المباشر بدلًا منه في DB (ويُكتب للملفات فقط لو exclude_no_username=false).
- إشعار فوري عند أي رسالة تطابق الكيورد (kw) — يرسل إلى kw_notify_chat_id أو monitor_chat_id أو admin_ids.
- Auto Flow:
  * حساب السماع لا يرسل لأي مستخدم نهائيًا.
  * يجمع كل اليوزرات التي "تم سماعها" في ملف outputs/auto_all.txt (مرة واحدة لكل يوزر).
  * حسابات الإرسال هي التي ترسل:
      - رسالة AUTO (اختياري/افتراضيًا ON)
      - ورسائل كل Flow (يمكن تعطيلها من DB/Config).

✅ Exact keyword behavior المطلوب:
- "خصوصي" ✅
- "خصوصي." ✅  (لأننا بنفصل الكلمات عن علامات الترقيم)
- "خصوصا" ❌
- "الخصوصي" ❌ (لأنه كلمة مختلفة تمامًا)

✅ NEW (مهم للي طلبته):
1) دعم "جامعة = Uni Tag" منفصل عن القروب:
   - أضفنا عمود اختياري في جدول groups اسمه uni_tag.
   - لو uni_tag موجود، يبقى الرسائل من القروب ده تتسجل في DB تحت source_tag = uni_tag (مش اسم القروب).
   - ده يسمح إن "جامعة واحدة" تسمع لأكثر من قروب (كلهم نفس uni_tag).

2) إضافة مواد "لكل الجامعات مرة واحدة":
   - في جدول uni_sections تقدر تضيف سكشن بـ uni_tag = "__ALL__"
   - السكشنات اللي تحت "__ALL__" تتطبق على كل الجامعات (مع سكشنات الجامعة نفسها).
   - (UI هتعمل زر “إضافة سكشن عام لكل الجامعات” في mod_bot)

ملاحظة: الكود Backward Compatible:
- لو uni_tag مش موجود في DB أو فاضي، بنستخدم اسم القروب (gconf.name) كـ uni_tag.
"""

import argparse
import asyncio
import csv
import logging
import os
import random
import sys
import sqlite3
import json
import hashlib
import re
import time

from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import portalocker
import yaml
from pyrogram import Client
from pyrogram.errors import (
    FloodWait, RPCError, PeerIdInvalid, UserNotParticipant,
    UserDeactivated, UserIsBlocked, UsernameNotOccupied,
    UserPrivacyRestricted, BadRequest, AuthKeyDuplicated, PeerFlood
)
from pyrogram.types import Message

MAX_SEND_PER_TURN = 2          # كل حساب يرسل مرتين فقط
SEND_DELAY_RANGE = (15, 35)    # Delay عشوائي حقيقي

# OpenAI (optional)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore


# -----------------------
# OpenAI client (optional)
# -----------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai_client = None
if OpenAI and OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        openai_client = None



def normalize_recipient(s: str) -> str:
    """
    Normalize telegram recipient to @username if possible.
    Accepts:
    - @username
    - username
    - https://t.me/username
    """
    s = (s or "").strip()
    if not s:
        return s

    # t.me link
    if s.startswith("https://t.me/"):
        s = s.replace("https://t.me/", "").strip().strip("/")

    # already normalized
    if s.startswith("@"):
        return s

    # raw username
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", s):
        return "@" + s

    return s

# -----------------------
# Time helpers (UTC aware)
# -----------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_iso(timespec: str = "seconds") -> str:
    return utc_now().isoformat(timespec=timespec)


# -----------------------
# Logging
# -----------------------
def setup_logging(path: Optional[str], level: str = "INFO"):
    levelno = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("tg_live")
    logger.setLevel(levelno)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(path)
                   for h in logger.handlers):
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)

    return logger


# -----------------------
# File helpers (locks)
# -----------------------
def atomic_write_lines(path: str, lines: List[str]):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(l.rstrip("\n") + "\n")
    os.replace(tmp, path)

@asynccontextmanager
async def file_lock(path: str, mode="a+"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    f = open(path, mode, encoding="utf-8")
    try:
        portalocker.lock(f, portalocker.LOCK_EX)
        yield f
    finally:
        try:
            f.flush()
            os.fsync(f.fileno())
        except Exception:
            pass
        portalocker.unlock(f)
        f.close()

def read_lines_unique(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    seen = set()
    out = []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def append_unique_line(path: str, line: str) -> bool:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        portalocker.lock(f, portalocker.LOCK_EX)
        f.seek(0)
        lines = [ln.strip() for ln in f if ln.strip()]
        if line in lines:
            portalocker.unlock(f)
            return False
        f.write(line + "\n")
        portalocker.unlock(f)
    return True

def remove_username_from_file_atomic(path: str, username: str):
    lines = read_lines_unique(path)
    new = [l for l in lines if l != username]
    atomic_write_lines(path, new)

def move_line_to_end_atomic(path: str, line: str) -> bool:
    lines = read_lines_unique(path)
    if not lines:
        return False
    found = False
    new = []
    for l in lines:
        if (not found) and l == line:
            found = True
            continue
        new.append(l)
    if not found:
        return False
    new.append(line)
    atomic_write_lines(path, new)
    return True

def append_line(path: str, line: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


# -----------------------
# SQLite store for listened messages & admin workflow
# -----------------------
DB_PATH = "data/listen_messages.db"

def db_add_column_if_missing(table: str, column: str, col_def: str):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        conn.commit()
    conn.close()

def _db_init_broadcast(cur):
    """
    جدول المنطق الثالث:
    كل مستخدم اتسمع في أي جروب (حسب الإعدادات)
    """
    cur.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        chat_id INTEGER,
        chat_username TEXT,
        created_at TEXT
    )
    """)



def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # messages
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        chat_username TEXT,
        user_id INTEGER,
        username TEXT,
        text TEXT,
        date TEXT,
        deleted INTEGER DEFAULT 0,
        edited_text TEXT
    )
    """)

    cur.execute("""CREATE TABLE IF NOT EXISTS blocked_users(user_id INTEGER PRIMARY KEY)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS approved_viewers(user_id INTEGER PRIMARY KEY)""")

    # Manual university sections (subjects)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS uni_sections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uni_tag TEXT NOT NULL,
        section_name TEXT NOT NULL,
        keywords_json TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_uni_sections_unique ON uni_sections(uni_tag, section_name)""")

    # Dynamic groups table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        chat TEXT NOT NULL,
        out_file TEXT NOT NULL,
        template_text TEXT NOT NULL,
        attachments_enabled INTEGER DEFAULT 0,
        send_enabled INTEGER DEFAULT 1,
        subjects_only INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    #_db_init_broadcast(cur)

    conn.commit()
    conn.close()

    # migrations
    db_add_column_if_missing("messages", "source_tag", "TEXT")
    db_add_column_if_missing("messages", "uni_subjects", "TEXT")
    db_add_column_if_missing("groups", "uni_tag", "TEXT")


def db_load_enabled_sections() -> Dict[str, List[Tuple[str, List[str]]]]:
    """Return: {uni_tag: [(section_name, [kw..]), ...]}"""
    out: Dict[str, List[Tuple[str, List[str]]]] = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""SELECT uni_tag, section_name, keywords_json
                       FROM uni_sections
                       WHERE COALESCE(enabled,1)=1""")
        for uni_tag, section_name, kws_json in cur.fetchall():
            try:
                kws = json.loads(kws_json or "[]")
                if not isinstance(kws, list):
                    kws = []
            except Exception:
                kws = []
            kws = [str(k).strip() for k in kws if str(k).strip()]
            out.setdefault(str(uni_tag).strip(), []).append((str(section_name).strip(), kws))
        conn.close()
    except Exception:
        return {}
    return out


# -----------------------
# Dynamic groups (stored in DB; editable from mod_bot)
# -----------------------
def db_seed_groups_from_config(cfg_groups):
    """Seed groups table from config.yaml on first run (only if table empty)."""
    if not cfg_groups:
        return
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(1) FROM groups")
        n = int(cur.fetchone()[0] or 0)
    except Exception:
        conn.close()
        return
    if n > 0:
        conn.close()
        return

    now = utc_now().isoformat()
    for g in cfg_groups:
        tpl_text = ""
        try:
            if getattr(g, "template_path", None) and os.path.exists(g.template_path):
                with open(g.template_path, "r", encoding="utf-8") as f:
                    tpl_text = f.read().strip()
        except Exception:
            tpl_text = ""

        name = str(g.name).strip()
        if not name:
            name = str(g.chat)

        out_file = str(getattr(g, "out_file", "") or "").strip() or f"outputs/{name}.txt"
        chat_s = str(getattr(g, "chat", ""))

        cur.execute(
            """INSERT OR IGNORE INTO groups
               (name, chat, out_file, template_text, attachments_enabled, send_enabled, subjects_only, uni_tag, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                chat_s,
                out_file,
                tpl_text,
                1 if bool(getattr(g, "attachments_enabled", False)) else 0,
                1 if bool(getattr(g, "send_enabled", True)) else 0,
                1 if bool(getattr(g, "subjects_only", False)) else 0,
                (getattr(g, "uni_tag", None) or "").strip(),
                now,
                now,
            )
        )
    conn.commit()
    conn.close()


def db_load_groups():
    """Load groups from DB as GroupConfig list. Template is stored inline in DB."""
    groups: List[GroupConfig] = []
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ✅ uni_tag may be missing in old DBs; we handle both
    cur.execute("PRAGMA table_info(groups)")
    cols = {r[1] for r in cur.fetchall()}
    has_uni_tag = "uni_tag" in cols

    if has_uni_tag:
        cur.execute("""SELECT name, chat, out_file, template_text,
                              COALESCE(attachments_enabled,0),
                              COALESCE(send_enabled,1),
                              COALESCE(subjects_only,0),
                              COALESCE(uni_tag,'')
                       FROM groups
                       ORDER BY id ASC""")
    else:
        cur.execute("""SELECT name, chat, out_file, template_text,
                              COALESCE(attachments_enabled,0),
                              COALESCE(send_enabled,1),
                              COALESCE(subjects_only,0)
                       FROM groups
                       ORDER BY id ASC""")
    rows = cur.fetchall()
    conn.close()

    for row in rows:
        if has_uni_tag:
            name, chat, out_file, template_text, att, send_en, subj_only, uni_tag = row
        else:
            name, chat, out_file, template_text, att, send_en, subj_only = row
            uni_tag = ""

        chat_val: Union[str, int]
        chat_s = str(chat).strip()
        if chat_s.lstrip("-").isdigit():
            try:
                chat_val = int(chat_s)
            except Exception:
                chat_val = chat_s
        else:
            chat_val = chat_s

        g = GroupConfig(
            name=str(name),
            chat=chat_val,
            out_file=str(out_file),
            template_path=None,
            attachments_enabled=bool(att),
            send_enabled=bool(send_en),
            subjects_only=bool(subj_only),
            uni_tag=(str(uni_tag).strip() or None),
        )
        setattr(g, "_template_text", (template_text or "").strip())
        groups.append(g)

    return groups


_GROUPS_CACHE = {"ts": 0.0, "by_user": {}, "by_id": {}, "list": []}

def _build_group_maps(groups):
    def norm_key(chat: Union[str, int]) -> str:
        if isinstance(chat, int):
            return str(chat)
        s = str(chat).strip()
        if s.startswith("@"):
            return s[1:].lower()
        return s.lower()

    by_user: Dict[str, GroupConfig] = {}
    by_id: Dict[str, GroupConfig] = {}
    for g in groups:
        if isinstance(g.chat, int) or str(g.chat).lstrip("-").isdigit():
            by_id[str(g.chat)] = g
        else:
            by_user[norm_key(g.chat)] = g
    return by_user, by_id


def get_groups_cached(ttl_sec: float = 30.0):
    """Return (groups_list, by_username, by_id) with TTL cache."""
    now_ts = utc_now().timestamp()
    if (now_ts - _GROUPS_CACHE["ts"]) > ttl_sec or not _GROUPS_CACHE["list"]:
        glist = db_load_groups()
        by_user, by_id = _build_group_maps(glist)
        _GROUPS_CACHE["list"] = glist
        _GROUPS_CACHE["by_user"] = by_user
        _GROUPS_CACHE["by_id"] = by_id
        _GROUPS_CACHE["ts"] = now_ts
    return list(_GROUPS_CACHE["list"]), dict(_GROUPS_CACHE["by_user"]), dict(_GROUPS_CACHE["by_id"])


def db_insert_message(
    chat_id,
    chat_username,
    user_id,
    username,
    text,
    date_iso,
    source_tag: Optional[str],
    uni_subjects_json: Optional[str] = None,
):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages(
            chat_id, chat_username, user_id, username, text, date, source_tag, uni_subjects
        )
        VALUES(?,?,?,?,?,?,?,?)
    """, (chat_id, chat_username, user_id, username, text, date_iso, source_tag, uni_subjects_json))
    conn.commit()
    conn.close()

def db_block_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO blocked_users(user_id) VALUES(?)", (user_id,))
    conn.commit()
    conn.close()

def db_is_blocked(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


# -----------------------
# Config dataclasses
# -----------------------
@dataclass
class RateRange:
    min_seconds: float
    max_seconds: float

@dataclass
class GroupConfig:
    name: str
    chat: Union[str, int]   # @username or id
    out_file: str
    template_path: Optional[str] = None
    attachments_enabled: bool = False
    send_enabled: bool = True
    subjects_only: bool = False
    # ✅ NEW: many chats can map to same "university tag"
    uni_tag: Optional[str] = None

@dataclass
class AutoFlowConfig:
    enabled: bool = True
    out_file: str = "outputs/auto_all.txt"
    template_path: str = "templates/auto_offer.txt"
    attachments_enabled: bool = False

@dataclass
class AppConfig:
    api_id: int
    api_hash: str
    listener_session: str
    sender_sessions: List[str]
    groups: List[GroupConfig]
    send_mode: str
    global_rate: RateRange
    per_account_rate: Optional[Dict[str, RateRange]]
    dry_run: bool
    max_retries: int
    exclude_no_username: bool
    proxy: Optional[dict]
    logging_path: Optional[str]
    logging_level: str
    consent_required: bool
    cooldown_days: int
    listen_scope: str
    keyword_filter: Optional[dict]
    viewer_bot: Optional[dict]
    auto_flow: AutoFlowConfig


# -----------------------
# Proxy parsing
# -----------------------
def parse_proxy(proxy_url: Optional[str]) -> Optional[dict]:
    if not proxy_url:
        return None
    u = urlparse(proxy_url)
    if not u.scheme or not u.hostname or not u.port:
        return None
    d = {"scheme": u.scheme, "hostname": u.hostname, "port": u.port}
    if u.username:
        d["username"] = u.username
    if u.password:
        d["password"] = u.password
    return d


# -----------------------
# Config loader
# -----------------------
def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    groups: List[GroupConfig] = []
    for g in raw.get("groups", []):
        groups.append(GroupConfig(
            name=g.get("name") or str(g.get("chat")),
            chat=g["chat"],
            out_file=g["out_file"],
            template_path=g.get("template_path"),
            attachments_enabled=bool(g.get("attachments_enabled", False)),
            send_enabled=bool(g.get("send_enabled", True)),
            subjects_only=bool(g.get("subjects_only", False)),
            uni_tag=(str(g.get("uni_tag", "")).strip() or None),
        ))

    gr = raw.get("global_rate", {"min_seconds": 2, "max_seconds": 5})

    per_acc_raw = raw.get("per_account_rate")
    per_acc = None
    if per_acc_raw:
        per_acc = {}
        for k, v in per_acc_raw.items():
            per_acc[k] = RateRange(float(v["min"]), float(v["max"]))

    proxy_conf = parse_proxy(raw.get("proxy"))

    auto_raw = raw.get("auto_flow") or raw.get("auto_reply") or {}
    auto_flow = AutoFlowConfig(
        enabled=bool(auto_raw.get("enabled", True)),
        out_file=str(auto_raw.get("out_file", "outputs/auto_all.txt")),
        template_path=str(auto_raw.get("template_path", "templates/auto_offer.txt")),
        attachments_enabled=bool(auto_raw.get("attachments_enabled", False)),
    )

    return AppConfig(
        api_id=int(raw["api_id"]),
        api_hash=str(raw["api_hash"]),
        listener_session=raw["listener_session"],
        sender_sessions=list(raw.get("sender_sessions", [])),
        groups=groups,
        send_mode=raw.get("send_mode", "round_robin"),
        global_rate=RateRange(float(gr.get("min_seconds", 2)), float(gr.get("max_seconds", 5))),
        per_account_rate=per_acc,
        dry_run=bool(raw.get("dry_run", True)),
        max_retries=int(raw.get("max_retries", 3)),
        exclude_no_username=bool(raw.get("exclude_no_username", True)),
        proxy=proxy_conf,
        logging_path=(raw.get("logging") or {}).get("path"),
        logging_level=(raw.get("logging") or {}).get("level", "INFO"),
        consent_required=bool(raw.get("consent_required", False)),
        cooldown_days=int(raw.get("cooldown_days", 5)),
        listen_scope=str(raw.get("listen_scope", "configured")),
        keyword_filter=raw.get("keyword_filter", None),
        viewer_bot=raw.get("viewer_bot", None),
        auto_flow=auto_flow,
    )


# -----------------------
# Helpers
# -----------------------
class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"

def format_message(template_text: str, username: str, group_name: str, sender_name: str):
    return template_text.format_map(SafeDict(username=username, group_name=group_name, sender_name=sender_name))

def message_key_for(name: str, template_path: Optional[str]) -> str:
    tpl = Path(template_path).name if template_path else "default"
    return f"{name}:{tpl}"

def load_permanent_blocked(path: str = "blocked_senders.txt") -> set:
    blocked = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if s:
                    blocked.add(s)
    return blocked

def choose_sender_index(
    mode: str,
    sender_sessions: List[str],
    last_index: int,
    disabled_until: Dict[str, float],
    now_ts: float,
    perma_blocked: set
) -> Optional[int]:
    n = len(sender_sessions)
    if n == 0:
        return None

    def is_available(sess):
        if sess in perma_blocked:
            return False
        return now_ts >= disabled_until.get(sess, 0.0)

    if mode == "single_account":
        return 0 if is_available(sender_sessions[0]) else None

    if mode == "random":
        order = list(range(n))
        random.shuffle(order)
        for i in order:
            if is_available(sender_sessions[i]):
                return i
        return None

    # round_robin
    for _ in range(n):
        last_index = (last_index + 1) % n
        if is_available(sender_sessions[last_index]):
            return last_index

    return None

def jitter_sleep_sec(cfg: AppConfig, session_path: str) -> float:
    if cfg.per_account_rate and session_path in cfg.per_account_rate:
        r = cfg.per_account_rate[session_path]
    else:
        r = cfg.global_rate
    return random.uniform(r.min_seconds, r.max_seconds)

def load_template_text(path: Optional[str]) -> str:
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    default_path = "templates/default.txt"
    if os.path.exists(default_path):
        with open(default_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "السلام عليكم @{username}"

def build_message_link(chat, message_id: int) -> Optional[str]:
    try:
        chat_username = getattr(chat, "username", None)
        if chat_username:
            return f"https://t.me/{chat_username}/{message_id}"
        cid = int(getattr(chat, "id", 0) or 0)
        if cid == 0:
            return None
        abs_id = abs(cid)
        internal = abs_id - 1000000000000 if str(abs_id).startswith("100") else abs_id
        return f"https://t.me/c/{internal}/{message_id}"
    except Exception:
        return None

def is_valid_tg_username_recipient(s: str) -> bool:
    """
    Valid Telegram username:
    @username (5–32 chars, letters/numbers/_)
    """
    if not s or not s.startswith("@"):
        return False
    return bool(re.fullmatch(r"@[A-Za-z0-9_]{5,32}", s))


def slugify_section_name(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^0-9a-zA-Z_]+", "", s)
    if s:
        return s[:60]
    h = hashlib.md5((name or "").encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"sec_{h}"

def match_manual_sections(text: str, sections: List[Tuple[str, List[str]]], case_insensitive: bool = True) -> List[str]:
    if not text or not sections:
        return []
    t = text.lower() if case_insensitive else text
    matched: List[str] = []
    for sec_name, kws in sections:
        for kw in (kws or []):
            if not kw:
                continue
            k = kw.lower() if case_insensitive else kw
            if k and k in t:
                matched.append(sec_name)
                break
    seen = set()
    out = []
    for s in matched:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------- KW notifications helpers ----------
def _normalize_target(t: Union[int, str]) -> Optional[Union[int, str]]:
    if t is None:
        return None
    if isinstance(t, int):
        return t
    s = str(t).strip()
    if s.startswith("https://t.me/"):
        s = s.replace("https://t.me/", "").strip().strip("/")
    if s.startswith("@"):
        s = s[1:]
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except Exception:
            return s
    return s

async def send_kw_notifications(
    bot_client: Optional[Client],
    targets: List[Union[int, str]],
    text: str,
    logger: logging.Logger
):
    if not bot_client or not targets:
        return

    bad = set()
    for raw in list(targets):
        chat_id = _normalize_target(raw)
        if chat_id is None:
            bad.add(raw)
            continue
        try:
            await bot_client.send_message(chat_id, text, disable_web_page_preview=True)
        except PeerIdInvalid as e:
            logger.warning(f"[KW-NOTIFY] PeerIdInvalid -> drop target for this run: {raw} ({e})")
            bad.add(raw)
        except Exception as e:
            logger.warning(f"[KW-NOTIFY] failed to send to {raw}: {e}")

    if bad:
        targets[:] = [t for t in targets if t not in bad]
        logger.info(f"[KW-NOTIFY] remaining targets after drop: {targets}")


# -----------------------
# Sent history (no-duplicate within cfg.cooldown_days)
# -----------------------
HISTORY_PATH = "logs/sent_history.csv"

def load_sent_history(path: str = HISTORY_PATH) -> Dict[Tuple[str, str], datetime]:
    hist: Dict[Tuple[str, str], datetime] = {}
    if not os.path.exists(path):
        return hist
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        _ = next(reader, None)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row[0])
                uname = row[1]
                mkey = row[2]
                hist[(uname, mkey)] = ts
            except Exception:
                continue
    return hist

def append_sent_history(username: str, message_key: str, path: str = HISTORY_PATH):
    head = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if head:
            w.writerow(["timestamp", "username", "message_key"])
        w.writerow([utc_now().isoformat(), username, message_key])

def within_history_window(hist: Dict[Tuple[str, str], datetime], username: str, message_key: str, days: int) -> bool:
    dt = hist.get((username, message_key))
    if not dt:
        return False
    return (utc_now() - dt) < timedelta(days=days)


# -----------------------
# OpenAI: extract university subjects (optional)
# -----------------------
def extract_university_subjects_with_ai(text: str, logger: logging.Logger) -> List[str]:
    if not text or not text.strip():
        return []
    if openai_client is None:
        return []

    try:
        user_prompt = (
            "أنت مساعد ذكي في جامعة.\n"
            "سأعطيك رسالة دردشة من طالب أو مجموعة طلاب. "
            "الرسالة قد تحتوي على أسماء مواد دراسية في الجامعة (أي كلية) بالعربي أو بالإنجليزي.\n\n"
            "المطلوب:\n"
            "1) استخرج أسماء المواد الجامعية فقط.\n"
            "2) تجاهل كلمات مثل: امتحان، دكتور، محاضرة، سكشن.\n"
            "3) ارجع JSON فقط بهذا الشكل:\n"
            '{ "subjects": ["اسم مادة 1", "اسم مادة 2"] }\n'
            "ولو لا توجد مواد:\n"
            '{ "subjects": [] }\n\n'
            f"النص:\n\"\"\"\n{text}\n\"\"\"\n"
        )

        resp = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "You extract university course names from text and return ONLY valid JSON."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )

        content = (resp.choices[0].message.content or "").strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                logger.warning("AI response is not valid JSON: %s", content[:200])
                return []
            data = json.loads(match.group(0))

        subjects = data.get("subjects", [])
        subjects = [s.strip() for s in subjects if isinstance(s, str) and s.strip()]
        return subjects

    except Exception as e:
        logger.exception("Error while calling OpenAI for university subject extraction: %s", e)
        return []


# -----------------------
# LISTEN MODE (NO SENDING TO USERS)
# -----------------------
async def run_listener(cfg: AppConfig, logger: logging.Logger):
    kw_conf = cfg.keyword_filter or {}
    kw_enabled = bool(kw_conf.get("enabled", False))
    kw_ci = bool(kw_conf.get("case_insensitive", True))

    DEFAULT_EXACT_KEYWORDS = ["مشروع","خصوصى","خصوصي","خصوصيين","خصو"]

    raw_kw_list = kw_conf.get("keywords", [])
    if not raw_kw_list:
        raw_kw_list = DEFAULT_EXACT_KEYWORDS

    kw_list = [str(k).strip() for k in raw_kw_list if str(k).strip()]

    kw_out_file = str(kw_conf.get("out_file", "outputs/keyword_hits.txt"))
    if kw_enabled:
        os.makedirs(os.path.dirname(kw_out_file) or ".", exist_ok=True)

    auto_enabled = bool(cfg.auto_flow.enabled)
    auto_out_file = cfg.auto_flow.out_file
    if auto_enabled:
        os.makedirs(os.path.dirname(auto_out_file) or ".", exist_ok=True)

    uni_out_file = "outputs/uni_subjects.jsonl"
    os.makedirs(os.path.dirname(uni_out_file) or ".", exist_ok=True)

    sections_cache: Dict[str, List[Tuple[str, List[str]]]] = {}
    sections_cache_ts: float = 0.0
    sections_cache_ttl: float = 30.0

    GLOBAL_UNI_TAG = "__ALL__"  # ✅ سكشنات عامة لكل الجامعات

    def get_sections_for_uni(uni_tag: str) -> List[Tuple[str, List[str]]]:
        nonlocal sections_cache, sections_cache_ts
        now_ts = utc_now().timestamp()
        if (now_ts - sections_cache_ts) > sections_cache_ttl or not sections_cache:
            sections_cache = db_load_enabled_sections()
            sections_cache_ts = now_ts

        uni_tag = (uni_tag or "").strip()
        uni_secs = sections_cache.get(uni_tag, [])
        global_secs = sections_cache.get(GLOBAL_UNI_TAG, [])
        # global + uni (بدون تكرار section_name)
        merged: List[Tuple[str, List[str]]] = []
        seen = set()
        for sec_name, kws in (global_secs + uni_secs):
            key = (sec_name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append((sec_name, kws))
        return merged

    sections_base_dir = "outputs/sections"
    os.makedirs(sections_base_dir, exist_ok=True)

    app = Client(
        name=cfg.listener_session,
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        proxy=cfg.proxy
    )

    notify_bot: Optional[Client] = None
    notify_targets: List[Union[int, str]] = []
    vb = cfg.viewer_bot or {}
    vb_token = (vb.get("bot_token") if isinstance(vb, dict) else None)

    if isinstance(vb, dict):
        if vb.get("kw_notify_chat_id") is not None:
            notify_targets.append(vb.get("kw_notify_chat_id"))
        elif vb.get("monitor_chat_id") is not None:
            notify_targets.append(vb.get("monitor_chat_id"))
        for aid in (vb.get("admin_ids") or []):
            notify_targets.append(aid)

    cleaned: List[Union[int, str]] = []
    for t in notify_targets:
        nt = _normalize_target(t)
        if nt is not None and nt not in cleaned:
            cleaned.append(nt)
    notify_targets = cleaned

    if vb_token and notify_targets:
        try:
            notify_bot = Client(
                "kw_notify_bot",
                api_id=cfg.api_id,
                api_hash=cfg.api_hash,
                bot_token=vb_token,
                workdir="sessions"
            )
            await notify_bot.start()
            logger.info(f"[KW-NOTIFY] notifier bot started. Targets: {notify_targets}")
        except AuthKeyDuplicated as e:
            logger.warning(f"[KW-NOTIFY] AuthKeyDuplicated (bot already running elsewhere). Notifications disabled. {e}")
            notify_bot = None
        except Exception as e:
            logger.warning(f"[KW-NOTIFY] cannot start notifier bot. Notifications disabled. {e}")
            notify_bot = None

    # ✅ Exact word match with punctuation-safe tokenization
    WORD_RE = re.compile(r"[0-9A-Za-z\u0600-\u06FF]+", re.UNICODE)

    def _tokenize_words(text: str) -> List[str]:
        if not text:
            return []
        t = text.lower() if kw_ci else text
        return WORD_RE.findall(t)

    KW_SET = set([k.lower() if kw_ci else k for k in kw_list])

    def contains_keyword(text: str) -> bool:
        if not text:
            return False
        words = _tokenize_words(text)
        return any(w in KW_SET for w in words)

    def get_group_conf_for(chat) -> Optional[GroupConfig]:
        _, by_user, by_id = get_groups_cached(30.0)
        if getattr(chat, "username", None):
            return by_user.get(str(chat.username).strip().lower())
        if getattr(chat, "id", None):
            return by_id.get(str(chat.id))
        return None

    def uni_tag_for_group(gconf: Optional[GroupConfig]) -> Optional[str]:
        if not gconf:
            return None
        ut = (gconf.uni_tag or "").strip()
        return ut if ut else (gconf.name or "").strip() or None

    @app.on_message()
    async def on_message(client: Client, msg: Message):
        try:
            chat = msg.chat
            if not chat:
                return

            user = msg.from_user
            if not user:
                return
            if db_is_blocked(user.id):
                return

            gconf = get_group_conf_for(chat)
            utag = uni_tag_for_group(gconf)  # ✅ الجامعة الفعلية اللي هنخزن تحتها

            raw_username = (user.username or "").strip()
            has_username = bool(raw_username)

            mtext = msg.text or msg.caption or ""
            chat_username = getattr(chat, "username", None)

            manual_sections: List[str] = []
            if utag:
                sec_defs = get_sections_for_uni(utag)
                manual_sections = match_manual_sections(mtext, sec_defs, case_insensitive=True)

            uni_subjects_ai = extract_university_subjects_with_ai(mtext, logger)

            subjects_all: List[str] = []
            for s in (uni_subjects_ai or []) + (manual_sections or []):
                if isinstance(s, str) and s.strip() and s.strip() not in subjects_all:
                    subjects_all.append(s.strip())
            uni_json = json.dumps(subjects_all, ensure_ascii=False) if subjects_all else None

            hit_kw = kw_enabled and contains_keyword(mtext)

            # listen_scope=configured: اسمع بس للجروبات اللي موجودة في DB/config
            if (not hit_kw) and (not subjects_all) and cfg.listen_scope == "configured" and not gconf:
                return

            msg_link = None
            if not has_username:
                msg_link = build_message_link(chat, msg.id)

            # ✅ source_tag logic:
            # - kw -> "kw"
            # - else if we have utag -> utag  (ده اللي بيوحّد كذا قروب لنفس الجامعة)
            # - else fallback
            if hit_kw:
                source_tag = "kw"
            elif utag:
                source_tag = utag
            elif subjects_all:
                source_tag = "uni_ai"
            else:
                source_tag = chat_username or str(chat.id)

            display_id = raw_username if has_username else (msg_link or str(user.id))

            should_capture_group = bool(gconf and (not gconf.subjects_only or hit_kw or subjects_all))
            if hit_kw or subjects_all or should_capture_group:
                db_insert_message(
                    chat_id=chat.id,
                    chat_username=chat_username if chat_username else str(chat.id),
                    user_id=user.id,
                    username=display_id,
                    text=mtext,
                    date_iso=utc_now().isoformat(),
                    source_tag=source_tag,
                    uni_subjects_json=uni_json
                )

            if auto_enabled:
                if has_username:
                    to_write = f"@{raw_username}" if not raw_username.startswith("@") else raw_username
                    if append_unique_line(auto_out_file, to_write):
                        logger.info(f"[AUTO] + {to_write} -> {auto_out_file}")
                else:
                    if not cfg.exclude_no_username:
                        to_write = msg_link or str(user.id)
                        if to_write and append_unique_line(auto_out_file, to_write):
                            logger.info(f"[AUTO] + {to_write} -> {auto_out_file}")

            if subjects_all:
                record = {
                    "username_or_link": display_id,
                    "chat_id": chat.id,
                    "chat_username": chat_username if chat_username else str(chat.id),
                    "uni_tag": utag or source_tag,
                    "subjects": subjects_all,
                    "text": mtext,
                    "date": utc_now().isoformat()
                }
                append_line(uni_out_file, json.dumps(record, ensure_ascii=False))
                logger.info(f"[UNI-SUBJECT] {display_id} -> {uni_out_file} | subjects={subjects_all}")

            if hit_kw and kw_enabled:
                if has_username or not cfg.exclude_no_username:
                    to_write = (
                        f"@{raw_username}" if has_username and not raw_username.startswith("@")
                        else (raw_username if has_username else (msg_link or str(user.id)))
                    )
                    if to_write and append_unique_line(kw_out_file, to_write):
                        logger.info(f"[LISTEN/KEYWORD] + {to_write} -> {kw_out_file}")

            # ملفات القروبات (للسندر): ما زالت per-group out_file
            if gconf and (not gconf.subjects_only or hit_kw or subjects_all):
                if has_username or not cfg.exclude_no_username:
                    to_write = (
                        f"@{raw_username}" if has_username and not raw_username.startswith("@")
                        else (raw_username if has_username else (msg_link or str(user.id)))
                    )
                    if to_write and append_unique_line(gconf.out_file, to_write):
                        logger.info(f"[LISTEN] + {to_write} -> {gconf.out_file} (group={gconf.name})")

            # ملفات السكشنات: نجمعها تحت outputs/sections/<uni_tag>/...
            if utag and manual_sections and has_username:
                handle = f"@{raw_username}" if not raw_username.startswith("@") else raw_username
                uni_dir = os.path.join(sections_base_dir, utag)
                os.makedirs(uni_dir, exist_ok=True)
                for sec in manual_sections:
                    slug = slugify_section_name(sec)
                    sec_path = os.path.join(uni_dir, f"{slug}.txt")
                    if append_unique_line(sec_path, handle):
                        logger.info(f"[SECTION] + {handle} -> {sec_path} ({sec})")

            if hit_kw and notify_bot and notify_targets:
                snippet = (mtext or "")
                if len(snippet) > 300:
                    snippet = snippet[:300] + "…"
                link_for_display = msg_link or ""
                text_notify = (
                    "🟨 <b>[kw] التقطنا رسالة جديدة</b>\n"
                    f"📌 <b>الشات:</b> {chat_username or chat.id}\n"
                    f"🏛️ <b>uni_tag:</b> {(utag or '—')}\n"
                    f"👤 <b>المرسِل:</b> {display_id}\n"
                    f"🕒 <b>الوقت:</b> {utc_iso('seconds')}\n"
                    f"🔗 <b>الرابط:</b> {link_for_display or '—'}\n"
                    f"\n<code>{snippet}</code>"
                )
                await send_kw_notifications(notify_bot, notify_targets, text_notify, logger)

        except Exception as e:
            logger.exception(f"[LISTEN] error: {e}")

    await app.start()
    try:
        await asyncio.Future()
    finally:
        await app.stop()
        if notify_bot:
            try:
                await notify_bot.stop()
            except Exception:
                pass


# -----------------------
# SEND MODE
# -----------------------
def append_send_log(path: str, row: Tuple):
    head = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if head:
            w.writerow(["timestamp", "username", "group", "sender_session", "result", "error"])
        w.writerow(row)

async def send_one_message(
    cfg,
    logger,
    client,
    session_path: str,
    username: str,
    text: str,
    *,
    perma_blocked: set,
    disabled_until: Dict[str, float],
    sent_hist: Dict,
    send_counter: Dict[str, int],
    attachments: Optional[List[dict]] = None,
) -> bool:

    now_ts = time.time()
    peer_flood_cooldown = 6 * 3600
    generic_error_cooldown = 300

    # 🔢 كل حساب يرسل مرتين فقط في الدور
    count = send_counter.get(session_path, 0)
    if count >= 2:
        disabled_until[session_path] = now_ts + random.uniform(20, 40)
        logger.info(f"[SEND] session {session_path} reached limit (2 sends) → cooldown")
        return False

    try:
        # ⏱ Delay عشوائي قبل الإرسال
        await asyncio.sleep(random.uniform(2.5, 6.0))

        await client.send_message(username, text)

        send_counter[session_path] = count + 1
        sent_hist[username.lower()] = now_ts
        return True

    except PeerFlood:
        disabled_until[session_path] = now_ts + peer_flood_cooldown
        logger.warning(f"[SEND] PEER_FLOOD -> TEMP disable {session_path}")
        return False

    except UserPrivacyRestricted:
        logger.info(f"[SEND] privacy restricted @{username} via {session_path}")
        return False

    except BadRequest as e:
        logger.info(f"[SEND] bad request @{username} via {session_path}: {e}")
        return False

    except FloodWait as e:
        wait_s = int(getattr(e, "seconds", 60))
        disabled_until[session_path] = now_ts + wait_s + 2
        logger.warning(f"[SEND] FLOOD_WAIT {wait_s}s -> disable {session_path}")
        return False

    except Exception as e:
        disabled_until[session_path] = now_ts + generic_error_cooldown
        logger.warning(f"[SEND] error @{username} via {session_path}: {e}")
        return False


async def _process_file_sends(
    *,
    cfg: AppConfig,
    logger: logging.Logger,
    clients: Dict[str, Client],
    perma_blocked: set,
    disabled_until: Dict[str, float],
    sent_hist: Dict,
    file_path: str,
    flow_name: str,
    template_path: Optional[str],
    attachments_enabled: bool,
    last_idx_ref: Dict[str, int],
    state_lock: asyncio.Lock,
    template_text_override: Optional[str] = None,
):
    logger.info(f"[SEND] processing {file_path} (flow={flow_name})")

    template_text = (
        template_text_override
        if template_text_override
        else load_template_text(template_path)
    )

    message_key = message_key_for(flow_name, template_path)

    # ✅ NEW: عداد الإرسال لكل session
    send_counter: Dict[str, int] = {}   # <——— الحل هنا

    while True:
        usernames = read_lines_unique(file_path)
        if not usernames:
            logger.info(f"[SEND] no usernames in {file_path}. sleeping 300s")
            await asyncio.sleep(300)
            continue

        for username in list(usernames):
            uname = normalize_recipient(username)
            if not is_valid_tg_username_recipient(uname):
                remove_username_from_file_atomic(file_path, username)
                continue

            if within_history_window(sent_hist, uname, message_key, cfg.cooldown_days):
                move_line_to_end_atomic(file_path, username)
                continue

            async with state_lock:
                idx = choose_sender_index(
                    cfg.send_mode,
                    cfg.sender_sessions,
                    last_idx_ref["last"],
                    disabled_until,
                    time.time(),
                    perma_blocked,
                )

                if idx is None:
                    logger.warning(f"[SEND] no sender available -> requeue {uname}")
                    move_line_to_end_atomic(file_path, username)
                    await asyncio.sleep(5)
                    continue

                last_idx_ref["last"] = idx
                session_path = cfg.sender_sessions[idx]
                client = clients[session_path]
                
            # 🔁 تغيير النص + random variation
            def mutate_text(tpl: str) -> str:
                variations = [
                    tpl,
                    tpl.replace("السلام عليكم", "أهلاً"),
                    tpl.replace("لو تحب", "لو حابب"),
                    tpl + " 😊",
                    tpl.replace("شرح", "مساعدة"),
                ]
                return random.choice(variations)

            # ✅ النص النهائي
            final_template = mutate_text(template_text)

            text = format_message(
                template_text=final_template,
                username=uname,
                group_name=flow_name,
                sender_name=session_path,
            )

            ok = await send_one_message(
                cfg=cfg,
                logger=logger,
                client=client,
                session_path=session_path,
                username=uname,
                text=text,   # ✅ بقى متعرّف
                perma_blocked=perma_blocked,
                disabled_until=disabled_until,
                sent_hist=sent_hist,
                send_counter=send_counter,
                attachments=None,
            )

            if ok:
                append_send_log(
                    "logs/send_log.csv",
                    (utc_iso(), uname, flow_name, session_path, "OK", ""),
                )
                remove_username_from_file_atomic(file_path, username)
                append_sent_history(uname, message_key)
                logger.info(f"[SEND] OK {uname} via {session_path}")
            else:
                move_line_to_end_atomic(file_path, username)

            # ⏱ Delay عشوائي حقيقي
            await asyncio.sleep(random.uniform(3.5, 8.5))


async def run_sender(cfg: AppConfig, only: Optional[str], logger: logging.Logger):
    if not cfg.sender_sessions:
        logger.error("No sender_sessions configured.")
        return

    clients: Dict[str, Client] = {}

    # 1️⃣ إنشاء كل الكلاينتس
    for sess in cfg.sender_sessions:
        clients[sess] = Client(
            name=sess,
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            proxy=cfg.proxy,
        )

    # 2️⃣ تشغيل الكلاينتس + استبعاد أي session بايظة
    alive_clients: Dict[str, Client] = {}

    for sess, c in clients.items():
        try:
            await c.__aenter__()
            alive_clients[sess] = c
            logger.info(f"[SENDER] ✅ session ok -> {sess}")
        except Exception as e:
            logger.error(f"[SENDER] ❌ skip broken session {sess}: {e}")
            try:
                await c.__aexit__(None, None, None)
            except Exception:
                pass

    if not alive_clients:
        logger.error("[SENDER] 🚨 no valid sender sessions available. abort send.")
        return

    # 🔥🔥🔥 ده الإصلاح الحقيقي
    clients = alive_clients
    cfg.sender_sessions = list(alive_clients.keys())
    # 🔥🔥🔥 من اللحظة دي أي session جديدة شغالة هتدخل الإرسال

    perma_blocked = load_permanent_blocked()
    disabled_until: Dict[str, float] = {}
    sent_hist = load_sent_history()

    state_lock = asyncio.Lock()
    last_idx_ref = {"last": -1}
    send_counter = defaultdict(int)

    try:
        tasks = []
        db_groups = db_load_groups()

        if only:
            matched = [g for g in db_groups if g.name == only or g.out_file == only]
            if matched:
                target_groups = matched
            else:
                target_groups = [
                    GroupConfig(
                        name=only,
                        chat="",
                        out_file=only,
                        template_path=None,
                        attachments_enabled=False,
                        send_enabled=True,
                    )
                ]
        else:
            target_groups = db_groups

        # AUTO FLOW
        if cfg.auto_flow.enabled and not only:
            tasks.append(
                asyncio.create_task(
                    _process_file_sends(
                        cfg=cfg,
                        logger=logger,
                        clients=clients,
                        perma_blocked=perma_blocked,
                        disabled_until=disabled_until,
                        sent_hist=sent_hist,
                        file_path=cfg.auto_flow.out_file,
                        flow_name="AUTO",
                        template_path=cfg.auto_flow.template_path,
                        attachments_enabled=cfg.auto_flow.attachments_enabled,
                        last_idx_ref=last_idx_ref,
                        state_lock=state_lock,
                    )
                )
            )

        # GROUP FLOWS
        for g in target_groups:
            if not bool(getattr(g, "send_enabled", True)):
                continue

            tasks.append(
                asyncio.create_task(
                    _process_file_sends(
                        cfg=cfg,
                        logger=logger,
                        clients=clients,
                        perma_blocked=perma_blocked,
                        disabled_until=disabled_until,
                        sent_hist=sent_hist,
                        file_path=g.out_file,
                        flow_name=g.name,
                        template_path=g.template_path,
                        attachments_enabled=g.attachments_enabled,
                        last_idx_ref=last_idx_ref,
                        template_text_override=getattr(g, "_template_text", None),
                        state_lock=state_lock,
                    )
                )
            )

        if not tasks:
            logger.warning("[SEND] No active flows.")
            return

        await asyncio.gather(*tasks)

    finally:
        for c in clients.values():
            try:
                await c.__aexit__(None, None, None)
            except Exception:
                pass


# -----------------------
# CLI
# -----------------------
def build_argparser():
    p = argparse.ArgumentParser(
        prog="tg_live_sender",
        description="Live-listen usernames (only) and send templated messages with dedupe + kw notifications + auto flow"
    )
    p.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    p.add_argument("--listen", action="store_true", help="Run live listener (collect usernames + kw/admin notify)")
    p.add_argument("--send", action="store_true", help="Send messages from outputs/* files using sender_sessions")
    p.add_argument("--send-only", metavar="FILE_OR_GROUP", help="Send from a specific file or group name")
    p.add_argument("--dry-run", action="store_true", help="Force dry-run (no actual sending)")
    return p

async def main():
    args = build_argparser().parse_args()
    cfg = load_config(args.config)
    logger = setup_logging(cfg.logging_path, cfg.logging_level)

    global DB_PATH
    if cfg.viewer_bot and isinstance(cfg.viewer_bot, dict) and cfg.viewer_bot.get("db_path"):
        DB_PATH = cfg.viewer_bot["db_path"]

    db_init()
    try:
        db_seed_groups_from_config(cfg.groups)
    except Exception:
        pass

    if args.dry_run:
        cfg.dry_run = True

    os.makedirs("outputs", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    os.makedirs("errors", exist_ok=True)

    if args.listen:
        await run_listener(cfg, logger)

    if args.send:
        await run_sender(cfg, args.send_only, logger)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Cancelled by user")


# --- Broadcast All (auto-added) ---

def _db_init_broadcast(cur):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_targets (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TEXT
    )
    """)

def collect_broadcast_user(user_id, username, enabled=True):
    if not enabled:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO broadcast_targets(user_id, username, first_seen) VALUES(?,?,?)",
        (user_id, username or "", datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def send_broadcast_all(send_func, template_text, limit=20):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM broadcast_targets ORDER BY first_seen DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    for (uid,) in rows:
        try:
            send_func(uid, template_text)
            time.sleep(random.uniform(2,5))
        except Exception:
            pass
