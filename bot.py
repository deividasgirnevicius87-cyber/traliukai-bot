import os
import sqlite3
import logging
import re
import json
import asyncio
from datetime import datetime, timedelta
import stripe
from google import genai
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    ChatJoinRequestHandler,
    MessageHandler,
    CommandHandler,
    filters
)

# --- KONFIGŪRACIJA ---
TELEGRAM_BOT_TOKEN = "9206631356:AAFq65tsgPUPLVnnKypU4rSGPPlHqo7j0l8"
GEMINI_API_KEY = "AQ.Ab8RN6J2Q0HGgTIIS7sotwzvTbXhRzkjsmVQtp_srepKibsEsg"
STRIPE_API_KEY = "mk_1TguBTBmGXQcERFrKQhTXKxo"
STRIPE_PRICE_ID = "price_1UYSXYBmGXQcERFrR0Um9w39"
STRIPE_PAYMENT_LINK = "https://buy.stripe.com/8x29AS1M970Lbfm1vqasg00"
WEBHOOK_PORT = 8080
TARGET_GROUP_ID = -1001823350926  # Jūsų privačios grupės ID

stripe.api_key = STRIPE_API_KEY
client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)
URL_PATTERN = re.compile(r'(https?://\S+|www\.\S+|t\.me/\S+)', re.IGNORECASE)

# Globalus kintamasis botui pasiekti web serveryje
bot_app = None

# --- DUOMENŲ BAZĖS VALDYMAS ---

def init_db():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            expires_at TEXT,
            reminded INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_username TEXT UNIQUE,
            email TEXT,
            expiry_date TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def add_subscription(user_id: int, chat_id: int, days: int = 30):
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    expires_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO subscriptions (user_id, chat_id, expires_at, reminded)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            expires_at = excluded.expires_at,
            reminded = 0
    """, (user_id, chat_id, expires_at))
    conn.commit()
    conn.close()

# --- START KOMANDOS APDOROJIMAS (BOTO VARTAI) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_name = user.first_name or "Narys"
    username = user.username
    
    # Patikriname, ar tai jūsų testinis slapyvardis arba patvirtintas narys bazėje
    is_subscriber = False
    if username:
        clean_username = username.strip().lstrip("@").lower()
        if clean_username == "ela1238":
            is_subscriber = True
        else:
            conn = sqlite3.connect("subscriptions.db")
            cursor = conn.cursor()
            cursor.execute("SELECT expiry_date FROM subscribers WHERE LOWER(telegram_username) = ?", (clean_username,))
            row = cursor.fetchone()
            conn.close()
            if row:
                expiry_str = row[0]
                expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
                if expiry_date > datetime.now():
                    is_subscriber = True

        if is_subscriber:
            try:
                # Sukuriame vienkartinę pakvietimo nuorodą į grupę
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=TARGET_GROUP_ID,
                    member_limit=1,
                    expire_date=datetime.now() + timedelta(hours=24)
                )
                
                # Įrašome į aktyvių prenumeratų lentelę
                add_subscription(user.id, TARGET_GROUP_ID, days=30)
                
                await update.message.reply_text(
                    f"🎉 **Apmokėjimas sėkmingai rastas!**\n\n"
                    f"Štai jūsų asmeninė vienkartinė pakvietimo nuoroda į „Traliukai Europa“ grupę:\n{invite_link.invite_link}\n\n"
                    "📌 *Pastaba: Nuoroda galioja 24 valandas ir skirta tik jums.*",
                    parse_mode="Markdown"
                )
                return
            except Exception as e:
                logging.error(f"Klaida kuriant pakvietimo nuorodą start komandoje: {e}")
                await update.message.reply_text("⚠️ Įvyko techninė klaida generuojant nuorodą. Susisiekite su administratoriumi.")
                return

    # Jei neapmokėta – rodo įprastą kvietimą pirkti
    keyboard = [[InlineKeyboardButton("💳 Mokėti prenumeratą (30 d.)", url=STRIPE_PAYMENT_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Sveiki, **{user_name}**!\n\n"
        "🚚 **„Traliukai Europa“ – didžiausia tralų ir logistikos bendruomenė Lietuvoje!**\n\n"
        "Tai išskirtinė erdvė, kurioje susitinka savo srities profesionalai, patikimi vežėjai ir krovinių užsakovai. Prisijungę prie mūsų bendruomenės gaunate daugybę privalumų:\n\n"
        "• **Kiekvieną dieną daug krovinių:** Kasdien skelbiami nauji maršrutai, pasiūlymai ir laisvi kroviniai tiek Lietuvoje, tiek visoje Europoje.\n"
        "• **Patikimi vežėjai:** Dirbkite tik su saugomis, patikrintais ir atsakingais rinkos dalyviais.\n"
        "• **Daug naudingos informacijos:** Operatyvios rinkos naujienos, patarimai bei aktuali informacija transportuotojams.\n\n"
        "📌 **Kaip prisijungti:**\n"
        "1. Spauskite žemiau esantį mokėjimo mygtuką.\n"
        "2. Apm
