"""
Kişisel Asistan Bot - Finansal Takip Sistemi v7.0
"""
import os
import logging
import base64
import json
import pytz
import re
import sqlite3
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

# --- FINANSAL VERİTABANI ---
def init_financial_db():
    """Finansal takip veritabanını başlat"""
    db_path = Path.home() / ".telegram_assistant" / "financial.db"
    os.makedirs(db_path.parent, exist_ok=True)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            account_type TEXT, -- 'araba', 'emlak', 'kisisel'
            transaction_type TEXT, -- 'gelir', 'gider'
            amount REAL,
            category TEXT,
            description TEXT,
            date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    return db_path

DB_PATH = init_financial_db()

# --- FİNANSAL İNTENT TANIMLAMA ---
def detect_financial_intent(text):
    """Finansal işlem tipini ve detaylarını tespit et"""
    text_lower = text.lower()
    
    # Para miktarı tespiti
    amount_match = re.search(r'(\d+(?:\.\d+)?)\s*tl', text_lower)
    if not amount_match:
        return None, None, None, None, None
    
    amount = float(amount_match.group(1))
    
    # İşlem tipi tespiti
    income_keywords = ['kazandım', 'aldım', 'gelir', 'sattım', 'komisyon', 'ödeme aldım']
    expense_keywords = ['harcadım', 'ödedim', 'aldım', 'masraf', 'gider', 'fatura']
    
    transaction_type = None
    if any(keyword in text_lower for keyword in income_keywords):
        transaction_type = 'gelir'
    elif any(keyword in text_lower for keyword in expense_keywords):
        transaction_type = 'gider'
    else:
        # Bağlama göre tahmin et
        if any(word in text_lower for word in ['satış', 'komisyon', 'kazanç']):
            transaction_type = 'gelir'
        else:
            transaction_type = 'gider'
    
    # Hesap tipi tespiti
    account_type = 'kisisel'  # varsayılan
    if any(word in text_lower for word in ['araba', 'galeri', 'otomobil', 'civic', 'bmw', 'mercedes']):
        account_type = 'araba'
    elif any(word in text_lower for word in ['emlak', 'ev', 'daire', 'kiralama', 'satış komisyonu']):
        account_type = 'emlak'
    
    # Kategori tespiti
    categories = {
        'araba': {
            'gelir': ['satış', 'servis', 'diğer'],
            'gider': ['alım', 'yakıt', 'bakım', 'kira', 'personel', 'reklam', 'diğer']
        },
        'emlak': {
            'gelir': ['satış_komisyonu', 'kiralama_komisyonu', 'danışmanlık', 'diğer'],
            'gider': ['pazarlama', 'ulaşım', 'ofis', 'lisans', 'diğer']
        },
        'kisisel': {
            'gelir': ['maaş', 'kira_geliri', 'yatırım', 'diğer'],
            'gider': ['yemek', 'ulaşım', 'ev', 'eğlence', 'sağlık', 'alışveriş', 'diğer']
        }
    }
    
    category = 'diğer'  # varsayılan
    
    # Kategori tahmin et
    for cat in categories[account_type][transaction_type]:
        if cat in text_lower or any(word in text_lower for word in cat.split('_')):
            category = cat
            break
    
    # Açıklama çıkar (miktarı çıkararak)
    description = re.sub(r'\d+(?:\.\d+)?\s*tl', '', text, flags=re.IGNORECASE).strip()
    description = re.sub(r'\s+', ' ', description)
    
    return account_type, transaction_type, amount, category, description

# --- FİNANSAL FONKSİYONLAR ---
def add_transaction(user_id, account_type, transaction_type, amount, category, description):
    """Finansal işlem ekle"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO transactions (user_id, account_type, transaction_type, amount, category, description, date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, account_type, transaction_type, amount, category, description, datetime.now().date()))
    
    conn.commit()
    conn.close()

def get_financial_summary(user_id, period='month', account_type=None):
    """Finansal özet getir"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Tarih filtreleri
    if period == 'day':
        date_filter = datetime.now().date()
        query_date = "date = ?"
    elif period == 'week':
        date_filter = (datetime.now() - timedelta(days=7)).date()
        query_date = "date >= ?"
    elif period == 'month':
        date_filter = datetime.now().replace(day=1).date()
        query_date = "date >= ?"
    else:  # year
        date_filter = datetime.now().replace(month=1, day=1).date()
        query_date = "date >= ?"
    
    base_query = f"SELECT transaction_type, SUM(amount), category FROM transactions WHERE user_id = ? AND {query_date}"
    params = [user_id, date_filter]
    
    if account_type:
        base_query += " AND account_type = ?"
        params.append(account_type)
    
    base_query += " GROUP BY transaction_type, category"
    
    cursor.execute(base_query, params)
    results = cursor.fetchall()
    
    # Toplam gelir/gider hesapla
    total_query = f"SELECT transaction_type, SUM(amount) FROM transactions WHERE user_id = ? AND {query_date}"
    total_params = [user_id, date_filter]
    
    if account_type:
        total_query += " AND account_type = ?"
        total_params.append(account_type)
        
    total_query += " GROUP BY transaction_type"
    
    cursor.execute(total_query, total_params)
    totals = cursor.fetchall()
    
    conn.close()
    
    return results, totals

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

# --- İNTENT TANIMLAMA ---
def detect_intent(text):
    """Kullanıcının mesajından ne istediğini anlar"""
    text_lower = text.lower()
    
    # Finansal işlem kontrolü
    if re.search(r'\d+(?:\.\d+)?\s*tl', text_lower):
        return 'financial', text
    
    # Finansal rapor kontrolü
    report_keywords = ['ne kadar', 'toplam', 'özet', 'rapor', 'durum', 'hesap']
    period_keywords = ['bugün', 'bu hafta', 'bu ay', 'bu yıl']
    account_keywords = ['araba', 'emlak', 'kişisel', 'genel']
    
    if any(keyword in text_lower for keyword in report_keywords):
        return 'financial_report', text
    
    # Hatırlatıcı intent'i
    reminder_keywords = ['hatırlat', 'randevu', 'toplantı', 'etkinlik', 'görüşme', 'buluşma', 'yapacak']
    time_patterns = [r'\d{1,2}:\d{2}', r'yarın', r'bugün', r'saat', r'sonra', r'gün', r'hafta']
    
    has_reminder_keyword = any(keyword in text_lower for keyword in reminder_keywords)
    has_time_pattern = any(re.search(pattern, text_lower) for pattern in time_patterns)
    
    if has_reminder_keyword or has_time_pattern:
        return 'reminder', text
    
    # Diğer intent'ler
    calendar_keywords = ['takvim', 'ajanda', 'program']
    if any(keyword in text_lower for keyword in calendar_keywords):
        return 'calendar', text
        
    reset_keywords = ['yeni konuşma', 'sıfırla', 'temizle', 'baştan', 'reset']
    if any(keyword in text_lower for keyword in reset_keywords):
        return 'reset_chat', text
        
    help_keywords = ['yardım', 'help', 'nasıl', 'komut', 'ne yapabilir']
    if any(keyword in text_lower for keyword in help_keywords):
        return 'help', text
        
    return 'chat', text

# --- TELEGRAM FONKSİYONLARI ---
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Asistanı başlatır"),
        BotCommand("yardim", "Yardım menüsünü gösterir"),
        BotCommand("hesap", "Mali durum özeti"),
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary")],
        [InlineKeyboardButton("📅 Takvim", callback_data="calendar")],
        [InlineKeyboardButton("💭 Yeni Sohbet", callback_data="new_chat")],
        [InlineKeyboardButton("❓ Yardım", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 Merhaba! Ben senin kişisel asistanın ve mali danışmanınım.\n\n"
        "Bana şöyle yazabilirsin:\n"
        "💰 \"5000 TL araba sattım\"\n"
        "💰 \"300 TL yakıt aldım\"\n"
        "💰 \"Bu ay ne kadar kazandım?\"\n"
        "⏰ \"Yarın 14:30'da toplantım var\"\n"
        "📅 \"Takvimimi göster\"\n\n"
        "Komut yazmana gerek yok!",
        reply_markup=reply_markup
    )

async def account_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mali durum özeti komutu"""
    await handle_financial_report(update, "bu ay genel durum")

async def handle_financial_transaction(update: Update, text: str):
    """Finansal işlem ekleme"""
    user_id = update.effective_user.id
    
    result = detect_financial_intent(text)
    if not result[0]:
        await update.message.reply_text(
            "💰 Para miktarını anlayamadım. Örnek:\n"
            "\"5000 TL araba sattım\"\n"
            "\"300 TL benzin aldım\""
        )
        return
    
    account_type, transaction_type, amount, category, description = result
    
    # İşlemi kaydet
    add_transaction(user_id, account_type, transaction_type, amount, category, description)
    
    # Onay mesajı
    account_names = {'araba': 'Araba İşi', 'emlak': 'Emlak İşi', 'kisisel': 'Kişisel'}
    type_emoji = '📈' if transaction_type == 'gelir' else '📉'
    
    await update.message.reply_text(
        f"{type_emoji} İşlem kaydedildi!\n\n"
        f"💼 Hesap: {account_names[account_type]}\n"
        f"💰 Miktar: {amount:,.0f} TL\n"
        f"📁 Kategori: {category}\n"
        f"📝 Açıklama: {description}"
    )

async def handle_financial_report(update: Update, text: str):
    """Finansal rapor oluştur"""
    user_id = update.effective_user.id
    text_lower = text.lower()
    
    # Period tespiti
    period = 'month'
    if 'bugün' in text_lower or 'gün' in text_lower:
        period = 'day'
    elif 'hafta' in text_lower:
        period = 'week'
    elif 'yıl' in text_lower:
        period = 'year'
    
    # Hesap tipi tespiti
    account_type = None
    if 'araba' in text_lower:
        account_type = 'araba'
    elif 'emlak' in text_lower:
        account_type = 'emlak'
    elif 'kişisel' in text_lower:
        account_type = 'kisisel'
    
    results, totals = get_financial_summary(user_id, period, account_type)
    
    if not results:
        period_names = {'day': 'bugün', 'week': 'bu hafta', 'month': 'bu ay', 'year': 'bu yıl'}
        await update.message.reply_text(f"📊 {period_names[period].title()} hiç işlem yok.")
        return
    
    # Rapor hazırla
    period_names = {'day': 'Bugün', 'week': 'Bu Hafta', 'month': 'Bu Ay', 'year': 'Bu Yıl'}
    account_names = {'araba': 'Araba İşi', 'emlak': 'Emlak İşi', 'kisisel': 'Kişisel'}
    
    report = f"📊 {period_names[period]} Mali Durum"
    if account_type:
        report += f" - {account_names[account_type]}"
    report += "\n\n"
    
    total_income = 0
    total_expense = 0
    
    for transaction_type, total in totals:
        if transaction_type == 'gelir':
            total_income = total
        else:
            total_expense = total
    
    report += f"📈 Toplam Gelir: {total_income:,.0f} TL\n"
    report += f"📉 Toplam Gider: {total_expense:,.0f} TL\n"
    report += f"💰 Net Kar: {(total_income - total_expense):,.0f} TL\n\n"
    
    # Kategori detayları
    income_categories = []
    expense_categories = []
    
    for transaction_type, amount, category in results:
        if transaction_type == 'gelir':
            income_categories.append(f"  • {category}: {amount:,.0f} TL")
        else:
            expense_categories.append(f"  • {category}: {amount:,.0f} TL")
    
    if income_categories:
        report += "📈 Gelir Detayları:\n" + "\n".join(income_categories) + "\n\n"
    
    if expense_categories:
        report += "📉 Gider Detayları:\n" + "\n".join(expense_categories)
    
    keyboard = [
        [InlineKeyboardButton("📊 Haftalık", callback_data="report_week")],
        [InlineKeyboardButton("📊 Aylık", callback_data="report_month")],
        [InlineKeyboardButton("🚗 Araba İşi", callback_data="report_araba")],
        [InlineKeyboardButton("🏠 Emlak İşi", callback_data="report_emlak")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(report, reply_markup=reply_markup)

async def handle_natural_reminder(update: Update, text: str):
    """Doğal dilden hatırlatıcı oluşturur"""
    parsed_time, message, found_time_expr = parse_time_from_text(text)
    
    if not parsed_time:
        await update.message.reply_text(
            "⏰ Hangi zaman için hatırlatıcı ayarlayacağım?\n"
            "Örnek: \"Yarın 14:30'da\" veya \"2 saat sonra\""
        )
        return

    if not message or len(message.strip()) < 3:
        await update.message.reply_text(
            f"📝 Neyi hatırlatacağımı söylemedin.\n"
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
        
        await update.message.reply_text(
            f"✅ Hatırlatıcın ayarlandı!\n\n"
            f"📝 {message}\n"
            f"📅 {formatted_time}"
        )
        
    except Exception as e:
        await update.message.reply_text("❌ Takvime eklerken sorun oluştu.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "financial_summary":
        user_id = query.from_user.id
        results, totals = get_financial_summary(user_id, 'month')
        
        if not totals:
            await query.edit_message_text("📊 Bu ay hiç işlem yok.")
            return
            
        total_income = sum(total for transaction_type, total in totals if transaction_type == 'gelir')
        total_expense = sum(total for transaction_type, total in totals if transaction_type == 'gider')
        
        report = f"📊 Bu Ay Mali Durum\n\n"
        report += f"📈 Gelir: {total_income:,.0f} TL\n"
        report += f"📉 Gider: {total_expense:,.0f} TL\n"
        report += f"💰 Net: {(total_income - total_expense):,.0f} TL"
        
        await query.edit_message_text(report)
        
    elif query.data.startswith("report_"):
        period_or_account = query.data.replace("report_", "")
        if period_or_account in ['week', 'month']:
            await handle_financial_report(query, f"bu {period_or_account}")
        else:
            await handle_financial_report(query, f"bu ay {period_or_account}")
            
    elif query.data == "calendar":
        await query.edit_message_text(
            f"📅 Takvimini açmak için tıkla:\n"
            f"https://calendar.google.com/calendar/u/0?cid={GOOGLE_CALENDAR_ID}"
        )
    elif query.data == "new_chat":
        user_id = query.from_user.id
        if user_id in chat_sessions:
            del chat_sessions[user_id]
        await query.edit_message_text("🤖 Sohbet geçmişi temizlendi!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ana mesaj işleyici"""
    user_text = update.message.text
    intent, text = detect_intent(user_text)
    
    if intent == 'financial':
        await handle_financial_transaction(update, text)
        return
        
    elif intent == 'financial_report':
        await handle_financial_report(update, text)
        return
        
    elif intent == 'reminder':
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
        await update.message.reply_text("🤖 Şu anda sorun yaşıyorum. Tekrar dener misin?")

def main() -> None:
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("hesap", account_summary_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Finansal takip sistemli bot başlatıldı!")
    application.run_polling()

if __name__ == "__main__":
    main()