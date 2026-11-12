import os
import logging
import re
import json
import asyncio
from datetime import datetime, timedelta
import stripe
from google import genai
from aiohttp import web
import psycopg2
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

bot_app = None

# --- DUOMENŲ BAZĖS VALDYMAS (PostgreSQL) ---

def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id BIGINT PRIMARY KEY,
            chat_id BIGINT,
            expires_at TIMESTAMP,
            reminded INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id SERIAL PRIMARY KEY,
            telegram_username TEXT UNIQUE,
            email TEXT,
            expiry_date TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()

def add_subscription(user_id: int, chat_id: int, days: int = 30):
    conn = get_db_connection()
    cursor = conn.cursor()
    expires_at = datetime.now() + timedelta(days=days)
    cursor.execute("""
        INSERT INTO subscriptions (user_id, chat_id, expires_at, reminded)
        VALUES (%s, %s, %s, 0)
        ON CONFLICT (user_id) DO UPDATE SET
            expires_at = EXCLUDED.expires_at,
            reminded = 0
    """, (user_id, chat_id, expires_at))
    conn.commit()
    cursor.close()
    conn.close()

# --- START KOMANDOS APDOROJIMAS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_name = user.first_name or "Narys"
    username = user.username
    
    is_subscriber = False
    if username:
        clean_username = username.strip().lstrip("@").lower()
        if clean_username == "ela1238":
            is_subscriber = True
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT expiry_date FROM subscribers WHERE LOWER(telegram_username) = LOWER(%s)", (clean_username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row:
                expiry_date = row[0]
                if expiry_date > datetime.now():
                    is_subscriber = True

        if is_subscriber:
            try:
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=TARGET_GROUP_ID,
                    member_limit=1,
                    expire_date=datetime.now() + timedelta(hours=24)
                )
                
                add_subscription(user.id, TARGET_GROUP_ID, days=30)
                
                await update.message.reply_text(
                    f"🎉 Apmokėjimas sėkmingai rastas!\n\n"
                    f"Štai jūsų asmeninė vienkartinė pakvietimo nuoroda į „Traliukai Europa“ grupę:\n{invite_link.invite_link}\n\n"
                    "📌 Pastaba: Nuoroda galioja 24 valandas ir skirta tik jums."
                )
                return
            except Exception as e:
                logging.error(f"Klaida kuriant pakvietimo nuorodą start komandoje: {e}")
                await update.message.reply_text("⚠️ Įvyko techninė klaida generuojant nuorodą. Susisiekite su administratoriumi.")
                return

    keyboard = [[InlineKeyboardButton("💳 Mokėti prenumeratą (30 d.)", url=STRIPE_PAYMENT_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Sveiki, {user_name}!\n\n"
        "🚚 „Traliukai Europa“ – didžiausia tralų ir logistikos bendruomenė Lietuvoje!\n\n"
        "Tai išskirtinė erdvė, kurioje susitinka savo srities profesionalai, patikimi vežėjai ir krovinių užsakovai. Prisijungę prie mūsų bendruomenės gaunate daugybę privalumų:\n\n"
        "• Kiekvieną dieną daug krovinių: Kasdien skelbiami nauji maršrutai, pasiūlymai ir laisvi kroviniai tiek Lietuvoje, tiek visoje Europoje.\n"
        "• Patikimi vežėjai: Dirbkite tik su saugomis, patikrintais ir atsakingais rinkos dalyviais.\n"
        "• Daug naudingos informacijos: Operatyvios rinkos naujienos, patarimai bei aktuali informacija transportuotojams.\n\n"
        "📌 Kaip prisijungti:\n"
        "1. Spauskite žemiau esantį mokėjimo mygtuką.\n"
        "2. Apmokėjimo lange būtinai įrašykite savo Telegram slapyvardį (pvz., @jusu_vardas).\n"
        "3. Apmokėję sugrįžkite čia ir dar kartą paspauskite /start, kad gautumėte pakvietimo nuorodą!\n",
        reply_markup=reply_markup
    )

# --- GEMINI PAGALBINĖ FUNKCIJA ---

async def analyze_with_gemini(prompt: str) -> str:
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini API Klaida: {e}")
        return ""

# --- STRIPE WEBHOOK SERVERIS ---

async def stripe_webhook(request):
    try:
        payload = await request.text()
        event = json.loads(payload)
    except Exception as e:
        return web.Response(text=str(e), status=400)

    event_type = event.get("type")

    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        customer_email = session.get('customer_details', {}).get('email')
        
        telegram_username = None
        if session.get('custom_fields'):
            for field in session['custom_fields']:
                if field.get('type') == 'text':
                    telegram_username = field.get('text', {}).get('value')

        if telegram_username:
            telegram_username = telegram_username.strip().lstrip("@").lower()
            expiry_date = datetime.now() + timedelta(days=30)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO subscribers (telegram_username, email, expiry_date)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_username) DO UPDATE SET
                    email = EXCLUDED.email,
                    expiry_date = EXCLUDED.expiry_date
            """, (telegram_username, customer_email, expiry_date))
            conn.commit()
            cursor.close()
            conn.close()
            
            logging.info(f"✅ Sėkmingai aktyvuota narystė duomenų bazėje: @{telegram_username} ({customer_email})")

    return web.Response(text="Success", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_post('/webhook/stripe', stripe_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
    await site.start()
    logging.info(f"Stripe Webhook serveris paleistas ant prievado {WEBHOOK_PORT}")

# --- FONINIS PRIMINIMŲ PROCESAS ---

async def check_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, chat_id, expires_at, reminded FROM subscriptions")
    rows = cursor.fetchall()
    now = datetime.now()

    for user_id, chat_id, expires_at, reminded in rows:
        time_left = expires_at - now

        if timedelta(days=0) < time_left <= timedelta(days=3) and reminded == 0:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ Jūsų narystė baigsis už 3 dienų. Atnaujinkite prenumeratą."
                )
                cursor.execute("UPDATE subscriptions SET reminded = 1 WHERE user_id = %s", (user_id,))
                conn.commit()
            except Exception as e:
                logging.error(f"Klaida siunčiant priminimą {user_id}: {e}")

        elif time_left <= timedelta(days=0):
            try:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
                await context.bot.send_message(
                    chat_id=user_id,
                    text="❌ Jūsų narystės laikas pasibaigė, buvote pašalinti iš grupės."
                )
            except Exception as e:
                logging.error(f"Klaida šalinant vartotoją {user_id}: {e}")

            cursor.execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
            conn.commit()

    cursor.close()
    conn.close()

# --- GRUPĖS MODERAVIMAS ---

async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    chat_id = update.message.chat_id
    message_id = update.message.message_id
    user = update.message.from_user
    user_id = user.id
    user_name = user.first_name or "Narys"
    user_link = f"[{user_name}](tg://user?id={user_id})"

    if URL_PATTERN.search(text):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ {user_name}, išorinės nuorodos ir reklama grupėje yra draudžiamos!"
            )
            return
        except Exception as e:
            logging.error(f"Nepavyko ištrinti žinutės: {e}")

    prompt = f"""
    Išanalizuok šią grupės nario žinutę logistikos ir tralų grupėje: "{text}"
    Nustatyk jos tipą ir grąžink atsakymą TIK tokiu formatu:
    
    Jei žinutėje yra įžeidimų ar necenzūrinių žodžių, parašyk:
    STATUSAS: PAŽEIDIMAS
    
    Jei tai logistikos SKELBIMAS, parašyk:
    STATUSAS: SKELBIMAS
    FORMATAS:
    1️⃣ Iš kur -> į kur: [maršrutas lietuviškai]
    2️⃣ Krovinys: [koks krovinys]
    3️⃣ Būklė: [ar važiuojanti mašina, ar ne]
    4️⃣ Apmokėjimas: [ar apmokėta / kiek mokama]
    5️⃣ Kontaktai: [telefono numeris ar kontaktai]
    
    Jei tai paprastas pokalbis, parašyk:
    STATUSAS: PALIKTI
    """

    response_text = await analyze_with_gemini(prompt)

    if "STATUSAS: PAŽEIDIMAS" in response_text:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 {user_name}, žinutė pašalinta dėl nepagarbios kalbos."
            )
        except Exception as e:
            logging.error(f"Klaida trinant pažeidimą: {e}")
            
    elif "STATUSAS: SKELBIMAS" in response_text:
        try:
            formatted_part = response_text.split("FORMATAS:")[1].strip()
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📋 Skelbimas (įkėlė: {user_link}):\n\n{formatted_part}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Klaida automatiškai formatuojant skelbimą: {e}")

# --- MAIN FUNKCIJA ---

async def main():
    global bot_app
    init_db()
    
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), group_message_handler))

    if bot_app.job_queue:
        bot_app.job_queue.run_repeating(check_subscriptions, interval=43200, first=10)

    await bot_app.initialize()
    await bot_app.start()

    await start_web_server()

    print("🤖 Automatinis Stripe botas su PostgreSQL paleistas ir veikia!")
    
    await bot_app.updater.start_polling()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
