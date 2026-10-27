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
    print(f"❌ Ошибка импорта vk_api: {e}")
    sys.stdout.flush()
    raise

try:
    from vk_api.requests_pool import VkRequestsPool
    print("✅ VkRequestsPool загружен")
    sys.stdout.flush()
except ImportError:
    VkRequestsPool = None
    print("⚠️ VkRequestsPool не найден, будет использоваться одиночная проверка")
    sys.stdout.flush()

# ====================== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======================
GROUP_TOKEN = os.getenv('GROUP_TOKEN', '')
USER_TOKEN = os.getenv('USER_TOKEN', '')
GROUP_ID = int(os.getenv('GROUP_ID', '242396312'))
CONFIRMATION_CODE = os.getenv('CONFIRMATION_CODE', '')
PORT = int(os.getenv('PORT', '3000'))
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', '447457340')
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(',') if x.strip()]
DELETE_AFTER = 300
# =============================================================================

MAX_QUEUE_SIZE = 10
VIP_DURATIONS = {
    '24': 24,
    '48': 48,
    '72': 72,
}
DEFAULT_VIP_DURATION = 24
RATE_LIMIT_DELAY = 0.1
DB_FILE = "bot_database.db"

queue = []
queue_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()
vk_group = None
vk_user = None
vk_user_session = None

user_name_cache = {}
user_name_cache_lock = threading.Lock()
USER_NAME_CACHE_TTL = 3600

pending_deletions = []
deletions_lock = threading.Lock()

# Защита от дублей по event_id
processed_events = set()
processed_events_lock = threading.Lock()

# Защита от дублей по peer_id + conversation_message_id
processed_messages = set()
processed_messages_lock = threading.Lock()

# Исключения: (user_id, link) -> добавлено
skip_likes = {}
skip_likes_lock = threading.Lock()

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
    global vk_user
    if vk_user is None:
        return ""
    
    now = time.time()
    with user_name_cache_lock:
        if user_id in user_name_cache:
            name, ts = user_name_cache[user_id]
            if now - ts < USER_NAME_CACHE_TTL:
                return name
    
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
    name = get_user_name(user_id)
    if name:
        return f"[id{user_id}|{name}]"
    return f"[id{user_id}|пользователь]"


def get_user_id_by_link(link: str) -> Optional[int]:
    global vk_user
    if vk_user is None:
        return None
    
    match = re.search(r'(?:https?://)?(?:m\.)?vk\.(?:com|ru)/([a-zA-Z0-9_.]+)', link)
    if not match:
        return None
    
    username = match.group(1)
    
    if username.lower() in ('wall', 'photo', 'video', 'clip', 'audio', 'topic',
                            'market', 'album', 'poll', 'note', 'doc', 'feed',
                            'im', 'friends', 'groups', 'videos', 'audios', 'photos'):
        return None
    
    if username.startswith('id') and username[2:].isdigit():
        return int(username[2:])
    
    try:
        rate_limit()
        response = vk_user.users.get(user_ids=[username])
        if isinstance(response, list) and len(response) > 0:
            return response[0].get('id')
    except Exception as e:
        print(f"⚠️ Ошибка получения ID: {e}", flush=True)
        return None
    
    return None


def add_skip_like(user_id: int, link: str):
    global skip_likes
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO skip_likes (user_id, link, added_at)
            VALUES (?, ?, ?)
        ''', (user_id, link, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        
        with skip_likes_lock:
            skip_likes[(user_id, link)] = datetime.now()
        print(f"✅ Исключение добавлено: user={user_id}, link={link}", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка добавления исключения: {e}", flush=True)


def remove_skip_like(user_id: int, link: str):
    global skip_likes
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM skip_likes WHERE user_id = ? AND link = ?',
                      (user_id, link))
        conn.commit()
        conn.close()
        
        with skip_likes_lock:
            skip_likes.pop((user_id, link), None)
        print(f"🗑️ Исключение удалено: user={user_id}, link={link}", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка удаления исключения: {e}", flush=True)


def is_skip_like(user_id: int, link: str) -> bool:
    with skip_likes_lock:
        return (user_id, link) in skip_likes


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
            CREATE TABLE IF NOT EXISTS bot_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id INTEGER NOT NULL,
                conv_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS skip_likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                added_at TEXT NOT NULL,
                UNIQUE(user_id, link)
            )
        ''')
        conn.commit()
        conn.close()
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
    sys.stdout.flush()


def load_data():
    global queue, vip_links, pending_deletions, skip_likes
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
        
        cursor.execute('SELECT peer_id, conv_message_id, created_at FROM bot_messages')
        for row in cursor.fetchall():
            pending_deletions.append({
                'peer_id': row[0],
                'conv_message_id': row[1],
                'created_at': datetime.fromisoformat(row[2])
            })
        
        cursor.execute('SELECT user_id, link, added_at FROM skip_likes')
        skip_likes = {}
        for row in cursor.fetchall():
            skip_likes[(row[0], row[1])] = datetime.fromisoformat(row[2])
        
        conn.close()
        print(f"📂 Загружено: {len(queue)} ссылок, {len(vip_links)} VIP, {len(pending_deletions)} на удаление, {len(skip_likes)} исключений")
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


def check_single_like(user_id: int, vk_link: str) -> bool:
    global vk_user
    if vk_user is None:
        return False
    
    content_type, owner_id, item_id = get_content_type(vk_link)
    if not content_type or owner_id == 0 or item_id == 0:
        return True
    
    try:
        rate_limit()
        
        if content_type == 'video':
            response = vk_user.likes.isLiked(
                user_id=user_id,
                type=content_type,
                owner_id=str(owner_id),
                item_id=item_id
            )
        else:
            response = vk_user.likes.isLiked(
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
    except Exception as e:
        print(f"   ❌ Ошибка проверки {vk_link}: {e}", flush=True)
        return False


def check_likes_batch(user_id: int, links: list) -> dict:
    global vk_user_session
    if not links:
        return {}
    
    results = {}
    execute_links = []
    single_links = []
    
    for link in links:
        # Пропускаем ссылки, для которых есть исключение
        if is_skip_like(user_id, link):
            print(f"   ⏭️ Пропуск (исключение) {link}", flush=True)
            results[link] = True
            continue
        
        ctype, oid, iid = get_content_type(link)
        if not ctype or oid == 0 or iid == 0:
            results[link] = True
            continue
        
        if ctype == 'video':
            single_links.append((link, ctype, oid, iid))
        else:
            execute_links.append((link, ctype, oid, iid))
    
    if single_links:
        print(f"   🎬 Одиночная проверка видео/клипов ({len(single_links)} шт.)", flush=True)
        for link, ctype, oid, iid in single_links:
            results[link] = check_single_like(user_id, link)
    
    if execute_links and vk_user_session is not None and VkRequestsPool is not None:
        print(f"   📦 Пакетная проверка постов/фото ({len(execute_links)} шт.)", flush=True)
        try:
            with VkRequestsPool(vk_user_session) as pool:
                requests_map = {}
                for link, ctype, oid, iid in execute_links:
                    req = pool.method('likes.isLiked', {
                        'user_id': user_id,
                        'type': ctype,
                        'owner_id': oid,
                        'item_id': iid
                    })
                    requests_map[req] = link
            
            for req, link in requests_map.items():
                if req.ok:
                    res = req.result
                    if isinstance(res, dict):
                        liked = res.get('liked', 0)
                        copied = res.get('copied', 0)
                        results[link] = (liked == 1 or copied == 1)
                    else:
                        results[link] = (res == 1)
                else:
                    print(f"   ⚠️ Ошибка проверки {link}: {req.error}", flush=True)
                    results[link] = False
                    
        except Exception as e:
            print(f"   ❌ Ошибка пакетной проверки: {e}", flush=True)
            for link, ctype, oid, iid in execute_links:
                if link not in results:
                    results[link] = check_single_like(user_id, link)
    elif execute_links:
        print(f"   📦 Проверка постов/фото (одиночная, {len(execute_links)} шт.)", flush=True)
        for link, ctype, oid, iid in execute_links:
            results[link] = check_single_like(user_id, link)
    
    return results


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
        conv_msg_id = None
        if isinstance(result, list) and len(result) > 0:
            conv_msg_id = result[0].get('conversation_message_id')
        elif isinstance(result, dict):
            conv_msg_id = result.get('conversation_message_id')
        elif isinstance(result, int) and result != 0:
            conv_msg_id = result
        if conv_msg_id:
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
    try:
        rate_limit()
        params = {
            'peer_id': peer_id,
            'cmids': conv_message_id,
            'delete_for_all': 1,
            'group_id': GROUP_ID
        }
        result = vk_api_request('messages.delete', params)
        if isinstance(result, dict) and 'error' in result:
            error_msg = str(result['error'])
            if 'message can not be found' in error_msg or 'message not found' in error_msg:
                remove_bot_message(conv_message_id)
                return True
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
        if isinstance(result, int) and result == 1:
            print(f"🗑️ Удалено сообщение {conv_message_id}", flush=True)
            return True
        print(f"⚠️ Не удалось удалить сообщение {conv_message_id}", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ Ошибка удаления: {e}", flush=True)
        return False


def cleanup_worker():
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


def handle_vip_commands(text: str, user_id: int, peer_id: int, message_id: int) -> bool:
    global vip_links, queue, skip_likes
    text_lower = text.lower().strip()
    if not is_owner(user_id):
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, "❌ Только владелец чата может использовать команды!")
        return True
    if message_id and not is_owner(user_id):
        delete_message_by_conv_id(peer_id, message_id)
    cleanup_expired_vip()
    
    if text_lower.startswith('!vip '):
        parts = text.split()
        if len(parts) < 2:
            send_message(peer_id, "⚠️ Использование: !vip [ссылка] [24|48|72]\n\nПример: !vip wall-123_456 48\n(срок необязателен, по умолчанию 24 часа)")
            return True

        vk_link = extract_vk_link(parts[1])
        if not vk_link:
            send_message(peer_id, "⚠️ Не удалось распознать ссылку!")
            return True

        # Срок: второй аргумент, если есть; иначе — по умолчанию
        duration_hours = DEFAULT_VIP_DURATION
        if len(parts) >= 3:
            duration_arg = parts[2].strip()
            if duration_arg in VIP_DURATIONS:
                duration_hours = VIP_DURATIONS[duration_arg]
            else:
                send_message(peer_id, f"⚠️ Неверный срок: {duration_arg}\nДоступные варианты: 24, 48, 72")
                return True

        with vip_links_lock:
            for vip in vip_links:
                if vip['link'] == vk_link:
                    send_message(peer_id, f"⚠️ Ссылка уже в VIP!")
                    return True
            vip_links.append({
                'link': vk_link,
                'added_by': user_id,
                'expires_at': datetime.now() + timedelta(hours=duration_hours)
            })
            save_vip_links()
            reload_vip_links()
        send_message(peer_id, f"⭐ VIP-ссылка добавлена на {duration_hours} ч!\n🔗 {make_clickable_link(vk_link)}")
        return True
    
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
    
    if text_lower == '!clearqueue':
        with queue_lock:
            count = len(queue)
            queue = []
            save_queue()
            reload_queue()
        send_message(peer_id, f"✅ Очередь полностью очищена! (удалено {count} ссылок)")
        return True
    
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
    
    # ===== КОМАНДА !skip =====
    if text_lower.startswith('!skip '):
        parts = text.split()
        if len(parts) < 3:
            send_message(peer_id, "⚠️ Использование: !skip [ссылка на участника] [ссылка на контент]\n\nПример: !skip vk.com/id123456789 wall-123_456")
            return True
        user_link = parts[1]
        content_link = parts[2]
        target_user_id = get_user_id_by_link(user_link)
        if not target_user_id:
            send_message(peer_id, "❌ Не удалось найти пользователя по ссылке!")
            return True
        vk_link = extract_vk_link(content_link)
        if not vk_link:
            send_message(peer_id, "❌ Не удалось распознать ссылку на контент!")
            return True
        add_skip_like(target_user_id, vk_link)
        user_mention = get_mention(target_user_id)
        send_message(peer_id, f"✅ Исключение добавлено!\n\n👤 Участник: {user_mention}\n🔗 Ссылка: {make_clickable_link(vk_link)}\n\nТеперь этот участник может пропустить лайк на эту ссылку.")
        return True
    
    # ===== КОМАНДА !unskip =====
    if text_lower.startswith('!unskip '):
        parts = text.split()
        if len(parts) < 3:
            send_message(peer_id, "⚠️ Использование: !unskip [ссылка на участника] [ссылка на контент]")
            return True
        user_link = parts[1]
        content_link = parts[2]
        target_user_id = get_user_id_by_link(user_link)
        if not target_user_id:
            send_message(peer_id, "❌ Не удалось найти пользователя по ссылке!")
            return True
        vk_link = extract_vk_link(content_link)
        if not vk_link:
            send_message(peer_id, "❌ Не удалось распознать ссылку на контент!")
            return True
        remove_skip_like(target_user_id, vk_link)
        user_mention = get_mention(target_user_id)
        send_message(peer_id, f"✅ Исключение удалено!\n\n👤 Участник: {user_mention}\n🔗 Ссылка: {make_clickable_link(vk_link)}\n\nТеперь участник снова проверяется на лайк.")
        return True
    
    # ===== КОМАНДА !skip_list =====
    if text_lower == '!skip_list':
        with skip_likes_lock:
            if not skip_likes:
                send_message(peer_id, "📭 Исключений нет")
                return True
            result = "📋 Активные исключения (лайки):\n\n"
            for (uid, link) in skip_likes.keys():
                user_mention = get_mention(uid)
                result += f"👤 {user_mention} → 🔗 {make_clickable_link(link)}\n"
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
    command_prefixes = ['!vip', '!delvip', '!delqueue', '!clearqueue', '!queue_list', '!skip', '!unskip', '!skip_list']
    is_command = any(text_lower.startswith(cmd) for cmd in command_prefixes)
    if is_command:
        handle_vip_commands(text, user_id, peer_id, message_id)
        return
    
    mention = get_mention(user_id)
    
    if is_owner(user_id):
        vk_link = extract_vk_link(text)
        if vk_link:
            with queue_lock:
                queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_owner_post': 0})
                if len(queue) > MAX_QUEUE_SIZE:
                    queue.pop(0)
                save_queue()
            send_message(peer_id, f"{mention}, ✅ ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}")
            return
        else:
            return
    
    vk_link = extract_vk_link(text)
    if not vk_link:
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, f"{mention}, 🔗 сообщение должно содержать только ссылку на контент!\n\n💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330")
        return
    
    text_without_link = text
    text_without_link = re.sub(r'https?://(m\.)?vk(?:video)?\.(com|ru)/' + re.escape(vk_link) + r'(\?[^\s]*)?', '', text_without_link)
    text_without_link = re.sub(r'(m\.)?vk(?:video)?\.(com|ru)/' + re.escape(vk_link) + r'(\?[^\s]*)?', '', text_without_link)
    text_without_link = text_without_link.replace(vk_link, '')
    text_without_link = text_without_link.strip()
    if text_without_link:
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, f"{mention}, 🔗 сообщение должно содержать ТОЛЬКО ссылку на контент!\n\n💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330")
        return
    
    if not can_user_post(user_id):
        need = max(0, 5 - get_posts_after_user(user_id))
        if message_id:
            delete_message_by_conv_id(peer_id, message_id)
        send_message(peer_id, f"{mention}, ⏳ ждем Вас через {need} ссылок!\n\n💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330")
        return
    
    cleanup_expired_vip()
    
    with vip_links_lock:
        vip_links_list = [v['link'] for v in vip_links]
    
    if vip_links_list:
        print(f"\n   ⭐ ПРОВЕРКА VIP-ССЫЛОК ({len(vip_links_list)} шт.):", flush=True)
        vip_results = check_likes_batch(user_id, vip_links_list)
        missing_vip = [link for link in vip_links_list if not vip_results.get(link, False)]
        
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
    
    with queue_lock:
        regular_links = [item['link'] for item in queue[-10:]]
    
    if regular_links:
        print(f"\n   📋 ПРОВЕРКА ОБЫЧНЫХ ССЫЛОК ({len(regular_links)} шт.):", flush=True)
        regular_results = check_likes_batch(user_id, regular_links)
        missing_regular = [link for link in regular_links if not regular_results.get(link, False)]
        
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
    
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_owner_post': 0})
        if len(queue) > MAX_QUEUE_SIZE:
            queue.pop(0)
        save_queue()
    
    text = f"{mention}, ✅ ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}\n📊 В очереди: {len(queue)}\n\n"
    text += "⏳ Ждем Вас через 5 ссылок!\n\n"
    text += "💎 По вопросам и для покупки VIP — пишите: https://vk.com/id1121274330"
    send_message(peer_id, text)
    print(f"   ✅ Опубликовано!", flush=True)


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = CONFIRMATION_CODE.encode() if self.path in ['/', '/callback'] else b'Bot is running'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception as e:
            print(f"❌ Ошибка do_GET: {e}", flush=True)
    
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            event_type = data.get('type', '')
            
            if event_type == 'confirmation':
                rb = CONFIRMATION_CODE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
                self.wfile.flush()
                return
            
            print(f"📥 Событие: {event_type}", flush=True)
            
            if event_type == 'message_new':
                # ===== ЗАЩИТА ОТ ДУБЛЕЙ ПО event_id =====
                event_id = data.get('event_id', '')
                if event_id:
                    with processed_events_lock:
                        if event_id in processed_events:
                            print(f"⏭️ Дубль события {event_id}, пропускаем", flush=True)
                            rb = b'ok'
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/plain')
                            self.send_header('Content-Length', str(len(rb)))
                            self.end_headers()
                            self.wfile.write(rb)
                            self.wfile.flush()
                            return
                        processed_events.add(event_id)
                
                msg = data.get('object', {}).get('message', {})
                action = msg.get('action', {})
                if action and action.get('type') in ['chat_invite_user', 'chat_invite_user_by_link']:
                    rb = b'ok'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.send_header('Content-Length', str(len(rb)))
                    self.end_headers()
                    self.wfile.write(rb)
                    self.wfile.flush()
                    return
                
                # ===== ЗАЩИТА ОТ ДУБЛЕЙ ПО peer_id + conversation_message_id =====
                peer_id = msg.get('peer_id', 0)
                cmid = msg.get('conversation_message_id', 0)
                if peer_id and cmid:
                    msg_key = (peer_id, cmid)
                    with processed_messages_lock:
                        if msg_key in processed_messages:
                            print(f"⏭️ Дубль сообщения {msg_key}, пропускаем", flush=True)
                            rb = b'ok'
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/plain')
                            self.send_header('Content-Length', str(len(rb)))
                            self.end_headers()
                            self.wfile.write(rb)
                            self.wfile.flush()
                            return
                        processed_messages.add(msg_key)
                
                # ===== СНАЧАЛА ОТВЕЧАЕМ ok И СБРАСЫВАЕМ БУФЕР =====
                rb = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
                self.wfile.flush()
                
                # ===== ПОТОМ ЗАПУСКАЕМ ОБРАБОТКУ =====
                thread = threading.Thread(target=process_message, args=(
                    msg.get('peer_id', 0),
                    msg.get('from_id', 0),
                    msg.get('text', ''),
                    msg.get('conversation_message_id', msg.get('id', 0)),
                    event_id
                ), daemon=True)
                thread.start()
                return
            else:
                rb = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
                self.wfile.flush()
        except Exception as e:
            print(f"❌ Ошибка do_POST: {e}", flush=True)
            try:
                rb = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
                self.wfile.flush()
            except:
                pass
    
    def log_message(self, fmt, *args):
        pass


# ✅ Класс сервера с allow_reuse_address для корректного перезапуска
class CallbackServer(HTTPServer):
    allow_reuse_address = True


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
    
    # ✅ Теперь CallbackServer определён выше — ошибки NameError не будет
    server = CallbackServer(('0.0.0.0', PORT), CallbackHandler)
    print(f"✅ Сервер запущен на 0.0.0.0:{PORT}", flush=True)
    sys.stdout.flush()
    server.serve_forever()
