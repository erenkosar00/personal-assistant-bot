"""
Kişisel Telegram Asistan Botu v3.0 - "Sıfır Komut" Deneyimi
- Doğal Dil Anlama (AI Tool Use)
- Gelişmiş Hatırlatıcı ve Planlama
- Sohbet ve Bilgi Hafızası
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

# Logging ayarları
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- API ANAHTARLARI VE AYARLAR ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not TOKEN: raise ValueError("TELEGRAM_TOKEN ortam değişkeni ayarlanmadı!")
if not GEMINI_API_KEY: raise ValueError("GEMINI_API_KEY ortam değişkeni ayarlanmadı!")

DB_PATH = Path("assistant.db")
TIMEZONE = pytz.timezone("Europe/Istanbul")

# --- VERİTABANI İŞLEMLERİ ---
def setup_database():
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, completed BOOLEAN DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, content TEXT)')
    # Hatırlatıcı tablosunu tam tarih/saat saklayacak şekilde güncelliyoruz
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, reminder_time TIMESTAMP, status TEXT DEFAULT "active")')
    cursor.execute('CREATE TABLE IF NOT EXISTS memories (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key))')
    conn.commit(); conn.close()

# --- GEMINI'NİN KULLANACAĞI "ALETLER" (PYTHON FONKSİYONLARI) ---
def add_task(user_id: int, title: str) -> str:
    """Kullanıcının görev listesine yeni bir görev ekler."""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO tasks (user_id, title) VALUES (?, ?)', (user_id, title))
    conn.commit(); conn.close()
    logger.info(f"Görev eklendi: {title} (Kullanıcı: {user_id})")
    return f"'{title}' görevi başarıyla eklendi."

def list_tasks(user_id: int) -> str:
    """Kullanıcının tamamlanmamış tüm görevlerini listeler."""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT title FROM tasks WHERE user_id = ? AND completed = 0', (user_id,))
    tasks = cursor.fetchall(); conn.close()
    if not tasks: return "Şu anda aktif bir görevin yok."
    return "Aktif görevlerin şunlar:\n- " + "\n- ".join([task[0] for task in tasks])

def add_note(user_id: int, title: str, content: str) -> str:
    """Kullanıcı için yeni bir not kaydeder."""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)', (user_id, title, content))
    conn.commit(); conn.close()
    logger.info(f"Not eklendi: {title} (Kullanıcı: {user_id})")
    return f"'{title}' başlıklı notun başarıyla kaydedildi."

def set_reminder(user_id: int, chat_id: int, time_string: str, message: str) -> str:
    """Kullanıcı için belirtilen zamanda bir hatırlatıcı kurar."""
    parsed_time = dateparser.parse(time_string, settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'Europe/Istanbul'})
    if not parsed_time:
        return "Üzgünüm, belirttiğin zamanı anlayamadım. Lütfen 'yarın 15:30' gibi daha net bir ifade kullan."

    reminder_time_utc = parsed_time.astimezone(pytz.utc)

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO reminders (user_id, chat_id, message, reminder_time) VALUES (?, ?, ?, ?)', 
                   (user_id, chat_id, message, reminder_time_utc))
    conn.commit(); conn.close()

    formatted_time = parsed_time.strftime('%d %B %Y, Saat %H:%M')
    logger.info(f"Hatırlatıcı kuruldu: {message} -> {formatted_time} (Kullanıcı: {user_id})")
    return f"Tamamdır, '{formatted_time}' için '{message}' hatırlatıcısını kurdum."

# Gemini'yi ve aletlerini yapılandır
genai.configure(api_key=GEMINI_API_KEY)
gemini_tools = [add_task, list_tasks, add_note, set_reminder]
gemini_model = genai.GenerativeModel(model_name='gemini-1.5-flash-latest', tools=gemini_tools)
chat_sessions = {}

# --- ANA KONTROL FONKSİYONLARI ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Merhaba! Ben senin kişisel asistanınım. Artık komutlara ihtiyacın yok, sadece ne istediğini söyle.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id; user_text = update.message.text

    if user_id not in chat_sessions:
        chat_sessions[user_id] = gemini_model.start_chat()
    chat = chat_sessions[user_id]

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        response = await chat.send_message_async(user_text)

        # Gemini bir alet kullanmak isterse...
        if response.candidates[0].content.parts[0].function_call:
            fc = response.candidates[0].content.parts[0].function_call
            tool_name = fc.name
            tool_args = {key: value for key, value in fc.args.items()}

            # Doğru aleti bul ve çalıştır
            tool_to_call = next((t for t in gemini_tools if t.__name__ == tool_name), None)
            if tool_to_call:
                # Gerekli ID'leri argümanlara ekle
                if 'user_id' in tool_to_call.__annotations__: tool_args['user_id'] = user_id
                if 'chat_id' in tool_to_call.__annotations__: tool_args['chat_id'] = chat_id

                tool_response = tool_to_call(**tool_args)

                # Aletin sonucunu Gemini'ye geri göndererek son kullanıcı cevabını oluşturmasını sağla
                response = await chat.send_message_async(
                    genai.Part.from_function_response(name=tool_name, response={'result': tool_response})
                )

        await update.message.reply_text(response.text)

    except Exception as e:
        logger.error(f"İşlem hatası (kullanıcı: {user_id}): {e}")
        await update.message.reply_text("🤖 Üzgünüm, bir sorunla karşılaştım. Lütfen tekrar dener misin?")

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    """Her dakika çalışıp zamanı gelmiş hatırlatıcıları gönderir."""
    now_utc = datetime.now(pytz.utc)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, message FROM reminders WHERE status = 'active' AND reminder_time <= ?", (now_utc,))
    reminders = cursor.fetchall()

    for r_id, chat_id, message in reminders:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"🔔 HATIRLATICI\n\n{message}")
            cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,))
            conn.commit()
            logger.info(f"Hatırlatıcı gönderildi: ID {r_id}")
        except Exception as e:
            logger.error(f"Hatırlatıcı ID {r_id} gönderilemedi: {e}")
    conn.close()

def main() -> None:
    setup_database()
    application = Application.builder().token(TOKEN).build()

    # Sadece temel komutları ve ana sohbet yöneticisini ekliyoruz
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Hatırlatıcıları kontrol eden periyodik görevi başlat
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Botun 'Sıfır Komut' versiyonu başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()
"""
Kişisel Telegram Asistan Botu v3.0 - "Sıfır Komut" Deneyimi
- Doğal Dil Anlama (AI Tool Use)
- Gelişmiş Hatırlatıcı ve Planlama
- Sohbet ve Bilgi Hafızası
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

# Logging ayarları
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- API ANAHTARLARI VE AYARLAR ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not TOKEN: raise ValueError("TELEGRAM_TOKEN ortam değişkeni ayarlanmadı!")
if not GEMINI_API_KEY: raise ValueError("GEMINI_API_KEY ortam değişkeni ayarlanmadı!")

DB_PATH = Path("assistant.db")
TIMEZONE = pytz.timezone("Europe/Istanbul")

# --- VERİTABANI İŞLEMLERİ ---
def setup_database():
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT NOT NULL, completed BOOLEAN DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, content TEXT)')
    # Hatırlatıcı tablosunu tam tarih/saat saklayacak şekilde güncelliyoruz
    cursor.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id INTEGER, message TEXT NOT NULL, reminder_time TIMESTAMP, status TEXT DEFAULT "active")')
    cursor.execute('CREATE TABLE IF NOT EXISTS memories (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key))')
    conn.commit(); conn.close()

# --- GEMINI'NİN KULLANACAĞI "ALETLER" (PYTHON FONKSİYONLARI) ---
def add_task(user_id: int, title: str) -> str:
    """Kullanıcının görev listesine yeni bir görev ekler."""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO tasks (user_id, title) VALUES (?, ?)', (user_id, title))
    conn.commit(); conn.close()
    logger.info(f"Görev eklendi: {title} (Kullanıcı: {user_id})")
    return f"'{title}' görevi başarıyla eklendi."

def list_tasks(user_id: int) -> str:
    """Kullanıcının tamamlanmamış tüm görevlerini listeler."""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT title FROM tasks WHERE user_id = ? AND completed = 0', (user_id,))
    tasks = cursor.fetchall(); conn.close()
    if not tasks: return "Şu anda aktif bir görevin yok."
    return "Aktif görevlerin şunlar:\n- " + "\n- ".join([task[0] for task in tasks])

def add_note(user_id: int, title: str, content: str) -> str:
    """Kullanıcı için yeni bir not kaydeder."""
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)', (user_id, title, content))
    conn.commit(); conn.close()
    logger.info(f"Not eklendi: {title} (Kullanıcı: {user_id})")
    return f"'{title}' başlıklı notun başarıyla kaydedildi."

def set_reminder(user_id: int, chat_id: int, time_string: str, message: str) -> str:
    """Kullanıcı için belirtilen zamanda bir hatırlatıcı kurar."""
    parsed_time = dateparser.parse(time_string, settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'Europe/Istanbul'})
    if not parsed_time:
        return "Üzgünüm, belirttiğin zamanı anlayamadım. Lütfen 'yarın 15:30' gibi daha net bir ifade kullan."

    reminder_time_utc = parsed_time.astimezone(pytz.utc)

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO reminders (user_id, chat_id, message, reminder_time) VALUES (?, ?, ?, ?)', 
                   (user_id, chat_id, message, reminder_time_utc))
    conn.commit(); conn.close()

    formatted_time = parsed_time.strftime('%d %B %Y, Saat %H:%M')
    logger.info(f"Hatırlatıcı kuruldu: {message} -> {formatted_time} (Kullanıcı: {user_id})")
    return f"Tamamdır, '{formatted_time}' için '{message}' hatırlatıcısını kurdum."

# Gemini'yi ve aletlerini yapılandır
genai.configure(api_key=GEMINI_API_KEY)
gemini_tools = [add_task, list_tasks, add_note, set_reminder]
gemini_model = genai.GenerativeModel(model_name='gemini-1.5-flash-latest', tools=gemini_tools)
chat_sessions = {}

# --- ANA KONTROL FONKSİYONLARI ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Merhaba! Ben senin kişisel asistanınım. Artık komutlara ihtiyacın yok, sadece ne istediğini söyle.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id; user_text = update.message.text

    if user_id not in chat_sessions:
        chat_sessions[user_id] = gemini_model.start_chat()
    chat = chat_sessions[user_id]

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        response = await chat.send_message_async(user_text)

        # Gemini bir alet kullanmak isterse...
        if response.candidates[0].content.parts[0].function_call:
            fc = response.candidates[0].content.parts[0].function_call
            tool_name = fc.name
            tool_args = {key: value for key, value in fc.args.items()}

            # Doğru aleti bul ve çalıştır
            tool_to_call = next((t for t in gemini_tools if t.__name__ == tool_name), None)
            if tool_to_call:
                # Gerekli ID'leri argümanlara ekle
                if 'user_id' in tool_to_call.__annotations__: tool_args['user_id'] = user_id
                if 'chat_id' in tool_to_call.__annotations__: tool_args['chat_id'] = chat_id

                tool_response = tool_to_call(**tool_args)

                # Aletin sonucunu Gemini'ye geri göndererek son kullanıcı cevabını oluşturmasını sağla
                response = await chat.send_message_async(
                    genai.Part.from_function_response(name=tool_name, response={'result': tool_response})
                )

        await update.message.reply_text(response.text)

    except Exception as e:
        logger.error(f"İşlem hatası (kullanıcı: {user_id}): {e}")
        await update.message.reply_text("🤖 Üzgünüm, bir sorunla karşılaştım. Lütfen tekrar dener misin?")

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    """Her dakika çalışıp zamanı gelmiş hatırlatıcıları gönderir."""
    now_utc = datetime.now(pytz.utc)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, message FROM reminders WHERE status = 'active' AND reminder_time <= ?", (now_utc,))
    reminders = cursor.fetchall()

    for r_id, chat_id, message in reminders:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"🔔 HATIRLATICI\n\n{message}")
            cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,))
            conn.commit()
            logger.info(f"Hatırlatıcı gönderildi: ID {r_id}")
        except Exception as e:
            logger.error(f"Hatırlatıcı ID {r_id} gönderilemedi: {e}")
    conn.close()

def main() -> None:
    setup_database()
    application = Application.builder().token(TOKEN).build()

    # Sadece temel komutları ve ana sohbet yöneticisini ekliyoruz
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Hatırlatıcıları kontrol eden periyodik görevi başlat
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders_job, interval=60, first=10)

    logger.info("Botun 'Sıfır Komut' versiyonu başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()
