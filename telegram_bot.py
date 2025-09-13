"""
Kullanıcı Dostu Telegram Bot - Doğal Dil Destekli v6.0
"""
import os
import logging
import base64
import json
import pytz
import re
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google.generativeai as genai

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

# --- INTENT TANIMLA ---
def detect_intent(text):
    """Kullanıcının mesajından ne istediğini anlar"""
    text_lower = text.lower()
    
    # Hatırlatıcı intent'i
    reminder_keywords = ['hatırlat', 'randevu', 'toplantı', 'etkinlik', 'görüşme', 'buluşma', 'yapacak']
    time_patterns = [r'\d{1,2}:\d{2}', r'yarın', r'bugün', r'saat', r'sonra', r'gün', r'hafta']
    
    has_reminder_keyword = any(keyword in text_lower for keyword in reminder_keywords)
    has_time_pattern = any(re.search(pattern, text_lower) for pattern in time_patterns)
    
    if has_reminder_keyword or has_time_pattern:
        return 'reminder', text
    
    # Takvim intent'i  
    calendar_keywords = ['takvim', 'ajanda', 'program', 'etkinlik', 'görüşme', 'randevu']
    if any(keyword in text_lower for keyword in calendar_keywords) and ('göster' in text_lower or 'aç' in text_lower):
        return 'calendar', text
        
    # Chat reset intent'i
    reset_keywords = ['yeni konuşma', 'sıfırla', 'temizle', 'baştan', 'reset']
    if any(keyword in text_lower for keyword in reset_keywords):
        return 'reset_chat', text
        
    # Yardım intent'i
    help_keywords = ['yardım', 'help', 'nasıl', 'komut', 'ne yapabilir']
    if any(keyword in text_lower for keyword in help_keywords):
        return 'help', text
        
    return 'chat', text

# --- ZAMAN AYRIŞTIRMA FONKSİYONU ---
def parse_time_from_text(text):
    """Metinden zaman bilgisini çıkarır"""
    istanbul_tz = pytz.timezone('Europe/Istanbul')
    now = datetime.now(istanbul_tz)
    
    patterns = [
        (r'yarın\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: (now + timedelta(days=1)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        (r'bugün\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        (r'(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        (r'yarın\s+(\d{1,2})\'?(?:de|da|te|ta)', lambda h: (now + timedelta(days=1)).replace(hour=int(h), minute=0, second=0, microsecond=0)),
        (r'bugün\s+(\d{1,2})\'?(?:de|da|te|ta)', lambda h: now.replace(hour=int(h), minute=0, second=0, microsecond=0)),
        (r'(\d{1,2})\'?(?:de|da|te|ta)', lambda h: now.replace(hour=int(h), minute=0, second=0, microsecond=0)),
        (r'(\d+)\s+saat\s+sonra', lambda h: now + timedelta(hours=int(h))),
        (r'(\d+)\s+dakika\s+sonra', lambda m: now + timedelta(minutes=int(m))),
        (r'(\d+)\s+gün\s+sonra\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda d, h, m: (now + timedelta(days=int(d))).replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
    ]
    
    for pattern, time_func in patterns:
        match = re.search(pattern, text.lower())
        if match:
            try:
                parsed_time = time_func(*match.groups())
                
                if parsed_time <= now and 'yarın' not in text.lower() and 'gün sonra' not in text.lower():
                    parsed_time += timedelta(days=1)
                
                if parsed_time.tzinfo is None:
                    parsed_time = istanbul_tz.localize(parsed_time)
                
                message = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
                message = re.sub(r'\s+', ' ', message)
                
                return parsed_time, message, match.group(0)
                
            except Exception as e:
                continue
    
    return None, text, None

# --- TELEGRAM FONKSİYONLARI ---
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Asistanı başlatır"),
        BotCommand("yardim", "Yardım menüsünü gösterir"),
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📅 Takvimimi Aç", callback_data="calendar")],
        [InlineKeyboardButton("💭 Yeni Sohbet", callback_data="new_chat")],
        [InlineKeyboardButton("❓ Yardım", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 Merhaba! Ben senin kişisel asistanınım.\n\n"
        "Bana doğal dilden şöyle yazabilirsin:\n"
        "• \"Yarın saat 14:30'da doktor randevum var\"\n"
        "• \"Bugün 18:00'de spor yapacağım\"\n"
        "• \"2 saat sonra alışveriş yapmayı hatırlat\"\n"
        "• \"Takvimimi göster\"\n"
        "• \"Yeni konuşma başlat\"\n\n"
        "Komut yazmana gerek yok, sadece normal konuş!",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 Yardım Menüsü\n\n"
        "Ben doğal dili anlıyorum! Şöyle yazabilirsin:\n\n"
        "⏰ Hatırlatıcı için:\n"
        "• \"Yarın 10:30'da toplantım var\"\n" 
        "• \"Bugün 18:00'de spor yapacağım\"\n"
        "• \"2 saat sonra ilaç almayı hatırlat\"\n"
        "• \"Yarın 9'da kahvaltı randevusu\"\n\n"
        "📅 Takvim için:\n"
        "• \"Takvimimi göster\"\n"
        "• \"Ajandamı aç\"\n\n"
        "💭 Sohbet için:\n"
        "• \"Yeni konuşma başlat\"\n"
        "• \"Sohbet geçmişini sıfırla\"\n\n"
        "Komut yazmana gerek yok, sadece ne istediğini söyle!"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "calendar":
        await query.edit_message_text(
            f"📅 Takvimini açmak için tıkla:\n"
            f"https://calendar.google.com/calendar/u/0?cid={GOOGLE_CALENDAR_ID}"
        )
    elif query.data == "new_chat":
        user_id = query.from_user.id
        if user_id in chat_sessions:
            del chat_sessions[user_id]
        await query.edit_message_text("🤖 Sohbet geçmişi temizlendi!")
    elif query.data == "help":
        await help_command(update, context)

async def handle_natural_reminder(update: Update, text: str):
    """Doğal dilden hatırlatıcı oluşturur"""
    parsed_time, message, found_time_expr = parse_time_from_text(text)
    
    if not parsed_time:
        await update.message.reply_text(
            "⏰ Hangi zaman için hatırlatıcı ayarlayacağım?\n\n"
            "Örnek: \"Yarın saat 14:30'da\" veya \"2 saat sonra\""
        )
        return

    if not message or len(message.strip()) < 3:
        await update.message.reply_text(
            f"📝 Neyi hatırlatacağımı söylemedin.\n"
            f"Zaman: {found_time_expr}\n"
            f"Örnek: \"Yarın 14:30'da doktor randevusu\""
        )
        return

    # Google Calendar etkinliği oluştur
    event = {
        'summary': message,
        'start': {
            'dateTime': parsed_time.isoformat(),
            'timeZone': 'Europe/Istanbul'
        },
        'end': {
            'dateTime': (parsed_time + timedelta(minutes=30)).isoformat(),
            'timeZone': 'Europe/Istanbul'
        },
        'reminders': {
            'useDefault': False,
            'overrides': [{'method': 'popup', 'minutes': 10}]
        },
    }

    try:
        result = calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        
        formatted_time = parsed_time.strftime('%d %B %Y, %A, Saat %H:%M')
        
        # Türkçe çeviri
        months = {
            'January': 'Ocak', 'February': 'Şubat', 'March': 'Mart',
            'April': 'Nisan', 'May': 'Mayıs', 'June': 'Haziran',
            'July': 'Temmuz', 'August': 'Ağustos', 'September': 'Eylül',
            'October': 'Ekim', 'November': 'Kasım', 'December': 'Aralık'
        }
        
        days = {
            'Monday': 'Pazartesi', 'Tuesday': 'Salı', 'Wednesday': 'Çarşamba',
            'Thursday': 'Perşembe', 'Friday': 'Cuma', 'Saturday': 'Cumartesi',
            'Sunday': 'Pazar'
        }
        
        for eng, tr in months.items():
            formatted_time = formatted_time.replace(eng, tr)
        for eng, tr in days.items():
            formatted_time = formatted_time.replace(eng, tr)
        
        keyboard = [[InlineKeyboardButton("📅 Takvimi Aç", callback_data="calendar")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ Hatırlatıcın ayarlandı!\n\n"
            f"📝 {message}\n"
            f"📅 {formatted_time}",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Google Calendar hatası: {e}")
        await update.message.reply_text("❌ Takvime eklerken sorun oluştu, lütfen tekrar dene.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ana mesaj işleyici - doğal dil anlama"""
    user_text = update.message.text
    intent, text = detect_intent(user_text)
    
    if intent == 'reminder':
        await handle_natural_reminder(update, text)
        return
        
    elif intent == 'calendar':
        await update.message.reply_text(
            f"📅 Takvimini açmak için tıkla:\n"
            f"https://calendar.google.com/calendar/u/0?cid={GOOGLE_CALENDAR_ID}"
        )
        return
        
    elif intent == 'reset_chat':
        user_id = update.effective_user.id
        if user_id in chat_sessions:
            del chat_sessions[user_id]
        await update.message.reply_text("🤖 Sohbet geçmişi temizlendi!")
        return
        
    elif intent == 'help':
        await help_command(update, context)
        return
    
    # Normal sohbet
    user_id = update.effective_user.id
    
    if user_id not in chat_sessions:
        chat_sessions[user_id] = gemini_model.start_chat()
    
    chat = chat_sessions[user_id]
    
    try:
        response = await chat.send_message_async(user_text)
        await update.message.reply_text(response.text)
    except Exception as e:
        logger.error(f"Gemini sohbet hatası: {e}")
        await update.message.reply_text("🤖 Üzgünüm, şu anda sorun yaşıyorum. Tekrar dener misin?")

def main() -> None:
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("yardim", help_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Kullanıcı dostu bot başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()