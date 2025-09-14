"""
Araba Satış Asistanı - Çalışan Versiyon
Syntax hatası olmadan, temel özelliklerle
"""
import os
import logging
import sqlite3
import re
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CarDealerBot:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_TOKEN")
        self.db_path = Path.home() / ".telegram_assistant" / "car_dealer.db"
        self.setup_database()
        
    def setup_database(self):
        os.makedirs(self.db_path.parent, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
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
        logger.info("Database ready")

    def parse_financial_text(self, text):
        text_lower = text.lower().strip()
        
        # Miktar çıkarma
        amount_match = re.search(r'(\d+(?:[.,]\d+)?)\s*tl', text_lower)
        if not amount_match:
            return None
        
        try:
            amount = float(amount_match.group(1).replace(',', '.'))
            if amount <= 0:
                return None
        except:
            return None
        
        # Gelir/Gider belirleme
        income_keywords = ['sattım', 'kazandım', 'gelir', 'komisyon', 'satış']
        expense_keywords = ['aldım', 'harcadım', 'ödedim', 'masraf', 'gider']
        
        is_income = any(word in text_lower for word in income_keywords)
        is_expense = any(word in text_lower for word in expense_keywords)
        
        transaction_type = 'gelir' if (is_income and not is_expense) else 'gider'
        
        # Kategori belirleme
        category = self.determine_category(text_lower, transaction_type)
        
        # Açıklama çıkarma
        description = re.sub(r'\d+(?:[.,]\d+)?\s*tl', '', text, flags=re.IGNORECASE).strip()
        description = re.sub(r'\s+', ' ', description)[:200]
        
        return {
            'amount': amount,
            'type': transaction_type,
            'category': category,
            'description': description or 'İşlem'
        }
    
    def determine_category(self, text, transaction_type):
        if transaction_type == 'gelir':
            if any(word in text for word in ['sattım', 'satış']):
                return 'satış'
            elif 'komisyon' in text:
                return 'komisyon'
            else:
                return 'diğer'
        else:  # gider
            if any(word in text for word in ['yakıt', 'benzin']):
                return 'yakıt'
            elif any(word in text for word in ['aldım', 'araba']):
                return 'alım'
            elif 'kira' in text:
                return 'kira'
            else:
                return 'diğer'

    def add_transaction(self, user_id, transaction):
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
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if period == 'week':
                date_filter = (datetime.now() - timedelta(days=7)).date()
            elif period == 'month':
                date_filter = datetime.now().replace(day=1).date()
            else:
                date_filter = datetime.now().date()
            
            cursor.execute('''
                SELECT type, SUM(amount), category
                FROM transactions 
                WHERE user_id = ? AND date >= ?
                GROUP BY type, category
                ORDER BY SUM(amount) DESC
            ''', (user_id, date_filter.isoformat()))
            
            results = cursor.fetchall()
            
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

    def format_financial_report(self, results, totals, period):
        if not results and not totals:
            period_names = {'week': 'bu hafta', 'month': 'bu ay', 'today': 'bugün'}
            return f"📊 {period_names.get(period, period).title()} hiç işlem yok."
        
        period_names = {'week': 'Bu Hafta', 'month': 'Bu Ay', 'today': 'Bugün'}
        report = f"📊 {period_names.get(period, period)} Mali Durum\n"
        report += "=" * 30 + "\n\n"
        
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
        welcome_text = (
            "🚗 Araba Satış Asistanına Hoş Geldiniz!\n\n"
            "Ben sizin kişisel araba satış asistanınızım.\n"
            "3 ana özelliğim var:\n\n"
            "💰 Finansal takip\n"
            "📅 Randevu yönetimi (yakında)\n"
            "🤖 Araba uzmanı danışmanlık (yakında)\n\n"
            "Şimdi nasıl kullanacağınızı göstereyim...\n\n"
            "FINANSAL TAKİP:\n"
            "• '350.000 TL Civic sattım'\n"
            "• '15.000 TL galeri kirası ödedim'\n"
            "• '500 TL yakıt aldım'\n\n"
            "RAPORLAR:\n"
            "• 'Bu hafta ne kadar kazandım?'\n"
            "• 'Aylık durum raporu'\n\n"
            "💡 Komut yazmaya gerek yok, doğal dilde yazın!"
        )
        
        keyboard = [[InlineKeyboardButton("✅ Anladım, Başlayalım!", callback_data="onboard_complete")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        if not self.is_user_onboarded(user.id):
            await self.show_onboarding(update)
            return
        
        keyboard = [
            [
                InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary"),
                InlineKeyboardButton("📊 Haftalık Rapor", callback_data="weekly_report")
            ],
            [
                InlineKeyboardButton("❓ Yardım", callback_data="help"),
                InlineKeyboardButton("🎓 Rehber", callback_data="tutorial")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_message = (
            f"🚗 Hoş geldin {user.first_name}!\n\n"
            "Ben senin araba satış asistanınım. Ne yapmak istiyorsun?\n\n"
            "💡 Doğal dilde yazabilirsin:\n"
            "• '500 TL yakıt aldım'\n"
            "• 'Bu hafta ne kadar kazandım?'"
        )
        
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "🤖 Araba Satış Asistanı Yardım\n\n"
            "💰 FİNANSAL İŞLEMLER:\n"
            "• '500 TL benzin aldım'\n"
            "• '350.000 TL araba sattım'\n"
            "• 'Bu hafta ne kadar kazandım?'\n\n"
            "⚡ KOMUTLAR:\n"
            "/start - Ana menü\n"
            "/yardim - Bu yardım menüsü\n\n"
            "💡 Komut yazmaya gerek yok, doğal dilde konuş!"
        )
        
        await update.message.reply_text(help_text)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        if query.data == "onboard_complete":
            user = query.from_user
            self.mark_user_onboarded(user.id, user.username, user.first_name)
            
            keyboard = [
                [
                    InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary"),
                    InlineKeyboardButton("📊 Haftalık Rapor", callback_data="weekly_report")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "🎉 Harika! Artık botu kullanmaya hazırsın.\n\n"
                "Bana doğal dilde yazabilirsin:\n"
                "• '500 TL yakıt aldım'\n"
                "• 'Bu hafta ne kadar kazandım?'",
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
            
        elif query.data == "help":
            help_text = (
                "🤖 Hızlı Yardım\n\n"
                "💰 Finansal: '500 TL benzin aldım'\n"
                "📊 Rapor: 'Bu hafta ne kadar kazandım?'\n\n"
                "Detaylı yardım: /yardim"
            )
            await query.edit_message_text(help_text)
            
        elif query.data == "tutorial":
            await self.show_onboarding_from_callback(query)

    async def show_onboarding_from_callback(self, query):
        welcome_text = (
            "🚗 Araba Satış Asistanı Rehberi\n\n"
            "TEMEL KULLANIM:\n\n"
            "💰 Mali işlemler için:\n"
            "• '350.000 TL Civic sattım'\n"
            "• '500 TL yakıt aldım'\n"
            "• '15.000 TL kira ödedim'\n\n"
            "📊 Raporlar için:\n"
            "• 'Bu hafta ne kadar kazandım?'\n"
            "• 'Bu ay durum nasıl?'\n\n"
            "💡 Komut yazmaya gerek yok!"
        )
        
        keyboard = [[InlineKeyboardButton("✅ Anladım", callback_data="onboard_complete")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(welcome_text, reply_markup=reply_markup)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user_id = update.effective_user.id
            text = update.message.text
            
            if not self.is_user_onboarded(user_id):
                await self.show_onboarding(update)
                return
            
            # Mali işlem kontrolü
            transaction = self.parse_financial_text(text)
            if transaction:
                success = self.add_transaction(user_id, transaction)
                
                if success:
                    type_emoji = "📈" if transaction['type'] == 'gelir' else "📉"
                    response = (
                        f"{type_emoji} İşlem kaydedildi!\n\n"
                        f"💰 Miktar: {transaction['amount']:,.0f} TL\n"
                        f"📁 Kategori: {transaction['category'].title()}\n"
                        f"📝 Açıklama: {transaction['description']}\n"
                        f"🏷️ Tür: {transaction['type'].title()}"
                    )
                    
                    keyboard = [
                        [InlineKeyboardButton("📊 Bu Hafta Özet", callback_data="weekly_report")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text(response, reply_markup=reply_markup)
                    return
                else:
                    await update.message.reply_text("❌ İşlem kaydedilemedi. Lütfen tekrar deneyin.")
                    return
            
            # Rapor istemi kontrolü
            text_lower = text.lower()
            if any(word in text_lower for word in ['ne kadar', 'özet', 'rapor', 'durum', 'toplam']):
                period = 'week'
                if 'ay' in text_lower or 'aylık' in text_lower:
                    period = 'month'
                elif 'bugün' in text_lower or 'gün' in text_lower:
                    period = 'today'
                
                results, totals = self.get_financial_summary(user_id, period)
                report = self.format_financial_report(results, totals, period)
                await update.message.reply_text(report)
                return
            
            # Varsayılan yanıt
            response = (
                "🤔 Ne yapmaya çalıştığınızı anlayamadım.\n\n"
                "💡 Şunları deneyebilirsiniz:\n"
                "• '500 TL yakıt aldım' - Mali işlem\n"
                "• 'Bu hafta ne kadar kazandım?' - Mali rapor\n\n"
                "❓ Yardım için /yardim yazabilirsiniz."
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("❓ Yardım", callback_data="help"),
                    InlineKeyboardButton("🎓 Rehber", callback_data="tutorial")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(response, reply_markup=reply_markup)
            
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            await update.message.reply_text("❌ Bir hata oluştu. Lütfen tekrar deneyin.")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Update {update} caused error {context.error}")

def main():
    bot = CarDealerBot()
    
    if not bot.token:
        print("❌ TELEGRAM_TOKEN environment variable not set!")
        return
    
    application = Application.builder().token(bot.token).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("yardim", bot.help_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    application.add_error_handler(bot.error_handler)
    
    print("🚗 Araba Satış Asistanı başlatılıyor...")
    print("🤖 Bot hazır! Kullanıcılar /start ile başlayabilir.")
    
    application.run_polling(
        poll_interval=1,
        timeout=10,
        bootstrap_retries=5
    )

if __name__ == "__main__":
    main()