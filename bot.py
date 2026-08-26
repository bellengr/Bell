import sys
import os
import re
import time
import json
import sqlite3
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Dict, Tuple, Any

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ...")
print("=" * 60)
sys.stdout.flush()

try:
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
    from vk_api.exceptions import ApiError
    print("✅ Библиотека vk-api загружена")
    sys.stdout.flush()
except ImportError as e:
    print(f"❌ Ошибка импорта vk-api: {e}")
    sys.stdout.flush()
    raise

# ====================== НАСТРОЙКИ ======================
TOKEN = "vk1.a.s5mgEVHWOVgpTPQ2AhN4hYF15Tc6vsHIsmavsZNDFTZkvKB-mwOR-f1aUuQ27AWpc5wfZLZH42iJy74xZDafcBZJwzmupX8OUN8MnlDxZYuHLk5NrJHDwIUuFiDy6S8OTbl0trJEUg77amTmVsgZPypu-EkumFvDiQFIkMt3twuGQD2PpnckpaASfFXLw0HMxp3CbBTZsLy1DEilvoJRbA"          # Замените на ваш токен
GROUP_ID = 241064421                 # ВАШ ID ГРУППЫ
# ======================================================

print(f"✅ Токен загружен: {TOKEN[:15]}...")
print(f"✅ ID группы: {GROUP_ID}")
sys.stdout.flush()

# Константы
MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
BOT_MESSAGE_DELAY = 40
RATE_LIMIT_DELAY = 0.34  # Задержка между запросами к API (не более 3 запросов в секунду)
DB_FILE = "bot_database.db"
VIP_FILE = "vip_links.json"

# Глобальные переменные с блокировками
queue = []
queue_lock = threading.Lock()
pending_links = {}
pending_links_lock = threading.Lock()
bot_messages = {}
bot_messages_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()

pending_new_members = {}
greeting_timers = {}
last_api_call = 0
api_call_lock = threading.Lock()

# Инициализация базы данных
def init_database():
    """Инициализация базы данных SQLite"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cursor = conn.cursor()
        
        # Таблица для очереди
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
        ''')
        
        # Таблица для лайков пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                UNIQUE(user_id, link)
            )
        ''')
        
        # Таблица для VIP-ссылок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vip_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL UNIQUE,
                added_by INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            )
        ''')
        
        conn.commit()
        conn.close()
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        sys.stdout.flush()
    sys.stdout.flush()

def load_queue_from_db():
    """Загрузка очереди из базы данных"""
    global queue
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, user_id, timestamp FROM queue ORDER BY id DESC LIMIT ?', (MAX_QUEUE_SIZE,))
        rows = cursor.fetchall()
        
        with queue_lock:
            queue = []
            for row in reversed(rows):
                queue.append({
                    'link': row[0],
                    'user_id': row[1],
                    'timestamp': datetime.fromisoformat(row[2])
                })
        
        conn.close()
        print(f"📂 Загружено {len(queue)} ссылок из очереди")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки очереди: {e}")
    sys.stdout.flush()

def save_queue_to_db():
    """Сохранение очереди в базу данных"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Очищаем таблицу
        cursor.execute('DELETE FROM queue')
        
        # Вставляем текущую очередь
        with queue_lock:
            for item in queue:
                cursor.execute(
                    'INSERT INTO queue (link, user_id, timestamp) VALUES (?, ?, ?)',
                    (item['link'], item['user_id'], item['timestamp'].isoformat())
                )
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения очереди: {e}")
    sys.stdout.flush()

def rate_limit():
    """Ограничение частоты запросов к API"""
    global last_api_call
    with api_call_lock:
        current_time = time.time()
        time_since_last_call = current_time - last_api_call
        if time_since_last_call < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - time_since_last_call)
        last_api_call = time.time()

def load_vip_links():
    """Загрузка VIP-ссылок из базы данных"""
    global vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, added_by, expires_at FROM vip_links WHERE expires_at > ?', 
                      (datetime.now().isoformat(),))
        rows = cursor.fetchall()
        
        with vip_links_lock:
            vip_links = []
            for row in rows:
                vip_links.append({
                    'link': row[0],
                    'added_by': row[1],
                    'expires_at': datetime.fromisoformat(row[2])
                })
        
        conn.close()
        print(f"📂 Загружено {len(vip_links)} VIP-ссылок")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки VIP-ссылок: {e}")
        vip_links = []
    sys.stdout.flush()

def save_vip_links():
    """Сохранение VIP-ссылок в базу данных"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Очищаем таблицу
        cursor.execute('DELETE FROM vip_links')
        
        # Вставляем текущие VIP-ссылки
        with vip_links_lock:
            for item in vip_links:
                cursor.execute(
                    'INSERT INTO vip_links (link, added_by, expires_at) VALUES (?, ?, ?)',
                    (item['link'], item['added_by'], item['expires_at'].isoformat())
                )
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения VIP-ссылок: {e}")
    sys.stdout.flush()

def cleanup_vip_links():
    """Очистка просроченных VIP-ссылок"""
    global vip_links
    now = datetime.now()
    with vip_links_lock:
        old_count = len(vip_links)
        vip_links = [item for item in vip_links if item['expires_at'] > now]
        if len(vip_links) < old_count:
            print(f"🗑️ Удалено {old_count - len(vip_links)} просроченных VIP-ссылок")
            save_vip_links()
    sys.stdout.flush()

def schedule_vip_cleanup():
    """Планировщик очистки VIP-ссылок"""
    def cleanup_loop():
        while True:
            time.sleep(3600)
            cleanup_vip_links()
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    print("🔄 Запущен планировщик очистки VIP-ссылок")
    sys.stdout.flush()

def clean_queue():
    """Очистка очереди от старых ссылок"""
    global queue
    with queue_lock:
        if len(queue) > MAX_QUEUE_SIZE:
            removed = queue[:-MAX_QUEUE_SIZE]
            queue = queue[-MAX_QUEUE_SIZE:]
            removed_links = [item['link'] for item in removed]
            
            with pending_links_lock:
                for user_id in list(pending_links.keys()):
                    pending_links[user_id] = [link for link in pending_links[user_id] if link not in removed_links]
                    if not pending_links[user_id]:
                        del pending_links[user_id]
            
            save_queue_to_db()
            print(f"🧹 Очищено {len(removed)} старых ссылок. В очереди: {len(queue)}")
    sys.stdout.flush()

def extract_vk_link(text: str) -> Optional[str]:
    """Извлечение ссылки VK из текста"""
    if not text:
        return None
    
    # Более точные паттерны с границами слов
    patterns = [
        r'\b(wall)(-?\d+)_(\d+)\b',
        r'\b(clip)(-?\d+)_(\d+)\b',
        r'\b(video)(-?\d+)_(\d+)\b',
        r'\b(photo)(-?\d+)_(\d+)\b',
        r'\b(album)(-?\d+)_(\d+)\b',
        r'\b(poll)(-?\d+)_(\d+)\b',
        r'\b(topic)(-?\d+)_(\d+)\b',
        r'\b(note)(-?\d+)_(\d+)\b',
        r'\b(audio)(-?\d+)_(\d+)\b',
        r'\b(doc)(-?\d+)_(\d+)\b',
        r'\b(market)(-?\d+)_(\d+)\b',
        r'\b(app)(-?\d+)_(\d+)\b',
        r'\b(page)(-?\d+)_(\d+)\b',
        r'\b(event)(-?\d+)_(\d+)\b',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None

def get_content_type(vk_link: str) -> Tuple[str, int, int]:
    """Определение типа контента для VK API"""
    parts = vk_link.split('_')
    if len(parts) != 2:
        return 'post', 0, 0
    
    type_and_owner = parts[0]
    try:
        item_id = int(parts[1])
    except ValueError:
        return 'post', 0, 0
    
    # Определяем тип контента
    content_types = {
        'wall': 'post',
        'photo': 'photo',
        'video': 'video',
        'clip': 'video',
        'topic': 'topic',
        'album': 'album',
        'market': 'market',
        'note': 'note',
        'poll': 'poll',
        'doc': 'doc',
        'audio': 'audio',
        'app': 'app',
        'page': 'page',
        'event': 'event'
    }
    
    content_type = 'post'
    owner_part = type_and_owner
    
    for prefix, api_type in content_types.items():
        if type_and_owner.startswith(prefix):
            content_type = api_type
            owner_part = type_and_owner[len(prefix):]
            break
    
    try:
        owner_id = int(owner_part)
    except ValueError:
        owner_id = 0
    
    return content_type, owner_id, item_id

def check_like(user_id: int, vk_link: str) -> bool:
    """Проверка лайка пользователя на ссылку"""
    if not vk_link or '_' not in vk_link:
        return False
    
    content_type, owner_id, item_id = get_content_type(vk_link)
    
    if owner_id == 0 or item_id == 0:
        return False
    
    try:
        rate_limit()
        response = vk.likes.isLiked(
            user_id=user_id,
            type=content_type,
            owner_id=owner_id,
            item_id=item_id
        )
        
        if isinstance(response, dict):
            return response.get('liked', 0) == 1
        return response == 1
    except ApiError as e:
        print(f"API ошибка проверки лайка: {e}")
        return False
    except Exception as e:
        print(f"Ошибка проверки лайка: {e}")
        return False

def check_previous_likes(user_id: int) -> Tuple[bool, List[str]]:
    """Проверка лайков на последние ссылки в очереди"""
    with queue_lock:
        links_to_check = [item['link'] for item in queue[-10:]]
    
    if not links_to_check:
        return True, []
    
    missing = []
    for link in links_to_check:
        if not check_like(user_id, link):
            missing.append(link)
    
    return len(missing) == 0, missing

def check_vip_likes(user_id: int) -> Tuple[bool, List[str]]:
    """Проверка лайков на VIP-ссылки"""
    cleanup_vip_links()
    
    with vip_links_lock:
        if not vip_links:
            return True, []
        vip_links_copy = vip_links.copy()
    
    missing = []
    for vip in vip_links_copy:
        if not check_like(user_id, vip['link']):
            missing.append(vip['link'])
    
    return len(missing) == 0, missing

def can_user_post(user_id: int) -> bool:
    """Проверка, может ли пользователь публиковать"""
    with queue_lock:
        # Проверяем, есть ли пользователь в очереди
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        
        if not user_posts:
            return True
        
        # Находим последний пост пользователя
        last_post_index = user_posts[-1]
        
        # Считаем, сколько постов после него
        posts_after = len(queue) - last_post_index - 1
        
        # Пользователь может публиковать, если после его последнего поста прошло 5 чужих
        return posts_after >= 5

def get_posts_after_user(user_id: int) -> int:
    """Получение количества постов после последнего поста пользователя"""
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        
        if not user_posts:
            return 0
        
        last_post_index = user_posts[-1]
        return len(queue) - last_post_index - 1

def is_group_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом группы"""
    try:
        rate_limit()
        response = vk.groups.getMembers(
            group_id=GROUP_ID,
            filter='managers',
            count=1000  # Увеличено для больших групп
        )
        admins = response.get('items', [])
        return user_id in admins
    except Exception as e:
        print(f"Ошибка проверки админа: {e}")
        return False

def is_chat_owner(peer_id: int, user_id: int) -> bool:
    """Проверка, является ли пользователь владельцем чата"""
    if peer_id < 2000000000:  # Не чат, а личные сообщения
        return False
    
    try:
        rate_limit()
        response = vk.messages.getConversationsById(
            peer_ids=[peer_id],
            extended=1
        )
        items = response.get('items', [])
        if not items:
            return False
        
        chat = items[0]
        if 'chat_settings' in chat:
            owner_id = chat['chat_settings'].get('owner_id')
            return owner_id == user_id
        return False
    except Exception as e:
        print(f"⚠️ Ошибка проверки владельца чата: {e}")
        return False

def is_admin_or_owner(peer_id: int, user_id: int) -> bool:
    """Проверка прав администратора или владельца"""
    if is_group_admin(user_id):
        return True
    if is_chat_owner(peer_id, user_id):
        return True
    return False

def send_message(peer_id: int, text: str, save_for_deletion: bool = True) -> Optional[int]:
    """Отправка сообщения с обработкой ошибок"""
    try:
        rate_limit()
        result = vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=int(time.time() * 1000)
        )
        
        print(f"✅ Отправлено сообщение в {peer_id}")
        
        if save_for_deletion and result:
            with bot_messages_lock:
                if peer_id not in bot_messages:
                    bot_messages[peer_id] = []
                bot_messages[peer_id].append(result)
        
        return result
    except ApiError as e:
        print(f"❌ API ошибка отправки сообщения: {e}")
        if e.code == 900:  # Слишком много сообщений
            time.sleep(1)
        return None
    except Exception as e:
        print(f"❌ Ошибка отправки сообщения: {e}")
        return None
    finally:
        sys.stdout.flush()

def force_delete_message(peer_id: int, message_id: int, user_id: Optional[int] = None) -> bool:
    """Принудительное удаление сообщения"""
    if not peer_id:
        return False
    
    # Админы и владельцы защищены от удаления
    if user_id and is_admin_or_owner(peer_id, user_id):
        print(f"👑 Администратор/владелец {user_id} — сообщение НЕ удалено.")
        return False
    
    # Если ID сообщения неизвестен, ищем в истории
    if not message_id or message_id == 0:
        try:
            rate_limit()
            history = vk.messages.getHistory(
                peer_id=peer_id,
                count=20
            )
            items = history.get('items', [])
            
            for msg in items:
                if msg.get('from_id') == user_id:
                    msg_id = msg.get('id')
                    if msg_id:
                        rate_limit()
                        vk.messages.delete(
                            peer_id=peer_id,
                            message_ids=[msg_id],
                            delete_for_all=True
                        )
                        print(f"🗑️ Удалено сообщение {msg_id}")
                        return True
            return False
        except Exception as e:
            print(f"❌ Ошибка поиска в истории: {e}")
            return False
    
    try:
        rate_limit()
        vk.messages.delete(
            peer_id=peer_id,
            message_ids=[message_id],
            delete_for_all=True
        )
        print(f"🗑️ Удалено сообщение {message_id}")
        return True
    except ApiError as e:
        if e.code == 15:  # Сообщение уже удалено
            print(f"ℹ️ Сообщение {message_id} уже удалено")
            return True
        print(f"❌ API ошибка удаления: {e}")
        return False
    except Exception as e:
        print(f"❌ Ошибка удаления {message_id}: {e}")
        return False

def delete_bot_messages_with_delay(peer_id: int, delay: int = BOT_MESSAGE_DELAY):
    """Отложенное удаление сообщений бота"""
    if not peer_id:
        return
    
    with bot_messages_lock:
        if peer_id not in bot_messages or not bot_messages[peer_id]:
            return
        
        messages_to_delete = bot_messages[peer_id].copy()
        bot_messages[peer_id] = []
    
    def delete_after_delay():
        time.sleep(delay)
        
        valid_ids = [msg_id for msg_id in messages_to_delete if msg_id and msg_id != 0]
        
        if valid_ids:
            # Удаляем пачками по 10 сообщений (ограничение API)
            for i in range(0, len(valid_ids), 10):
                chunk = valid_ids[i:i+10]
                try:
                    rate_limit()
                    vk.messages.delete(
                        peer_id=peer_id,
                        message_ids=chunk,
                        delete_for_all=True
                    )
                    print(f"🗑️ Удалено {len(chunk)} сообщений бота")
                except Exception as e:
                    print(f"❌ Ошибка удаления пачки: {e}")
                time.sleep(0.5)
    
    thread = threading.Thread(target=delete_after_delay, daemon=True)
    thread.start()
    print(f"🔄 Запущен таймер удаления {len(messages_to_delete)} сообщений через {delay} секунд")
    sys.stdout.flush()

def handle_new_member(peer_id: int, user_id: int):
    """Обработка нового участника"""
    print(f"👋 Новый участник {user_id} в беседе {peer_id}")
    
    if peer_id not in pending_new_members:
        pending_new_members[peer_id] = []
    
    if user_id not in pending_new_members[peer_id]:
        pending_new_members[peer_id].append(user_id)
    
    # Отправляем приветствие с задержкой
    def delayed_greeting():
        time.sleep(3)
        greeting = "👋 Привет! Перед публикацией своей ссылки обязательно прочитай закреп в чате. Обязательно!"
        send_message(peer_id, greeting, save_for_deletion=True)
        
        if peer_id in pending_new_members:
            pending_new_members[peer_id] = [uid for uid in pending_new_members[peer_id] if uid != user_id]
    
    thread = threading.Thread(target=delayed_greeting, daemon=True)
    thread.start()
    sys.stdout.flush()

def handle_vip_commands(text: str, user_id: int, peer_id: int, message_id: int) -> bool:
    """Обработка VIP-команд"""
    global vip_links
    
    # Команда добавления VIP-ссылки
    vip_match = re.match(r'^!vip\s+(\S+)', text, re.IGNORECASE)
    if vip_match:
        raw_link = vip_match.group(1)
        vk_link = extract_vk_link(raw_link)
        
        if not vk_link:
            send_message(peer_id, "❌ Не удалось распознать ссылку.")
            return True
        
        with vip_links_lock:
            # Проверяем на дубликаты
            for vip in vip_links:
                if vip['link'] == vk_link:
                    send_message(peer_id, f"⚠️ Ссылка {vk_link} уже есть в VIP-списке.")
                    return True
            
            expires_at = datetime.now() + timedelta(hours=VIP_DURATION_HOURS)
            vip_links.append({
                'link': vk_link,
                'added_by': user_id,
                'expires_at': expires_at
            })
            save_vip_links()
        
        send_message(peer_id, f"⭐ VIP-ссылка {vk_link} добавлена!\n⏳ Действует до: {expires_at.strftime('%d.%m.%Y %H:%M')}")
        return True
    
    # Команда удаления VIP-ссылки
    if text.lower().startswith('!delvip'):
        parts = text.split()
        if len(parts) < 2:
            send_message(peer_id, "❌ Формат: !delvip wall-123_456")
            return True
        
        if not is_group_admin(user_id):
            send_message(peer_id, "❌ Только администраторы группы могут удалять VIP-ссылки.")
            return True
        
        vk_link = parts[1]
        
        with vip_links_lock:
            vip_to_remove = None
            for vip in vip_links:
                if vip['link'] == vk_link:
                    vip_to_remove = vip
                    break
            
            if not vip_to_remove:
                send_message(peer_id, f"⚠️ VIP-ссылка {vk_link} не найдена.")
                return True
            
            vip_links.remove(vip_to_remove)
            save_vip_links()
        
        send_message(peer_id, f"✅ VIP-ссылка {vk_link} удалена.")
        return True
    
    # Команда показа списка VIP-ссылок
    if text.lower() == '!vip_list':
        cleanup_vip_links()
        
        with vip_links_lock:
            if not vip_links:
                send_message(peer_id, "📭 Активных VIP-ссылок нет.")
                return True
            
            vip_text = "⭐ Активные VIP-ссылки:\n"
            for i, vip in enumerate(vip_links, 1):
                remaining = vip['expires_at'] - datetime.now()
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                vip_text += f"{i}. {vip['link']} (осталось {hours}ч {minutes}мин)\n")
        
        send_message(peer_id, vip_text)
        return True
    
    return False

def process_message(event):
    """Обработка входящего сообщения"""
    print("📩 Получено новое сообщение")
    
    try:
        message = event.object.message
        peer_id = message['peer_id']
        user_id = message['from_id']
        text = message.get('text', '').strip()
        message_id = message.get('id', 0)
    except (AttributeError, KeyError) as e:
        print(f"⚠️ Ошибка чтения сообщения: {e}")
        return
    
    print(f"   От: {user_id}")
    print(f"   Текст: {text[:50]}..." if text else "   Текст: (пусто)")
    print(f"   Беседа: {peer_id}")
    sys.stdout.flush()
    
    # Игнорируем сообщения от ботов
    if user_id < 0:
        print("   ⚠️ Сообщение от бота, игнорируем")
        return
    
    # Обработка VIP-команд
    if text.startswith('!vip') or text.lower() == '!vip_list':
        handle_vip_commands(text, user_id, peer_id, message_id)
        return
    
    # Проверяем, есть ли ссылка в сообщении
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        # Админы и владельцы могут писать без ссылок
        if is_admin_or_owner(peer_id, user_id):
            print(f"👑 Владелец/администратор {user_id} — сообщение не обработано.")
            return
        
        # Удаляем сообщение без ссылки
        force_delete_message(peer_id, message_id, user_id)
        send_message(peer_id, "🔗 Для публикации нужна ссылка на контент ВКонтакте.\n📌 Поддерживаются: посты, клипы, видео, фото, альбомы.")
        return
    
    print(f"   ✅ Найдена ссылка: {vk_link}")
    sys.stdout.flush()
    
    # Проверяем VIP-лайки
    vip_ok, vip_missing = check_vip_likes(user_id)
    if not vip_ok:
        vip_text = "\n".join([f"⭐ {link}" for link in vip_missing])
        print(f"   ❌ VIP-лайки не выполнены")
        
        if not is_admin_or_owner(peer_id, user_id):
            force_delete_message(peer_id, message_id, user_id)
        
        send_message(peer_id, f"⭐ Ты должен поставить лайки на ВСЕ VIP-ссылки:\n{vip_text}")
        return
    
    # Проверяем частоту публикаций
    if not can_user_post(user_id):
        posts_after = get_posts_after_user(user_id)
        need_to_wait = max(0, 5 - posts_after)
        
        print(f"   ⏳ Нужно ждать {need_to_wait} постов")
        
        if not is_admin_or_owner(peer_id, user_id):
            force_delete_message(peer_id, message_id, user_id)
        
        send_message(peer_id, f"⏳ Ты можешь отправить новую ссылку только после {need_to_wait} чужих постов.\n📊 Сейчас прошло {posts_after}.")
        return
    
    # Проверяем лайки на предыдущие ссылки
    all_liked, missing_links = check_previous_likes(user_id)
    if not all_liked:
        missing_text = "\n".join([f"📌 {link}" for link in missing_links])
        print(f"   ❌ Пропущены лайки на {len(missing_links)} ссылок")
        
        if not is_admin_or_owner(peer_id, user_id):
            force_delete_message(peer_id, message_id, user_id)
        
        send_message(peer_id, f"❌ Ты пропустил лайки на эти ссылки:\n{missing_text}\n\n📌 Поставь лайки и отправь ссылку заново!")
        return
    
    print(f"   ✅ Все условия выполнены! Публикуем ссылку")
    sys.stdout.flush()
    
    # Добавляем в очередь
    with queue_lock:
        queue.append({
            'link': vk_link,
            'user_id': user_id,
            'timestamp': datetime.now()
        })
        clean_queue()
        save_queue_to_db()
    
    # Удаляем старые сообщения бота
    delete_bot_messages_with_delay(peer_id, BOT_MESSAGE_DELAY)
    
    # Отправляем подтверждение
    send_message(peer_id, f"✅ Ссылка {vk_link} опубликована!\n📊 В очереди: {len(queue)} ссылок\n⏳ Ждём тебя через 5 ссылок!")

def handle_event(event):
    """Обработка событий от Long Poll"""
    if event.type == VkBotEventType.MESSAGE_NEW:
        process_message(event)
    
    elif event.type == VkBotEventType.MESSAGE_EVENT:
        try:
            payload = event.object.payload
            if payload:
                print(f"📦 Событие: {payload}")
                sys.stdout.flush()
                
                if payload.get('type') == 'chat_invite_user':
                    user_id = payload.get('user_id')
                    peer_id = event.object.peer_id
                    if user_id and peer_id:
                        handle_new_member(peer_id, user_id)
        except Exception as e:
            print(f"⚠️ Ошибка обработки события: {e}")
            sys.stdout.flush()

def main():
    """Основная функция"""
    # Инициализация
    init_database()
    load_queue_from_db()
    load_vip_links()
    schedule_vip_cleanup()
    
    # Подключение к VK API
    try:
        print("🔄 Подключение к VK API...")
        sys.stdout.flush()
        
        vk_session = vk_api.VkApi(token=TOKEN)
        global vk
        vk = vk_session.get_api()
        
        print("✅ VK API подключен")
        sys.stdout.flush()
        
        print("🔄 Подключение Long Poll...")
        sys.stdout.flush()
        
        longpoll = VkBotLongPoll(vk_session, GROUP_ID)
        
        print("✅ Long Poll подключен")
        sys.stdout.flush()
        
    except Exception as e:
        print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ К VK:")
        print(f"   {e}")
        print(traceback.format_exc())
        sys.stdout.flush()
        raise
    
    print("=" * 60)
    print("🚀 Бот запущен и слушает сообщения...")
    print(f"📌 ID группы: {GROUP_ID}")
    print(f"📌 Максимальный размер очереди: {MAX_QUEUE_SIZE} ссылок")
    print(f"⭐ Активных VIP-ссылок: {len(vip_links)}")
    print(f"⏳ Сообщения бота удаляются через {BOT_MESSAGE_DELAY} секунд")
    print("=" * 60)
    print("⏳ Ожидание сообщений...")
    print("=" * 60)
    sys.stdout.flush()
    
    try:
        for event in longpoll.listen():
            handle_event(event)
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
        save_queue_to_db()
        save_vip_links()
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА:")
        print(f"   {e}")
        print(traceback.format_exc())
        save_queue_to_db()
        save_vip_links()
        sys.stdout.flush()
        raise

if __name__ == "__main__":
    main()