"""
TinglaKitob Bot — SQLite ma'lumotlar bazasi moduli.
Foydalanuvchilar, statistika, xatcho'plar va kunlik limitlarni boshqaradi.
"""

import sqlite3
import os
import json
from datetime import datetime, date

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tinglakitob.db")


def get_conn():
    """SQLite ulanishini qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Barcha jadvallarni yaratadi (agar mavjud bo'lmasa)."""
    conn = get_conn()
    c = conn.cursor()

    # Foydalanuvchilar jadvali
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Konvertatsiyalar jadvali (statistika + kunlik limit uchun)
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_name TEXT,
            target_lang TEXT,
            voice TEXT,
            word_count INTEGER DEFAULT 0,
            parts_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # Xatcho'plar jadvali (davom etish uchun)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_name TEXT,
            text_hash TEXT,
            current_part INTEGER,
            total_parts INTEGER,
            target_lang TEXT,
            voice TEXT,
            chunks_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # Referallar jadvali
    c.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            bonus_given INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (referrer_id) REFERENCES users(user_id)
        )
    """)

    # Bonus limitlar jadvali (admin qo'lda qo'shadi)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bonus_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bonus_count INTEGER DEFAULT 0,
            added_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    conn.commit()
    conn.close()


# ── Foydalanuvchi funksiyalari ────────────────────────────────────

def register_user(user_id: int, username: str = None, first_name: str = None) -> bool:
    """Yangi foydalanuvchini ro'yxatga oladi. Yangi bo'lsa True qaytaradi."""
    conn = get_conn()
    existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if existing:
        # Username yangilash
        conn.execute("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                     (username, first_name, user_id))
        conn.commit()
        conn.close()
        return False  # Eski foydalanuvchi
    else:
        conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name)
        )
        conn.commit()
        conn.close()
        return True  # Yangi foydalanuvchi


def get_total_users() -> int:
    """Jami foydalanuvchilar sonini qaytaradi."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return count


def get_all_user_ids() -> list:
    """Barcha foydalanuvchilarning user_id larini qaytaradi (broadcast uchun)."""
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [row[0] for row in rows]


# ── Konvertatsiya va kunlik limit ─────────────────────────────────

def add_conversion(user_id: int, file_name: str, target_lang: str, voice: str,
                   word_count: int = 0, parts_count: int = 0):
    """Yangi konvertatsiyani qayd qiladi."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO conversions (user_id, file_name, target_lang, voice, word_count, parts_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, file_name, target_lang, voice, word_count, parts_count)
    )
    conn.commit()
    conn.close()


def get_today_conversion_count(user_id: int) -> int:
    """Bugungi konvertatsiyalar sonini qaytaradi."""
    conn = get_conn()
    today = date.today().isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM conversions WHERE user_id = ? AND date(created_at) = ?",
        (user_id, today)
    ).fetchone()[0]
    conn.close()
    return count


def get_total_conversions() -> int:
    """Jami konvertatsiyalar sonini qaytaradi."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM conversions").fetchone()[0]
    conn.close()
    return count


def get_today_total_conversions() -> int:
    """Bugungi jami konvertatsiyalar sonini qaytaradi."""
    conn = get_conn()
    today = date.today().isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM conversions WHERE date(created_at) = ?",
        (today,)
    ).fetchone()[0]
    conn.close()
    return count


def get_top_users(limit: int = 10) -> list:
    """Eng faol foydalanuvchilarni qaytaradi."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT u.user_id, u.username, u.first_name, COUNT(c.id) as conv_count "
        "FROM users u LEFT JOIN conversions c ON u.user_id = c.user_id "
        "GROUP BY u.user_id ORDER BY conv_count DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Xatcho'p (Bookmark) funksiyalari ─────────────────────────────

def save_bookmark(user_id: int, file_name: str, text_hash: str,
                  current_part: int, total_parts: int, target_lang: str,
                  voice: str, chunks_data: list):
    """Xatcho'pni saqlaydi (mavjud bo'lsa yangilaydi)."""
    conn = get_conn()
    # Avvalgi xatcho'pni o'chirish (har bir foydalanuvchi uchun faqat 1 ta aktiv)
    conn.execute("DELETE FROM bookmarks WHERE user_id = ?", (user_id,))
    chunks_json = json.dumps(chunks_data, ensure_ascii=False)
    conn.execute(
        "INSERT INTO bookmarks (user_id, file_name, text_hash, current_part, total_parts, "
        "target_lang, voice, chunks_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, file_name, text_hash, current_part, total_parts,
         target_lang, voice, chunks_json)
    )
    conn.commit()
    conn.close()


def get_bookmark(user_id: int) -> dict:
    """Foydalanuvchining aktiv xatcho'pini qaytaradi."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    if row:
        result = dict(row)
        result['chunks'] = json.loads(result['chunks_json'])
        return result
    return None


def delete_bookmark(user_id: int):
    """Foydalanuvchining xatcho'pini o'chiradi."""
    conn = get_conn()
    conn.execute("DELETE FROM bookmarks WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ── Bonus limit funksiyalari ──────────────────────────────────────

def add_bonus_limit(user_id: int, bonus_count: int, added_by: int):
    """Foydalanuvchiga qo'shimcha limit beradi (admin tomonidan)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO bonus_limits (user_id, bonus_count, added_by) VALUES (?, ?, ?)",
        (user_id, bonus_count, added_by)
    )
    conn.commit()
    conn.close()


def get_bonus_limit(user_id: int) -> int:
    """Foydalanuvchining jami bonus limitini qaytaradi."""
    conn = get_conn()
    result = conn.execute(
        "SELECT COALESCE(SUM(bonus_count), 0) FROM bonus_limits WHERE user_id = ?",
        (user_id,)
    ).fetchone()[0]
    conn.close()
    return result


# ── Referal funksiyalari ──────────────────────────────────────────

def add_referral(referrer_id: int, referred_id: int):
    """Referal qayd qiladi."""
    conn = get_conn()
    # Avval tekshirish — takroriy referalni oldini olish
    existing = conn.execute(
        "SELECT id FROM referrals WHERE referred_id = ?", (referred_id,)
    ).fetchone()
    if existing:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
        (referrer_id, referred_id)
    )
    conn.commit()
    conn.close()
    return True


def get_referral_count(user_id: int) -> int:
    """Foydalanuvchining taklif qilgan odamlari sonini qaytaradi."""
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)
    ).fetchone()[0]
    conn.close()
    return count


def get_unrewarded_referrals(user_id: int) -> int:
    """Bonus berilmagan referallar sonini qaytaradi."""
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND bonus_given = 0",
        (user_id,)
    ).fetchone()[0]
    conn.close()
    return count


def mark_referrals_rewarded(user_id: int, count: int):
    """Referallarni mukofotlangan deb belgilaydi."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM referrals WHERE referrer_id = ? AND bonus_given = 0 LIMIT ?",
        (user_id, count)
    ).fetchall()
    for row in rows:
        conn.execute("UPDATE referrals SET bonus_given = 1 WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()


def get_user_by_username(username: str) -> dict:
    """Username bo'yicha foydalanuvchini topadi."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?",
        (username.lstrip('@'),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# Dastur ishga tushganda bazani yaratish
init_db()
