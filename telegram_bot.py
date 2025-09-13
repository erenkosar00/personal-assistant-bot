"""
Kişisel Telegram Asistan Botu v3.1 - Kararlı Sürüm
"""
import os
import logging
import sqlite3
import pytz
import google.generativeai as genai
import dateparser
from datetime import datetime
from pathlib import Path
from telegram import Update, constants
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
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, reminder_time TIMESTAMP, status TEXT DEFAULT "active")')
    conn.commit(); conn.close()

# --- GEMINI ARAÇLARI (ALETLER) ---
def set_reminder(user_id: int, chat_id: int, time_string: str, message: str) -> str:
    """Kullanıcı için belirtilen zamanda bir hatırlatıcı kurar."""
    parsed_time = dateparser.parse(time_string, settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'Europe/Istanbul'})
    if not parsed_time: return "Zamanı anlayamadım. 'yarın 15:30' gibi bir ifade kullan."

    reminder_time_utc = parsed_time.astimezone(pytz.utc)

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO reminders (user_id, chat_id, message, reminder_time) VALUES (?, ?, ?, ?)', (user_id, chat_id, message, reminder_time_utc))
    conn.commit(); conn.close()

    formatted_time = parsed_time.strftime('%d %B %Y, Saat %H:%M')
    logger.info(f"Hatırlatıcı kuruldu: {message} -> {formatted_time} (Kullanıcı: {user_id})")
    return f"Tamamdır, '{formatted_time}' için '{message}' hatırlatıcısını kurdum."

# --- GEMINI AYARLARI ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(model_name='gemini-1.5-flash-latest', tools=[set_reminder])
chat_sessions = {} # Hafıza için

# --- TELEGRAM FONKSİYONLARI ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Merhaba! Ben senin kişisel asistanınım. Ne istediğini söylemen yeterli.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id; user_text = update.message.text
    if user_id not in chat_sessions: chat_sessions[user_id] = gemini_model.start_chat()
    chat = chat_sessions[user_id]

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        response = await chat.send_message_async(user_text)

        if response.candidates[0].content.parts[0].function_call:
            fc = response.candidates[0].content.parts[0].function_call
            if fc.name == "set_reminder":
                tool_args = {key: value for key, value in fc.args.items()}
                tool_args['user_id'] = user_id
                tool_args['chat_id'] = chat_id
                tool_response = set_reminder(**tool_args)

                # Gemini'ye aletin sonucunu gönderip nihai cevabı al
                response = await chat.send_message_async(
                    # DÜZELTME: Artık genai.Part kullanmıyoruz
                    [genai.types.FunctionResponse(name=fc.name, response={'result': tool_response})]
                )

        await update.message.reply_text(response.text)
    except Exception as e:
        logger.error(f"İşlem hatası (kullanıcı: {user_id}): {e}")
        await update.message.reply_text("🤖 Üzgünüm, bir sorunla karşılaştım.")

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(pytz.utc)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, message FROM reminders WHERE status = 'active' AND reminder_time <= ?", (now_utc,))
    reminders = cursor.fetchall()
    for r_id, chat_id, message in reminders:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"🔔 HATIRLATICI\n\n{message}")
            cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,)); conn.commit()
            logger.info(f"Hatırlatıcı gönderildi: ID {r_id}")
        except Exception as e:
            logger.error(f"Hatırlatıcı ID {r_id} gönderilemedi: {e}")
    conn.close()

def main() -> None:
    setup_database()
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Botun son kararlı versiyonu başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()
