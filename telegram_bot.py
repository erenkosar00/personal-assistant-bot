"""
Kişisel Telegram Asistan Botu v5.3 - Zaman Ayrıştırma Düzeltildi
"""
import os
import logging
import base64
import json
import pytz
import re
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
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

# --- ZAMAN AYRIŞTIRMA FONKSİYONU ---
def parse_time_from_text(text):
    """
    Metinden zaman bilgisini çıkarır ve İstanbul saat dilimine göre datetime döndürür
    """
    istanbul_tz = pytz.timezone('Europe/Istanbul')
    now = datetime.now(istanbul_tz)
    
    # Yaygın zaman ifadeleri için pattern'ler
    patterns = [
        # "yarın saat 11:00" veya "yarın 11:00"
        (r'yarın\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: (now + timedelta(days=1)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        
        # "bugün saat 11:00" veya "bugün 11:00" 
        (r'bugün\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        
        # "saat 11:00" (bugün için)
        (r'(?:^|\s)saat\s+(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        
        # "11:00" (sadece saat)
        (r'(?:^|\s)(\d{1,2}):(\d{2})(?:\s|$)', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
        
        # "yarın 11'de" 
        (r'yarın\s+(\d{1,2})\'?(?:de|da)', lambda h: (now + timedelta(days=1)).replace(hour=int(h), minute=0, second=0, microsecond=0)),
        
        # "bugün 11'de"
        (r'bugün\s+(\d{1,2})\'?(?:de|da)', lambda h: now.replace(hour=int(h), minute=0, second=0, microsecond=0)),
        
        # "11'de" (bugün için)
        (r'(?:^|\s)(\d{1,2})\'?(?:de|da)(?:\s|$)', lambda h: now.replace(hour=int(h), minute=0, second=0, microsecond=0)),
        
        # "1 saat sonra"
        (r'(\d+)\s+saat\s+sonra', lambda h: now + timedelta(hours=int(h))),
        
        # "30 dakika sonra"
        (r'(\d+)\s+dakika\s+sonra', lambda m: now + timedelta(minutes=int(m))),
        
        # "2 gün sonra saat 15:00"
        (r'(\d+)\s+gün\s+sonra\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda d, h, m: (now + timedelta(days=int(d))).replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
    ]
    
    for pattern, time_func in patterns:
        match = re.search(pattern, text.lower())
        if match:
            try:
                parsed_time = time_func(*match.groups())
                
                # Geçmişte kalmışsa (bugün için) yarına al
                if parsed_time <= now and 'yarın' not in text.lower() and 'gün sonra' not in text.lower():
                    parsed_time += timedelta(days=1)
                
                # Zaman dilimini ayarla
                if parsed_time.tzinfo is None:
                    parsed_time = istanbul_tz.localize(parsed_time)
                
                # Mesajdan zaman ifadesini çıkar
                message = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
                message = re.sub(r'\s+', ' ', message)  # Çoklu boşlukları tek boşluk yap
                
                return parsed_time, message, match.group(0)
                
            except Exception as e:
                logger.error(f"Zaman ayrıştırma hatası: {e}")
                continue
    
    return None, text, None

# --- TELEGRAM FONKSİYONLARI ---
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Asistanı başlatır"),
        BotCommand("hatirlat", "Google Takvime akıllı hatırlatıcı ekler"),
        BotCommand("takvim", "Google Takvimini açar"),
        BotCommand("yeni_sohbet", "Yapay zeka sohbet geçmişini sıfırlar"),
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Merhaba! Ben Google Takvim ile entegre kişisel asistanınım.\n\n"
        "Komutlar:\n"
        "/hatirlat yarın saat 11:00 proje toplantısı\n"
        "/hatirlat bugün 15:30 doktor randevusu\n"
        "/hatirlat 2 saat sonra alışveriş\n"
        "/takvim - Takvimi açar\n"
        "/yeni_sohbet - Sohbet geçmişini sıfırlar"
    )

async def set_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Lütfen hatırlatıcı için zaman ve mesaj belirtin.\n\n"
            "Örnekler:\n"
            "• /hatirlat yarın saat 11:00 proje toplantısı\n"
            "• /hatirlat bugün 15:30 doktor randevusu\n"
            "• /hatirlat 2 saat sonra alışveriş\n"
            "• /hatirlat yarın 9'da egzersiz"
        )
        return

    full_text = " ".join(context.args)
    
    # Zaman ayrıştırma
    parsed_time, message, found_time_expr = parse_time_from_text(full_text)
    
    if not parsed_time:
        await update.message.reply_text(
            "❌ Zaman ifadesi bulunamadı.\n\n"
            "Desteklenen formatlar:\n"
            "• yarın saat 11:00\n"
            "• bugün 15:30\n"
            "• 2 saat sonra\n"
            "• yarın 9'da\n"
            "• 30 dakika sonra"
        )
        return

    if not message or len(message.strip()) < 3:
        await update.message.reply_text(
            f"❌ Hatırlatıcı mesajı bulunamadı.\n"
            f"Zaman: {found_time_expr}\n"
            f"Lütfen neyi hatırlatacağımı da belirtin."
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
            'overrides': [
                {'method': 'popup', 'minutes': 10}
            ]
        },
    }

    try:
        if not calendar_service:
            await update.message.reply_text("❌ Google Calendar servis bağlantısı kurulamadı.")
            return
            
        result = calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        
        # Türkçe tarih formatı
        formatted_time = parsed_time.strftime('%d %B %Y, %A, Saat %H:%M')
        
        # Ay isimlerini Türkçe'ye çevir
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
        
        await update.message.reply_text(
            f"✅ Google Takvime başarıyla eklendi!\n\n"
            f"📝 Etkinlik: {message}\n"
            f"📅 Tarih: {formatted_time}\n"
            f"🔗 Etkinlik ID: {result['id'][:8]}..."
        )
        
    except Exception as e:
        logger.error(f"Google Calendar API hatası: {e}")
        await update.message.reply_text(
            f"❌ Takvime etkinlik eklerken hata oluştu:\n{str(e)[:100]}"
        )

async def calendar_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📅 Asistan Takvimini açmak için tıklayın:\n"
        f"https://calendar.google.com/calendar/u/0?cid={GOOGLE_CALENDAR_ID}"
    )

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in chat_sessions:
        del chat_sessions[user_id]
    await update.message.reply_text("🤖 Sohbet geçmişi temizlendi. Yeni bir sohbete başlayabilirsiniz.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    if user_id not in chat_sessions:
        chat_sessions[user_id] = gemini_model.start_chat()
    
    chat = chat_sessions[user_id]
    
    try:
        response = await chat.send_message_async(user_text)
        await update.message.reply_text(response.text)
    except Exception as e:
        logger.error(f"Gemini sohbet hatası: {e}")
        await update.message.reply_text("🤖 Üzgünüm, şu anda bir sorunla karşılaştım. Lütfen tekrar deneyin.")

def main() -> None:
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("hatirlat", set_reminder_command))
    application.add_handler(CommandHandler("takvim", calendar_link_command))
    application.add_handler(CommandHandler("yeni_sohbet", new_chat_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot başlatıldı - Zaman ayrıştırma düzeltildi!")
    application.run_polling()

if __name__ == "__main__":
    main()