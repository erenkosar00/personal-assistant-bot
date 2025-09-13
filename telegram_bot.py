"""
Kişisel Telegram Asistan Botu - Görev, Not, Hatırlatıcı ve Hava Durumu
"""
import os
import logging
import sqlite3
import httpx
import pytz # <-- YENİ KÜTÜPHANE
from datetime import datetime
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Logging ayarları
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- GÜVENLİK VE YOL AYARI ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("Lütfen Railway'de TELEGRAM_TOKEN ortam değişkenini ayarlayın!")

DB_PATH = Path("assistant.db")

# --- SAAT DİLİMİ AYARI ---
TIMEZONE = pytz.timezone("Europe/Istanbul")

def setup_database():
    # ... (Bu fonksiyonun geri kalanı aynı, değişiklik yok) ...
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL,
        completed BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL,
        content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER,
        message TEXT NOT NULL, time TEXT NOT NULL, last_sent DATE, active BOOLEAN DEFAULT 1)
    ''')
    conn.commit()
    conn.close()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Kişisel Asistan Bot'a Hoş Geldiniz!\n/help yazarak komutları görebilirsiniz.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🆘 YARDIM MENÜSÜ

📋 Görev Yönetimi:
/gorev_ekle [görev metni]
/gorevler
/gorev_tamamla [görev ID]

📝 Not Yönetimi:
/not_ekle [başlık] [içerik]
/notlar
/not_sil [not ID]

🔔 Hatırlatıcılar:
/hatirlatici_ekle [saat] [mesaj]
/hatirlaticilar
/hatirlatici_sil [ID]
    """
    await update.message.reply_text(help_text)

# --- GÖREV FONKSİYONLARI (DEĞİŞİKLİK YOK) ---
async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("Kullanım: /gorev_ekle [görev metni]"); return
    title = ' '.join(context.args)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO tasks (user_id, title) VALUES (?, ?)', (user_id, title))
    task_id = cursor.lastrowid; conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Görev eklendi: '{title}' (ID: {task_id})")

async def list_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, title FROM tasks WHERE user_id = ? AND completed = 0 ORDER BY created_at DESC', (user_id,))
    tasks = cursor.fetchall(); conn.close()
    if not tasks: await update.message.reply_text("📭 Aktif göreviniz bulunmuyor!"); return
    message_text = "📋 Aktif Görevleriniz:\n\n"
    for task_id, title in tasks: message_text += f"▫️ {title} (ID: {task_id})\n"
    await update.message.reply_text(message_text)

async def complete_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("Kullanım: /gorev_tamamla [görev ID]"); return
    try: task_id = int(context.args[0])
    except ValueError: await update.message.reply_text("❌ Geçersiz ID! Sadece sayı girin."); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('UPDATE tasks SET completed = 1 WHERE id = ? AND user_id = ?', (task_id, user_id))
    changes = conn.total_changes; conn.commit(); conn.close()
    if changes > 0: await update.message.reply_text(f"🎉 ID {task_id} olan görev tamamlandı!")
    else: await update.message.reply_text("❌ Bu ID'ye sahip bir görev bulunamadı veya size ait değil.")

# --- NOT FONKSİYONLARI (DEĞİŞİKLİK YOK) ---
async def add_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2: await update.message.reply_text("Kullanım: /not_ekle [başlık] [içerik]"); return
    title = context.args[0]; content = ' '.join(context.args[1:])
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)', (user_id, title, content))
    note_id = cursor.lastrowid; conn.commit(); conn.close()
    await update.message.reply_text(f"📝 Not kaydedildi: '{title}' (ID: {note_id})")

async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, title FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT 10', (user_id,))
    notes = cursor.fetchall(); conn.close()
    if not notes: await update.message.reply_text("📭 Henüz notunuz bulunmuyor!"); return
    message_text = "📝 Son Notlarınız:\n\n"
    for note_id, title in notes: message_text += f"📌 {title} (ID: {note_id})\n"
    await update.message.reply_text(message_text)

async def delete_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("Kullanım: /not_sil [not ID]"); return
    try: note_id = int(context.args[0])
    except ValueError: await update.message.reply_text("❌ Geçersiz ID! Sadece sayı girin."); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    changes = conn.total_changes; conn.commit(); conn.close()
    if changes > 0: await update.message.reply_text(f"🗑️ ID {note_id} olan not silindi!")
    else: await update.message.reply_text("❌ Bu ID'ye sahip bir not bulunamadı veya size ait değil.")

# --- HATIRLATICI FONKSİYONLARI (DEĞİŞİKLİK YOK) ---
async def add_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id
    if len(context.args) < 2: await update.message.reply_text("Kullanım: /hatirlatici_ekle [saat] [mesaj]"); return
    time_str = context.args[0]; message = ' '.join(context.args[1:])
    try: hour, minute = map(int, time_str.split(':')); assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError): await update.message.reply_text("❌ Geçersiz saat formatı (HH:MM)!"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO reminders (user_id, chat_id, message, time) VALUES (?, ?, ?, ?)',(user_id, chat_id, message, time_str))
    r_id = cursor.lastrowid; conn.commit(); conn.close()
    await update.message.reply_text(f"🔔 Hatırlatıcı eklendi!\n⏰ {time_str} - {message}\n🆔 ID: {r_id}")

async def list_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, message, time FROM reminders WHERE user_id = ? AND active = 1 ORDER BY time', (user_id,))
    reminders = cursor.fetchall(); conn.close()
    if not reminders: await update.message.reply_text("📭 Aktif hatırlatıcınız yok!"); return
    message_text = "🔔 Aktif Hatırlatıcılarınız:\n\n"
    for r_id, msg, time_str in reminders: message_text += f"⏰ {time_str} - {msg} (ID: {r_id})\n"
    await update.message.reply_text(message_text)

async def delete_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("Kullanım: /hatirlatici_sil [ID]"); return
    try: r_id = int(context.args[0])
    except ValueError: await update.message.reply_text("❌ Geçersiz ID!"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('UPDATE reminders SET active = 0 WHERE id = ? AND user_id = ?', (r_id, user_id))
    changes = conn.total_changes; conn.commit(); conn.close()
    if changes > 0: await update.message.reply_text(f"🗑️ ID {r_id} olan hatırlatıcı silindi!")
    else: await update.message.reply_text("❌ Hatırlatıcı bulunamadı!")

# --- SAAT DİLİMİNE GÖRE GÜNCELLENMİŞ FONKSİYON ---
async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    """Her dakika çalışıp hatırlatıcıları Türkiye saatine göre kontrol eden görev."""
    now = datetime.now(TIMEZONE) # <-- DEĞİŞİKLİK BURADA
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, chat_id, message FROM reminders WHERE active = 1 AND time = ? AND (last_sent != ? OR last_sent IS NULL)', (current_time, current_date))
    reminders = cursor.fetchall()
    for r_id, chat_id, message in reminders:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"🔔 HATIRLATICI\n\n{message}")
            cursor.execute('UPDATE reminders SET last_sent = ? WHERE id = ?', (current_date, r_id)); conn.commit()
            logger.info(f"Hatırlatıcı gönderildi: ID {r_id}")
        except Exception as e: logger.error(f"Hatırlatıcı ID {r_id} gönderilemedi: {e}")
    conn.close()

def main() -> None:
    """Botu başlatır ve çalışır halde tutar."""
    setup_database()
    application = Application.builder().token(TOKEN).build()
    application.add_handlers([
        CommandHandler("start", start_command), CommandHandler("help", help_command),
        CommandHandler("gorev_ekle", add_task_command), CommandHandler("gorevler", list_tasks_command),
        CommandHandler("gorev_tamamla", complete_task_command),
        CommandHandler("not_ekle", add_note_command), CommandHandler("notlar", list_notes_command),
        CommandHandler("not_sil", delete_note_command),
        CommandHandler("hatirlatici_ekle", add_reminder_command), CommandHandler("hatirlaticilar", list_reminders_command),
        CommandHandler("hatirlatici_sil", delete_reminder_command)
    ])
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)
    logger.info("Bot başlatıldı, yeni mesajlar bekleniyor...")
    application.run_polling()

if __name__ == "__main__":
    main()
