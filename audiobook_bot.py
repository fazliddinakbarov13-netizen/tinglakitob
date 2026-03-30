import os
import re
import math
import asyncio
import tempfile
import logging
import time
import hashlib
from dotenv import load_dotenv

import fitz  # PyMuPDF
from google import genai
from google.genai import types
import edge_tts
from langdetect import detect

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import db  # SQLite modul

# ── Konfiguratsiya ────────────────────────────────────────────────
load_dotenv()

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "1"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
WELCOME_BONUS = int(os.getenv("WELCOME_BONUS", "3"))
REFERRAL_FRIENDS_NEEDED = int(os.getenv("REFERRAL_FRIENDS_NEEDED", "3"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "2"))

# Foydalanuvchi qufl tizimi
user_locks: dict[int, asyncio.Lock] = {}

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_error.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# ── Ovoz xaritasi (8 ta til, Erkak/Ayol) ─────────────────────────

VOICE_MAP = {
    'uz': {
        'ayol': 'uz-UZ-MadinaNeural',
        'erkak': 'uz-UZ-SardorNeural'
    },
    'ru': {
        'ayol': 'ru-RU-SvetlanaNeural',
        'erkak': 'ru-RU-DmitryNeural'
    },
    'en': {
        'ayol': 'en-US-AriaNeural',
        'erkak': 'en-US-GuyNeural'
    },
    'tr': {
        'ayol': 'tr-TR-EmelNeural',
        'erkak': 'tr-TR-AhmetNeural'
    },
    'ar': {
        'ayol': 'ar-SA-ZariyahNeural',
        'erkak': 'ar-SA-HamedNeural'
    },
    'hi': {
        'ayol': 'hi-IN-SwaraNeural',
        'erkak': 'hi-IN-MadhurNeural'
    },
    'zh': {
        'ayol': 'zh-CN-XiaoxiaoNeural',
        'erkak': 'zh-CN-YunxiNeural'
    },
    'ko': {
        'ayol': 'ko-KR-SunHiNeural',
        'erkak': 'ko-KR-InJoonNeural'
    },
}

LANG_NAMES = {
    'uz': "O'zbek", 'ru': 'Rus', 'en': 'Ingliz',
    'tr': 'Turk', 'ar': 'Arab', 'hi': 'Hind',
    'zh': 'Xitoy', 'ko': 'Koreys'
}

LANG_FLAGS = {
    'uz': '🇺🇿', 'ru': '🇷🇺', 'en': '🇬🇧',
    'tr': '🇹🇷', 'ar': '🇸🇦', 'hi': '🇮🇳',
    'zh': '🇨🇳', 'ko': '🇰🇷'
}

# ── TTS sozlamalari ──────────────────────────────────────────────

MAX_TTS_CHARS = 500
TTS_TIMEOUT = 30

# ── Yordamchi funksiyalar ─────────────────────────────────────────


def split_text_for_tts(text: str, max_chars: int = MAX_TTS_CHARS) -> list:
    """Matnni tinish belgilari bo'yicha kichik qismlarga bo'ladi."""
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r'(?<=[.!?;:\n])\s+', text)
    chunks = []
    current = ""

    for sent in sentences:
        if len(sent) > max_chars:
            words = sent.split()
            for word in words:
                if len(current) + len(word) + 1 > max_chars:
                    if current.strip():
                        chunks.append(current.strip())
                    current = word
                else:
                    current = current + " " + word if current else word
        elif len(current) + len(sent) + 1 > max_chars:
            if current.strip():
                chunks.append(current.strip())
            current = sent
        else:
            current = current + " " + sent if current else sent

    if current.strip():
        chunks.append(current.strip())

    return chunks


async def tts_one_chunk(text: str, voice: str, path: str, retries: int = 5, rate: str = "+0%"):
    """Bitta kichik matn qismini audioga aylantiradi (timeout + retry)."""
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await asyncio.wait_for(communicate.save(path), timeout=TTS_TIMEOUT)
            if os.path.exists(path) and os.path.getsize(path) > 100:
                return True
            else:
                raise Exception("Audio fayl bo'sh yaratildi")
        except asyncio.TimeoutError:
            logging.warning(f"Edge-TTS timeout ({TTS_TIMEOUT}s) - urinish {attempt+1}/{retries}")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            if attempt < retries - 1:
                await asyncio.sleep(2)
            else:
                raise Exception(f"Edge-TTS {retries} marta {TTS_TIMEOUT}s timeout berdi")
        except Exception as e:
            wait = 2 * (attempt + 1)
            logging.warning(f"Edge-TTS sub-chunk urinish {attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(wait)
            else:
                raise e


async def generate_audio_safe(text: str, voice: str, output_path: str, rate: str = "+0%"):
    """Edge-TTS orqali xavfsiz audio yaratish (sub-chunking bilan)."""
    sub_chunks = split_text_for_tts(text)

    if len(sub_chunks) == 1:
        await tts_one_chunk(sub_chunks[0], voice, output_path, rate=rate)
    else:
        temp_files = []
        parent_dir = os.path.dirname(output_path)

        for ci, chunk in enumerate(sub_chunks):
            chunk_path = os.path.join(parent_dir, f"_sub_{ci}.mp3")
            await tts_one_chunk(chunk, voice, chunk_path, rate=rate)
            temp_files.append(chunk_path)
            await asyncio.sleep(2)

        with open(output_path, 'wb') as outfile:
            for tf in temp_files:
                with open(tf, 'rb') as infile:
                    outfile.write(infile.read())
                try:
                    os.remove(tf)
                except Exception:
                    pass


def smart_split(text: str) -> list:
    """Matnni bob yoki so'z limiti bo'yicha qismlarga ajratadi."""
    pattern = re.compile(
        r'(?i)^\s*(chapter\s+\d+|глава\s+\d+|\d+-bob|kirish|introduction|part\s+[ivxlc]+)[^\n]*',
        re.MULTILINE
    )

    parts = pattern.split(text)
    chunks = []

    if len(parts) > 1:
        if parts[0].strip():
            start_text = parts[0].strip()
            words = start_text.split()
            if len(words) > 800:
                for j in range(0, len(words), 800):
                    sub_text = " ".join(words[j:j+800])
                    sub_title = f"Kirish ({j//800 + 1}-qism)"
                    chunks.append((sub_title, sub_text))
            else:
                chunks.append(("Kirish", start_text))

        for i in range(1, len(parts), 2):
            chapter_title = parts[i].strip()
            chapter_content = parts[i+1].strip() if i+1 < len(parts) else ""
            full_text = chapter_title + "\n" + chapter_content

            words = full_text.split()
            if len(words) > 800:
                for j in range(0, len(words), 800):
                    sub_text = " ".join(words[j:j+800])
                    sub_title = f"{chapter_title} ({j//800 + 1}-qism)"
                    chunks.append((sub_title, sub_text))
            else:
                chunks.append((chapter_title, full_text))
    else:
        words = text.split()
        for j in range(0, len(words), 800):
            sub_text = " ".join(words[j:j+800])
            sub_title = f"{j//800 + 1}-qism"
            chunks.append((sub_title, sub_text))

    return [c for c in chunks if len(c[1].split()) > 3]


def cyrillic_to_latin_uz(text: str) -> str:
    """Kirill alifbosidagi o'zbek matnini lotin alifbosiga transliteratsiya qiladi."""
    mapping = {
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo', 'Ж': 'J',
        'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M', 'Н': 'N', 'О': 'O',
        'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U', 'Ф': 'F', 'Х': 'X', 'Ц': 'Ts',
        'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sh', 'Ъ': "'", 'Ы': 'I', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'j',
        'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
        'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'x', 'ц': 'ts',
        'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': "'", 'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'Ғ': "G'", 'ғ': "g'", 'Қ': 'Q', 'қ': 'q', 'Ў': "O'", 'ў': "o'", 'Ҳ': 'H', 'ҳ': 'h'
    }
    for cyr, lat in mapping.items():
        text = text.replace(cyr, lat)
    return text


def sanitize_for_markdown(text: str) -> str:
    """Telegram Markdown uchun xavfli belgilarni tozalaydi."""
    text = text.replace('_', ' ')
    for ch in ['*', '`', '[', ']']:
        text = text.replace(ch, '')
    return text


def get_file_extension(filename: str) -> str:
    """Fayl kengaytmasini aniqlaydi."""
    _, ext = os.path.splitext(filename)
    return ext.lower()


def text_hash(text: str) -> str:
    """Matnning qisqa hashini qaytaradi (xatcho'p uchun)."""
    return hashlib.md5(text[:500].encode()).hexdigest()[:12]


# ── Gemini tarjima ────────────────────────────────────────────────

def sync_translate(prompt: str) -> str:
    """Gemini API orqali tarjima."""
    retries = 5
    for attempt in range(retries):
        try:
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    safety_settings=[
                        types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
                        types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
                        types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
                        types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
                    ]
                )
            )
            if response and response.text:
                return response.text.strip()
            return ""
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower() or "Resource" in err_str:
                wait_time = min(20 * (attempt + 1), 60)
                logging.warning(f"Gemini API limit ({attempt+1}/{retries}): {wait_time}s kutilmoqda...")
                time.sleep(wait_time)
            elif attempt < retries - 1:
                logging.warning(f"Gemini xato ({attempt+1}/{retries}): {e}. 5s kutilmoqda...")
                time.sleep(5)
            else:
                raise e


async def check_channel_subscription(user_id: int, bot) -> bool:
    """Foydalanuvchi kanalga obuna ekanligini tekshiradi."""
    if not CHANNEL_USERNAME:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = db.register_user(user.id, user.username, user.first_name)
    context.user_data.clear()

    # Referal tekshiruvi (deep link: /start ref_12345)
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].replace("ref_", ""))
            if referrer_id != user.id and is_new:
                if db.add_referral(referrer_id, user.id):
                    # Referrerga xabar berish
                    ref_count = db.get_referral_count(referrer_id)
                    unrewarded = db.get_unrewarded_referrals(referrer_id)
                    if unrewarded >= REFERRAL_FRIENDS_NEEDED:
                        groups = unrewarded // REFERRAL_FRIENDS_NEEDED
                        bonus = groups * REFERRAL_BONUS
                        db.add_bonus_limit(referrer_id, bonus, 0)
                        db.mark_referrals_rewarded(referrer_id, groups * REFERRAL_FRIENDS_NEEDED)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🎉 Tabriklaymiz! {groups * REFERRAL_FRIENDS_NEEDED} ta do'stingiz qo'shildi!\n"
                                     f"💎 Sizga +{bonus} ta bonus limit berildi!"
                            )
                        except Exception:
                            pass
                    else:
                        needed = REFERRAL_FRIENDS_NEEDED - unrewarded
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"👥 Yangi do'stingiz qo'shildi! ({ref_count} ta jami)\n"
                                     f"🎁 Yana {needed} ta do'st taklif qiling va +{REFERRAL_BONUS} limit oling!"
                            )
                        except Exception:
                            pass
        except Exception:
            pass

    # Yangi foydalanuvchiga welcome bonus
    if is_new and WELCOME_BONUS > 0:
        db.add_bonus_limit(user.id, WELCOME_BONUS, 0)

    # Kanal obuna tekshiruvi
    is_subscribed = await check_channel_subscription(user.id, context.bot)
    if not is_subscribed and CHANNEL_USERNAME:
        keyboard = [
            [InlineKeyboardButton(f"📢 Kanalga obuna bo'lish", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")]
        ]
        await update.message.reply_text(
            f"⚠️ Botdan foydalanish uchun avval kanalimizga obuna bo'ling:\n\n"
            f"👉 {CHANNEL_USERNAME}\n\n"
            f"Obuna bo'lgach, *Tekshirish* tugmasini bosing.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    today_count = db.get_today_conversion_count(user.id)
    bonus = db.get_bonus_limit(user.id)
    total_limit = DAILY_LIMIT + bonus
    remaining = max(0, total_limit - today_count)

    # Referal link
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    ref_count = db.get_referral_count(user.id)

    welcome = "🎉 Xush kelibsiz! Sizga 3 ta bepul bonus berildi!\n\n" if is_new else ""

    msg = (
        f"{welcome}"
        "📚 *TinglaKitob Bot*\n\n"
        "PDF yoki TXT kitob yuboring — men uni 8 xil tilda ovozli audioga aylantirib beraman!\n\n"
        "🎁 *Buyruqlar:*\n"
        "📚 /kutubxona — Tayyor kitoblar\n"
        "🔖 /davom — Oxirgi joydan davom etish\n"
        "📊 /statistika — Shaxsiy statistika\n"
        "👥 /referal — Do'stlarni taklif qilish\n\n"
        f"📌 Limitingiz: {remaining}/{total_limit}\n\n"
        "Boshlash uchun kitobni fayl sifatida yuboring."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    context.user_data.clear()

    msg = (
        "🔄 Bot qayta ishga tushirildi!\n\n"
        "Boshlash uchun istalgan PDF yoki TXT kitob faylini yuboring."
    )
    await context.bot.send_message(chat_id=query.message.chat_id, text=msg, parse_mode="Markdown")


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kanal obunasini tekshirish."""
    query = update.callback_query
    user = update.effective_user
    is_subscribed = await check_channel_subscription(user.id, context.bot)

    if is_subscribed:
        await query.answer("✅ Obuna tasdiqlandi!")
        await query.edit_message_text(
            "✅ Obuna tasdiqlandi! Endi menga kitob faylini yuboring."
        )
    else:
        await query.answer("❌ Siz hali obuna bo'lmadingiz!", show_alert=True)


async def referal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Referal havolasini ko'rsatish."""
    user = update.effective_user
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    ref_count = db.get_referral_count(user.id)
    unrewarded = db.get_unrewarded_referrals(user.id)
    needed = REFERRAL_FRIENDS_NEEDED - (unrewarded % REFERRAL_FRIENDS_NEEDED) if unrewarded % REFERRAL_FRIENDS_NEEDED != 0 else REFERRAL_FRIENDS_NEEDED

    msg = (
        f"👥 *Referal tizimi*\n\n"
        f"📎 Sizning havolangiz:\n`{ref_link}`\n\n"
        f"👥 Jami taklif qilganlar: {ref_count} ta\n"
        f"🎁 Har {REFERRAL_FRIENDS_NEEDED} ta do'st = +{REFERRAL_BONUS} bonus limit\n"
        f"📌 Keyingi bonusgacha: yana {needed} ta do'st kerak\n\n"
        f"☝️ Havolani do'stlaringizga yuboring!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Iltimos, kitobni fayl sifatida yuboring (PDF yoki TXT)",
        parse_mode="Markdown"
    )


# ── /statistika buyrug'i ─────────────────────────────────────────

async def statistika_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    today_count = db.get_today_conversion_count(user.id)
    remaining = max(0, DAILY_LIMIT - today_count)

    msg = (
        f"📊 *Shaxsiy statistika*\n\n"
        f"👤 Ism: {user.first_name}\n"
        f"📖 Bugungi konvertatsiyalar: {today_count}\n"
        f"🎁 Qolgan limit: {remaining}/{DAILY_LIMIT}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /admin buyrug'i ──────────────────────────────────────────────

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bu buyruq faqat admin uchun.")
        return

    total_users = db.get_total_users()
    total_conv = db.get_total_conversions()
    today_conv = db.get_today_total_conversions()
    top_users = db.get_top_users(5)

    top_text = ""
    for i, u in enumerate(top_users, 1):
        name = u.get('first_name') or u.get('username') or str(u['user_id'])
        top_text += f"  {i}. {name} — {u['conv_count']} ta\n"

    msg = (
        f"👑 *ADMIN PANEL*\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"📖 Jami konvertatsiyalar: {total_conv}\n"
        f"📊 Bugungi konvertatsiyalar: {today_conv}\n\n"
        f"🏆 *Top foydalanuvchilar:*\n{top_text}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /limit buyrug'i (admin uchun) ────────────────────────────────

async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin foydalanuvchiga qo'shimcha limit beradi.
    Ishlatilishi: /limit @username 10
    yoki: /limit 123456789 5
    """
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bu buyruq faqat admin uchun.")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "📝 *Ishlatilishi:*\n"
            "`/limit @username 10`\n"
            "`/limit 123456789 5`\n\n"
            "Bu foydalanuvchiga qo'shimcha limit beradi.",
            parse_mode="Markdown"
        )
        return

    target = args[0]
    try:
        bonus = int(args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Limit soni raqam bo'lishi kerak.")
        return

    # Username yoki ID bo'yicha topish
    target_user_id = None
    if target.startswith('@'):
        found = db.get_user_by_username(target)
        if found:
            target_user_id = found['user_id']
    else:
        try:
            target_user_id = int(target)
        except ValueError:
            pass

    if not target_user_id:
        await update.message.reply_text("⚠️ Foydalanuvchi topilmadi. U avval botga /start bergan bo'lishi kerak.")
        return

    db.add_bonus_limit(target_user_id, bonus, user.id)
    total_bonus = db.get_bonus_limit(target_user_id)

    await update.message.reply_text(
        f"✅ Foydalanuvchiga +{bonus} ta qo'shimcha limit berildi!\n"
        f"Jami bonus limiti: {total_bonus} ta",
        parse_mode="Markdown"
    )

    # Foydalanuvchiga xabar berish
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 Sizga +{bonus} ta qo'shimcha kitob o'girish limiti berildi!\n"
                 f"Jami bonus: {total_bonus} ta"
        )
    except Exception:
        pass


# ── /kutubxona buyrug'i ──────────────────────────────────────────

async def kutubxona_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    books_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books")
    if not os.path.exists(books_dir):
        os.makedirs(books_dir)

    files = [f for f in os.listdir(books_dir) if f.endswith(('.pdf', '.txt'))]

    if not files:
        await update.message.reply_text(
            "📚 Kutubxona hozircha bo'sh.\n\n"
            "Admin kitoblarni `books/` papkasiga joylashtirishi kerak.",
            parse_mode="Markdown"
        )
        return

    keyboard = []
    for i, f in enumerate(sorted(files)):
        name = os.path.splitext(f)[0].replace('_', ' ').title()
        keyboard.append([InlineKeyboardButton(f"📖 {name}", callback_data=f"book_{i}")])

    context.user_data['library_files'] = sorted(files)

    await update.message.reply_text(
        "📚 *Kutubxona*\n\nQuyidagi kitoblardan birini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def book_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kutubxonadan kitob tanlash."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    idx = int(query.data.split("_")[1])
    files = context.user_data.get('library_files', [])

    if idx >= len(files):
        await query.edit_message_text("Kitob topilmadi.")
        return

    books_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books")
    file_name = files[idx]
    file_path = os.path.join(books_dir, file_name)
    ext = get_file_extension(file_name)

    text = ""
    if ext == '.pdf':
        try:
            pdf_doc = fitz.open(file_path)
            for page in pdf_doc:
                text += page.get_text()
            pdf_doc.close()
        except Exception as e:
            logging.error(f"Library PDF error: {e}")
    else:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception as e:
            logging.error(f"Library TXT error: {e}")

    words = text.split()
    word_count = len(words)

    if word_count < 100:
        await query.edit_message_text("Bu kitobda yetarli matn topilmadi.")
        return

    try:
        detected_lang = detect(text[:500])
    except Exception:
        detected_lang = "en"

    safe_file_name = sanitize_for_markdown(file_name)
    est_minutes = math.ceil(word_count / 150)

    context.user_data['text'] = text
    context.user_data['detected_lang'] = detected_lang
    context.user_data['file_name'] = safe_file_name

    flag_emoji = LANG_FLAGS.get(detected_lang, '🏳️')

    info_text = (
        f"📄 Fayl: {safe_file_name}\n"
        f"🌐 Aniqlangan til: {detected_lang.upper()} {flag_emoji}\n"
        f"📝 So'zlar soni: {word_count}\n"
        f"⏱ Taxminiy davomiyligi: {est_minutes} daqiqa\n\n"
        f"Qaysi tilda audio xohlaysiz?"
    )

    keyboard = [
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz"),
         InlineKeyboardButton("🇷🇺 Ruscha", callback_data="lang_ru")],
        [InlineKeyboardButton("🇬🇧 Inglizcha", callback_data="lang_en"),
         InlineKeyboardButton("🇹🇷 Turkcha", callback_data="lang_tr")],
        [InlineKeyboardButton("🇸🇦 Arabcha", callback_data="lang_ar"),
         InlineKeyboardButton("🇮🇳 Hindcha", callback_data="lang_hi")],
        [InlineKeyboardButton("🇨🇳 Xitoycha", callback_data="lang_zh"),
         InlineKeyboardButton("🇰🇷 Koreyscha", callback_data="lang_ko")],
    ]

    await query.edit_message_text(info_text, reply_markup=InlineKeyboardMarkup(keyboard))


# ── /davom buyrug'i (Xatcho'p) ───────────────────────────────────

async def davom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bookmark = db.get_bookmark(user.id)

    if not bookmark:
        await update.message.reply_text(
            "🔖 Sizda saqlangan xatcho'p yo'q.\n\n"
            "Jarayonni bekor qilganingizda xatcho'p avtomatik saqlanadi."
        )
        return

    msg = (
        f"🔖 *Saqlangan xatcho'p topildi!*\n\n"
        f"📖 Kitob: {bookmark['file_name']}\n"
        f"📌 To'xtagan joy: {bookmark['current_part']}/{bookmark['total_parts']} qism\n"
        f"🌐 Til: {LANG_FLAGS.get(bookmark['target_lang'], '🏳️')}\n\n"
        f"Davom ettirilsinmi?"
    )

    keyboard = [
        [InlineKeyboardButton("▶️ Davom ettirish", callback_data="resume_bookmark")],
        [InlineKeyboardButton("🗑 Xatcho'pni o'chirish", callback_data="delete_bookmark")]
    ]

    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def resume_bookmark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xatcho'pdan davom ettirish."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user = update.effective_user
    bookmark = db.get_bookmark(user.id)

    if not bookmark:
        await query.edit_message_text("Xatcho'p topilmadi.")
        return

    # Xatcho'p ma'lumotlarini context ga yuklash
    context.user_data['resume_from'] = bookmark['current_part']
    context.user_data['resume_chunks'] = bookmark['chunks']
    context.user_data['file_name'] = bookmark['file_name']
    context.user_data['resume_target_lang'] = bookmark['target_lang']
    context.user_data['resume_voice'] = bookmark['voice']
    context.user_data['resume_total'] = bookmark['total_parts']

    # Xatcho'pni o'chirish (bir marta ishlatish)
    db.delete_bookmark(user.id)

    await query.edit_message_text(
        f"▶️ {bookmark['current_part']}-qismdan davom ettirilmoqda...\n"
        f"Ovoz tanlashingiz kerak:"
    )

    # To'g'ridan-to'g'ri ovoz tanlash bosqichiga o'tish
    keyboard = [
        [InlineKeyboardButton("👩 Ayol ovozi", callback_data=f"resume_voice_ayol")],
        [InlineKeyboardButton("👨 Erkak ovozi", callback_data=f"resume_voice_erkak")]
    ]
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🗣 Ovoz turini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def delete_bookmark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xatcho'pni o'chirish."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    db.delete_bookmark(update.effective_user.id)
    await query.edit_message_text("🗑 Xatcho'p o'chirildi.")


# ── Fayl qabul qilish ────────────────────────────────────────────

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name or "Kitob.pdf"
    ext = get_file_extension(file_name)

    if ext not in ['.pdf', '.txt']:
        await update.message.reply_text(
            "Iltimos, faqat PDF yoki TXT formatidagi fayl yuboring.",
            parse_mode="Markdown"
        )
        return

    user = update.effective_user
    db.register_user(user.id, user.username, user.first_name)

    # Kanal obuna tekshiruvi
    is_subscribed = await check_channel_subscription(user.id, context.bot)
    if not is_subscribed and CHANNEL_USERNAME:
        keyboard = [
            [InlineKeyboardButton(f"📢 Kanalga obuna bo'lish", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")]
        ]
        await update.message.reply_text(
            f"⚠️ Avval kanalimizga obuna bo'ling: {CHANNEL_USERNAME}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Kunlik limit tekshiruvi (admin uchun cheksiz)
    if user.id != ADMIN_ID:
        today_count = db.get_today_conversion_count(user.id)
        bonus = db.get_bonus_limit(user.id)
        total_limit = DAILY_LIMIT + bonus
        if today_count >= total_limit:
            await update.message.reply_text(
                f"⚠️ Limitingiz tugadi ({today_count}/{total_limit}).\n\n"
                f"🎁 Kunlik bepul: {DAILY_LIMIT} ta\n"
                f"💎 Bonus limitingiz: {bonus} ta\n\n"
                "💰 Qo'shimcha limit olish uchun admin bilan bog'laning:\n"
                "👉 @tinglakitob\_admin",
                parse_mode="Markdown"
            )
            return

    safe_file_name = sanitize_for_markdown(file_name)

    msg = await update.message.reply_text(
        "⏳ Fayl qabul qilindi, matn ajratib olinmoqda...",
        parse_mode="Markdown"
    )

    try:
        with tempfile.TemporaryDirectory() as tmpdirname:
            safe_path_name = re.sub(r'[^\w\-.]', '_', file_name)
            file_path = os.path.join(tmpdirname, safe_path_name)

            download_success = False
            for dl_attempt in range(3):
                try:
                    tg_file = await context.bot.get_file(doc.file_id)
                    await tg_file.download_to_drive(file_path)
                    download_success = True
                    break
                except Exception as dl_err:
                    logging.warning(f"Fayl yuklash urinishi {dl_attempt+1}/3: {dl_err}")
                    if dl_attempt < 2:
                        await asyncio.sleep(2)

            if not download_success:
                await msg.edit_text("Faylni Telegramdan yuklab olishda xatolik. Iltimos qayta yuboring.")
                return

            text = ""
            if ext == '.pdf':
                try:
                    pdf_doc = fitz.open(file_path)
                    for page in pdf_doc:
                        text += page.get_text()
                    pdf_doc.close()
                except Exception as e:
                    logging.error(f"PDF extraction error: {e}")
            else:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except Exception as e:
                    logging.error(f"TXT extraction error: {e}")

            words = text.split()
            word_count = len(words)

            if word_count < 100:
                await msg.edit_text("Kitobda yetarli matn topilmadi.")
                return

            try:
                detected_lang = detect(text[:500])
            except Exception:
                detected_lang = "en"

            flag_emoji = LANG_FLAGS.get(detected_lang, '🏳️')
            est_minutes = math.ceil(word_count / 150)

            context.user_data['text'] = text
            context.user_data['detected_lang'] = detected_lang
            context.user_data['file_name'] = safe_file_name

            info_text = (
                f"📄 Fayl: {safe_file_name}\n"
                f"🌐 Aniqlangan til: {detected_lang.upper()} {flag_emoji}\n"
                f"📝 So'zlar soni: {word_count}\n"
                f"⏱ Taxminiy davomiyligi: {est_minutes} daqiqa\n\n"
                f"Qaysi tilda audio xohlaysiz?"
            )

            keyboard = [
                [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz"),
                 InlineKeyboardButton("🇷🇺 Ruscha", callback_data="lang_ru")],
                [InlineKeyboardButton("🇬🇧 Inglizcha", callback_data="lang_en"),
                 InlineKeyboardButton("🇹🇷 Turkcha", callback_data="lang_tr")],
                [InlineKeyboardButton("🇸🇦 Arabcha", callback_data="lang_ar"),
                 InlineKeyboardButton("🇮🇳 Hindcha", callback_data="lang_hi")],
                [InlineKeyboardButton("🇨🇳 Xitoycha", callback_data="lang_zh"),
                 InlineKeyboardButton("🇰🇷 Koreyscha", callback_data="lang_ko")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await msg.edit_text(info_text, reply_markup=reply_markup)

    except Exception as e:
        logging.error(f"Document handler error: {e}")
        try:
            await msg.edit_text("Faylni qayta ishlashda xatolik yuz berdi.")
        except Exception:
            pass


# ── Til tanlash -> Ovoz tanlash ──────────────────────────────────

async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Til tanlangan — endi ovoz (erkak/ayol) tanlashni ko'rsatish."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    target_lang = query.data.split("_")[1]
    context.user_data['target_lang'] = target_lang

    lang_name = LANG_NAMES.get(target_lang, target_lang)
    flag = LANG_FLAGS.get(target_lang, '🏳️')

    keyboard = [
        [InlineKeyboardButton("👩 Ayol ovozi", callback_data="voice_ayol")],
        [InlineKeyboardButton("👨 Erkak ovozi", callback_data="voice_erkak")]
    ]

    await query.edit_message_text(
        f"🌐 Tanlangan til: {flag} {lang_name}\n\n"
        f"🗣 Ovoz turini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Ovoz tanlash -> Audio jarayoni boshlash ──────────────────────

async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ovoz tanlangan — audio jarayoni boshlanadi."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    voice_type = query.data.split("_")[1]  # 'ayol' yoki 'erkak'
    target_lang = context.user_data.get('target_lang')
    text = context.user_data.get('text')
    detected_lang = context.user_data.get('detected_lang')
    file_name = context.user_data.get('file_name', 'audiobook')

    if not text or not target_lang:
        await query.edit_message_text("Jarayon eskirgan. Iltimos, faylni qayta yuboring.")
        return

    # ── LIMIT TEKSHIRUVI (audio boshlashdan OLDIN) ──
    user = update.effective_user
    if user.id != ADMIN_ID:
        today_count = db.get_today_conversion_count(user.id)
        bonus = db.get_bonus_limit(user.id)
        total_limit = DAILY_LIMIT + bonus
        if today_count >= total_limit:
            await query.edit_message_text(
                f"⛔ Limitingiz tugagan!\n\n"
                f"📊 Ishlatilgan: {today_count} ta\n"
                f"🎁 Kunlik bepul: {DAILY_LIMIT} ta\n"
                f"💎 Bonus: {bonus} ta\n\n"
                f"💰 Qo'shimcha limit sotib olish uchun adminga yozing:\n"
                f"👉 @tinglakitob_admin"
            )
            return

    # Limitni DARHOL qayd qilish (jarayon boshlanishi bilan)
    db.add_conversion(user.id, file_name, target_lang, "", 0, 0)

    voice = VOICE_MAP.get(target_lang, {}).get(voice_type, 'en-US-AriaNeural')
    context.user_data['voice'] = voice
    context.user_data['voice_type'] = voice_type

    # Tezlik tanlash
    keyboard = [
        [InlineKeyboardButton("🐢 0.75x Sekin", callback_data="speed_-25%"),
         InlineKeyboardButton("🔊 1x Oddiy", callback_data="speed_+0%")],
        [InlineKeyboardButton("⚡ 1.25x Tez", callback_data="speed_+25%"),
         InlineKeyboardButton("🚀 1.5x Juda tez", callback_data="speed_+50%")]
    ]

    await query.edit_message_text(
        "⏩ Audio tezligini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def speed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tezlik tanlangan — audio jarayoni boshlanadi."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    speed_rate = query.data.split("_")[1]  # '+0%', '+25%', '+50%', '-25%'
    context.user_data['speed_rate'] = speed_rate

    voice = context.user_data.get('voice')
    voice_type = context.user_data.get('voice_type', 'ayol')
    target_lang = context.user_data.get('target_lang')
    text = context.user_data.get('text')
    detected_lang = context.user_data.get('detected_lang')
    file_name = context.user_data.get('file_name', 'audiobook')

    if not text or not target_lang or not voice:
        await query.edit_message_text("Jarayon eskirgan. Faylni qayta yuboring.")
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _process_audio(query, context, text, detected_lang, target_lang,
                         voice, voice_type, file_name, start_from=0)


async def resume_voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xatcho'pdan davom — ovoz tanlangan."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    voice_type = query.data.split("_")[2]  # 'ayol' yoki 'erkak'
    target_lang = context.user_data.get('resume_target_lang')
    file_name = context.user_data.get('file_name', 'audiobook')
    resume_from = context.user_data.get('resume_from', 0)
    resume_chunks = context.user_data.get('resume_chunks', [])

    if not resume_chunks or not target_lang:
        await query.edit_message_text("Xatcho'p ma'lumotlari topilmadi.")
        return

    voice = VOICE_MAP.get(target_lang, {}).get(voice_type, 'en-US-AriaNeural')

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Resume chunks ni context ga yuklash
    context.user_data['preloaded_chunks'] = resume_chunks

    await _process_audio(query, context, None, None, target_lang,
                         voice, voice_type, file_name, start_from=resume_from)


# ── Asosiy audio jarayon funksiyasi ──────────────────────────────

async def _process_audio(query, context, text, detected_lang, target_lang,
                         voice, voice_type, file_name, start_from=0):
    """Barcha audio yaratish va yuborish jarayoni."""

    user_id = query.from_user.id
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()

    lock = user_locks[user_id]

    if lock.locked():
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Iltimos kuting, jarayon bajarilmoqda!"
            )
        except Exception:
            pass
        return

    async with lock:
        target_emoji = LANG_FLAGS.get(target_lang, '🏳️')
        target_lang_name = LANG_NAMES.get(target_lang, 'English')

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_process")]
        ])

        progress_msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="⏳ Tayyorgarlik ko'rilmoqda...",
            reply_markup=cancel_keyboard
        )

        context.user_data['is_cancelled'] = False

        try:
            # Chunks olish (resume yoki yangi)
            preloaded = context.user_data.get('preloaded_chunks')
            if preloaded:
                chunks = [(c[0], c[1]) for c in preloaded]
                context.user_data.pop('preloaded_chunks', None)
            else:
                chunks = smart_split(text)

            total_parts = len(chunks)

            # Tarjima kerakmi tekshirish
            need_translation = False
            if detected_lang and detected_lang != target_lang:
                need_translation = True
                # O'zbek matnini boshqa til deb xato aniqlasa
                if target_lang == 'uz' and text:
                    uzbek_words = ["bilan", "uchun", "ham", "deb", "qilib", "bir", "edi", "ekan"]
                    txt_lower = file_name.lower() + " " + (text[:2000] if text else "").lower()
                    if sum(1 for w in uzbek_words if w in txt_lower) >= 2:
                        logging.info("Matn o'zbekcha — tarjima bekor.")
                        need_translation = False

            success_count = 0
            error_count = 0
            all_audio_paths = []

            BATCH_SIZE = 3

            uz_instr = (
                "If translating to Uzbek, ALWAYS use the official Uzbek Latin alphabet. "
            ) if target_lang == 'uz' else ""

            async def translate_one(chunk_content: str) -> str:
                prompt = (
                    f"Translate the following text to {target_lang_name} language. {uz_instr}"
                    "IMPORTANT: Do NOT summarize the text. Translate it word-for-word, keeping every single sentence. "
                    "If the input text is ALREADY in the target language, do NOT translate or summarize it, just return the EXACT original text as is. "
                    "Preserve the literary style, tone, and feel of the book. "
                    f"Return ONLY the final translated text, without any explanations.\\n\\n{chunk_content}"
                )
                result = await asyncio.to_thread(sync_translate, prompt)
                return result

            with tempfile.TemporaryDirectory() as tmpdir:
                for batch_start in range(start_from, total_parts, BATCH_SIZE):
                    if context.user_data.get('is_cancelled'):
                        # Xatcho'p saqlash
                        chunks_data = [(c[0], c[1]) for c in chunks]
                        db.save_bookmark(
                            user_id, file_name, text_hash(chunks[0][1] if chunks else ""),
                            batch_start, total_parts, target_lang, voice, chunks_data
                        )
                        restart_kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 Boshidan boshlash", callback_data="restart_bot")]
                        ])
                        await progress_msg.edit_text(
                            f"🛑 Jarayon bekor qilindi!\n"
                            f"🔖 Xatcho'p saqlandi: {batch_start}/{total_parts} qism\n"
                            f"Davom ettirish uchun /davom bosing.",
                            reply_markup=restart_kb
                        )
                        return

                    batch_end = min(batch_start + BATCH_SIZE, total_parts)
                    batch = chunks[batch_start:batch_end]
                    batch_indices = list(range(batch_start + 1, batch_end + 1))

                    # Tarjima bosqichi
                    perc = int((batch_start + 1) / total_parts * 100)
                    blocks = int(perc / 10)
                    bar = "█" * blocks + "░" * (10 - blocks)

                    range_str = f"{batch_indices[0]}-{batch_indices[-1]}" if len(batch_indices) > 1 else str(batch_indices[0])

                    if need_translation:
                        status_text = f"⚡ Tarjima... {bar} {perc}% — {range_str}/{total_parts}"
                        try:
                            await progress_msg.edit_text(status_text, reply_markup=cancel_keyboard)
                        except Exception:
                            pass

                        tasks = [translate_one(chunk_content) for _, chunk_content in batch]
                        try:
                            results = await asyncio.gather(*tasks, return_exceptions=True)
                        except Exception as e:
                            logging.error(f"Batch tarjima xatosi: {e}")
                            results = [e] * len(batch)

                        translated_texts = []
                        for i, result in enumerate(results):
                            idx = batch_indices[i]
                            if isinstance(result, Exception):
                                logging.error(f"Tarjima xatosi ({idx}-qism): {result}")
                                await context.bot.send_message(
                                    chat_id=query.message.chat_id,
                                    text=f"🔴 Tarjimada xato ({idx}-qism). O'tkazib yuborilmoqda..."
                                )
                                translated_texts.append(None)
                            elif result and len(result.strip()) > 20:
                                translated_texts.append(result)
                            else:
                                translated_texts.append(batch[i][1])
                    else:
                        translated_texts = [chunk_content for _, chunk_content in batch]

                    # ── AUDIO yaratish va yuborish ──
                    for i, (chunk_title, chunk_content) in enumerate(batch):
                        idx = batch_indices[i]
                        final_text = translated_texts[i]

                        if final_text is None:
                            error_count += 1
                            continue

                        if not final_text or len(final_text.strip()) < 10:
                            logging.warning(f"Bo'sh/qisqa matn ({idx}-qism)")
                            error_count += 1
                            continue

                        if context.user_data.get('is_cancelled'):
                            break

                        perc = int(idx / total_parts * 100)
                        blocks = int(perc / 10)
                        bar = "█" * blocks + "░" * (10 - blocks)
                        status_text = f"🔊 Audio yaratilmoqda... {bar} {perc}% — {idx}/{total_parts}"
                        try:
                            await progress_msg.edit_text(status_text, reply_markup=cancel_keyboard)
                        except Exception:
                            pass

                        try:
                            audio_path = os.path.join(tmpdir, f"part_{idx}.mp3")
                            speed_rate = context.user_data.get('speed_rate', '+0%')

                            if target_lang == 'uz':
                                final_text = cyrillic_to_latin_uz(final_text)

                            # Brend intro yaratish (faqat birinchi qism uchun)
                            if idx == 1:
                                intro_text = "Bu audio TinglaKitob bot orqali yaratildi."
                                intro_path = os.path.join(tmpdir, "_intro.mp3")
                                try:
                                    await generate_audio_safe(intro_text, voice, intro_path, rate=speed_rate)
                                except Exception:
                                    intro_path = None
                            else:
                                intro_path = None

                            await generate_audio_safe(final_text, voice, audio_path, rate=speed_rate)

                            # Introni birinchi qism boshiga qo'shish
                            if intro_path and os.path.exists(intro_path) and idx == 1:
                                merged = os.path.join(tmpdir, f"part_{idx}_final.mp3")
                                with open(merged, 'wb') as mf:
                                    with open(intro_path, 'rb') as inf:
                                        mf.write(inf.read())
                                    with open(audio_path, 'rb') as af2:
                                        mf.write(af2.read())
                                os.replace(merged, audio_path)

                            # Telegramga yuborish
                            status_text_upload = f"📤 Yuborilmoqda... {bar} {perc}% — {idx}/{total_parts}"
                            try:
                                await progress_msg.edit_text(status_text_upload, reply_markup=cancel_keyboard)
                            except Exception:
                                pass

                            safe_chunk_title = sanitize_for_markdown(chunk_title)
                            voice_label = "👩 Ayol" if voice_type == "ayol" else "👨 Erkak"
                            caption = (
                                f"📖 {file_name}\n"
                                f"📌 Qism: {idx}/{total_parts} - {safe_chunk_title}\n"
                                f"🌐 Til: {target_emoji} | 🗣 {voice_label}"
                            )

                            upload_success = False
                            for send_attempt in range(3):
                                try:
                                    with open(audio_path, 'rb') as af:
                                        await context.bot.send_audio(
                                            chat_id=query.message.chat_id,
                                            audio=af,
                                            caption=caption,
                                            read_timeout=300,
                                            write_timeout=300,
                                            connect_timeout=300
                                        )
                                    upload_success = True
                                    break
                                except Exception as up_err:
                                    logging.warning(f"Audio yuborishda xato, urinish {send_attempt+1}/3: {up_err}")
                                    await asyncio.sleep(5)

                            if not upload_success:
                                raise Exception("Telegram serveriga ulanolmadi")

                            all_audio_paths.append(audio_path)
                            success_count += 1

                        except Exception as e:
                            logging.error(f"Xato ({idx}-qism): {e}")
                            try:
                                await context.bot.send_message(
                                    chat_id=query.message.chat_id,
                                    text=f"🔴 Xato ({idx}-qism). O'tkazib yuborildi."
                                )
                            except Exception:
                                pass
                            error_count += 1
                            continue

                # ── Barchasini bitta MP3 ga birlashtirish ──
                if success_count > 1 and all_audio_paths:
                    try:
                        merge_status = f"🎵 Barcha {success_count} qism bitta MP3 ga birlashtirilmoqda..."
                        try:
                            await progress_msg.edit_text(merge_status)
                        except Exception:
                            pass

                        merged_path = os.path.join(tmpdir, "full_audiobook.mp3")
                        with open(merged_path, 'wb') as outfile:
                            for ap in sorted(all_audio_paths, key=lambda x: int(re.search(r'part_(\d+)', x).group(1))):
                                if os.path.exists(ap):
                                    with open(ap, 'rb') as infile:
                                        outfile.write(infile.read())

                        merged_size = os.path.getsize(merged_path)
                        # Telegram 50MB limit
                        if merged_size < 50 * 1024 * 1024:
                            caption = f"📖 {file_name}\n🎵 To'liq audiobook ({success_count} qism)\n🌐 {target_emoji}"
                            with open(merged_path, 'rb') as af:
                                await context.bot.send_audio(
                                    chat_id=query.message.chat_id,
                                    audio=af,
                                    caption=caption,
                                    title=f"{file_name} - To'liq",
                                    read_timeout=300,
                                    write_timeout=300,
                                    connect_timeout=300
                                )
                        else:
                            await context.bot.send_message(
                                chat_id=query.message.chat_id,
                                text=f"⚠️ To'liq MP3 hajmi {merged_size // (1024*1024)} MB — Telegram limiti (50 MB) dan katta."
                            )
                    except Exception as merge_err:
                        logging.error(f"MP3 birlashtirish xatosi: {merge_err}")


            # Yakuniy xabar
            restart_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Boshidan boshlash", callback_data="restart_bot")]
            ])

            if error_count == 0:
                result_text = f"🎉 Barcha {success_count} ta qism muvaffaqiyatli yakunlandi! Yoqimli tinglash!"
            else:
                result_text = (
                    f"✅ Yakunlandi!\n"
                    f"📊 Muvaffaqiyatli: {success_count} ta qism\n"
                    f"⚠️ Xato: {error_count} ta qism o'tkazib yuborildi"
                )

            await progress_msg.edit_text(result_text, reply_markup=restart_kb)

        except Exception as e:
            logging.error(f"Process audio error: {e}")
            restart_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Qayta ishga tushirish", callback_data="restart_bot")]
            ])
            try:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="Jarayonda umumiy xatolik yuz berdi.",
                    reply_markup=restart_kb
                )
            except Exception:
                pass


# ── Bekor qilish handler ─────────────────────────────────────────

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer("Bekor qilinmoqda...")
    except Exception:
        pass

    context.user_data['is_cancelled'] = True


# ── Global xato handler ──────────────────────────────────────────

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Ushlalmagan xato: {context.error}", exc_info=context.error)


# ── Asosiy ishga tushirish ────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token or token == "your_token_here":
        print("Iltimos, .env faylida TELEGRAM_TOKEN ni o'rnating.")
        return

    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0
    )
    app = Application.builder().token(token).request(request).concurrent_updates(True).build()

    # Buyruqlar
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("kutubxona", kutubxona_cmd))
    app.add_handler(CommandHandler("davom", davom_cmd))
    app.add_handler(CommandHandler("statistika", statistika_cmd))
    app.add_handler(CommandHandler("referal", referal_cmd))

    # Fayl handler
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

    # Callback handler'lar (tartib muhim!)
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_bot$"))
    app.add_handler(CallbackQueryHandler(cancel_handler, pattern="^cancel_process$"))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(book_select_callback, pattern=r"^book_\d+$"))
    app.add_handler(CallbackQueryHandler(resume_bookmark_callback, pattern="^resume_bookmark$"))
    app.add_handler(CallbackQueryHandler(delete_bookmark_callback, pattern="^delete_bookmark$"))
    app.add_handler(CallbackQueryHandler(resume_voice_callback, pattern=r"^resume_voice_"))
    app.add_handler(CallbackQueryHandler(speed_callback, pattern=r"^speed_"))
    app.add_handler(CallbackQueryHandler(voice_callback, pattern=r"^voice_"))
    app.add_handler(CallbackQueryHandler(lang_callback, pattern=r"^lang_"))

    # Global xato handler
    app.add_error_handler(error_handler)

    print("Bot ishga tushdi! ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()