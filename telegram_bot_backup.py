"""
Kişisel Telegram Asistan Botu
24/7 çalışan akıllı asistan
"""

import os
import logging
import sqlite3
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

class PersonalAssistantBot:
    def __init__(self, token):
        self.token = token
        self.db_path = Path.home() / ".telegram_assistant" / "assistant.db"
        self.setup_database()
    
    def setup_database(self):
        """Veritabanı kurulumu"""
        os.makedirs(self.db_path.parent, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Görevler tablosu
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                priority TEXT DEFAULT 'medium',
                completed BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Notlar tablosu
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                title TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Bot başlatma komutu"""
        welcome_message = """
🤖 Kişisel Asistan Bot'a Hoş Geldiniz!

Ben sizin 24/7 kişisel asistanınızım. Size nasıl yardımcı olabilirim?

📋 Komutlar:
- /gorev_ekle - Yeni görev ekle
- /gorevler - Aktif görevleri listele  
- /gorev_tamam ID - Görevi tamamla
- /not_ekle - Not ekle
- /notlar - Notları listele
- /help - Yardım menüsü

Veya sadece mesaj yazın, size yardımcı olmaya çalışayım!
        """
        await update.message.reply_text(welcome_message)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Yardım komutu"""
        help_text = """
🆘 YARDIM MENÜSÜ

Görev Yönetimi:
- /gorev_ekle [başlık] - Yeni görev ekle
- /gorevler - Aktif görevleri listele
- /gorev_tamam [ID] - Görevi tamamla

Not Yönetimi:
- /not_ekle [başlık] [içerik] - Not ekle
- /notlar - Notları listele

Genel:
- /start - Bot'u başlat
- Sadece mesaj yazarak da benimle konuşabilirsiniz!

Örnek: /gorev_ekle Alışveriş yap
        """
        await update.message.reply_text(help_text)
    
    async def add_task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Görev ekleme komutu"""
        user_id = update.effective_user.id
        
        if not context.args:
            await update.message.reply_text("📝 Kullanım: /gorev_ekle [görev başlığı]")
            return
        
        title = ' '.join(context.args)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO tasks (user_id, title, priority)
            VALUES (?, ?, ?)
        ''', (user_id, title, 'medium'))
        
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ Görev eklendi!\n📝 {title}\n🆔 ID: {task_id}")
    
    async def list_tasks_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Görevleri listeleme"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, priority, created_at 
            FROM tasks 
            WHERE user_id = ? AND completed = 0 
            ORDER BY created_at DESC
        ''', (user_id,))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            await update.message.reply_text("📭 Aktif göreviniz bulunmuyor!")
            return
        
        message = "📋 Aktif Görevleriniz:\n\n"
        
        for task in tasks:
            task_id, title, priority, created_at = task
            priority_emoji = "🔴" if priority == "high" else "🟡" if priority == "medium" else "🟢"
            date = created_at[:10]  # Sadece tarih kısmı
            
            message += f"{priority_emoji} {title}\n"
            message += f"🆔 ID: {task_id} | 📅 {date}\n\n"
        
        message += "Görevi tamamlamak için: /gorev_tamam [ID]"
        
        await update.message.reply_text(message)
    
    async def complete_task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Görev tamamlama"""
        user_id = update.effective_user.id
        
        if not context.args:
            await update.message.reply_text("✅ Kullanım: /gorev_tamam [görev ID]")
            return
        
        try:
            task_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Geçersiz ID! Sadece sayı girin.")
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Önce görevin varlığını kontrol et
        cursor.execute('SELECT title FROM tasks WHERE id = ? AND user_id = ?', (task_id, user_id))
        task = cursor.fetchone()
        
        if not task:
            await update.message.reply_text("❌ Bu ID'ye sahip görev bulunamadı!")
            conn.close()
            return
        
        # Görevi tamamla
        cursor.execute('UPDATE tasks SET completed = 1 WHERE id = ? AND user_id = ?', (task_id, user_id))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"🎉 Görev tamamlandı!\n📝 {task[0]}")
    
    async def add_note_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Not ekleme komutu"""
        user_id = update.effective_user.id
        
        if len(context.args) < 2:
            await update.message.reply_text("📝 Kullanım: /not_ekle [başlık] [içerik]")
            return
        
        title = context.args[0]
        content = ' '.join(context.args[1:])
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO notes (user_id, title, content)
            VALUES (?, ?, ?)
        ''', (user_id, title, content))
        
        note_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"📝 Not kaydedildi!\n{title}\n🆔 ID: {note_id}")
    
    async def list_notes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Notları listeleme"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, content, created_at 
            FROM notes 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 10
        ''', (user_id,))
        
        notes = cursor.fetchall()
        conn.close()
        
        if not notes:
            await update.message.reply_text("📭 Henüz not bulunmuyor!")
            return
        
        message = "📝 Son Notlarınız:\n\n"
        
        for note in notes:
            note_id, title, content, created_at = note
            date = created_at[:10]
            
            message += f"📌 {title}\n"
            message += f"{content[:100]}{'...' if len(content) > 100 else ''}\n"
            message += f"🆔 {note_id} | 📅 {date}\n\n"
        
        await update.message.reply_text(message)
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Genel mesajları işle"""
        user_text = update.message.text.lower()
        
        # Basit yanıtlar
        if any(word in user_text for word in ['merhaba', 'selam', 'hi', 'hello']):
            await update.message.reply_text("👋 Merhaba! Size nasıl yardımcı olabilirim?")
        
        elif any(word in user_text for word in ['nasılsın', 'naber', 'ne haber']):
            await update.message.reply_text("😊 Ben iyiyim, teşekkürler! Sizin için buradayım. Ne yapmak istiyorsunuz?")
        
        elif any(word in user_text for word in ['teşekkür', 'sağol', 'thanks']):
            await update.message.reply_text("😊 Rica ederim! Her zaman yardıma hazırım.")
        
        else:
            response = """
🤔 Anlamadım ama size yardımcı olmaya çalışabilirim!

Yapabileceklerim:
- Görev yönetimi (/gorevler)
- Not alma (/notlar)
- Genel sohbet

/help yazarak tüm komutları görebilirsiniz.
            """
            await update.message.reply_text(response)
    
    def run(self):
        """Bot'u çalıştır"""
        application = Application.builder().token(self.token).build()
        
        # Komut handler'ları
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("gorev_ekle", self.add_task_command))
        application.add_handler(CommandHandler("gorevler", self.list_tasks_command))
        application.add_handler(CommandHandler("gorev_tamam", self.complete_task_command))
        application.add_handler(CommandHandler("not_ekle", self.add_note_command))
        application.add_handler(CommandHandler("notlar", self.list_notes_command))
        
        # Mesaj handler'ı
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        print("🤖 Bot başlatıldı! Ctrl+C ile durdurun.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Bot Token'ınız
    BOT_TOKEN = "7206049774:AAG3o_WtNfLQO_olJfIh7zYOdaNmoZ2P5c0"
    
    bot = PersonalAssistantBot(BOT_TOKEN)
    bot.run()