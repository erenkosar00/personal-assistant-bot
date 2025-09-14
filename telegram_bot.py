"""
Araba Satış Asistanı Botu v1.0
Finansal takip + Google Calendar + AI Uzman + Kullanım Kılavuzu
"""
import os
import logging
import sqlite3
import re
import json
import base64
import pytz
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Google Calendar
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# AI (Gemini)
try:
    import google.generativeai as genai
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CarDealerBot:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_TOKEN")
        self.db_path = Path.home() / ".telegram_assistant" / "car_dealer.db"
        
        # Initialize services
        self.setup_database()
        self.setup_google_calendar()
        self.setup_ai()
        
    def setup_database(self):
        """Database setup"""
        os.makedirs(self.db_path.parent, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Transactions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                category TEXT,
                description TEXT,
                date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # User onboarding tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                onboarded BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized")
    
    def setup_google_calendar(self):
        """Google Calendar setup"""
        self.calendar_service = None
        self.calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
        
        if not GOOGLE_AVAILABLE:
            logger.warning("Google libraries not available")
            return
        
        try:
            credentials_base64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64")
            if credentials_base64:
                creds_json = base64.b64decode(credentials_base64).decode('utf-8')
                creds_data = json.loads(creds_json)
                
                credentials = service_account.Credentials.from_service_account_info(
                    creds_data,
                    scopes=['https://www.googleapis.com/auth/calendar']
                )
                
                self.calendar_service = build('calendar', 'v3', credentials=credentials)
                logger.info("Google Calendar initialized")
        except Exception as e:
            logger.error(f"Google Calendar setup failed: {e}")
    
    def setup_ai(self):
        """AI setup for car expertise"""
        self.ai_model = None
        
        if not AI_AVAILABLE:
            logger.warning("Gemini AI not available")
            return
        
        try:
            api_key = os.environ.get("GEMINI_API_KEY")
            if api_key:
                genai.configure(api_key=api_key)
                self.ai_model = genai.GenerativeModel('gemini-1.5-flash-latest')
                logger.info("Gemini AI initialized")
        except Exception as e:
            logger.error(f"AI setup failed: {e}")

    def parse_financial_text(self, text):
        """Parse financial transaction from text"""
        text_lower = text.lower().strip()
        
        # Extract amount
        amount_match = re.search(r'(\d+(?:[.,]\d+)?)\s*tl', text_lower)
        if not amount_match:
            return None
        
        try:
            amount = float(amount_match.group(1).replace(',', '.'))
            if amount <= 0:
                return None
        except:
            return None
        
        # Determine income/expense
        income_keywords = ['sattım', 'kazandım', 'gelir', 'komisyon', 'satış']
        expense_keywords = ['aldım', 'harcadım', 'ödedim', 'masraf', 'gider']
        
        is_income = any(word in text_lower for word in income_keywords)
        is_expense = any(word in text_lower for word in expense_keywords)
        
        transaction_type = 'gelir' if (is_income and not is_expense) else 'gider'
        
        # Determine category
        category = self.determine_category(text_lower, transaction_type)
        
        # Extract description
        description = re.sub(r'\d+(?:[.,]\d+)?\s*tl', '', text, flags=re.IGNORECASE).strip()
        description = re.sub(r'\s+', ' ', description)[:200]
        
        return {
            'amount': amount,
            'type': transaction_type,
            'category': category,
            'description': description or 'İşlem'
        }
    
    def determine_category(self, text, transaction_type):
        """Determine transaction category"""
        categories = {
            'gelir': {
                'satış': ['sattım', 'satış', 'araba sattım'],
                'komisyon': ['komisyon', 'aracılık'],
                'servis': ['servis', 'tamir', 'bakım'],
                'diğer': []
            },
            'gider': {
                'alım': ['aldım', 'araba aldım', 'araç aldım'],
                'yakıt': ['yakıt', 'benzin', 'mazot'],
                'bakım': ['bakım', 'tamir', 'servis'],
                'kira': ['kira', 'ofis'],
                'personel': ['maaş', 'personel'],
                'reklam': ['reklam', 'pazarlama'],
                'diğer': []
            }
        }
        
        for category, keywords in categories[transaction_type].items():
            if any(keyword in text for keyword in keywords):
                return category
        
        return 'diğer'

    def parse_reminder_text(self, text):
        """Parse reminder from text"""
        text_lower = text.lower().strip()
        istanbul_tz = pytz.timezone('Europe/Istanbul')
        now = datetime.now(istanbul_tz)
        
        # Time patterns
        patterns = [
            (r'yarın\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: (now + timedelta(days=1)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
            (r'bugün\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
            (r'(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
            (r'(\d+)\s+saat\s+sonra', lambda h: now + timedelta(hours=int(h))),
        ]
        
        for pattern, time_func in patterns:
            match = re.search(pattern, text_lower)
            if match:
                try:
                    parsed_time = time_func(*match.groups())
                    # If time is in past, assume next day
                    if parsed_time <= now:
                        parsed_time += timedelta(days=1)
                    
                    # Extract message
                    message = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
                    message = re.sub(r'\s+', ' ', message)
                    
                    return parsed_time, message
                except:
                    continue
        
        return None, text

    def is_car_related_question(self, text):
        """Check if question is car-related"""
        car_keywords = [
            'araba', 'araç', 'otomobil', 'honda', 'toyota', 'bmw', 'mercedes',
            'civic', 'corolla', 'focus', 'golf', 'passat', 'a4', 'c180',
            'fiat', 'renault', 'peugeot', 'hyundai', 'nissan', 'ford',
            'satış', 'alım', 'piyasa', 'fiyat', 'değer', 'model', 'yıl',
            'km', 'motor', 'vites', 'hasar', 'tramer', 'ekspertiz',
            'muayene', 'plaka', 'ruhsat', 'sigorta', 'kasko'
        ]
        
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in car_keywords)

    async def get_car_expert_response(self, text):
        """Get AI response as car expert"""
        if not self.ai_model:
            return "AI servis şu anda mevcut değil. Daha sonra tekrar deneyin."
        
        try:
            system_prompt = """Sen Türkiye'de faaliyet gösteren deneyimli bir araba galeri sahibisin. 
            Araba alım-satım konusunda uzman tavsiyeler veriyorsun. 
            
            Görevin:
            - Araba fiyatları hakkında piyasa bilgisi vermek
            - Hangi araçların daha karlı olduğu konusunda tavsiye vermek  
            - Müşteri görüşmelerinde pazarlık stratejileri önermek
            - Araba alım-satım süreçleri hakkında rehberlik yapmak
            - Piyasa trendleri hakkında analiz yapmak
            
            Türkçe cevap ver ve pratik, uygulanabilir tavsiyeler ver.
            Cevapların profesyonel ama samimi olsun."""
            
            full_prompt = f"{system_prompt}\n\nKullanıcı sorusu: {text}"
            
            response = self.ai_model.generate_content(full_prompt)
            return response.text
            
        except Exception as e:
            logger.error(f"AI response failed: {e}")
            return "AI servisinde geçici bir sorun var. Lütfen daha sonra tekrar deneyin."

    def add_transaction(self, user_id, transaction):
        """Add transaction to database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO transactions (user_id, amount, type, category, description, date)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                transaction['amount'],
                transaction['type'],
                transaction['category'],
                transaction['description'],
                datetime.now().date().isoformat()
            ))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Transaction add failed: {e}")
            return False

    def get_financial_summary(self, user_id, period='week'):
        """Get financial summary"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Date filter
            if period == 'week':
                date_filter = (datetime.now() - timedelta(days=7)).date()
            elif period == 'month':
                date_filter = datetime.now().replace(day=1).date()
            else:  # today
                date_filter = datetime.now().date()
            
            cursor.execute('''
                SELECT type, SUM(amount), category
                FROM transactions 
                WHERE user_id = ? AND date >= ?
                GROUP BY type, category
                ORDER BY SUM(amount) DESC
            ''', (user_id, date_filter.isoformat()))
            
            results = cursor.fetchall()
            
            # Get totals
            cursor.execute('''
                SELECT type, SUM(amount)
                FROM transactions 
                WHERE user_id = ? AND date >= ?
                GROUP BY type
            ''', (user_id, date_filter.isoformat()))
            
            totals = cursor.fetchall()
            conn.close()
            
            return results, totals
            
        except Exception as e:
            logger.error(f"Financial summary failed: {e}")
            return [], []

    def create_calendar_event(self, title, start_time):
        """Create Google Calendar event"""
        if not self.calendar_service or not self.calendar_id:
            return False
        
        try:
            end_time = start_time + timedelta(minutes=30)
            
            event = {
                'summary': title,
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': 'Europe/Istanbul'
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': 'Europe/Istanbul'
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'popup', 'minutes': 10}
                    ]
                }
            }
            
            self.calendar_service.events().insert(
                calendarId=self.calendar_id,
                body=event
            ).execute()
            
            return True
            
        except Exception as e:
            logger.error(f"Calendar event creation failed: {e}")
            return False

    def format_financial_report(self, results, totals, period):
        """Format financial report"""
        if not results and not totals:
            period_names = {'week': 'bu hafta', 'month': 'bu ay', 'today': 'bugün'}
            return f"📊 {period_names.get(period, period).title()} hiç işlem yok."
        
        period_names = {'week': 'Bu Hafta', 'month': 'Bu Ay', 'today': 'Bugün'}
        report = f"📊 {period_names.get(period, period)} Mali Durum\n{'='*30}\n\n"
        
        # Calculate totals
        total_income = 0
        total_expense = 0
        
        for transaction_type, total in totals:
            if transaction_type == 'gelir':
                total_income = total
            elif transaction_type == 'gider':
                total_expense = total
        
        net_result = total_income - total_expense
        net_emoji = "💰" if net_result >= 0 else "📉"
        
        report += f"📈 Toplam Gelir: {total_income:,.0f} TL\n"
        report += f"📉 Toplam Gider: {total_expense:,.0f} TL\n"
        report += f"{net_emoji} Net Durum: {net_result:,.0f} TL\n\n"
        
        # Category details
        if results:
            income_items = []
            expense_items = []
            
            for transaction_type, amount, category in results:
                category_display = category.replace('_', ' ').title()
                item = f"  • {category_display}: {amount:,.0f} TL"
                
                if transaction_type == 'gelir':
                    income_items.append(item)
                else:
                    expense_items.append(item)
            
            if income_items:
                report += "📈 Gelir Detayları:\n" + "\n".join(income_items) + "\n\n"
            
            if expense_items:
                report += "📉 Gider Detayları:\n" + "\n".join(expense_items) + "\n\n"
        
        return report

    def is_user_onboarded(self, user_id):
        """Check if user completed onboarding"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('SELECT onboarded FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            conn.close()
            
            return result and result[0] == 1
        except:
            return False

    def mark_user_onboarded(self, user_id, username, first_name):
        """Mark user as onboarded"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO users (id, username, first_name, onboarded)
                VALUES (?, ?, ?, 1)
            ''', (user_id, username, first_name))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Mark onboarded failed: {e}")

    async def show_onboarding(self, update: Update):
        """Show onboarding tutorial"""
        user = update.effective_user
        
        tutorial_steps = [
            {
                "title": "🚗 Araba Satış Asistanına Hoş Geldiniz!",
                "text": (
                    "Ben sizin kişisel araba satış asistanınızım. "
                    "3 ana özelliğim var:\n\n"
                    "💰 Finansal takip\n"
                    "📅 Randevu yönetimi\n"
                    "🤖 Araba uzmanı danışmanlık\n\n"
                    "Şimdi nasıl kullanacağınızı göstereyim..."
                ),
                "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_1")]]
            },
            {
                "title": "💰 Finansal Takip Nasıl Kullanılır?",
                "text": (
                    "Mali işlemlerinizi doğal dille girebilirsiniz:\n\n"
                    "✅ Örnekler:\n"
                    "• \"350.000 TL Civic sattım\"\n"
                    "• \"15.000 TL galeri kirası ödedim\"\n"
                    "• \"500 TL yakıt aldım\"\n\n"
                    "Bot otomatik olarak gelir/gider kategorisine ayırır.\n\n"
                    "📊 Raporlar için:\n"
                    "• \"Bu hafta ne kadar kazandım?\"\n"
                    "• \"Aylık durum raporu\""
                ),
                "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_2")]]
            },
            {
                "title": "📅 Randevu Sistemi Nasıl Çalışır?",
                "text": (
                    "Randevularınızı doğal dille ekleyebilirsiniz:\n\n"
                    "✅ Örnekler:\n"
                    "• \"Yarın 14:30'da müşteri randevusu\"\n"
                    "• \"Bugün 16:00'da BMW test sürüşü\"\n"
                    "• \"2 saat sonra ekspertiz randevusu\"\n\n"
                    "🔔 Randevular otomatik olarak:\n"
                    "• Google Takvime eklenir\n"
                    "• 10 dakika önceden hatırlatılır\n\n"
                    "📱 Takviminizi telefonunuzda görebilirsiniz."
                ),
                "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_3")]]
            },
            {
                "title": "🤖 Araba Uzmanı Danışmanlık",
                "text": (
                    "Araba alım-satımıyla ilgili tüm sorularınızı sorabilirsiniz:\n\n"
                    "✅ Soru örnekleri:\n"
                    "• \"2018 Civic ne kadara satarım?\"\n"
                    "• \"Hangi markalar daha karlı?\"\n"
                    "• \"BMW mu Mercedes mi tercih edilir?\"\n"
                    "• \"Müşteri 300.000 TL teklif etti, kabul edeyim mi?\"\n\n"
                    "🎯 Yapay zeka uzmanım:\n"
                    "• Piyasa fiyatları hakkında bilgi verir\n"
                    "• Pazarlık stratejileri önerir\n"
                    "• Karlılık analizleri yapar"
                ),
                "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_4")]]
            },
            {
                "title": "🎉 Tebrikler! Hazırsınız!",
                "text": (
                    "Artık tüm özellikleri kullanabilirsiniz:\n\n"
                    "🚀 Hızlı başlangıç:\n"
                    "• Bir finansal işlem girin: \"500 TL yakıt aldım\"\n"
                    "• Randevu ekleyin: \"Yarın 15:00'te müşteri gelecek\"\n"
                    "• Araba sorusu sorun: \"2020 Focus ne kadar eder?\"\n\n"
                    "💡 İpucu: Komut yazmaya gerek yok!\n"
                    "Doğal Türkçe ile konuşun.\n\n"
                    "❓ Yardıma ihtiyacınız olursa /yardim yazın."
                ),
                "keyboard": [[InlineKeyboardButton("✅ Tamamlandı", callback_data="onboard_complete")]]
            }
        ]
        
        # Send first step
        first_step = tutorial_steps[0]
        keyboard = InlineKeyboardMarkup(first_step["keyboard"])
        
        await update.message.reply_text(
            first_step["title"] + "\n\n" + first_step["text"],
            reply_markup=keyboard
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        
        # Check if user needs onboarding
        if not self.is_user_onboarded(user.id):
            await self.show_onboarding(update)
            return
        
        # Regular start message for existing users
        keyboard = [
            [
                InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary"),
                InlineKeyboardButton("📊 Haftalık Rapor", callback_data="weekly_report")
            ],
            [
                InlineKeyboardButton("📅 Takvim", callback_data="calendar"),
                InlineKeyboardButton("❓ Yardım", callback_data="help")
            ],
            [InlineKeyboardButton("🎓 Kullanım Kılavuzu", callback_data="tutorial")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_message = (
            f"🚗 Hoş geldin {user.first_name}!\n\n"
            "Ben senin araba satış asistanınım. Ne yapmak istiyorsun?\n\n"
            "💡 Doğal dilde yazabilirsin:\n"
            "• \"500 TL yakıt aldım\"\n"
            "• \"Yarın 14:30'da randevu var\"\n"
            "• \"2018 Civic ne kadar eder?\""
        )
        
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /yardim command"""
        help_text = (
            "🤖 Araba Satış Asistanı Yardım\n\n"
            
            "💰 FİNANSAL İŞLEMLER:\n"
            "• \"500 TL benzin aldım\"\n"
            "• \"350.000 TL araba sattım\"\n"
            "• \"Bu hafta ne kadar kazandım?\"\n\n"
            
            "📅 RANDEVU SİSTEMİ:\n"
            "• \"Yarın 14:30'da müşteri randevusu\"\n"
            "• \"Bugün 16:00'da test sürüşü\"\n"
            "• \"2 saat sonra ekspertiz\"\n\n"
            
            "🤖 ARABA UZMANI:\n"
            "• \"2018 Civic ne kadara satarım?\"\n"
            "• \"Hangi markalar daha karlı?\"\n"
            "• \"BMW mu Mercedes mi?\"\n\n"
            
            "⚡ HIZLI KOMUTLAR:\n"
            "/start - Ana menü\n"
            "/yardim - Bu yardım menüsü\n"
            "/tutorial - Kullanım kılavuzu\n\n"
            
            "💡 Komut yazmaya gerek yok, doğal dilde konuş!"
        )
        
        await update.message.reply_text(help_text)

    async def tutorial_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tutorial command"""
        await self.show_onboarding(update)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        # Onboarding steps
        if query.data.startswith("onboard_step_"):
            step = int(query.data.split("_")[-1])
            
            tutorial_steps = [
                {
                    "title": "💰 Finansal Takip Nasıl Kullanılır?",
                    "text": (
                        "Mali işlemlerinizi doğal dille girebilirsiniz:\n\n"
                        "✅ Örnekler:\n"
                        "• \"350.000 TL Civic sattım\"\n"
                        "• \"15.000 TL galeri kirası ödedim\"\n"
                        "• \"500 TL yakıt aldım\"\n\n"
                        "Bot otomatik olarak gelir/gider kategorisine ayırır.\n\n"
                        "📊 Raporlar için:\n"
                        "• \"Bu hafta ne kadar kazandım?\"\n"
                        "• \"Aylık durum raporu\""
                    ),
                    "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_2")]]
                },
                {
                    "title": "📅 Randevu Sistemi Nasıl Çalışır?",
                    "text": (
                        "Randevularınızı doğal dille ekleyebilirsiniz:\n\n"
                        "✅ Örnekler:\n"
                        "• \"Yarın 14:30'da müşteri randevusu\"\n"
                        "• \"Bugün 16:00'da BMW test sürüşü\"\n"
                        "• \"2 saat sonra ekspertiz randevusu\"\n\n"
                        "🔔 Randevular otomatik olarak:\n"
                        "• Google Takvime eklenir\n"
                        "• 10 dakika önceden hatırlatılır\n\n"
                        "📱 Takviminizi telefonunuzda görebilirsiniz."
                    ),
                    "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_3")]]
                },
                {
                    "title": "🤖 Araba Uzmanı Danışmanlık",
                    "text": (
                        "Araba alım-satımıyla ilgili tüm sorularınızı sorabilirsiniz:\n\n"
                        "✅ Soru örnekleri:\n"
                        "• \"2018 Civic ne kadara satarım?\"\n"
                        "• \"Hangi markalar daha karlı?\"\n"
                        "• \"BMW mu Mercedes mi tercih edilir?\"\n"
                        "• \"Müşteri 300.000 TL teklif etti, kabul edeyim mi?\"\n\n"
                        "🎯 Yapay zeka uzmanım:\n"
                        "• Piyasa fiyatları hakkında bilgi verir\n"
                        "• Pazarlık stratejileri önerir\n"
                        "• Karlılık analizleri yapar"
                    ),
                    "keyboard": [[InlineKeyboardButton("▶️ Devam Et", callback_data="onboard_step_4")]]
                },
                {
                    "title": "🎉 Tebrikler! Hazırsınız!",
                    "text": (
                        "Artık tüm özellikleri kullanabilir