"""
Kişisel Telegram Asistan Botu v2.0
- İnteraktif Butonlar & Komut Menüsü
- Kapsamlı Kullanım Rehberi (/nasıl)
- Gemini AI Sohbet & Kalıcı Hafıza
"""
import os
import logging
import sqlite3
import pytz
import google.generativeai as genai
from datetime import datetime
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, constants
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

# Her kullanıcı için sohbet oturumlarını saklayacak sözlük
chat_sessions = {}

DB_PATH = Path("assistant.db")
TIMEZONE = pytz.timezone("Europe/Istanbul")

def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, completed BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, time TEXT NOT NULL, last_sent DATE, active BOOLEAN DEFAULT 1)')
    cursor.execute('CREATE TABLE IF NOT EXISTS memories (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key))')
    conn.commit()
    conn.close()

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Asistanı başlatır"),
        BotCommand("help", "Hızlı yardım menüsünü gösterir"),
        BotCommand("nasıl", "Detaylı kullanım kılavuzunu gösterir"),
        BotCommand("yeni_sohbet", "Yapay zeka sohbet geçmişini sıfırlar"),
        BotCommand("gorev_ekle", "Yeni bir görev ekler"),
        BotCommand("gorevler", "Aktif görevleri butonlarla listeler"),
        BotCommand("not_ekle", "Yeni bir not ekler"),
        BotCommand("notlar", "Tüm notları butonlarla listeler"),
        BotCommand("hatirlatici_ekle", "Yeni bir hatırlatıcı kurar"),
        BotCommand("hatirlaticilar", "Tüm hatırlatıcıları butonlarla listeler"),
        BotCommand("unutma", "Bota kalıcı bir bilgi öğretir"),
        BotCommand("hatirla", "Bota öğrettiğiniz bir bilgiyi sorar"),
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_text = ("🤖 Merhaba! Ben sizin kişisel asistanınızım.\n\n"
                  "Artık sohbetlerimizi hatırlayabiliyorum. Görevlerinizi, notlarınızı ve hatırlatıcılarınızı yönetebilirim. "
                  "Sohbet çubuğundaki / menüsünden tüm komutları görebilirsiniz.")
    await update.message.reply_text(start_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = ("🆘 HIZLI YARDIM\n\n"
                 "▫️ /gorevler, /notlar, /hatirlaticilar ile listeleme yap.\n"
                 "▫️ /gorev_ekle, /not_ekle, /hatirlatici_ekle ile ekleme yap.\n"
                 "▫️ /unutma [anahtar] [bilgi] ile bana bir şey öğret.\n"
                 "▫️ /hatirla [anahtar] ile öğrettiğin şeyi sor.\n"
                 "▫️ /yeni_sohbet ile sohbet hafızamı sıfırla.\n"
                 "▫️ Detaylı rehber için /nasıl yaz.")
    await update.message.reply_text(help_text)

async def nasil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guide_text = """
🤖 **Asistanını Nasıl Kullanırsın?**

Merhaba! İşte beni nasıl verimli kullanabileceğinle ilgili detaylı rehber:

🧠 **Yapay Zeka ile Sohbet**
Benimle herhangi bir konuda, komut kullanmadan sohbet edebilirsin. Sohbetlerimizi hatırlarım! Eğer sohbet karışırsa  komutuyla hafızamı sıfırlayabilirsin.

💾 **Kalıcı Hafıza**
Bana kalıcı bilgiler öğretebilirsin.
• 
• 
Daha sonra sohbet içinde bu bilgileri kullanırım veya  komutuyla öğrettiğin bilgiyi sana söylerim.

📋 **Görev Yönetimi**
• **Ekle:** 
• **Listele & Tamamla:**  komutuyla görevlerini listele ve çıkan ✅ butonlarına basarak tamamla.

📝 **Not Alma**
• **Ekle:** 
• **Listele & Sil:**  komutuyla notlarını listele ve çıkan 🗑️ butonlarına basarak sil.

🔔 **Hatırlatıcılar**
• **Kur:**  (HH:MM formatında)
• **Listele & Sil:**  komutuyla hatırlatıcılarını listele ve 🗑️ butonlarıyla sil.
    """
    await update.message.reply_text(guide_text, parse_mode='Markdown')

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in chat_sessions:
        del chat_sessions[user_id]
        await update.message.reply_text("🤖 Sohbet geçmişiniz temizlendi.")
    else:
        await update.message.reply_text("🤖 Zaten yeni bir sohbetteyiz.")

async def unutma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2: await update.message.reply_text("Kullanım: /unutma [anahtar] [kaydedilecek bilgi]"); return
    key = context.args[0].lower(); value = ' '.join(context.args[1:])
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO memories (user_id, key, value) VALUES (?, ?, ?)', (user_id, key, value))
    conn.commit(); conn.close()
    await update.message.reply_text(f"🧠 Hafızama kaydettim: {key} = {value}")

async def hatirla_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("Kullanım: /hatirla [anahtar]"); return
    key = context.args[0].lower()
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT value FROM memories WHERE user_id = ? AND key = ?', (user_id, key))
    result = cursor.fetchone(); conn.close()
    if result: await update.message.reply_text(f"🧠 Hatırlıyorum: {key} = {result[0]}")
    else: await update.message.reply_text(f"🤔 '{key}' hakkında bir şey hatırlamıyorum.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; user_text = update.message.text
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT key, value FROM memories WHERE user_id = ?', (user_id,))
    memories = cursor.fetchall(); conn.close()

    long_term_memory_context = "\n".join([f"- {key}: {value}" for key, value in memories])
    system_prompt = f"Sen, sahibinin kişisel bir asistanısın. Sahibin hakkında bilmen gereken bazı özel bilgiler şunlar:\n{long_term_memory_context}\n\nBu bilgileri kullanarak kısa ve samimi cevaplar ver."

    if user_id not in chat_sessions:
        logger.info(f"Kullanıcı {user_id} için yeni sohbet oturumu oluşturuluyor.")
        chat_sessions[user_id] = gemini_model.start_chat(history=[
            {'role': 'user', 'parts': [system_prompt]},
            {'role': 'model', 'parts': ["Anlaşıldı. Sahibim hakkında bu bilgileri hatırlayacağım ve ona göre davranacağım."]}
        ])

    chat_session = chat_sessions[user_id]

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)
        response = await chat_session.send_message_async(user_text)
        await update.message.reply_text(response.text)
    except Exception as e:
        logger.error(f"Gemini API hatası (kullanıcı: {user_id}): {e}")
        await update.message.reply_text("🤖 Üzgünüm, yapay zeka modülümde bir sorun oluştu. Lütfen /yeni_sohbet komutuyla hafızamı sıfırlayın.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action, value = query.data.split(':'); item_id = int(value); user_id = query.from_user.id
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    if action == "complete_task":
        cursor.execute('UPDATE tasks SET completed = 1 WHERE id = ? AND user_id = ?', (item_id, user_id))
        await query.edit_message_text(text=f"🎉 Görev (ID: {item_id}) başarıyla tamamlandı!")
    elif action == "delete_note":
        cursor.execute('DELETE FROM notes WHERE id = ? AND user_id = ?', (item_id, user_id))
        await query.edit_message_text(text=f"🗑️ Not (ID: {item_id}) başarıyla silindi!")
    elif action == "delete_reminder":
        cursor.execute('UPDATE reminders SET active = 0 WHERE id = ? AND user_id = ?', (item_id, user_id))
        await query.edit_message_text(text=f"🗑️ Hatırlatıcı (ID: {item_id}) başarıyla silindi!")
    conn.commit(); conn.close()

async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("Kullanım: /gorev_ekle [görev metni]"); return
    title = ' '.join(context.args); conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO tasks (user_id, title) VALUES (?, ?)', (user_id, title))
    task_id = cursor.lastrowid; conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Görev eklendi: '{title}' (ID: {task_id})")

async def list_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, title FROM tasks WHERE user_id = ? AND completed = 0 ORDER BY created_at DESC', (user_id,))
    items = cursor.fetchall(); conn.close()
    if not items: await update.message.reply_text("📭 Aktif göreviniz bulunmuyor!"); return
    keyboard = [[InlineKeyboardButton(f"✅ {title}", callback_data=f"complete_task:{item_id}")] for item_id, title in items]
    await update.message.reply_text("📋 Aktif Görevleriniz (Tamamlamak için butona basın):", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2: await update.message.reply_text("Kullanım: /not_ekle [başlık] [içerik]"); return
    title = context.args[0]; content = ' '.join(context.args[1:])
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)', (user_id, title, content))
    note_id = cursor.lastrowid; conn.commit(); conn.close()
    await update.message.reply_text(f"📝 Not kaydedildi: '{title}' (ID: {note_id})")

async def list_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, title FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT 10', (user_id,))
    items = cursor.fetchall(); conn.close()
    if not items: await update.message.reply_text("📭 Henüz notunuz bulunmuyor!"); return
    keyboard = [[InlineKeyboardButton(f"🗑️ {title}", callback_data=f"delete_note:{item_id}")] for item_id, title in items]
    await update.message.reply_text("📝 Son Notlarınız (Silmek için butona basın):", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id
    if len(context.args) < 2: await update.message.reply_text("Kullanım: /hatirlatici_ekle [saat] [mesaj]"); return
    time_str = context.args[0]; message = ' '.join(context.args[1:])
    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59): raise ValueError
    except (ValueError): await update.message.reply_text("❌ Geçersiz saat formatı (HH:MM)!"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO reminders (user_id, chat_id, message, time) VALUES (?, ?, ?, ?)',(user_id, chat_id, message, time_str))
    r_id = cursor.lastrowid; conn.commit(); conn.close()
    await update.message.reply_text(f"🔔 Hatırlatıcı eklendi!\n⏰ {time_str} - {message}\n🆔 ID: {r_id}")

async def list_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT id, message, time FROM reminders WHERE user_id = ? AND active = 1 ORDER BY time', (user_id,))
    items = cursor.fetchall(); conn.close()
    if not items: await update.message.reply_text("📭 Aktif hatırlatıcınız yok!"); return
    keyboard = [[InlineKeyboardButton(f"🗑️ {time_str} - {msg}", callback_data=f"delete_reminder:{item_id}")] for item_id, msg, time_str in items]
    await update.message.reply_text("🔔 Aktif Hatırlatıcılarınız (Silmek için butona basın):", reply_markup=InlineKeyboardMarkup(keyboard))

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE); current_time = now.strftime("%H:%M"); current_date = now.strftime("%Y-%m-%d")
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
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handlers([
        CommandHandler("start", start_command), CommandHandler("help", help_command),
        CommandHandler("nasıl", nasil_command), CommandHandler("yeni_sohbet", new_chat_command),
        CommandHandler("unutma", unutma_command), CommandHandler("hatirla", hatirla_command),
        CommandHandler("gorev_ekle", add_task_command), CommandHandler("gorevler", list_tasks_command),
        CommandHandler("not_ekle", add_note_command), CommandHandler("notlar", list_notes_command),
        CommandHandler("hatirlatici_ekle", add_reminder_command), CommandHandler("hatirlaticilar", list_reminders_command),
        CallbackQueryHandler(button_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    ])

    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Botun son versiyonu başlatıldı, tüm geliştirmeler aktif!")
    application.run_polling()

if __name__ == "__main__":
    main()
