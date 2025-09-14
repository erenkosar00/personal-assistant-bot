"""
Araba Satış Asistanı v3.1 - Production Ready
Tüm deployment sorunları çözülmüş versiyon
"""
import os
import logging
import base64
import json
import re
import sqlite3
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# Optional imports with graceful fallback
try:
    import pytz
    TIMEZONE_AVAILABLE = True
except ImportError:
    TIMEZONE_AVAILABLE = False

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

try:
    import google.generativeai as genai
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class CarDealerBot:
    def __init__(self):
        # Telegram Token - Required
        self.token = os.environ.get("TELEGRAM_TOKEN")
        if not self.token:
            raise ValueError("TELEGRAM_TOKEN environment variable required!")
        
        # Database - Use /tmp for Railway compatibility
        self.db_path = Path("/tmp") / "car_dealer.db"
        self.setup_database()
        
        # Google Calendar - Optional
        self.calendar_service = None
        self.calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
        self.setup_google_calendar()
        
        # Gemini AI - Optional
        self.gemini_model = None
        self.chat_sessions = {}
        self.max_sessions = 100  # Prevent memory leak
        self.setup_gemini_ai()
        
        # Timezone handling
        self.timezone = self.get_timezone()
        
    def get_timezone(self):
        """Get timezone with fallback"""
        if TIMEZONE_AVAILABLE:
            return pytz.timezone('Europe/Istanbul')
        else:
            # Fallback - assume UTC+3 for Turkey
            return None
            
    def get_current_time(self):
        """Get current time with timezone handling"""
        if self.timezone:
            return datetime.now(self.timezone)
        else:
            # Fallback to UTC+3
            return datetime.utcnow() + timedelta(hours=3)
        
    def setup_database(self):
        """Database setup with error handling"""
        try:
            # Ensure directory exists
            os.makedirs(self.db_path.parent, exist_ok=True)
            
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Transactions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    account_type TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    date TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    onboarded BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create indexes for performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)')
            
            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database setup failed: {e}")
            raise
        
    def setup_google_calendar(self):
        """Google Calendar setup with error handling"""
        if not GOOGLE_AVAILABLE:
            logger.warning("Google Calendar libraries not available")
            return
            
        try:
            credentials_base64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64")
            if credentials_base64 and self.calendar_id:
                creds_json_str = base64.b64decode(credentials_base64).decode('utf-8')
                creds_json = json.loads(creds_json_str)
                scopes = ['https://www.googleapis.com/auth/calendar']
                
                credentials = service_account.Credentials.from_service_account_info(
                    creds_json, scopes=scopes
                )
                
                self.calendar_service = build('calendar', 'v3', credentials=credentials)
                logger.info("Google Calendar initialized successfully")
            else:
                logger.info("Google Calendar credentials not provided - feature disabled")
        except Exception as e:
            logger.error(f"Google Calendar setup failed: {e}")
            self.calendar_service = None
            
    def setup_gemini_ai(self):
        """Gemini AI setup with error handling"""
        if not AI_AVAILABLE:
            logger.warning("Gemini AI libraries not available")
            return
            
        try:
            api_key = os.environ.get("GEMINI_API_KEY")
            if api_key:
                genai.configure(api_key=api_key)
                self.gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
                logger.info("Gemini AI initialized successfully")
            else:
                logger.info("Gemini API key not provided - feature disabled")
        except Exception as e:
            logger.error(f"Gemini AI setup failed: {e}")
            self.gemini_model = None

    def cleanup_chat_sessions(self):
        """Prevent memory leak by limiting chat sessions"""
        if len(self.chat_sessions) > self.max_sessions:
            # Remove oldest sessions
            sessions_to_remove = len(self.chat_sessions) - self.max_sessions
            oldest_keys = list(self.chat_sessions.keys())[:sessions_to_remove]
            for key in oldest_keys:
                del self.chat_sessions[key]
            logger.info(f"Cleaned up {sessions_to_remove} chat sessions")

    # === FINANCIAL FUNCTIONS ===
    
    def detect_financial_intent(self, text):
        """Detect financial transaction from text"""
        text_lower = text.lower()
        
        # Extract amount
        amount_match = re.search(r'(\d+(?:[.,]\d+)?)\s*tl', text_lower)
        if not amount_match:
            return None
        
        try:
            amount = float(amount_match.group(1).replace(',', '.'))
            if amount <= 0:
                return None
        except (ValueError, TypeError):
            return None
        
        # Determine transaction type
        income_keywords = ['kazandım', 'sattım', 'gelir', 'komisyon', 'ödeme aldım', 'satış yaptım']
        expense_keywords = ['harcadım', 'ödedim', 'aldım', 'masraf', 'gider', 'fatura', 'para harcadım']
        
        is_income = any(keyword in text_lower for keyword in income_keywords)
        is_expense = any(keyword in text_lower for keyword in expense_keywords)
        
        if is_income and not is_expense:
            transaction_type = 'gelir'
        elif is_expense and not is_income:
            transaction_type = 'gider'
        else:
            # Context-based decision
            if any(word in text_lower for word in ['satış', 'komisyon', 'kazanç']):
                transaction_type = 'gelir'
            else:
                transaction_type = 'gider'
        
        # Determine account type
        account_type = 'kisisel'  # default
        if any(word in text_lower for word in ['araba', 'araç', 'galeri', 'civic', 'bmw', 'mercedes', 'toyota', 'honda']):
            account_type = 'araba'
        elif any(word in text_lower for word in ['emlak', 'ev', 'daire', 'kiralama', 'satış komisyonu', 'gayrimenkul']):
            account_type = 'emlak'
        
        # Determine category
        category = self.determine_category(text_lower, account_type, transaction_type)
        
        # Extract description
        description = re.sub(r'\d+(?:[.,]\d+)?\s*tl', '', text, flags=re.IGNORECASE).strip()
        description = re.sub(r'\s+', ' ', description)[:200]
        
        return {
            'account_type': account_type,
            'transaction_type': transaction_type,
            'amount': amount,
            'category': category,
            'description': description or 'İşlem'
        }
    
    def determine_category(self, text, account_type, transaction_type):
        """Determine transaction category"""
        categories = {
            'araba': {
                'gelir': ['satış', 'servis', 'komisyon', 'diğer'],
                'gider': ['alım', 'yakıt', 'bakım', 'kira', 'personel', 'reklam', 'sigorta', 'diğer']
            },
            'emlak': {
                'gelir': ['satış_komisyonu', 'kiralama_komisyonu', 'danışmanlık', 'emlak_satış', 'diğer'],
                'gider': ['pazarlama', 'ulaşım', 'ofis', 'lisans', 'reklam', 'diğer']
            },
            'kisisel': {
                'gelir': ['maaş', 'kira_geliri', 'yatırım', 'borç_ödeme', 'hediye', 'diğer'],
                'gider': ['yemek', 'ulaşım', 'ev', 'eğlence', 'sağlık', 'alışveriş', 'fatura', 'diğer']
            }
        }
        
        available_categories = categories.get(account_type, {}).get(transaction_type, ['diğer'])
        
        for category in available_categories:
            if category == 'diğer':
                continue
            
            # Check if category keywords exist in text
            category_words = category.replace('_', ' ').split()
            if any(word in text for word in category_words):
                return category
                
            # Special keyword matching
            if category == 'yakıt' and any(word in text for word in ['benzin', 'mazot', 'lpg']):
                return category
            elif category == 'satış' and any(word in text for word in ['sattım', 'satış']):
                return category
            elif category == 'alım' and any(word in text for word in ['aldım', 'araba aldım']):
                return category
        
        return 'diğer'

    def add_transaction(self, user_id, transaction):
        """Add financial transaction with error handling"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO transactions (user_id, account_type, transaction_type, amount, category, description, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                transaction['account_type'],
                transaction['transaction_type'],
                transaction['amount'],
                transaction['category'],
                transaction['description'],
                self.get_current_time().date().isoformat()
            ))
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"Transaction add failed: {e}")
            return False

    def get_financial_summary(self, user_id, period='week', account_type=None):
        """Get financial summary with error handling"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Date filters
            now = self.get_current_time()
            if period == 'day':
                date_filter = now.date()
                query_date = "date = ?"
            elif period == 'week':
                date_filter = (now - timedelta(days=7)).date()
                query_date = "date >= ?"
            elif period == 'month':
                date_filter = now.replace(day=1).date()
                query_date = "date >= ?"
            else:  # year
                date_filter = now.replace(month=1, day=1).date()
                query_date = "date >= ?"
            
            base_query = f"SELECT transaction_type, SUM(amount), category FROM transactions WHERE user_id = ? AND {query_date}"
            params = [user_id, date_filter.isoformat()]
            
            if account_type:
                base_query += " AND account_type = ?"
                params.append(account_type)
            
            base_query += " GROUP BY transaction_type, category ORDER BY SUM(amount) DESC"
            
            cursor.execute(base_query, params)
            results = cursor.fetchall()
            
            # Get totals
            total_query = f"SELECT transaction_type, SUM(amount) FROM transactions WHERE user_id = ? AND {query_date}"
            total_params = [user_id, date_filter.isoformat()]
            
            if account_type:
                total_query += " AND account_type = ?"
                total_params.append(account_type)
                
            total_query += " GROUP BY transaction_type"
            
            cursor.execute(total_query, total_params)
            totals = cursor.fetchall()
            
            conn.close()
            return results, totals
            
        except Exception as e:
            logger.error(f"Financial summary failed: {e}")
            return [], []

    def format_financial_report(self, results, totals, period, account_type=None):
        """Format financial report"""
        if not results and not totals:
            period_names = {'day': 'bugün', 'week': 'bu hafta', 'month': 'bu ay', 'year': 'bu yıl'}
            return f"📊 {period_names.get(period, period).title()} hiç işlem yok."
        
        period_names = {'day': 'Bugün', 'week': 'Bu Hafta', 'month': 'Bu Ay', 'year': 'Bu Yıl'}
        account_names = {'araba': 'Araba İşi', 'emlak': 'Emlak İşi', 'kisisel': 'Kişisel'}
        
        report = f"📊 {period_names.get(period, period)} Mali Durum"
        if account_type:
            report += f" - {account_names.get(account_type, account_type)}"
        report += "\n" + "="*40 + "\n\n"
        
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
            income_categories = []
            expense_categories = []
            
            for transaction_type, amount, category in results:
                category_display = category.replace('_', ' ').title()
                if transaction_type == 'gelir':
                    income_categories.append(f"  • {category_display}: {amount:,.0f} TL")
                else:
                    expense_categories.append(f"  • {category_display}: {amount:,.0f} TL")
            
            if income_categories:
                report += "📈 Gelir Detayları:\n" + "\n".join(income_categories) + "\n\n"
            
            if expense_categories:
                report += "📉 Gider Detayları:\n" + "\n".join(expense_categories) + "\n\n"
        
        return report

    # === CALENDAR FUNCTIONS ===
    
    def parse_time_from_text(self, text):
        """Parse time from text with fallback for missing pytz"""
        now = self.get_current_time()
        
        patterns = [
            (r'yarın\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: (now + timedelta(days=1)).replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
            (r'bugün\s+(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
            (r'(?:saat\s+)?(\d{1,2}):(\d{2})', lambda h, m: now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)),
            (r'yarın\s+(\d{1,2})\'?(?:de|da|te|ta)', lambda h: (now + timedelta(days=1)).replace(hour=int(h), minute=0, second=0, microsecond=0)),
            (r'bugün\s+(\d{1,2})\'?(?:de|da|te|ta)', lambda h: now.replace(hour=int(h), minute=0, second=0, microsecond=0)),
            (r'(\d{1,2})\'?(?:de|da|te|ta)', lambda h: now.replace(hour=int(h), minute=0, second=0, microsecond=0)),
            (r'(\d+)\s+saat\s+sonra', lambda h: now + timedelta(hours=int(h))),
            (r'(\d+)\s+dakika\s+sonra', lambda m: now + timedelta(minutes=int(m))),
        ]
        
        for pattern, time_func in patterns:
            match = re.search(pattern, text.lower())
            if match:
                try:
                    parsed_time = time_func(*match.groups())
                    
                    # If time is in past, assume next day
                    if parsed_time <= now and 'yarın' not in text.lower() and 'sonra' not in text.lower():
                        parsed_time += timedelta(days=1)
                    
                    # Remove time expression from message
                    message = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
                    message = re.sub(r'\s+', ' ', message)
                    
                    return parsed_time, message, match.group(0)
                    
                except Exception as e:
                    logger.error(f"Time parsing error: {e}")
                    continue
        
        return None, text, None

    def create_calendar_event(self, title, start_time, duration_minutes=30):
        """Create Google Calendar event with error handling"""
        if not self.calendar_service or not self.calendar_id:
            logger.error("Calendar service not available")
            return False
        
        try:
            end_time = start_time + timedelta(minutes=duration_minutes)
            
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
            
            result = self.calendar_service.events().insert(
                calendarId=self.calendar_id, 
                body=event
            ).execute()
            
            logger.info(f"Calendar event created: {result.get('id', 'unknown')}")
            return True
            
        except Exception as e:
            logger.error(f"Calendar event creation failed: {e}")
            return False

    def format_turkish_datetime(self, dt):
        """Format datetime in Turkish with fallback"""
        try:
            months = {
                1: 'Ocak', 2: 'Şubat', 3: 'Mart', 4: 'Nisan',
                5: 'Mayıs', 6: 'Haziran', 7: 'Temmuz', 8: 'Ağustos',
                9: 'Eylül', 10: 'Ekim', 11: 'Kasım', 12: 'Aralık'
            }
            
            days = {
                0: 'Pazartesi', 1: 'Salı', 2: 'Çarşamba', 3: 'Perşembe',
                4: 'Cuma', 5: 'Cumartesi', 6: 'Pazar'
            }
            
            return f"{dt.day} {months[dt.month]} {dt.year}, {days[dt.weekday()]}, {dt.strftime('%H:%M')}"
        except Exception as e:
            logger.error(f"Date formatting error: {e}")
            return dt.strftime('%d.%m.%Y %H:%M')

    # === AI FUNCTIONS ===
    
    def is_car_related_question(self, text):
        """Check if question is car-related"""
        car_keywords = [
            'araba', 'araç', 'otomobil', 'honda', 'toyota', 'bmw', 'mercedes',
            'civic', 'corolla', 'focus', 'golf', 'passat', 'a4', 'c180',
            'fiat', 'renault', 'peugeot', 'hyundai', 'nissan', 'ford',
            'satış', 'alım', 'piyasa', 'fiyat', 'değer', 'model', 'yıl',
            'km', 'motor', 'vites', 'hasar', 'tramer', 'ekspertiz',
            'muayene', 'plaka', 'ruhsat', 'sigorta', 'kasko', 'galeri'
        ]
        
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in car_keywords)

    async def get_car_expert_response(self, text):
        """Get AI car expert response with async support"""
        if not self.gemini_model:
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
            
            # Use async generate if available, else sync
            try:
                response = await self.gemini_model.generate_content_async(full_prompt)
            except:
                response = self.gemini_model.generate_content(full_prompt)
            
            return response.text
            
        except Exception as e:
            logger.error(f"AI response failed: {e}")
            return "AI servisinde geçici bir sorun var. Lütfen daha sonra tekrar deneyin."

    # === USER MANAGEMENT ===
    
    def is_user_onboarded(self, user_id):
        """Check if user completed onboarding"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('SELECT onboarded FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            conn.close()
            
            return result and result[0] == 1
        except Exception as e:
            logger.error(f"User onboarding check failed: {e}")
            return False

    def mark_user_onboarded(self, user_id, username, first_name):
        """Mark user as onboarded"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO users (id, username, first_name, onboarded)
                VALUES (?, ?, ?, 1)
            ''', (user_id, username or '', first_name or ''))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Mark onboarded failed: {e}")

    # === INTENT DETECTION ===
    
    def detect_intent(self, text):
        """Detect user intent"""
        text_lower = text.lower()
        
        # Financial transaction
        if re.search(r'\d+(?:[.,]\d+)?\s*tl', text_lower):
            return 'financial', text
        
        # Financial report
        report_keywords = ['ne kadar', 'toplam', 'özet', 'rapor', 'durum', 'hesap']
        if any(keyword in text_lower for keyword in report_keywords):
            return 'financial_report', text
        
        # Reminder intent
        reminder_keywords = ['hatırlat', 'randevu', 'toplantı', 'etkinlik', 'görüşme', 'buluşma']
        time_patterns = [r'\d{1,2}:\d{2}', r'yarın', r'bugün', r'saat', r'sonra']
        
        has_reminder_keyword = any(keyword in text_lower for keyword in reminder_keywords)
        has_time_pattern = any(re.search(pattern, text_lower) for pattern in time_patterns)
        
        if has_reminder_keyword or has_time_pattern:
            return 'reminder', text
        
        # Car related question
        if self.is_car_related_question(text):
            return 'car_expert', text
        
        # Other intents
        if any(keyword in text_lower for keyword in ['takvim', 'ajanda', 'program']):
            return 'calendar', text
            
        if any(keyword in text_lower for keyword in ['yeni konuşma', 'sıfırla', 'temizle', 'reset']):
            return 'reset_chat', text
            
        if any(keyword in text_lower for keyword in ['yardım', 'help', 'nasıl']):
            return 'help', text
        
        return 'chat', text

    # === TELEGRAM HANDLERS ===
    
    async def show_onboarding(self, update: Update):
        """Show onboarding tutorial"""
        welcome_text = (
            "🚗 Araba Satış Asistanına Hoş Geldiniz!\n\n"
            "Ben sizin kişisel araba satış asistanınızım. 4 ana özelliğim var:\n\n"
            "💰 FİNANSAL TAKİP:\n"
            "• '350.000 TL Civic sattım'\n"
            "• '15.000 TL galeri kirası ödedim'\n"
            "• '500 TL yakıt aldım'\n"
            "• 'Bu hafta ne kadar kazandım?'\n\n"
            "📅 RANDEVU SİSTEMİ:\n"
            "• 'Yarın 14:30'da müşteri randevusu'\n"
            "• 'Bugün 16:00'da BMW test sürüşü'\n"
            "• '2 saat sonra ekspertiz randevusu'\n\n"
            "🤖 ARABA UZMANI:\n"
            "• '2018 Civic ne kadara satarım?'\n"
            "• 'Hangi markalar daha karlı?'\n"
            "• 'BMW mu Mercedes mi tercih edilir?'\n\n"
            "💬 GENEL SOHBET:\n"
            "• Herhangi bir konu hakkında konuşabiliriz\n\n"
            "💡 Komut yazmaya gerek yok, doğal Türkçe ile konuş!"
        )
        
        keyboard = [[InlineKeyboardButton("✅ Anladım, Başlayalım!", callback_data="onboard_complete")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        
        # Check onboarding
        if not self.is_user_onboarded(user.id):
            await self.show_onboarding(update)
            return
        
        # Main menu
        keyboard = [
            [
                InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary"),
                InlineKeyboardButton("📊 Haftalık Rapor", callback_data="weekly_report")
            ],
            [
                InlineKeyboardButton("📅 Takvim", callback_data="calendar"),
                InlineKeyboardButton("🤖 Araba Uzmanı", callback_data="car_expert_info")
            ],
            [
                InlineKeyboardButton("❓ Yardım", callback_data="help"),
                InlineKeyboardButton("🎓 Rehber", callback_data="tutorial")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🚗 Hoş geldin {user.first_name}!\n\n"
            "Ben senin araba satış asistanınım. Ne yapmak istiyorsun?\n\n"
            "💡 Doğal dilde yazabilirsin:\n"
            "• '500 TL yakıt aldım'\n"
            "• 'Yarın 14:30'da randevu var'\n"
            "• '2018 Civic ne kadar eder?'",
            reply_markup=reply_markup
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle help command"""
        help_text = (
            "🤖 Araba Satış Asistanı Yardım\n\n"
            "💰 FİNANSAL İŞLEMLER:\n"
            "• '500 TL benzin aldım'\n"
            "• '350.000 TL araba sattım'\n"
            "• 'Bu hafta ne kadar kazandım?'\n\n"
            "📅 RANDEVU SİSTEMİ:\n"
            "• 'Yarın 14:30'da müşteri randevusu'\n"
            "• 'Bugün 16:00'da test sürüşü'\n"
            "• '2 saat sonra ekspertiz'\n\n"
            "🤖 ARABA UZMANI:\n"
            "• '2018 Civic ne kadara satarım?'\n"
            "• 'Hangi markalar daha karlı?'\n"
            "• 'BMW mu Mercedes mi?'\n\n"
            "⚡ KOMUTLAR:\n"
            "/start - Ana menü\n"
            "/yardim - Bu yardım menüsü\n\n"
            "💡 Komut yazmaya gerek yok, doğal dilde konuş!"
        )
        
        await update.message.reply_text(help_text)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        try:
            user_id = query.from_user.id
            
            if query.data == "onboard_complete":
                user = query.from_user
                self.mark_user_onboarded(user.id, user.username, user.first_name)
                
                keyboard = [
                    [
                        InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary"),
                        InlineKeyboardButton("📊 Haftalık Rapor", callback_data="weekly_report")
                    ],
                    [
                        InlineKeyboardButton("📅 Takvim", callback_data="calendar"),
                        InlineKeyboardButton("🤖 Araba Uzmanı", callback_data="car_expert_info")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    "🎉 Harika! Artık botu kullanmaya hazırsın.\n\n"
                    "Bana doğal dilde yazabilirsin:\n"
                    "• '500 TL yakıt aldım'\n"
                    "• 'Yarın 14:30'da randevu'\n"
                    "• '2018 Civic kaça satarım?'",
                    reply_markup=reply_markup
                )
                
            elif query.data == "financial_summary":
                results, totals = self.get_financial_summary(user_id, 'week')
                report = self.format_financial_report(results, totals, 'week')
                await query.edit_message_text(report)
                
            elif query.data == "weekly_report":
                results, totals = self.get_financial_summary(user_id, 'week')
                report = self.format_financial_report(results, totals, 'week')
                await query.edit_message_text(report)
                
            elif query.data == "calendar":
                if self.calendar_id:
                    calendar_url = f"https://calendar.google.com/calendar/u/0?cid={self.calendar_id}"
                    await query.edit_message_text(
                        f"📅 Takviminizi görüntülemek için tıklayın:\n{calendar_url}"
                    )
                else:
                    await query.edit_message_text("📅 Takvim servisi şu anda mevcut değil.")
                
            elif query.data == "car_expert_info":
                await query.edit_message_text(
                    "🤖 Araba Uzmanı Danışmanlık\n\n"
                    "Bana araba ile ilgili herhangi bir soru sorabilirsiniz:\n\n"
                    "• '2018 Civic ne kadara satarım?'\n"
                    "• 'Hangi markalar daha karlı?'\n"
                    "• 'BMW mu Mercedes mi tercih edilir?'\n"
                    "• 'Müşteri 300.000 TL teklif etti, kabul edeyim mi?'\n\n"
                    "Sorularınızı doğal dilde yazın!"
                )
                
            elif query.data == "help":
                help_text = (
                    "🤖 Hızlı Yardım\n\n"
                    "💰 Finansal: '500 TL benzin aldım'\n"
                    "📅 Randevu: 'Yarın 14:30'da toplantı'\n"
                    "🤖 Uzman: '2018 Civic ne kadar eder?'\n\n"
                    "Detaylı yardım: /yardim"
                )
                await query.edit_message_text(help_text)
                
            elif query.data == "tutorial":
                await query.edit_message_text(
                    "🚗 Araba Satış Asistanı Rehberi\n\n"
                    "TEMEL KULLANIM:\n\n"
                    "💰 Mali işlemler için:\n"
                    "• '350.000 TL Civic sattım'\n"
                    "• '500 TL yakıt aldım'\n"
                    "• '15.000 TL kira ödedim'\n\n"
                    "📅 Randevular için:\n"
                    "• 'Yarın 14:30'da müşteri randevusu'\n"
                    "• 'Bugün 16:00'da test sürüşü'\n\n"
                    "🤖 Araba soruları için:\n"
                    "• '2018 Civic ne kadar eder?'\n"
                    "• 'Hangi marka daha karlı?'\n\n"
                    "📊 Raporlar için:\n"
                    "• 'Bu hafta ne kadar kazandım?'\n"
                    "• 'Bu ay durum nasıl?'\n\n"
                    "💡 Komut yazmaya gerek yok!"
                )
                
        except Exception as e:
            logger.error(f"Button callback error: {e}")
            await query.edit_message_text("❌ Bir hata oluştu. Lütfen tekrar deneyin.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Main message handler with error handling"""
        try:
            user_id = update.effective_user.id
            text = update.message.text
            
            if not text or len(text.strip()) == 0:
                return
            
            # Check onboarding
            if not self.is_user_onboarded(user_id):
                await self.show_onboarding(update)
                return
            
            # Detect intent
            intent, text_data = self.detect_intent(text)
            
            if intent == 'financial':
                await self.handle_financial_transaction(update, text_data)
                
            elif intent == 'financial_report':
                await self.handle_financial_report(update, text_data)
                
            elif intent == 'reminder':
                await self.handle_reminder(update, text_data)
                
            elif intent == 'car_expert':
                await self.handle_car_expert(update, text_data)
                
            elif intent == 'calendar':
                await self.handle_calendar_request(update)
                
            elif intent == 'reset_chat':
                await self.handle_reset_chat(update)
                
            elif intent == 'help':
                await self.help_command(update, context)
                
            else:  # chat
                await self.handle_general_chat(update, text_data)
                
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            await update.message.reply_text(
                "❌ Bir hata oluştu. Lütfen tekrar deneyin."
            )

    async def handle_financial_transaction(self, update: Update, text: str):
        """Handle financial transaction"""
        user_id = update.effective_user.id
        
        transaction = self.detect_financial_intent(text)
        if not transaction:
            await update.message.reply_text(
                "💰 Para miktarını anlayamadım.\n\n"
                "Örnekler:\n"
                "• '500 TL benzin aldım'\n"
                "• '350.000 TL araba sattım'\n"
                "• '15.000 TL kira ödedim'"
            )
            return
        
        success = self.add_transaction(user_id, transaction)
        
        if success:
            account_names = {'araba': 'Araba İşi', 'emlak': 'Emlak İşi', 'kisisel': 'Kişisel'}
            type_emoji = '📈' if transaction['transaction_type'] == 'gelir' else '📉'
            
            response = (
                f"{type_emoji} İşlem kaydedildi!\n\n"
                f"💼 Hesap: {account_names[transaction['account_type']]}\n"
                f"💰 Miktar: {transaction['amount']:,.0f} TL\n"
                f"📁 Kategori: {transaction['category'].title()}\n"
                f"📝 Açıklama: {transaction['description']}\n"
                f"🏷️ Tür: {transaction['transaction_type'].title()}"
            )
            
            keyboard = [
                [InlineKeyboardButton("📊 Bu Hafta Özet", callback_data="weekly_report")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(response, reply_markup=reply_markup)
        else:
            await update.message.reply_text("❌ İşlem kaydedilemedi. Lütfen tekrar deneyin.")

    async def handle_financial_report(self, update: Update, text: str):
        """Handle financial report request"""
        user_id = update.effective_user.id
        text_lower = text.lower()
        
        # Period detection
        period = 'week'
        if 'bugün' in text_lower or 'gün' in text_lower:
            period = 'day'
        elif 'ay' in text_lower or 'aylık' in text_lower:
            period = 'month'
        elif 'yıl' in text_lower:
            period = 'year'
        
        # Account type detection
        account_type = None
        if 'araba' in text_lower:
            account_type = 'araba'
        elif 'emlak' in text_lower:
            account_type = 'emlak'
        elif 'kişisel' in text_lower or 'kisisel' in text_lower:
            account_type = 'kisisel'
        
        results, totals = self.get_financial_summary(user_id, period, account_type)
        report = self.format_financial_report(results, totals, period, account_type)
        
        keyboard = [
            [
                InlineKeyboardButton("📊 Haftalık", callback_data="report_week"),
                InlineKeyboardButton("📊 Aylık", callback_data="report_month")
            ],
            [
                InlineKeyboardButton("🚗 Araba İşi", callback_data="report_araba"),
                InlineKeyboardButton("🏠 Emlak İşi", callback_data="report_emlak")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(report, reply_markup=reply_markup)

    async def handle_reminder(self, update: Update, text: str):
        """Handle reminder creation"""
        parsed_time, message, time_expr = self.parse_time_from_text(text)
        
        if not parsed_time:
            await update.message.reply_text(
                "⏰ Zaman bilgisini anlayamadım.\n\n"
                "Örnekler:\n"
                "• 'Yarın 14:30'da doktor randevusu'\n"
                "• 'Bugün 16:00'da toplantı'\n"
                "• '2 saat sonra alışveriş yap'\n"
                "• '15:45'te araba servise götür'"
            )
            return

        if not message or len(message.strip()) < 3:
            await update.message.reply_text(
                "📝 Neyi hatırlatacağımı belirtmedin.\n\n"
                "Örnek: 'Yarın 14:30'da doktor randevusu'"
            )
            return

        # Create calendar event
        success = self.create_calendar_event(message, parsed_time, 30)

        if success:
            formatted_time = self.format_turkish_datetime(parsed_time)
            
            response = (
                f"✅ Hatırlatıcın başarıyla ayarlandı!\n\n"
                f"📝 Konu: {message}\n"
                f"📅 Tarih: {formatted_time}\n"
                f"⏰ Hatırlatma: 10 dakika önce\n\n"
                f"📱 Takviminde görebilirsin!"
            )
            
            keyboard = [
                [InlineKeyboardButton("📅 Takvimi Aç", callback_data="calendar")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(response, reply_markup=reply_markup)
        else:
            await update.message.reply_text(
                "❌ Takvime eklerken sorun oluştu.\n"
                "Google Calendar ayarları kontrol edilsin."
            )

    async def handle_car_expert(self, update: Update, text: str):
        """Handle car expert consultation"""
        response = await self.get_car_expert_response(text)
        
        keyboard = [
            [InlineKeyboardButton("🤖 Başka Soru Sor", callback_data="car_expert_info")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(response, reply_markup=reply_markup)

    async def handle_calendar_request(self, update: Update):
        """Handle calendar request"""
        if self.calendar_id:
            calendar_url = f"https://calendar.google.com/calendar/u/0?cid={self.calendar_id}"
            await update.message.reply_text(
                f"📅 Takviminizi görüntülemek için tıklayın:\n{calendar_url}"
            )
        else:
            await update.message.reply_text("📅 Takvim servisi şu anda mevcut değil.")

    async def handle_reset_chat(self, update: Update):
        """Handle chat reset"""
        user_id = update.effective_user.id
        if user_id in self.chat_sessions:
            del self.chat_sessions[user_id]
        await update.message.reply_text("🤖 Sohbet geçmişi temizlendi!")

    async def handle_general_chat(self, update: Update, text: str):
        """Handle general chat with AI"""
        user_id = update.effective_user.id
        
        if not self.gemini_model:
            await update.message.reply_text(
                "💬 AI sohbet servisi şu anda mevcut değil.\n\n"
                "Şunları deneyebilirsiniz:\n"
                "• Finansal işlemler: '500 TL yakıt aldım'\n"
                "• Randevular: 'Yarın 14:30'da toplantı'\n"
                "• Araba soruları: '2018 Civic ne kadar?'"
            )
            return
        
        # Cleanup sessions if needed
        self.cleanup_chat_sessions()
        
        # Get or create chat session
        if user_id not in self.chat_sessions:
            self.chat_sessions[user_id] = self.gemini_model.start_chat()
        
        try:
            # Use sync method as async might not be available
            response = self.chat_sessions[user_id].send_message(text)
            
            # Split long responses
            response_text = response.text
            if len(response_text) > 4000:
                chunks = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(response_text)
                
        except Exception as e:
            logger.error(f"Chat error: {e}")
            await update.message.reply_text(
                "🤖 Şu anda sohbet servisinde sorun var. Daha sonra tekrar deneyin."
            )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler"""
        logger.error(f"Update {update} caused error {context.error}")

def main():
    """Main function"""
    try:
        bot = CarDealerBot()
        
        # Create application
        application = Application.builder().token(bot.token).build()
        
        # Set bot commands
        async def post_init(app):
            try:
                await app.bot.set_my_commands([
                    BotCommand("start", "Botu başlatır"),
                    BotCommand("yardim", "Yardım menüsü")
                ])
                logger.info("Bot commands set successfully")
            except Exception as e:
                logger.error(f"Failed to set bot commands: {e}")
        
        application.post_init = post_init
        
        # Add handlers
        application.add_handler(CommandHandler("start", bot.start_command))
        application.add_handler(CommandHandler("yardim", bot.help_command))
        application.add_handler(CommandHandler("help", bot.help_command))
        
        application.add_handler(CallbackQueryHandler(bot.button_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
        
        # Add error handler
        application.add_error_handler(bot.error_handler)
        
        # Status messages
        print("🚗 Araba Satış Asistanı v3.1 başlatılıyor...")
        print("🤖 Bot hazır! Kullanıcılar /start ile başlayabilir.")
        print()
        print("📊 Özellik Durumu:")
        print(f"  💰 Finansal takip: ✅ Aktif")
        print(f"  📅 Google Calendar: {'✅ Aktif' if bot.calendar_service else '⚠️  Pasif (credentials gerekli)'}")
        print(f"  🤖 AI Uzman: {'✅ Aktif' if bot.gemini_model else '⚠️  Pasif (API key gerekli)'}")
        print(f"  🌐 Timezone: {'✅ pytz' if TIMEZONE_AVAILABLE else '⚠️  Fallback UTC+3'}")
        print()
        
        # Run bot
        application.run_polling(
            poll_interval=1,
            timeout=10,
            bootstrap_retries=5
        )
        
    except Exception as e:
        logger.error(f"Bot startup failed: {e}")
        print(f"❌ Bot başlatılamadı: {e}")

if __name__ == "__main__":
    main()