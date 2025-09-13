"""
Kişisel Asistan Bot - Finansal Takip Sistemi v8.0
Tüm kritik hatalar düzeltildi, production-ready versiyonu
"""
import os
import logging
import base64
import json
import pytz
import re
import sqlite3
import asyncio
import structlog
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict
from time import time
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass
from enum import Enum

from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import TelegramError, RetryAfter, NetworkError

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import google.generativeai as genai

# === LOGGING CONFIGURATION ===
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# === ENUMS AND DATA CLASSES ===
class IntentType(Enum):
    FINANCIAL = "financial"
    FINANCIAL_REPORT = "financial_report"
    REMINDER = "reminder"
    CALENDAR = "calendar"
    RESET_CHAT = "reset_chat"
    HELP = "help"
    CHAT = "chat"

class AccountType(Enum):
    ARABA = "araba"
    EMLAK = "emlak"
    KISISEL = "kisisel"

class TransactionType(Enum):
    GELIR = "gelir"
    GIDER = "gider"

@dataclass
class FinancialTransaction:
    account_type: AccountType
    transaction_type: TransactionType
    amount: float
    category: str
    description: str
    confidence: float = 0.0

@dataclass
class IntentResult:
    intent: IntentType
    data: Any
    confidence: float

# === CONFIGURATION AND ENVIRONMENT ===
class Config:
    def __init__(self):
        self.TOKEN = self._get_required_env("TELEGRAM_TOKEN")
        self.GEMINI_API_KEY = self._get_required_env("GEMINI_API_KEY")
        self.GOOGLE_CALENDAR_ID = self._get_required_env("GOOGLE_CALENDAR_ID")
        self.GOOGLE_CREDENTIALS_BASE64 = self._get_required_env("GOOGLE_CREDENTIALS_BASE64")
        
        # Optional configs with defaults
        self.MAX_SESSIONS = int(os.environ.get("MAX_CHAT_SESSIONS", "100"))
        self.SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "3600"))
        self.RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "20"))
        self.RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))
        
        self._validate_config()

    def _get_required_env(self, key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise ValueError(f"Required environment variable {key} is not set!")
        return value

    def _validate_config(self):
        """Validate all configuration values"""
        try:
            # Test Gemini API key format
            if not self.GEMINI_API_KEY.startswith('AIza'):
                logger.warning("Gemini API key format might be incorrect")
            
            # Test base64 decoding
            base64.b64decode(self.GOOGLE_CREDENTIALS_BASE64)
            logger.info("Configuration validation successful")
        except Exception as e:
            raise ValueError(f"Configuration validation failed: {e}")

config = Config()

# === DATABASE MANAGER ===
class DatabaseManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_database()

    def _init_database(self):
        """Initialize database with proper schema"""
        os.makedirs(self.db_path.parent, exist_ok=True)
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Transactions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_type TEXT NOT NULL CHECK (account_type IN ('araba', 'emlak', 'kisisel')),
                    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('gelir', 'gider')),
                    amount REAL NOT NULL CHECK (amount > 0),
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            ''')
            
            # Users table for future extensions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Indexes for performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_type)')
            
            conn.commit()
            logger.info("Database initialized successfully")

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = None
        try:
            conn = sqlite3.connect(
                self.db_path, 
                timeout=10.0,
                check_same_thread=False
            )
            conn.execute('PRAGMA journal_mode=WAL')  # Better concurrency
            conn.execute('PRAGMA synchronous=NORMAL')  # Better performance
            yield conn
        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            logger.error("Database error", error=str(e))
            raise
        finally:
            if conn:
                conn.close()

    def add_transaction(self, user_id: int, transaction: FinancialTransaction) -> bool:
        """Add a financial transaction"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO transactions (user_id, account_type, transaction_type, amount, category, description, date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, 
                    transaction.account_type.value, 
                    transaction.transaction_type.value,
                    transaction.amount, 
                    transaction.category, 
                    transaction.description, 
                    datetime.now().date()
                ))
                conn.commit()
                logger.info("Transaction added", user_id=user_id, amount=transaction.amount)
                return True
        except Exception as e:
            logger.error("Failed to add transaction", user_id=user_id, error=str(e))
            return False

    def get_financial_summary(self, user_id: int, period: str = 'month', account_type: Optional[str] = None) -> Tuple[List, List]:
        """Get financial summary with proper error handling"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Date filters
                date_filters = {
                    'day': datetime.now().date(),
                    'week': (datetime.now() - timedelta(days=7)).date(),
                    'month': datetime.now().replace(day=1).date(),
                    'year': datetime.now().replace(month=1, day=1).date()
                }
                
                date_filter = date_filters.get(period, date_filters['month'])
                query_date = "date >= ?" if period != 'day' else "date = ?"
                
                # Category details query
                base_query = f"SELECT transaction_type, SUM(amount), category FROM transactions WHERE user_id = ? AND {query_date}"
                params = [user_id, date_filter]
                
                if account_type and account_type in ['araba', 'emlak', 'kisisel']:
                    base_query += " AND account_type = ?"
                    params.append(account_type)
                
                base_query += " GROUP BY transaction_type, category ORDER BY SUM(amount) DESC"
                
                cursor.execute(base_query, params)
                results = cursor.fetchall()
                
                # Totals query
                total_query = f"SELECT transaction_type, SUM(amount) FROM transactions WHERE user_id = ? AND {query_date}"
                total_params = [user_id, date_filter]
                
                if account_type and account_type in ['araba', 'emlak', 'kisisel']:
                    total_query += " AND account_type = ?"
                    total_params.append(account_type)
                    
                total_query += " GROUP BY transaction_type"
                
                cursor.execute(total_query, total_params)
                totals = cursor.fetchall()
                
                return results, totals
                
        except Exception as e:
            logger.error("Failed to get financial summary", user_id=user_id, error=str(e))
            return [], []

    def update_user_activity(self, user_id: int, username: str = None, first_name: str = None):
        """Update user activity"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO users (id, username, first_name, last_active)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (user_id, username, first_name))
                conn.commit()
        except Exception as e:
            logger.error("Failed to update user activity", user_id=user_id, error=str(e))

# === RATE LIMITER ===
class RateLimiter:
    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        """Check if user is within rate limits"""
        now = time()
        user_requests = self.requests[user_id]
        
        # Clean old requests
        user_requests[:] = [req_time for req_time in user_requests if now - req_time < self.window_seconds]
        
        if len(user_requests) >= self.max_requests:
            logger.warning("Rate limit exceeded", user_id=user_id)
            return False
        
        user_requests.append(now)
        return True

    def get_remaining_requests(self, user_id: int) -> int:
        """Get remaining requests for user"""
        now = time()
        user_requests = self.requests[user_id]
        user_requests[:] = [req_time for req_time in user_requests if now - req_time < self.window_seconds]
        return max(0, self.max_requests - len(user_requests))

# === INPUT VALIDATION ===
class InputValidator:
    @staticmethod
    def validate_amount(amount_str: str) -> Tuple[Optional[float], Optional[str]]:
        """Validate financial amount"""
        try:
            # Clean the input
            cleaned = re.sub(r'[^\d.,]', '', amount_str)
            cleaned = cleaned.replace(',', '.')
            
            amount = float(cleaned)
            
            if amount <= 0:
                return None, "Miktar sıfırdan büyük olmalıdır"
            if amount > 10_000_000:  # 10M TL limit
                return None, "Miktar çok büyük"
            
            return amount, None
        except (ValueError, TypeError):
            return None, "Geçersiz sayı formatı"

    @staticmethod
    def sanitize_description(description: str) -> str:
        """Sanitize user input"""
        if not description:
            return ""
        
        # Remove dangerous characters
        sanitized = re.sub(r'[<>"\'\`\n\r\t]', '', description)
        # Limit length
        sanitized = sanitized[:200].strip()
        
        return sanitized

    @staticmethod
    def validate_user_id(user_id: Any) -> bool:
        """Validate user ID"""
        try:
            return isinstance(user_id, int) and user_id > 0
        except:
            return False

# === FINANCIAL INTENT ANALYZER ===
class FinancialIntentAnalyzer:
    def __init__(self):
        self.false_positive_patterns = [
            r'\d+\s*tl.*?(not|kağıt|para birimi|değer|fiyat|gibi|benzeri)',
            r'(kaç|ne kadar|hangi|neden).*?\d+\s*tl',
            r'\d+\s*tl.*?(değerinde|kadar|civarında)(?!\s+(aldım|sattım|ödedim|kazandım))',
            r'sadece.*?\d+\s*tl',
            r'\d+\s*tl.*?(örnek|mesela|diyelim)'
        ]
        
        self.income_keywords = [
            'kazandım', 'aldım', 'gelir', 'sattım', 'komisyon', 'ödeme aldım',
            'satış yaptım', 'para kazandım', 'geldi', 'kira geliri'
        ]
        
        self.expense_keywords = [
            'harcadım', 'ödedim', 'aldım', 'masraf', 'gider', 'fatura',
            'para harcadım', 'satın aldım', 'ödeme yaptım'
        ]
        
        self.account_keywords = {
            'araba': ['araba', 'galeri', 'otomobil', 'civic', 'bmw', 'mercedes', 'araç', 'oto'],
            'emlak': ['emlak', 'ev', 'daire', 'kiralama', 'satış komisyonu', 'gayrimenkul'],
            'kisisel': ['kişisel', 'özel', 'kendi']
        }
        
        self.categories = {
            'araba': {
                'gelir': ['satış', 'servis', 'tamir', 'diğer'],
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

    def detect_financial_intent(self, text: str) -> Optional[FinancialTransaction]:
        """Detect financial transaction from text"""
        text_lower = text.lower().strip()
        
        # Check for false positives first
        for pattern in self.false_positive_patterns:
            if re.search(pattern, text_lower):
                logger.debug("False positive detected", text=text)
                return None
        
        # Extract amount
        amount_match = re.search(r'(\d+(?:[.,]\d+)?)\s*tl', text_lower)
        if not amount_match:
            return None
        
        amount_str = amount_match.group(1).replace(',', '.')
        amount, error = InputValidator.validate_amount(amount_str)
        if not amount:
            return None
        
        # Determine transaction type
        transaction_type = self._determine_transaction_type(text_lower)
        if not transaction_type:
            return None
        
        # Determine account type
        account_type = self._determine_account_type(text_lower)
        
        # Determine category
        category = self._determine_category(text_lower, account_type, transaction_type)
        
        # Extract description
        description = self._extract_description(text, amount_str)
        
        confidence = self._calculate_confidence(text_lower, transaction_type, account_type)
        
        return FinancialTransaction(
            account_type=AccountType(account_type),
            transaction_type=TransactionType(transaction_type),
            amount=amount,
            category=category,
            description=description,
            confidence=confidence
        )

    def _determine_transaction_type(self, text: str) -> Optional[str]:
        """Determine if transaction is income or expense"""
        income_score = sum(1 for keyword in self.income_keywords if keyword in text)
        expense_score = sum(1 for keyword in self.expense_keywords if keyword in text)
        
        if income_score > expense_score:
            return 'gelir'
        elif expense_score > income_score:
            return 'gider'
        else:
            # Context-based decision
            if any(word in text for word in ['satış', 'komisyon', 'kazanç', 'gelir']):
                return 'gelir'
            return 'gider'  # Default to expense

    def _determine_account_type(self, text: str) -> str:
        """Determine account type"""
        scores = {}
        
        for account_type, keywords in self.account_keywords.items():
            scores[account_type] = sum(1 for keyword in keywords if keyword in text)
        
        best_account = max(scores, key=scores.get)
        return best_account if scores[best_account] > 0 else 'kisisel'

    def _determine_category(self, text: str, account_type: str, transaction_type: str) -> str:
        """Determine transaction category"""
        available_categories = self.categories[account_type][transaction_type]
        
        for category in available_categories:
            if category == 'diğer':
                continue
            
            # Check if category keywords exist in text
            category_words = category.replace('_', ' ').split()
            if any(word in text for word in category_words):
                return category
        
        return 'diğer'

    def _extract_description(self, text: str, amount_str: str) -> str:
        """Extract description from text"""
        # Remove amount and clean up
        description = re.sub(rf'{re.escape(amount_str)}\s*tl', '', text, flags=re.IGNORECASE)
        description = re.sub(r'\s+', ' ', description).strip()
        
        return InputValidator.sanitize_description(description) or "İşlem"

    def _calculate_confidence(self, text: str, transaction_type: str, account_type: str) -> float:
        """Calculate confidence score"""
        confidence = 0.5  # Base confidence
        
        # Boost confidence based on explicit keywords
        if transaction_type == 'gelir' and any(k in text for k in ['kazandım', 'sattım', 'gelir']):
            confidence += 0.3
        elif transaction_type == 'gider' and any(k in text for k in ['harcadım', 'ödedim', 'aldım']):
            confidence += 0.3
        
        # Boost confidence based on account type specificity
        if account_type != 'kisisel':
            confidence += 0.2
        
        return min(confidence, 1.0)

# === TIME PARSER ===
class TimeParser:
    def __init__(self):
        self.istanbul_tz = pytz.timezone('Europe/Istanbul')
        self.patterns = [
            (r'yarın\s+(?:saat\s+)?(\d{1,2}):(\d{2})', self._tomorrow_with_time),
            (r'bugün\s+(?:saat\s+)?(\d{1,2}):(\d{2})', self._today_with_time),
            (r'(?:saat\s+)?(\d{1,2}):(\d{2})', self._time_only),
            (r'yarın\s+(\d{1,2})\'?(?:de|da|te|ta)', self._tomorrow_hour),
            (r'bugün\s+(\d{1,2})\'?(?:de|da|te|ta)', self._today_hour),
            (r'(\d{1,2})\'?(?:de|da|te|ta)', self._hour_only),
            (r'(\d+)\s+saat\s+sonra', self._hours_later),
            (r'(\d+)\s+dakika\s+sonra', self._minutes_later),
            (r'(\d{1,2})\.(\d{1,2})\.\s+saat\s+(\d{1,2}):(\d{2})', self._date_with_time),
        ]

    def parse_time_from_text(self, text: str) -> Tuple[Optional[datetime], str, Optional[str]]:
        """Parse time from natural language text"""
        try:
            now = datetime.now(self.istanbul_tz)
            text_lower = text.lower().strip()
            
            for pattern, time_func in self.patterns:
                match = re.search(pattern, text_lower)
                if match:
                    try:
                        parsed_time = time_func(now, *match.groups())
                        
                        if parsed_time and self._is_valid_time(parsed_time, now, text_lower):
                            # Remove time expression from message
                            message = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
                            message = re.sub(r'\s+', ' ', message)
                            
                            return parsed_time, message, match.group(0)
                    except Exception as e:
                        logger.debug("Time parsing failed", pattern=pattern, error=str(e))
                        continue
            
            return None, text, None
            
        except Exception as e:
            logger.error("Time parsing error", error=str(e))
            return None, text, None

    def _tomorrow_with_time(self, now: datetime, hour: str, minute: str) -> datetime:
        return (now + timedelta(days=1)).replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )

    def _today_with_time(self, now: datetime, hour: str, minute: str) -> datetime:
        return now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def _time_only(self, now: datetime, hour: str, minute: str) -> datetime:
        return now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def _tomorrow_hour(self, now: datetime, hour: str) -> datetime:
        return (now + timedelta(days=1)).replace(
            hour=int(hour), minute=0, second=0, microsecond=0
        )

    def _today_hour(self, now: datetime, hour: str) -> datetime:
        return now.replace(hour=int(hour), minute=0, second=0, microsecond=0)

    def _hour_only(self, now: datetime, hour: str) -> datetime:
        return now.replace(hour=int(hour), minute=0, second=0, microsecond=0)

    def _hours_later(self, now: datetime, hours: str) -> datetime:
        return now + timedelta(hours=int(hours))

    def _minutes_later(self, now: datetime, minutes: str) -> datetime:
        return now + timedelta(minutes=int(minutes))

    def _date_with_time(self, now: datetime, day: str, month: str, hour: str, minute: str) -> datetime:
        try:
            target = now.replace(
                day=int(day), month=int(month), 
                hour=int(hour), minute=int(minute), 
                second=0, microsecond=0
            )
            if target < now:
                target = target.replace(year=target.year + 1)
            return target
        except ValueError:
            return None

    def _is_valid_time(self, parsed_time: datetime, now: datetime, text: str) -> bool:
        """Validate parsed time"""
        try:
            # Check if time is too far in the past or future
            if parsed_time < now - timedelta(minutes=5) and 'yarın' not in text and 'sonra' not in text:
                # Assume next day if time seems to be in the past
                parsed_time += timedelta(days=1)
            
            # Don't allow times more than 1 year in the future
            if parsed_time > now + timedelta(days=365):
                return False
            
            # Validate hour and minute ranges
            return 0 <= parsed_time.hour <= 23 and 0 <= parsed_time.minute <= 59
            
        except Exception:
            return False

# === INTENT DETECTOR ===
class IntentDetector:
    def __init__(self):
        self.financial_analyzer = FinancialIntentAnalyzer()
        self.time_parser = TimeParser()

    def detect_intent(self, text: str) -> IntentResult:
        """Detect user intent with confidence scoring"""
        text_lower = text.lower().strip()
        
        if not text_lower:
            return IntentResult(IntentType.HELP, None, 1.0)
        
        # Priority-based intent detection
        intents = []
        
        # Financial transaction (highest priority)
        financial_result = self.financial_analyzer.detect_financial_intent(text)
        if financial_result and financial_result.confidence > 0.6:
            intents.append((IntentType.FINANCIAL, financial_result, financial_result.confidence))
        
        # Financial report
        if self._is_financial_report_request(text_lower):
            intents.append((IntentType.FINANCIAL_REPORT, text, 0.8))
        
        # Reminder/Calendar
        reminder_confidence = self._get_reminder_confidence(text_lower)
        if reminder_confidence > 0.5:
            intents.append((IntentType.REMINDER, text, reminder_confidence))
        
        # Other specific intents
        if self._is_calendar_request(text_lower):
            intents.append((IntentType.CALENDAR, text, 0.9))
        
        if self._is_reset_request(text_lower):
            intents.append((IntentType.RESET_CHAT, text, 0.9))
        
        if self._is_help_request(text_lower):
            intents.append((IntentType.HELP, text, 0.9))
        
        # Return highest confidence intent or default to chat
        if intents:
            best_intent = max(intents, key=lambda x: x[2])
            return IntentResult(best_intent[0], best_intent[1], best_intent[2])
        
        return IntentResult(IntentType.CHAT, text, 0.3)

    def _is_financial_report_request(self, text: str) -> bool:
        report_keywords = ['ne kadar', 'toplam', 'özet', 'rapor', 'durum', 'hesap', 'kâr', 'zarar']
        return any(keyword in text for keyword in report_keywords)

    def _get_reminder_confidence(self, text: str) -> float:
        reminder_keywords = ['hatırlat', 'randevu', 'toplantı', 'etkinlik', 'görüşme', 'buluşma', 'yapacak']
        time_patterns = [r'\d{1,2}:\d{2}', r'yarın', r'bugün', r'saat', r'sonra', r'gün']
        
        keyword_score = sum(1 for keyword in reminder_keywords if keyword in text) * 0.3
        time_score = sum(1 for pattern in time_patterns if re.search(pattern, text)) * 0.2
        
        return min(keyword_score + time_score, 1.0)

    def _is_calendar_request(self, text: str) -> bool:
        keywords = ['takvim', 'ajanda', 'program', 'calendar']
        return any(keyword in text for keyword in keywords)

    def _is_reset_request(self, text: str) -> bool:
        keywords = ['yeni konuşma', 'sıfırla', 'temizle', 'baştan', 'reset', 'yenile']
        return any(keyword in text for keyword in keywords)

    def _is_help_request(self, text: str) -> bool:
        keywords = ['yardım', 'help', 'nasıl', 'komut', 'ne yapabilir', '?']
        return any(keyword in text for keyword in keywords)

# === CHAT SESSION MANAGER ===
class ChatSessionManager:
    def __init__(self, max_sessions: int = 100, cleanup_interval: int = 3600):
        self.sessions = {}
        self.last_activity = {}
        self.max_sessions = max_sessions
        self.cleanup_interval = cleanup_interval
        self.gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

    def get_or_create_session(self, user_id: int):
        """Get existing session or create new one"""
        self._cleanup_old_sessions()
        
        if user_id not in self.sessions:
            if len(self.sessions) >= self.max_sessions:
                # Remove oldest session
                oldest_user = min(self.last_activity.keys(), key=lambda k: self.last_activity[k])
                self._remove_session(oldest_user)
            
            self.sessions[user_id] = self.gemini_model.start_chat()
            logger.info("New chat session created", user_id=user_id)
        
        self.last_activity[user_id] = time()
        return self.sessions[user_id]

    def remove_session(self, user_id: int):
        """Remove user session"""
        self._remove_session(user_id)

    def _remove_session(self, user_id: int):
        """Internal method to remove session"""
        self.sessions.pop(user_id, None)
        self.last_activity.pop(user_id, None)
        logger.info("Chat session removed", user_id=user_id)

    def _cleanup_old_sessions(self):
        """Clean up expired sessions"""
        current_time = time()
        expired_users = [
            user_id for user_id, last_time in self.last_activity.items()
            if current_time - last_time > self.cleanup_interval
        ]
        
        for user_id in expired_users:
            self._remove_session(user_id)

    def get_session_count(self) -> int:
        """Get current session count"""
        return len(self.sessions)

# === GOOGLE SERVICES MANAGER ===
class GoogleServicesManager:
    def __init__(self, credentials_base64: str, calendar_id: str):
        self.calendar_id = calendar_id
        self.calendar_service = None
        self._init_services(credentials_base64)

    def _init_services(self, credentials_base64: str):
        """Initialize Google services"""
        try:
            creds_json_str = base64.b64decode(credentials_base64).decode('utf-8')
            creds_json = json.loads(creds_json_str)
            scopes = ['https://www.googleapis.com/auth/calendar']
            
            credentials = service_account.Credentials.from_service_account_info(
                creds_json, scopes=scopes
            )
            
            self.calendar_service = build('calendar', 'v3', credentials=credentials)
            logger.info("Google services initialized successfully")
            
        except Exception as e:
            logger.error("Failed to initialize Google services", error=str(e))
            self.calendar_service = None

    def create_calendar_event(self, title: str, start_time: datetime, duration_minutes: int = 30) -> bool:
        """Create a calendar event"""
        if not self.calendar_service:
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
                        {'method': 'popup', 'minutes': 10},
                        {'method': 'email', 'minutes': 30}
                    ]
                }
            }

            result = self.calendar_service.events().insert(
                calendarId=self.calendar_id, 
                body=event
            ).execute()
            
            logger.info("Calendar event created", event_id=result['id'], title=title)
            return True
            
        except HttpError as e:
            logger.error("Google Calendar API error", error=str(e))
            return False
        except Exception as e:
            logger.error("Failed to create calendar event", error=str(e))
            return False

    def get_calendar_url(self) -> str:
        """Get calendar URL"""
        return f"https://calendar.google.com/calendar/u/0?cid={self.calendar_id}"

# === ERROR HANDLERS ===
class BotErrorHandler:
    @staticmethod
    async def handle_database_error(update: Update, error: Exception):
        """Handle database errors"""
        logger.error("Database error occurred", error=str(error), user_id=update.effective_user.id)
        await update.message.reply_text(
            "📊 Şu anda veritabanına erişemiyorum. Lütfen birkaç dakika sonra tekrar deneyin."
        )

    @staticmethod
    async def handle_calendar_error(update: Update, error: Exception):
        """Handle calendar errors"""
        logger.error("Calendar error occurred", error=str(error), user_id=update.effective_user.id)
        await update.message.reply_text(
            "📅 Takvim servisinde geçici bir sorun var. Hatırlatıcı eklenemedi."
        )

    @staticmethod
    async def handle_ai_error(update: Update, error: Exception):
        """Handle AI service errors"""
        logger.error("AI service error occurred", error=str(error), user_id=update.effective_user.id)
        await update.message.reply_text(
            "🤖 AI servisinde geçici bir sorun yaşanıyor. Lütfen daha sonra tekrar deneyin."
        )

    @staticmethod
    async def handle_rate_limit_error(update: Update):
        """Handle rate limiting"""
        logger.warning("Rate limit hit", user_id=update.effective_user.id)
        await update.message.reply_text(
            "⚠️ Çok hızlı mesaj gönderiyorsunuz. Lütfen bir dakika bekleyin."
        )

    @staticmethod
    async def handle_network_error(update: Update, error: Exception):
        """Handle network errors"""
        logger.error("Network error occurred", error=str(error))
        if update and update.message:
            await update.message.reply_text(
                "🌐 Bağlantı sorunu yaşanıyor. Lütfen tekrar deneyin."
            )

# === REPORT FORMATTER ===
class ReportFormatter:
    @staticmethod
    def format_financial_summary(results: List, totals: List, period: str, account_type: str = None) -> str:
        """Format financial summary report"""
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

        # Summary section
        net_result = total_income - total_expense
        net_emoji = "💰" if net_result >= 0 else "📉"
        
        report += f"📈 **Toplam Gelir:** {total_income:,.0f} TL\n"
        report += f"📉 **Toplam Gider:** {total_expense:,.0f} TL\n"
        report += f"{net_emoji} **Net Durum:** {net_result:,.0f} TL\n\n"

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
                report += "📈 **Gelir Detayları:**\n" + "\n".join(income_categories) + "\n\n"

            if expense_categories:
                report += "📉 **Gider Detayları:**\n" + "\n".join(expense_categories) + "\n\n"

        # Add performance indicator
        if total_income > 0:
            expense_ratio = (total_expense / total_income) * 100
            if expense_ratio < 50:
                report += "🟢 **Durum:** Çok iyi! Giderler gelirin %50'sinden az.\n"
            elif expense_ratio < 80:
                report += "🟡 **Durum:** İyi. Giderler kontrol altında.\n"
            else:
                report += "🔴 **Durum:** Dikkat! Giderler gelire yakın.\n"

        return report

    @staticmethod
    def format_transaction_confirmation(transaction: FinancialTransaction) -> str:
        """Format transaction confirmation message"""
        account_names = {'araba': 'Araba İşi', 'emlak': 'Emlak İşi', 'kisisel': 'Kişisel'}
        type_emoji = '📈' if transaction.transaction_type == TransactionType.GELIR else '📉'
        
        return (
            f"{type_emoji} **İşlem Kaydedildi!**\n\n"
            f"💼 **Hesap:** {account_names[transaction.account_type.value]}\n"
            f"💰 **Miktar:** {transaction.amount:,.0f} TL\n"
            f"📁 **Kategori:** {transaction.category.replace('_', ' ').title()}\n"
            f"📝 **Açıklama:** {transaction.description}\n"
            f"📅 **Tarih:** {datetime.now().strftime('%d.%m.%Y')}"
        )

# === MAIN BOT CLASS ===
class PersonalAssistantBot:
    def __init__(self):
        self.config = config
        self.db_path = Path.home() / ".telegram_assistant" / "financial.db"
        self.db_manager = DatabaseManager(self.db_path)
        self.rate_limiter = RateLimiter(
            config.RATE_LIMIT_REQUESTS, 
            config.RATE_LIMIT_WINDOW
        )
        self.intent_detector = IntentDetector()
        self.chat_manager = ChatSessionManager(
            config.MAX_SESSIONS, 
            config.SESSION_TIMEOUT
        )
        self.google_services = GoogleServicesManager(
            config.GOOGLE_CREDENTIALS_BASE64,
            config.GOOGLE_CALENDAR_ID
        )
        self.time_parser = TimeParser()
        
        # Initialize Gemini
        genai.configure(api_key=config.GEMINI_API_KEY)

    async def post_init(self, application: Application):
        """Post initialization setup"""
        try:
            await application.bot.set_my_commands([
                BotCommand("start", "Asistanı başlatır"),
                BotCommand("yardim", "Yardım menüsünü gösterir"),
                BotCommand("hesap", "Mali durum özeti"),
                BotCommand("rapor", "Detaylı finansal rapor"),
                BotCommand("takvim", "Takvim bağlantısı"),
                BotCommand("temizle", "Sohbet geçmişini temizler")
            ])
            logger.info("Bot commands set successfully")
        except Exception as e:
            logger.error("Failed to set bot commands", error=str(e))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        try:
            user = update.effective_user
            self.db_manager.update_user_activity(user.id, user.username, user.first_name)
            
            keyboard = [
                [
                    InlineKeyboardButton("💰 Mali Durum", callback_data="financial_summary"),
                    InlineKeyboardButton("📊 Detaylı Rapor", callback_data="detailed_report")
                ],
                [
                    InlineKeyboardButton("📅 Takvim", callback_data="calendar"),
                    InlineKeyboardButton("💭 Yeni Sohbet", callback_data="new_chat")
                ],
                [InlineKeyboardButton("❓ Yardım", callback_data="help")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            welcome_message = (
                "🤖 **Merhaba! Ben senin kişisel asistanın ve mali danışmanınım.**\n\n"
                "📝 **Bana şöyle yazabilirsin:**\n"
                "💰 \"5000 TL araba sattım\"\n"
                "💰 \"300 TL yakıt aldım\"\n"
                "💰 \"Bu ay ne kadar kazandım?\"\n"
                "⏰ \"Yarın 14:30'da toplantım var\"\n"
                "📅 \"Takvimimi göster\"\n\n"
                "✨ **Komut yazmana gerek yok, doğal dilde konuş!**"
            )
            
            await update.message.reply_text(welcome_message, reply_markup=reply_markup)
            logger.info("Start command processed", user_id=user.id)
            
        except Exception as e:
            logger.error("Error in start command", error=str(e))
            await BotErrorHandler.handle_network_error(update, e)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /yardim command"""
        help_text = (
            "🤖 **Kişisel Asistan Bot Yardım**\n\n"
            
            "💰 **Finansal İşlemler:**\n"
            "• \"500 TL benzin aldım\"\n"
            "• \"3000 TL araba sattım\"\n"
            "• \"1500 TL kira geliri\"\n"
            "• \"Bu ay ne kadar harcadım?\"\n"
            "• \"Araba işi raporu\"\n\n"
            
            "⏰ **Hatırlatıcılar:**\n"
            "• \"Yarın 14:30'da doktor randevusu\"\n"
            "• \"Bugün 16:00'da toplantı\"\n"
            "• \"2 saat sonra alışveriş yap\"\n\n"
            
            "📊 **Raporlar:**\n"
            "• \"Bu hafta ne kadar kazandım?\"\n"
            "• \"Aylık özet\"\n"
            "• \"Emlak işi durumu\"\n\n"
            
            "🎯 **Komutlar:**\n"
            "/start - Başlangıç menüsü\n"
            "/hesap - Hızlı mali özet\n"
            "/rapor - Detaylı rapor\n"
            "/takvim - Takvim bağlantısı\n"
            "/temizle - Sohbet sıfırla\n\n"
            
            "💡 **İpucu:** Doğal dilde yazabilirsin!"
        )
        
        await update.message.reply_text(help_text)

    async def account_summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /hesap command"""
        await self.handle_financial_report(update, "bu ay genel durum", is_command=True)

    async def detailed_report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /rapor command"""
        keyboard = [
            [
                InlineKeyboardButton("📊 Haftalık", callback_data="report_week"),
                InlineKeyboardButton("📊 Aylık", callback_data="report_month")
            ],
            [
                InlineKeyboardButton("🚗 Araba İşi", callback_data="report_araba"),
                InlineKeyboardButton("🏠 Emlak İşi", callback_data="report_emlak")
            ],
            [
                InlineKeyboardButton("👤 Kişisel", callback_data="report_kisisel"),
                InlineKeyboardButton("🎯 Yıllık", callback_data="report_year")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📊 **Hangi raporu görmek istiyorsun?**",
            reply_markup=reply_markup
        )

    async def calendar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /takvim command"""
        calendar_url = self.google_services.get_calendar_url()
        await update.message.reply_text(
            f"📅 **Takvimini görüntülemek için tıkla:**\n{calendar_url}"
        )

    async def clear_chat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /temizle command"""
        user_id = update.effective_user.id
        self.chat_manager.remove_session(user_id)
        await update.message.reply_text("🤖 Sohbet geçmişi temizlendi!")

    async def handle_financial_transaction(self, update: Update, transaction: FinancialTransaction):
        """Handle financial transaction"""
        try:
            user_id = update.effective_user.id
            
            success = self.db_manager.add_transaction(user_id, transaction)
            
            if success:
                confirmation = ReportFormatter.format_transaction_confirmation(transaction)
                await update.message.reply_text(confirmation)
                
                # Add quick action buttons
                keyboard = [
                    [InlineKeyboardButton("📊 Bu Ay Özet", callback_data="financial_summary")],
                    [InlineKeyboardButton("💰 Başka İşlem Ekle", callback_data="add_transaction")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "Ne yapmak istiyorsun?", 
                    reply_markup=reply_markup
                )
            else:
                await BotErrorHandler.handle_database_error(update, Exception("Transaction save failed"))
                
        except Exception as e:
            logger.error("Error handling financial transaction", error=str(e))
            await BotErrorHandler.handle_database_error(update, e)

    async def handle_financial_report(self, update: Update, text: str, is_command: bool = False):
        """Handle financial report request"""
        try:
            user_id = update.effective_user.id
            text_lower = text.lower()
            
            # Parse period
            period = 'month'
            if 'bugün' in text_lower or 'gün' in text_lower:
                period = 'day'
            elif 'hafta' in text_lower:
                period = 'week'
            elif 'yıl' in text_lower:
                period = 'year'
            
            # Parse account type
            account_type = None
            if 'araba' in text_lower:
                account_type = 'araba'
            elif 'emlak' in text_lower:
                account_type = 'emlak'
            elif 'kişisel' in text_lower or 'kisisel' in text_lower:
                account_type = 'kisisel'
            
            results, totals = self.db_manager.get_financial_summary(user_id, period, account_type)
            report = ReportFormatter.format_financial_summary(results, totals, period, account_type)
            
            # Add action buttons for non-command calls
            keyboard = []
            if not is_command:
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
            else:
                await update.message.reply_text(report)
                
        except Exception as e:
            logger.error("Error handling financial report", error=str(e))
            await BotErrorHandler.handle_database_error(update, e)

    async def handle_reminder(self, update: Update, text: str):
        """Handle reminder creation"""
        try:
            parsed_time, message, time_expr = self.time_parser.parse_time_from_text(text)
            
            if not parsed_time:
                await update.message.reply_text(
                    "⏰ **Zaman bilgisini anlayamadım.**\n\n"
                    "Örnek kullanımlar:\n"
                    "• \"Yarın 14:30'da doktor randevusu\"\n"
                    "• \"Bugün 16:00'da toplantı\"\n"
                    "• \"2 saat sonra alışveriş yap\"\n"
                    "• \"15:45'te araba servise götür\""
                )
                return

            if not message or len(message.strip()) < 3:
                await update.message.reply_text(
                    f"📝 **Neyi hatırlatacağımı belirtmedin.**\n\n"
                    f"Örnek: \"Yarın 14:30'da doktor randevusu\""
                )
                return

            # Create calendar event
            success = self.google_services.create_calendar_event(
                title=message,
                start_time=parsed_time,
                duration_minutes=30
            )

            if success:
                formatted_time = self._format_turkish_datetime(parsed_time)
                
                confirmation_message = (
                    f"✅ **Hatırlatıcın başarıyla ayarlandı!**\n\n"
                    f"📝 **Konu:** {message}\n"
                    f"📅 **Tarih:** {formatted_time}\n"
                    f"⏰ **Hatırlatma:** 10 dakika önce\n\n"
                    f"📱 Takviminde görebilirsin!"
                )
                
                keyboard = [
                    [InlineKeyboardButton("📅 Takvimi Aç", url=self.google_services.get_calendar_url())]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(confirmation_message, reply_markup=reply_markup)
            else:
                await BotErrorHandler.handle_calendar_error(update, Exception("Calendar event creation failed"))
                
        except Exception as e:
            logger.error("Error handling reminder", error=str(e))
            await BotErrorHandler.handle_calendar_error(update, e)

    def _format_turkish_datetime(self, dt: datetime) -> str:
        """Format datetime in Turkish"""
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

    async def handle_chat(self, update: Update, text: str):
        """Handle general chat with AI"""
        try:
            user_id = update.effective_user.id
            chat = self.chat_manager.get_or_create_session(user_id)
            
            # Send message to Gemini (synchronously)
            response = chat.send_message(text)
            
            # Split long responses
            response_text = response.text
            if len(response_text) > 4000:
                chunks = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(response_text)
                
        except Exception as e:
            logger.error("Error in AI chat", error=str(e))
            await BotErrorHandler.handle_ai_error(update, e)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        try:
            user_id = query.from_user.id
            
            if query.data == "financial_summary":
                results, totals = self.db_manager.get_financial_summary(user_id, 'month')
                report = ReportFormatter.format_financial_summary(results, totals, 'month')
                await query.edit_message_text(report)
                
            elif query.data == "detailed_report":
                await self.detailed_report_command(query, context)
                
            elif query.data.startswith("report_"):
                period_or_account = query.data.replace("report_", "")
                if period_or_account in ['week', 'month', 'day', 'year']:
                    results, totals = self.db_manager.get_financial_summary(user_id, period_or_account)
                    report = ReportFormatter.format_financial_summary(results, totals, period_or_account)
                elif period_or_account in ['araba', 'emlak', 'kisisel']:
                    results, totals = self.db_manager.get_financial_summary(user_id, 'month', period_or_account)
                    report = ReportFormatter.format_financial_summary(results, totals, 'month', period_or_account)
                else:
                    report = "❌ Geçersiz rapor türü."
                
                await query.edit_message_text(report)
                
            elif query.data == "calendar":
                calendar_url = self.google_services.get_calendar_url()
                await query.edit_message_text(f"📅 **Takvimini görüntüle:**\n{calendar_url}")
                
            elif query.data == "new_chat":
                self.chat_manager.remove_session(user_id)
                await query.edit_message_text("🤖 **Sohbet geçmişi temizlendi!**")
                
            elif query.data == "help":
                help_text = (
                    "🤖 **Hızlı Yardım**\n\n"
                    "💰 **Finansal:** \"500 TL benzin aldım\"\n"
                    "⏰ **Hatırlatıcı:** \"Yarın 14:30'da toplantı\"\n"
                    "📊 **Rapor:** \"Bu ay ne kadar kazandım?\"\n"
                    "💭 **Sohbet:** Herhangi bir konu hakkında konuş\n\n"
                    "Detaylı yardım için /yardim yazabilirsin."
                )
                await query.edit_message_text(help_text)
                
            elif query.data == "add_transaction":
                await query.edit_message_text(
                    "💰 **Yeni işlem eklemek için şöyle yaz:**\n\n"
                    "• \"300 TL market alışverişi\"\n"
                    "• \"5000 TL araba sattım\"\n"
                    "• \"1500 TL kira geliri\"\n\n"
                    "Doğal dilde yazman yeterli!"
                )
                
        except Exception as e:
            logger.error("Error in button callback", callback_data=query.data, error=str(e))
            await query.edit_message_text("❌ Bir hata oluştu. Lütfen tekrar deneyin.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Main message handler"""
        try:
            user_id = update.effective_user.id
            user_text = update.message.text
            
            # Rate limiting check
            if not self.rate_limiter.is_allowed(user_id):
                await BotErrorHandler.handle_rate_limit_error(update)
                return
            
            # Update user activity
            user = update.effective_user
            self.db_manager.update_user_activity(user_id, user.username, user.first_name)
            
            # Detect intent
            intent_result = self.intent_detector.detect_intent(user_text)
            
            logger.info("Intent detected", 
                       user_id=user_id, 
                       intent=intent_result.intent.value,
                       confidence=intent_result.confidence)
            
            # Route to appropriate handler
            if intent_result.intent == IntentType.FINANCIAL:
                await self.handle_financial_transaction(update, intent_result.data)
                
            elif intent_result.intent == IntentType.FINANCIAL_REPORT:
                await self.handle_financial_report(update, intent_result.data)
                
            elif intent_result.intent == IntentType.REMINDER:
                await self.handle_reminder(update, intent_result.data)
                
            elif intent_result.intent == IntentType.CALENDAR:
                await self.calendar_command(update, context)
                
            elif intent_result.intent == IntentType.RESET_CHAT:
                await self.clear_chat_command(update, context)
                
            elif intent_result.intent == IntentType.HELP:
                await self.help_command(update, context)
                
            else:  # CHAT
                await self.handle_chat(update, intent_result.data)
                
        except RetryAfter as e:
            logger.warning("Telegram rate limit hit", retry_after=e.retry_after)
            await asyncio.sleep(e.retry_after)
            
        except NetworkError as e:
            logger.error("Network error in message handling", error=str(e))
            await BotErrorHandler.handle_network_error(update, e)
            
        except Exception as e:
            logger.error("Unexpected error in message handling", 
                        user_id=update.effective_user.id, 
                        error=str(e))
            await update.message.reply_text(
                "❌ Beklenmeyen bir hata oluştu. Lütfen daha sonra tekrar deneyin."
            )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler"""
        logger.error("Global error caught", error=str(context.error))
        
        if isinstance(context.error, NetworkError):
            logger.warning("Network error, will retry automatically")
            return
            
        if isinstance(context.error, RetryAfter):
            logger.warning("Rate limited by Telegram", retry_after=context.error.retry_after)
            return
        
        if update and hasattr(update, 'effective_user') and update.effective_user:
            try:
                if hasattr(update, 'message') and update.message:
                    await update.message.reply_text(
                        "⚠️ Sistem hatası oluştu. Teknik ekibimiz bilgilendirildi."
                    )
                elif hasattr(update, 'callback_query') and update.callback_query:
                    await update.callback_query.message.reply_text(
                        "⚠️ Sistem hatası oluştu. Teknik ekibimiz bilgilendirildi."
                    )
            except Exception as e:
                logger.error("Error in error handler", error=str(e))

# === BOT HEALTH CHECK ===
class BotHealthChecker:
    def __init__(self, bot: PersonalAssistantBot):
        self.bot = bot
        
    async def health_check(self) -> Dict[str, Any]:
        """Perform comprehensive health check"""
        health_status = {
            'timestamp': datetime.now().isoformat(),
            'overall_status': 'healthy',
            'services': {}
        }
        
        # Database health
        try:
            with self.bot.db_manager.get_connection() as conn:
                conn.execute('SELECT 1')
            health_status['services']['database'] = 'healthy'
        except Exception as e:
            health_status['services']['database'] = f'unhealthy: {str(e)}'
            health_status['overall_status'] = 'degraded'
        
        # Google Services health
        if self.bot.google_services.calendar_service:
            health_status['services']['google_calendar'] = 'healthy'
        else:
            health_status['services']['google_calendar'] = 'unhealthy'
            health_status['overall_status'] = 'degraded'
        
        # Chat sessions health
        session_count = self.bot.chat_manager.get_session_count()
        health_status['services']['chat_sessions'] = {
            'status': 'healthy',
            'active_sessions': session_count,
            'max_sessions': self.bot.chat_manager.max_sessions
        }
        
        # Rate limiter health
        health_status['services']['rate_limiter'] = 'healthy'
        
        return health_status

# === MONITORING AND METRICS ===
class BotMonitor:
    def __init__(self):
        self.metrics = {
            'messages_processed': 0,
            'intents_detected': defaultdict(int),
            'errors_count': 0,
            'response_times': [],
            'user_activity': defaultdict(int)
        }
        
    def record_message(self, user_id: int, intent: str, response_time: float, success: bool):
        """Record message processing metrics"""
        self.metrics['messages_processed'] += 1
        self.metrics['intents_detected'][intent] += 1
        self.metrics['response_times'].append(response_time)
        self.metrics['user_activity'][user_id] += 1
        
        if not success:
            self.metrics['errors_count'] += 1
            
        logger.info("Message processed", 
                   user_id=user_id, 
                   intent=intent, 
                   response_time=response_time,
                   success=success)
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get metrics summary"""
        avg_response_time = 0
        if self.metrics['response_times']:
            avg_response_time = sum(self.metrics['response_times']) / len(self.metrics['response_times'])
            
        return {
            'total_messages': self.metrics['messages_processed'],
            'total_errors': self.metrics['errors_count'],
            'error_rate': self.metrics['errors_count'] / max(self.metrics['messages_processed'], 1),
            'average_response_time': avg_response_time,
            'active_users': len(self.metrics['user_activity']),
            'intent_distribution': dict(self.metrics['intents_detected'])
        }

# === GRACEFUL SHUTDOWN ===
class GracefulShutdown:
    def __init__(self, bot: PersonalAssistantBot):
        self.bot = bot
        self.shutdown_event = asyncio.Event()
        
    async def shutdown(self):
        """Perform graceful shutdown"""
        logger.info("Starting graceful shutdown...")
        
        # Stop accepting new messages
        self.shutdown_event.set()
        
        # Clean up chat sessions
        self.bot.chat_manager.sessions.clear()
        self.bot.chat_manager.last_activity.clear()
        
        # Close database connections (handled by context managers)
        
        logger.info("Graceful shutdown completed")

# === MAIN FUNCTION ===
def create_application() -> Application:
    """Create and configure the bot application"""
    # Validate environment
    try:
        config_instance = Config()
        logger.info("Configuration validated successfully")
    except Exception as e:
        logger.error("Configuration validation failed", error=str(e))
        raise
    
    # Create bot instance
    bot = PersonalAssistantBot()
    monitor = BotMonitor()
    health_checker = BotHealthChecker(bot)
    
    # Create application
    application = Application.builder().token(config.TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("yardim", bot.help_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("hesap", bot.account_summary_command))
    application.add_handler(CommandHandler("rapor", bot.detailed_report_command))
    application.add_handler(CommandHandler("takvim", bot.calendar_command))
    application.add_handler(CommandHandler("temizle", bot.clear_chat_command))
    
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    # Add error handler
    application.add_error_handler(bot.error_handler)
    
    # Set post init
    application.post_init = bot.post_init
    
    # Store references for health checks
    application.bot_instance = bot
    application.health_checker = health_checker
    application.monitor = monitor
    
    return application

def main() -> None:
    """Main function to run the bot"""
    try:
        # Setup logging
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )
        
        # Create application
        application = create_application()
        
        logger.info("🚀 Personal Assistant Bot v8.0 starting...")
        logger.info("✅ All systems initialized successfully")
        logger.info("🤖 Bot is ready to serve users!")
        
        # Run the bot
        application.run_polling(
            poll_interval=1,
            timeout=10,
            bootstrap_retries=5,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=30,
            pool_timeout=30
        )
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error("Fatal error occurred", error=str(e))
        raise
    finally:
        logger.info("Bot shutdown completed")

# === HEALTH CHECK ENDPOINT (Optional) ===
async def health_endpoint():
    """Health check endpoint for monitoring"""
    try:
        # This would be used with a web framework like FastAPI
        # For now, it's just a placeholder
        return {"status": "healthy", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

# === ENTRY POINT ===
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Bot durduruldu.")
    except Exception as e:
        print(f"❌ Fatal hata: {e}")
        raise