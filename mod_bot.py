#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mod_bot.py — Viewer/Moderator bot for listened messages (all-UI buttons + group status submenu)
- كل الأوامر متاحة كأزرار:
  • قائمة رئيسية: Inbox, Filters, Groups, Search, Stats, Export
  • فلاتر الحالة: جديد/جاري/تم/لم يتم
  • القروبات: أزرار مع ترقيم صفحات لو زادت، وبعد اختيار قروب تظهر 4 أزرار حالات للقروب
  • البحث: زر يفعّل وضع انتظار كلمة البحث
  • الإحصاءات: زر يفعرض الكل + أزرار لكل tag
  • التصدير: زر يصدِّر CSV للكل أو لtag
- أزرار لكل رسالة:
  [⏳ جاري] [✅ تم] [❌ لم يتم]
  [👁 عرض]
  [✏️ تعديل] [🗑 حذف]
  [📝 ملاحظة] [👤 تعيين]
  [📚 مادة 1] [📚 مادة 2] ... (إن وجدت)
  [⛔ حذف+حظر]
- جديد: الحذف والحظر = حذف نهائي من DB + إزالة الكارت فورًا.
- جديد: تغيير الحالة ينقل الرسالة للحالة الجديدة ويزيل كارتها من القائمة الحالية فورًا.
- الترتيب: الأحدث أولًا (ORDER BY id DESC).

✅ تعديل مهم:
- إزالة رسالة "ليس لديك صلاحية الوصول..." (الآن تجاهل بصمت).
- زر "👁 عرض": الردود تظهر تحت السؤال مباشرة (Heuristic حسب chat_id + time window).

✅ Fix مهم لمشكلة "سيكشن يتضاف ومش بيظهر":
- توحيد/تنظيف (Normalize) uni_tag و section_name قبل الحفظ والقراءة (مسافات/تكرار مسافات).
- دعم الفاصلة العربية "،" والـ ";" في إدخال الكلمات المفتاحية.
- TRIM في الاستعلامات عشان لو عندك بيانات قديمة بمسافات.
"""

import os
import csv
import sqlite3
import yaml
import json
import html
import re
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode


# -----------------------
# Normalization helpers (FIX)
# -----------------------
def norm_key(s: str) -> str:
    """Normalize keys used for DB matching (uni_tag, section_name, keywords)."""
    s = (s or "").strip()
    # collapse multiple spaces
    s = re.sub(r"\s+", " ", s)
    return s


# -----------------------
# Config
# -----------------------
def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load_config()
VBOT = CFG.get("viewer_bot", {}) or {}
BOT_TOKEN = VBOT.get("bot_token")
ADMIN_IDS = set(VBOT.get("admin_ids", []))
API_ID = int(CFG["api_id"])
API_HASH = str(CFG["api_hash"])
DB_PATH = VBOT.get("db_path", "data/listen_messages.db")
MONITOR_CHAT_ID = VBOT.get("monitor_chat_id")  # اختياري
EXPORT_DIR = VBOT.get("export_dir", "exports")
PROXY = VBOT.get("proxy", None)

# replies heuristic window (minutes)
REPLIES_WINDOW_MIN = int(VBOT.get("replies_window_min", 240))  # default 4 hours
REPLIES_LIMIT = int(VBOT.get("replies_limit", 12))

if not BOT_TOKEN:
    raise SystemExit("viewer_bot.bot_token not set in config.yaml")


# -----------------------
# DB init & migrations
# -----------------------
def db_conn():
    return sqlite3.connect(DB_PATH)


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
        edited_text TEXT,
        tg_msg_id INTEGER,
        reply_to_tg_id INTEGER
    )
    """)
    # add columns if missing
    cur.execute("PRAGMA table_info(messages)")
    cols = {r[1] for r in cur.fetchall()}
    if "source_tag" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN source_tag TEXT")
    if "status" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN status TEXT DEFAULT 'new'")
    if "status_updated_at" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN status_updated_at TEXT")
    if "note" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN note TEXT")
    if "assigned_to" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN assigned_to INTEGER")
    if "uni_subjects" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN uni_subjects TEXT")

    if "tg_msg_id" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN tg_msg_id INTEGER")
    if "reply_to_tg_id" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN reply_to_tg_id INTEGER")


    # other tables
    cur.execute("""CREATE TABLE IF NOT EXISTS blocked_users(user_id INTEGER PRIMARY KEY)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS approved_viewers(user_id INTEGER PRIMARY KEY)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS roles(user_id INTEGER PRIMARY KEY, role TEXT)""")  # admin/editor/viewer
    cur.execute("""
      CREATE TABLE IF NOT EXISTS actions_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        actor_id INTEGER,
        action TEXT,
        msg_id INTEGER,
        extra TEXT
      )
    """)

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
        uni_tag TEXT NOT NULL DEFAULT '',
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
    conn.commit()

    # migration: ensure groups.uni_tag exists (university tag)
    try:
        cur.execute("PRAGMA table_info(groups)")
        cols = [str(r[1]) for r in cur.fetchall()]
        if "uni_tag" not in cols:
            cur.execute("ALTER TABLE groups ADD COLUMN uni_tag TEXT DEFAULT ''")
        cur.execute("UPDATE groups SET uni_tag=name WHERE TRIM(COALESCE(uni_tag,''))='' ")
        conn.commit()
    except Exception:
        pass

    # seed roles for admins
    for aid in ADMIN_IDS:
        cur.execute("INSERT OR IGNORE INTO roles(user_id, role) VALUES(?, ?)", (aid, "admin"))
    conn.commit()
    conn.close()


# -----------------------
# Dynamic groups (from DB)
# -----------------------
def db_list_groups() -> List[Tuple[int, str]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM groups ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return [(int(r[0]), str(r[1])) for r in rows]

def db_list_universities() -> List[Tuple[int, str, int]]:
    """Return list of (rep_gid, uni_tag, sources_count)"""
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""SELECT MIN(id) as gid,
                            COALESCE(NULLIF(TRIM(uni_tag),''), name) as utag,
                            COUNT(*) as cnt
                     FROM groups
                     GROUP BY COALESCE(NULLIF(TRIM(uni_tag),''), name)
                     ORDER BY utag ASC""")
    rows = cur.fetchall()
    conn.close()
    out = []
    for gid, utag, cnt in rows:
        out.append((int(gid), str(utag), int(cnt)))
    return out


def db_list_sources_for_uni(uni_tag: str) -> List[Tuple[int, str, str, int, int]]:
    """Return list of sources (groups rows) for a university tag.

    Matches rows where effective university key = COALESCE(NULLIF(TRIM(uni_tag),''), name).
    Returns: (gid, name, chat, send_enabled, subjects_only)
    """
    uni_tag = norm_key(uni_tag)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id,
                  name,
                  chat,
                  COALESCE(send_enabled,0),
                  COALESCE(subjects_only,0)
             FROM groups
            WHERE TRIM(COALESCE(NULLIF(uni_tag,''), name)) = TRIM(?)
            ORDER BY id ASC""",
        (uni_tag,),
    )
    rows = cur.fetchall()
    conn.close()
    out: List[Tuple[int, str, str, int, int]] = []
    for gid, name, chat, send_en, subj_only in rows:
        out.append((int(gid), str(name), str(chat), int(send_en), int(subj_only)))
    return out



def db_get_group(gid: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id,name,COALESCE(NULLIF(TRIM(uni_tag),''), name) as uni_tag,chat,out_file,template_text,
             COALESCE(attachments_enabled,0),
                  COALESCE(send_enabled,1),
                  COALESCE(subjects_only,0)
           FROM groups WHERE id=?""",
        (gid,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def db_get_group_by_name(name: str):
    name = norm_key(name)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, name, uni_tag, chat, out_file, template_text,
                  COALESCE(attachments_enabled,0),
                  COALESCE(send_enabled,0),
                  COALESCE(subjects_only,0),
                  created_at, updated_at
             FROM groups
            WHERE TRIM(name)=TRIM(?)""",
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def db_create_group(
    name: str,
    chat: str,
    out_file: str,
    template_text: str,
    uni_tag: str = "",
    attachments_enabled: int = 0,
    send_enabled: int = 0,
    subjects_only: int = 0,
) -> Tuple[bool, str]:
    name = norm_key(name)
    chat = (chat or "").strip()
    if not name:
        return False, "اسم القروب فارغ."
    if not chat:
        return False, "chat (@username أو -100...) فارغ."
    if not out_file:
        out_file = f"outputs/{name}.txt"
    now = datetime.utcnow().isoformat()
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO groups(name,uni_tag,chat,out_file,template_text,attachments_enabled,send_enabled,subjects_only,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                uni_tag,
                chat,
                out_file,
                (template_text or "").strip(),
                int(attachments_enabled),
                int(send_enabled),
                int(subjects_only),
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return True, "تمت إضافة القروب."
    except sqlite3.IntegrityError:
        conn.close()
        return False, "يوجد قروب بنفس الاسم بالفعل."


def db_update_group_field(gid: int, field: str, value) -> Tuple[bool, str]:
    if field not in {"name", "chat", "out_file", "template_text", "attachments_enabled", "send_enabled", "subjects_only"}:
        return False, "حقل غير مدعوم."
    conn = db_conn()
    cur = conn.cursor()
    if field == "name":
        row = db_get_group(gid)
        if not row:
            conn.close()
            return False, "القروب غير موجود."
        old_name = norm_key(str(row[1]))
        new_name = norm_key(str(value))
        if not new_name:
            conn.close()
            return False, "الاسم الجديد فارغ."
        try:
            cur.execute("UPDATE groups SET name=?, updated_at=? WHERE id=?", (new_name, datetime.utcnow().isoformat(), gid))
            # cascade with TRIM to catch old whitespace
            cur.execute("UPDATE uni_sections SET uni_tag=?, updated_at=? WHERE TRIM(uni_tag)=TRIM(?)", (new_name, datetime.utcnow().isoformat(), old_name))
            cur.execute("UPDATE messages SET source_tag=? WHERE TRIM(COALESCE(source_tag,''))=TRIM(?)", (new_name, old_name))
            conn.commit()
            conn.close()
            return True, "تم تغيير الاسم (مع تحديث السكشنات والرسائل)."
        except sqlite3.IntegrityError:
            conn.close()
            return False, "يوجد قروب آخر بنفس الاسم."
    else:
        cur.execute(f"UPDATE groups SET {field}=?, updated_at=? WHERE id=?", (value, datetime.utcnow().isoformat(), gid))
        conn.commit()
        conn.close()
        return True, "تم الحفظ."


def db_delete_group(gid: int) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    row = db_get_group(gid)
    if row:
        gname = norm_key(str(row[1]))
        cur.execute("DELETE FROM uni_sections WHERE TRIM(uni_tag)=TRIM(?)", (gname,))
    cur.execute("DELETE FROM groups WHERE id=?", (gid,))
    conn.commit()
    conn.close()
    return True


def group_names() -> List[str]:
    return [name for _, name in db_list_groups()]


def supported_tags() -> List[str]:
    tags = group_names()
    if "kw" not in tags:
        tags.append("kw")
    return tags


def db_seed_groups_from_config():
    groups_cfg = CFG.get("groups", []) or []
    if not groups_cfg:
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) FROM groups")
    n = int(cur.fetchone()[0] or 0)
    if n > 0:
        conn.close()
        return
    now = datetime.utcnow().isoformat()
    for g in groups_cfg:
        name = norm_key(str(g.get("name") or ""))
        uni_tag = norm_key(str(g.get("uni_tag") or g.get("uni") or name))
        chat = str(g.get("chat") or "").strip()
        out_file = str(g.get("out_file") or f"outputs/{name}.txt")
        tpl_text = ""
        tp = g.get("template_path")
        if tp and os.path.exists(tp):
            try:
                tpl_text = open(tp, "r", encoding="utf-8").read().strip()
            except Exception:
                tpl_text = ""
        cur.execute(
            """INSERT OR IGNORE INTO groups(name,uni_tag,chat,out_file,template_text,attachments_enabled,send_enabled,subjects_only,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                uni_tag,
                chat,
                out_file,
                tpl_text,
                1 if bool(g.get("attachments_enabled")) else 0,
                1 if bool(g.get("send_enabled", True)) else 0,
                1 if bool(g.get("subjects_only")) else 0,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()


def resolve_tag(display_name: Optional[str]) -> Optional[str]:
    if display_name is None:
        return None
    if display_name == "kw":
        return "kw"
    return display_name


# dirs
os.makedirs("sessions", exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)


# -----------------------
# DB helpers
# -----------------------
def role_of(user_id: int) -> str:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT role FROM roles WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return "viewer"


def log_action(actor_id: int, action: str, msg_id: Optional[int], extra: str = ""):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO actions_log(ts, actor_id, action, msg_id, extra) VALUES(?,?,?,?,?)",
        (datetime.utcnow().isoformat(), actor_id, action, msg_id, extra),
    )
    conn.commit()
    conn.close()


def db_query_list(tag: Optional[str], status: Optional[str], limit: int, offset: int) -> List[Tuple]:
    conn = db_conn()
    cur = conn.cursor()
    base = """SELECT id, chat_username, user_id, username,
                     COALESCE(edited_text, text) AS content,
                     date, COALESCE(status,'new'), COALESCE(note,''), COALESCE(assigned_to,''),
                     COALESCE(uni_subjects,'')
              FROM messages
              WHERE deleted=0"""
    params = []
    if tag:
        base += " AND TRIM(COALESCE(source_tag,''))=TRIM(?)"
        params.append(tag)
    if status:
        base += " AND COALESCE(status,'new')=?"
        params.append(status)
    base += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur.execute(base, tuple(params))
    rows = cur.fetchall()
    conn.close()
    # strict filter: keep only messages that contain one of the section keywords in content
    if keywords:
        kws = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    else:
        kws = []
    if not kws:
        return rows
    out = []
    for r in rows:
        _id, uname, content, d, chat_un = r
        c = (content or "")
        c_low = c.lower()
        ok = any(k.lower() in c_low for k in kws)
        if ok:
            out.append(r)
    return out


def db_count(tag: Optional[str], status: Optional[str]) -> int:
    conn = db_conn()
    cur = conn.cursor()
    base = "SELECT COUNT(*) FROM messages WHERE deleted=0"
    params = []
    if tag:
        base += " AND TRIM(COALESCE(source_tag,''))=TRIM(?)"
        params.append(tag)
    if status:
        base += " AND COALESCE(status,'new')=?"
        params.append(status)
    cur.execute(base, tuple(params))
    n = cur.fetchone()[0]
    conn.close()
    return n


def db_get_message(msg_id: int):
    """Return:
    id, chat_id, chat_username, user_id, username, text, edited_text, date,
    status, note, assigned_to, source_tag, uni_subjects
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, COALESCE(chat_id,0), COALESCE(chat_username,''), COALESCE(user_id,0),
                  COALESCE(username,''), COALESCE(text,''), COALESCE(edited_text,''), COALESCE(date,''),
                  COALESCE(status,'new'), COALESCE(note,''), COALESCE(assigned_to,''),
                  COALESCE(source_tag,''), COALESCE(uni_subjects,'')
           FROM messages WHERE id=?""",
        (msg_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row



def db_count_direct_replies(parent_id: int) -> int:
    """Count direct replies using reply_to_tg_id linkage (threaded)."""
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, tg_msg_id FROM messages WHERE id=? AND deleted=0", (int(parent_id),))
    row = cur.fetchone()
    if not row or row[1] is None:
        conn.close()
        return 0
    chat_id, parent_tg = row[0], row[1]
    cur.execute(
        """SELECT COUNT(1)
               FROM messages
              WHERE deleted=0
                AND chat_id=?
                AND reply_to_tg_id=?""",
        (chat_id, int(parent_tg)),
    )
    n = cur.fetchone()[0] or 0
    conn.close()
    return int(n)

def db_list_direct_replies(parent_id: int, limit: int = 10, offset: int = 0):
    """List direct replies for a message, ordered oldest→newest."""
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, tg_msg_id FROM messages WHERE id=? AND deleted=0", (int(parent_id),))
    row = cur.fetchone()
    if not row or row[1] is None:
        conn.close()
        return []
    chat_id, parent_tg = row[0], row[1]
    cur.execute(
        """SELECT id,
                    COALESCE(username,'') AS username,
                    COALESCE(edited_text, text) AS content,
                    COALESCE(date,'') AS date,
                    COALESCE(chat_username,'') AS chat_username
             FROM messages
             WHERE deleted=0
               AND chat_id=?
               AND reply_to_tg_id=?
             ORDER BY id ASC
             LIMIT ? OFFSET ?""",
        (chat_id, int(parent_tg), int(limit), int(offset)),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def render_message_card(msg_id: int, username: str, date_str: str, content: str, max_chars: int = 420) -> str:
    """Render a compact card for lists (safe for Telegram HTML)."""
    uname = (username or '').strip()
    user_part = html.escape(uname) if uname else "—"
    dt_part = html.escape((date_str or '').replace('T', ' ').replace('+00:00', ''))
    text = (content or '').strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    text = html.escape(text)
    return f"<b>#{msg_id}</b>\n📅 {dt_part} | 👤 {user_part}\n📝 {text}"

def db_get_nearby_replies(msg_id: int, window_minutes: int, limit: int) -> List[Tuple[int, str, str, str]]:
    """Heuristic replies: same chat_id, messages AFTER the question within time window."""
    row = db_get_message(msg_id)
    if not row:
        return []
    _, chat_id, _, _, _, _, _, date_iso, *_ = row
    try:
        dt0 = datetime.fromisoformat(str(date_iso))
    except Exception:
        return []
    dt1 = dt0 + timedelta(minutes=int(window_minutes))
    start_iso = dt0.isoformat()
    end_iso = dt1.isoformat()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id,
                  COALESCE(username,'') AS username,
                  COALESCE(edited_text, text, '') AS content,
                  COALESCE(date,'') AS date
           FROM messages
           WHERE deleted=0
             AND COALESCE(chat_id,0)=?
             AND COALESCE(date,'') > ?
             AND COALESCE(date,'') <= ?
             AND id != ?
           ORDER BY COALESCE(date,'') ASC
           LIMIT ?""",
        (int(chat_id), start_iso, end_iso, int(msg_id), int(limit)),
    )
    rows = cur.fetchall()
    conn.close()
    out: List[Tuple[int, str, str, str]] = []
    for rid, runame, rtext, rdate in rows:
        out.append((int(rid), str(runame or ""), str(rtext or ""), str(rdate or "")))
    return out


# حذف نهائي من قاعدة البيانات
def db_delete_message(msg_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    conn.commit()
    conn.close()


def db_edit_message(msg_id: int, new_text: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET edited_text=? WHERE id=?", (new_text, msg_id))
    conn.commit()
    conn.close()


def db_block_user(user_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO blocked_users(user_id) VALUES(?)", (user_id,))
    conn.commit()
    conn.close()


def db_is_approved(user_id: int) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM approved_viewers WHERE user_id=?", (user_id,))
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def db_approve(user_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO approved_viewers(user_id) VALUES(?)", (user_id,))
    cur.execute("INSERT OR REPLACE INTO roles(user_id, role) VALUES(?, 'admin')", (user_id,))
    conn.commit()
    conn.close()


def db_set_status(msg_id: int, status: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET status=?, status_updated_at=? WHERE id=?", (status, datetime.utcnow().isoformat(), msg_id))
    conn.commit()
    conn.close()


def db_set_note(msg_id: int, note: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET note=? WHERE id=?", (note, msg_id))
    conn.commit()
    conn.close()


def db_assign_to(msg_id: int, uid: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET assigned_to=? WHERE id=?", (uid, msg_id))
    conn.commit()
    conn.close()


def db_get_subjects(msg_id: int) -> List[str]:
    row = db_get_message(msg_id)
    if not row:
        return []
    uni_json = row[-1]
    if not uni_json:
        return []
    try:
        data = json.loads(uni_json)
        if isinstance(data, list):
            return [s for s in data if isinstance(s, str) and s.strip()]
        return []
    except Exception:
        return []


# -----------------------
# Manual sections (uni_sections) helpers
# -----------------------
def db_list_sections(uni_tag: str) -> List[Tuple[int, str, List[str], int]]:
    """FIX: TRIM + normalize uni_tag so sections always show even if extra spaces."""
    uni_tag = norm_key(uni_tag)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, section_name, keywords_json, COALESCE(enabled,1)
           FROM uni_sections
           WHERE TRIM(uni_tag)=TRIM(?)
           ORDER BY id DESC""",
        (uni_tag,),
    )
    rows = cur.fetchall()
    conn.close()
    out = []
    for sid, sname, kws_json, enabled in rows:
        try:
            kws = json.loads(kws_json or "[]")
            if not isinstance(kws, list):
                kws = []
        except Exception:
            kws = []
        kws = [norm_key(str(k)) for k in kws if norm_key(str(k))]
        out.append((int(sid), str(sname), kws, int(enabled or 0)))
    return out


def db_get_section(section_id: int) -> Optional[Tuple[int, str, str, List[str], int]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, uni_tag, section_name, keywords_json, COALESCE(enabled,1)
           FROM uni_sections WHERE id=?""",
        (section_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    sid, uni_tag, sname, kws_json, enabled = row
    try:
        kws = json.loads(kws_json or "[]")
        if not isinstance(kws, list):
            kws = []
    except Exception:
        kws = []
    kws = [norm_key(str(k)) for k in kws if norm_key(str(k))]
    return (int(sid), str(uni_tag), str(sname), kws, int(enabled or 0))


def db_add_section(uni_tag: str, section_name: str, keywords: List[str]) -> Tuple[bool, str]:
    """FIX: normalize uni_tag/section_name and keywords; prevents 'added but not showing'."""
    uni_tag = norm_key(uni_tag)
    section_name = norm_key(section_name)

    if not section_name:
        return False, "اسم السكشن فارغ."
    keywords = [norm_key(str(k)) for k in (keywords or []) if norm_key(str(k))]
    if not keywords:
        return False, "لازم تضيف كلمات مفتاحية على الأقل."
    now = datetime.utcnow().isoformat()
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO uni_sections(uni_tag, section_name, keywords_json, enabled, created_at, updated_at)
               VALUES(?,?,?,?,?,?)""",
            (uni_tag, section_name, json.dumps(keywords, ensure_ascii=False), 1, now, now),
        )
        conn.commit()
        conn.close()
        return True, "تمت إضافة السكشن."
    except sqlite3.IntegrityError:
        conn.close()
        return False, "السكشن موجود بالفعل بنفس الاسم لهذه الجامعة."


def db_update_section_keywords(section_id: int, keywords: List[str]) -> Tuple[bool, str]:
    keywords = [norm_key(str(k)) for k in (keywords or []) if norm_key(str(k))]
    if not keywords:
        return False, "لازم تضيف كلمات مفتاحية على الأقل."
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE uni_sections SET keywords_json=?, updated_at=? WHERE id=?""",
        (json.dumps(keywords, ensure_ascii=False), datetime.utcnow().isoformat(), section_id),
    )
    conn.commit()
    conn.close()
    return True, "تم تحديث الكلمات."


def db_rename_section(section_id: int, new_name: str) -> Tuple[bool, str]:
    new_name = norm_key(new_name)
    if not new_name:
        return False, "الاسم الجديد فارغ."
    row = db_get_section(section_id)
    if not row:
        return False, "السكشن غير موجود."
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE uni_sections SET section_name=?, updated_at=? WHERE id=?""", (new_name, datetime.utcnow().isoformat(), section_id))
        conn.commit()
        conn.close()
        return True, "تم تغيير الاسم."
    except sqlite3.IntegrityError:
        conn.close()
        return False, "يوجد سكشن آخر بنفس الاسم في هذه الجامعة."


def db_delete_section(section_id: int) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM uni_sections WHERE id=?", (section_id,))
    conn.commit()
    conn.close()
    return True


# -----------------------
# UI helpers
# -----------------------
def status_label(s: str) -> str:
    s = (s or "new").lower()
    return {
        "new": "🆕 جديد",
        "serving": "⏳ جاري خدمته",
        "done": "✅ تم خدمته",
        "not_served": "❌ لم يتم خدمته",
    }.get(s, s)


def can_edit(uid: int) -> bool:
    r = role_of(uid)
    return r in ("admin", "editor")


def render_user_display(u: str) -> str:
    if not u:
        return ""
    s = str(u).strip()
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://") or low.startswith("t.me/"):
        return s
    if s.startswith("@"):
        return s
    return f"@{s}"


def short_snip(s: str, n: int = 220) -> str:
    s = (s or "").strip()
    s = s.replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def build_item_kb(msg_id: int, user_id: int, uni_subjects: Optional[List[str]] = None):
    row_status = [
        InlineKeyboardButton("⏳ جاري", callback_data=f"status:serving:{msg_id}"),
        InlineKeyboardButton("✅ تم", callback_data=f"status:done:{msg_id}"),
        InlineKeyboardButton("❌ لم يتم", callback_data=f"status:not_served:{msg_id}"),
    ]
    row_view = [InlineKeyboardButton("👁 عرض", callback_data=f"view:{msg_id}")]
    row_edit_del = [
        InlineKeyboardButton("✏️ تعديل", callback_data=f"edit:{msg_id}"),
        InlineKeyboardButton("🗑 حذف", callback_data=f"delete:{msg_id}"),
    ]
    row_note_assign = [
        InlineKeyboardButton("📝 ملاحظة", callback_data=f"note:start:{msg_id}"),
        InlineKeyboardButton("👤 تعيين", callback_data=f"assign:start:{msg_id}"),
    ]

    rows = [row_status, row_view, row_edit_del, row_note_assign]

    if uni_subjects:
        subj_buttons = []
        for i, subj in enumerate(uni_subjects):
            label = subj
            if len(label) > 20:
                label = label[:17] + "…"
            subj_buttons.append(InlineKeyboardButton(f"📚 {label}", callback_data=f"subj:{msg_id}:{i}"))
        if len(subj_buttons) <= 3:
            rows.append(subj_buttons)
        else:
            rows.append(subj_buttons[:3])
            rows.append(subj_buttons[3:6])

    row_block = [InlineKeyboardButton("⛔ حذف+حظر", callback_data=f"block_del:{msg_id}:{user_id}")]
    rows.append(row_block)

    return InlineKeyboardMarkup(rows)


def build_nav_kb(tag: Optional[str], status: Optional[str], offset: int, total: int, page_size: int):
    prev_off = max(0, offset - page_size)
    next_off = offset + page_size if offset + page_size < total else offset
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"nav:{tag or ''}:{status or ''}:{prev_off}"))
    if offset + page_size < total:
        row.append(InlineKeyboardButton("➡️ التالي", callback_data=f"nav:{tag or ''}:{status or ''}:{next_off}"))
    rows = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def build_main_menu_kb():
    rows = [
        [InlineKeyboardButton("📥 الوارد", callback_data="menu:inbox"), InlineKeyboardButton("🔎 بحث", callback_data="menu:search")],
        [
            InlineKeyboardButton("🆕 جديد", callback_data="menu:filter:new"),
            InlineKeyboardButton("⏳ جاري", callback_data="menu:filter:serving"),
            InlineKeyboardButton("✅ تم", callback_data="menu:filter:done"),
            InlineKeyboardButton("❌ لم يتم", callback_data="menu:filter:not_served"),
        ],
        [InlineKeyboardButton("📂 القروبات", callback_data="menu:groups:0"), InlineKeyboardButton("📚 سكشنات المواد", callback_data="menu:sections:0")],
        [InlineKeyboardButton("📡 مصادر الجامعات", callback_data="menu:unisources:0")],
        [InlineKeyboardButton("🧩 إدارة القروبات", callback_data="menu:groups_manage:0")],
        [InlineKeyboardButton("📊 إحصاءات", callback_data="menu:stats"), InlineKeyboardButton("⬇️ تصدير CSV", callback_data="menu:export")],
    ]
    return InlineKeyboardMarkup(rows)


def build_groups_menu_kb(offset: int = 0, page_size: int = 8):
    tags = sorted(list(dict.fromkeys(supported_tags())))
    total = len(tags)
    end = min(offset + page_size, total)
    page = tags[offset:end]
    rows = []
    for i in range(0, len(page), 3):
        chunk = page[i : i + 3]
        rows.append([InlineKeyboardButton(f"{t}", callback_data=f"cmd:tag:{t}") for t in chunk])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"menu:groups:{max(0, offset - page_size)}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"menu:groups:{end}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def build_group_status_kb(display_name: str) -> InlineKeyboardMarkup:
    tag = resolve_tag(display_name)
    counts = {"new": db_count(tag, "new"), "done": db_count(tag, "done"), "serving": db_count(tag, "serving"), "not_served": db_count(tag, "not_served")}
    rows = [
        [InlineKeyboardButton(f"🆕 جديد ({counts['new']})", callback_data=f"gfilter:{display_name}:new:0"), InlineKeyboardButton(f"✅ تم ({counts['done']})", callback_data=f"gfilter:{display_name}:done:0")],
        [InlineKeyboardButton(f"⏳ جاري ({counts['serving']})", callback_data=f"gfilter:{display_name}:serving:0"), InlineKeyboardButton(f"❌ لم يتم ({counts['not_served']})", callback_data=f"gfilter:{display_name}:not_served:0")],
        [InlineKeyboardButton("🔙 رجوع للقروبات", callback_data="menu:groups:0")],
    ]
    return InlineKeyboardMarkup(rows)


# -----------------------
# Groups management UI
# -----------------------
def build_groups_manage_kb(offset: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    gs = db_list_groups()
    total = len(gs)
    end = min(offset + page_size, total)
    page = gs[offset:end]
    rows: List[List[InlineKeyboardButton]] = []
    for gid, name in page:
        rows.append([InlineKeyboardButton(f"🏛️ {name}", callback_data=f"grp:view:{gid}")])
    nav: List[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"menu:groups_manage:{max(0, offset - page_size)}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"menu:groups_manage:{end}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ إضافة قروب", callback_data="grp:add")])
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def build_group_detail_kb(gid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ تغيير الاسم", callback_data=f"grp:edit:name:{gid}"), InlineKeyboardButton("✏️ تغيير chat", callback_data=f"grp:edit:chat:{gid}")],
        [InlineKeyboardButton("🏫 تغيير الجامعة (uni_tag)", callback_data=f"grp:edit:uni_tag:{gid}")],
        [InlineKeyboardButton("📝 تعديل التمبلت", callback_data=f"grp:edit:template_text:{gid}"), InlineKeyboardButton("🗂 تغيير out_file", callback_data=f"grp:edit:out_file:{gid}")],
        [InlineKeyboardButton("📎 تبديل المرفقات", callback_data=f"grp:toggle:attachments_enabled:{gid}"), InlineKeyboardButton("🚀 تبديل الإرسال", callback_data=f"grp:toggle:send_enabled:{gid}")],
        [InlineKeyboardButton("🎯 subjects_only", callback_data=f"grp:toggle:subjects_only:{gid}")],
        [InlineKeyboardButton("🗑 حذف القروب", callback_data=f"grp:del:{gid}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu:groups_manage:0")],
    ]
    return InlineKeyboardMarkup(rows)


def build_group_delete_confirm_kb(gid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"grp:delc:{gid}"), InlineKeyboardButton("❌ إلغاء", callback_data=f"grp:view:{gid}")]])


# -----------------------
# Sections UI
# -----------------------
def parse_keywords_input(text: str) -> List[str]:
    """FIX: support Arabic comma (،) + semicolons."""
    if not text:
        return []
    parts = re.split(r"[,\n،;؛]+", text)
    kws = [norm_key(p) for p in parts if norm_key(p)]
    seen = set()
    out = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def build_universities_kb(offset: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    unis = db_list_universities()
    total = len(unis)
    end = min(offset + page_size, total)
    page = unis[offset:end]
    rows = []
    for gid, uni_tag, cnt in page:
        extra = f" ({cnt})" if cnt > 1 else ""
        rows.append([InlineKeyboardButton(f"🏛️ {uni_tag}{extra}", callback_data=f"sec:uni:{gid}:0")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"menu:sections:{max(0, offset - page_size)}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"menu:sections:{end}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ إضافة مادة لكل الجامعات", callback_data="sec:add_all")])
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)


# -----------------------
# Universities sources UI (multi sources per university)
# -----------------------
def build_unisources_kb(offset: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    """List universities (grouped by uni_tag) to manage their sources."""
    unis = db_list_universities()
    total = len(unis)
    end = min(offset + page_size, total)
    page = unis[offset:end]
    rows: List[List[InlineKeyboardButton]] = []
    for gid, uni_tag, cnt in page:
        extra = f" ({cnt})" if cnt > 1 else ""
        rows.append([InlineKeyboardButton(f"🏛️ {uni_tag}{extra}", callback_data=f"us:uni:{gid}:0")])

    nav: List[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"menu:unisources:{max(0, offset - page_size)}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"menu:unisources:{end}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def build_uni_sources_kb(uni_gid: int, offset: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    rowg = db_get_group(uni_gid)
    uni_name = norm_key(str(rowg[2] if (rowg and len(rowg) > 2) else rowg[1])) if rowg else ""
    sources = db_list_sources_for_uni(uni_name) if uni_name else []
    total = len(sources)
    end = min(offset + page_size, total)
    page = sources[offset:end]

    rows: List[List[InlineKeyboardButton]] = []
    for gid, name, chat, send_en, subj_only in page:
        flags = []
        if subj_only:
            flags.append("📚")
        if send_en:
            flags.append("📤")
        fl = (" " + "".join(flags)) if flags else ""
        rows.append([InlineKeyboardButton(f"🔌 {name}{fl}", callback_data=f"grp:view:{gid}")])

    nav: List[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"us:uni:{uni_gid}:{max(0, offset - page_size)}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"us:uni:{uni_gid}:{end}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("➕ إضافة مصدر (قروب/قناة)", callback_data=f"us:add:{uni_gid}")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:unisources:0")])
    rows.append([InlineKeyboardButton("🏠 القائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)



def build_sections_list_kb(uni_gid: int, offset: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    row = db_get_group(int(uni_gid))
    uni_name = norm_key(str(row[2] if row and len(row) > 2 else (row[1] if row else '')))
    secs = db_list_sections(uni_name)
    total = len(secs)
    end = min(offset + page_size, total)
    page = secs[offset:end]
    rows = []
    for sid, sname, kws, enabled in page:
        flag = "✅" if enabled else "⛔"
        rows.append([InlineKeyboardButton(f"{flag} {sname} ({len(kws)})", callback_data=f"sec:view:{sid}:{uni_gid}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"sec:uni:{uni_gid}:{max(0, offset - page_size)}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"sec:uni:{uni_gid}:{end}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ إضافة سكشن", callback_data=f"sec:add:{uni_gid}")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"menu:sections:0")])
    return InlineKeyboardMarkup(rows)


def build_section_detail_kb(section_id: int, uni_gid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👁 عرض مسجات السكشن", callback_data=f"sec:msgs:{section_id}:{uni_gid}:0")],
        [InlineKeyboardButton("✏️ تعديل الكلمات", callback_data=f"sec:editkw:{section_id}:{uni_gid}"), InlineKeyboardButton("🏷️ تغيير الاسم", callback_data=f"sec:rename:{section_id}:{uni_gid}")],
        [InlineKeyboardButton("🗑️ حذف", callback_data=f"sec:del:{section_id}:{uni_gid}"), InlineKeyboardButton("🔙 رجوع", callback_data=f"sec:uni:{uni_gid}:0")],
    ]
    return InlineKeyboardMarkup(rows)


def build_section_delete_confirm_kb(section_id: int, uni_gid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"sec:delc:{section_id}:{uni_gid}"), InlineKeyboardButton("❌ إلغاء", callback_data=f"sec:view:{section_id}:{uni_gid}")]]
    return InlineKeyboardMarkup(rows)


def db_count_section_messages(uni_name: str, section_name: str) -> int:
    conn = db_conn()
    cur = conn.cursor()
    # normalize for LIKE
    uni_name = norm_key(uni_name)
    section_name = norm_key(section_name)
    pattern = f'%"{section_name}"%'
    cur.execute(
        """SELECT COUNT(1)
             FROM messages
             WHERE TRIM(COALESCE(source_tag,'')) = TRIM(?)
               AND COALESCE(uni_subjects,'') LIKE ?""",
        (uni_name, pattern),
    )
    n = cur.fetchone()[0] or 0
    conn.close()
    return int(n)


def db_list_section_messages(uni_name: str, section_name: str, keywords: Optional[List[str]], limit: int, offset: int) -> List[Tuple]:
    conn = db_conn()
    cur = conn.cursor()
    uni_name = norm_key(uni_name)
    section_name = norm_key(section_name)
    pattern = f'%"{section_name}"%'
    cur.execute(
        """SELECT id,
                    COALESCE(username,'') AS username,
                    COALESCE(edited_text, text) AS content,
                    COALESCE(date,'') AS date,
                    COALESCE(chat_username,'') AS chat_username
             FROM messages
             WHERE TRIM(COALESCE(source_tag,'')) = TRIM(?)
               AND COALESCE(uni_subjects,'') LIKE ?
             ORDER BY id DESC
             LIMIT ? OFFSET ?""",
        (uni_name, pattern, int(limit), int(offset)),
    )
    rows = cur.fetchall()
    conn.close()
    # strict filter: keep only messages that contain one of the section keywords in content
    if keywords:
        kws = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    else:
        kws = []
    if not kws:
        return rows
    out = []
    for r in rows:
        _id, uname, content, d, chat_un = r
        c = (content or "")
        c_low = c.lower()
        ok = any(k.lower() in c_low for k in kws)
        if ok:
            out.append(r)
    return out


def build_section_messages_kb(section_id: int, uni_gid: int, offset: int, total: int, page_size: int, msg_items: List[Tuple[int, str]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for mid, label in msg_items:
        btn_txt = label.strip() if label else f"#{mid}"
        if len(btn_txt) > 58:
            btn_txt = btn_txt[:58] + "…"
        rows.append([InlineKeyboardButton(f"👁 {btn_txt}", callback_data=f"view:{mid}")])

    nav: List[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"sec:msgs:{section_id}:{uni_gid}:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"sec:msgs:{section_id}:{uni_gid}:{offset + page_size}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"sec:view:{section_id}:{uni_gid}")])
    return InlineKeyboardMarkup(rows)


async def notify_monitor(app: Client, text: str):
    if MONITOR_CHAT_ID:
        try:
            await app.send_message(MONITOR_CHAT_ID, text)
        except Exception:
            pass


# -----------------------
# Bot client
# -----------------------
app = Client(
    "viewer_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="sessions",
    parse_mode=ParseMode.HTML,
    proxy=PROXY,
)

# حالات انتظار إدخال نص/معرّف
editing_state = {}
search_state = set()
note_state = {}
assign_state = {}

sec_add_state: Dict[int, Dict[str, object]] = {}
sec_add_all_state: Dict[int, Dict[str, object]] = {}
sec_editkw_state: Dict[int, Dict[str, int]] = {}
sec_rename_state: Dict[int, Dict[str, int]] = {}

grp_add_state: Dict[int, Dict[str, object]] = {}

us_add_source_state: Dict[int, Dict[str, object]] = {}  # add new source under a university

grp_edit_state: Dict[int, Dict[str, object]] = {}


# -----------------------
# Access control (MODIFIED: silent if not approved)
# -----------------------
def guard(func):
    async def wrapper(client, message):
        uid = message.from_user.id if message.from_user else None
        if uid is None:
            return
        if uid in ADMIN_IDS or db_is_approved(uid):
            return await func(client, message)
        # ✅ silent ignore (no "ليس لديك صلاحية..." message)
        return

    return wrapper


# -----------------------
# List sender
# -----------------------
async def send_list(ctx_msg: Message, viewer_uid: int, tag: Optional[str], status: Optional[str], offset: int, page_size: int = 10):
    total = db_count(tag, status)
    rows = db_query_list(tag, status, page_size, offset)
    if total == 0:
        await ctx_msg.reply_text("لا توجد رسائل مطابقة حاليًا.", reply_markup=build_main_menu_kb())
        return
    header = (
        f"📂 الفئة: <b>{html.escape(tag or 'الكل')}</b> | "
        f"الحالة: <b>{html.escape(status_label(status or 'الكل'))}</b>\n"
        f"النتائج: {offset+1}-{min(offset+page_size, total)} / {total}"
    )
    nav = build_nav_kb(tag, status, offset, total, page_size)
    await ctx_msg.reply_text(header, reply_markup=nav)

    for _id, chat_un, user_id, username, text, date, st, note, assigned, uni_json in rows:
        short = (text or "")[:300]
        extra = ""
        if note:
            extra += f"\n📝 ملاحظة: {html.escape(str(note))}"
        if assigned:
            extra += f"\n👤 مكلَّف: {html.escape(str(assigned))}"

        uni_subjects = []
        if uni_json:
            try:
                uni_subjects = json.loads(uni_json)
                if not isinstance(uni_subjects, list):
                    uni_subjects = []
            except Exception:
                uni_subjects = []

        if uni_subjects:
            extra += "\n📚 المواد:\n" + "\n".join(f"• {html.escape(str(s))}" for s in uni_subjects)

        kb = build_item_kb(_id, user_id, uni_subjects) if can_edit(viewer_uid) else None
        await ctx_msg.reply_text(
            f"#{_id} | {render_user_display(username)} | {html.escape(str(chat_un))}\n"
            f"الحالة: {html.escape(status_label(st))}\n"
            f"{html.escape(str(date))}\n\n{html.escape(short)}{extra}",
            reply_markup=kb,
        )


# -----------------------
# Handlers (commands)
# -----------------------
@app.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    if uid in ADMIN_IDS or db_is_approved(uid):
        await message.reply_text("اختر من القائمة:", reply_markup=build_main_menu_kb())
        return
    await message.reply_text("تم استلام طلبك. بانتظار موافقة الأدمن.")
    for aid in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ موافقة", callback_data=f"approve:{uid}"), InlineKeyboardButton("❌ رفض", callback_data=f"deny:{uid}")]])
            await client.send_message(aid, f"طلب دخول من المستخدم: {uid}", reply_markup=kb)
        except Exception:
            pass

@app.on_callback_query(filters.regex("^student:my_requests$"))
async def student_my_requests(client, callback_query):
    user_id = callback_query.from_user.id

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, subject, status, created_at
        FROM requests
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 10
    """, (user_id,))

    rows = cur.fetchall()
    conn.close()

    if not rows:
        await callback_query.answer("ما عندك طلبات لحد دلوقتي", show_alert=True)
        return

    text = "📋 **طلباتك الأخيرة:**\n\n"
    for rid, subject, status, created in rows:
        text += (
            f"🆔 #{rid}\n"
            f"📚 المادة: {subject}\n"
            f"📌 الحالة: {status}\n"
            f"🕒 {created}\n"
            "──────────────\n"
        )

    await callback_query.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="student:home")]
        ])
    )

@app.on_message(filters.command("inbox"))
@guard
async def inbox_cmd(client, message: Message):
    await send_list(message, message.from_user.id, tag=None, status=None, offset=0)


@app.on_message(filters.command("new"))
@guard
async def f_new(client, message: Message):
    await send_list(message, message.from_user.id, tag=None, status="new", offset=0)


@app.on_message(filters.command("serving"))
@guard
async def f_serving(client, message: Message):
    await send_list(message, message.from_user.id, tag=None, status="serving", offset=0)


@app.on_message(filters.command("done"))
@guard
async def f_done(client, message: Message):
    await send_list(message, message.from_user.id, tag=None, status="done", offset=0)


@app.on_message(filters.command("notserved"))
@guard
async def f_notserved(client, message: Message):
    await send_list(message, message.from_user.id, tag=None, status="not_served", offset=0)


@app.on_message(filters.command("kw"))
@guard
async def kw_cmd(client, message: Message):
    await send_list(message, message.from_user.id, tag="kw", status=None, offset=0)


@app.on_message(filters.command(["g"]))
@guard
async def by_group_cmd(client, message: Message):
    arg = " ".join(message.command[1:]).strip()
    if not arg:
        await message.reply_text("اكتب اسم القروب بعد الأمر: /g Tabuk", reply_markup=build_main_menu_kb())
        return
    await send_list(message, message.from_user.id, tag=norm_key(arg), status=None, offset=0)


@app.on_message(filters.command("search"))
@guard
async def search_cmd(client, message: Message):
    search_state.add(message.from_user.id)
    await message.reply_text("🔎 أرسل كلمة/عبارة للبحث…")


# -----------------------
# Callback query
# -----------------------
@app.on_callback_query()
async def on_cbq_all(client, cq):
    data = cq.data or ""
    uid = cq.from_user.id

    if data.startswith("approve:") and uid in ADMIN_IDS:
        target = int(data.split(":")[1])
        db_approve(target)
        log_action(uid, "approve_user", None, str(target))
        await cq.message.reply_text(f"تمت موافقة المستخدم {target}")
        try:
            await client.send_message(target, "تمت الموافقة على دخولك. اضغط /start لفتح القائمة.")
        except Exception:
            pass
        await cq.answer("Approved", show_alert=False)
        return

    if data.startswith("deny:") and uid in ADMIN_IDS:
        target = int(data.split(":")[1])
        log_action(uid, "deny_user", None, str(target))
        await cq.message.reply_text(f"تم رفض طلب المستخدم {target}")
        try:
            await client.send_message(target, "تم رفض طلبك.")
        except Exception:
            pass
        await cq.answer("Denied", show_alert=False)
        return

    if data == "home":
        await cq.message.reply_text("القائمة الرئيسية:", reply_markup=build_main_menu_kb())
        await cq.answer()
        return

    if data == "menu:inbox":
        await send_list(cq.message, uid, tag=None, status=None, offset=0)
        await cq.answer()
        return

    if data.startswith("menu:filter:"):
        st = data.split(":")[2]
        await send_list(cq.message, uid, tag=None, status=st, offset=0)
        await cq.answer()
        return

    if data.startswith("menu:groups:"):
        off = int(data.split(":")[2])
        await cq.message.reply_text("اختر القروب:", reply_markup=build_groups_menu_kb(off))
        await cq.answer()
        return

    # ---- Groups management ----
    if data.startswith("menu:groups_manage:"):
        off = int(data.split(":")[2])
        await cq.message.reply_text("🧩 إدارة القروبات:", reply_markup=build_groups_manage_kb(off))
        await cq.answer()
        return

    if data.startswith("menu:unisources:"):
        off = int(data.split(":")[2])
        try:
            await cq.message.edit_text("📡 اختر الجامعة لإدارة المصادر (قروبات/قنوات):", reply_markup=build_unisources_kb(off))
        except Exception:
            await cq.message.reply_text("📡 اختر الجامعة لإدارة المصادر (قروبات/قنوات):", reply_markup=build_unisources_kb(off))
        await cq.answer()
        return

    if data.startswith("us:uni:"):
        try:
            parts = data.split(":")
            uni_gid = int(parts[2])
            off = int(parts[3] or 0)
            rowg = db_get_group(uni_gid)
            if not rowg:
                await cq.answer("جامعة غير معروفة", show_alert=True)
                return
            uni_name = norm_key(str(rowg[2] if (rowg and len(rowg) > 2) else rowg[1]))
            kb = build_uni_sources_kb(uni_gid, off)
            try:
                await cq.message.edit_text(f"🏛️ <b>{html.escape(uni_name)}</b>\nاختر مصدرًا لإدارته أو أضف مصدر جديد:", reply_markup=kb)
            except Exception:
                await cq.message.reply_text(f"🏛️ <b>{html.escape(uni_name)}</b>\nاختر مصدرًا لإدارته أو أضف مصدر جديد:", reply_markup=kb)
            await cq.answer()
            return
        except Exception:
            await cq.answer("تعذر فتح مصادر الجامعة", show_alert=True)
            return

    if data.startswith("us:add:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية إدارة القروبات.", show_alert=True)
            return
        try:
            uni_gid = int(data.split(":")[2])
            rowg = db_get_group(uni_gid)
            if not rowg:
                await cq.answer("جامعة غير معروفة", show_alert=True)
                return
            uni_name = norm_key(str(rowg[2] if (rowg and len(rowg) > 2) else rowg[1]))
            us_add_source_state[uid] = {"step": 1, "uni_gid": uni_gid, "uni_name": uni_name}
            await cq.message.reply_text(
                "➕ إضافة مصدر (قروب/قناة)\n\n"
                f"🏛️ الجامعة: <b>{html.escape(uni_name)}</b>\n\n"
                "ابعت الآن:\n"
                "• <b>@username</b> للقناة/القروب\n"
                "أو\n"
                "• <b>chat_id</b> زي <code>-1001234567890</code>\n\n"
                "ملاحظة: لازم تضيف البوت للمصدر وتديه صلاحيات مناسبة."
            )
            await cq.answer()
            return
        except Exception:
            await cq.answer("تعذر بدء إضافة المصدر", show_alert=True)
            return

    if data == "grp:add":
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية إدارة القروبات.", show_alert=True)
            return
        grp_add_state[uid] = {"step": 1, "name": "", "chat": ""}
        await cq.message.reply_text("➕ إضافة قروب\n\nأرسل <b>اسم القروب</b> الآن…")
        await cq.answer()
        return

    if data.startswith("grp:view:"):
        gid = int(data.split(":")[2])
        row = db_get_group(gid)
        if not row:
            await cq.answer("القروب غير موجود", show_alert=True)
            return
        # db_get_group returns: (id, name, uni_tag, chat, out_file, template_text,
        # attachments_enabled, send_enabled, subjects_only)
        _id, name, uni_tag, chat, out_file, tpl, att, send_en, subj_only = row
        await cq.message.reply_text(
            "🏛️ <b>{}</b>\n"
            "🏫 uni_tag: <code>{}</code>\n"
            "🔗 chat: <code>{}</code>\n"
            "📄 out_file: <code>{}</code>\n"
            "📎 attachments: <b>{}</b> | 🚀 send: <b>{}</b> | 🎯 subjects_only: <b>{}</b>\n"
            "\n📝 template (مختصر):\n<code>{}</code>".format(
                html.escape(str(name)),
                html.escape(str(uni_tag)),
                html.escape(str(chat)),
                html.escape(str(out_file)),
                "ON" if int(att) else "OFF",
                "ON" if int(send_en) else "OFF",
                "ON" if int(subj_only) else "OFF",
                html.escape((str(tpl)[:500] + ("…" if len(str(tpl)) > 500 else ""))),
            ),
            reply_markup=build_group_detail_kb(gid),
        )
        await cq.answer()
        return

    if data.startswith("grp:edit:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعديل.", show_alert=True)
            return
        _, _, field, gid = data.split(":")
        gid = int(gid)
        grp_edit_state[uid] = {"gid": gid, "field": field}
        prompt = {
            "name": "أرسل الاسم الجديد للقروب…",
            "chat": "أرسل chat الجديد (@username أو -100...)…",
            "template_text": "أرسل نص التمبلت بالكامل…",
            "out_file": "أرسل مسار out_file (مثال: outputs/Tabuk.txt)…",
            "uni_tag": "أرسل اسم/وسم الجامعة (uni_tag) اللي القروب تابع لها…",
        }.get(field, "أرسل القيمة الجديدة…")
        await cq.message.reply_text(prompt)
        await cq.answer()
        return

    if data.startswith("grp:toggle:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعديل.", show_alert=True)
            return
        _, _, field, gid = data.split(":")
        gid = int(gid)
        row = db_get_group(gid)
        if not row:
            await cq.answer("القروب غير موجود", show_alert=True)
            return
        # indices per db_get_group: 6=attachments, 7=send_enabled, 8=subjects_only
        current = int(row[6] if field == "attachments_enabled" else row[7] if field == "send_enabled" else row[8])
        newv = 0 if current else 1
        ok, msg_txt = db_update_group_field(gid, field, newv)
        await cq.answer(msg_txt, show_alert=False)
        await cq.message.reply_text("تم التحديث ✅", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع للتفاصيل", callback_data=f"grp:view:{gid}")]]))
        return

    if data.startswith("grp:del:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية الحذف.", show_alert=True)
            return
        gid = int(data.split(":")[2])
        row = db_get_group(gid)
        if not row:
            await cq.answer("القروب غير موجود", show_alert=True)
            return
        await cq.message.reply_text(f"🗑️ تأكيد حذف القروب: <b>{html.escape(str(row[1]))}</b>", reply_markup=build_group_delete_confirm_kb(gid))
        await cq.answer()
        return

    if data.startswith("grp:delc:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية الحذف.", show_alert=True)
            return
        gid = int(data.split(":")[2])
        db_delete_group(gid)
        log_action(uid, "delete_group", None, str(gid))
        await cq.answer("تم الحذف", show_alert=False)
        await cq.message.reply_text("تم حذف القروب ✅", reply_markup=build_groups_manage_kb(0))
        return

    # ---- Sections ----
    if data.startswith("menu:sections:"):
        off = int(data.split(":")[2])
        # Better UX: edit the same message so the click always feels responsive.
        try:
            await cq.message.edit_text("اختر الجامعة:", reply_markup=build_universities_kb(off))
        except Exception:
            await cq.message.reply_text("اختر الجامعة:", reply_markup=build_universities_kb(off))
        await cq.answer()
        return

    if data == "sec:add_all":
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعديل.", show_alert=True)
            return
        sec_add_all_state[uid] = {"step": 1}
        await cq.message.reply_text("📚 إضافة مادة لكل الجامعات\n\nأرسل اسم المادة/السكشن…")
        await cq.answer()
        return

    if data.startswith("sec:uni:"):
        try:
            parts = data.split(":")
            if len(parts) < 4:
                await cq.answer("بيانات الزر غير صحيحة", show_alert=True)
                return
            uni_gid = int(parts[2])
            off = int(parts[3] or 0)

            rowg = db_get_group(uni_gid)
            if not rowg:
                await cq.answer("جامعة غير معروفة", show_alert=True)
                return

            uni_name = norm_key(str(rowg[2] if (rowg and len(rowg) > 2) else rowg[1]))
            kb = build_sections_list_kb(uni_gid, off)

            # Prefer editing current message so the button click always updates something.
            try:
                await cq.message.edit_text(f"🏛️ <b>{html.escape(uni_name)}</b>\nاختر سكشن:", reply_markup=kb)
            except Exception:
                await cq.message.reply_text(f"🏛️ <b>{html.escape(uni_name)}</b>\nاختر سكشن:", reply_markup=kb)

            await cq.answer()
            return
        except Exception as e:
            logging.exception("sec:uni handler failed")
            await cq.answer("حصل خطأ أثناء فتح السكشنات", show_alert=True)
            return

    if data.startswith("sec:view:"):
        _, _, sid, uni_gid = data.split(":")
        sid = int(sid)
        uni_gid = int(uni_gid)
        row = db_get_section(sid)
        if not row:
            await cq.answer("السكشن غير موجود", show_alert=True)
            return
        _, uni_tag, sname, kws, enabled = row
        flag = "✅" if enabled else "⛔"
        kw_text = "\n".join(f"• {html.escape(str(k))}" for k in kws) if kws else "—"
        await cq.message.reply_text(
            f"{flag} <b>{html.escape(str(sname))}</b>\n"
            f"🏛️ الجامعة: <b>{html.escape(str(uni_tag))}</b>\n"
            f"🔑 الكلمات ({len(kws)}):\n{kw_text}",
            reply_markup=build_section_detail_kb(sid, uni_gid),
        )
        await cq.answer()
        return

    if data.startswith("sec:msgs:"):
        _, _, sid, uni_gid, off = data.split(":")
        sid = int(sid)
        uni_gid = int(uni_gid)
        off = int(off or 0)

        row = db_get_section(sid)
        if not row:
            await cq.answer("السكشن غير موجود", show_alert=True)
            return
        _, uni_tag, sname, kws, enabled = row

        page_size = 5
        # Parse keywords for strict matching in message content
        try:
            keywords = json.loads(kws) if kws else []
            if not isinstance(keywords, list):
                keywords = []
        except Exception:
            keywords = []
        total = db_count_section_messages(uni_tag, sname)
        items = db_list_section_messages(uni_tag, sname, keywords=keywords, limit=page_size, offset=off)
        total_show = len(items)

        if total == 0:
            await cq.message.reply_text(
                f"🏛️ الجامعة: <b>{html.escape(str(uni_tag))}</b>\n"
                f"📚 السكشن: <b>{html.escape(str(sname))}</b>\n\n"
                f"لا توجد مسجات مطابقة لهذا السكشن حتى الآن.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"sec:view:{sid}:{uni_gid}")]]),
            )
            await cq.answer()
            return

        text_out = (
            f"🏛️ الجامعة: <b>{html.escape(str(uni_tag))}</b>\n"
            f"📚 السكشن: <b>{html.escape(str(sname))}</b>\n"
            f"📦 العدد الكلي: <b>{total}</b>\n"
            f"📄 المعروض الآن: <b>{total_show}</b>"
        )

        cards = []
        kb_rows: List[List[InlineKeyboardButton]] = []
        for mid, uname, content, dt, chat_un in items:
            cards.append(render_message_card(int(mid), str(uname), str(dt), str(content)))
            rc = db_count_direct_replies(int(mid))
            kb_rows.append([
                InlineKeyboardButton(f"👁 #{mid}", callback_data=f"view:{mid}"),
                InlineKeyboardButton(f"💬 الردود ({rc})", callback_data=f"thr:{mid}:0"),
            ])

        if cards:
            text_out += "\n\n" + "\n\n────────\n\n".join(cards)

        nav: List[InlineKeyboardButton] = []
        if off > 0:
            nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"sec:msgs:{sid}:{uni_gid}:{max(0, off-page_size)}"))
        if off + page_size < total:
            nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"sec:msgs:{sid}:{uni_gid}:{off+page_size}"))
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"sec:view:{sid}:{uni_gid}")])

        await cq.message.edit_text(text_out, reply_markup=InlineKeyboardMarkup(kb_rows))
        await cq.answer()
        return
        await cq.answer()
        return

    if data.startswith("sec:add:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية إدارة السكشنات.", show_alert=True)
            return
        uni_gid = int(data.split(":")[2])
        rowg = db_get_group(uni_gid)
        if not rowg:
            await cq.answer("جامعة غير معروفة", show_alert=True)
            return
        uni_name = norm_key(str(rowg[2] if (rowg and len(rowg)>2) else rowg[1]))
        sec_add_state[uid] = {"uni_gid": uni_gid, "step": 1, "name": ""}
        await cq.message.reply_text(f"➕ إضافة سكشن لجامعة <b>{html.escape(uni_name)}</b>\n\nأرسل <b>اسم السكشن</b> الآن…")
        await cq.answer()
        return

    if data.startswith("sec:editkw:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعديل.", show_alert=True)
            return
        _, _, sid, uni_gid = data.split(":")
        sid = int(sid)
        uni_gid = int(uni_gid)
        sec_editkw_state[uid] = {"section_id": sid, "uni_gid": uni_gid}
        await cq.message.reply_text("✏️ أرسل الكلمات الجديدة (افصل بينهم بفاصلة أو سطر جديد)…")
        await cq.answer()
        return

    if data.startswith("sec:rename:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعديل.", show_alert=True)
            return
        _, _, sid, uni_gid = data.split(":")
        sid = int(sid)
        uni_gid = int(uni_gid)
        sec_rename_state[uid] = {"section_id": sid, "uni_gid": uni_gid}
        await cq.message.reply_text("🏷️ أرسل الاسم الجديد للسكشن…")
        await cq.answer()
        return

    if data.startswith("sec:del:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية الحذف.", show_alert=True)
            return
        _, _, sid, uni_gid = data.split(":")
        sid = int(sid)
        uni_gid = int(uni_gid)
        row = db_get_section(sid)
        if not row:
            await cq.answer("السكشن غير موجود", show_alert=True)
            return
        _, uni_tag, sname, _, _ = row
        await cq.message.reply_text(
            f"🗑️ تأكيد حذف السكشن: <b>{html.escape(str(sname))}</b>\n🏛️ {html.escape(str(uni_tag))}",
            reply_markup=build_section_delete_confirm_kb(sid, uni_gid),
        )
        await cq.answer()
        return

    if data.startswith("sec:delc:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية الحذف.", show_alert=True)
            return
        _, _, sid, uni_gid = data.split(":")
        sid = int(sid)
        uni_gid = int(uni_gid)
        db_delete_section(sid)
        log_action(uid, "delete_section", None, str(sid))

        rowg = db_get_group(uni_gid)
        uni_name = norm_key(str(rowg[2] if (rowg and len(rowg)>2) else rowg[1])) if rowg else "—"
        await cq.answer("تم الحذف", show_alert=False)
        await cq.message.reply_text(f"تم حذف السكشن ✅\n🏛️ <b>{html.escape(uni_name)}</b>", reply_markup=build_sections_list_kb(uni_gid, 0))
        return

    if data == "menu:search":
        search_state.add(uid)
        await cq.message.reply_text("🔎 أرسل كلمة/عبارة للبحث…")
        await cq.answer()
        return

    # --- تنقّل الصفحات ---
    if data.startswith("nav:"):
        _, tag, st, off = data.split(":")
        tag = tag or None
        st = st or None
        off = int(off or 0)
        await send_list(cq.message, uid, tag, st, off)
        await cq.answer()
        return

    # --- اختيار قروب وحالته ---
    if data.startswith("cmd:tag:"):
        disp = data.split(":")[2]
        await cq.message.reply_text(f"القروب: {html.escape(disp)}\nاختر حالة:", reply_markup=build_group_status_kb(disp))
        await cq.answer()
        return

    if data.startswith("gfilter:"):
        _, disp, st, off = data.split(":")
        tag = resolve_tag(disp)
        off = int(off or 0)
        await send_list(cq.message, uid, tag=tag, status=st, offset=off)
        await cq.answer()
        return

    # --- تغيير الحالة ---
    if data.startswith("status:"):
        _, st, mid = data.split(":")
        mid = int(mid)
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية تعديل الحالة.", show_alert=True)
            return
        if st not in ("serving", "done", "not_served"):
            await cq.answer("حالة غير معروفة", show_alert=True)
            return
        db_set_status(mid, st)
        log_action(uid, f"status_{st}", mid)
        await cq.answer("تم تحديث الحالة", show_alert=False)
        if st == "done":
            await notify_monitor(client, f"✅ تمت خدمة الرسالة #{mid} بواسطة {uid}")
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    # --- عرض/تعديل/ملاحظة/تعيين/مواد/حذف/حظر ---
    if data.startswith("view:"):
        msg_id = int(data.split(":")[1])
        row = db_get_message(msg_id)
        if not row:
            await cq.answer("غير موجود/محذوف", show_alert=True)
            return

        (_id, chat_id, chat_un, user_id2, username, text, edited_text, date, st, note, assigned, tag, uni_json) = row

        show_text = (edited_text or text or "")
        uni_subjects = []
        if uni_json:
            try:
                uni_subjects = json.loads(uni_json)
                if not isinstance(uni_subjects, list):
                    uni_subjects = []
            except Exception:
                uni_subjects = []

        header_lines = [
            f"[{html.escape(str(tag))}] #{_id}",
            f"👤 {render_user_display(username)} | 💬 {html.escape(str(chat_un))}",
            f"🕒 {html.escape(str(date))}",
            f"الحالة: {html.escape(status_label(st))}",
        ]
        if note:
            header_lines.append(f"📝 ملاحظة: {html.escape(str(note))}")
        if assigned:
            header_lines.append(f"👤 مكلَّف: {html.escape(str(assigned))}")
        if uni_subjects:
            header_lines.append("📚 المواد: " + " | ".join(html.escape(str(s)) for s in uni_subjects))

        body = "\n".join(header_lines) + "\n\n" + html.escape(show_text)

        # ✅ Replies directly under question (heuristic)

        
        # replies button (threaded via reply_to_tg_id)
        rcount = db_count_direct_replies(msg_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 الردود ({rcount})", callback_data=f"thr:{msg_id}:0")],
            [InlineKeyboardButton("🏠 القائمة", callback_data="home")],
        ])
        await cq.message.reply_text(body, reply_markup=kb)
        await cq.answer()
        return

    
    if data.startswith("thr:"):
        try:
            _, mid_s, off_s = data.split(":")
            mid = int(mid_s)
            off = int(off_s or 0)
            page_size = 5
            total = db_count_direct_replies(mid)
            items = db_list_direct_replies(mid, limit=page_size, offset=off)

            header = f"💬 <b>الردود على #{mid}</b>\n📦 العدد: <b>{total}</b>"
            cards = []
            kb_rows: List[List[InlineKeyboardButton]] = []
            for rid, runame, rcontent, rdate, _chat_un in items:
                cards.append(render_message_card(int(rid), str(runame), str(rdate), str(rcontent)))
                # nested replies count
                n2 = db_count_direct_replies(int(rid))
                kb_rows.append([
                    InlineKeyboardButton(f"👁 #{rid}", callback_data=f"view:{rid}"),
                    InlineKeyboardButton(f"💬 الردود ({n2})", callback_data=f"thr:{rid}:0"),
                ])

            text_body = header
            if cards:
                text_body += "\n\n" + "\n\n────────\n\n".join(cards)
            else:
                text_body += "\n\nلا توجد ردود مسجلة."

            nav: List[InlineKeyboardButton] = []
            if off > 0:
                nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"thr:{mid}:{max(0, off-page_size)}"))
            if off + page_size < total:
                nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"thr:{mid}:{off+page_size}"))
            if nav:
                kb_rows.append(nav)
            kb_rows.append([InlineKeyboardButton("🔙 رجوع للرسالة", callback_data=f"view:{mid}")])

            await cq.message.reply_text(text_body, reply_markup=InlineKeyboardMarkup(kb_rows))
            await cq.answer()
            return
        except Exception:
            logging.exception("thr handler failed")
            await cq.answer("حصل خطأ أثناء عرض الردود", show_alert=True)
            return
    if data.startswith("edit:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعديل.", show_alert=True)
            return
        msg_id = int(data.split(":")[1])
        editing_state[uid] = msg_id
        await cq.message.reply_text(f"أرسل النص الجديد ليتم حفظه للرسالة #{msg_id}")
        await cq.answer()
        return

    if data.startswith("note:start:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية إضافة ملاحظات.", show_alert=True)
            return
        msg_id = int(data.split(":")[2])
        note_state[uid] = msg_id
        await cq.message.reply_text(f"📝 أرسل نص الملاحظة للرسالة #{msg_id}")
        await cq.answer()
        return

    if data.startswith("assign:start:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية التعيين.", show_alert=True)
            return
        msg_id = int(data.split(":")[2])
        assign_state[uid] = msg_id
        await cq.message.reply_text(f"👤 أرسل user_id للشخص المكلَّف بالرسالة #{msg_id}")
        await cq.answer()
        return

    if data.startswith("subj:"):
        _, mid, idx = data.split(":")
        mid = int(mid)
        idx = int(idx)
        subjects = db_get_subjects(mid)
        if not subjects or idx < 0 or idx >= len(subjects):
            await cq.answer("المادة غير متاحة.", show_alert=True)
            return
        subj = subjects[idx]
        await cq.answer(f"📚 المادة: {subj}", show_alert=True)
        return

    if data.startswith("delete:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية الحذف.", show_alert=True)
            return
        msg_id = int(data.split(":")[1])
        db_delete_message(msg_id)
        log_action(uid, "delete_hard", msg_id)
        await cq.answer("تم الحذف نهائيًا", show_alert=False)
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if data.startswith("block_del:"):
        if not can_edit(uid):
            await cq.answer("ليست لديك صلاحية الحظر.", show_alert=True)
            return
        parts = data.split(":")
        msg_id = int(parts[1])
        user_id = int(parts[2])
        db_delete_message(msg_id)
        db_block_user(user_id)
        log_action(uid, "block_and_delete_hard", msg_id, str(user_id))
        await cq.answer("تم الحذف والحظر", show_alert=False)
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    await cq.answer()


# -----------------------
# Free-text handler
# -----------------------
@app.on_message(filters.text & ~filters.command(["start", "inbox", "new", "serving", "done", "notserved", "search", "stats", "exportcsv", "kw", "g"]))
@guard
async def on_free_text(client, message: Message):
    uid = message.from_user.id

    
    # ---- Add new source under a university ----
    if uid in us_add_source_state:
        st = us_add_source_state.get(uid) or {}
        if not can_edit(uid):
            us_add_source_state.pop(uid, None)
            await message.reply_text("ليست لديك صلاحية إدارة القروبات.", reply_markup=build_main_menu_kb())
            return

        step = int(st.get("step", 1))
        if step == 1:
            chat = (message.text or "").strip()
            if not chat:
                await message.reply_text("ابعت @username أو chat_id صحيح.")
                return

            uni_gid = int(st.get("uni_gid", 0) or 0)
            uni_name = norm_key(str(st.get("uni_name") or ""))

            # generate unique source name under this university
            existing = db_list_sources_for_uni(uni_name) if uni_name else []
            seq = len(existing) + 1
            base = uni_name or "UNI"
            name = f"{base}#{seq}"
            # ensure unique if already used
            tries = 0
            while tries < 50 and db_get_group_by_name(name):
                seq += 1
                name = f"{base}#{seq}"
                tries += 1

            out_file = f"outputs/{name}.txt"
            ok, msg_err = db_create_group(
                name=name,
                chat=chat,
                out_file=out_file,
                template_text=" ",
                uni_tag=uni_name,
                attachments_enabled=0,
                send_enabled=0,
                subjects_only=1,
            )
            us_add_source_state.pop(uid, None)

            if not ok:
                await message.reply_text(f"تعذر إضافة المصدر: {msg_err}")
                return

            await message.reply_text(
                f"✅ تم إضافة المصدر بنجاح\n\n"
                f"🏛️ الجامعة: <b>{html.escape(uni_name)}</b>\n"
                f"🔌 المصدر: <b>{html.escape(name)}</b>\n"
                f"📌 chat: <code>{html.escape(chat)}</code>\n\n"
                "تم تفعيل وضع (مواد فقط) للمصدر تلقائيًا."
            )

            # show updated sources list
            kb = build_uni_sources_kb(uni_gid, 0) if uni_gid else build_main_menu_kb()
            await message.reply_text("📡 مصادر الجامعة:", reply_markup=kb)
            return

# ---- Groups add/edit flows ----
    if uid in grp_add_state:
        st = grp_add_state.get(uid) or {}
        step = int(st.get("step", 1))
        if not can_edit(uid):
            grp_add_state.pop(uid, None)
            await message.reply_text("ليست لديك صلاحية إدارة القروبات.", reply_markup=build_main_menu_kb())
            return
        if step == 1:
            name = norm_key(message.text or "")
            if not name:
                await message.reply_text("اكتب اسم القروب (غير فارغ)…")
                return
            grp_add_state[uid] = {"step": 2, "name": name, "uni_tag": ""}
            await message.reply_text(f"✅ اسم المصدر: <b>{html.escape(name)}</b>\n\nأرسل اسم/وسم الجامعة (uni_tag) اللي المصدر تابع لها…\nمثال: Tabuk أو NBU\n\nلو عايزها نفس اسم المصدر ابعت نفس الاسم")
            return
        if step == 2:
            uni_tag = norm_key(message.text or "")
            if not uni_tag:
                uni_tag = norm_key(str(st.get("name", "")))
            grp_add_state[uid] = {"step": 3, "name": st.get("name", ""), "uni_tag": uni_tag}
            await message.reply_text(f"✅ الجامعة: <b>{html.escape(uni_tag)}</b>\n\nأرسل chat (@username أو -100...)…")
            return
        if step == 3:
            chat = (message.text or "").strip()
            if not chat:
                await message.reply_text("اكتب chat (@username أو -100...)…")
                return
            grp_add_state[uid] = {"step": 4, "name": st.get("name", ""), "uni_tag": st.get("uni_tag",""), "chat": chat}
            await message.reply_text("📝 أرسل نص التمبلت الآن (هيتخزن في DB)…")
            return
        name = norm_key(str(st.get("name", "")))
        uni_tag = norm_key(str(st.get("uni_tag", "")))
        chat = str(st.get("chat", "")).strip()
        tpl = message.text or ""
        out_file = f"outputs/{name}.txt"
        ok, msg_txt = db_create_group(name=name, chat=chat, uni_tag=uni_tag, out_file=out_file, template_text=tpl, attachments_enabled=0, send_enabled=0, subjects_only=0)
        grp_add_state.pop(uid, None)
        await message.reply_text(msg_txt, reply_markup=build_groups_manage_kb(0))
        return

    if uid in grp_edit_state:
        st = grp_edit_state.pop(uid, None) or {}
        if not can_edit(uid):
            await message.reply_text("ليست لديك صلاحية التعديل.", reply_markup=build_main_menu_kb())
            return
        gid = int(st.get("gid", 0))
        field = str(st.get("field", "")).strip()
        val = message.text or ""
        if field in ("attachments_enabled", "send_enabled", "subjects_only"):
            try:
                val = int(str(val).strip())
            except Exception:
                val = 0
        if field in ("name","uni_tag"):
            val = norm_key(val)
        ok, msg_txt = db_update_group_field(gid, field, val.strip() if isinstance(val, str) else val)
        await message.reply_text(msg_txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع للتفاصيل", callback_data=f"grp:view:{gid}")]]))
        return

    # ---- Sections flows ----
    if uid in sec_add_all_state:
        st = sec_add_all_state.get(uid) or {}
        step = int(st.get("step", 1))
        if not can_edit(uid):
            sec_add_all_state.pop(uid, None)
            await message.reply_text("ليست لديك صلاحية التعديل.", reply_markup=build_main_menu_kb())
            return
        if step == 1:
            name = norm_key(message.text or "")
            if not name:
                await message.reply_text("اكتب اسم المادة/السكشن (لازم يكون غير فارغ)…")
                return
            sec_add_all_state[uid] = {"step": 2, "name": name}
            await message.reply_text(f"✅ الاسم: <b>{html.escape(name)}</b>\n\nأرسل الكلمات المفتاحية الآن (افصل بينهم بفاصلة أو سطر جديد)…")
            return
        name = norm_key(str(st.get("name", "")))
        kws = parse_keywords_input(message.text or "")
        # add to all universities (distinct uni_tag)
        unis = db_list_universities()
        ok_count = 0
        for _gid, uni_tag, _cnt in unis:
            ok, _ = db_add_section(norm_key(uni_tag), name, kws)
            if ok:
                ok_count += 1
        log_action(uid, "add_section_all", None, f"name={name} unis={len(unis)}")
        sec_add_all_state.pop(uid, None)
        await message.reply_text(f"تمت إضافة السكشن لكل الجامعات. (تم/تحديث: {ok_count} جامعة)", reply_markup=build_main_menu_kb())
        return

    if uid in sec_add_state:
        st = sec_add_state.get(uid) or {}
        uni_gid = int(st.get("uni_gid", 0))
        step = int(st.get("step", 1))
        rowg = db_get_group(uni_gid)
        uni_name = norm_key(str(rowg[2] if (rowg and len(rowg)>2) else (rowg[1] if rowg else ''))) if rowg else None
        if not uni_name or not can_edit(uid):
            sec_add_state.pop(uid, None)
            await message.reply_text("لا يمكن إكمال العملية.", reply_markup=build_main_menu_kb())
            return
        if step == 1:
            name = norm_key(message.text or "")
            if not name:
                await message.reply_text("اكتب اسم السكشن (لازم يكون غير فارغ)…")
                return
            sec_add_state[uid] = {"uni_gid": uni_gid, "step": 2, "name": name}
            await message.reply_text(f"✅ الاسم: <b>{html.escape(name)}</b>\n\nأرسل الكلمات المفتاحية الآن (افصل بينهم بفاصلة أو سطر جديد)…")
            return
        name = norm_key(str(st.get("name", "")))
        kws = parse_keywords_input(message.text or "")
        ok, msg_txt = db_add_section(uni_name, name, kws)
        log_action(uid, "add_section", None, f"uni={uni_name} name={name}")
        sec_add_state.pop(uid, None)
        await message.reply_text(msg_txt, reply_markup=build_sections_list_kb(uni_gid, 0))
        return

    if uid in sec_editkw_state:
        st = sec_editkw_state.pop(uid, None) or {}
        if not can_edit(uid):
            await message.reply_text("ليست لديك صلاحية التعديل.", reply_markup=build_main_menu_kb())
            return
        sid = int(st.get("section_id", 0))
        uni_gid = int(st.get("uni_gid", 0))
        kws = parse_keywords_input(message.text or "")
        ok, msg_txt = db_update_section_keywords(sid, kws)
        log_action(uid, "update_section_keywords", None, str(sid))
        await message.reply_text(msg_txt, reply_markup=build_section_detail_kb(sid, uni_gid))
        return

    if uid in sec_rename_state:
        st = sec_rename_state.pop(uid, None) or {}
        if not can_edit(uid):
            await message.reply_text("ليست لديك صلاحية التعديل.", reply_markup=build_main_menu_kb())
            return
        sid = int(st.get("section_id", 0))
        uni_gid = int(st.get("uni_gid", 0))
        new_name = norm_key(message.text or "")
        ok, msg_txt = db_rename_section(sid, new_name)
        log_action(uid, "rename_section", None, str(sid))
        await message.reply_text(msg_txt, reply_markup=build_section_detail_kb(sid, uni_gid))
        return

    # تعديل نص الرسالة
    if uid in editing_state:
        msg_id = editing_state.pop(uid)
        new_text = message.text or ""
        db_edit_message(msg_id, new_text)
        log_action(uid, "edit_text", msg_id)
        await message.reply_text(f"تم تعديل الرسالة #{msg_id}", reply_markup=build_main_menu_kb())
        return

    # ملاحظة
    if uid in note_state:
        mid = note_state.pop(uid)
        db_set_note(mid, message.text or "")
        log_action(uid, "set_note", mid)
        await message.reply_text(f"تم حفظ الملاحظة للرسالة #{mid}", reply_markup=build_main_menu_kb())
        return

    # تعيين
    if uid in assign_state:
        mid = assign_state.pop(uid)
        try:
            assigned_uid = int((message.text or "").strip())
        except Exception:
            await message.reply_text("صيغة user_id غير صحيحة.", reply_markup=build_main_menu_kb())
            return
        db_assign_to(mid, assigned_uid)
        log_action(uid, "assign_to", mid, str(assigned_uid))
        await message.reply_text(f"تم تعيين {assigned_uid} للرسالة #{mid}", reply_markup=build_main_menu_kb())
        return

    # بحث
    if uid in search_state:
        search_state.discard(uid)
        q = (message.text or "").strip()
        if not q:
            await message.reply_text("أرسل كلمة/عبارة للبحث.", reply_markup=build_main_menu_kb())
            return
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT id, chat_username, user_id, username,
                      COALESCE(edited_text, text), date,
                      COALESCE(status,'new'), COALESCE(source_tag,''),
                      COALESCE(uni_subjects,'')
               FROM messages
               WHERE deleted=0 AND (text LIKE ? OR edited_text LIKE ?)
               ORDER BY id DESC LIMIT 10""",
            (f"%{q}%", f"%{q}%"),
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await message.reply_text("لا نتائج.", reply_markup=build_main_menu_kb())
            return
        await message.reply_text(
            f"نتائج البحث لـ: <b>{html.escape(q)}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة", callback_data="home")]]),
        )
        for _id, chat_un, user_id2, username, text, date, st, tag, uni_json in rows:
            short = (text or "")[:300]
            uni_subjects = []
            if uni_json:
                try:
                    uni_subjects = json.loads(uni_json)
                    if not isinstance(uni_subjects, list):
                        uni_subjects = []
                except Exception:
                    uni_subjects = []
            extra = ""
            if uni_subjects:
                extra += "\n📚 المواد:\n" + "\n".join(f"• {html.escape(str(s))}" for s in uni_subjects)
            kb = build_item_kb(_id, user_id2, uni_subjects) if can_edit(uid) else None
            await message.reply_text(
                f"[{html.escape(str(tag))}] #{_id} | {render_user_display(username)} | {html.escape(str(chat_un))}\n"
                f"الحالة: {html.escape(status_label(st))}\n"
                f"{html.escape(str(date))}\n\n{html.escape(short)}{extra}",
                reply_markup=kb,
            )
        return

    await message.reply_text("اختر من القائمة:", reply_markup=build_main_menu_kb())


# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    db_init()
    try:
        db_seed_groups_from_config()
    except Exception:
        pass
    app.run()