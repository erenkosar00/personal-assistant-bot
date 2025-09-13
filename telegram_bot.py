"""
Kişisel Telegram Asistan Botu - Gemini AI Entegrasyonlu
"""
import os
import logging
import sqlite3
import httpx
import pytz
import google.generativeai as genai # <-- YENİ GEMINI KÜTÜPHANESİ
from datetime import datetime
from pathlib import Path
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Logging ayarları
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- API ANAHTARLARI VE AYARLAR ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TOKEN: raise ValueError("TELEGRAM_TOKEN ortam değişkeni ayarlanmadı!")
if not GEMINI_API_KEY: raise ValueError("GEMINI_API_KEY ortam değişkeni ayarlanmadı!")

# Gemini API'ı yapılandır
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-pro')

DB_PATH = Path("assistant.db")
TIMEZONE = pytz.timezone("Europe/Istanbul")

# --- VERİTABANI KURULUMU ---
def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, completed BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, time TEXT NOT NULL, last_sent DATE, active BOOLEAN DEFAULT 1)')
    conn.commit()
    conn.close()

# --- GENEL KOMUTLAR ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_text = "🤖 Merhaba! Ben sizin kişisel asistanınızım.\n\n"                  "Görevlerinizi, notlarınızı ve hatırlatıcılarınızı yönetebilirim. "                  "Ayrıca benimle serbestçe sohbet edebilirsiniz!\n\n"                  "/help yazarak tüm komutları görebilirsiniz."
    await update.message.reply_text(start_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = "🆘 YARDIM MENÜSÜ\n\n"                 "📋 Görevler:\n/gorev_ekle, /gorevler, /gorev_tamamla\n\n"                 "📝 Notlar:\n/not_ekle, /notlar, /not_sil\n\n"                 "🔔 Hatırlatıcılar:\n/hatirlatici_ekle, /hatirlaticilar, /hatirlatici_sil\n\n"                 "🤖 Yapay Zeka:\nKomut kullanmadan herhangi bir şey yazarak benimle sohbet edebilirsiniz!"
    await update.message.reply_text(help_text)

# --- GEMINI SOHBET FONKSİYONU ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Komut olmayan tüm metin mesajlarını işler ve Gemini'a gönderir."""
    user_text = update.message.text
    chat_id = update.effective_chat.id

    try:
        # Kullanıcıya "düşünüyorum..." aksiyonu göster
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

        # Gemini'dan asenkron olarak cevap al
        response = await gemini_model.generate_content_async(user_text)

        # Cevabı kullanıcıya gönder
        await update.message.reply_text(response.text)

    except Exception as e:
        logger.error(f"Gemini API hatası: {e}")
        await update.message.reply_text("🤖 Üzgünüm, şu an yapay zeka modülümde bir sorun var. Lütfen daha sonra tekrar deneyin.")

# --- DİĞER FONKSİYONLAR (GÖREV, NOT, HATIRLATICI - DEĞİŞİKLİK YOK) ---
# (Burada önceki kodumuzdaki görev, not ve hatırlatıcı fonksiyonları yer alıyor)
async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def list_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def complete_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def add_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def delete_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def add_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def list_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def delete_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.effective_user.id; ...
async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE): ...

def main() -> None:
    """Botu başlatır ve çalışır halde tutar."""
    setup_database()
    application = Application.builder().token(TOKEN).build()

    # Komutları ve yeni sohbet handler'ını ekle
    handlers = [
        CommandHandler("start", start_command), CommandHandler("help", help_command),
        CommandHandler("gorev_ekle", add_task_command), CommandHandler("gorevler", list_tasks_command),
        CommandHandler("gorev_tamamla", complete_task_command),
        CommandHandler("not_ekle", add_note_command), CommandHandler("notlar", list_notes_command),
        CommandHandler("not_sil", delete_note_command),
        CommandHandler("hatirlatici_ekle", add_reminder_command), CommandHandler("hatirlaticilar", list_reminders_command),
        CommandHandler("hatirlatici_sil", delete_reminder_command),
        # --- YENİ --- Sohbet handler'ı en sona eklenir
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    ]
    application.add_handlers(handlers)

    # JobQueue
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Bot başlatıldı, Gemini yetenekleri aktif!")
    application.run_polling()

if __name__ == "__main__":
    main()
