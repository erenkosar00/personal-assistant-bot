"""
Kişisel Telegram Asistan Botu v4.0 - Kararlı ve Akıllı Komutlar
"""
import os
import logging
import sqlite3
import pytz
import google.generativeai as genai
import dateparser
from datetime import datetime
from pathlib import Path
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- AYARLAR ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not TOKEN: raise ValueError("TELEGRAM_TOKEN ayarlanmadı!")
if not GEMINI_API_KEY: raise ValueError("GEMINI_API_KEY ayarlanmadı!")

DB_PATH = Path("assistant.db")
TIMEZONE = pytz.timezone("Europe/Istanbul")

# --- VERİTABANI ---
def setup_database():
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, reminder_time TEXT, status TEXT DEFAULT "active")')
    conn.commit(); conn.close()

# --- GEMINI AYARLARI ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
chat_sessions = {}

# --- TELEGRAM FONKSİYONLARI ---
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Asistanı başlatır"),
        BotCommand("hatirlat", "Yeni bir hatırlatıcı kurar (Örn: /hatirlat yarın 10da toplantı)"),
        BotCommand("hatirlaticilar", "Aktif hatırlatıcıları listeler"),
        BotCommand("yeni_sohbet", "Yapay zeka sohbet geçmişini sıfırlar"),
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Merhaba! Ben senin kişisel asistanınım. /hatirlat komutuyla veya serbest sohbetle başlayabilirsin.")

async def set_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Lütfen hatırlatıcı için bir zaman ve mesaj belirtin.\nÖrnek: ")
        return

    full_text = " ".join(context.args)
    parsed_time = dateparser.parse(full_text, settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'Europe/Istanbul'})

    if not parsed_time:
        await update.message.reply_text("Üzgünüm, belirttiğin zamanı anlayamadım. Lütfen 'yarın 15:30' veya '2 saat sonra' gibi bir ifade kullan.")
        return

    # Mesajı, ayrıştırılan tarihten sonra kalan kısım olarak al
    # Bu kısım biraz karmaşık olabilir, şimdilik tüm metni mesaj olarak alalım.
    message = full_text

    reminder_time_utc_str = parsed_time.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO reminders (user_id, chat_id, message, reminder_time) VALUES (?, ?, ?, ?)', 
                   (user_id, chat_id, message, reminder_time_utc_str))
    conn.commit(); conn.close()

    formatted_time = parsed_time.strftime('%d %B %Y, Saat %H:%M')
    await update.message.reply_text(f"✅ Anlaşıldı! '{formatted_time}' için hatırlatıcı kuruldu.")

async def list_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Bu fonksiyon şimdilik basit tutuldu, daha sonra butonlar eklenebilir.
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, message, reminder_time FROM reminders WHERE user_id = ? AND status = 'active' ORDER BY reminder_time", (user_id,))
    reminders = cursor.fetchall(); conn.close()

    if not reminders:
        await update.message.reply_text("Aktif hatırlatıcın bulunmuyor.")
        return

    message_text = "🔔 Aktif Hatırlatıcıların:\n\n"
    for r_id, msg, time_str in reminders:
        # UTC'den yerel saate çevir
        reminder_time_local = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.utc).astimezone(TIMEZONE)
        formatted_time = reminder_time_local.strftime('%d %b, %H:%M')
        message_text += f"▫️ {msg} ({formatted_time})\n"

    await update.message.reply_text(message_text)

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in chat_sessions: del chat_sessions[user_id]
    await update.message.reply_text("🤖 Sohbet geçmişi temizlendi.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; user_text = update.message.text
    if user_id not in chat_sessions: chat_sessions[user_id] = gemini_model.start_chat()
    chat = chat_sessions[user_id]
    try:
        response = await chat.send_message_async(user_text)
        await update.message.reply_text(response.text)
    except Exception as e:
        logger.error(f"Sohbet hatası: {e}")
        await update.message.reply_text("🤖 Üzgünüm, bir sorunla karşılaştım.")

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    now_utc_str = datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, message FROM reminders WHERE status = 'active' AND reminder_time <= ?", (now_utc_str,))
    reminders = cursor.fetchall()
    for r_id, chat_id, message in reminders:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"🔔 HATIRLATICI\n\n{message}")
            cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,)); conn.commit()
        except Exception as e:
            logger.error(f"Hatırlatıcı ID {r_id} gönderilemedi: {e}")
    conn.close()

def main() -> None:
    setup_database()
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("hatirlat", set_reminder_command))
    application.add_handler(CommandHandler("hatirlaticilar", list_reminders_command))
    application.add_handler(CommandHandler("yeni_sohbet", new_chat_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Botun kararlı ve akıllı komut versiyonu başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()
