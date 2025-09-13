"""
Kişisel Telegram Asistan Botu - Basit Hatırlatıcı Sistemi
24/7 çalışan akıllı asistan
"""
import os
import logging
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Logging ayarları
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- GÜVENLİK VE YOL DÜZELTMESİ ---
# Token'ı ASLA koda yazmıyoruz, Railway'deki "Variables" kısmından alıyoruz.
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("Lütfen Railway'de TELEGRAM_TOKEN ortam değişkenini ayarlayın!")

# Veritabanı yolunu projenin kendi klasörü olarak ayarlıyoruz.
# Path.home() KULLANMIYORUZ!
DB_PATH = Path("assistant.db")

def setup_database():
    """Veritabanı kurulumu"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Hatırlatıcılar tablosu
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            chat_id INTEGER,
            message TEXT NOT NULL,
            time TEXT NOT NULL,
            last_sent DATE,
            active BOOLEAN DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot başlatma komutu"""
    welcome_message = """
🤖 Kişisel Asistan Bot'a Hoş Geldiniz!
/help yazarak tüm komutları görebilirsiniz.
    """
    await update.message.reply_text(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yardım komutu"""
    help_text = """
🆘 YARDIM MENÜSÜ
/hatirlatici_ekle [saat] [mesaj] - Günlük hatırlatıcı ekler.
/hatirlaticilar - Aktif hatırlatıcıları listeler.
/hatirlatici_sil [ID] - Bir hatırlatıcıyı siler.

Örnek: /hatirlatici_ekle 09:00 Su iç ve egzersiz yap
    """
    await update.message.reply_text(help_text)

async def add_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hatırlatıcı ekleme komutu"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: /hatirlatici_ekle [saat] [mesaj]\nÖrnek: /hatirlatici_ekle 09:30 Toplantı")
        return
    
    time_str = context.args[0]
    message = ' '.join(context.args[1:])
    
    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Geçersiz saat formatı! Lütfen HH:MM formatında girin (Örnek: 09:30).")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO reminders (user_id, chat_id, message, time) VALUES (?, ?, ?, ?)',
        (user_id, chat_id, message, time_str)
    )
    reminder_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"🔔 Hatırlatıcı eklendi!\n⏰ {time_str} - {message}\n🆔 ID: {reminder_id}")

async def list_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hatırlatıcıları listeleme"""
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, message, time FROM reminders WHERE user_id = ? AND active = 1 ORDER BY time',
        (user_id,)
    )
    reminders = cursor.fetchall()
    conn.close()
    
    if not reminders:
        await update.message.reply_text("📭 Aktif hatırlatıcınız bulunmuyor!")
        return
    
    message_text = "🔔 Aktif Hatırlatıcılarınız:\n\n"
    for r_id, msg, time_str in reminders:
        message_text += f"⏰ {time_str} - {msg} (ID: {r_id})\n"
    
    await update.message.reply_text(message_text)

async def delete_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hatırlatıcı silme"""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Kullanım: /hatirlatici_sil [hatırlatıcı ID]")
        return
    
    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz ID! Sadece sayı girin.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE reminders SET active = 0 WHERE id = ? AND user_id = ?',
        (reminder_id, user_id)
    )
    changes = conn.total_changes
    conn.commit()
    conn.close()
    
    if changes > 0:
        await update.message.reply_text(f"🗑️ ID {reminder_id} olan hatırlatıcı silindi!")
    else:
        await update.message.reply_text("❌ Bu ID'ye sahip bir hatırlatıcı bulunamadı veya size ait değil.")

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    """Her dakika çalışıp hatırlatıcıları kontrol eden görev."""
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, chat_id, message FROM reminders WHERE active = 1 AND time = ? AND (last_sent != ? OR last_sent IS NULL)',
        (current_time, current_date)
    )
    reminders_to_send = cursor.fetchall()

    for r_id, chat_id, message in reminders_to_send:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"🔔 HATIRLATICI\n\n{message}")
            cursor.execute('UPDATE reminders SET last_sent = ? WHERE id = ?', (current_date, r_id))
            conn.commit()
            logger.info(f"Hatırlatıcı gönderildi: ID {r_id}")
        except Exception as e:
            logger.error(f"Hatırlatıcı ID {r_id} gönderilemedi: {e}")
            
    conn.close()

def main() -> None:
    """Botu başlatır ve çalışır halde tutar."""
    setup_database()
    
    application = Application.builder().token(TOKEN).build()
    
    # Komut handler'ları
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("hatirlatici_ekle", add_reminder_command))
    application.add_handler(CommandHandler("hatirlaticilar", list_reminders_command))
    application.add_handler(CommandHandler("hatirlatici_sil", delete_reminder_command))
    
    # Hatırlatıcı görevini zamanlayıcıya ekle
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10) # Her 60 saniyede bir çalıştır
    
    logger.info("Bot başlatıldı, yeni mesajlar bekleniyor...")
    application.run_polling()

if __name__ == "__main__":
    main()
