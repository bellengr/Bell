import sys
import os
import re
import time
import json
import sqlite3
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
TOKEN = "vk1.a.s5mgEVHWOVgpTPQ2AhN4hYF15Tc6vsHIsmavsZNDFTZkvKB-mwOR-f1aUuQ27AWpc5wfZLZH42iJy74xZDafcBZJwzmupX8OUN8MnlDxZYuHLk5NrJHDwIUuFiDy6S8OTbl0trJEUg77amTmVsgZPypu-EkumFvDiQFIkMt3twuGQD2PpnckpaASfFXLw0HMxp3CbBTZsLy1DEilvoJRbA"  # ← ЗАМЕНИТЕ НА НОВЫЙ ТОКЕН!
GROUP_ID = 241064421
# ======================================================

# Константы
MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
BOT_MESSAGE_DELAY = 40
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"

# Глобальные переменные
queue = []
queue_lock = threading.Lock()
bot_messages = {}
bot_messages_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()

admin_cache = {}
admin_cache_lock = threading.Lock()
admin_cache_time = {}

def init_database():
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
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
    sys.stdout.flush()

def load_queue_from_db():
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
    global queue
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM queue')
        
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

def load_vip_links():
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
    sys.stdout.flush()

def save_vip_links():
    global vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vip_links')
        
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

def rate_limit():
    time.sleep(RATE_LIMIT_DELAY)

def is_group_admin(user_id: int) -> bool:
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
    if is_group_admin(user_id):
        print(f"👑 {user_id} - админ группы")
        return True
    
    owner_id = get_chat_owner(peer_id)
    if owner_id == user_id:
        print(f"👑 {user_id} - владелец чата")
        return True
    
    return False

def extract_vk_link(text: str) -> Optional[str]:
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
        r'vk\.(?:com|ru)/(wall|clip|video|photo|album|poll|topic|note|audio|doc|market|app|page|event)(-?\d+)_(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            if 'vk.' in match.group(0):
                parts = match.group(0).split('/')
                if len(parts) >= 2:
                    return parts[-1]
            return match.group(0)
    return None

def get_content_type(vk_link: str) -> Tuple[str, int, int]:
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
    if not vk_link or '_' not in vk_link:
        return False
    
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
    except ApiError:
        return True
    except Exception as e:
        print(f"Ошибка проверки лайка: {e}")
        return True

def check_previous_likes(user_id: int) -> Tuple[bool, List[str]]:
    global queue
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
    global vip_links
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
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        
        if not user_posts:
            return True
        
        last_post_index = user_posts[-1]
        posts_after = len(queue) - last_post_index - 1
        return posts_after >= 5

def get_posts_after_user(user_id: int) -> int:
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        
        if not user_posts:
            return 0
        
        last_post_index = user_posts[-1]
        return len(queue) - last_post_index - 1

def send_message(peer_id: int, text: str) -> Optional[int]:
    """Отправка сообщения с получением ID для удаления"""
    global bot_messages
    try:
        rate_limit()
        
        # Отправляем сообщение
        result = vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=int(time.time() * 1000)
        )
        
        print(f"✅ Отправлено сообщение в {peer_id}")
        print(f"   Результат отправки: {result}")
        
        # Пытаемся получить ID сообщения
        message_id = None
        
        # Если API вернул ID
        if isinstance(result, dict):
            message_id = result.get('message_id')
        elif isinstance(result, int) and result > 0:
            message_id = result
        
        # Если ID не получен, пробуем через getHistory
        if not message_id:
            try:
                rate_limit()
                history = vk.messages.getHistory(
                    peer_id=peer_id,
                    count=5
                )
                items = history.get('items', [])
                
                for msg in items:
                    if msg.get('from_id', 0) < 0:  # Сообщение от бота
                        message_id = msg.get('conversation_message_id', msg.get('id', 0))
                        if message_id:
                            print(f"   ✅ Найден ID: {message_id}")
                            break
            except Exception as e:
                print(f"   ⚠️ Не удалось получить ID из истории: {e}")
        
        # Сохраняем для удаления
        if message_id:
            with bot_messages_lock:
                if peer_id not in bot_messages:
                    bot_messages[peer_id] = []
                bot_messages[peer_id].append(message_id)
                print(f"   💾 Сохранено для удаления: {message_id}")
            
            # Запускаем таймер удаления
            delete_bot_messages(peer_id, BOT_MESSAGE_DELAY)
        
        return message_id
    except ApiError as e:
        print(f"❌ API ошибка отправки: {e}")
        return None
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None
    finally:
        sys.stdout.flush()

def delete_message(peer_id: int, message_id: int) -> bool:
    if not message_id or message_id == 0:
        return False
    
    try:
        rate_limit()
        
        if peer_id >= 2000000000:
            vk.messages.delete(
                peer_id=peer_id,
                cmids=[message_id],
                delete_for_all=True
            )
        else:
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
        else:
            print(f"❌ Ошибка удаления {message_id}: {e}")
            return False
    except Exception as e:
        print(f"❌ Ошибка удаления {message_id}: {e}")
        return False

def delete_bot_messages(peer_id: int, delay: int = BOT_MESSAGE_DELAY):
    """Удаление сообщений бота с задержкой"""
    global bot_messages
    with bot_messages_lock:
        if peer_id not in bot_messages or not bot_messages[peer_id]:
            return
        
        messages_to_delete = bot_messages[peer_id].copy()
        bot_messages[peer_id] = []
    
    if not messages_to_delete:
        return
    
    print(f"🔄 Запланировано удаление {len(messages_to_delete)} сообщений через {delay} секунд")
    
    def delete_after_delay():
        time.sleep(delay)
        for msg_id in messages_to_delete:
            delete_message(peer_id, msg_id)
            time.sleep(0.5)
    
    thread = threading.Thread(target=delete_after_delay, daemon=True)
    thread.start()

def handle_vip_commands(text: str, user_id: int, peer_id: int) -> bool:
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
            save_vip_links()
        
        send_message(peer_id, f"⭐ VIP-ссылка {vk_link} добавлена!\n⏳ Действует до: {expires_at.strftime('%d.%m.%Y %H:%M')}")
        return True
    
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
        return True
    
    return False

def process_message(event):
    """Обработка входящего сообщения"""
    global queue
    
    print("\n📩 Получено новое сообщение")
    
    try:
        message = event.object.message
        peer_id = message['peer_id']
        user_id = message['from_id']
        text = message.get('text', '').strip()
        
        message_id = message.get('conversation_message_id', message.get('id', 0))
        
    except (AttributeError, KeyError) as e:
        print(f"⚠️ Ошибка чтения сообщения: {e}")
        return
    
    print(f"👤 От: {user_id}")
    print(f"💬 Текст: {text[:100]}..." if text else "💬 Текст: (пусто)")
    print(f"📍 ID сообщения: {message_id}")
    print(f"💬 Беседа: {peer_id}")
    sys.stdout.flush()
    
    if user_id < 0:
        print("🤖 Сообщение от бота, игнорируем")
        return
    
    if is_admin_or_owner(peer_id, user_id):
        print(f"👑 Пользователь {user_id} - админ/владелец, игнорируем")
        return
    
    if text.startswith('!vip') or text.lower() == '!vip_list':
        handle_vip_commands(text, user_id, peer_id)
        return
    
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        print(f"🗑️ Удаляем сообщение без ссылки")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, "🔗 Для публикации нужна ссылка на контент ВКонтакте.\n📌 Поддерживаются: посты, клипы, видео, фото, альбомы.")
        return
    
    print(f"✅ Найдена ссылка: {vk_link}")
    sys.stdout.flush()
    
    vip_ok, vip_missing = check_vip_likes(user_id)
    if not vip_ok:
        vip_text = "\n".join([f"⭐ {link}" for link in vip_missing])
        print(f"❌ VIP-лайки не выполнены")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, f"⭐ Ты должен поставить лайки на ВСЕ VIP-ссылки:\n{vip_text}")
        return
    
    if not can_user_post(user_id):
        posts_after = get_posts_after_user(user_id)
        need_to_wait = max(0, 5 - posts_after)
        
        print(f"⏳ Нужно ждать {need_to_wait} постов")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, f"⏳ Ты можешь отправить новую ссылку только после {need_to_wait} чужих постов.\n📊 Сейчас прошло {posts_after}.")
        return
    
    all_liked, missing_links = check_previous_likes(user_id)
    if not all_liked:
        missing_text = "\n".join([f"📌 {link}" for link in missing_links])
        print(f"❌ Пропущены лайки на {len(missing_links)} ссылок")
        
        if message_id and message_id != 0:
            delete_message(peer_id, message_id)
        
        send_message(peer_id, f"❌ Ты пропустил лайки на эти ссылки:\n{missing_text}\n\n📌 Поставь лайки и отправь ссылку заново!")
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
        
        save_queue_to_db()
    
    # Отправляем подтверждение
    send_message(peer_id, f"✅ Ссылка {vk_link} опубликована!\n📊 В очереди: {len(queue)} ссылок\n⏳ Ждём тебя через 5 ссылок!")

def handle_event(event):
    if event.type == VkBotEventType.MESSAGE_NEW:
        process_message(event)

def main():
    global vk
    
    init_database()
    load_queue_from_db()
    load_vip_links()
    
    try:
        print("🔄 Подключение к VK API...")
        sys.stdout.flush()
        
        vk_session = vk_api.VkApi(token=TOKEN)
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
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА:")
        print(f"   {e}")
        print(traceback.format_exc())
        sys.stdout.flush()
        raise

if __name__ == "__main__":
    main()
