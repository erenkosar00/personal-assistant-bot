"""
Kişisel Telegram Asistan Botu v4.0 - Google Calendar Entegrasyonlu
"""
import os
import logging
import sqlite3
import pytz
import google.generativeai as genai
import dateparser
import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- AYARLAR ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_BASE64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64")

if not all([TOKEN, GEMINI_API_KEY, GOOGLE_CALENDAR_ID, GOOGLE_CREDENTIALS_BASE64]):
    raise ValueError("Gerekli tüm ortam değişkenleri ayarlanmalıdır!")

# --- GOOGLE AYARLARI ---
try:
    creds_json_str = base64.b64decode(GOOGLE_CREDENTIALS_BASE64).decode('utf-8')
    creds_json = json.loads(creds_json_str)
    SCOPES = ['https://www.googleapis.com/auth/calendar']
    GOOGLE_CREDS = service_account.Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    calendar_service = build('calendar', 'v3', credentials=GOOGLE_CREDS)
except Exception as e:
    logger.error(f"Google Credentials yüklenemedi: {e}")
    calendar_service = None

# --- GEMINI AYARLARI ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
chat_sessions = {}

# --- TELEGRAM FONKSİYONLARI ---
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Asistanı başlatır"),
        BotCommand("hatirlat", "Google Takvim'e hatırlatıcı ekler (Örn: /hatirlat yarın 10da toplantı)"),
        BotCommand("yeni_sohbet", "Yapay zeka sohbet geçmişini sıfırlar"),
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Merhaba! Ben Google Takvim ile entegre kişisel asistanınım. Ne istediğini söylemen yeterli.")

async def set_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Lütfen hatırlatıcı için bir zaman ve mesaj belirt.\nÖrnek: ")
        return

    full_text = " ".join(context.args)
    parsed_time = dateparser.parse(full_text, settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'Europe/Istanbul'})

    if not parsed_time:
        await update.message.reply_text("Üzgünüm, belirttiğin zamanı anlayamadım. Lütfen 'yarın 15:30' veya '2 saat sonra' gibi bir ifade kullan.")
        return

    # Basit bir mantıkla, ilk zaman ifadesinden sonrasını mesaj olarak alalım
    # Örneğin "yarın 10da toplantı" -> "toplantı"
    # Bu kısım daha da geliştirilebilir.
    message = full_text

    # Google Takvim için etkinliği oluştur
    event = {
        'summary': message,
        'start': {'dateTime': parsed_time.isoformat(), 'timeZone': 'Europe/Istanbul'},
        'end': {'dateTime': (parsed_time + timedelta(hours=1)).isoformat(), 'timeZone': 'Europe/Istanbul'},
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]},
    }
    try:
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        formatted_time = parsed_time.strftime('%d %B %Y, Saat %H:%M')
        await update.message.reply_text(f"✅ Google Takvimine eklendi!\n\n🗓️ Etkinlik: {message}\n⏰ Zaman: {formatted_time}")
    except Exception as e:
        logger.error(f"Google Calendar API hatası: {e}")
        await update.message.reply_text("Takvimine etkinlik eklerken bir sorun oluştu. Google Cloud ayarlarını kontrol et.")

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

def main() -> None:
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("hatirlat", set_reminder_command))
    application.add_handler(CommandHandler("yeni_sohbet", new_chat_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Botun Google Takvim entegrasyonlu son versiyonu başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()
