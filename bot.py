import sys
import re
import time
import sqlite3
import json
import os
import urllib.parse
import requests
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple
from http.server import HTTPServer, BaseHTTPRequestHandler

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ (Callback API + Likes Check)...")
print("=" * 60)
sys.stdout.flush()

try:
    import vk_api
    from vk_api.exceptions import ApiError
    print("✅ Библиотека vk-api загружена")
    sys.stdout.flush()
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    sys.stdout.flush()
    raise

# ====================== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======================
GROUP_TOKEN = os.getenv('GROUP_TOKEN', '')
USER_TOKEN = os.getenv('USER_TOKEN', '')
GROUP_ID = int(os.getenv('GROUP_ID', '241064421'))
CONFIRMATION_CODE = os.getenv('CONFIRMATION_CODE', 'c756e565')
PORT = int(os.getenv('PORT', '3000'))
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', '447457340')
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(',') if x.strip()]
DELETE_AFTER = 300  # 5 минут
# =============================================================================

MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"

queue = []
queue_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()
vk_group = None
vk_user = None

user_activity = {}
activity_lock = threading.Lock()

# Кэш имён пользователей
user_name_cache = {}  # {user_id: (name, timestamp)}
user_name_cache_lock = threading.Lock()
USER_NAME_CACHE_TTL = 3600  # 1 час

pending_deletions = []
deletions_lock = threading.Lock()

VK_API_VERSION = "5.131"

def make_clickable_link(vk_link: str) -> str:
    if not vk_link:
        return vk_link
    if vk_link.startswith('http'):
        return vk_link
    return f"https://vk.com/{vk_link}"

def is_owner(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_user_name(user_id: int) -> str:
    """Получает имя пользователя с кэшированием"""
    global vk_user
    if vk_user is None:
        return ""
    
    now = time.time()
    
    # Проверяем кэш
    with user_name_cache_lock:
        if user_id in user_name_cache:
            name, ts = user_name_cache[user_id]
            if now - ts < USER_NAME_CACHE_TTL:
                return name
    
    # Запрашиваем у VK API
    try:
        rate_limit()
        user_info = vk_user.users.get(user_ids=[user_id])[0]
        name = f"{user_info['first_name']} {user_info['last_name']}"
        
        with user_name_cache_lock:
            user_name_cache[user_id] = (name, now)
        
        return name
    except Exception as e:
        print(f"⚠️ Не удалось получить имя пользователя {user_id}: {e}", flush=True)
        return ""

def get_mention(user_id: int) -> str:
    """Возвращает упоминание пользователя в формате ВК"""
    name = get_user_name(user_id)
    if name:
        return f"[id{user_id}|{name}]"
    return f"[id{user_id}|пользователь]"

def init_database():
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                is_owner_post INTEGER DEFAULT 0
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id INTEGER PRIMARY KEY,
                last_post_time TEXT,
                post_count INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id INTEGER NOT NULL,
                conv_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')
        conn.commit()
        conn.close()
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
    sys.stdout.flush()

def load_data():
    global queue, vip_links, user_activity, pending_deletions
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, user_id, timestamp, is_owner_post FROM queue ORDER BY id DESC LIMIT ?', (MAX_QUEUE_SIZE,))
        rows = cursor.fetchall()
        queue = []
        for row in reversed(rows):
            queue.append({'link': row[0], 'user_id': row[1], 'timestamp': datetime.fromisoformat(row[2]), 'is_owner_post': row[3] if len(row) > 3 else 0})
        cursor.execute('SELECT link, added_by, expires_at FROM vip_links')
        vip_rows = cursor.fetchall()
        vip_links = []
        now = datetime.now()
        for row in vip_rows:
            expires_at = datetime.fromisoformat(row[2])
            if expires_at > now:
                vip_links.append({'link': row[0], 'added_by': row[1], 'expires_at': expires_at})
        
        cursor.execute('SELECT user_id, last_post_time, post_count FROM user_activity')
        for row in cursor.fetchall():
            user_activity[row[0]] = {
                'last_post_time': datetime.fromisoformat(row[1]) if row[1] else None,
                'post_count': row[2]
            }
        
        cursor.execute('SELECT peer_id, conv_message_id, created_at FROM bot_messages')
        for row in cursor.fetchall():
            pending_deletions.append({
                'peer_id': row[0],
                'conv_message_id': row[1],
                'created_at': datetime.fromisoformat(row[2])
            })
        
        conn.close()
        print(f"📂 Загружено: {len(queue)} ссылок, {len(vip_links)} VIP, {len(pending_deletions)} на удаление")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки: {e}")
    sys.stdout.flush()

def save_bot_message(peer_id: int, conv_message_id: int):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO bot_messages (peer_id, conv_message_id, created_at) VALUES (?, ?, ?)',
                      (peer_id, conv_message_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения: {e}")

def remove_bot_message(conv_message_id: int):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM bot_messages WHERE conv_message_id = ?', (conv_message_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка удаления из БД: {e}")

def save_user_activity(user_id: int):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_activity (user_id, last_post_time, post_count)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_post_time = excluded.last_post_time,
                post_count = excluded.post_count
        ''', (user_id, datetime.now().isoformat(), user_activity.get(user_id, {}).get('post_count', 0) + 1))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения активности: {e}")

def save_queue():
    global queue
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM queue')
        for item in queue:
            cursor.execute('INSERT INTO queue (link, user_id, timestamp, is_owner_post) VALUES (?, ?, ?, ?)',
                          (item['link'], item['user_id'], item['timestamp'].isoformat(), item.get('is_owner_post', 0)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения: {e}")

def save_vip_links():
    global vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vip_links')
        for item in vip_links:
            cursor.execute('INSERT INTO vip_links (link, added_by, expires_at) VALUES (?, ?, ?)',
                          (item['link'], item['added_by'], item['expires_at'].isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения VIP: {e}")

def reload_vip_links():
    """Перезагружает список VIP-ссылок из базы данных"""
    global vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, added_by, expires_at FROM vip_links')
        vip_rows = cursor.fetchall()
        vip_links = []
        now = datetime.now()
        for row in vip_rows:
            expires_at = datetime.fromisoformat(row[2])
            if expires_at > now:
                vip_links.append({'link': row[0], 'added_by': row[1], 'expires_at': expires_at})
        conn.close()
        print(f"🔄 VIP-ссылки перезагружены: {len(vip_links)}", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка перезагрузки VIP: {e}", flush=True)

def reload_queue():
    """Перезагружает очередь из базы данных"""
    global queue
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, user_id, timestamp, is_owner_post FROM queue ORDER BY id DESC LIMIT ?', (MAX_QUEUE_SIZE,))
        rows = cursor.fetchall()
        queue = []
        for row in reversed(rows):
            queue.append({'link': row[0], 'user_id': row[1], 'timestamp': datetime.fromisoformat(row[2]), 'is_owner_post': row[3] if len(row) > 3 else 0})
        conn.close()
        print(f"🔄 Очередь перезагружена: {len(queue)}", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка перезагрузки очереди: {e}", flush=True)

def cleanup_expired_vip():
    global vip_links
    with vip_links_lock:
        now = datetime.now()
        vip_links = [v for v in vip_links if v['expires_at'] > now]
        save_vip_links()

def cleanup_old_queue():
    global queue
    with queue_lock:
        if len(queue) > MAX_QUEUE_SIZE:
            queue = queue[-MAX_QUEUE_SIZE:]
            save_queue()

def rate_limit():
    time.sleep(RATE_LIMIT_DELAY)

def extract_vk_link(text: str) -> Optional[str]:
    if not text:
        return None
    patterns = [
        r'(wall-?\d+_\d+)',
        r'(photo-?\d+_\d+)',
        r'(video-?\d+_\d+)',
        r'(clip-?\d+_\d+)',
        r'(audio-?\d+_\d+)',
        r'(topic-?\d+_\d+)',
        r'(market-?\d+_\d+)',
        r'(album-?\d+_\d+)',
        r'(poll-?\d+_\d+)',
        r'(note-?\d+_\d+)',
        r'(doc-?\d+_\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

def get_content_type(vk_link: str):
    if '_' not in vk_link:
        return '', 0, 0
    parts = vk_link.split('_')
    type_and_owner = parts[0]
    try:
        item_id = int(parts[1])
    except:
        return '', 0, 0
    if type_and_owner.startswith('wall'):
        return 'post', int(type_and_owner[4:]), item_id
    elif type_and_owner.startswith('photo'):
        return 'photo', int(type_and_owner[5:]), item_id
    elif type_and_owner.startswith('video'):
        return 'video', int(type_and_owner[5:]), item_id
    elif type_and_owner.startswith('clip'):
        return 'video', int(type_and_owner[4:]), item_id
    elif type_and_owner.startswith('audio'):
        return 'audio', int(type_and_owner[5:]), item_id
    elif type_and_owner.startswith('market'):
        return 'market', int(type_and_owner[6:]), item_id
    elif type_and_owner.startswith('topic'):
        return 'topic', int(type_and_owner[5:]), item_id
    return '', 0, 0

def check_user_like(user_id: int, vk_link: str) -> bool:
    global vk_user
    if vk_user is None:
        return False
    
    content_type, owner_id, item_id = get_content_type(vk_link)
    if not content_type or owner_id == 0 or item_id == 0:
        return True
    
    try:
        rate_limit()
        response = vk_user.likes.isLiked(
            user_id=user_id,
            type=content_type,
            owner_id=owner_id,
            item_id=item_id
        )
        
        if isinstance(response, dict):
            liked = response.get('liked', 0)
            copied = response.get('copied', 0)
            result = liked == 1 or copied == 1
            print(f"   📊 Лайк {'✅ ЕСТЬ' if result else '❌ НЕТ'}")
            return result
        return response == 1
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        return False

def can_user_post(user_id: int) -> bool:
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        if not user_posts:
            return True
        return len(queue) - user_posts[-1] - 1 >= 5

def get_posts_after_user(user_id: int) -> int:
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        if not user_posts:
            return 0
        return len(queue) - user_posts[-1] - 1

def vk_api_request(method: str, params: dict) -> dict:
    """
    Выполняет прямой запрос к VK API через HTTP
    """
    url = f"https://api.vk.com/method/{method}"
    
    params['v'] = VK_API_VERSION
    params['access_token'] = GROUP_TOKEN
    
    try:
        response = requests.post(url, data=params, timeout=10)
        result = response.json()
        
        if 'error' in result:
            print(f"⚠️ Ошибка VK API: {result['error']}", flush=True)
            return {'error': result['error']}
        
        return result.get('response', {})
    except Exception as e:
        print(f"⚠️ Ошибка запроса к VK API: {e}", flush=True)
        return {'error': str(e)}

def send_message(peer_id: int, text: str) -> Optional[int]:
    """
    Отправка сообщения через прямой HTTP-запрос с peer_ids.
    """
    global pending_deletions
    
    try:
        rate_limit()
        random_id = int(time.time() * 1000)
        
        params = {
            'peer_ids': peer_id,
            'message': text,
            'random_id': random_id,
            'group_id': GROUP_ID
        }
        
        result = vk_api_request('messages.send', params)
        
        print(f"✅ Отправлено: {text[:50]}...", flush=True)
        print(f"📦 Ответ API: {result}", flush=True)
        
        conv_msg_id = None
        
        if isinstance(result, list) and len(result) > 0:
            conv_msg_id = result[0].get('conversation_message_id')
        elif isinstance(result, dict):
            conv_msg_id = result.get('conversation_message_id')
        elif isinstance(result, int) and result != 0:
            conv_msg_id = result
        
        if conv_msg_id:
            print(f"📦 Получен conversation_message_id: {conv_msg_id}", flush=True)
            
            with deletions_lock:
                pending_deletions.append({
                    'peer_id': peer_id,
                    'conv_message_id': conv_msg_id,
                    'created_at': datetime.now()
                })
                save_bot_message(peer_id, conv_msg_id)
            print(f"✅ Сообщение будет удалено через {DELETE_AFTER} секунд", flush=True)
            return conv_msg_id
        else:
            print(f"⚠️ Не удалось получить conversation_message_id", flush=True)
            return None
            
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None

def delete_message_by_conv_id(peer_id: int, conv_message_id: int) -> bool:
    """
    Удаление сообщения по conversation_message_id через прямой HTTP-запрос
    """
    try:
        rate_limit()
        
        params = {
            'peer_id': peer_id,
            'cmids': conv_message_id,
            'delete_for_all': 1,
            'group_id': GROUP_ID
        }
        
        result = vk_api_request('messages.delete', params)
        
        # Если ошибка "message can not be found" - удаляем запись из БД
        if isinstance(result, dict) and 'error' in result:
            error_msg = str(result['error'])
            if 'message can not be found' in error_msg or 'message not found' in error_msg:
                print(f"⚠️ Сообщение {conv_message_id} уже не существует, удаляем запись", flush=True)
                remove_bot_message(conv_message_id)
                return True
        
        # VK API возвращает словарь вида { "peer_id_message_id": 1 } при успехе
        if isinstance(result, dict):
            key = f"{peer_id}_{conv_message_id}"
            if key in result and result[key] == 1:
                print(f"🗑️ Удалено сообщение {conv_message_id}", flush=True)
                return True
            for k, v in result.items():
                if str(conv_message_id) in k or str(peer_id) in k:
                    if v == 1:
                        print(f"🗑️ Удалено сообщение {conv_message_id}", flush=True)
                        return True
            if result.get('status') == 'ok' or result.get('deleted') == 1:
                print(f"🗑️ Удалено сообщение {conv_message_id}", flush=True)
                return True
        
        if isinstance(result, int):
            if result == 1:
                print(f"🗑️ Удалено сообщение {conv_message_id}", flush=True)
                return True
        
        print(f"⚠️ Не удалось удалить сообщение {conv_message_id}", flush=True)
        return False
        
    except Exception as e:
        print(f"⚠️ Ошибка удаления: {e}", flush=True)
        return False

def cleanup_worker():
    """Фоновый воркер для удаления сообщений бота"""
    global pending_deletions
    print("🔄 Воркер удаления запущен", flush=True)
    
    while True:
        try:
            time.sleep(30)
            
            now = datetime.now()
            to_delete = []
            
            with deletions_lock:
                remaining = []
                for item in pending_deletions:
                    elapsed = (now - item['created_at']).total_seconds()
                    if elapsed >= DELETE_AFTER:
                        to_delete.append(item)
                    else:
                        remaining.append(item)
                pending_deletions = remaining
            
            for item in to_delete:
                print(f"🔍 Удаляю сообщение {item['conv_message_id']}...", flush=True)
                if delete_message_by_conv_id(item['peer_id'], item['conv_message_id']):
                    remove_bot_message(item['conv_message_id'])
        except Exception as e:
            print(f"❌ Ошибка воркера: {e}", flush=True)
            time.sleep(5)

def get_inactive_users(peer_id: int) -> str:
    global user_activity
    now = datetime.now()
    inactive = []
    
    with activity_lock:
        for user_id, data in user_activity.items():
            if data.get('last_post_time'):
                last_post = data['last_post_time']
                days_inactive = (now - last_post).days
                if days_inactive > 10:
                    inactive.append((user_id, days_inactive))
            else:
                inactive.append((user_id, 999))
    
    if not inactive:
        return "✅ Все участники активны!"
    
    text = "📋 Неактивные участники (более 10 дней без публикаций):\n\n"
    for user_id, days in inactive:
        try:
            rate_limit()
            user_info = vk_user.users.get(user_ids=[user_id])[0]
            name = f"{user_info['first_name']} {user_info['last_name']}"
            text += f"👤 {name} (ID: {user_id}) — {days} дней\n"
        except:
            text += f"👤 ID: {user_id} — {days} дней\n"
    
    return text

def handle_vip_commands(text: str, user_id: int, peer_id: int, message_id: int) -> bool:
    global vip_links, queue
    
    text_lower = text.lower().strip()
    
    if not is_owner(user_id):
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, "❌ Только владелец чата может использовать команды!")
        return True
    
    if message_id:
        delete_message_by_conv_id(peer_id, message_id)
    
    cleanup_expired_vip()
    
    # ===== КОМАНДА !vip =====
    if text_lower.startswith('!vip '):
        try:
            vk_link = extract_vk_link(text.split()[1])
        except IndexError:
            send_message(peer_id, "⚠️ Использование: !vip [ссылка]")
            return True
        
        if vk_link:
            with vip_links_lock:
                for vip in vip_links:
                    if vip['link'] == vk_link:
                        send_message(peer_id, f"⚠️ Ссылка уже в VIP!")
                        return True
                vip_links.append({'link': vk_link, 'added_by': user_id, 'expires_at': datetime.now() + timedelta(hours=VIP_DURATION_HOURS)})
                save_vip_links()
                reload_vip_links()
            send_message(peer_id, f"⭐ VIP-ссылка добавлена на 24 часа!\n🔗 {make_clickable_link(vk_link)}")
        else:
            send_message(peer_id, "⚠️ Не удалось распознать ссылку!")
        return True
    
    # ===== КОМАНДА !delvip =====
    if text_lower.startswith('!delvip'):
        parts = text.split()
        if len(parts) >= 2:
            link_to_delete = extract_vk_link(parts[1])
            if link_to_delete:
                with vip_links_lock:
                    initial_count = len(vip_links)
                    vip_links = [v for v in vip_links if v['link'] != link_to_delete]
                    removed = initial_count - len(vip_links)
                    save_vip_links()
                    reload_vip_links()
                if removed > 0:
                    send_message(peer_id, f"✅ VIP-ссылка удалена!")
                else:
                    send_message(peer_id, f"⚠️ Ссылка не найдена в VIP!")
            else:
                send_message(peer_id, "⚠️ Не удалось распознать ссылку!")
        else:
            send_message(peer_id, "⚠️ Использование: !delvip [ссылка]")
        return True
    
    # ===== КОМАНДА !vip_list =====
    if text_lower == '!vip_list':
        with vip_links_lock:
            if not vip_links:
                send_message(peer_id, "📭 VIP-ссылок нет")
                return True
            result = "⭐ VIP-ссылки:\n\n"
            now = datetime.now()
            for vip in vip_links:
                remaining = vip['expires_at'] - now
                hours = int(remaining.total_seconds() // 3600)
                result += f"🔗 {make_clickable_link(vip['link'])}\n⏳ Осталось: {hours}ч\n\n"
            send_message(peer_id, result)
        return True
    
    # ===== КОМАНДА !inactive =====
    if text_lower == '!inactive':
        inactive_text = get_inactive_users(peer_id)
        send_message(peer_id, inactive_text)
        return True
    
    # ===== КОМАНДА !delqueue =====
    if text_lower.startswith('!delqueue'):
        parts = text.split()
        if len(parts) >= 2:
            link_to_delete = extract_vk_link(parts[1])
            if link_to_delete:
                with queue_lock:
                    initial_count = len(queue)
                    new_queue = []
                    for item in queue:
                        item_link = item['link']
                        if item_link.startswith('http'):
                            extracted = extract_vk_link(item_link)
                            if extracted:
                                item_link = extracted
                        if item_link != link_to_delete:
                            new_queue.append(item)
                    
                    removed_count = initial_count - len(new_queue)
                    queue = new_queue
                    save_queue()
                    reload_queue()
                
                if removed_count > 0:
                    send_message(peer_id, f"✅ Ссылка удалена из очереди ({removed_count} шт.)!\n🔗 {make_clickable_link(link_to_delete)}")
                else:
                    send_message(peer_id, f"⚠️ Ссылка не найдена в очереди!")
            else:
                send_message(peer_id, "⚠️ Не удалось распознать ссылку!")
        else:
            send_message(peer_id, "⚠️ Использование: !delqueue [ссылка]")
        return True
    
    # ===== КОМАНДА !clearqueue =====
    if text_lower == '!clearqueue':
        with queue_lock:
            count = len(queue)
            queue = []
            save_queue()
            reload_queue()
        send_message(peer_id, f"✅ Очередь полностью очищена! (удалено {count} ссылок)")
        return True
    
    # ===== КОМАНДА !queue_list =====
    if text_lower == '!queue_list':
        with queue_lock:
            if not queue:
                send_message(peer_id, "📭 Очередь пустая")
                return True
            result = "📋 Очередь ссылок:\n\n"
            for i, item in enumerate(queue, 1):
                result += f"{i}. 🔗 {make_clickable_link(item['link'])}\n"
            send_message(peer_id, result)
        return True
    
    return False

def process_message(peer_id: int, user_id: int, text: str, message_id: int, event_id: str = ""):
    global queue
    
    print(f"\n📩 {user_id}: {text[:80]}", flush=True)
    sys.stdout.flush()
    
    if user_id < 0:
        return
    
    text_lower = text.lower().strip()
    
    # Проверяем все команды владельца
    command_prefixes = ['!vip', '!delvip', '!inactive', '!delqueue', '!clearqueue', '!queue_list']
    is_command = any(text_lower.startswith(cmd) for cmd in command_prefixes)
    
    if is_command:
        handle_vip_commands(text, user_id, peer_id, message_id)
        return
    
    # Получаем упоминание пользователя
    mention = get_mention(user_id)
    
    # === ПУБЛИКАЦИЯ ССЫЛКИ АДМИНИСТРАТОРА ===
    if is_owner(user_id):
        vk_link = extract_vk_link(text)
        if vk_link:
            with queue_lock:
                queue.append({
                    'link': vk_link, 
                    'user_id': user_id, 
                    'timestamp': datetime.now(), 
                    'is_owner_post': 0
                })
                if len(queue) > MAX_QUEUE_SIZE:
                    queue.pop(0)
                save_queue()
            send_message(peer_id, f"{mention}, ✅ ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}")
            return
        else:
            return
    
    # === ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ ===
    vk_link = extract_vk_link(text)
    
    # Если ссылки нет - удаляем сообщение пользователя и отправляем предупреждение
    if not vk_link:
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, f"{mention}, 🔗 сообщение должно содержать только ссылку на контент!\n\n💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330")
        return
    
    # Проверяем, что сообщение содержит ТОЛЬКО ссылку
    if text != vk_link and not text.startswith('https://vk.com/') and not text.startswith('https://vk.ru/'):
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, f"{mention}, 🔗 сообщение должно содержать ТОЛЬКО ссылку на контент!\n\n💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330")
        return
    
    # Проверяем очередь
    if not can_user_post(user_id):
        need = max(0, 5 - get_posts_after_user(user_id))
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, f"{mention}, ⏳ ждем Вас через {need} ссылок!\n\n💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330")
        return
    
    # ===== ПРОВЕРКА VIP ССЫЛОК (всегда проверяем) =====
    cleanup_expired_vip()
    
    with vip_links_lock:
        if vip_links:
            missing_vip = []
            for vip in vip_links:
                if not check_user_like(user_id, vip['link']):
                    missing_vip.append(vip['link'])
            
            if missing_vip:
                if message_id:
                    delete_message_by_conv_id(peer_id, message_id)
                text = f"{mention}, ⭐ обязательно проставь лайки на VIP ссылки:\n\n"
                for link in missing_vip:
                    text += f"⭐ {make_clickable_link(link)}\n"
                text += f"\n{'─' * 30}\n"
                text += "⏳ На выполнение даётся 5 минут!\n"
                text += "✅ После того, как поставишь лайки, отправь свою ссылку снова.\n\n"
                text += "💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330"
                send_message(peer_id, text)
                return
    
    # ===== ПРОВЕРКА ОБЫЧНЫХ ССЫЛОК (ВСЕ ссылки в очереди) =====
    with queue_lock:
        regular_links = [item['link'] for item in queue[-10:]]
    
    if regular_links:
        missing_regular = []
        for link in regular_links:
            if not check_user_like(user_id, link):
                missing_regular.append(link)
        
        if missing_regular:
            if message_id:
                delete_message_by_conv_id(peer_id, message_id)
            text = f"{mention}, 📋 обязательно проставь лайки на предыдущие 10 ссылок:\n\n"
            for link in missing_regular:
                text += f"▫️ {make_clickable_link(link)}\n"
            text += f"\n{'─' * 30}\n"
            text += "⏳ На выполнение даётся 5 минут!\n"
            text += "✅ После того, как поставишь лайки, отправь свою ссылку снова.\n\n"
            text += "💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330"
            send_message(peer_id, text)
            return
    
    # ========== ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ - ПУБЛИКУЕМ ССЫЛКУ ==========
    
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_owner_post': 0})
        if len(queue) > MAX_QUEUE_SIZE:
            queue.pop(0)
        save_queue()
    
    with activity_lock:
        user_activity[user_id] = {
            'last_post_time': datetime.now(),
            'post_count': user_activity.get(user_id, {}).get('post_count', 0) + 1
        }
    save_user_activity(user_id)
    
    text = f"{mention}, ✅ ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}\n📊 В очереди: {len(queue)}\n\n"
    text += "⏳ Ждем Вас через 5 ссылок!\n\n"
    text += "💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330"
    send_message(peer_id, text)
    print(f"   ✅ Опубликовано!", flush=True)

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = CONFIRMATION_CODE.encode() if self.path in ['/', '/callback'] else b'Bot is running'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        
        try:
            data = json.loads(body)
            event_type = data.get('type', '')
            print(f"📥 Событие: {event_type}", flush=True)
            
            if event_type == 'confirmation':
                rb = CONFIRMATION_CODE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
            
            elif event_type == 'message_new':
                msg = data.get('object', {}).get('message', {})
                
                action = msg.get('action', {})
                if action and action.get('type') in ['chat_invite_user', 'chat_invite_user_by_link']:
                    rb = b'ok'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.send_header('Content-Length', str(len(rb)))
                    self.end_headers()
                    self.wfile.write(rb)
                    return
                
                event_id = data.get('event_id', '')
                thread = threading.Thread(target=process_message, args=(
                    msg.get('peer_id', 0),
                    msg.get('from_id', 0),
                    msg.get('text', ''),
                    msg.get('conversation_message_id', msg.get('id', 0)),
                    event_id
                ), daemon=True)
                thread.start()
                
                rb = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
            
            elif event_type in ['chat_invite_user', 'chat_invite_user_by_link']:
                rb = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
            
            else:
                rb = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
        except Exception as e:
            print(f"❌ Ошибка: {e}", flush=True)
            rb = b'ok'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(rb)))
            self.end_headers()
            self.wfile.write(rb)
    
    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    init_database()
    load_data()
    cleanup_old_queue()
    
    vk_group_session = vk_api.VkApi(token=GROUP_TOKEN)
    vk_group = vk_group_session.get_api()
    print("✅ Групповой API подключен", flush=True)
    
    vk_user_session = vk_api.VkApi(token=USER_TOKEN)
    vk_user = vk_user_session.get_api()
    print("✅ Пользовательский API подключен", flush=True)
    
    cleanup_thread = threading.Thread(target=cleanup_worker)
    cleanup_thread.daemon = False
    cleanup_thread.start()
    print("✅ Воркер удаления запущен", flush=True)
    
    print(f"📡 Порт: {PORT}", flush=True)
    sys.stdout.flush()
    
    server = HTTPServer(('0.0.0.0', PORT), CallbackHandler)
    print(f"✅ Сервер запущен на 0.0.0.0:{PORT}", flush=True)
    sys.stdout.flush()
    server.serve_forever()
