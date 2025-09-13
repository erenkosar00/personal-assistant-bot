"""
Kişisel Telegram Asistan Botu - İnteraktif Butonlu
"""
import os
import logging
import sqlite3
import httpx
import pytz
import google.generativeai as genai
from datetime import datetime
from pathlib import Path
# --- YENİ --- Butonlar için gerekli modülleri ekliyoruz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
# --- YENİ --- Butonları dinlemek için CallbackQueryHandler'ı ekliyoruz
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Logging ayarları
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- API ANAHTARLARI VE AYARLAR ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not TOKEN: raise ValueError("TELEGRAM_TOKEN ortam değişkeni ayarlanmadı!")
if not GEMINI_API_KEY: raise ValueError("GEMINI_API_KEY ortam değişkeni ayarlanmadı!")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
DB_PATH = Path("assistant.db")
TIMEZONE = pytz.timezone("Europe/Istanbul")

def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, completed BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, time TEXT NOT NULL, last_sent DATE, active BOOLEAN DEFAULT 1)')
    conn.commit()
    conn.close()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Merhaba! Ben sizin kişisel asistanınızım.\n/help yazarak komutları görebilirsiniz.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🆘 YARDIM MENÜSÜ

📋 Görev Yönetimi:
/gorev_ekle [görev metni]
/gorevler (Artık butonlu!)

📝 Not Yönetimi:
/not_ekle [başlık] [içerik]
/notlar
/not_sil [not ID]

🔔 Hatırlatıcılar:
/hatirlatici_ekle [saat] [mesaj]
/hatirlaticilar
/hatirlatici_sil [ID]

🤖 Yapay Zeka:
Komut kullanmadan herhangi bir şey yazarak benimle sohbet edebilirsiniz!
    """
    await update.message.reply_text(help_text)

# --- GÖREV FONKSİYONLARI (GÜNCELLENDİ) ---
async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id;
    if not context.args: await update.message.reply_text("Kullanım: /gorev_ekle [görev metni]"); return
    title=' '.join(context.args); conn=sqlite3.connect(DB_PATH); cursor=conn.cursor()
    cursor.execute('INSERT INTO tasks (user_id, title) VALUES (?, ?)',(user_id, title))
    task_id=cursor.lastrowid;conn.commit();conn.close()
    await update.message.reply_text(f"✅ Görev eklendi: '{title}' (ID: {task_id})")

async def list_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, title FROM tasks WHERE user_id = ? AND completed = 0 ORDER BY created_at DESC', (user_id,))
    tasks = cursor.fetchall(); conn.close()

    if not tasks:
        await update.message.reply_text("📭 Aktif göreviniz bulunmuyor!")
        return

    message_text = "📋 Aktif Görevleriniz (Tamamlamak için butona basın):\n\n"
    keyboard = []
    for task_id, title in tasks:
        # Her görev için bir buton oluşturuyoruz.
        # callback_data, butona tıklandığında bota gönderilecek gizli veridir.
        # Hangi görevin tamamlanacağını bu veri sayesinde anlıyoruz.
        button = InlineKeyboardButton(f"✅ {title}", callback_data=f"complete_task:{task_id}")
        keyboard.append([button])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(message_text, reply_markup=reply_markup)

# --- YENİ BUTON YÖNETİCİSİ ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buton tıklamalarını yönetir."""
    query = update.callback_query
    await query.answer() # Butona basıldığında Telegram'a "tamam aldım" der.

    data = query.data

    # Gelen veriyi ayır: 'complete_task:12' -> 'complete_task', '12'
    action, value = data.split(':')

    if action == "complete_task":
        task_id = int(value)
        user_id = query.from_user.id

        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute('UPDATE tasks SET completed = 1 WHERE id = ? AND user_id = ?', (task_id, user_id))
        conn.commit(); conn.close()

        # Butonların olduğu orijinal mesajı düzenleyerek kullanıcıya geri bildirim veriyoruz.
        await query.edit_message_text(text=f"🎉 Görev (ID: {task_id}) başarıyla tamamlandı!")

# --- DİĞER TÜM FONKSİYONLAR (DEĞİŞİKLİK YOK) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def add_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def delete_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def add_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def list_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def delete_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE): ...
async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE): ...

def main() -> None:
    setup_database()
    application = Application.builder().token(TOKEN).build()

    handlers = [
        CommandHandler("start", start_command), CommandHandler("help", help_command),
        CommandHandler("gorev_ekle", add_task_command), CommandHandler("gorevler", list_tasks_command),
        # /gorev_tamamla komutunu sildik.
        CommandHandler("not_ekle", add_note_command), CommandHandler("notlar", list_notes_command),
        CommandHandler("not_sil", delete_note_command),
        CommandHandler("hatirlatici_ekle", add_reminder_command), CommandHandler("hatirlaticilar", list_reminders_command),
        CommandHandler("hatirlatici_sil", delete_reminder_command),
        # --- YENİ --- Butonları dinlemesi için bu handler'ı ekliyoruz.
        CallbackQueryHandler(button_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    ]
    application.add_handlers(handlers)

    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Bot başlatıldı, interaktif butonlar aktif!")
    application.run_polling()

if __name__ == "__main__":
    main()
