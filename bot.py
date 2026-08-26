import sys
import os
import re
import time
import sqlite3  # ВАЖНО: в новой версии используется SQLite, а не JSON файл!
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple

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
TOKEN = "vk1.a.s5mgEVHWOVgpTPQ2AhN4hYF15Tc6vsHIsmavsZNDFTZkvKB-mwOR-f1aUuQ27AWpc5wfZLZH42iJy74xZDafcBZJwzmupX8OUN8MnlDxZYuHLk5NrJHDwIUuFiDy6S8OTbl0trJEUg77amTmVsgZPypu-EkumFvDiQFIkMt3twuGQD2PpnckpaASfFXLw0HMxp3CbBTZsLy1DEilvoJRbA"  # ВАШ ТОКЕН
GROUP_ID = 241064421  # ВАШ ID ГРУППЫ
# ======================================================

# Константы
MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
BOT_MESSAGE_DELAY = 40
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"  # ВАЖНО: SQLite база данных!

# Глобальные переменные
queue = []
queue_lock = threading.Lock()
bot_messages = {}
bot_messages_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()

# Кэш администраторов
admin_cache = {}
admin_cache_lock = threading.Lock()
admin_cache_time = {}

def init_database():
    """Инициализация базы данных SQLite"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
        ''')
        
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
        print("✅ База данных SQLite инициализирована")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
    sys.stdout.flush()

def rate_limit():
    """Ограничение частоты запросов к API"""
    time.sleep(RATE_LIMIT_DELAY)

def is_group_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом группы"""
    with admin_cache_lock:
        if user_id in admin_cache:
            cache_time = admin_cache_time.get(user_id, 0)
            if time.time() - cache_time < 300:
                return admin_cache[user_id]
    
    try:
        rate_limit()
        response = vk.groups.getMembers(
            group_id=GROUP_ID,
            filter='managers',
            count=1000
        )
        admins = response.get('items', [])
        is_admin = user_id in admins
        
        with admin_cache_lock:
            admin_cache[user_id] = is_admin
            admin_cache_time[user_id] = time.time()
        
        return is_admin
    except Exception as e:
        print(f"Ошибка проверки админа: {e}")
        return False

def get_chat_owner(peer_id: int) -> Optional[int]:
    """Получение ID владельца чата"""
    if peer_id < 2000000000:
        return None
    
    try:
        rate_limit()
        response = vk.messages.getConversationsById(
            peer_ids=[peer_id],
            extended=1
        )
        items = response.get('items', [])
        if not items:
            return None
        
        chat = items[0]
        if 'chat_settings' in chat:
            return chat['chat_settings'].get('owner_id')
        return None
    except Exception as e:
        print(f"⚠️ Ошибка получения владельца чата: {e}")
        return None

def is_admin_or_owner(peer_id: int, user_id: int) -> bool:
    """Проверка прав администратора или владельца"""
    # Проверяем, является ли пользователь админом группы
    if is_group_admin(user_id):
        print(f"👑 {user_id} - админ группы")
        return True
    
    # Проверяем, является ли пользователь владельцем чата
    owner_id = get_chat_owner(peer_id)
    if owner_id == user_id:
        print(f"👑 {user_id} - владелец чата")
        return True
    
    return False

def extract_vk_link(text: str) -> Optional[str]:
    """Извлечение ссылки VK из текста"""
    if not text:
        return None
    
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
    if not vk_link or '_' not in vk_link:
        return '', 0, 0
    
    parts = vk_link.split('_')
    if len(parts) != 2:
        return '', 0, 0
    
    type_and_owner = parts[0]
    try:
        item_id = int(parts[1])
    except ValueError:
        return '', 0, 0
    
    content_type = ''
    owner_id = 0
    
    if type_and_owner.startswith('wall'):
        content_type = 'post'
        owner_str = type_and_owner[4:]
    elif type_and_owner.startswith('photo'):
        content_type = 'photo'
        owner_str = type_and_owner[5:]
    elif type_and_owner.startswith('video'):
        content_type = 'video'
        owner_str = type_and_owner[5:]
    elif type_and_owner.startswith('clip'):
        content_type = 'video'
        owner_str = type_and_owner[4:]
    elif type_and_owner.startswith('audio'):
        content_type = 'audio'
        owner_str = type_and_owner[5:]
    elif type_and_owner.startswith('note'):
        content_type = 'note'
        owner_str = type_and_owner[4:]
    elif type_and_owner.startswith('market'):
        content_type = 'market'
        owner_str = type_and_owner[6:]
    elif type_and_owner.startswith('topic'):
        content_type = 'topic'
        owner_str = type_and_owner[5:]
    else:
        return '', 0, 0
    
    try:
        owner_id = int(owner_str)
    except ValueError:
        return '', 0, 0
    
    return content_type, owner_id, item_id

def check_like(user_id: int, vk_link: str) -> bool:
    """Проверка лайка пользователя на контент"""
    if not vk_link or '_' not in vk_link:
        return False
    
    # Пропускаем типы, которые не поддерживают лайки
    unsupported = ['album', 'poll', 'doc', 'app', 'page', 'event']
    for prefix in unsupported:
        if vk_link.startswith(prefix):
            return True
    
    content_type, owner_id, item_id = get_content_type(vk_link)
    
    if not content_type:
        return True
    
    if owner_id == 0 or item_id == 0:
        return True
    
    try:
        rate_limit()
        response = vk.likes.isLiked(
            user_id=user_id,
            type=content_type,
            owner_id=owner_id,
            item_id=item_id
        )
        
        if isinstance(response, dict):
            liked = response.get('liked', 0)
            copied = response.get('copied', 0)
            return liked == 1 or copied == 1
        return response == 1
    except ApiError as e:
        # Если не можем проверить - пропускаем
        return True
    except Exception as e:
        print(f"Ошибка проверки лайка: {e}")
        return True

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
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        
        if not user_posts:
            return True
        
        last_post_index = user_posts[-1]
        posts_after = len(queue) - last_post_index - 1
        return posts_after >= 5

def get_posts_after_user(user_id: int) -> int:
    """Получение количества постов после последнего поста пользователя"""
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        
        if not user_posts:
            return 0
        
        last_post_index = user_posts[-1]
        return len(queue) - last_post_index - 1

def send_message(peer_id: int, text: str) -> Optional[int]:
    """Отправка сообщения"""
    try:
        rate_limit()
        result = vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=int(time.time() * 1000)
        )
        
        print(f"✅ Отправлено сообщение в {peer_id}, ID: {result}")
        
        # Сохраняем ID для удаления
        if result:
            with bot_messages_lock:
                if peer_id not in bot_messages:
                    bot_messages[peer_id] = []
                bot_messages[peer_id].append(result)
        
        return result
    except ApiError as e:
        print(f"❌ API ошибка отправки: {e}")
        return None
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None
    finally:
        sys.stdout.flush()

def delete_message(peer_id: int, message_id: int) -> bool:
    """Удаление сообщения"""
    if not message_id or message_id == 0:
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
        if e.code == 15:
            print(f"ℹ️ Сообщение {message_id} уже удалено")
            return True
        elif e.code == 924:
            print(f"❌ Нет прав на удаление {message_id}")
            return False
        else:
            print(f"❌ API ошибка удаления {message_id}: {e}")
            return False
    except Exception as e:
        print(f"❌ Ошибка удаления {message_id}: {e}")
        return False

def delete_bot_messages(peer_id: int, delay: int = BOT_MESSAGE_DELAY):
    """Удаление сообщений бота с задержкой"""
    with bot_messages_lock:
        if peer_id not in bot_messages or not bot_messages[peer_id]:
            return
        
        messages_to_delete = bot_messages[peer_id].copy()
        bot_messages[peer_id] = []
    
    def delete_after_delay():
        time.sleep(delay)
        for msg_id in messages_to_delete:
            delete_message(peer_id, msg_id)
            time.sleep(0.5)
    
    thread = threading.Thread(target=delete_after_delay, daemon=True)
    thread.start()

def handle_vip_commands(text: str, user_id: int, peer_id: int) -> bool:
    """Обработка VIP-команд"""
    global vip_links
    
    vip_match = re.match(r'^!vip\s+(\S+)', text, re.IGNORECASE)
    if vip_match:
        raw_link = vip_match.group(1)
        vk_link = extract_vk_link(raw_link)
        
        if not vk_link:
            send_message(peer_id, "❌ Не удалось распознать ссылку.")
            return True
        
        with vip_links_lock:
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
            # Сохраняем в базу данных
            try:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT INTO vip_links (link, added_by, expires_at) VALUES (?, ?, ?)',
                    (vk_link, user_id, expires_at.isoformat())
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"⚠️ Ошибка сохранения VIP: {e}")
        
        send_message(peer_id, f"⭐ VIP-ссылка {vk_link} добавлена!\n⏳ Действует до: {expires_at.strftime('%d.%m.%Y %H:%M')}")
        delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        return True
    
    if text.lower() == '!vip_list':
        with vip_links_lock:
            if not vip_links:
                send_message(peer_id, "📭 Активных VIP-ссылок нет.")
                return True
            
            vip_text = "⭐ Активные VIP-ссылки:\n"
            now = datetime.now()
            for i, vip in enumerate(vip_links, 1):
                remaining = vip['expires_at'] - now
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                vip_text += f"{i}. {vip['link']} (осталось {hours}ч {minutes}мин)\n"
        
        send_message(peer_id, vip_text)
        delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        return True
    
    return False

def process_message(event):
    """Обработка входящего сообщения"""
    print("\n📩 Получено новое сообщение")
    
    try:
        message = event.object.message
        peer_id = message['peer_id']
        user_id = message['from_id']
        text = message.get('text', '').strip()
        
        # ВАЖНО: Получаем ID сообщения правильно!
        message_id = message.get('conversation_message_id', message.get('id', 0))
        
        print(f"📍 ID сообщения: {message_id}")
        print(f"📍 Ключи сообщения: {list(message.keys())}")
        
    except (AttributeError, KeyError) as e:
        print(f"⚠️ Ошибка чтения сообщения: {e}")
        return
    
    print(f"👤 От: {user_id}")
    print(f"💬 Текст: {text[:50]}..." if text else "💬 Текст: (пусто)")
    print(f"💬 Беседа: {peer_id}")
    sys.stdout.flush()
    
    # Игнорируем сообщения от ботов
    if user_id < 0:
        print("🤖 Сообщение от бота, игнорируем")
        return
    
    # Проверяем права пользователя
    if is_admin_or_owner(peer_id, user_id):
        print(f"👑 Пользователь {user_id} - админ/владелец, игнорируем")
        return
    
    # Обработка VIP-команд
    if text.startswith('!vip') or text.lower() == '!vip_list':
        handle_vip_commands(text, user_id, peer_id)
        return
    
    # Проверяем, есть ли ссылка
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        print(f"🗑️ Удаляем сообщение без ссылки")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        else:
            print(f"⚠️ Не можем удалить - нет ID")
        
        send_message(peer_id, "🔗 Для публикации нужна ссылка на контент ВКонтакте.\n📌 Поддерживаются: посты, клипы, видео, фото, альбомы.")
        delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        return
    
    print(f"✅ Найдена ссылка: {vk_link}")
    sys.stdout.flush()
    
    # Проверяем VIP-лайки
    vip_ok, vip_missing = check_vip_likes(user_id)
    if not vip_ok:
        vip_text = "\n".join([f"⭐ {link}" for link in vip_missing])
        print(f"❌ VIP-лайки не выполнены")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, f"⭐ Ты должен поставить лайки на ВСЕ VIP-ссылки:\n{vip_text}")
        delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        return
    
    # Проверяем частоту публикаций
    if not can_user_post(user_id):
        posts_after = get_posts_after_user(user_id)
        need_to_wait = max(0, 5 - posts_after)
        
        print(f"⏳ Нужно ждать {need_to_wait} постов")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, f"⏳ Ты можешь отправить новую ссылку только после {need_to_wait} чужих постов.\n📊 Сейчас прошло {posts_after}.")
        delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        return
    
    # Проверяем лайки на предыдущие ссылки
    all_liked, missing_links = check_previous_likes(user_id)
    if not all_liked:
        missing_text = "\n".join([f"📌 {link}" for link in missing_links])
        print(f"❌ Пропущены лайки на {len(missing_links)} ссылок")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, f"❌ Ты пропустил лайки на эти ссылки:\n{missing_text}\n\n📌 Поставь лайки и отправь ссылку заново!")
        delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        return
    
    print(f"✅ Все условия выполнены! Публикуем ссылку")
    sys.stdout.flush()
    
    # Добавляем в очередь
    with queue_lock:
        queue.append({
            'link': vk_link,
            'user_id': user_id,
            'timestamp': datetime.now()
        })
        
        if len(queue) > MAX_QUEUE_SIZE:
            queue = queue[-MAX_QUEUE_SIZE:]
        
        # Сохраняем в базу данных
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM queue')
            for item in queue:
                cursor.execute(
                    'INSERT INTO queue (link, user_id, timestamp) VALUES (?, ?, ?)',
                    (item['link'], item['user_id'], item['timestamp'].isoformat())
                )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ Ошибка сохранения очереди: {e}")
    
    # Удаляем сообщения бота
    delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
    
    # Отправляем подтверждение
    send_message(peer_id, f"✅ Ссылка {vk_link} опубликована!\n📊 В очереди: {len(queue)} ссылок\n⏳ Ждём тебя через 5 ссылок!")
    delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)

def handle_event(event):
    """Обработка событий от Long Poll"""
    if event.type == VkBotEventType.MESSAGE_NEW:
        process_message(event)

def main():
    """Основная функция"""
    # Инициализация
    init_database()
    
    # Подключение к VK API
    try:
        print("🔄 Подключение к VK API...")
        sys.stdout.flush()
        
        vk_session = vk_api.VkApi(token=TOKEN)
        global vk
        vk = vk_session.get_api()
        
        # Проверяем подключение
        group_info = vk.groups.getById(group_id=GROUP_ID)
        print(f"✅ Группа: {group_info[0]['name']}")
        
        # Проверяем доступ к сообщениям
        try:
            response = vk.messages.getConversations(count=1)
            print(f"✅ Доступ к сообщениям есть")
        except ApiError as e:
            print(f"⚠️ Проблема с доступом к сообщениям: {e}")
        
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
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА:")
        print(f"   {e}")
        print(traceback.format_exc())
        sys.stdout.flush()
        raise

if __name__ == "__main__":
    main()