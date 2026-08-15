#!/usr/bin/env python3
"""
Instagram Reel Downloader Telegram Bot
========================================
A production-ready Telegram bot that lets users download PUBLICLY accessible
Instagram Reels (video + audio extraction), with history, stats, settings,
i18n (English/Hindi), rate limiting, and an admin panel.

This bot only fetches content that is publicly reachable via yt-dlp. It does
NOT bypass private accounts, logins, paywalls, or any Instagram access
control. Private/restricted/unavailable content is reported as an error.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in BOT_TOKEN / ADMIN_IDS
    python bot.py

Everything (config, database, downloader, keyboards, handlers, main) lives
in this single file by design, as requested.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

import yt_dlp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# =====================================================================================
# LOGGING
# =====================================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("instagram_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)


# =====================================================================================
# CONFIG
# =====================================================================================

class Config:
    BOT_TOKEN: str = os.getenv("8899688395:AAEoQpM_wZDKhW_6gy_OGl3-CyNWh8Dy-ZY", "").strip()
    ADMIN_IDS: set[int] = {
        int(x) for x in os.getenv("7045220016", "").split(",") if x.strip().isdigit()
    }
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///bot.db")
    DOWNLOAD_TIMEOUT: int = int(os.getenv("DOWNLOAD_TIMEOUT", "120"))
    MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
    RATE_LIMIT_SECONDS: int = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
    TEMP_DIR: str = os.getenv("TEMP_DIR", "downloads")
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
    HISTORY_PAGE_SIZE: int = int(os.getenv("HISTORY_PAGE_SIZE", "5"))

    @staticmethod
    def sqlite_path() -> str:
        url = Config.DATABASE_URL
        if url.startswith("sqlite:///"):
            return url[len("sqlite:///"):] or "bot.db"
        return "bot.db"

    @staticmethod
    def validate() -> None:
        if not Config.BOT_TOKEN:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and fill in your "
                "Telegram bot token before starting the bot."
            )
        if not Config.ADMIN_IDS:
            logger.warning(
                "No ADMIN_IDS configured — the /admin panel will be inaccessible "
                "until you set ADMIN_IDS in your environment."
            )


# =====================================================================================
# TRANSLATIONS (i18n)
# =====================================================================================

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "welcome_title": "🎬 <b>INSTAGRAM DOWNLOADER</b>",
        "welcome_body": "Send a public Instagram Reel link, or choose an option below.",
        "btn_download": "📥 Download Reel",
        "btn_audio": "🎵 Extract Audio",
        "btn_history": "📜 Download History",
        "btn_stats": "📊 My Stats",
        "btn_settings": "⚙️ Settings",
        "btn_help": "❓ Help",
        "btn_about": "👨‍💻 About",
        "btn_back": "◀️ Back",
        "btn_main_menu": "🏠 Main Menu",
        "ask_url_reel": "📥 Send me a <b>public Instagram Reel URL</b> and I'll fetch it for you.",
        "ask_url_audio": "🎵 Send me a <b>public Instagram Reel URL</b> and I'll extract the audio.",
        "invalid_url": "⚠️ That doesn't look like a valid public Instagram Reel URL. Please try again, "
                        "e.g. <code>https://www.instagram.com/reel/XXXXXXXXX/</code>",
        "processing": "⏳ Processing your Reel...",
        "download_success_caption": "✅ Here's your Reel!",
        "btn_download_another": "📥 Download Another",
        "btn_open_instagram": "🔗 Open Instagram",
        "audio_success_caption": "🎵 Here's your extracted audio!",
        "btn_another_audio": "🎵 Another Audio",
        "history_title": "📜 <b>Download History</b>",
        "history_empty": "You have no downloads yet.",
        "history_item": "{icon} <b>{type}</b> — {status}\n🔗 {url}\n🕐 {time}",
        "btn_prev": "◀️ Previous",
        "btn_next": "▶️ Next",
        "btn_clear_history": "🗑️ Clear History",
        "clear_history_confirm": "Are you sure you want to delete your entire download history?",
        "clear_history_yes": "✅ Yes, clear it",
        "clear_history_no": "❌ Cancel",
        "history_cleared": "🗑️ Your history has been cleared.",
        "stats_title": "📊 <b>Your Stats</b>\n\n"
                        "👤 User ID: <code>{user_id}</code>\n"
                        "📥 Total downloads: <b>{total}</b>\n"
                        "🎬 Reel downloads: <b>{reels}</b>\n"
                        "🎵 Audio downloads: <b>{audio}</b>\n"
                        "📅 First seen: {first_seen}\n"
                        "🕐 Last active: {last_active}",
        "settings_title": "⚙️ <b>Settings</b>",
        "btn_language": "🌐 Language",
        "btn_notifications": "🔔 Notifications",
        "btn_clear_my_history": "🗑️ Clear My History",
        "language_prompt": "🌐 Choose your language:",
        "language_set": "✅ Language updated!",
        "notif_on": "🔔 Notifications: ON (tap to turn off)",
        "notif_off": "🔕 Notifications: OFF (tap to turn on)",
        "notif_updated": "✅ Notification preference updated.",
        "help_text": "❓ <b>Help</b>\n\n"
                      "1️⃣ Tap <b>📥 Download Reel</b>\n"
                      "2️⃣ Send a public Instagram Reel link\n"
                      "3️⃣ Get your video back instantly!\n\n"
                      "You can also extract just the audio with <b>🎵 Extract Audio</b>.\n"
                      "Only publicly accessible content can be downloaded — private accounts "
                      "and restricted content are not supported.",
        "about_text": "👨‍💻 <b>About</b>\n\n"
                       "Instagram Reel Downloader Bot\n"
                       "Built with python-telegram-bot &amp; yt-dlp.\n"
                       "Fetches only publicly available media, in line with Instagram's access controls.",
        "banned_message": "🚫 You have been banned from using this bot.",
        "rate_limit_message": "⏱️ Please wait {seconds}s before your next request.",
        "concurrent_limit_message": "⏳ You already have {n} download(s) in progress. Please wait for them to finish.",
        "error_generic": "❌ Something went wrong. Please try again later.",
        "error_private": "🔒 This content is private or restricted and can't be downloaded.",
        "error_unavailable": "❌ This content is unavailable or has been deleted.",
        "error_unsupported": "⚠️ This Instagram URL isn't supported.",
        "error_timeout": "⏱️ The download timed out. Please try again.",
        "error_too_large": "📦 This file is too large to send (limit: {limit}MB).",
        "error_ffmpeg_missing": "⚠️ Audio extraction is unavailable: FFmpeg is not installed on the server.",
        "error_network": "🌐 A network error occurred. Please try again.",
        "error_ffmpeg_failure": "⚠️ Failed to process audio for this video.",
        "error_upload_failure": "❌ Failed to send the file via Telegram. Please try again.",
        "admin_only": "🚫 This command is for admins only.",
        "admin_panel_title": "🛠️ <b>Admin Panel</b>",
        "admin_stats_text": "📊 <b>Bot Statistics</b>\n\n"
                             "👥 Total users: <b>{total_users}</b>\n"
                             "🚫 Banned users: <b>{banned}</b>\n"
                             "📥 Total downloads: <b>{total_downloads}</b>\n"
                             "✅ Successful: <b>{success}</b>\n"
                             "❌ Failed: <b>{failed}</b>",
        "admin_broadcast_prompt": "📢 Send the message you want to broadcast to all users.",
        "admin_broadcast_done": "✅ Broadcast sent to {sent}/{total} users.",
        "admin_ban_prompt": "🚫 Send the Telegram user ID to ban.",
        "admin_ban_done": "✅ User {user_id} has been banned.",
        "admin_unban_prompt": "✅ Send the Telegram user ID to unban.",
        "admin_unban_done": "✅ User {user_id} has been unbanned.",
        "admin_invalid_id": "⚠️ Please send a valid numeric user ID.",
        "admin_user_list_title": "👥 <b>Users</b> (page {page})",
        "admin_forcejoin_placeholder": "📣 Force-join channel settings are managed via the FORCE_JOIN_CHANNEL "
                                        "environment variable (not yet configured).",
        "admin_settings_placeholder": "⚙️ Bot settings are managed via environment variables. "
                                       "See .env.example for all available options.",
        "admin_restart_ack": "🔄 Restart signal received. If running under a process manager "
                              "(Docker/systemd/Railway) the bot will restart automatically.",
    },
    "hi": {
        "welcome_title": "🎬 <b>इंस्टाग्राम डाउनलोडर</b>",
        "welcome_body": "एक पब्लिक इंस्टाग्राम रील लिंक भेजें, या नीचे कोई विकल्प चुनें।",
        "btn_download": "📥 रील डाउनलोड करें",
        "btn_audio": "🎵 ऑडियो निकालें",
        "btn_history": "📜 डाउनलोड इतिहास",
        "btn_stats": "📊 मेरे आँकड़े",
        "btn_settings": "⚙️ सेटिंग्स",
        "btn_help": "❓ मदद",
        "btn_about": "👨‍💻 परिचय",
        "btn_back": "◀️ वापस",
        "btn_main_menu": "🏠 मुख्य मेनू",
        "ask_url_reel": "📥 कृपया एक <b>पब्लिक इंस्टाग्राम रील URL</b> भेजें।",
        "ask_url_audio": "🎵 कृपया एक <b>पब्लिक इंस्टाग्राम रील URL</b> भेजें, मैं ऑडियो निकाल दूँगा।",
        "invalid_url": "⚠️ यह मान्य पब्लिक इंस्टाग्राम रील URL नहीं लगता। कृपया पुनः प्रयास करें, "
                        "जैसे: <code>https://www.instagram.com/reel/XXXXXXXXX/</code>",
        "processing": "⏳ आपकी रील प्रोसेस हो रही है...",
        "download_success_caption": "✅ यह रही आपकी रील!",
        "btn_download_another": "📥 एक और डाउनलोड करें",
        "btn_open_instagram": "🔗 इंस्टाग्राम खोलें",
        "audio_success_caption": "🎵 यह रहा आपका ऑडियो!",
        "btn_another_audio": "🎵 एक और ऑडियो",
        "history_title": "📜 <b>डाउनलोड इतिहास</b>",
        "history_empty": "अभी तक कोई डाउनलोड नहीं है।",
        "history_item": "{icon} <b>{type}</b> — {status}\n🔗 {url}\n🕐 {time}",
        "btn_prev": "◀️ पिछला",
        "btn_next": "▶️ अगला",
        "btn_clear_history": "🗑️ इतिहास साफ़ करें",
        "clear_history_confirm": "क्या आप अपना पूरा डाउनलोड इतिहास हटाना चाहते हैं?",
        "clear_history_yes": "✅ हाँ, साफ़ करें",
        "clear_history_no": "❌ रद्द करें",
        "history_cleared": "🗑️ आपका इतिहास साफ़ कर दिया गया है।",
        "stats_title": "📊 <b>आपके आँकड़े</b>\n\n"
                        "👤 यूज़र आईडी: <code>{user_id}</code>\n"
                        "📥 कुल डाउनलोड: <b>{total}</b>\n"
                        "🎬 रील डाउनलोड: <b>{reels}</b>\n"
                        "🎵 ऑडियो डाउनलोड: <b>{audio}</b>\n"
                        "📅 पहली बार: {first_seen}\n"
                        "🕐 अंतिम सक्रिय: {last_active}",
        "settings_title": "⚙️ <b>सेटिंग्स</b>",
        "btn_language": "🌐 भाषा",
        "btn_notifications": "🔔 सूचनाएं",
        "btn_clear_my_history": "🗑️ मेरा इतिहास साफ़ करें",
        "language_prompt": "🌐 अपनी भाषा चुनें:",
        "language_set": "✅ भाषा अपडेट हो गई!",
        "notif_on": "🔔 सूचनाएं: चालू (बंद करने के लिए टैप करें)",
        "notif_off": "🔕 सूचनाएं: बंद (चालू करने के लिए टैप करें)",
        "notif_updated": "✅ सूचना प्राथमिकता अपडेट हो गई।",
        "help_text": "❓ <b>मदद</b>\n\n"
                      "1️⃣ <b>📥 रील डाउनलोड करें</b> दबाएँ\n"
                      "2️⃣ पब्लिक इंस्टाग्राम रील लिंक भेजें\n"
                      "3️⃣ तुरंत अपनी वीडियो पाएँ!\n\n"
                      "आप <b>🎵 ऑडियो निकालें</b> से केवल ऑडियो भी निकाल सकते हैं।\n"
                      "केवल पब्लिक कंटेंट डाउनलोड किया जा सकता है — प्राइवेट अकाउंट समर्थित नहीं हैं।",
        "about_text": "👨‍💻 <b>परिचय</b>\n\n"
                       "इंस्टाग्राम रील डाउनलोडर बॉट\n"
                       "python-telegram-bot और yt-dlp से बना है।\n"
                       "केवल पब्लिक रूप से उपलब्ध मीडिया लाता है।",
        "banned_message": "🚫 आपको इस बॉट का उपयोग करने से प्रतिबंधित कर दिया गया है।",
        "rate_limit_message": "⏱️ कृपया अगली रिक्वेस्ट से पहले {seconds} सेकंड प्रतीक्षा करें।",
        "concurrent_limit_message": "⏳ आपके {n} डाउनलोड पहले से चल रहे हैं। कृपया प्रतीक्षा करें।",
        "error_generic": "❌ कुछ गलत हो गया। कृपया बाद में पुनः प्रयास करें।",
        "error_private": "🔒 यह कंटेंट प्राइवेट या प्रतिबंधित है और डाउनलोड नहीं किया जा सकता।",
        "error_unavailable": "❌ यह कंटेंट उपलब्ध नहीं है या हटा दिया गया है।",
        "error_unsupported": "⚠️ यह इंस्टाग्राम URL समर्थित नहीं है।",
        "error_timeout": "⏱️ डाउनलोड का समय समाप्त हो गया। कृपया पुनः प्रयास करें।",
        "error_too_large": "📦 यह फ़ाइल भेजने के लिए बहुत बड़ी है (सीमा: {limit}MB)।",
        "error_ffmpeg_missing": "⚠️ ऑडियो निष्कर्षण उपलब्ध नहीं है: सर्वर पर FFmpeg स्थापित नहीं है।",
        "error_network": "🌐 एक नेटवर्क त्रुटि हुई। कृपया पुनः प्रयास करें।",
        "error_ffmpeg_failure": "⚠️ इस वीडियो के लिए ऑडियो प्रोसेस करने में विफल।",
        "error_upload_failure": "❌ Telegram के माध्यम से फ़ाइल भेजने में विफल। कृपया पुनः प्रयास करें।",
        "admin_only": "🚫 यह कमांड केवल एडमिन के लिए है।",
        "admin_panel_title": "🛠️ <b>एडमिन पैनल</b>",
        "admin_stats_text": "📊 <b>बॉट आँकड़े</b>\n\n"
                             "👥 कुल यूज़र: <b>{total_users}</b>\n"
                             "🚫 प्रतिबंधित यूज़र: <b>{banned}</b>\n"
                             "📥 कुल डाउनलोड: <b>{total_downloads}</b>\n"
                             "✅ सफल: <b>{success}</b>\n"
                             "❌ विफल: <b>{failed}</b>",
        "admin_broadcast_prompt": "📢 वह संदेश भेजें जो आप सभी यूज़र को भेजना चाहते हैं।",
        "admin_broadcast_done": "✅ प्रसारण {sent}/{total} यूज़र को भेजा गया।",
        "admin_ban_prompt": "🚫 प्रतिबंधित करने के लिए Telegram यूज़र आईडी भेजें।",
        "admin_ban_done": "✅ यूज़र {user_id} को प्रतिबंधित कर दिया गया है।",
        "admin_unban_prompt": "✅ अप्रतिबंधित करने के लिए Telegram यूज़र आईडी भेजें।",
        "admin_unban_done": "✅ यूज़र {user_id} को अप्रतिबंधित कर दिया गया है।",
        "admin_invalid_id": "⚠️ कृपया एक मान्य संख्यात्मक यूज़र आईडी भेजें।",
        "admin_user_list_title": "👥 <b>यूज़र</b> (पृष्ठ {page})",
        "admin_forcejoin_placeholder": "📣 फ़ोर्स-जॉइन चैनल सेटिंग्स FORCE_JOIN_CHANNEL "
                                        "एनवायरनमेंट वेरिएबल से प्रबंधित होती हैं (अभी कॉन्फ़िगर नहीं)।",
        "admin_settings_placeholder": "⚙️ बॉट सेटिंग्स एनवायरनमेंट वेरिएबल से प्रबंधित होती हैं। "
                                       "सभी विकल्पों के लिए .env.example देखें।",
        "admin_restart_ack": "🔄 पुनरारंभ संकेत प्राप्त हुआ। यदि प्रोसेस मैनेजर (Docker/systemd/Railway) "
                              "के अंतर्गत चल रहा है, तो बॉट स्वतः पुनरारंभ हो जाएगा।",
    },
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    """Translate a key into the given language, falling back to English."""
    table = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = table.get(key, TRANSLATIONS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text


# =====================================================================================
# DATABASE
# =====================================================================================

class Database:
    """Thin, thread-safe SQLite wrapper exposed via async methods.

    Kept deliberately simple (stdlib sqlite3 + a lock + executor) so the whole
    bot can live in a single file without extra async-sqlite dependencies.
    Swapping to PostgreSQL later only requires replacing this class.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_seen TEXT NOT NULL,
                    last_active TEXT NOT NULL,
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    language TEXT NOT NULL DEFAULT 'en',
                    notifications INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);

                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_downloads_telegram_id ON downloads(telegram_id);
                CREATE INDEX IF NOT EXISTS idx_downloads_created_at ON downloads(created_at);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()

    # ---- sync internals (run in executor) -------------------------------------------

    def _get_or_create_user_sync(self, telegram_id: int, username: Optional[str]) -> dict:
        now = datetime.utcnow().isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET last_active=?, username=? WHERE telegram_id=?",
                    (now, username, telegram_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
                return dict(row)
            conn.execute(
                "INSERT INTO users (telegram_id, username, first_seen, last_active, "
                "is_banned, language, notifications) VALUES (?, ?, ?, ?, 0, 'en', 1)",
                (telegram_id, username, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return dict(row)

    def _set_language_sync(self, telegram_id: int, lang: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET language=? WHERE telegram_id=?", (lang, telegram_id)
            )
            conn.commit()

    def _toggle_notifications_sync(self, telegram_id: int) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT notifications FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            new_val = 0 if (row and row["notifications"]) else 1
            conn.execute(
                "UPDATE users SET notifications=? WHERE telegram_id=?",
                (new_val, telegram_id),
            )
            conn.commit()
            return new_val

    def _is_banned_sync(self, telegram_id: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT is_banned FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return bool(row["is_banned"]) if row else False

    def _set_banned_sync(self, telegram_id: int, banned: bool) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE users SET is_banned=? WHERE telegram_id=?",
                (1 if banned else 0, telegram_id),
            )
            conn.commit()
            return True

    def _add_download_sync(
        self, telegram_id: int, url: str, media_type: str, status: str
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO downloads (telegram_id, url, media_type, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, url, media_type, status, now),
            )
            conn.commit()

    def _get_user_downloads_sync(
        self, telegram_id: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM downloads WHERE telegram_id=? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (telegram_id, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) c FROM downloads WHERE telegram_id=?", (telegram_id,)
            ).fetchone()["c"]
            return [dict(r) for r in rows], total

    def _clear_user_history_sync(self, telegram_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM downloads WHERE telegram_id=?", (telegram_id,))
            conn.commit()

    def _get_user_stats_sync(self, telegram_id: int) -> dict:
        with self._lock, self._connect() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) c FROM downloads WHERE telegram_id=? AND status='success'",
                (telegram_id,),
            ).fetchone()["c"]
            reels = conn.execute(
                "SELECT COUNT(*) c FROM downloads WHERE telegram_id=? AND media_type='reel' "
                "AND status='success'",
                (telegram_id,),
            ).fetchone()["c"]
            audio = conn.execute(
                "SELECT COUNT(*) c FROM downloads WHERE telegram_id=? AND media_type='audio' "
                "AND status='success'",
                (telegram_id,),
            ).fetchone()["c"]
            return {
                "user": dict(user) if user else None,
                "total": total,
                "reels": reels,
                "audio": audio,
            }

    def _get_global_stats_sync(self) -> dict:
        with self._lock, self._connect() as conn:
            return {
                "total_users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
                "banned": conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE is_banned=1"
                ).fetchone()["c"],
                "total_downloads": conn.execute(
                    "SELECT COUNT(*) c FROM downloads"
                ).fetchone()["c"],
                "success": conn.execute(
                    "SELECT COUNT(*) c FROM downloads WHERE status='success'"
                ).fetchone()["c"],
                "failed": conn.execute(
                    "SELECT COUNT(*) c FROM downloads WHERE status='failed'"
                ).fetchone()["c"],
            }

    def _get_all_users_sync(self, limit: int, offset: int) -> tuple[list[dict], int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY first_seen DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            return [dict(r) for r in rows], total

    def _get_all_telegram_ids_sync(self) -> list[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT telegram_id FROM users WHERE is_banned=0"
            ).fetchall()
            return [r["telegram_id"] for r in rows]

    # ---- async facade -----------------------------------------------------------------

    async def _run(self, func, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func, *args)

    async def get_or_create_user(self, telegram_id: int, username: Optional[str]) -> dict:
        return await self._run(self._get_or_create_user_sync, telegram_id, username)

    async def set_language(self, telegram_id: int, lang: str) -> None:
        await self._run(self._set_language_sync, telegram_id, lang)

    async def toggle_notifications(self, telegram_id: int) -> int:
        return await self._run(self._toggle_notifications_sync, telegram_id)

    async def is_banned(self, telegram_id: int) -> bool:
        return await self._run(self._is_banned_sync, telegram_id)

    async def set_banned(self, telegram_id: int, banned: bool) -> bool:
        return await self._run(self._set_banned_sync, telegram_id, banned)

    async def add_download(
        self, telegram_id: int, url: str, media_type: str, status: str
    ) -> None:
        await self._run(self._add_download_sync, telegram_id, url, media_type, status)

    async def get_user_downloads(
        self, telegram_id: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        return await self._run(self._get_user_downloads_sync, telegram_id, limit, offset)

    async def clear_user_history(self, telegram_id: int) -> None:
        await self._run(self._clear_user_history_sync, telegram_id)

    async def get_user_stats(self, telegram_id: int) -> dict:
        return await self._run(self._get_user_stats_sync, telegram_id)

    async def get_global_stats(self) -> dict:
        return await self._run(self._get_global_stats_sync)

    async def get_all_users(self, limit: int, offset: int) -> tuple[list[dict], int]:
        return await self._run(self._get_all_users_sync, limit, offset)

    async def get_all_telegram_ids(self) -> list[int]:
        return await self._run(self._get_all_telegram_ids_sync)


# =====================================================================================
# UTILS: URL validation, filename sanitization, rate limiting
# =====================================================================================

INSTAGRAM_URL_RE = re.compile(
    r"^https?://(www\.|m\.)?instagram\.com/(reel|reels|p|tv)/[A-Za-z0-9_\-]+/?(\?.*)?$"
)

ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}


def is_valid_instagram_url(url: str) -> bool:
    """Validate that the URL is a well-formed, public Instagram reel/post URL."""
    if not url or len(url) > 500:
        return False
    url = url.strip()
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return False
    return bool(INSTAGRAM_URL_RE.match(url))


def sanitize_filename(name: str) -> str:
    """Strip anything that isn't a safe filename character, prevent traversal."""
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", name)
    name = name.replace("..", "_")
    return name[:150] if name else "file"


def new_job_id() -> str:
    """Generate a random, filesystem-safe job id (never derived from user input)."""
    return uuid.uuid4().hex


def human_time(iso_string: Optional[str]) -> str:
    if not iso_string:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_string


class RateLimiter:
    """Per-user cooldown + concurrent-download cap, fully in-memory and thread-safe."""

    def __init__(self, cooldown_seconds: int, max_concurrent: int):
        self.cooldown = cooldown_seconds
        self.max_concurrent = max_concurrent
        self._last_request: dict[int, float] = {}
        self._active: dict[int, int] = {}
        self._lock = threading.Lock()

    def seconds_until_ready(self, user_id: int) -> float:
        with self._lock:
            elapsed = time.time() - self._last_request.get(user_id, 0.0)
            remaining = self.cooldown - elapsed
            return max(0.0, round(remaining, 1))

    def record_request(self, user_id: int) -> None:
        with self._lock:
            self._last_request[user_id] = time.time()

    def try_acquire(self, user_id: int) -> bool:
        with self._lock:
            current = self._active.get(user_id, 0)
            if current >= self.max_concurrent:
                return False
            self._active[user_id] = current + 1
            return True

    def release(self, user_id: int) -> None:
        with self._lock:
            current = self._active.get(user_id, 0)
            self._active[user_id] = max(0, current - 1)

    def active_count(self, user_id: int) -> int:
        with self._lock:
            return self._active.get(user_id, 0)


# =====================================================================================
# DOWNLOADER (yt-dlp + ffmpeg), only for publicly accessible content
# =====================================================================================

class DownloadError(Exception):
    """Raised for any recoverable download/extraction failure, with a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class Downloader:
    def __init__(self, temp_dir: str, timeout: int, max_file_size_mb: int):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.max_file_size_mb = max_file_size_mb
        self.ffmpeg_available = shutil.which("ffmpeg") is not None
        if not self.ffmpeg_available:
            logger.warning(
                "FFmpeg was not found on PATH. Audio extraction will be unavailable "
                "until FFmpeg is installed (see README.md)."
            )

    def _ydl_opts(self, out_template: str) -> dict:
        return {
            "outtmpl": out_template,
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 2,
            "max_filesize": self.max_file_size_mb * 1024 * 1024,
            "restrictfilenames": True,
        }

    def _download_sync(self, url: str, job_id: str) -> str:
        out_template = str(self.temp_dir / f"{job_id}.%(ext)s")
        opts = self._ydl_opts(out_template)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                if not os.path.exists(filepath):
                    for ext in ("mp4", "mkv", "webm"):
                        candidate = str(self.temp_dir / f"{job_id}.{ext}")
                        if os.path.exists(candidate):
                            filepath = candidate
                            break
                if not os.path.exists(filepath):
                    raise DownloadError("MEDIA_UNAVAILABLE", "Downloaded file not found.")
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                if size_mb > self.max_file_size_mb:
                    os.remove(filepath)
                    raise DownloadError(
                        "FILE_TOO_LARGE",
                        f"File exceeds the {self.max_file_size_mb}MB limit.",
                    )
                return filepath
        except DownloadError:
            raise
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            if "private" in msg or "login" in msg:
                raise DownloadError("PRIVATE_CONTENT", "This content is private or restricted.")
            if "not found" in msg or "unavailable" in msg or "removed" in msg or "404" in msg:
                raise DownloadError(
                    "MEDIA_UNAVAILABLE", "This content is unavailable or has been deleted."
                )
            if "unsupported url" in msg or "no video formats" in msg:
                raise DownloadError("UNSUPPORTED_URL", "This Instagram URL is not supported.")
            if "timed out" in msg or "timeout" in msg:
                raise DownloadError("TIMEOUT", "The download timed out.")
            raise DownloadError("API_FAILURE", "Could not fetch this content right now.")
        except Exception:
            logger.exception("Unexpected error while downloading %s", url)
            raise DownloadError("UNKNOWN_ERROR", "An unexpected error occurred.")

    async def download_video(self, url: str, job_id: str) -> str:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._download_sync, url, job_id),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise DownloadError("TIMEOUT", "Download timed out. Please try again.")

    def _extract_audio_sync(self, video_path: str, job_id: str) -> str:
        if not self.ffmpeg_available:
            raise DownloadError(
                "FFMPEG_MISSING", "Audio extraction requires FFmpeg, which is not installed."
            )
        audio_path = str(self.temp_dir / f"{job_id}.mp3")
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", video_path,
                    "-vn", "-acodec", "libmp3lame", "-q:a", "2",
                    audio_path,
                ],
                capture_output=True,
                timeout=self.timeout,
                text=True,
            )
            if result.returncode != 0 or not os.path.exists(audio_path):
                logger.error("FFmpeg failed: %s", result.stderr[-500:] if result.stderr else "")
                raise DownloadError("FFMPEG_FAILURE", "Failed to extract audio from the video.")
            return audio_path
        except DownloadError:
            raise
        except subprocess.TimeoutExpired:
            raise DownloadError("TIMEOUT", "Audio extraction timed out.")
        except Exception:
            logger.exception("Unexpected error extracting audio for job %s", job_id)
            raise DownloadError("FFMPEG_FAILURE", "Failed to extract audio from the video.")

    async def extract_audio(self, video_path: str, job_id: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract_audio_sync, video_path, job_id)

    @staticmethod
    def cleanup(*paths: Optional[str]) -> None:
        for p in paths:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                logger.warning("Failed to remove temp file: %s", p)


ERROR_CODE_TO_MESSAGE_KEY = {
    "PRIVATE_CONTENT": "error_private",
    "MEDIA_UNAVAILABLE": "error_unavailable",
    "UNSUPPORTED_URL": "error_unsupported",
    "TIMEOUT": "error_timeout",
    "FILE_TOO_LARGE": "error_too_large",
    "FFMPEG_MISSING": "error_ffmpeg_missing",
    "FFMPEG_FAILURE": "error_ffmpeg_failure",
    "API_FAILURE": "error_generic",
    "UNKNOWN_ERROR": "error_generic",
}


# =====================================================================================
# KEYBOARDS
# =====================================================================================

def kb_main_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("btn_download", lang), callback_data="menu:download")],
            [InlineKeyboardButton(t("btn_audio", lang), callback_data="menu:audio")],
            [
                InlineKeyboardButton(t("btn_history", lang), callback_data="history:0"),
                InlineKeyboardButton(t("btn_stats", lang), callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton(t("btn_settings", lang), callback_data="menu:settings"),
                InlineKeyboardButton(t("btn_help", lang), callback_data="menu:help"),
            ],
            [InlineKeyboardButton(t("btn_about", lang), callback_data="menu:about")],
        ]
    )


def kb_back_main(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("btn_main_menu", lang), callback_data="menu:main")]]
    )


def kb_after_download(lang: str, media_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("btn_download_another", lang), callback_data="menu:download")],
            [InlineKeyboardButton(t("btn_open_instagram", lang), url=media_url)],
            [InlineKeyboardButton(t("btn_main_menu", lang), callback_data="menu:main")],
        ]
    )


def kb_after_audio(lang: str, media_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("btn_another_audio", lang), callback_data="menu:audio")],
            [InlineKeyboardButton(t("btn_open_instagram", lang), url=media_url)],
            [InlineKeyboardButton(t("btn_main_menu", lang), callback_data="menu:main")],
        ]
    )


def kb_history(lang: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(t("btn_prev", lang), callback_data=f"history:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(t("btn_next", lang), callback_data=f"history:{page + 1}"))
    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(t("btn_clear_history", lang), callback_data="history:clear")])
    rows.append([InlineKeyboardButton(t("btn_main_menu", lang), callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_clear(lang: str, target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("clear_history_yes", lang), callback_data=f"{target}:yes"),
                InlineKeyboardButton(t("clear_history_no", lang), callback_data=f"{target}:no"),
            ]
        ]
    )


def kb_settings(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("btn_language", lang), callback_data="settings:language")],
            [InlineKeyboardButton(t("btn_notifications", lang), callback_data="settings:notifications")],
            [InlineKeyboardButton(t("btn_clear_my_history", lang), callback_data="settings:clear")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data="menu:main")],
        ]
    )


def kb_language(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
                InlineKeyboardButton("🇮🇳 हिंदी", callback_data="lang:hi"),
            ],
            [InlineKeyboardButton(t("btn_back", lang), callback_data="menu:settings")],
        ]
    )


def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Statistics", callback_data="admin:stats"),
                InlineKeyboardButton("👥 User List", callback_data="admin:users:0"),
            ],
            [
                InlineKeyboardButton("📥 Download Stats", callback_data="admin:downloads"),
                InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton("🚫 Ban User", callback_data="admin:ban"),
                InlineKeyboardButton("✅ Unban User", callback_data="admin:unban"),
            ],
            [
                InlineKeyboardButton("📣 Force Join Settings", callback_data="admin:forcejoin"),
                InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin:settings"),
            ],
            [InlineKeyboardButton("🔄 Restart", callback_data="admin:restart")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main")],
        ]
    )


def kb_admin_users(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin:users:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️ Next", callback_data=f"admin:users:{page + 1}"))
    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)


def kb_admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin:main")]])


# =====================================================================================
# HANDLER HELPERS
# =====================================================================================

def get_services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Database, Downloader, RateLimiter]:
    db: Database = context.bot_data["db"]
    downloader: Downloader = context.bot_data["downloader"]
    limiter: RateLimiter = context.bot_data["rate_limiter"]
    return db, downloader, limiter


async def get_user_lang(db: Database, telegram_id: int, username: Optional[str]) -> tuple[dict, str]:
    user = await db.get_or_create_user(telegram_id, username)
    return user, user.get("language", "en")


def is_admin(user_id: int) -> bool:
    return user_id in Config.ADMIN_IDS


async def safe_edit_or_send(query, text: str, reply_markup=None) -> None:
    """Edit the callback's message; if that fails (e.g. same content), send a new one."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except TelegramError:
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except TelegramError:
            logger.exception("Failed to send fallback message")


# =====================================================================================
# CORE HANDLERS: /start, main menu navigation
# =====================================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, _, _ = get_services(context)
    tg_user = update.effective_user
    user, lang = await get_user_lang(db, tg_user.id, tg_user.username)
    if user["is_banned"]:
        await update.message.reply_text(t("banned_message", lang))
        return
    context.user_data.clear()
    text = f"{t('welcome_title', lang)}\n\n{t('welcome_body', lang)}"
    await update.message.reply_text(text, reply_markup=kb_main_menu(lang), parse_mode=ParseMode.HTML)


async def show_main_menu(query, db: Database, lang: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    text = f"{t('welcome_title', lang)}\n\n{t('welcome_body', lang)}"
    await safe_edit_or_send(query, text, kb_main_menu(lang))


# =====================================================================================
# DOWNLOAD / AUDIO FLOWS
# =====================================================================================

async def prompt_for_url(query, lang: str, mode: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """mode is 'reel' or 'audio'."""
    context.user_data["awaiting"] = mode
    key = "ask_url_reel" if mode == "reel" else "ask_url_audio"
    await safe_edit_or_send(query, t(key, lang), kb_back_main(lang))


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, downloader, limiter = get_services(context)
    tg_user = update.effective_user
    user, lang = await get_user_lang(db, tg_user.id, tg_user.username)

    if user["is_banned"]:
        await update.message.reply_text(t("banned_message", lang))
        return

    # Admin free-text flows (broadcast / ban / unban)
    admin_state = context.user_data.get("admin_awaiting")
    if admin_state and is_admin(tg_user.id):
        await handle_admin_text_input(update, context, admin_state, lang)
        return

    awaiting = context.user_data.get("awaiting")
    if awaiting not in ("reel", "audio"):
        # No pending action; nudge the user back to the menu.
        await update.message.reply_text(
            f"{t('welcome_title', lang)}\n\n{t('welcome_body', lang)}",
            reply_markup=kb_main_menu(lang),
            parse_mode=ParseMode.HTML,
        )
        return

    url = (update.message.text or "").strip()
    if not is_valid_instagram_url(url):
        await update.message.reply_text(t("invalid_url", lang), parse_mode=ParseMode.HTML)
        return

    # Rate limiting
    wait = limiter.seconds_until_ready(tg_user.id)
    if wait > 0:
        await update.message.reply_text(t("rate_limit_message", lang, seconds=wait))
        return
    if not limiter.try_acquire(tg_user.id):
        await update.message.reply_text(
            t("concurrent_limit_message", lang, n=limiter.active_count(tg_user.id))
        )
        return
    limiter.record_request(tg_user.id)

    context.user_data["awaiting"] = None
    processing_msg = await update.message.reply_text(t("processing", lang))

    job_id = new_job_id()
    media_type = "reel" if awaiting == "reel" else "audio"
    video_path: Optional[str] = None
    audio_path: Optional[str] = None
    try:
        video_path = await downloader.download_video(url, job_id)

        if awaiting == "reel":
            with open(video_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=t("download_success_caption", lang),
                    reply_markup=kb_after_download(lang, url),
                )
            await db.add_download(tg_user.id, url, "reel", "success")
        else:
            audio_path = await downloader.extract_audio(video_path, job_id)
            with open(audio_path, "rb") as f:
                await update.message.reply_audio(
                    audio=f,
                    caption=t("audio_success_caption", lang),
                    reply_markup=kb_after_audio(lang, url),
                )
            await db.add_download(tg_user.id, url, "audio", "success")

    except DownloadError as e:
        logger.info("Download failed for user=%s url=%s code=%s", tg_user.id, url, e.code)
        msg_key = ERROR_CODE_TO_MESSAGE_KEY.get(e.code, "error_generic")
        if e.code == "FILE_TOO_LARGE":
            err_text = t(msg_key, lang, limit=Config.MAX_FILE_SIZE_MB)
        else:
            err_text = t(msg_key, lang)
        await update.message.reply_text(err_text, reply_markup=kb_back_main(lang))
        await db.add_download(tg_user.id, url, media_type, "failed")
    except TelegramError:
        logger.exception("Telegram upload failure for user=%s", tg_user.id)
        await update.message.reply_text(t("error_upload_failure", lang), reply_markup=kb_back_main(lang))
        await db.add_download(tg_user.id, url, media_type, "failed")
    except Exception:
        logger.exception("Unknown error handling download for user=%s", tg_user.id)
        await update.message.reply_text(t("error_generic", lang), reply_markup=kb_back_main(lang))
        await db.add_download(tg_user.id, url, media_type, "failed")
    finally:
        limiter.release(tg_user.id)
        Downloader.cleanup(video_path, audio_path)
        try:
            await processing_msg.delete()
        except TelegramError:
            pass


# =====================================================================================
# HISTORY
# =====================================================================================

async def render_history(query, db: Database, lang: str, telegram_id: int, page: int) -> None:
    limit = Config.HISTORY_PAGE_SIZE
    offset = page * limit
    rows, total = await db.get_user_downloads(telegram_id, limit, offset)
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(page, total_pages - 1)

    if not rows:
        text = f"{t('history_title', lang)}\n\n{t('history_empty', lang)}"
        await safe_edit_or_send(query, text, kb_back_main(lang))
        return

    lines = [t("history_title", lang), ""]
    for row in rows:
        icon = "🎬" if row["media_type"] == "reel" else "🎵"
        status_icon = "✅" if row["status"] == "success" else "❌"
        lines.append(
            t(
                "history_item",
                lang,
                icon=icon,
                type=row["media_type"].capitalize(),
                status=f"{status_icon} {row['status']}",
                url=row["url"],
                time=human_time(row["created_at"]),
            )
        )
        lines.append("")
    text = "\n".join(lines).strip()
    await safe_edit_or_send(query, text, kb_history(lang, page, total_pages))


# =====================================================================================
# CALLBACK QUERY ROUTER
# =====================================================================================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    tg_user = update.effective_user
    db, downloader, limiter = get_services(context)
    user, lang = await get_user_lang(db, tg_user.id, tg_user.username)

    await query.answer()

    if user["is_banned"]:
        await safe_edit_or_send(query, t("banned_message", lang))
        return

    data = query.data or ""

    # ---------------- Main menu ----------------
    if data == "menu:main":
        await show_main_menu(query, db, lang, context)
        return

    if data == "menu:download":
        await prompt_for_url(query, lang, "reel", context)
        return

    if data == "menu:audio":
        await prompt_for_url(query, lang, "audio", context)
        return

    if data == "menu:help":
        await safe_edit_or_send(query, t("help_text", lang), kb_back_main(lang))
        return

    if data == "menu:about":
        await safe_edit_or_send(query, t("about_text", lang), kb_back_main(lang))
        return

    if data == "menu:stats":
        stats = await db.get_user_stats(tg_user.id)
        u = stats["user"] or {}
        text = t(
            "stats_title",
            lang,
            user_id=tg_user.id,
            total=stats["total"],
            reels=stats["reels"],
            audio=stats["audio"],
            first_seen=human_time(u.get("first_seen")),
            last_active=human_time(u.get("last_active")),
        )
        await safe_edit_or_send(query, text, kb_back_main(lang))
        return

    if data == "menu:settings":
        await safe_edit_or_send(query, t("settings_title", lang), kb_settings(lang))
        return

    # ---------------- History ----------------
    if data.startswith("history:"):
        suffix = data.split(":", 1)[1]
        if suffix == "clear":
            await safe_edit_or_send(
                query, t("clear_history_confirm", lang), kb_confirm_clear(lang, "historyclear")
            )
            return
        page = int(suffix) if suffix.isdigit() else 0
        context.user_data["history_page"] = page
        await render_history(query, db, lang, tg_user.id, page)
        return

    if data.startswith("historyclear:"):
        choice = data.split(":", 1)[1]
        if choice == "yes":
            await db.clear_user_history(tg_user.id)
            await safe_edit_or_send(query, t("history_cleared", lang), kb_back_main(lang))
        else:
            await render_history(query, db, lang, tg_user.id, context.user_data.get("history_page", 0))
        return

    # ---------------- Settings ----------------
    if data == "settings:language":
        await safe_edit_or_send(query, t("language_prompt", lang), kb_language(lang))
        return

    if data.startswith("lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang in TRANSLATIONS:
            await db.set_language(tg_user.id, new_lang)
        await safe_edit_or_send(query, t("language_set", new_lang), kb_settings(new_lang))
        return

    if data == "settings:notifications":
        new_val = await db.toggle_notifications(tg_user.id)
        key = "notif_on" if new_val else "notif_off"
        await query.answer(t("notif_updated", lang), show_alert=False)
        await safe_edit_or_send(query, f"{t('settings_title', lang)}\n\n{t(key, lang)}", kb_settings(lang))
        return

    if data == "settings:clear":
        await safe_edit_or_send(
            query, t("clear_history_confirm", lang), kb_confirm_clear(lang, "settingsclear")
        )
        return

    if data.startswith("settingsclear:"):
        choice = data.split(":", 1)[1]
        if choice == "yes":
            await db.clear_user_history(tg_user.id)
            await safe_edit_or_send(query, t("history_cleared", lang), kb_settings(lang))
        else:
            await safe_edit_or_send(query, t("settings_title", lang), kb_settings(lang))
        return

    # ---------------- Admin ----------------
    if data.startswith("admin:"):
        if not is_admin(tg_user.id):
            await safe_edit_or_send(query, t("admin_only", lang))
            return
        await handle_admin_callback(query, context, data, lang)
        return

    logger.warning("Unhandled callback_data: %s", data)


# =====================================================================================
# ADMIN PANEL
# =====================================================================================

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, _, _ = get_services(context)
    tg_user = update.effective_user
    user, lang = await get_user_lang(db, tg_user.id, tg_user.username)
    if not is_admin(tg_user.id):
        await update.message.reply_text(t("admin_only", lang))
        return
    context.user_data.pop("admin_awaiting", None)
    await update.message.reply_text(t("admin_panel_title", lang), reply_markup=kb_admin_main(), parse_mode=ParseMode.HTML)


async def handle_admin_callback(query, context: ContextTypes.DEFAULT_TYPE, data: str, lang: str) -> None:
    db, _, _ = get_services(context)
    action = data.split(":", 1)[1]

    if action == "main":
        context.user_data.pop("admin_awaiting", None)
        await safe_edit_or_send(query, t("admin_panel_title", lang), kb_admin_main())
        return

    if action == "stats" or action == "downloads":
        stats = await db.get_global_stats()
        text = t("admin_stats_text", lang, **stats)
        await safe_edit_or_send(query, text, kb_admin_back())
        return

    if action.startswith("users:"):
        page = int(action.split(":", 1)[1])
        limit = 10
        rows, total = await db.get_all_users(limit, page * limit)
        total_pages = max(1, (total + limit - 1) // limit)
        page = min(page, total_pages - 1)
        lines = [t("admin_user_list_title", lang, page=page + 1), ""]
        for u in rows:
            ban_flag = "🚫" if u["is_banned"] else "✅"
            uname = f"@{u['username']}" if u.get("username") else "(no username)"
            lines.append(f"{ban_flag} <code>{u['telegram_id']}</code> — {uname}")
        text = "\n".join(lines)
        await safe_edit_or_send(query, text, kb_admin_users(page, total_pages))
        return

    if action == "broadcast":
        context.user_data["admin_awaiting"] = "broadcast"
        await safe_edit_or_send(query, t("admin_broadcast_prompt", lang), kb_admin_back())
        return

    if action == "ban":
        context.user_data["admin_awaiting"] = "ban"
        await safe_edit_or_send(query, t("admin_ban_prompt", lang), kb_admin_back())
        return

    if action == "unban":
        context.user_data["admin_awaiting"] = "unban"
        await safe_edit_or_send(query, t("admin_unban_prompt", lang), kb_admin_back())
        return

    if action == "forcejoin":
        await safe_edit_or_send(query, t("admin_forcejoin_placeholder", lang), kb_admin_back())
        return

    if action == "settings":
        await safe_edit_or_send(query, t("admin_settings_placeholder", lang), kb_admin_back())
        return

    if action == "restart":
        await safe_edit_or_send(query, t("admin_restart_ack", lang), kb_admin_back())
        return

    logger.warning("Unhandled admin action: %s", action)


async def handle_admin_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, lang: str
) -> None:
    db, _, _ = get_services(context)
    text = (update.message.text or "").strip()
    context.user_data.pop("admin_awaiting", None)

    if state == "broadcast":
        ids = await db.get_all_telegram_ids()
        sent = 0
        for uid in ids:
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                sent += 1
            except TelegramError:
                logger.info("Broadcast failed for user %s", uid)
        await update.message.reply_text(
            t("admin_broadcast_done", lang, sent=sent, total=len(ids)),
            reply_markup=kb_admin_main(),
        )
        return

    if state in ("ban", "unban"):
        if not text.isdigit():
            await update.message.reply_text(t("admin_invalid_id", lang), reply_markup=kb_admin_main())
            return
        target_id = int(text)
        ok = await db.set_banned(target_id, banned=(state == "ban"))
        if not ok:
            await update.message.reply_text(t("admin_invalid_id", lang), reply_markup=kb_admin_main())
            return
        key = "admin_ban_done" if state == "ban" else "admin_unban_done"
        await update.message.reply_text(
            t(key, lang, user_id=target_id), reply_markup=kb_admin_main(), parse_mode=ParseMode.HTML
        )
        return


# =====================================================================================
# GLOBAL ERROR HANDLER
# =====================================================================================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(t("error_generic", "en"))
    except Exception:
        pass


# =====================================================================================
# STARTUP
# =====================================================================================

def build_application() -> Application:
    Config.validate()

    db = Database(Config.sqlite_path())
    downloader = Downloader(Config.TEMP_DIR, Config.DOWNLOAD_TIMEOUT, Config.MAX_FILE_SIZE_MB)
    limiter = RateLimiter(Config.RATE_LIMIT_SECONDS, Config.MAX_CONCURRENT_DOWNLOADS)

    application = Application.builder().token(Config.BOT_TOKEN).build()
    application.bot_data["db"] = db
    application.bot_data["downloader"] = downloader
    application.bot_data["rate_limiter"] = limiter

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_error_handler(on_error)

    if not downloader.ffmpeg_available:
        logger.warning(
            "⚠️  FFmpeg not detected. Install it to enable the 'Extract Audio' feature "
            "(e.g. `apt install ffmpeg` / `brew install ffmpeg` / included in the provided Dockerfile)."
        )

    return application


def main() -> None:
    application = build_application()
    logger.info("🎬 Instagram Downloader Bot starting (polling mode)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
