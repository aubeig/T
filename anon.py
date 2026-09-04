import logging
import os
import secrets
import string
import sqlite3
import re
import shutil
import time
import asyncio
import html
import threading
import httpx
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from git import Repo
import violet_buffer as vb

# ══════════════════════════════════════════════════════════════════
#  ЭКОНОМИКА (Violet Buffer)
# ══════════════════════════════════════════════════════════════════
# Требует переменные окружения (см. violet_buffer.py):
#   BUFFER_URL     — адрес твоего Violet Buffer, например http://127.0.0.1:2459
#   BUFFER_TOKEN   — X-Token этого бота как приложения (из manage.sh create)
#   BUFFER_SECRET  — секрет этого бота (из manage.sh create)
STARTING_BALANCE = 1000          # виол при первой регистрации
SPONSOR_LINK_PRICE = 3500        # цена покупки спонсорской ссылки

# ══════════════════════════════════════════════════════════════════
#  RICH MESSAGES (Bot API 10.1+) — см. подробности у send_rich_screen()
# ══════════════════════════════════════════════════════════════════
# python-telegram-bot ещё не имеет типизированной поддержки sendRichMessage /
# editMessageText(rich_message=...) (issue #5261 в их репозитории открыт на
# момент написания) — вызываем эти методы напрямую через HTTP, в обход
# библиотеки. Это самая новая часть Bot API (10.1 вышел 11.06.2026, 10.3 —
# 24.08.2026), поэтому держим отдельный флаг: если у Telegram что-то не
# заладится с рендерингом или методы временно недоступны — можно откатиться
# на обычный MarkdownV2 одной строкой, не трогая остальной код.
RICH_MESSAGES_ENABLED = os.environ.get("RICH_MESSAGES_ENABLED", "1") != "0"

# ══════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_ID = int(os.environ.get("ADMIN_ID")) if os.environ.get("ADMIN_ID") else None
ADMIN_PASSWORD = "sirok228"

# --- ХРАНЕНИЕ БД НА GITHUB ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
DB_FILENAME = os.environ.get("DB_FILENAME", "data.db")
REPO_PATH = "/tmp/repo"
DB_PATH = os.path.join(REPO_PATH, DB_FILENAME)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)

repo = None

# ══════════════════════════════════════════════════════════════════
#  ЮНИКОД-СИМВОЛЫ (вместо эмодзи, единый техно-стиль)
# ══════════════════════════════════════════════════════════════════
SYM = {
    "menu": "◆",
    "link": "⟐",
    "add": "✦",
    "inbox": "▣",
    "reply": "↩",
    "trash": "✕",
    "back": "◁",
    "cancel": "✕",
    "check": "✓",
    "cross": "✕",
    "warn": "▲",
    "lock": "◈",
    "unlock": "◇",
    "gear": "⚙",
    "users": "▤",
    "stats": "▦",
    "gift": "◉",
    "broadcast": "▶",
    "report": "▥",
    "write": "✎",
    "clock": "◷",
    "id": "▪",
    "file": "▢",
    "photo": "▨",
    "video": "▧",
    "doc": "▦",
    "voice": "◐",
    "vnote": "○",
    "arrow": "→",
    "star": "✧",
    "dot": "·",
    "sep": "—",
    "ban": "⊘",
    "target": "◎",
    "transfer": "⇄",
    "view": "▷",
    "coin": "◈",
    "shop": "▥",
}

# ══════════════════════════════════════════════════════════════════
#  GIT / GITHUB СИНХРОНИЗАЦИЯ БД (в фоне, не блокирует хендлеры)
# ══════════════════════════════════════════════════════════════════

def setup_repo():
    """Клонирует репозиторий из GitHub во временную папку."""
    global repo
    remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    if os.path.exists(REPO_PATH):
        try:
            shutil.rmtree(REPO_PATH)
        except Exception as e:
            logging.warning(f"Не удалось удалить REPO_PATH: {e}")

    max_retries = 5
    for attempt in range(max_retries):
        try:
            logging.info(f"Клонирование репозитория {GITHUB_REPO}... (попытка {attempt + 1})")
            repo = Repo.clone_from(remote_url, REPO_PATH)
            repo.config_writer().set_value("user", "name", "AnonBot").release()
            repo.config_writer().set_value("user", "email", "bot@render.com").release()
            logging.info("Репозиторий успешно склонирован и настроен.")
            return True
        except Exception as e:
            logging.error(f"Ошибка при клонировании репозитория (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
            else:
                logging.critical("Не удалось склонировать репозиторий, создаю локальную БД")
                os.makedirs(REPO_PATH, exist_ok=True)
                return False


def _push_db_to_github_sync(commit_message):
    """Синхронная реализация push — вызывается только из фонового потока."""
    if not repo:
        return False
    max_retries = 3
    for attempt in range(max_retries):
        try:
            repo.index.add([DB_PATH])
            if repo.is_dirty(index=True, working_tree=False):
                repo.index.commit(commit_message)
                origin = repo.remote(name='origin')
                origin.push()
                logging.info(f"БД отправлена на GitHub: {commit_message}")
            return True
        except Exception as e:
            logging.error(f"Ошибка push БД (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return False


# Очередь на фоновую отправку, чтобы не плодить неограниченное число потоков
_push_lock = threading.Lock()
_pending_push_message = None
_push_thread_running = False


def push_db_to_github(commit_message):
    """
    Планирует отправку БД на GitHub в фоновом потоке.
    Не блокирует event loop бота. Несколько быстрых вызовов подряд
    схлопываются в один push с последним commit-message.
    """
    global _pending_push_message, _push_thread_running

    with _push_lock:
        _pending_push_message = commit_message
        if _push_thread_running:
            return
        _push_thread_running = True

    def _worker():
        global _pending_push_message, _push_thread_running
        # небольшая задержка чтобы собрать несколько подряд идущих изменений в один push
        time.sleep(2)
        with _push_lock:
            msg = _pending_push_message
            _pending_push_message = None
        _push_db_to_github_sync(msg or "Update DB")
        with _push_lock:
            _push_thread_running = False

    threading.Thread(target=_worker, daemon=True).start()


# ══════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════════

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cursor = conn.cursor()

        # WAL — критично для стабильности: без него параллельные обращения
        # (хендлер пишет + одновременно читается другой хендлер) регулярно
        # ловили "database is locked" и БД казалась "ненормальной"/глючной.
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=30000')
        cursor.execute('PRAGMA foreign_keys=ON')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned BOOLEAN DEFAULT 0,
                ban_reason TEXT DEFAULT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS links (
                link_id TEXT PRIMARY KEY,
                user_id INTEGER,
                title TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                is_sponsor BOOLEAN DEFAULT 0,
                sponsor_owner_id INTEGER DEFAULT NULL,
                custom_id TEXT DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id TEXT,
                from_user_id INTEGER,
                to_user_id INTEGER,
                message_text TEXT,
                message_type TEXT DEFAULT "text",
                file_id TEXT,
                file_size INTEGER,
                file_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (link_id) REFERENCES links (link_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS replies (
                reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                from_user_id INTEGER,
                reply_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (message_id) REFERENCES messages (message_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_messages (
                admin_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_admin_id INTEGER,
                to_user_id INTEGER,
                message_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()
        logging.info("База данных успешно инициализирована")
    except Exception as e:
        logging.error(f"Ошибка при инициализации БД: {e}")


def run_query(query, params=(), commit=False, fetch=None):
    try:
        with sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False) as conn:
            conn.execute('PRAGMA busy_timeout=30000')
            cursor = conn.cursor()
            cursor.execute(query, params)
            if commit:
                conn.commit()
            if fetch == "one":
                return cursor.fetchone()
            if fetch == "all":
                return cursor.fetchall()
            if commit:
                return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Ошибка базы данных: {e} | query={query[:120]}")
        return None


def save_user(user_id, username, first_name):
    """
    Регистрирует юзера, если его ещё нет, и начисляет стартовый баланс.
    Возвращает True, если это была первая регистрация (юзер только что создан).
    """
    result = run_query('SELECT 1 FROM users WHERE user_id = ?', (user_id,), fetch="one")
    is_new = result is None

    run_query('INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
               (user_id, username, first_name), commit=True)

    if is_new:
        # idempotency_key детерминирован от user_id — если этот код случайно
        # вызовется дважды для одного и того же нового юзера (гонка, повторный
        # запуск), буфер сам не даст задвоить стартовое начисление.
        try:
            vb.credit(user_id, STARTING_BALANCE,
                      {"reason": "starting_balance"},
                      idempotency_key=f"starting_balance_{user_id}")
        except vb.BufferError as e:
            logging.error(f"Не удалось начислить стартовый баланс user_id={user_id}: {e}")

    return is_new


def create_anon_link(user_id, title, description, is_sponsor=False, sponsor_owner_id=None, custom_id=None):
    if custom_id:
        existing = run_query('SELECT link_id FROM links WHERE link_id = ?', (custom_id,), fetch="one")
        if existing:
            return None
        link_id = custom_id
    else:
        link_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))

    expires_at = datetime.now() + timedelta(days=365)
    # run_query возвращает None при ошибке БД (см. run_query) — раньше это
    # игнорировалось, и админ видел "Ссылка создана!", хотя запись в базу
    # не прошла. Теперь реальный сбой INSERT честно возвращает None наверх.
    result = run_query('INSERT INTO links (link_id, user_id, title, description, expires_at, is_sponsor, sponsor_owner_id, custom_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (link_id, user_id, title, description, expires_at, is_sponsor, sponsor_owner_id, custom_id), commit=True)
    if result is None:
        logging.error(f"Не удалось создать ссылку в БД для пользователя {user_id} (custom_id={custom_id})")
        return None
    push_db_to_github(f"Create link for user {user_id}")
    return link_id


def save_message(link_id, from_user_id, to_user_id, message_text, message_type='text', file_id=None, file_size=None, file_name=None):
    message_id = run_query(
        'INSERT INTO messages (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name),
        commit=True
    )
    push_db_to_github(f"Save message from {from_user_id} to {to_user_id}")
    return message_id


def save_reply(message_id, from_user_id, reply_text):
    run_query('INSERT INTO replies (message_id, from_user_id, reply_text) VALUES (?, ?, ?)',
               (message_id, from_user_id, reply_text), commit=True)
    push_db_to_github(f"Save reply to message {message_id}")


def save_admin_message(from_admin_id, to_user_id, message_text):
    run_query('INSERT INTO admin_messages (from_admin_id, to_user_id, message_text) VALUES (?, ?, ?)',
               (from_admin_id, to_user_id, message_text), commit=True)
    push_db_to_github(f"Save admin message to user {to_user_id}")


def get_link_info(link_id):
    return run_query('SELECT l.link_id, l.user_id, l.title, l.description, u.username, l.is_sponsor FROM links l LEFT JOIN users u ON l.user_id = u.user_id WHERE l.link_id = ? AND l.is_active = 1', (link_id,), fetch="one")


def get_user_links(user_id):
    return run_query('SELECT link_id, title, description, created_at, is_sponsor FROM links WHERE user_id = ? AND is_active = 1', (user_id,), fetch="all")


def get_message_owner_ids(message_id):
    """Возвращает (from_user_id, to_user_id) для проверки прав на удаление/действия."""
    return run_query('SELECT from_user_id, to_user_id FROM messages WHERE message_id = ?', (message_id,), fetch="one")


def get_user_messages_with_replies(user_id, limit=50):
    return run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.file_size, m.file_name,
               m.created_at, l.title as link_title, l.link_id,
               (SELECT COUNT(*) FROM replies r WHERE r.message_id = m.message_id AND r.is_active = 1) as reply_count,
               (SELECT r.reply_text FROM replies r WHERE r.message_id = m.message_id AND r.is_active = 1
                ORDER BY r.created_at DESC LIMIT 1) as last_reply_preview
        FROM messages m
        JOIN links l ON m.link_id = l.link_id
        WHERE m.to_user_id = ? AND m.is_active = 1
        ORDER BY m.created_at DESC LIMIT ?
    ''', (user_id, limit), fetch="all")


def get_conversation_for_link(link_id):
    return run_query('''
        SELECT
            'message' as type, m.message_id, m.message_text, m.message_type,
            m.file_id, m.file_size, m.file_name, m.created_at,
            u.username as from_username, u.first_name as from_first_name, m.from_user_id,
            NULL as reply_text, NULL as reply_username, NULL as reply_first_name, NULL as reply_id
        FROM messages m
        LEFT JOIN users u ON m.from_user_id = u.user_id
        WHERE m.link_id = ? AND m.is_active = 1
        UNION ALL
        SELECT
            'reply' as type, r.message_id, NULL, NULL, NULL, NULL, NULL, r.created_at,
            NULL, NULL, r.from_user_id, r.reply_text, u.username, u.first_name, r.reply_id
        FROM replies r
        LEFT JOIN users u ON r.from_user_id = u.user_id
        LEFT JOIN messages m ON r.message_id = m.message_id
        WHERE m.link_id = ? AND r.is_active = 1
        ORDER BY created_at ASC
    ''', (link_id, link_id), fetch="all")


def get_conversation_for_user(user_id):
    return run_query('''
        SELECT
            m.message_id, m.message_text, m.message_type, m.file_id, m.file_size, m.file_name,
            m.created_at,
            u_from.username as from_username, u_from.first_name as from_first_name, m.from_user_id,
            u_to.username as to_username, u_to.first_name as to_first_name, m.to_user_id,
            l.title as link_title, l.link_id,
            r.reply_text, r.reply_id,
            u_reply.username as reply_username, u_reply.first_name as reply_first_name,
            CASE WHEN r.reply_id IS NOT NULL THEN 'reply' ELSE 'message' END as type
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        LEFT JOIN replies r ON m.message_id = r.message_id AND r.is_active = 1
        LEFT JOIN users u_reply ON r.from_user_id = u_reply.user_id
        WHERE (m.from_user_id = ? OR m.to_user_id = ? OR r.from_user_id = ?)
          AND m.is_active = 1
        ORDER BY m.created_at ASC, r.created_at ASC
    ''', (user_id, user_id, user_id), fetch="all")


def get_admin_messages_for_user(user_id):
    """
    Прямые сообщения администратора пользователю (кнопка "Написать" в
    управлении пользователем). Раньше эти сообщения писались в БД, но
    НИГДЕ не читались обратно — поэтому в просмотре переписки админ видел
    только анонимные сообщения/ответы, а свои же прямые сообщения не видел.
    """
    return run_query('''
        SELECT admin_message_id, from_admin_id, message_text, created_at
        FROM admin_messages
        WHERE to_user_id = ?
        ORDER BY created_at ASC
    ''', (user_id,), fetch="all") or []


def get_all_users_for_admin():
    result = run_query("SELECT user_id, username, first_name, created_at, is_banned, ban_reason FROM users ORDER BY created_at DESC", fetch="all")
    return result or []


def get_user_links_for_admin(user_id):
    result = run_query('''
        SELECT l.link_id, l.title, l.description, l.created_at,
               (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id AND m.is_active = 1) as message_count,
               l.is_sponsor
        FROM links l
        WHERE l.user_id = ? AND l.is_active = 1
        ORDER BY l.created_at DESC
    ''', (user_id,), fetch="all")
    return result or []


def get_admin_stats():
    stats = {}
    try:
        stats['users'] = run_query("SELECT COUNT(*) FROM users", fetch="one")[0] or 0
        stats['links'] = run_query("SELECT COUNT(*) FROM links WHERE is_active = 1", fetch="one")[0] or 0
        stats['messages'] = run_query("SELECT COUNT(*) FROM messages WHERE is_active = 1", fetch="one")[0] or 0
        stats['replies'] = run_query("SELECT COUNT(*) FROM replies WHERE is_active = 1", fetch="one")[0] or 0
        stats['photos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'photo' AND is_active = 1", fetch="one")[0] or 0
        stats['videos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'video' AND is_active = 1", fetch="one")[0] or 0
        stats['documents'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'document' AND is_active = 1", fetch="one")[0] or 0
        stats['voice'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'voice' AND is_active = 1", fetch="one")[0] or 0
        stats['video_note'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'video_note' AND is_active = 1", fetch="one")[0] or 0
        stats['links_type'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'link' AND is_active = 1", fetch="one")[0] or 0
        stats['banned'] = run_query("SELECT COUNT(*) FROM users WHERE is_banned = 1", fetch="one")[0] or 0
        stats['sponsor_links'] = run_query("SELECT COUNT(*) FROM links WHERE is_sponsor = 1", fetch="one")[0] or 0
    except Exception as e:
        logging.error(f"Ошибка при получении статистики: {e}")
        stats = {'users': 0, 'links': 0, 'messages': 0, 'replies': 0, 'photos': 0, 'videos': 0,
                  'documents': 0, 'voice': 0, 'video_note': 0, 'links_type': 0, 'banned': 0, 'sponsor_links': 0}
    return stats


def ban_user(user_id, reason=None):
    return run_query('UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?',
                      (reason, user_id), commit=True)


def unban_user(user_id):
    return run_query('UPDATE users SET is_banned = 0, ban_reason = NULL WHERE user_id = ?',
                      (user_id,), commit=True)


def delete_user(user_id):
    try:
        user_links = get_user_links_for_admin(user_id)
        for link in user_links:
            delete_link_completely(link[0])
        run_query('DELETE FROM messages WHERE from_user_id = ? OR to_user_id = ?', (user_id, user_id), commit=True)
        run_query('DELETE FROM replies WHERE from_user_id = ?', (user_id,), commit=True)
        run_query('DELETE FROM users WHERE user_id = ?', (user_id,), commit=True)
        push_db_to_github(f"Completely delete user {user_id}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при удалении пользователя: {e}")
        return False


def is_user_banned(user_id):
    result = run_query('SELECT is_banned FROM users WHERE user_id = ?', (user_id,), fetch="one")
    return bool(result and result[0] == 1)


def get_ban_reason(user_id):
    result = run_query('SELECT ban_reason FROM users WHERE user_id = ?', (user_id,), fetch="one")
    return result[0] if result and result[0] else None


async def send_ban_notice(update: Update):
    """Единое сообщение о блокировке — теперь ВСЕГДА с причиной, если она указана."""
    user_id = update.effective_user.id
    reason = get_ban_reason(user_id)
    if reason:
        text = f"{SYM['ban']} *Вы заблокированы в этом боте*\n\n*Причина:* {esc(reason)}"
    else:
        text = f"{SYM['ban']} *Вы заблокированы в этом боте* и не можете использовать его функции\\."
    try:
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    except Exception:
        try:
            await update.message.reply_text(text.replace('\\', '').replace('*', ''))
        except Exception:
            pass


def create_sponsor_link(admin_id, title, description, target_user_id=None, custom_id=None):
    return create_anon_link(target_user_id or admin_id, title, description, is_sponsor=True, sponsor_owner_id=admin_id, custom_id=custom_id)


def get_sponsor_links(admin_id):
    return run_query('SELECT link_id, title, description, created_at, user_id, custom_id FROM links WHERE is_sponsor = 1 AND sponsor_owner_id = ?', (admin_id,), fetch="all")


def transfer_sponsor_link(link_id, new_user_id):
    return run_query('UPDATE links SET user_id = ? WHERE link_id = ?', (new_user_id, link_id), commit=True)


def update_link_title(link_id, title):
    result = run_query('UPDATE links SET title = ? WHERE link_id = ?', (title, link_id), commit=True)
    push_db_to_github(f"Update link {link_id} title")
    return result


def update_link_description(link_id, description):
    result = run_query('UPDATE links SET description = ? WHERE link_id = ?', (description, link_id), commit=True)
    push_db_to_github(f"Update link {link_id} description")
    return result


def delete_link_completely(link_id):
    try:
        run_query('DELETE FROM replies WHERE message_id IN (SELECT message_id FROM messages WHERE link_id = ?)', (link_id,), commit=True)
        run_query('DELETE FROM messages WHERE link_id = ?', (link_id,), commit=True)
        run_query('DELETE FROM links WHERE link_id = ?', (link_id,), commit=True)
        push_db_to_github(f"Completely delete link {link_id}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при удалении ссылки: {e}")
        return False


def delete_message_completely(message_id):
    try:
        run_query('DELETE FROM replies WHERE message_id = ?', (message_id,), commit=True)
        run_query('DELETE FROM messages WHERE message_id = ?', (message_id,), commit=True)
        push_db_to_github(f"Completely delete message {message_id}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщения: {e}")
        return False


def get_message_info(message_id):
    # ВАЖНО: тут нужны ОБА числовых ID — from_user_id и to_user_id.
    # Причина: кнопка "Ответить" висит на message_id в обе стороны диалога
    # (и когда владелец ссылки отвечает анонимному отправителю, и когда
    # отправитель отвечает на этот ответ) — направление отправки зависит
    # от того, КТО именно сейчас пишет текст (см. handle_text), а не от
    # фиксированной колонки.
    return run_query('''
        SELECT m.message_text, m.message_type, m.file_name, m.created_at,
               u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title, l.link_id, m.to_user_id, m.from_user_id
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        WHERE m.message_id = ?
    ''', (message_id,), fetch="one")


def get_link_owner(link_id):
    return run_query('SELECT user_id FROM links WHERE link_id = ?', (link_id,), fetch="one")


def get_message_owner(message_id):
    return run_query('SELECT from_user_id FROM messages WHERE message_id = ?', (message_id,), fetch="one")


def get_all_data_for_html():
    data = {}
    try:
        data['stats'] = get_admin_stats()
        data['users'] = run_query('''
            SELECT u.user_id, u.username, u.first_name, u.created_at, u.is_banned, u.ban_reason,
                   (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id AND l.is_active = 1) as link_count,
                   (SELECT COUNT(*) FROM messages m WHERE m.to_user_id = u.user_id AND m.is_active = 1) as received_messages,
                   (SELECT COUNT(*) FROM messages m WHERE m.from_user_id = u.user_id AND m.is_active = 1) as sent_messages
            FROM users u
            ORDER BY u.created_at DESC
        ''', fetch="all") or []

        data['links'] = run_query('''
            SELECT l.link_id, l.title, l.description, l.created_at, l.expires_at, l.is_sponsor,
                   u.username, u.first_name, u.user_id,
                   (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id AND m.is_active = 1) as message_count
            FROM links l
            LEFT JOIN users u ON l.user_id = u.user_id
            WHERE l.is_active = 1
            ORDER BY l.created_at DESC
        ''', fetch="all") or []

        data['recent_messages'] = run_query('''
            SELECT m.message_id, m.message_text, m.message_type, m.file_size, m.file_name, m.created_at,
                   u_from.username as from_username, u_from.first_name as from_first_name, u_from.user_id as from_user_id,
                   u_to.username as to_username, u_to.first_name as to_first_name, u_to.user_id as to_user_id,
                   l.title as link_title, l.link_id
            FROM messages m
            LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
            LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
            LEFT JOIN links l ON m.link_id = l.link_id
            WHERE m.is_active = 1
            ORDER BY m.created_at DESC
            LIMIT 300
        ''', fetch="all") or []

    except Exception as e:
        logging.error(f"Ошибка при получении данных для HTML: {e}")
        data = {'stats': get_admin_stats(), 'users': [], 'links': [], 'recent_messages': []}

    return data


# ══════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════

def safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_str(value, default=""):
    if value is None:
        return default
    return str(value)


def esc(text) -> str:
    """
    Экранирует ЛЮБОЙ текст для MarkdownV2. Единая точка экранирования —
    используем её для КАЖДОГО куска динамических данных (юзерский ввод,
    имена, причины бана, тексты рассылки и т.д.), включая literal-текст
    внутри f-строк, чтобы больше никогда не было рассинхрона между
    "тут заэкранировано, а тут забыли".
    """
    if text is None:
        return ""
    text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!\\'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


def esc_code(text) -> str:
    """
    Экранирует текст для использования ВНУТРИ MarkdownV2 code-span (`...`).
    Внутри code-конструкций Telegram распознаёт экранирование ТОЛЬКО
    обратного слеша и обратной кавычки. Если использовать здесь обычный
    esc() (который экранирует точки/тире/etc для обычного текста), в
    code-span попадут литеральные "\\." — из-за этого ссылки визуально
    ломались и не копировались/не открывались как надо. Отдельная функция —
    единая точка правды для всего, что рендерится внутри `backticks`.
    """
    if text is None:
        return ""
    text = str(text)
    return text.replace('\\', '\\\\').replace('`', '\\`')


def quote(text: str) -> str:
    """
    Оборачивает уже подготовленный (эскейпленный) текст в MarkdownV2
    blockquote — красивое визуальное выделение цитируемого контента
    (описания ссылок, тексты входящих сообщений, ответы и т.д.) во всех
    менюшках. Каждая строка получает префикс '>' согласно синтаксису
    MarkdownV2 blockquote.
    """
    if not text:
        return text
    return "\n".join(f">{line}" for line in text.split("\n"))


def quote_expandable(text: str) -> str:
    """
    Раскрывающаяся цитата MarkdownV2 (expandable blockquote, Bot API 7.4+).
    В отличие от quote() — сворачивается в один тап, что удобно для длинных
    описаний ссылок/сообщений в предпросмотрах. Синтаксис: первая строка
    получает префикс '**>', остальные — '>', последняя заканчивается '||'.
    """
    if not text:
        return text
    lines = text.split("\n")
    lines[0] = f"**>{lines[0]}"
    lines[1:] = [f">{line}" for line in lines[1:]]
    lines[-1] = f"{lines[-1]}||"
    return "\n".join(lines)


def spoiler(text: str) -> str:
    """
    Спойлер MarkdownV2 / Rich Markdown (``||текст||``) — скрывает
    содержимое до тапа пользователя (RichTextSpoiler, Bot API 10.1+;
    тот же синтаксис поддерживается и в обычном MarkdownV2). Используем
    для превью чужого контента в списках (сообщения, ответы) — чтобы
    не светить их при взгляде через плечо, пока не тапнешь конкретное.
    Принимает уже подготовленный (эскейпленный) текст.
    """
    if not text:
        return text
    return f"||{text}||"


def table_md(headers, rows) -> str:
    """
    GFM pipe-таблица для Rich Markdown — рендерится нативной таблицей
    Telegram (RichBlockTable, Bot API 10.1+, is_compact добавлен в 10.3)
    вместо россыпи строк "точка — значение". headers/rows должны
    приходить уже подготовленными (эскейпленными через esc()/esc_code()
    там, где нужно) — как и везде в этом файле, единая точка эскейпинга
    остаётся на вызывающей стороне. Падает обратно на обычный текст
    автоматически вместе с остальным rich_message, если Rich Messages
    недоступны (см. send_rich_or_plain/show_screen).
    """
    def _row(cells):
        return "| " + " | ".join(str(c) for c in cells) + " |"
    if not headers:
        return ""
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    lines = [_row(headers), sep] + [_row(r) for r in rows]
    return "\n".join(lines)


def header(text: str, icon: str = "") -> str:
    """
    Единый стиль заголовка экрана — жирный текст в виде blockquote-плашки.
    Раньше заголовки ("*Главное меню*" и т.д.) были просто болдом вразнобой
    по всем менюшкам — теперь единая точка стиля: заголовок всегда выделен
    цитатой, тело экрана идёт под ним обычным текстом.
    """
    prefix = f"{icon} " if icon else ""
    return quote(f"*{prefix}{esc(text)}*")


def header_html(text: str, icon: str = "") -> str:
    """То же самое, но для экранов на parse_mode='HTML' (рассылка и т.п.)."""
    prefix = f"{esc_html(icon)} " if icon else ""
    return f"<blockquote><b>{prefix}{esc_html(text)}</b></blockquote>"


def esc_html(text) -> str:
    """HTML-экранирование для литерального (не форматированного) текста —
    только &, < и > обязательны для parse_mode='HTML'."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def user_html(message_or_text) -> str:
    """
    Достаёт текст/подпись пользователя С СОХРАНЕНИЕМ форматирования
    (жирный, курсив, ссылки, спойлеры и т.д.), которое он сам применил
    при наборе сообщения. PTB конвертирует Telegram-entities в готовый
    HTML — это и есть форматирование "от пользователя", которое раньше
    убивалось поголовным esc() для MarkdownV2.
    Принимает либо объект Message, либо голую строку (тогда просто
    HTML-экранирует её без форматирования — на случай текста, у
    которого нет message-объекта, например превью рассылки).
    """
    if message_or_text is None:
        return ""
    if isinstance(message_or_text, str):
        return esc_html(message_or_text)
    msg = message_or_text
    try:
        if getattr(msg, 'text', None):
            return msg.text_html
        if getattr(msg, 'caption', None):
            return msg.caption_html
    except Exception:
        pass
    return esc_html(getattr(msg, 'text', None) or getattr(msg, 'caption', None) or "")


def quote_html(html_text: str) -> str:
    """HTML blockquote — визуальное цитирование текста пользователя (уже
    подготовленного, с сохранённым форматированием) в уведомлениях."""
    if not html_text:
        return html_text
    return f"<blockquote>{html_text}</blockquote>"


def quote_html_expandable(html_text: str) -> str:
    """
    Раскрывающийся HTML blockquote (Bot API 7.4+, атрибут expandable) —
    "рич"-версия quote_html() для уведомлений о входящих анонимных
    сообщениях/ответах: длинный текст сворачивается, разворачивается по тапу.
    """
    if not html_text:
        return html_text
    return f"<blockquote expandable>{html_text}</blockquote>"


def buttons_to_keyboard(buttons, extra_rows=None):
    """Собирает InlineKeyboardMarkup из [(label, url), ...] + опциональных доп. рядов кнопок."""
    rows = [[InlineKeyboardButton(label, url=url)] for label, url in buttons]
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows) if rows else None


def button_manager_keyboard(buttons, back_callback):
    """
    Клавиатура визуального редактора кнопок-ссылок — пришёл на замену
    ручному вводу строк [[Текст|ссылка]] в тексте (регекс-парсинг такого
    формата был источником багов: конфликтовал с форматированием текста,
    молча ломался при опечатках). Список текущих кнопок с удалением по
    тапу + добавление новой + выход.

    "Добавить кнопку" теперь показывается всегда, а не прячется по
    достижении лимита — при лимите она просто помечена disabled
    (DisabledButton, Bot API 10.3, см. show_button_manager/show_screen),
    чтобы было видно, что действие существует, но временно недоступно,
    а не что оно пропало из интерфейса.
    """
    rows = []
    for i, (label, url) in enumerate(buttons):
        short = label if len(label) <= 28 else label[:27] + "…"
        rows.append([InlineKeyboardButton(f"{SYM['cross']} {short}", callback_data=f"btn_remove_{i}")])
    rows.append([InlineKeyboardButton(f"{SYM['add']} Добавить кнопку", callback_data="btn_add")])
    rows.append([InlineKeyboardButton(f"{SYM['back']} Готово", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


async def show_button_manager(update, context):
    """Экран редактора кнопок — общий для рассылки и личного сообщения от админа."""
    buttons = context.user_data.get('btn_list', [])
    target = context.user_data.get('btn_target')
    back_cb = "btn_back_broadcast" if target == 'broadcast' else "btn_back_admin_message"
    at_limit = len(buttons) >= 8
    if buttons:
        lines = "\n".join(f"{i+1}\\. *{esc(label)}* → `{esc_code(url)}`" for i, (label, url) in enumerate(buttons))
    else:
        lines = "_Кнопок пока нет_"
    limit_note = f"\n{SYM['warn']} Достигнут лимит в 8 кнопок{esc('.')}" if at_limit else ""
    await show_screen(
        update, context,
        f"{header('Кнопки-ссылки', SYM['link'])}\n\n{lines}\n\n"
        f"{SYM['dot']} Нажмите на кнопку в списке ниже, чтобы удалить её{esc('.')}{limit_note}",
        button_manager_keyboard(buttons, back_cb),
        disabled_callbacks={"btn_add"} if at_limit else None
    )


async def show_broadcast_preview(update, context):
    """Экран предпросмотра рассылки (текст с сохранённым форматированием + кнопки)."""
    text_plain = context.user_data.get('broadcast_message', '')
    text_html_stored = context.user_data.get('broadcast_message_html') or esc_html(text_plain)
    buttons = context.user_data.get('btn_list', [])
    await show_screen(
        update, context,
        f"{header_html('Предпросмотр рассылки', SYM['broadcast'])}\n\n{quote_html(text_html_stored)}\n\n"
        f"{SYM['check']} Сообщение готово к отправке\n"
        f"{SYM['dot']} Форматирование (жирный/курсив/подчёркнутый/зачёркнутый/моно/код) сохраняется как в наборе.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{SYM['link']} Кнопки ({len(buttons)})", callback_data="btn_manage_broadcast")],
            [InlineKeyboardButton(f"{SYM['broadcast']} Отправить", callback_data="broadcast_send")],
            [InlineKeyboardButton(f"{SYM['write']} Редактировать текст", callback_data="admin_broadcast")]
        ]),
        parse_mode='HTML'
    )


async def show_admin_message_preview(update, context):
    """Экран предпросмотра личного сообщения пользователю от админа (текст + кнопки)."""
    target_user_id = context.user_data.get('admin_messaging_user')
    text_plain = context.user_data.get('admin_message_text', '')
    text_html_stored = context.user_data.get('admin_message_html') or esc_html(text_plain)
    buttons = context.user_data.get('btn_list', [])
    await show_screen(
        update, context,
        f"{header_html(f'Сообщение пользователю {target_user_id}', SYM['write'])}\n\n{quote_html(text_html_stored)}\n\n"
        f"{SYM['dot']} Форматирование сохраняется как в наборе.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{SYM['link']} Кнопки ({len(buttons)})", callback_data="btn_manage_admin_message")],
            [InlineKeyboardButton(f"{SYM['check']} Отправить", callback_data="admin_message_send")],
            [InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data=f"admin_user_manage_{target_user_id}")]
        ]),
        parse_mode='HTML'
    )


URL_RE = re.compile(r'^(https?://|t\.me/|www\.)', re.IGNORECASE)


def looks_like_link(text: str) -> bool:
    """Определяет, похоже ли сообщение на голую ссылку (для типа 'link')."""
    if not text:
        return False
    t = text.strip()
    return bool(URL_RE.match(t)) and ' ' not in t and '\n' not in t


def format_datetime(dt_string):
    """Форматирует дату-время (Красноярское время UTC+7)."""
    if isinstance(dt_string, str):
        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f']:
            try:
                dt = datetime.strptime(dt_string, fmt)
                break
            except ValueError:
                continue
        else:
            return dt_string
    elif isinstance(dt_string, datetime):
        dt = dt_string
    else:
        return str(dt_string)

    krasnoyarsk_time = dt + timedelta(hours=7)
    return krasnoyarsk_time.strftime("%Y-%m-%d %H:%M:%S") + " (KRA)"


def escape_html_safe(text):
    if not text:
        return ""
    return html.escape(str(text))


def human_file_size(file_size):
    """Человекочитаемый размер файла (KB/MB) для отчётов и уведомлений."""
    if not file_size:
        return ""
    kb = file_size / 1024
    return f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"


def markdownv2_to_rich_markdown(text: str) -> str:
    """
    Конвертирует уже готовый MarkdownV2-текст (собранный esc()/quote()/
    header()/spoiler()/quote_expandable() и т.д. по всему файлу) в текст,
    валидный для поля rich_message.markdown (Bot API 10.1+).

    Rich Markdown — это GFM-совместимый диалект (официальные спеки Rich
    Message Formatting), и многое он понимает сам, без переделки:
      * > — блочную цитату (то же, что quote());
      * ||...|| — спойлер (то же, что spoiler());
      * `...` — инлайн-код;
      * \\X — экранирование пунктуации (\\. \\- \\| \\> и т.д.) работает
        ровно как в MarkdownV2, поэтому esc() переделывать не нужно.

    Здесь устраняются только РЕАЛЬНЫЕ отличия диалектов:
      1. Жирный: MarkdownV2 — *bold*, Rich Markdown (GFM) — **bold**.
         Неэкранированные одиночные * (вне code-спанов и HTML-блоков)
         удваиваются.
      2. Раскрывающаяся цитата MarkdownV2 (**>текст||) конвертируется в
         нативный <blockquote expandable> (RichBlockExpandableBlockQuotation,
         Bot API 10.3) — это тот же «разворачиваемый» блок, а не <details>
         со спойлер-видом. Внутри HTML-блоков markdown НЕ парсится, поэтому
         MarkdownV2-экранирование там снимается сразу.
      3. Внутри code-спанов MarkdownV2 требует экранировать \\ и `
         (так делает esc_code()), а GFM рендерит содержимое code-span
         буквально — это экранирование снимается.
      4. Заголовок экрана (header() → однострочная цитата с болдом
         ">**текст**") превращается в нативный заголовок "## текст".

    Это единая точка конвертации — правится один раз здесь, а не в 150+
    местах по файлу. Все show_screen()/send_rich_message()/edit_rich_message()
    пропускают текст через неё перед rich-вызовом; обычный MarkdownV2-
    fallback получает исходный текст как есть.
    """
    if not text:
        return text

    _CODE_SPAN = r'`(?:[^`\\\n]|\\.)*`'
    _EXPANDABLE_TAG = r'<blockquote expandable>.*?</blockquote>'

    def _unescape_mdv2(s: str) -> str:
        # Снимает MarkdownV2-экранирование символов, которые экранирует esc():
        # _ * [ ] ( ) ~ ` > # + - = | { } . ! \ — внутри HTML-блока они были
        # бы видны буквально как «\X», т.к. там markdown не парсится.
        return re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', s)

    # 1. Раскрывающаяся цитата MarkdownV2: первая строка "**>текст",
    #    остальные ">текст", последняя заканчивается "||" (см.
    #    quote_expandable()) -> <blockquote expandable>…</blockquote>.
    def _convert_expandable(src: str) -> str:
        lines = src.split("\n")
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.startswith("**>"):
                out.append(line)
                i += 1
                continue
            first = line[3:]
            # Однострочный случай: "**>текст||" целиком в одной строке.
            if first.endswith("||") and (i + 1 >= len(lines) or not lines[i + 1].startswith(">")):
                out.append("<blockquote expandable>" + _unescape_mdv2(first[:-2]) + "</blockquote>")
                i += 1
                continue
            body = [first]
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                content = lines[i][1:]
                if content.endswith("||") and (i + 1 >= len(lines) or not lines[i + 1].startswith(">")):
                    content = content[:-2]
                    body.append(content)
                    i += 1
                    break
                body.append(content)
                i += 1
            out.append("<blockquote expandable>" + "\n".join(_unescape_mdv2(b) for b in body) + "</blockquote>")
        return "\n".join(out)

    text = _convert_expandable(text)

    # 2. Жирный: *bold* -> **bold**. Не трогаем code-спаны, уже экранированные
    #    \* (литеральные звёздочки пользователя) и HTML-блоки, которые сами
    #    сгенерировали на шаге 1 (в них markdown не парсится).
    parts = re.split(r'(' + _CODE_SPAN + r'|' + _EXPANDABLE_TAG + r')', text, flags=re.DOTALL)
    for idx in range(0, len(parts), 2):  # чётные индексы — обычный текст
        parts[idx] = re.sub(r'(?<![\\*])\*(?!\*)', '**', parts[idx])
    text = "".join(parts)

    # 3. code-спаны: снимаем MarkdownV2-экранирование \\ и \` — GFM рендерит
    #    содержимое code-span буквально, без обработки бэкслеш-экранирования.
    def _fix_code_span(m):
        inner = m.group(0)[1:-1]
        return "`" + inner.replace("\\`", "`").replace("\\\\", "\\") + "`"

    text = re.sub(_CODE_SPAN, _fix_code_span, text)

    # 4. Заголовки экранов: header() даёт однострочную цитату с болдом
    #    ">**текст**" — превращаем её в нативный заголовок Rich Markdown.
    def _heading(m):
        return "## " + m.group(1)

    text = re.sub(r'^>\*\*(.+?)\*\*$', _heading, text, flags=re.MULTILINE)

    return text



#
# ══════════════════════════════════════════════════════════════════
#  RICH MESSAGES — сырые вызовы Bot API 10.1+ (sendRichMessage,
#  editMessageText с параметром rich_message), в обход PTB.
# ══════════════════════════════════════════════════════════════════
#
# InputRichMessage принимает ровно одно из полей "markdown"/"html"/"blocks".
# Здесь используется текстовый вариант: экраны по-прежнему пишутся на обычном
# MarkdownV2 через esc()/quote()/header()/spoiler()/quote_expandable(), а перед
# rich-отправкой текст прогоняется через markdownv2_to_rich_markdown() — так
# сами экраны не пришлось переписывать под два разных диалекта разметки.
# Кнопки идут отдельным полем reply_markup, как и в обычном sendMessage, а
# с Bot API 10.3 их можно встраивать и в само тело сообщения — блоками
# <tg-button-row>/<tg-button> (RichBlockButtons/RichMessageButton), см.
# rich_button()/rich_button_row() и параметр body_buttons ниже.

async def _rich_api_call(method: str, payload: dict) -> dict:
    """
    Прямой POST-запрос к Bot API методу, которого ещё нет в python-telegram-bot.
    Namespace HTTP, а не приватные атрибуты Bot из PTB — чтобы это не
    ломалось при обновлении версии библиотеки.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload)
            return resp.json()
    except Exception as e:
        logging.warning(f"Rich API вызов {method} не удался (транспорт): {e}")
        return {"ok": False, "description": str(e)}


def disable_buttons(reply_markup, disabled_callbacks):
    """
    Помечает конкретные кнопки готовой клавиатуры как disabled
    (DisabledButton / поле disabled в InlineKeyboardButton, Bot API 10.3) —
    кнопка остаётся видна, но визуально "неактивна", вместо того чтобы
    просто исчезать из разметки. PTB ещё не знает про этот параметр в
    своём конструкторе InlineKeyboardButton, поэтому патчим уже готовый
    to_dict() перед отправкой напрямую в Bot API (см. _rich_api_call) —
    сам объект reply_markup, который видит PTB-путь (обычный fallback),
    не трогаем: там кнопка просто останется кликабельной, что не ломает
    функциональность, а лишь не показывает лишнюю визуальную подсказку.
    disabled_callbacks — множество callback_data, которые нужно
    "притушить".
    """
    if reply_markup is None or not disabled_callbacks:
        return reply_markup.to_dict() if reply_markup is not None else None
    data = reply_markup.to_dict()
    for row in data.get("inline_keyboard", []):
        for btn in row:
            if btn.get("callback_data") in disabled_callbacks:
                btn["disabled"] = True
    return data


# (префикс callback_data, стиль) — раскраска inline-кнопок. Значения стиля:
# "danger" (красный), "success" (зелёный), "primary" (синий). Совпадение —
# по префиксу, т.к. большинство callback_data содержат динамические id.
# Порядок важен: более конкретные префиксы идут раньше более общих.
BUTTON_STYLE_RULES = (
    # — разрушительные/необратимые действия → красный —
    ("admin_confirm_delete_user_", "danger"),
    ("admin_delete_user_", "danger"),
    ("admin_ban_user_", "danger"),
    ("admin_delete_sponsor_", "danger"),
    ("confirm_delete_link_", "danger"),
    ("confirm_delete_message_", "danger"),
    ("delete_", "danger"),
    ("btn_remove_", "danger"),
    # списание виол = «минус» → красный
    ("admin_econ_pick_take_", "danger"),
    ("admin_econ_user_take_", "danger"),
    # — положительные/подтверждающие действия → зелёный —
    ("admin_unban_user_", "success"),
    ("admin_econ_pick_give_", "success"),
    ("admin_econ_user_give_", "success"),
    ("broadcast_send", "success"),
    ("admin_message_send", "success"),
    ("link_type_sponsor", "success"),
    ("link_confirm_sponsor", "success"),
    # — главные/рекомендуемые действия → синий —
    ("create_link", "primary"),
    ("link_type_normal", "primary"),
    ("admin_broadcast", "primary"),
    ("admin_stats", "primary"),
)


def classify_button_style(callback_data):
    """Возвращает стиль кнопки по её callback_data (или None = дефолтный)."""
    if not callback_data:
        return None
    for prefix, style in BUTTON_STYLE_RULES:
        if callback_data.startswith(prefix):
            return style
    return None


def rich_button(text, data=None, url=None, style="primary"):
    """
    Кнопка В ТЕЛЕ rich-сообщения — <tg-button> (RichMessageButton, Bot API
    10.3). Это те самые «цветные кнопки прямо в сообщении», в отличие от
    reply_markup-клавиатуры под сообщением. style: "danger"/"success"/
    "primary"/"link" ("link" — только для callback-кнопок).
    """
    label = esc_html(text)
    if url:
        return f'<tg-button type="url" style="{style}" url="{html.escape(str(url), quote=True)}">{label}</tg-button>'
    return f'<tg-button type="callback_data" style="{style}" data="{html.escape(str(data), quote=True)}">{label}</tg-button>'


def rich_button_row(buttons, align="center"):
    """
    Ряд кнопок в теле rich-сообщения — <tg-button-row> (RichBlockButtons,
    Bot API 10.3). buttons — список строк от rich_button(). align:
    "left"/"center"/"right". До 8 кнопок в ряду.
    """
    return f'<tg-button-row align="{align}">' + "".join(buttons) + "</tg-button-row>"


def markup_to_payload(reply_markup, disabled_callbacks=None, force_reply: bool = False):
    """
    Готовит reply_markup-словарь для сырого вызова Bot API, объединяя
    DisabledButton (см. disable_buttons), раскраску кнопок style
    (см. classify_button_style, Bot API 9.4+) и поле force_reply
    (InlineKeyboardMarkup.force_reply / ReplyKeyboardMarkup.force_reply,
    Bot API 10.3) — просит клиент сразу открыть текстовый ввод, когда
    экран ждёт от пользователя текст (создание ссылки, ответ, причина
    бана и т.д.), без отдельного ForceReply-сообщения.
    Только для rich-пути (сырые HTTP-вызовы) — PTB ещё не знает про эти
    поля в своих моделях, так что на обычном fallback (когда рич
    недоступен) экран просто ведёт себя как раньше, без раскраски и
    принудительного ввода — это не ломает функциональность, лишь не
    показывает лишнее удобство.
    """
    data = disable_buttons(reply_markup, disabled_callbacks)
    if data is not None:
        # style поверх уже готового словаря (не пересоздаём to_dict заново).
        for row in data.get("inline_keyboard", []):
            for btn in row:
                style = classify_button_style(btn.get("callback_data"))
                if style:
                    btn["style"] = style
        if force_reply:
            data["force_reply"] = True
    return data


async def send_rich_message(chat_id, markdown_text: str, reply_markup=None, disabled_callbacks=None, force_reply: bool = False, body_buttons=None):
    """sendRichMessage — возвращает message_id при успехе, иначе None.
    markdown_text приходит в диалекте MarkdownV2 (как везде в файле) и
    конвертируется в rich-markdown внутри — см. markdownv2_to_rich_markdown().
    disabled_callbacks — см. disable_buttons() (DisabledButton, Bot API 10.3).
    force_reply — см. markup_to_payload() (force_reply, Bot API 10.3).
    body_buttons — список строк от rich_button_row() (RichBlockButtons /
    <tg-button-row>, Bot API 10.3): цветные кнопки В ТЕЛЕ сообщения. Если
    заданы, они заменяют reply_markup на rich-пути (клавиатура убирается,
    навигация живёт в самих кнопках); reply_markup остаётся только для
    MarkdownV2-fallback.
    """
    md = markdownv2_to_rich_markdown(markdown_text)
    if body_buttons:
        md = md + "\n\n" + "\n".join(body_buttons)
    payload = {"chat_id": chat_id, "rich_message": {"markdown": md}}
    if body_buttons:
        # Кнопки теперь в теле сообщения — прячем reply_markup-клавиатуру
        # (пустая inline-клавиатура снимает прежнюю при edit).
        payload["reply_markup"] = {"inline_keyboard": []}
    elif reply_markup is not None:
        payload["reply_markup"] = markup_to_payload(reply_markup, disabled_callbacks, force_reply)
    data = await _rich_api_call("sendRichMessage", payload)
    if data.get("ok"):
        result = data.get("result") or {}
        if "message_id" in result:
            return result["message_id"]
    logging.info(f"sendRichMessage недоступен, будет откат на MarkdownV2: {data.get('description')}")
    return None


async def edit_rich_message(chat_id, message_id, markdown_text: str, reply_markup=None, disabled_callbacks=None, force_reply: bool = False, body_buttons=None) -> bool:
    """editMessageText(rich_message=...) — True при успехе.
    markdown_text приходит в диалекте MarkdownV2 (как везде в файле) и
    конвертируется в rich-markdown внутри — см. markdownv2_to_rich_markdown().
    force_reply — см. markup_to_payload() (force_reply, Bot API 10.3).
    body_buttons — см. send_rich_message() (кнопки в теле сообщения).
    """
    md = markdownv2_to_rich_markdown(markdown_text)
    if body_buttons:
        md = md + "\n\n" + "\n".join(body_buttons)
    payload = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"markdown": md}}
    if body_buttons:
        payload["reply_markup"] = {"inline_keyboard": []}
    elif reply_markup is not None:
        payload["reply_markup"] = markup_to_payload(reply_markup, disabled_callbacks, force_reply)
    data = await _rich_api_call("editMessageText", payload)
    if data.get("ok"):
        return True
    if "message is not modified" in str(data.get("description", "")).lower():
        return True
    logging.info(f"editMessageText(rich_message) недоступен, будет откат на MarkdownV2: {data.get('description')}")
    return False


async def send_rich_or_plain(bot, chat_id, text: str, parse_mode: str = 'MarkdownV2', reply_markup=None):
    """
    Для одноразовых отправок вне show_screen (уведомления, /start с
    аргументом-ссылкой) — тот же принцип: пробуем Rich Message (markdown
    или html — под parse_mode), при неудаче тихо откатываемся на обычный
    send_message. При parse_mode='MarkdownV2' текст конвертируется в
    rich-markdown диалект — см. markdownv2_to_rich_markdown(). Возвращает
    отправленный message_id.
    """
    if RICH_MESSAGES_ENABLED and parse_mode in ('MarkdownV2', 'HTML'):
        payload = {"chat_id": chat_id}
        if parse_mode == 'MarkdownV2':
            payload["rich_message"] = {"markdown": markdownv2_to_rich_markdown(text)}
        else:
            # <blockquote expandable> — валидный тег Rich HTML (Bot API 10.3,
            # RichBlockExpandableBlockQuotation), его можно отправлять как есть.
            payload["rich_message"] = {"html": text}
        if reply_markup is not None:
            payload["reply_markup"] = markup_to_payload(reply_markup)
        data = await _rich_api_call("sendRichMessage", payload)
        if data.get("ok"):
            result = data.get("result") or {}
            if "message_id" in result:
                return result["message_id"]
        logging.info(f"sendRichMessage недоступен для уведомления, откат на {parse_mode}: {data.get('description')}")

    sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    return sent.message_id


# ══════════════════════════════════════════════════════════════════
#  ЕДИНЫЙ МЕНЕДЖЕР ЭКРАНА — чтобы меню не плодились новыми сообщениями
# ══════════════════════════════════════════════════════════════════
#
# Проблема была в том, что после текстового ввода (создание ссылки,
# ответ, причина бана и т.д.) бот слал НОВОЕ сообщение через
# update.message.reply_text(), а предыдущее меню оставалось висеть
# в чате — меню "плодились". Теперь у каждого пользователя хранится
# id последнего экрана-меню в user_data['screen_msg_id'], и любое
# обновление экрана идёт через edit, а не через новое сообщение.

async def send_screen(context: ContextTypes.DEFAULT_TYPE, chat_id, text: str,
                      reply_markup=None, parse_mode='MarkdownV2',
                      disabled_callbacks=None, force_reply: bool = False,
                      body_buttons=None):
    """
    Отправляет НОВОЕ сообщение-экран, отдавая приоритет Rich Message
    (sendRichMessage, Bot API 10.1+), и запоминает состояние экрана в
    user_data (screen_msg_id / screen_is_rich / screen_force_reply).
    body_buttons — кнопки в теле сообщения (см. send_rich_message).
    Возвращает message_id отправленного сообщения.
    """
    want_rich = RICH_MESSAGES_ENABLED and parse_mode == 'MarkdownV2'
    if want_rich:
        msg_id = await send_rich_message(chat_id, text, reply_markup, disabled_callbacks, force_reply, body_buttons)
        if msg_id is not None:
            context.user_data['screen_msg_id'] = msg_id
            context.user_data['screen_is_rich'] = True
            context.user_data['screen_force_reply'] = bool(force_reply)
            return msg_id

    sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    context.user_data['screen_msg_id'] = sent.message_id
    context.user_data['screen_is_rich'] = False
    context.user_data['screen_force_reply'] = False
    return sent.message_id


async def show_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                       reply_markup=None, parse_mode='MarkdownV2', disabled_callbacks=None,
                       force_reply: bool = False, body_buttons=None):
    """
    Показывает "экран" — редактируя предыдущее сообщение-меню этого
    пользователя, либо отправляя новое, если редактировать нечего.
    Работает как из callback_query, так и из обычного текстового апдейта.

    Rich-путь (sendRichMessage/editMessageText с rich_message, Bot API 10.1+)
    используется для MarkdownV2-экранов при включённом RICH_MESSAGES_ENABLED;
    при любой ошибке тихо откатываемся на обычный sendMessage/editMessageText.

    "Режим" экрана (rich/plain и значение force_reply) запоминается в
    user_data. Если новому экрану нужен другой режим, чем у текущего
    сообщения, старое сообщение заменяется новым: Telegram не позволяет
    при edit сменить тип сообщения (text <-> rich) или значение force_reply,
    поэтому без этой проверки rich-экраны "застревали" бы в обычном
    MarkdownV2 после первого же экрана, отправленного через reply_text.

    disabled_callbacks — множество callback_data, которые нужно показать
    "притушенными" (disabled, Bot API 10.3); работает только на rich-пути,
    на обычном откате кнопка остаётся кликабельной.
    force_reply — просит клиент сразу открыть текстовый ввод (force_reply,
    Bot API 10.3); тоже только rich-путь.
    body_buttons — кнопки в теле сообщения (RichBlockButtons, Bot API 10.3,
    см. send_rich_message); на rich-пути они заменяют reply_markup, на
    обычном откате используется reply_markup.
    """
    chat_id = update.effective_chat.id
    want_rich = RICH_MESSAGES_ENABLED and parse_mode == 'MarkdownV2'

    query = update.callback_query
    edit_msg_id = (query.message.message_id
                   if query is not None and query.message is not None
                   else context.user_data.get('screen_msg_id'))

    current_rich = bool(context.user_data.get('screen_is_rich', False))
    current_fr = bool(context.user_data.get('screen_force_reply', False))

    # 1. Пробуем отредактировать текущее сообщение, если его режим совпадает
    #    с требуемым (rich <-> rich, plain <-> plain, force_reply неизменен).
    if edit_msg_id:
        same_mode = (current_rich == want_rich)
        same_fr = (bool(force_reply) == current_fr) if want_rich else True

        if same_mode and same_fr:
            if want_rich:
                if await edit_rich_message(chat_id, edit_msg_id, text, reply_markup, disabled_callbacks, force_reply, body_buttons):
                    context.user_data['screen_msg_id'] = edit_msg_id
                    context.user_data['screen_is_rich'] = True
                    context.user_data['screen_force_reply'] = bool(force_reply)
                    return
            else:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=edit_msg_id,
                        text=text, parse_mode=parse_mode, reply_markup=reply_markup
                    )
                    context.user_data['screen_msg_id'] = edit_msg_id
                    context.user_data['screen_is_rich'] = False
                    context.user_data['screen_force_reply'] = False
                    return
                except BadRequest as e:
                    if "Message is not modified" in str(e):
                        context.user_data['screen_msg_id'] = edit_msg_id
                        context.user_data['screen_is_rich'] = False
                        context.user_data['screen_force_reply'] = False
                        return
                except TelegramError as e:
                    logging.error(f"Ошибка edit (по id): {e}")

        # Режим сменился или edit не прошёл — убираем старое сообщение, чтобы
        # не оставлять его висеть поверх нового (не плодить экраны).
        if context.user_data.get('screen_msg_id') == edit_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=edit_msg_id)
            except Exception:
                pass
        context.user_data.pop('screen_msg_id', None)

    # 2. Отправляем новый экран.
    await send_screen(context, chat_id, text, reply_markup, parse_mode,
                      disabled_callbacks=disabled_callbacks, force_reply=force_reply,
                      body_buttons=body_buttons)


FLOW_KEYS = (
    'creating_link', 'link_stage', 'link_title', 'link_description',
    'link_type', 'link_custom_id',
    'econ_action', 'econ_target_user',
    'replying_to', 'replying_to_admin',
    'creating_sponsor_link', 'sponsor_stage', 'sponsor_title', 'sponsor_description', 'sponsor_custom_id',
    'transferring_sponsor_link', 'banning_user', 'admin_messaging_user',
    'admin_message_text', 'admin_message_html',
    'broadcasting', 'broadcast_message', 'broadcast_message_html',
    'btn_target', 'btn_list', 'btn_stage', 'btn_pending_label',
    'editing_link_field', 'editing_link_field_kind', 'editing_link_return_cb',
)


def clear_flow_state(context: ContextTypes.DEFAULT_TYPE):
    """
    Сбрасывает ВСЕ пошаговые диалоговые состояния (создание ссылки, ответ,
    рассылка и т.д.). Нужно, чтобы залипшее состояние одного меню не
    "проглатывало" текст, предназначенный для другого места — например,
    когда пользователь отвечает (свайп-reply) на оповещение администратора,
    а бот по ошибке трактует текст как шаг мастера создания ссылки.
    """
    for key in FLOW_KEYS:
        context.user_data.pop(key, None)


async def cleanup_user_message(update: Update):
    """Удаляет исходное сообщение пользователя (ввод), чтобы не мусорить в чате."""
    try:
        await update.message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════

def chunk_rows(buttons, per_row=2):
    """Раскладывает плоский список кнопок по рядам, чтобы меню не висело
    длинным столбиком, а красиво лежало в 2 (или per_row) кнопки в ряд."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['link']} Мои ссылки", callback_data="my_links"),
            InlineKeyboardButton(f"{SYM['add']} Создать ссылку", callback_data="create_link"),
        ],
        [InlineKeyboardButton(f"{SYM['inbox']} Мои сообщения", callback_data="my_messages")],
        [InlineKeyboardButton(f"{SYM['coin']} Баланс", callback_data="my_balance")],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="main_menu")]])


def back_to_main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} Главное меню", callback_data="main_menu")]])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['stats']} Статистика", callback_data="admin_stats"),
            InlineKeyboardButton(f"{SYM['users']} Пользователи", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton(f"{SYM['gift']} Спонсорские", callback_data="admin_sponsor_links"),
            InlineKeyboardButton(f"{SYM['report']} HTML отчёт", callback_data="admin_html_report"),
        ],
        [InlineKeyboardButton(f"{SYM['broadcast']} Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton(f"{SYM['coin']} Экономика", callback_data="admin_econ_menu")],
        [InlineKeyboardButton(f"{SYM['back']} Главное меню", callback_data="main_menu")]
    ])


ECON_PAGE_SIZE = 8


def admin_econ_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{SYM['add']} Начислить", callback_data="admin_econ_pick_give_0")],
        [InlineKeyboardButton(f"{SYM['trash']} Списать", callback_data="admin_econ_pick_take_0")],
        [InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_panel")]
    ])


def main_menu_body_buttons():
    """Цветные кнопки В ТЕЛЕ сообщения (RichBlockButtons, Bot API 10.3) для
    главного меню. На rich-пути они заменяют reply_markup-клавиатуру; на
    MarkdownV2-fallback остаётся обычная main_keyboard()."""
    return [
        rich_button_row([
            rich_button(f"{SYM['link']} Мои ссылки", data="my_links", style="primary"),
            rich_button(f"{SYM['add']} Создать ссылку", data="create_link", style="success"),
        ], align="center"),
        rich_button_row([
            rich_button(f"{SYM['inbox']} Мои сообщения", data="my_messages", style="primary"),
            rich_button(f"{SYM['coin']} Баланс", data="my_balance"),
        ], align="center"),
    ]


def admin_panel_body_buttons():
    """Цветные кнопки В ТЕЛЕ сообщения для панели администратора."""
    return [
        rich_button_row([
            rich_button(f"{SYM['stats']} Статистика", data="admin_stats", style="primary"),
            rich_button(f"{SYM['users']} Пользователи", data="admin_users", style="primary"),
        ], align="center"),
        rich_button_row([
            rich_button(f"{SYM['gift']} Спонсорские", data="admin_sponsor_links"),
            rich_button(f"{SYM['report']} HTML отчёт", data="admin_html_report"),
        ], align="center"),
        rich_button_row([
            rich_button(f"{SYM['broadcast']} Оповещение", data="admin_broadcast", style="primary"),
            rich_button(f"{SYM['coin']} Экономика", data="admin_econ_menu"),
        ], align="center"),
        rich_button_row([
            rich_button(f"{SYM['back']} Главное меню", data="main_menu", style="link"),
        ], align="center"),
    ]


def econ_user_picker_keyboard(action, page, users):
    """Клавиатура выбора пользователя для начисления/списания, с листанием
    страниц — список пользователей может быть большим. Кнопки «Пред.»/«След.»
    всегда видны, а недоступная помечается disabled (Bot API 10.3), вместо
    того чтобы пропадать из разметки. Возвращает (keyboard, disabled_callbacks)."""
    total = len(users)
    start = page * ECON_PAGE_SIZE
    page_users = users[start:start + ECON_PAGE_SIZE]
    rows = []
    for u in page_users:
        username = u[1] if u[1] else (u[2] or f"ID:{u[0]}")
        label = f"@{username}" if u[1] else username
        rows.append([InlineKeyboardButton(
            f"{SYM['id']} {label}"[:32],
            callback_data=f"admin_econ_user_{action}_{u[0]}_{page}"
        )])

    disabled = set()
    if total > ECON_PAGE_SIZE:
        prev_cb = f"admin_econ_pick_{action}_{page - 1}"
        next_cb = f"admin_econ_pick_{action}_{page + 1}"
        rows.append([
            InlineKeyboardButton(f"{SYM['back']} Пред.", callback_data=prev_cb),
            InlineKeyboardButton(f"След. {SYM['arrow']}", callback_data=next_cb),
        ])
        if page <= 0:
            disabled.add(prev_cb)
        if start + ECON_PAGE_SIZE >= total:
            disabled.add(next_cb)

    rows.append([InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_econ_menu")])
    return InlineKeyboardMarkup(rows), disabled


def user_management_keyboard(user_id, is_banned=False):
    """Клавиатура управления пользователем. «Забанить» и «Разбанить» видны
    одновременно: недоступное действие помечается disabled (Bot API 10.3),
    вместо того чтобы исчезать. Возвращает (keyboard, disabled_callbacks)."""
    ban_cb = f"admin_ban_user_{user_id}"
    unban_cb = f"admin_unban_user_{user_id}"
    disabled = {ban_cb} if is_banned else {unban_cb}
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['link']} Ссылки", callback_data=f"admin_user_links_{user_id}"),
            InlineKeyboardButton(f"{SYM['view']} Переписка", callback_data=f"admin_view_conversation_{user_id}")
        ],
        [
            InlineKeyboardButton(f"{SYM['ban']} Забанить", callback_data=ban_cb),
            InlineKeyboardButton(f"{SYM['unlock']} Разбанить", callback_data=unban_cb),
        ],
        [
            InlineKeyboardButton(f"{SYM['trash']} Удалить", callback_data=f"admin_delete_user_{user_id}"),
            InlineKeyboardButton(f"{SYM['write']} Написать", callback_data=f"admin_message_user_{user_id}")
        ],
        [InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_users")]
    ]), disabled


def message_actions_keyboard(message_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['reply']} Ответить", callback_data=f"reply_{message_id}"),
            InlineKeyboardButton(f"{SYM['trash']} Удалить", callback_data=f"confirm_delete_message_{message_id}"),
        ]
    ])


def delete_confirmation_keyboard(item_type, item_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{SYM['check']} Да, удалить", callback_data=f"delete_{item_type}_{item_id}")],
        [InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="cancel_delete")]
    ])


def broadcast_formatting_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['check']} Отправить", callback_data="broadcast_send"),
            InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_panel")
        ]
    ])


def sponsor_links_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['add']} Создать", callback_data="admin_create_sponsor_link"),
            InlineKeyboardButton(f"{SYM['report']} Мои ссылки", callback_data="admin_my_sponsor_links"),
        ],
        [InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_panel")]
    ])


def sponsor_link_actions_keyboard(link_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{SYM['write']} Название", callback_data=f"link_edit_title_{link_id}"),
            InlineKeyboardButton(f"{SYM['write']} Описание", callback_data=f"link_edit_desc_{link_id}"),
        ],
        [
            InlineKeyboardButton(f"{SYM['transfer']} Передать", callback_data=f"admin_transfer_sponsor_{link_id}"),
            InlineKeyboardButton(f"{SYM['trash']} Удалить", callback_data=f"admin_delete_sponsor_{link_id}"),
        ],
        [InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_my_sponsor_links")]
    ])


TYPE_ICON = {
    "text": SYM['write'], "photo": SYM['photo'], "video": SYM['video'],
    "document": SYM['doc'], "voice": SYM['voice'], "video_note": SYM['vnote'],
    "link": SYM['link'],
}

TYPE_LABEL = {
    "photo": "[Фото]", "video": "[Видео]", "document": "[Файл]",
    "voice": "[Голосовое]", "video_note": "[Видео-кружок]",
}


# ══════════════════════════════════════════════════════════════════
#  ОСНОВНЫЕ ОБРАБОТЧИКИ
# ══════════════════════════════════════════════════════════════════

def is_admin_user(user) -> bool:
    return bool((ADMIN_USERNAME and user.username == ADMIN_USERNAME) or (ADMIN_ID and user.id == ADMIN_ID))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user

        if is_user_banned(user.id):
            await send_ban_notice(update)
            return

        save_user(user.id, user.username, user.first_name)

        if context.args:
            link_id = context.args[0]
            link_info = get_link_info(link_id)
            if link_info:
                context.user_data['current_link'] = link_id
                sponsor_badge = f"{SYM['gift']} *СПОНСОРСКАЯ ССЫЛКА*\n\n" if link_info[5] else ""
                text = (
                    header("Анонимная ссылка", SYM['link']) + "\n\n"
                    f"{sponsor_badge}"
                    f"{SYM['write']} *{esc(link_info[2])}*\n"
                    f"{quote_expandable(esc(link_info[3]))}\n\n"
                    f"Отправьте анонимное сообщение, ссылку или медиафайл{esc('.')}"
                )
                await send_screen(context, update.effective_chat.id, text, back_to_main_keyboard())
                return
            else:
                await update.message.reply_text(f"{SYM['warn']} Ссылка недействительна или больше не существует\\.", parse_mode='MarkdownV2')
                return

        text = (
            header("Анонимный Бот", SYM['menu']) + "\n\n"
            f"Создавайте ссылки для получения анонимных сообщений{esc('.')}"
        )
        await send_screen(context, update.effective_chat.id, text, main_keyboard(),
                          body_buttons=main_menu_body_buttons())
    except Exception as e:
        logging.error(f"Ошибка в команде start: {e}")
        try:
            await update.message.reply_text(f"{SYM['warn']} Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')
        except Exception:
            pass


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if not is_admin_user(user):
            await update.message.reply_text(f"{SYM['lock']} *Доступ запрещён*", parse_mode='MarkdownV2')
            return

        if not context.user_data.get('admin_authenticated'):
            # Удаляем сообщение с паролем ВСЕГДА — и при успехе, и при неудаче,
            # чтобы пароль не оставался читаемым текстом в чате в любом случае.
            try:
                await update.message.delete()
            except Exception:
                pass

            if context.args and context.args[0] == ADMIN_PASSWORD:
                context.user_data['admin_authenticated'] = True
                await send_screen(
                    context, update.effective_chat.id,
                    header("Панель администратора", SYM['check']),
                    admin_keyboard(),
                    body_buttons=admin_panel_body_buttons()
                )
            else:
                await send_screen(context, update.effective_chat.id, f"{SYM['lock']} *Доступ запрещён*")
            return
        else:
            await send_screen(
                context, update.effective_chat.id,
                header("Панель администратора", SYM['gear']),
                admin_keyboard(),
                body_buttons=admin_panel_body_buttons()
            )
    except Exception as e:
        logging.error(f"Ошибка в команде admin: {e}")
        try:
            await update.message.reply_text(f"{SYM['warn']} Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')
        except Exception:
            pass


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /balance — свой баланс (для любого пользователя)
    /balance <user_id> — баланс конкретного пользователя (только для админа)
    """
    try:
        user = update.effective_user
        target_id = user.id

        if context.args:
            if not is_admin_user(user):
                await update.message.reply_text(f"{SYM['lock']} Только администратор может смотреть чужой баланс\\.", parse_mode='MarkdownV2')
                return
            target_id = safe_int(context.args[0], default=None)
            if target_id is None:
                await update.message.reply_text(f"{SYM['warn']} Некорректный ID пользователя\\.", parse_mode='MarkdownV2')
                return

        try:
            balance = await asyncio.to_thread(vb.get_balance, target_id)
        except vb.BufferError as e:
            logging.error(f"Ошибка получения баланса для {target_id}: {e}")
            await update.message.reply_text(f"{SYM['warn']} Буфер недоступен\\. Попробуйте позже\\.", parse_mode='MarkdownV2')
            return

        who = "Ваш баланс" if target_id == user.id else f"Баланс пользователя `{target_id}`"
        await update.message.reply_text(
            f"{SYM['coin']} {who}: *{balance}* виол",
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logging.error(f"Ошибка в команде balance: {e}")
        try:
            await update.message.reply_text(f"{SYM['warn']} Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')
        except Exception:
            pass


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    try:
        user = query.from_user
        data = query.data
        is_admin = is_admin_user(user)
        context.user_data['screen_msg_id'] = query.message.message_id

        # ─── Главное меню ───
        if data == "main_menu":
            clear_flow_state(context)
            text = header("Главное меню", SYM['menu'])
            await show_screen(update, context, text, main_keyboard(),
                              body_buttons=main_menu_body_buttons())
            return

        elif data == "my_balance":
            try:
                balance = await asyncio.to_thread(vb.get_balance, user.id)
                text = (
                    header("Ваш баланс", SYM['coin']) + "\n\n"
                    f"{SYM['coin']} *{balance}* виол"
                )
            except vb.BufferError as e:
                logging.error(f"Ошибка получения баланса user_id={user.id}: {e}")
                text = f"{SYM['warn']} Не удалось получить баланс\\. Попробуйте позже\\."
            await show_screen(update, context, text, main_keyboard())
            return

        elif data == "link_confirm_sponsor":
            # Апсейл при создании ссылки: пользователь согласился сделать её
            # спонсорской. Заголовок/описание/уникальный ID уже собраны
            # текстовым мастером (см. handle_text) и лежат в user_data.
            if not context.user_data.get('creating_link'):
                await query.answer("Сессия создания ссылки истекла, начните заново", show_alert=True)
                return
            title = context.user_data.get('link_title')
            desc = context.user_data.get('link_description')
            custom_id = context.user_data.get('link_custom_id')

            # Идемпотентность: тот же принцип, что был у покупки в магазине —
            # ключ живёт по времени в секундах, чтобы двойной тап не списал дважды.
            purchase_key = f"create_sponsor_link_{user.id}_{int(time.time())}"
            ok, balance, err = await asyncio.to_thread(
                vb.try_debit, user.id, SPONSOR_LINK_PRICE,
                {"reason": "create_sponsor_link"}, purchase_key
            )
            if not ok:
                if err == "insufficient_balance":
                    text = (
                        f"{SYM['warn']} Недостаточно виол\\.\n\n"
                        f"Нужно: *{SPONSOR_LINK_PRICE}*\n"
                        f"У вас: *{balance}*\n\n"
                        f"Создать обычную ссылку вместо спонсорской?"
                    )
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['check']} Создать обычную ссылку", callback_data="link_decline_sponsor")]])
                else:
                    text = f"{SYM['warn']} Не удалось выполнить покупку\\. Попробуйте позже\\."
                    keyboard = cancel_keyboard()
                await show_screen(update, context, text, keyboard)
                return

            link_id = create_anon_link(user.id, title, desc, is_sponsor=True, sponsor_owner_id=user.id, custom_id=custom_id)
            if not link_id:
                # Списание уже прошло, а создание ссылки — нет (чаще всего —
                # выбранный уникальный ID уже занят). Возвращаем виолы, чтобы
                # пользователь не остался без ссылки и без денег, и даём
                # ввести другой ID, не начиная весь мастер заново.
                logging.error(f"Списание прошло, но создание спонсорской ссылки не удалось для user_id={user.id} (custom_id={custom_id}). Возвращаю виолы.")
                try:
                    await asyncio.to_thread(
                        vb.credit, user.id, SPONSOR_LINK_PRICE,
                        {"reason": "refund_failed_sponsor_link_create"}
                    )
                except vb.BufferError as e:
                    logging.critical(f"КРИТИЧНО: не удалось вернуть виолы user_id={user.id} после неудачного создания: {e}")

                if custom_id:
                    context.user_data['link_stage'] = 'custom_id'
                    context.user_data.pop('link_custom_id', None)
                    reason = f"ID `{esc_code(custom_id)}` уже занят"
                    await show_screen(
                        update, context,
                        f"{SYM['warn']} {reason}\\. Виолы возвращены на баланс\\.\n\n"
                        f"{SYM['id']} Введите другой уникальный ID \\(латиница, цифры, `_` `-`, 3\\-32 символа\\) или `-` для автогенерации:",
                        cancel_keyboard(),
                        force_reply=True
                    )
                else:
                    clear_flow_state(context)
                    await show_screen(update, context, f"{SYM['warn']} Не удалось создать ссылку\\. Виолы возвращены на баланс\\.", main_keyboard())
                return

            clear_flow_state(context)
            bot_username = context.bot.username
            link_url = f"https://t.me/{bot_username}?start={link_id}"
            id_line = f"{SYM['id']} ID: `{esc_code(custom_id)}`\n" if custom_id else ""
            text = (
                header("Спонсорская ссылка создана!", SYM['check']) + "\n\n"
                f"{SYM['gift']} *{esc(title)}*\n{quote_expandable(esc(desc))}\n\n"
                f"{id_line}"
                f"Списано: *{SPONSOR_LINK_PRICE}* виол\\. Остаток: *{balance}*\n\n"
                f"`{esc_code(link_url)}`\n\nПоделитесь ей, чтобы получать сообщения{esc('!')}"
            )
            await show_screen(update, context, text, main_keyboard())
            return

        elif data == "link_decline_sponsor":
            if not context.user_data.get('creating_link'):
                await query.answer("Сессия создания ссылки истекла, начните заново", show_alert=True)
                return
            title = context.user_data.get('link_title')
            desc = context.user_data.get('link_description')
            link_id = create_anon_link(user.id, title, desc)
            clear_flow_state(context)
            if link_id is None:
                await show_screen(update, context, f"{SYM['warn']} Не удалось создать ссылку\\. Попробуйте ещё раз{esc('.')}", main_keyboard())
                return
            bot_username = context.bot.username
            link_url = f"https://t.me/{bot_username}?start={link_id}"
            text = (
                header("Ссылка создана!", SYM['check']) + "\n\n"
                f"*{esc(title)}*\n{quote_expandable(esc(desc))}\n\n"
                f"`{esc_code(link_url)}`\n\nПоделитесь ей, чтобы получать сообщения{esc('!')}"
            )
            await show_screen(update, context, text, main_keyboard())
            return

        # ─── Экономика (начислить/списать) — только админ, вместо /give и /take ───
        elif data == "admin_econ_menu":
            if not is_admin:
                await query.answer("Доступ запрещён", show_alert=True)
                return
            await show_screen(update, context, header("Экономика", SYM['coin']) + "\n\nВыберите действие:", admin_econ_keyboard())
            return

        elif data.startswith("admin_econ_pick_"):
            if not is_admin:
                await query.answer("Доступ запрещён", show_alert=True)
                return
            rest = data.replace("admin_econ_pick_", "")
            action, _, page_str = rest.rpartition("_")
            page = max(0, safe_int(page_str, default=0) or 0)
            if action not in ("give", "take"):
                await query.answer("Ошибка", show_alert=True)
                return
            users = get_all_users_for_admin()
            if not users:
                await show_screen(update, context, f"Пользователей не найдено{esc('.')}", admin_econ_keyboard())
                return
            action_label = "начисления" if action == "give" else "списания"
            text = header("Выберите пользователя", SYM['users']) + f"\n\nДля {action_label} виол:"
            keyboard, disabled = econ_user_picker_keyboard(action, page, users)
            await show_screen(update, context, text, keyboard, disabled_callbacks=disabled)
            return

        elif data.startswith("admin_econ_user_"):
            if not is_admin:
                await query.answer("Доступ запрещён", show_alert=True)
                return
            rest = data.replace("admin_econ_user_", "")
            parts = rest.rsplit("_", 2)
            if len(parts) != 3:
                await query.answer("Ошибка", show_alert=True)
                return
            action, target_id_str, page_str = parts
            target_id = safe_int(target_id_str)
            if action not in ("give", "take") or target_id is None:
                await query.answer("Ошибка", show_alert=True)
                return
            context.user_data['econ_action'] = action
            context.user_data['econ_target_user'] = target_id
            action_verb = "начислить" if action == "give" else "списать"
            balance_line = ""
            try:
                balance = await asyncio.to_thread(vb.get_balance, target_id)
                balance_line = f"\n{SYM['coin']} Текущий баланс: *{balance}*"
            except vb.BufferError as e:
                logging.error(f"Ошибка получения баланса для {target_id} в панели экономики: {e}")
            await show_screen(
                update, context,
                f"{SYM['coin']} Сколько виол {action_verb} пользователю `{target_id}`\\?{balance_line}\n\nВведите сумму:",
                InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data=f"admin_econ_pick_{action}_{page_str}")]]),
                force_reply=True
            )
            return

        elif data == "my_links":
            links = get_user_links(user.id)
            if links:
                bot_username = context.bot.username
                back_row = [InlineKeyboardButton(f"{SYM['back']} Главное меню", callback_data="main_menu")]

                text = header("Ваши анонимные ссылки", SYM['link']) + "\n\n"
                flat_buttons = []
                for link in links:
                    link_url = f"https://t.me/{bot_username}?start={link[0]}"
                    created = format_datetime(link[3])
                    is_sponsor = len(link) > 4 and link[4]
                    sponsor_badge = f"{SYM['gift']} *СПОНСОРСКАЯ*\n" if is_sponsor else ""
                    text += (
                        f"{sponsor_badge}"
                        f"{SYM['write']} *{esc(link[1])}*\n"
                        f"{quote_expandable(esc(link[2]))}\n"
                        f"`{esc_code(link_url)}`\n"
                        f"{SYM['clock']} `{esc_code(created)}`\n\n"
                    )
                    flat_buttons.append(InlineKeyboardButton(f"{SYM['gear']} {link[1][:18]}", callback_data=f"link_manage_{link[0]}"))
                keyboard_buttons = chunk_rows(flat_buttons, 2)
                keyboard_buttons.append(back_row)
                await show_screen(update, context, text, InlineKeyboardMarkup(keyboard_buttons))
            else:
                await show_screen(update, context, f"У вас пока нет созданных ссылок{esc('.')}", main_keyboard())
            return

        elif data.startswith("link_manage_"):
            link_id = data.replace("link_manage_", "")
            link_info = get_link_info(link_id)
            if link_info and (link_info[1] == user.id or is_admin):
                bot_username = context.bot.username
                link_url = f"https://t.me/{bot_username}?start={link_id}"
                text = (
                    header("Управление ссылкой", SYM['gear']) + "\n\n"
                    f"*{esc(link_info[2])}*\n"
                    f"{quote_expandable(esc(link_info[3]))}\n\n"
                    f"`{esc_code(link_url)}`"
                )
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"{SYM['write']} Название", callback_data=f"link_edit_title_{link_id}"),
                        InlineKeyboardButton(f"{SYM['write']} Описание", callback_data=f"link_edit_desc_{link_id}"),
                    ],
                    [InlineKeyboardButton(f"{SYM['trash']} Удалить", callback_data=f"confirm_delete_link_{link_id}")],
                    [InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="my_links")]
                ])
                await show_screen(update, context, text, keyboard)
            else:
                await query.answer(f"{SYM['lock']} Нет доступа к этой ссылке", show_alert=True)
            return

        elif data.startswith("link_edit_title_") or data.startswith("link_edit_desc_"):
            is_title = data.startswith("link_edit_title_")
            link_id = data.replace("link_edit_title_" if is_title else "link_edit_desc_", "")
            link_info = get_link_info(link_id)
            if link_info and (link_info[1] == user.id or is_admin):
                is_sponsor = bool(link_info[5])
                context.user_data['editing_link_field'] = link_id
                context.user_data['editing_link_field_kind'] = 'title' if is_title else 'description'
                context.user_data['editing_link_return_cb'] = (
                    f"admin_sponsor_actions_{link_id}" if is_sponsor else f"link_manage_{link_id}"
                )
                field_label = "новое *название*" if is_title else "новое *описание*"
                cancel_cb = f"admin_sponsor_actions_{link_id}" if is_sponsor else f"link_manage_{link_id}"
                await show_screen(
                    update, context,
                    f"{SYM['write']} Введите {field_label} для ссылки:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data=cancel_cb)]]),
                    force_reply=True
                )
            else:
                await query.answer(f"{SYM['lock']} Нет доступа к этой ссылке", show_alert=True)
            return

        elif data == "my_messages":
            messages = get_user_messages_with_replies(user.id)
            if messages:
                text = header("Ваши последние сообщения", SYM['inbox']) + "\n\n"
                keyboard_buttons = []
                for msg in messages:
                    msg_id, msg_text, msg_type, file_id, file_size, file_name, created, link_title, link_id, reply_count, last_reply_preview = msg
                    type_icon = TYPE_ICON.get(msg_type, SYM['doc'])
                    preview = safe_str(msg_text) or TYPE_LABEL.get(msg_type, f"[{msg_type}]")
                    if len(preview) > 50:
                        preview = preview[:50] + "..."
                    created_str = format_datetime(created)
                    # Индикатор типа последнего ответа — раньше видно было только
                    # число ответов, а голосовые/кружки без подписи ничем не
                    # выделялись в списке (нужно было открывать каждый, чтобы понять).
                    reply_hint = ""
                    if reply_count:
                        reply_preview_str = safe_str(last_reply_preview) or ""
                        is_media_reply = reply_preview_str in TYPE_LABEL.values()
                        reply_icon = SYM['voice'] if reply_preview_str == TYPE_LABEL.get('voice') else (
                            SYM['vnote'] if reply_preview_str == TYPE_LABEL.get('video_note') else SYM['reply']
                        )
                        reply_hint = f" {SYM['dot']} Ответов: {reply_count} {reply_icon}" if is_media_reply else f" {SYM['dot']} Ответов: {reply_count}"
                    text += (
                        f"{type_icon} *{esc(link_title)}*\n"
                        f"{quote(esc(preview))}\n"
                        f"{SYM['clock']} `{esc_code(created_str)}`{reply_hint}\n\n"
                    )
                    keyboard_buttons.append([
                        InlineKeyboardButton(f"{SYM['reply']} #{msg_id}", callback_data=f"reply_{msg_id}"),
                        InlineKeyboardButton(f"{SYM['trash']} #{msg_id}", callback_data=f"confirm_delete_message_{msg_id}")
                    ])
                keyboard_buttons.append([InlineKeyboardButton(f"{SYM['back']} Главное меню", callback_data="main_menu")])
                await show_screen(update, context, text, InlineKeyboardMarkup(keyboard_buttons))
            else:
                await show_screen(update, context, f"У вас пока нет сообщений{esc('.')}", main_keyboard())
            return

        elif data == "create_link":
            context.user_data['creating_link'] = True
            context.user_data.pop('link_type', None)
            context.user_data.pop('link_stage', None)
            text = (
                header("Создание ссылки", SYM['add']) + "\n\n"
                f"Выберите тип ссылки:\n\n"
                f"{SYM['link']} *Обычная* — бесплатно\n"
                f"{SYM['gift']} *Спонсорская* — {SPONSOR_LINK_PRICE} виол, свой уникальный ID"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{SYM['link']} Обычная", callback_data="link_type_normal")],
                [InlineKeyboardButton(f"{SYM['gift']} Спонсорская", callback_data="link_type_sponsor")],
                [InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="main_menu")],
            ])
            await show_screen(update, context, text, keyboard)
            return

        elif data in ("link_type_normal", "link_type_sponsor"):
            if not context.user_data.get('creating_link'):
                await query.answer("Сессия создания ссылки истекла, начните заново", show_alert=True)
                return
            link_type = 'sponsor' if data == "link_type_sponsor" else 'normal'
            context.user_data['link_type'] = link_type
            context.user_data['link_stage'] = 'title'
            if link_type == 'sponsor':
                prompt = f"{SYM['gift']} *Спонсорская ссылка*\n\n{SYM['write']} Введите *название*:"
            else:
                prompt = f"{SYM['write']} Введите *название* для вашей ссылки:"
            await show_screen(update, context, prompt, cancel_keyboard(), force_reply=True)
            return

        elif data.startswith("reply_"):
            message_id_str = data.replace("reply_", "")
            if message_id_str and message_id_str != "None":
                message_id = safe_int(message_id_str)
                context.user_data['replying_to'] = message_id
                await show_screen(
                    update, context,
                    header("Режим ответа", SYM['reply']) + "\n\nВведите ваш ответ на это сообщение:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="my_messages")]]),
                    force_reply=True
                )
            else:
                await query.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        elif data == "user_reply_admin":
            # Ответ именно на ПРЯМОЕ сообщение админа ("Написать"), не на оповещение —
            # доступно только через эту кнопку, которая не прикрепляется к рассылкам.
            context.user_data['replying_to_admin'] = True
            await show_screen(
                update, context,
                header("Ответ администратору", SYM['reply']) + "\n\nВведите текст ответа:",
                InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="main_menu")]]),
                force_reply=True
            )
            return

        elif data.startswith("confirm_delete_link_"):
            link_id = data.replace("confirm_delete_link_", "")
            link_info = get_link_info(link_id) if link_id and link_id != "None" else None
            if link_info:
                text = (
                    header("Подтверждение удаления ссылки", SYM['trash']) + "\n\n"
                    f"*Название:* {esc(link_info[2])}\n"
                    f"*Описание:*\n{quote(esc(link_info[3]))}\n\n"
                    f"{SYM['warn']} Все сообщения через эту ссылку тоже будут удалены{esc('!')}"
                )
                await show_screen(update, context, text, delete_confirmation_keyboard("link", link_id))
            else:
                await query.answer("Ошибка: ссылка не найдена", show_alert=True)
            return

        elif data.startswith("confirm_delete_message_"):
            message_id_str = data.replace("confirm_delete_message_", "")
            message_id = safe_int(message_id_str) if message_id_str and message_id_str != "None" else None
            message_info = get_message_info(message_id) if message_id else None
            owner_ids = get_message_owner_ids(message_id) if message_id else None
            if message_info and owner_ids:
                if owner_ids[1] != user.id and not is_admin:
                    await query.answer(f"{SYM['lock']} Это сообщение не в вашем инбоксе — удалить его можете только вы", show_alert=True)
                    return
                msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title, link_id, _to_uid, _from_uid = message_info
                preview = safe_str(msg_text) if msg_text else f"[{msg_type}]"
                text = (
                    header("Подтверждение удаления сообщения", SYM['trash']) + "\n\n"
                    f"{quote(esc(preview))}\n\n"
                    f"Удалить это сообщение{esc('?')}"
                )
                await show_screen(update, context, text, delete_confirmation_keyboard("message", message_id))
            else:
                await query.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        elif data.startswith("delete_link_"):
            link_id = data.replace("delete_link_", "")
            link_info = get_link_info(link_id) if link_id and link_id != "None" else None
            if link_info:
                if link_info[1] != user.id and not is_admin:
                    await query.answer(f"{SYM['lock']} Это не ваша ссылка", show_alert=True)
                    return
                success = delete_link_completely(link_id)
                if success:
                    await show_screen(update, context, f"{SYM['check']} Ссылка и все связанные сообщения удалены{esc('!')}", main_keyboard())
                else:
                    await show_screen(update, context, f"{SYM['warn']} Ошибка при удалении ссылки", main_keyboard())
            else:
                await query.answer("Ошибка: ссылка не найдена", show_alert=True)
            return

        elif data.startswith("delete_message_"):
            message_id_str = data.replace("delete_message_", "")
            if message_id_str and message_id_str != "None":
                message_id = safe_int(message_id_str)
                owner_ids = get_message_owner_ids(message_id)
                if not owner_ids or (owner_ids[1] != user.id and not is_admin):
                    await query.answer(f"{SYM['lock']} Нет доступа к этому сообщению", show_alert=True)
                    return
                success = delete_message_completely(message_id)
                if success:
                    await show_screen(update, context, f"{SYM['check']} Сообщение удалено{esc('!')}", main_keyboard())
                else:
                    await show_screen(update, context, f"{SYM['warn']} Ошибка при удалении сообщения", main_keyboard())
            else:
                await query.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        elif data == "cancel_delete":
            await show_screen(update, context, f"{SYM['cancel']} Удаление отменено", main_keyboard())
            return

        # ─── АДМИН ПАНЕЛЬ ───
        if data.startswith("admin_") or data.startswith("btn_") or data in ("broadcast_send",):
            if not is_admin or not context.user_data.get('admin_authenticated'):
                await show_screen(update, context, f"{SYM['lock']} *Требуется аутентификация*\n\nИспользуйте команду /admin с паролем", None)
                return

            if data == "admin_panel":
                clear_flow_state(context)
                await show_screen(update, context, header("Панель администратора", SYM['gear']), admin_keyboard(),
                                  body_buttons=admin_panel_body_buttons())
                return

            elif data == "admin_stats":
                stats = get_admin_stats()
                table = table_md(
                    [esc("Показатель"), esc("Значение")],
                    [
                        [f"{SYM['users']} " + esc("Пользователей"), stats['users']],
                        [f"{SYM['ban']} " + esc("Заблокировано"), stats['banned']],
                        [f"{SYM['link']} " + esc("Активных ссылок"), stats['links']],
                        [f"{SYM['gift']} " + esc("Спонсорских"), stats['sponsor_links']],
                        [f"{SYM['inbox']} " + esc("Сообщений"), stats['messages']],
                        [f"{SYM['reply']} " + esc("Ответов"), stats['replies']],
                        [f"{SYM['photo']} " + esc("Фото"), stats['photos']],
                        [f"{SYM['video']} " + esc("Видео"), stats['videos']],
                        [f"{SYM['doc']} " + esc("Документов"), stats['documents']],
                        [f"{SYM['voice']} " + esc("Голосовых"), stats['voice']],
                        [f"{SYM['vnote']} " + esc("Кружков"), stats['video_note']],
                        [f"{SYM['link']} " + esc("Ссылок в сообщениях"), stats['links_type']],
                    ]
                )
                text = header("Статистика бота", SYM['stats']) + "\n\n" + table
                await show_screen(update, context, text, admin_keyboard())
                return

            elif data == "admin_users":
                users = get_all_users_for_admin()
                if users:
                    text = header("Управление пользователями", SYM['users']) + "\n\n"
                    flat_buttons = []
                    for u in users[:15]:
                        username = u[1] if u[1] else (u[2] or f"ID:{u[0]}")
                        username_display = f"@{username}" if u[1] else username
                        created = format_datetime(u[3])
                        ban_status = f"{SYM['ban']} ЗАБЛОКИРОВАН" if u[4] else f"{SYM['check']} АКТИВЕН"
                        text += f"*{esc(username_display)}*\n`{u[0]}` {SYM['dot']} `{esc_code(created)}` {SYM['dot']} {ban_status}\n\n"
                        flat_buttons.append(InlineKeyboardButton(f"{SYM['id']} {username_display}"[:30], callback_data=f"admin_user_manage_{u[0]}"))
                    keyboard_buttons = chunk_rows(flat_buttons, 2)
                    keyboard_buttons.append([InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_panel")])
                    await show_screen(update, context, text, InlineKeyboardMarkup(keyboard_buttons))
                else:
                    await show_screen(update, context, f"Пользователей не найдено{esc('.')}", admin_keyboard())
                return

            elif data.startswith("admin_user_manage_"):
                user_id = safe_int(data.replace("admin_user_manage_", ""))
                user_info = run_query("SELECT username, first_name, is_banned FROM users WHERE user_id = ?", (user_id,), fetch="one")
                if user_info:
                    username, first_name, is_banned = user_info
                    user_display = f"@{username}" if username else (first_name or f"ID:{user_id}")
                    status = f"{SYM['ban']} ЗАБЛОКИРОВАН" if is_banned else f"{SYM['check']} АКТИВЕН"
                    text = (
                        header("Управление пользователем", SYM['id']) + "\n\n"
                        f"ID: `{user_id}`\n"
                        f"Имя: {esc(user_display)}\n"
                        f"Статус: {status}"
                    )
                    keyboard, disabled = user_management_keyboard(user_id, bool(is_banned))
                    await show_screen(update, context, text, keyboard, disabled_callbacks=disabled)
                else:
                    await query.answer("Пользователь не найден", show_alert=True)
                return

            elif data.startswith("admin_ban_user_"):
                user_id = safe_int(data.replace("admin_ban_user_", ""))
                context.user_data['banning_user'] = user_id
                await show_screen(
                    update, context,
                    f"{SYM['ban']} *Блокировка пользователя* `{user_id}`\n\nВведите причину блокировки:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data=f"admin_user_manage_{user_id}")]]),
                    force_reply=True
                )
                return

            elif data.startswith("admin_unban_user_"):
                user_id = safe_int(data.replace("admin_unban_user_", ""))
                success = unban_user(user_id)
                if success:
                    notice_delivered = True
                    try:
                        await send_rich_or_plain(
                            context.bot, user_id,
                            f"{SYM['check']} *Ваша блокировка снята{esc('!')}*\n\nТеперь вы снова можете использовать бота{esc('.')}",
                            'MarkdownV2'
                        )
                    except Exception as e:
                        notice_delivered = False
                        logging.error(f"Не удалось уведомить пользователя {user_id} о разбане: {e}")
                    delivery_note = "" if notice_delivered else f"\n{SYM['warn']} Уведомление не доставлено \\(бот заблокирован пользователем\\)"
                    keyboard, disabled = user_management_keyboard(user_id, False)
                    await show_screen(update, context, f"{SYM['check']} Пользователь `{user_id}` разблокирован{esc('!')}{delivery_note}", keyboard, disabled_callbacks=disabled)
                else:
                    await query.answer("Ошибка при разблокировке", show_alert=True)
                return

            elif data.startswith("admin_delete_user_"):
                user_id = safe_int(data.replace("admin_delete_user_", ""))
                text = (
                    f"{SYM['trash']} *УДАЛЕНИЕ ПОЛЬЗОВАТЕЛЯ*\n\n"
                    f"Удалить пользователя `{user_id}` полностью?\n\n"
                    f"{SYM['warn']} Это действие необратимо{esc('!')}\n"
                    f"{SYM['dot']} Все ссылки будут удалены\n"
                    f"{SYM['dot']} Все сообщения будут удалены\n"
                    f"{SYM['dot']} Все ответы будут удалены"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{SYM['check']} ДА, УДАЛИТЬ", callback_data=f"admin_confirm_delete_user_{user_id}")],
                    [InlineKeyboardButton(f"{SYM['cancel']} ОТМЕНА", callback_data=f"admin_user_manage_{user_id}")]
                ])
                await show_screen(update, context, text, keyboard)
                return

            elif data.startswith("admin_confirm_delete_user_"):
                user_id = safe_int(data.replace("admin_confirm_delete_user_", ""))
                success = delete_user(user_id)
                if success:
                    await show_screen(
                        update, context,
                        f"{SYM['check']} Пользователь `{user_id}` и все его данные удалены{esc('!')}",
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} К списку", callback_data="admin_users")]])
                    )
                else:
                    await query.answer("Ошибка при удалении пользователя", show_alert=True)
                return

            elif data.startswith("admin_message_user_"):
                user_id = safe_int(data.replace("admin_message_user_", ""))
                context.user_data['admin_messaging_user'] = user_id
                await show_screen(
                    update, context,
                    f"{SYM['write']} *Сообщение пользователю* `{user_id}`\n\nВведите текст:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data=f"admin_user_manage_{user_id}")]]),
                    force_reply=True
                )
                return

            elif data == "admin_sponsor_links":
                await show_screen(
                    update, context,
                    header("Спонсорские ссылки", SYM['gift']) + "\n\nМогут быть созданы для любого пользователя и переданы позже" + esc('.'),
                    sponsor_links_keyboard()
                )
                return

            elif data == "admin_create_sponsor_link":
                context.user_data['creating_sponsor_link'] = True
                context.user_data['sponsor_stage'] = 'title'
                await show_screen(
                    update, context,
                    header("Создание спонсорской ссылки", SYM['gift']) + "\n\nВведите *название*:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_sponsor_links")]]),
                    force_reply=True
                )
                return

            elif data == "admin_my_sponsor_links":
                sponsor_links = get_sponsor_links(user.id)
                if sponsor_links:
                    text = header("Ваши спонсорские ссылки", SYM['gift']) + "\n\n"
                    flat_buttons = []
                    bot_username = context.bot.username
                    for link in sponsor_links:
                        link_id, title, description, created, target_user_id, custom_id = link
                        link_url = f"https://t.me/{bot_username}?start={link_id}"
                        created_str = format_datetime(created)
                        custom_info = f"\nID: `{esc_code(custom_id)}`" if custom_id else ""
                        text += (
                            f"{SYM['gift']} *{esc(title)}*\n{quote_expandable(esc(description))}\n"
                            f"Владелец: `{target_user_id}`{custom_info}\n"
                            f"`{esc_code(link_url)}`\n{SYM['clock']} `{esc_code(created_str)}`\n\n"
                        )
                        flat_buttons.append(InlineKeyboardButton(f"{SYM['transfer']} {title[:20]}", callback_data=f"admin_sponsor_actions_{link_id}"))
                    keyboard_buttons = chunk_rows(flat_buttons, 2)
                    keyboard_buttons.append([InlineKeyboardButton(f"{SYM['back']} Назад", callback_data="admin_sponsor_links")])
                    await show_screen(update, context, text, InlineKeyboardMarkup(keyboard_buttons))
                else:
                    await show_screen(update, context, f"У вас пока нет спонсорских ссылок{esc('.')}", sponsor_links_keyboard())
                return

            elif data.startswith("admin_sponsor_actions_"):
                link_id = data.replace("admin_sponsor_actions_", "")
                link_info = get_link_info(link_id)
                if link_info:
                    text = (
                        header("Управление спонсорской ссылкой", SYM['gift']) + "\n\n"
                        f"Название: {esc(link_info[2])}\n"
                        f"Описание: {esc(link_info[3])}\n"
                        f"ID: `{link_id}`"
                    )
                    await show_screen(update, context, text, sponsor_link_actions_keyboard(link_id))
                else:
                    await query.answer("Ссылка не найдена", show_alert=True)
                return

            elif data.startswith("admin_transfer_sponsor_"):
                link_id = data.replace("admin_transfer_sponsor_", "")
                context.user_data['transferring_sponsor_link'] = link_id
                await show_screen(
                    update, context,
                    header("Передача спонсорской ссылки", SYM['transfer']) + "\n\nВведите *ID пользователя*:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data=f"admin_sponsor_actions_{link_id}")]]),
                    force_reply=True
                )
                return

            elif data.startswith("admin_delete_sponsor_"):
                link_id = data.replace("admin_delete_sponsor_", "")
                success = delete_link_completely(link_id)
                if success:
                    await show_screen(update, context, f"{SYM['check']} Спонсорская ссылка удалена{esc('!')}", sponsor_links_keyboard())
                else:
                    await query.answer("Ошибка при удалении ссылки", show_alert=True)
                return

            elif data == "admin_html_report":
                await show_screen(update, context, f"{SYM['report']} *Генерация HTML отчёта{esc('...')}*", None)

                html_content = generate_beautiful_html_report()
                report_path = "/tmp/admin_report.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)

                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"admin_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption=f"{SYM['report']} HTML отчёт администратора"
                    )

                await show_screen(update, context, f"{SYM['check']} HTML отчёт отправлен{esc('!')}", admin_keyboard())
                return

            elif data == "admin_broadcast":
                is_fresh_start = not context.user_data.get('broadcasting')
                context.user_data['broadcasting'] = True
                context.user_data['broadcast_message'] = ""
                context.user_data['broadcast_message_html'] = ""
                if is_fresh_start:
                    context.user_data['btn_list'] = []
                context.user_data['btn_target'] = 'broadcast'
                await show_screen(
                    update, context,
                    header("Режим рассылки", SYM['broadcast']) + "\n\nВведите сообщение для рассылки всем пользователям:",
                    broadcast_formatting_keyboard(),
                    force_reply=True
                )
                return

            elif data == "broadcast_send":
                if context.user_data.get('broadcasting'):
                    message_text = context.user_data.get('broadcast_message', '')
                    message_html = context.user_data.get('broadcast_message_html') or esc_html(message_text)
                    if not message_text or not message_text.strip():
                        await query.answer("Сообщение не может быть пустым!", show_alert=True)
                        return

                    link_buttons = context.user_data.get('btn_list', [])
                    for k in ('broadcasting', 'broadcast_message', 'broadcast_message_html', 'btn_list', 'btn_target'):
                        context.user_data.pop(k, None)

                    users = get_all_users_for_admin()
                    success_count = 0
                    failed_count = 0

                    await show_screen(update, context, f"{SYM['broadcast']} *Отправка рассылки{esc('...')}*", None)

                    broadcast_keyboard = buttons_to_keyboard(link_buttons)
                    for u in users:
                        try:
                            await send_rich_or_plain(
                                context.bot, u[0],
                                f"{SYM['broadcast']} <b>Оповещение от администратора</b>\n\n{message_html.strip()}",
                                'HTML',
                                broadcast_keyboard
                            )
                            success_count += 1
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logging.error(f"Ошибка отправки пользователю {u[0]}: {e}")
                            failed_count += 1

                    await show_screen(
                        update, context,
                        header("Рассылка завершена!", SYM['check']) + "\n\n"
                        f"{SYM['dot']} Отправлено: {success_count}\n"
                        f"{SYM['dot']} Не удалось: {failed_count}",
                        admin_keyboard()
                    )
                return

            # ─── Визуальный редактор кнопок-ссылок (общий для рассылки и личных сообщений) ───
            elif data == "btn_manage_broadcast":
                context.user_data['btn_target'] = 'broadcast'
                context.user_data.setdefault('btn_list', [])
                await show_button_manager(update, context)
                return

            elif data == "btn_manage_admin_message":
                context.user_data['btn_target'] = 'admin_message'
                context.user_data.setdefault('btn_list', [])
                await show_button_manager(update, context)
                return

            elif data == "btn_add":
                if len(context.user_data.get('btn_list', [])) >= 8:
                    await query.answer("Максимум 8 кнопок", show_alert=True)
                    return
                context.user_data['btn_stage'] = 'label'
                await show_screen(
                    update, context,
                    f"{SYM['write']} Введите *текст кнопки*:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="btn_add_cancel")]]),
                    force_reply=True
                )
                return

            elif data == "btn_add_cancel":
                context.user_data['btn_stage'] = None
                context.user_data.pop('btn_pending_label', None)
                await show_button_manager(update, context)
                return

            elif data.startswith("btn_remove_"):
                idx_str = data.replace("btn_remove_", "")
                buttons = context.user_data.get('btn_list', [])
                if idx_str.isdigit() and int(idx_str) < len(buttons):
                    buttons.pop(int(idx_str))
                await show_button_manager(update, context)
                return

            elif data == "btn_back_broadcast":
                context.user_data['btn_stage'] = None
                await show_broadcast_preview(update, context)
                return

            elif data == "btn_back_admin_message":
                context.user_data['btn_stage'] = None
                await show_admin_message_preview(update, context)
                return

            elif data == "admin_message_send":
                target_user_id = context.user_data.get('admin_messaging_user')
                text_plain = context.user_data.get('admin_message_text', '')
                text_html_stored = context.user_data.get('admin_message_html') or esc_html(text_plain)
                buttons = context.user_data.get('btn_list', [])
                if not target_user_id or not text_plain.strip():
                    await query.answer("Нет текста для отправки", show_alert=True)
                    return
                keyboard = buttons_to_keyboard(
                    buttons,
                    extra_rows=[[InlineKeyboardButton(f"{SYM['reply']} Ответить", callback_data="user_reply_admin")]]
                )
                try:
                    await send_rich_or_plain(
                        context.bot, target_user_id,
                        f"{SYM['write']} <b>Сообщение от администратора</b>\n\n{quote_html(text_html_stored)}",
                        'HTML',
                        keyboard
                    )
                    save_admin_message(user.id, target_user_id, text_plain)
                    await show_screen(
                        update, context,
                        f"{SYM['check']} Сообщение отправлено пользователю `{target_user_id}`{esc('!')}",
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} Назад", callback_data=f"admin_user_manage_{target_user_id}")]])
                    )
                except Exception as e:
                    logging.error(f"Ошибка отправки сообщения пользователю {target_user_id}: {e}")
                    await show_screen(
                        update, context,
                        f"{SYM['warn']} Не удалось отправить сообщение пользователю `{target_user_id}`\n\nВозможно, он заблокировал бота{esc('.')}",
                        admin_keyboard()
                    )
                for k in ('admin_messaging_user', 'admin_message_text', 'admin_message_html', 'btn_list', 'btn_target', 'btn_stage', 'btn_pending_label'):
                    context.user_data.pop(k, None)
                return

            elif data.startswith("admin_user_links_"):
                user_id = safe_int(data.replace("admin_user_links_", ""))
                user_links = get_user_links_for_admin(user_id)
                if user_links:
                    text = f"{SYM['link']} *Ссылки пользователя* `{user_id}`\n\n"
                    flat_buttons = []
                    for link in user_links:
                        created = format_datetime(link[3])
                        is_sponsor = len(link) > 5 and link[5]
                        sponsor_badge = f"{SYM['gift']} *СПОНСОРСКАЯ*\n" if is_sponsor else ""
                        text += f"{sponsor_badge}*{esc(link[1])}*\n{quote_expandable(esc(link[2]))}\n{SYM['clock']} `{esc_code(created)}` {SYM['dot']} Сообщений: {link[4]}\n\n"
                        flat_buttons.append(InlineKeyboardButton(f"{SYM['view']} {link[1][:18]}", callback_data=f"admin_link_conv_{link[0]}"))
                    keyboard_buttons = chunk_rows(flat_buttons, 2)
                    keyboard_buttons.append([InlineKeyboardButton(f"{SYM['view']} Вся переписка пользователя", callback_data=f"admin_view_conversation_{user_id}")])
                    keyboard_buttons.append([InlineKeyboardButton(f"{SYM['back']} Назад", callback_data=f"admin_user_manage_{user_id}")])
                    keyboard = InlineKeyboardMarkup(keyboard_buttons)
                    await show_screen(update, context, text, keyboard)
                else:
                    keyboard, disabled = user_management_keyboard(user_id)
                    await show_screen(update, context, f"У пользователя нет ссылок{esc('.')}", keyboard, disabled_callbacks=disabled)
                return

            elif data.startswith("admin_view_conversation_"):
                user_id = safe_int(data.replace("admin_view_conversation_", ""))
                await show_screen(update, context, f"{SYM['report']} *Генерация отчёта переписки{esc('...')}*", None)

                html_content = generate_conversation_report(user_id)
                report_path = f"/tmp/conversation_{user_id}.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)

                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"conversation_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption=f"{SYM['view']} Переписка пользователя {user_id} (HTML)"
                    )

                # Переписка теперь видна и прямо в чате — Rich Markdown-отчётом
                # с реальными сообщениями (заголовки, цитаты, время), а не
                # только HTML-файлом.
                md_report = build_conversation_report_markdown(user_id)
                keyboard, disabled = user_management_keyboard(user_id)
                await show_screen(update, context, md_report, keyboard, disabled_callbacks=disabled)
                return

            elif data.startswith("admin_link_conv_"):
                link_id = data.replace("admin_link_conv_", "")
                link_info = get_link_info(link_id)
                if not link_info:
                    await query.answer("Ссылка не найдена", show_alert=True)
                    return
                owner_id = link_info[1]
                await show_screen(update, context, f"{SYM['report']} *Генерация переписки по ссылке{esc('...')}*", None)

                html_content = generate_link_conversation_report(link_id, link_info)
                report_path = f"/tmp/link_conversation_{link_id}.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)

                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"link_{link_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption=f"{SYM['view']} Переписка по ссылке «{link_info[2]}» (HTML)"
                    )

                md_report = build_link_conversation_report_markdown(link_id, link_info)
                await show_screen(
                    update, context, md_report,
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} Назад", callback_data=f"admin_user_links_{owner_id}")]])
                )
                return

    except Exception as e:
        logging.error(f"Ошибка в обработчике кнопок: {e}")
        try:
            await show_screen(update, context, f"{SYM['warn']} Произошла ошибка\\. Попробуйте позже\\.", main_keyboard())
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user

        if is_user_banned(user.id):
            await send_ban_notice(update)
            return

        text = update.message.text
        # Сохраняем HTML-версию текста СО ВСЕМ форматированием пользователя
        # (жирный/курсив/ссылки/спойлеры и т.д.) до того как исходное
        # сообщение будет удалено (cleanup_user_message ниже).
        text_html = user_html(update.message)
        save_user(user.id, user.username, user.first_name)
        is_admin = is_admin_user(user)

        # ─── Защита от "залипшего" состояния ───
        # Если человек ответил (свайп-reply) на КАКОЕ-ТО ДРУГОЕ сообщение,
        # не на текущий экран-меню бота (например, свайпнул reply на
        # оповещение администратора), а в user_data всё ещё висит
        # незавершённый пошаговый флоу (создание ссылки и т.д.) — этот
        # текст явно не для него. Сбрасываем флоу, чтобы ввод не улетел
        # не туда и меню не накладывались друг на друга.
        reply_to_msg = update.message.reply_to_message
        active_screen_id = context.user_data.get('screen_msg_id')
        has_active_flow = any(context.user_data.get(k) for k in FLOW_KEYS)
        if has_active_flow and reply_to_msg and active_screen_id and reply_to_msg.message_id != active_screen_id:
            clear_flow_state(context)
            has_active_flow = False

        await cleanup_user_message(update)

        # ─── Визуальный редактор кнопок: ввод текста/ссылки кнопки ───
        # Проверяется РАНЬШЕ всех остальных флоу, т.к. это самое "вложенное"
        # временное состояние (пользователь мог зайти сюда из рассылки ИЛИ
        # из личного сообщения — btn_target хранит, куда возвращаться).
        if context.user_data.get('btn_stage') == 'label':
            label = text.strip()[:64]
            if not label:
                await show_screen(
                    update, context,
                    f"{SYM['warn']} Текст кнопки не может быть пустым{esc('.')} Введите текст кнопки:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="btn_add_cancel")]]),
                    force_reply=True
                )
                return
            context.user_data['btn_pending_label'] = label
            context.user_data['btn_stage'] = 'url'
            await show_screen(
                update, context,
                f"{SYM['link']} Введите *ссылку* для кнопки «{esc(label)}»:\n\n"
                f"{SYM['dot']} Например: `https://t.me/channel`",
                InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="btn_add_cancel")]]),
                force_reply=True
            )
            return

        if context.user_data.get('btn_stage') == 'url':
            url = text.strip()
            # Как и в рассылках/ответах — если забыли схему, подставляем
            # https:// сами, чтобы Telegram не отклонил кнопку целиком
            # ошибкой Button_url_invalid.
            if not re.match(r'^(https?://|tg://)', url, re.IGNORECASE):
                url = 'https://' + url.lstrip('/')
            label = context.user_data.pop('btn_pending_label', 'Кнопка')
            buttons = context.user_data.setdefault('btn_list', [])
            if len(buttons) < 8:
                buttons.append((label, url))
            context.user_data['btn_stage'] = None
            await show_button_manager(update, context)
            return

        # ─── Ответ пользователя АДМИНУ (на прямое сообщение "Написать", не на оповещение) ───
        if context.user_data.get('replying_to_admin'):
            context.user_data.pop('replying_to_admin')
            if ADMIN_ID:
                try:
                    reply_header = f"{SYM['reply']} <b>Ответ от пользователя</b> <code>{user.id}</code>"
                    if user.username:
                        reply_header += f" (@{esc_html(user.username)})"
                    await send_rich_or_plain(
                        context.bot, ADMIN_ID,
                        f"{reply_header}\n\n{quote_html_expandable(text_html)}",
                        'HTML',
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['write']} Ответить", callback_data=f"admin_message_user_{user.id}")]])
                    )
                except Exception as e:
                    logging.error(f"Failed to forward user reply to admin: {e}")
            await show_screen(update, context, f"{SYM['check']} Ваш ответ отправлен администратору{esc('!')}", main_keyboard())
            return

        # ─── Экономика: сумма для начисления/списания (замена /give и /take) ───
        if context.user_data.get('econ_action') and context.user_data.get('econ_target_user') is not None:
            action = context.user_data.get('econ_action')
            target_id = context.user_data.get('econ_target_user')
            if not is_admin:
                clear_flow_state(context)
                return

            amount = safe_int(text.strip(), default=None)
            if amount is None or amount <= 0:
                await show_screen(
                    update, context,
                    f"{SYM['warn']} Сумма должна быть положительным целым числом\\. Введите ещё раз:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_econ_menu")]]),
                    force_reply=True
                )
                return

            context.user_data.pop('econ_action')
            context.user_data.pop('econ_target_user')

            if action == "give":
                try:
                    new_balance = await asyncio.to_thread(
                        vb.credit, target_id, amount,
                        {"reason": "admin_give", "admin_id": user.id}
                    )
                    result_text = (
                        f"{SYM['check']} Начислено *{amount}* виол пользователю `{target_id}`\\.\n"
                        f"Новый баланс: *{new_balance}*"
                    )
                    logging.info(f"Админ {user.id} начислил {amount} виол пользователю {target_id}")
                except vb.BufferError as e:
                    logging.error(f"Ошибка начисления {amount} для {target_id}: {e}")
                    result_text = f"{SYM['warn']} Не удалось начислить\\. Буфер недоступен\\."
            else:
                ok, new_balance, debit_err = await asyncio.to_thread(
                    vb.try_debit, target_id, amount,
                    {"reason": "admin_take", "admin_id": user.id}
                )
                if not ok:
                    if debit_err == "insufficient_balance":
                        result_text = (
                            f"{SYM['warn']} У пользователя `{target_id}` недостаточно средств\\.\n"
                            f"Баланс: *{new_balance}*, нужно списать: *{amount}*"
                        )
                    else:
                        result_text = f"{SYM['warn']} Не удалось списать\\. Буфер недоступен\\."
                else:
                    result_text = (
                        f"{SYM['check']} Списано *{amount}* виол у пользователя `{target_id}`\\.\n"
                        f"Новый баланс: *{new_balance}*"
                    )
                    logging.info(f"Админ {user.id} списал {amount} виол у пользователя {target_id}")

            await show_screen(update, context, result_text, admin_econ_keyboard())
            return

        # ─── Блокировка пользователя (причина бана) ───
        if context.user_data.get('banning_user'):
            target_id = context.user_data.pop('banning_user')
            success = ban_user(target_id, text)
            if success:
                notice_delivered = True
                try:
                    ban_message = f"{SYM['ban']} *Вы были заблокированы в боте*\n\n*Причина:*\n{quote(esc(text))}"
                    await send_rich_or_plain(context.bot, target_id, ban_message, 'MarkdownV2')
                except Exception as e:
                    notice_delivered = False
                    logging.error(f"Не удалось уведомить пользователя {target_id} о бане: {e}")
                delivery_note = "" if notice_delivered else f"\n{SYM['warn']} Уведомление не доставлено \\(бот заблокирован пользователем\\)"
                await show_screen(
                    update, context,
                    f"{SYM['check']} Пользователь `{target_id}` заблокирован{esc('!')}\nПричина: {esc(text)}{delivery_note}",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} Назад", callback_data=f"admin_user_manage_{target_id}")]])
                )
            else:
                await show_screen(update, context, f"{SYM['warn']} Ошибка при блокировке пользователя", admin_keyboard())
            return

        # ─── Сообщение от админа пользователю ───
        # Теперь ведёт на экран предпросмотра (текст + визуальный редактор
        # кнопок), а не отправляет мгновенно — так же, как рассылка.
        if context.user_data.get('admin_messaging_user'):
            context.user_data['admin_message_text'] = text
            context.user_data['admin_message_html'] = text_html
            context.user_data.setdefault('btn_list', [])
            context.user_data['btn_target'] = 'admin_message'
            await show_admin_message_preview(update, context)
            return

        # ─── Редактирование названия/описания существующей ссылки (обычной или спонсорской) ───
        if context.user_data.get('editing_link_field'):
            link_id = context.user_data.pop('editing_link_field')
            field = context.user_data.pop('editing_link_field_kind', 'title')
            return_cb = context.user_data.pop('editing_link_return_cb', 'my_links')
            if field == 'title':
                update_link_title(link_id, text)
            else:
                update_link_description(link_id, text)
            await show_screen(
                update, context,
                f"{SYM['check']} Ссылка обновлена{esc('!')}",
                InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} Назад", callback_data=return_cb)]])
            )
            return

        # ─── Передача спонсорской ссылки ───
        if context.user_data.get('transferring_sponsor_link'):
            link_id = context.user_data.get('transferring_sponsor_link')
            try:
                new_user_id = int(text)
            except ValueError:
                await show_screen(
                    update, context,
                    f"{SYM['warn']} Неверный формат ID{esc('.')} Введите числовой ID{esc('.')}",
                    sponsor_links_keyboard(),
                    force_reply=True
                )
                return
            context.user_data.pop('transferring_sponsor_link', None)
            success = transfer_sponsor_link(link_id, new_user_id)
            if success:
                await show_screen(
                    update, context,
                    f"{SYM['check']} Спонсорская ссылка передана пользователю `{new_user_id}`{esc('!')}",
                    sponsor_links_keyboard()
                )
            else:
                await show_screen(update, context, f"{SYM['warn']} Ошибка при передаче ссылки", sponsor_links_keyboard())
            return

        # ─── Создание спонсорской ссылки (пошагово) ───
        if context.user_data.get('creating_sponsor_link'):
            stage = context.user_data.get('sponsor_stage')

            if stage == 'title':
                context.user_data['sponsor_title'] = text
                context.user_data['sponsor_stage'] = 'description'
                await show_screen(
                    update, context,
                    f"{SYM['write']} Введите *описание* для спонсорской ссылки:",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_sponsor_links")]]),
                    force_reply=True
                )
            elif stage == 'description':
                context.user_data['sponsor_description'] = text
                context.user_data['sponsor_stage'] = 'custom_id'
                await show_screen(
                    update, context,
                    f"{SYM['id']} Введите *кастомный ID* \\(или `-` для автогенерации\\):",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_sponsor_links")]]),
                    force_reply=True
                )
            elif stage == 'custom_id':
                custom_id = text.strip()
                if custom_id in ('-', ''):
                    custom_id = None
                elif not re.match(r'^[A-Za-z0-9_-]{3,32}$', custom_id):
                    # Частая причина ошибки на этом шаге: кириллица, пробелы,
                    # эмодзи или длина не 3-32 символа — regex это отклоняет.
                    # Показываем, что именно было введено, чтобы было понятно, что не так.
                    await show_screen(
                        update, context,
                        f"{SYM['warn']} ID `{esc_code(custom_id[:40])}` не подходит{esc('.')}\n\n"
                        f"Разрешены только латинские буквы, цифры, `_` и `-`, длина 3\\-32 символа "
                        f"\\(без кириллицы, пробелов и эмодзи\\){esc('.')} Введите ID заново или `-` для автогенерации:",
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_sponsor_links")]]),
                        force_reply=True
                    )
                    return
                context.user_data['sponsor_custom_id'] = custom_id
                context.user_data['sponsor_stage'] = 'target_user'
                await show_screen(
                    update, context,
                    f"{SYM['target']} Введите *ID пользователя* \\(или 0 без привязки\\):",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="admin_sponsor_links")]]),
                    force_reply=True
                )
            elif stage == 'target_user':
                try:
                    target_user_id = int(text) if text != '0' else None
                except ValueError:
                    await show_screen(
                        update, context,
                        f"{SYM['warn']} Неверный формат ID{esc('.')} Введите число или 0{esc('.')}",
                        sponsor_links_keyboard(),
                        force_reply=True
                    )
                    return
                title = context.user_data.pop('sponsor_title')
                description = context.user_data.pop('sponsor_description')
                custom_id = context.user_data.pop('sponsor_custom_id', None)
                context.user_data.pop('creating_sponsor_link')
                context.user_data.pop('sponsor_stage')

                link_id = create_sponsor_link(user.id, title, description, target_user_id, custom_id)

                if link_id is None:
                    reason = "Кастомный ID уже занят" if custom_id else "Не удалось сохранить ссылку в базе данных"
                    await show_screen(
                        update, context,
                        f"{SYM['warn']} {reason}{esc('!')} Попробуйте снова{esc('.')}",
                        sponsor_links_keyboard()
                    )
                    return

                bot_username = context.bot.username
                link_url = f"https://t.me/{bot_username}?start={link_id}"
                custom_info = f"\nID: `{esc_code(custom_id)}`" if custom_id else ""

                await show_screen(
                    update, context,
                    header("Спонсорская ссылка создана!", SYM['check']) + "\n\n"
                    f"*{esc(title)}*\n{esc(description)}\n"
                    f"Владелец: `{target_user_id or 'не назначен'}`{custom_info}\n\n"
                    f"`{esc_code(link_url)}`",
                    sponsor_links_keyboard()
                )
            return

        # ─── Ответ на сообщение (в т.ч. на сообщения-ссылки — раньше ломалось
        # из-за MarkdownV2-экранирования спецсимволов внутри URL; теперь HTML) ───
        if context.user_data.get('replying_to'):
            message_id = context.user_data.pop('replying_to')
            message_info = get_message_info(message_id)

            if message_info:
                save_reply(message_id, user.id, text)
                msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title, link_id, to_user_id, from_user_id = message_info

                # Кнопка "Ответить" висит на этом message_id для ОБЕИХ сторон
                # диалога (и когда владелец ссылки отвечает анонимному
                # отправителю, и когда отправитель отвечает на этот ответ).
                # Поэтому получателя нельзя жёстко привязывать к колонке —
                # он определяется тем, кто СЕЙЧАС реально пишет (user.id):
                # если пишет отправитель — уходит владельцу, и наоборот.
                if user.id == from_user_id:
                    target_id = to_user_id
                else:
                    target_id = from_user_id

                notification = f"{SYM['reply']} <b>Новый ответ на ваше сообщение</b>\n\n{quote_html_expandable(text_html)}"
                try:
                    if not target_id:
                        raise ValueError("target_id is empty — сообщение без адресата")
                    await send_rich_or_plain(
                        context.bot, target_id, notification, 'HTML',
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['reply']} Ответить", callback_data=f"reply_{message_id}")]])
                    )
                except Exception as e:
                    logging.error(f"Failed to send reply notification to {target_id}: {e}")

                await show_screen(update, context, f"{SYM['check']} Ответ отправлен{esc('!')}", main_keyboard())
            else:
                await show_screen(update, context, f"{SYM['warn']} Сообщение не найдено{esc('.')}", main_keyboard())
            return

        # ─── Создание ссылки (пошагово) ───
        if context.user_data.get('creating_link'):
            stage = context.user_data.get('link_stage')
            link_type = context.user_data.get('link_type', 'normal')

            if stage == 'title':
                context.user_data['link_title'] = text
                context.user_data['link_stage'] = 'description'
                await show_screen(update, context, f"{SYM['write']} Теперь введите *описание* для ссылки:", cancel_keyboard(), force_reply=True)

            elif stage == 'description':
                title = context.user_data.get('link_title')
                context.user_data['link_description'] = text

                if link_type != 'sponsor':
                    # Обычная ссылка — тип уже выбран заранее, создаём сразу, без апсейла.
                    link_id = create_anon_link(user.id, title, text)
                    clear_flow_state(context)
                    if link_id is None:
                        await show_screen(update, context, f"{SYM['warn']} Не удалось создать ссылку\\. Попробуйте ещё раз{esc('.')}", main_keyboard())
                        return
                    bot_username = context.bot.username
                    link_url = f"https://t.me/{bot_username}?start={link_id}"
                    result_text = (
                        header("Ссылка создана!", SYM['check']) + "\n\n"
                        f"*{esc(title)}*\n{quote_expandable(esc(text))}\n\n"
                        f"`{esc_code(link_url)}`\n\nПоделитесь ей, чтобы получать сообщения{esc('!')}"
                    )
                    await show_screen(update, context, result_text, main_keyboard())
                    return

                # Спонсорская — сначала уникальный ID, потом оплата.
                context.user_data['link_stage'] = 'custom_id'
                await show_screen(
                    update, context,
                    f"{SYM['id']} Введите *уникальный ID* для ссылки\n"
                    f"Латиница, цифры, `_` и `-`, 3\\-32 символа — или `-` для автогенерации:",
                    cancel_keyboard(),
                    force_reply=True
                )

            elif stage == 'custom_id':
                custom_id = text.strip()
                if custom_id in ('-', ''):
                    custom_id = None
                elif not re.match(r'^[A-Za-z0-9_-]{3,32}$', custom_id):
                    await show_screen(
                        update, context,
                        f"{SYM['warn']} ID `{esc_code(custom_id[:40])}` не подходит{esc('.')}\n\n"
                        f"Разрешены только латинские буквы, цифры, `_` и `-`, длина 3\\-32 символа "
                        f"\\(без кириллицы, пробелов и эмодзи\\){esc('.')} Введите ID заново или `-` для автогенерации:",
                        cancel_keyboard(),
                        force_reply=True
                    )
                    return
                context.user_data['link_custom_id'] = custom_id
                context.user_data['link_stage'] = 'confirm_sponsor'

                title = context.user_data.get('link_title')
                desc = context.user_data.get('link_description')
                balance = None
                try:
                    balance = await asyncio.to_thread(vb.get_balance, user.id)
                except vb.BufferError as e:
                    logging.error(f"Ошибка получения баланса для апсейла user_id={user.id}: {e}")

                id_line = f"{SYM['id']} ID: `{esc_code(custom_id)}`\n" if custom_id else ""
                preview = (
                    header("Спонсорская ссылка готова", SYM['gift']) + "\n\n"
                    f"*{esc(title)}*\n{quote_expandable(esc(desc))}\n\n"
                    f"{id_line}"
                )
                if balance is not None and balance >= SPONSOR_LINK_PRICE:
                    preview += (
                        f"{SYM['coin']} Списание: *{SPONSOR_LINK_PRICE}* виол\\. Баланс: *{balance}* виол"
                    )
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{SYM['check']} Оплатить и создать — {SPONSOR_LINK_PRICE} виол", callback_data="link_confirm_sponsor")],
                        [InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="main_menu")],
                    ])
                else:
                    shown_balance = balance if balance is not None else 0
                    preview += (
                        f"{SYM['warn']} Недостаточно виол \\(нужно *{SPONSOR_LINK_PRICE}*, у вас *{shown_balance}*\\)\\.\n"
                        f"Создать обычную ссылку вместо спонсорской \\(без уникального ID\\)?"
                    )
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{SYM['check']} Создать обычную ссылку", callback_data="link_decline_sponsor")],
                        [InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="main_menu")],
                    ])
                await show_screen(update, context, preview, keyboard)

            elif stage == 'confirm_sponsor':
                await show_screen(
                    update, context,
                    f"{SYM['warn']} Используйте кнопки выше, чтобы завершить создание ссылки{esc('.')}",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{SYM['check']} Оплатить и создать — {SPONSOR_LINK_PRICE} виол", callback_data="link_confirm_sponsor")],
                        [InlineKeyboardButton(f"{SYM['cancel']} Отмена", callback_data="main_menu")],
                    ])
                )
            return

        # ─── Отправка анонимного сообщения (текст или голая ссылка) ───
        # HTML вместо MarkdownV2: раньше голые ссылки ("похоже на ссылку")
        # ломались из-за экранирования спецсимволов URL внутри blockquote —
        # заодно тут же сохраняется форматирование, которое ввёл отправитель.
        if context.user_data.get('current_link'):
            link_id = context.user_data.pop('current_link')
            link_info = get_link_info(link_id)
            if link_info:
                msg_type = 'link' if looks_like_link(text) else 'text'
                msg_id = save_message(link_id, user.id, link_info[1], text, msg_type)
                icon = TYPE_ICON.get(msg_type, SYM['write'])
                label = "новая ссылка" if msg_type == 'link' else "новое анонимное сообщение"
                sponsor_badge = f"{SYM['gift']} <b>через спонсорскую ссылку</b>\n" if link_info[5] else ""
                notification = f"{icon} <b>Вам {label}</b>\n{sponsor_badge}\n{quote_html_expandable(text_html)}"
                try:
                    await send_rich_or_plain(context.bot, link_info[1], notification, 'HTML', message_actions_keyboard(msg_id))
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")

                await show_screen(update, context, f"{SYM['check']} Ваше сообщение отправлено анонимно{esc('!')}", main_keyboard())
            else:
                await show_screen(update, context, f"{SYM['warn']} Ссылка больше не активна{esc('.')}", main_keyboard())
            return

        # ─── Рассылка от админа ───
        if context.user_data.get('broadcasting') and is_admin:
            # Сохраняем и голый текст, и HTML-версию с форматированием
            # (жирный/курсив/подчёркнутый/зачёркнутый/моно/код), которое
            # админ применил при наборе.
            context.user_data['broadcast_message'] = text
            context.user_data['broadcast_message_html'] = text_html
            context.user_data.setdefault('btn_list', [])
            context.user_data['btn_target'] = 'broadcast'
            await show_broadcast_preview(update, context)
            return

        await show_screen(update, context, f"Используйте кнопки для навигации{esc('.')}", main_keyboard())

    except Exception as e:
        logging.error(f"Ошибка в обработчике текста: {e}")
        try:
            await update.effective_chat.send_message(f"{SYM['warn']} Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')
        except Exception:
            pass


async def send_media_by_type(bot, chat_id, msg_type, file_id, caption="", parse_mode='HTML', reply_markup=None):
    """
    Единая точка отправки медиа любого типа — раньше один и тот же
    if/elif по всем типам (photo/video/document/voice/video_note)
    копипастился в каждом месте, где боту нужно переслать файл
    (новое сообщение по ссылке, ответ с медиа и т.д.). Теперь один helper.
    """
    if msg_type == 'photo':
        return await bot.send_photo(chat_id, file_id, caption=caption or None, parse_mode=parse_mode, reply_markup=reply_markup)
    elif msg_type == 'video':
        return await bot.send_video(chat_id, file_id, caption=caption or None, parse_mode=parse_mode, reply_markup=reply_markup)
    elif msg_type == 'document':
        return await bot.send_document(chat_id, file_id, caption=caption or None, parse_mode=parse_mode, reply_markup=reply_markup)
    elif msg_type == 'voice':
        return await bot.send_voice(chat_id, file_id, caption=caption or None, parse_mode=parse_mode, reply_markup=reply_markup)
    elif msg_type == 'video_note':
        result = await bot.send_video_note(chat_id, file_id, reply_markup=reply_markup)
        if caption:
            await bot.send_message(chat_id, caption, parse_mode=parse_mode)
        return result
    return None


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user

        if is_user_banned(user.id):
            await send_ban_notice(update)
            return

        save_user(user.id, user.username, user.first_name)
        msg = update.message
        caption = msg.caption or ""
        # Подпись со ВСЕМ форматированием, которое применил отправитель —
        # захватываем до удаления исходного сообщения.
        caption_html = user_html(msg) if caption else ""
        file_id, msg_type, file_size, file_name = None, "unknown", None, None

        await cleanup_user_message(update)

        if msg.photo:
            file_id, msg_type = msg.photo[-1].file_id, "photo"
            file_size = msg.photo[-1].file_size
        elif msg.video:
            file_id, msg_type = msg.video.file_id, "video"
            file_size = msg.video.file_size
            file_name = msg.video.file_name
        elif msg.voice:
            file_id, msg_type = msg.voice.file_id, "voice"
            file_size = msg.voice.file_size
        elif msg.document:
            file_id, msg_type = msg.document.file_id, "document"
            file_size = msg.document.file_size
            file_name = msg.document.file_name
        elif msg.video_note:
            file_id, msg_type = msg.video_note.file_id, "video_note"
            file_size = msg.video_note.file_size

        if not file_id:
            return

        # ─── Ответ медиафайлом на конкретное анонимное сообщение ───
        # Раньше handle_media вообще не смотрел на 'replying_to' — медиа,
        # отправленное в режиме ответа, требовало current_link и падало
        # с "Сначала откройте анонимную ссылку", хотя человек просто отвечал.
        if context.user_data.get('replying_to'):
            message_id = context.user_data.pop('replying_to')
            message_info = get_message_info(message_id)

            if message_info:
                log_text = (caption or TYPE_LABEL.get(msg_type, f"[{msg_type}]"))
                save_reply(message_id, user.id, log_text)
                m_text, m_type, m_file_name, created, from_user, from_name, to_user, to_name, link_title, link_id, to_user_id, from_user_id = message_info

                # Та же логика определения адресата, что и в текстовых ответах:
                # смотрим, кто СЕЙЧАС реально отвечает, а не фиксированную колонку.
                target_id = to_user_id if user.id == from_user_id else from_user_id

                header = f"{SYM['reply']} <b>Новый ответ на ваше сообщение</b>"
                media_caption = f"{header}\n\n{quote_html_expandable(caption_html)}" if caption else header
                try:
                    if not target_id:
                        raise ValueError("target_id is empty — сообщение без адресата")
                    await send_media_by_type(
                        context.bot, target_id, msg_type, file_id, media_caption, 'HTML',
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['reply']} Ответить", callback_data=f"reply_{message_id}")]])
                    )
                except Exception as e:
                    logging.error(f"Failed to send media reply to {target_id}: {e}")

                await show_screen(update, context, f"{SYM['check']} Ответ отправлен{esc('!')}", main_keyboard())
            else:
                await show_screen(update, context, f"{SYM['warn']} Сообщение не найдено{esc('.')}", main_keyboard())
            return

        # ─── Ответ медиафайлом напрямую администратору (не на оповещение) ───
        if context.user_data.get('replying_to_admin'):
            context.user_data.pop('replying_to_admin')
            if ADMIN_ID:
                try:
                    reply_header = f"{SYM['reply']} <b>Ответ от пользователя</b> <code>{user.id}</code>"
                    if user.username:
                        reply_header += f" (@{esc_html(user.username)})"
                    media_caption = f"{reply_header}\n\n{quote_html_expandable(caption_html)}" if caption else reply_header
                    await send_media_by_type(
                        context.bot, ADMIN_ID, msg_type, file_id, media_caption, 'HTML',
                        InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['write']} Ответить", callback_data=f"admin_message_user_{user.id}")]])
                    )
                except Exception as e:
                    logging.error(f"Failed to forward user media reply to admin: {e}")
            await show_screen(update, context, f"{SYM['check']} Ваш ответ отправлен администратору{esc('!')}", main_keyboard())
            return

        if not context.user_data.get('current_link'):
            await show_screen(
                update, context,
                f"{SYM['warn']} Сначала откройте анонимную ссылку, чтобы отправить медиафайл{esc('.')}",
                main_keyboard()
            )
            return

        link_id = context.user_data.pop('current_link')
        link_info = get_link_info(link_id)
        if not link_info:
            await show_screen(update, context, f"{SYM['warn']} Ссылка больше не активна{esc('.')}", main_keyboard())
            return

        msg_id = save_message(link_id, user.id, link_info[1], caption, msg_type, file_id, file_size, file_name)

        # Размер и имя файла показываем всегда, когда есть — админу/получателю
        # видно, что именно пришло, не открывая файл.
        file_info = ""
        if file_size:
            kb = (file_size or 0) / 1024
            size_str = f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
            file_info = f" ({size_str})"
        if file_name:
            file_info += f"\n{SYM['file']} <code>{esc_html(file_name)}</code>"

        icon = TYPE_ICON.get(msg_type, SYM['doc'])
        sponsor_badge = f"{SYM['gift']} <b>через спонсорскую ссылку</b>\n" if link_info[5] else ""
        user_caption = f"{icon} <b>Новый анонимный файл</b>{file_info}\n{sponsor_badge}\n{quote_html_expandable(caption_html) if caption else ''}"

        try:
            await send_media_by_type(context.bot, link_info[1], msg_type, file_id, user_caption, 'HTML', message_actions_keyboard(msg_id))
        except Exception as e:
            logging.error(f"Failed to send media to user: {e}")

        await show_screen(update, context, f"{SYM['check']} Ваше медиа отправлено анонимно{esc('!')}", main_keyboard())

    except Exception as e:
        logging.error(f"Ошибка в обработчике медиа: {e}")
        try:
            await update.effective_chat.send_message(f"{SYM['warn']} Произошла ошибка при отправке медиа{esc('.')}", parse_mode='MarkdownV2')
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  HTML ОТЧЁТЫ (тёмный AUBEIG-стиль: фиолет/циан на чёрном)
# ══════════════════════════════════════════════════════════════════

REPORT_STYLE = '''
        :root {
            --bg: #050510;
            --surface: rgba(20, 20, 35, 0.55);
            --primary: #7000FF;
            --primary-glow: #8A2BE2;
            --accent: #00F0FF;
            --text: #E8E8FF;
            --text-sec: #A0A0C0;
            --border-color: rgba(255, 255, 255, 0.08);
            --radius: 20px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        @keyframes fadeInUp { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes glowPulse { 0%, 100% { opacity: 0.85; } 50% { opacity: 1; } }
        @keyframes floatOrb { 0%, 100% { transform: translate(0, 0) scale(1); } 50% { transform: translate(30px, -40px) scale(1.08); } }
        @keyframes shimmer { 0% { background-position: -400px 0; } 100% { background-position: 400px 0; } }
        body {
            font-family: 'Nunito', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 48px 20px;
            overflow-x: auto;
            position: relative;
        }
        .bg-orb {
            position: fixed; border-radius: 50%; filter: blur(60px); z-index: 0; pointer-events: none;
            animation: floatOrb 14s ease-in-out infinite;
        }
        .bg-orb.o1 { width: 420px; height: 420px; top: -10%; left: -8%; background: rgba(112,0,255,0.22); }
        .bg-orb.o2 { width: 380px; height: 380px; top: 20%; right: -10%; background: rgba(0,240,255,0.14); animation-delay: -4s; }
        .bg-orb.o3 { width: 320px; height: 320px; bottom: -12%; left: 30%; background: rgba(138,43,226,0.16); animation-delay: -8s; }
        .container { max-width: 1400px; margin: 0 auto; min-width: 1000px; position: relative; z-index: 1; }
        .reveal { opacity: 0; transform: translateY(24px); transition: opacity 0.7s ease, transform 0.7s ease; }
        .reveal.in-view { opacity: 1; transform: translateY(0); }
        .header { text-align: center; margin-bottom: 44px; animation: fadeInUp 0.6s ease both; }
        .badge {
            display: inline-block; padding: 6px 16px; border-radius: 999px; font-size: 0.78em;
            font-weight: 800; letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 18px;
            background: linear-gradient(135deg, rgba(112,0,255,0.25), rgba(0,240,255,0.15));
            border: 1px solid rgba(255,255,255,0.12); color: var(--accent);
        }
        .title {
            font-weight: 900; font-size: 3em;
            background: linear-gradient(135deg, #7000FF 0%, #8A2BE2 45%, #00F0FF 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
            margin-bottom: 14px; letter-spacing: 1px; line-height: 1.15;
            animation: glowPulse 3.5s ease-in-out infinite;
        }
        .subtitle { font-weight: 700; font-size: 1.15em; color: #b9a8ff; opacity: 0.95; margin-bottom: 10px; }
        .meta-line { color: var(--text-sec); font-size: 0.92em; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; margin-bottom: 32px; }
        .stat-card {
            background: linear-gradient(135deg, rgba(112,0,255,0.10) 0%, rgba(0,240,255,0.05) 100%);
            backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
            padding: 26px 20px; border-radius: var(--radius); text-align: center;
            border: 1px solid var(--border-color); transition: all 0.3s ease;
            animation: fadeInUp 0.5s ease both;
            position: relative; overflow: hidden;
        }
        .stat-card::before {
            content: ''; position: absolute; inset: 0; border-radius: var(--radius);
            background: linear-gradient(135deg, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0.02) 60%, rgba(255,255,255,0.10) 100%);
            opacity: 0; transition: opacity 0.4s ease; pointer-events: none;
        }
        .stat-card:hover::before { opacity: 1; }
        .stats-grid .stat-card:nth-child(1) { animation-delay: 0.05s; }
        .stats-grid .stat-card:nth-child(2) { animation-delay: 0.1s; }
        .stats-grid .stat-card:nth-child(3) { animation-delay: 0.15s; }
        .stats-grid .stat-card:nth-child(4) { animation-delay: 0.2s; }
        .stats-grid .stat-card:nth-child(5) { animation-delay: 0.25s; }
        .stat-card:hover { transform: translateY(-5px); border-color: rgba(0,240,255,0.35); }
        .stat-card h3 {
            font-weight: 900; font-size: 2.4em; margin-bottom: 8px;
            background: linear-gradient(135deg, #00F0FF 0%, #8A2BE2 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
        }
        .stat-card p { color: var(--text-sec); font-size: 0.88em; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; }
        .section {
            background: var(--surface); backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
            padding: 28px; border-radius: var(--radius); margin-bottom: 26px;
            border: 1px solid var(--border-color); position: relative; overflow: hidden;
            animation: fadeInUp 0.6s ease both;
        }
        .section::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
            background: linear-gradient(90deg, #7000FF, #8A2BE2, #00F0FF);
        }
        .section h2 {
            font-weight: 900; font-size: 1.5em; margin-bottom: 20px; color: #ffffff; letter-spacing: 0.5px;
            display: flex; align-items: center; gap: 10px;
        }
        .section h2::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 10px var(--accent); }
        .section h2 .count { color: var(--text-sec); font-weight: 700; font-size: 0.6em; margin-left: auto; }
        table { width: 100%; border-collapse: collapse; background: rgba(255,255,255,0.02); border-radius: 14px; overflow: hidden; margin-top: 10px; }
        th, td { padding: 13px 16px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); }
        th {
            background: linear-gradient(135deg, rgba(112,0,255,0.2) 0%, rgba(0,240,255,0.1) 100%);
            color: var(--accent); font-weight: 800; text-transform: uppercase; letter-spacing: 1px; font-size: 0.78em;
        }
        td { color: #dcdcf5; font-weight: 600; font-size: 0.93em; }
        tr:last-child td { border-bottom: none; }
        tr { transition: background 0.2s ease; }
        tr:hover { background: rgba(255,255,255,0.045); }
        .user-banned { color: #ff5c7a; font-weight: 800; }
        .user-active { color: #00F0FF; font-weight: 800; }
        .sponsor-tag { color: #FFD24A; font-weight: 800; background: rgba(255,210,74,0.12); padding: 3px 10px; border-radius: 8px; font-size: 0.85em; }
        .msg-thread { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
        .message-block {
            background: rgba(255,255,255,0.045); padding: 16px 18px; border-radius: 14px;
            border-left: 3px solid #8A2BE2; animation: fadeInUp 0.4s ease both; transition: background 0.2s ease;
            max-width: 88%;
        }
        .message-block:hover { background: rgba(255,255,255,0.075); }
        .message-block.reply { border-left-color: #00F0FF; margin-left: auto; }
        .message-block.admin { border-left-color: #FFD24A; background: rgba(255,210,74,0.06); }
        .message-header { display: flex; justify-content: space-between; gap: 14px; margin-bottom: 8px; font-weight: 700; color: var(--accent); font-size: 0.85em; }
        .message-block.admin .message-header { color: #FFD24A; }
        .message-header .role-tag { text-transform: uppercase; letter-spacing: 0.5px; font-size: 0.9em; }
        .message-content { color: #e5e5fa; line-height: 1.55; word-break: break-word; white-space: pre-wrap; }
        .timestamp { color: var(--text-sec); font-size: 0.85em; font-weight: 600; white-space: nowrap; }
        .media-info {
            background: rgba(112,0,255,0.1); padding: 7px 12px; border-radius: 8px; margin-top: 10px;
            font-size: 0.82em; border-left: 2px solid #8A2BE2; display: flex; gap: 8px; align-items: center;
            color: #cfc6ff; font-weight: 600;
        }
        .empty-state { color: var(--text-sec); text-align: center; padding: 30px; font-weight: 600; }
        .footer {
            text-align: center; margin-top: 36px; padding: 24px; background: rgba(112,0,255,0.08);
            border-radius: var(--radius); border: 1px solid var(--border-color); animation: fadeInUp 0.6s ease both;
        }
        .footer-text { font-weight: 800; font-size: 0.95em; color: var(--accent); letter-spacing: 2px; text-transform: uppercase; }
        code { background: rgba(255,255,255,0.08); padding: 2px 6px; border-radius: 6px; font-family: monospace; }
'''

REPORT_HEAD = '''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&display=swap" rel="stylesheet">
    <style>{style}</style>
</head>
<body>
    <div class="bg-orb o1"></div>
    <div class="bg-orb o2"></div>
    <div class="bg-orb o3"></div>
    <div class="container">'''

REPORT_SCRIPT = '''
    <script>
        // Плавное появление секций/карточек при скролле (как на лендинге).
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.section, .stat-card').forEach(el => el.classList.add('reveal'));
            const io = new IntersectionObserver((entries) => {
                entries.forEach(entry => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('in-view');
                        io.unobserve(entry.target);
                    }
                });
            }, { threshold: 0.08 });
            document.querySelectorAll('.reveal').forEach(el => io.observe(el));
        });
    </script>
'''


def generate_conversation_report(user_id):
    """
    Генерирует HTML-отчёт ПОЛНОЙ переписки пользователя: анонимные
    сообщения, ответы на них, И (это раньше отсутствовало) прямые
    сообщения администратора этому пользователю — все три источника
    сведены в единую ленту по времени, а не только "мои ответы".
    """
    conversations = get_conversation_for_user(user_id)
    admin_msgs = get_admin_messages_for_user(user_id)

    # Единая лента событий: (datetime_key, html_block)
    events = []

    for conv in conversations:
        if conv[19] == 'message':
            media_info = ""
            if conv[2] and conv[2] != 'text':
                size_str = f" · {human_file_size(conv[4])}" if conv[4] else ""
                media_info = f'<div class="media-info">▢ {conv[2].upper()}{size_str} · {escape_html_safe(conv[5]) or "без названия"}</div>'
            sender = escape_html_safe(conv[7] or conv[8] or 'Аноним')
            content = escape_html_safe(conv[1]) if conv[1] else f'[{conv[2]}]'
            block = f'''
                <div class="message-block">
                    <div class="message-header">
                        <span class="role-tag">От: {sender}</span>
                        <span class="timestamp">{format_datetime(conv[6])}</span>
                    </div>
                    <div class="message-content">{content}{media_info}</div>
                </div>'''
            events.append((str(conv[6]), block))
        else:
            sender = escape_html_safe(conv[17] or conv[18] or 'Аноним')
            block = f'''
                <div class="message-block reply">
                    <div class="message-header">
                        <span class="role-tag">Ответ от: {sender}</span>
                        <span class="timestamp">{format_datetime(conv[6])}</span>
                    </div>
                    <div class="message-content">{escape_html_safe(conv[15])}</div>
                </div>'''
            events.append((str(conv[6]), block))

    for am in admin_msgs:
        admin_message_id, from_admin_id, message_text, created_at = am
        block = f'''
                <div class="message-block admin">
                    <div class="message-header">
                        <span class="role-tag">⚙ Администратор</span>
                        <span class="timestamp">{format_datetime(created_at)}</span>
                    </div>
                    <div class="message-content">{escape_html_safe(message_text)}</div>
                </div>'''
        events.append((str(created_at), block))

    events.sort(key=lambda e: e[0])

    html_content = REPORT_HEAD.format(title=f"Переписка пользователя {user_id}", style=REPORT_STYLE)
    html_content += f'''
        <div class="header">
            <div class="badge">Переписка пользователя</div>
            <h1 class="title">ID {user_id}</h1>
            <p class="subtitle">Полная история: сообщения, ответы и обращения администратора</p>
            <p class="meta-line">Сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} (KRA) · Событий: {len(events)}</p>
        </div>
        <div class="section">
            <h2>Хронология <span class="count">{len(events)} событий</span></h2>
            <div class="msg-thread">
    '''

    if events:
        html_content += "".join(block for _, block in events)
    else:
        html_content += '<div class="empty-state">Нет данных о переписке</div>'

    html_content += '''
            </div>
        </div>
        <div class="footer"><div class="footer-text">Анонимный бот · система управления</div></div>
    </div>
''' + REPORT_SCRIPT + '''</body>
</html>'''
    return html_content


def generate_link_conversation_report(link_id, link_info=None):
    """Генерирует HTML-отчёт переписки по КОНКРЕТНОЙ ссылке (не всей переписке пользователя)."""
    conversations = get_conversation_for_link(link_id) or []
    title = link_info[2] if link_info else link_id
    is_sponsor = bool(link_info[5]) if link_info and len(link_info) > 5 else False
    sponsor_tag = ' <span class="sponsor-tag">СПОНСОРСКАЯ</span>' if is_sponsor else ''

    html_content = REPORT_HEAD.format(title=f"Переписка по ссылке — {title}", style=REPORT_STYLE)
    html_content += f'''
        <div class="header">
            <div class="badge">Переписка по ссылке</div>
            <h1 class="title">{escape_html_safe(title)}</h1>
            <p class="subtitle">{sponsor_tag}</p>
            <p class="meta-line">ID ссылки: <code>{escape_html_safe(link_id)}</code> · Сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} (KRA)</p>
        </div>
        <div class="section">
            <h2>Хронология <span class="count">{len(conversations)} событий</span></h2>
            <div class="msg-thread">
    '''

    if conversations:
        for conv in conversations:
            if conv[0] == 'message':
                media_info = ""
                if conv[3] and conv[3] != 'text':
                    size_str = f" · {human_file_size(conv[5])}" if conv[5] else ""
                    media_info = f'<div class="media-info">▢ {conv[3].upper()}{size_str} · {escape_html_safe(conv[6]) or "без названия"}</div>'
                sender = escape_html_safe(conv[8] or conv[9] or 'Аноним')
                content = escape_html_safe(conv[2]) if conv[2] else f'[{conv[3]}]'
                html_content += f'''
                <div class="message-block">
                    <div class="message-header">
                        <span class="role-tag">От: {sender}</span>
                        <span class="timestamp">{format_datetime(conv[7])}</span>
                    </div>
                    <div class="message-content">{content}{media_info}</div>
                </div>
                '''
            else:
                sender = escape_html_safe(conv[12] or conv[13] or 'Аноним')
                html_content += f'''
                <div class="message-block reply">
                    <div class="message-header">
                        <span class="role-tag">Ответ от: {sender}</span>
                        <span class="timestamp">{format_datetime(conv[7])}</span>
                    </div>
                    <div class="message-content">{escape_html_safe(conv[11])}</div>
                </div>
                '''
    else:
        html_content += '<div class="empty-state">Нет данных о переписке</div>'

    html_content += '''
            </div>
        </div>
        <div class="footer"><div class="footer-text">Анонимный бот · система управления</div></div>
    </div>
''' + REPORT_SCRIPT + '''</body>
</html>'''
    return html_content


def generate_beautiful_html_report():
    """Генерирует HTML-отчёт админ-панели."""
    data = get_all_data_for_html()

    html_content = REPORT_HEAD.format(title="Анонимный Бот — Админ Панель", style=REPORT_STYLE)
    html_content += f'''
        <div class="header">
            <div class="badge">Панель управления</div>
            <h1 class="title">Админ Панель</h1>
            <div class="subtitle">Анонимный бот · полная статистика системы</div>
            <div class="meta-line">Отчёт сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} (KRA)</div>
        </div>

        <div class="stats-grid">
            <div class="stat-card"><h3>{data['stats']['users']}</h3><p>Пользователей</p></div>
            <div class="stat-card"><h3>{data['stats']['banned']}</h3><p>Заблокировано</p></div>
            <div class="stat-card"><h3>{data['stats']['links']}</h3><p>Активных ссылок</p></div>
            <div class="stat-card"><h3>{data['stats']['sponsor_links']}</h3><p>Спонсорских</p></div>
        </div>
        <div class="stats-grid">
            <div class="stat-card"><h3>{data['stats']['messages']}</h3><p>Сообщений</p></div>
            <div class="stat-card"><h3>{data['stats']['replies']}</h3><p>Ответов</p></div>
            <div class="stat-card"><h3>{data['stats']['photos']}</h3><p>Фотографий</p></div>
            <div class="stat-card"><h3>{data['stats']['videos']}</h3><p>Видео</p></div>
            <div class="stat-card"><h3>{data['stats']['links_type']}</h3><p>Ссылок в сообщ.</p></div>
        </div>

        <div class="section">
            <h2>Пользователи <span class="count">{len(data['users'])} всего</span></h2>
            <table>
                <thead><tr><th>ID</th><th>Информация</th><th>Регистрация</th><th>Статус</th><th>Статистика</th></tr></thead>
                <tbody>
    '''

    for user in data['users'][:30]:
        username_display = f"@{user[1]}" if user[1] else (escape_html_safe(user[2]) if user[2] else f"ID:{user[0]}")
        if user[4]:
            reason = f' <span style="color:#A0A0C0;font-weight:600;">— {escape_html_safe(user[5])}</span>' if user[5] else ''
            status = f'<span class="user-banned">ЗАБЛОКИРОВАН</span>{reason}'
        else:
            status = '<span class="user-active">АКТИВЕН</span>'
        created = user[3].split()[0] if isinstance(user[3], str) else user[3].strftime("%Y-%m-%d")
        html_content += f'''
                    <tr>
                        <td><code>{user[0]}</code></td>
                        <td>{username_display}</td>
                        <td>{created}</td>
                        <td>{status}</td>
                        <td>ссылок {user[6]} · получено {user[7]} · отправлено {user[8]}</td>
                    </tr>
        '''

    html_content += '''
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Ссылки <span class="count">''' + str(len(data['links'])) + ''' активных</span></h2>
            <table>
                <thead><tr><th>ID ссылки</th><th>Название</th><th>Владелец</th><th>Тип</th><th>Сообщения</th><th>Создана</th></tr></thead>
                <tbody>
    '''

    for link in data['links'][:35]:
        owner = f"@{link[6]}" if link[6] else (escape_html_safe(link[7]) if link[7] else f"ID:{link[8]}")
        link_type = '<span class="sponsor-tag">спонсор</span>' if link[5] else "обычная"
        created = link[3].split()[0] if isinstance(link[3], str) else link[3].strftime("%Y-%m-%d")
        html_content += f'''
                    <tr>
                        <td><code>{link[0]}</code></td>
                        <td>{escape_html_safe(link[1])}</td>
                        <td>{owner}</td>
                        <td>{link_type}</td>
                        <td>{link[9]}</td>
                        <td>{created}</td>
                    </tr>
        '''

    html_content += '''
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Последние сообщения <span class="count">показано ''' + str(min(25, len(data['recent_messages']))) + '''</span></h2>
            <table>
                <thead><tr><th>ID</th><th>Тип</th><th>Отправитель</th><th>Получатель</th><th>Ссылка</th><th>Дата</th></tr></thead>
                <tbody>
    '''

    for msg in data['recent_messages'][:25]:
        from_user = f"@{msg[6]}" if msg[6] else (escape_html_safe(msg[7]) if msg[7] else f"ID:{msg[8]}")
        to_user = f"@{msg[9]}" if msg[9] else (escape_html_safe(msg[10]) if msg[10] else f"ID:{msg[11]}")
        created = msg[5].split()[0] if isinstance(msg[5], str) else msg[5].strftime("%Y-%m-%d")
        html_content += f'''
                    <tr>
                        <td>#{msg[0]}</td>
                        <td>{escape_html_safe(msg[2])}</td>
                        <td>{from_user}</td>
                        <td>{to_user}</td>
                        <td>{escape_html_safe(msg[12])}</td>
                        <td>{created}</td>
                    </tr>
        '''

    html_content += '''
                </tbody>
            </table>
        </div>

        <div class="footer"><div class="footer-text">Анонимный бот · система управления</div></div>
    </div>
''' + REPORT_SCRIPT + '''</body>
</html>'''
    return html_content


# ══════════════════════════════════════════════════════════════════
#  ОТЧЁТЫ ПЕРЕПИСОК В RICH MARKDOWN (Bot API 10.1+) — та же лента,
#  что и в HTML-файле, но прямо в чате: нативные заголовки, цитаты-
#  сообщения (реальные сообщения, а не спойлеры) и моноширинное время.
# ══════════════════════════════════════════════════════════════════

REPORT_MAX_EVENTS = 40
REPORT_EVENT_TEXT_LIMIT = 300


def _render_report_markdown(title, subtitle, events):
    """Собирает Rich Markdown-ленту событий переписки. events — список
    кортежей (created, sender, role, content, media_info), уже
    отсортированный по времени. Текст пишется на MarkdownV2 через
    esc()/quote()/header(), конвертация в rich-markdown происходит
    автоматически в show_screen (см. markdownv2_to_rich_markdown)."""
    total = len(events)
    shown = events[-REPORT_MAX_EVENTS:] if total > REPORT_MAX_EVENTS else events
    lines = [header(title, SYM['view']), ""]
    lines.append(f"{SYM['dot']} {subtitle}")
    lines.append(f"{SYM['dot']} Событий: {total}")
    lines.append(f"{SYM['dot']} Сгенерирован: `{esc_code(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}`")
    if total > len(shown):
        lines.append(f"{SYM['warn']} Показаны последние {len(shown)} событий{esc('.')}")
    lines.append("")
    for created, sender, role, content, media_info in shown:
        lines.append(f"{role} *{esc(sender)}* {SYM['dot']} `{esc_code(format_datetime(created))}`")
        body = []
        if content:
            body.append(content[:REPORT_EVENT_TEXT_LIMIT])
        if media_info:
            body.append(media_info)
        if body:
            lines.append(quote(esc("\n".join(body))))
        lines.append("")
    return "\n".join(lines)


def build_conversation_report_markdown(user_id):
    """Rich Markdown-версия ПОЛНОЙ переписки пользователя (те же три
    источника, что и в generate_conversation_report: анонимные сообщения,
    ответы и прямые сообщения администратора) в единой хронологии."""
    conversations = get_conversation_for_user(user_id) or []
    admin_msgs = get_admin_messages_for_user(user_id) or []
    events = []

    for conv in conversations:
        if conv[19] == 'message':
            media_info = ""
            if conv[2] and conv[2] != 'text':
                size_str = f" · {human_file_size(conv[4])}" if conv[4] else ""
                media_info = f"▢ {conv[2].upper()}{size_str} · {safe_str(conv[5]) or 'без названия'}"
            sender = conv[7] or conv[8] or 'Аноним'
            content = safe_str(conv[1]) if conv[1] else f"[{conv[2]}]"
            events.append((conv[6], sender, "От:", content, media_info))
        else:
            sender = conv[17] or conv[18] or 'Аноним'
            events.append((conv[6], sender, "Ответ от:", safe_str(conv[15]), ""))

    for am in admin_msgs:
        _, _, message_text, created_at = am
        events.append((created_at, "Администратор", "⚙", safe_str(message_text), ""))

    events.sort(key=lambda e: e[0])
    return _render_report_markdown(
        f"Переписка пользователя {user_id}",
        "Полная история: сообщения, ответы и обращения администратора",
        events,
    )


def build_link_conversation_report_markdown(link_id, link_info=None):
    """Rich Markdown-версия переписки по конкретной ссылке."""
    conversations = get_conversation_for_link(link_id) or []
    title = link_info[2] if link_info else link_id
    is_sponsor = bool(link_info[5]) if link_info and len(link_info) > 5 else False
    subtitle = f"Ссылка: {title}" + (" · спонсорская" if is_sponsor else "")
    events = []

    for conv in conversations:
        if conv[0] == 'message':
            media_info = ""
            if conv[3] and conv[3] != 'text':
                size_str = f" · {human_file_size(conv[5])}" if conv[5] else ""
                media_info = f"▢ {conv[3].upper()}{size_str} · {safe_str(conv[6]) or 'без названия'}"
            sender = conv[8] or conv[9] or 'Аноним'
            content = safe_str(conv[2]) if conv[2] else f"[{conv[3]}]"
            events.append((conv[7], sender, "От:", content, media_info))
        else:
            sender = conv[12] or conv[13] or 'Аноним'
            events.append((conv[7], sender, "Ответ от:", safe_str(conv[11]), ""))

    events.sort(key=lambda e: e[0])
    return _render_report_markdown(
        f"Переписка по ссылке «{title}»",
        subtitle,
        events,
    )


# ══════════════════════════════════════════════════════════════════
#  HTTP HEALTH-CHECK (для Render Web Service / UptimeRobot)
# ══════════════════════════════════════════════════════════════════

def run_http_server():
    """
    Render (веб-сервис) ждёт открытый порт, иначе считает деплой
    неудачным / убивает контейнер. Поднимаем простой aiohttp-сервер
    в отдельном потоке со своим event loop.
    """
    try:
        from aiohttp import web
    except ImportError:
        logging.error("aiohttp не установлен — health-check сервер не запущен. Добавьте aiohttp в requirements.txt")
        return

    async def handle_health(request):
        return web.Response(text="OK")

    async def _run():
        app = web.Application()
        app.router.add_get('/', handle_health)
        app.router.add_get('/health', handle_health)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get('PORT', 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logging.info(f"Health-check сервер запущен на порту {port}")
        while True:
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())


# ══════════════════════════════════════════════════════════════════
#  ЗАПУСК БОТА
# ══════════════════════════════════════════════════════════════════

def main():
    if not all([BOT_TOKEN, ADMIN_ID]):
        logging.critical("КРИТИЧЕСКАЯ ОШИБКА: не установлены BOT_TOKEN и/или ADMIN_ID")
        return

    try:
        setup_repo()
        init_db()
    except Exception as e:
        logging.error(f"Ошибка при инициализации: {e}")

    # health-check сервер — в отдельном потоке, не мешает polling-у бота
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL | filters.VIDEO_NOTE
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))

    async def error_handler(update, context):
        logging.error(f"Exception: {context.error}", exc_info=context.error)

    application.add_error_handler(error_handler)

    logging.info("Бот запускается (long polling)...")

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            pool_timeout=20,
            read_timeout=20,
            connect_timeout=20
        )
    except Exception as e:
        logging.critical(f"Критическая ошибка при запуске бота: {e}")


if __name__ == "__main__":
    main()
