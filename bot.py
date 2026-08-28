import sys
import re
import time
import sqlite3
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ (BotHost + Мульти-проверка v2)...")
print("=" * 60)
sys.stdout.flush()

try:
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
    from vk_api.exceptions import ApiError
    print("✅ Библиотека vk-api загружена")
    sys.stdout.flush()
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    sys.stdout.flush()
    raise

# ====================== НАСТРОЙКИ ======================
GROUP_TOKEN = "vk1.a.5qm5-BTRZRJwz4_MQekr5_u4GxRq7VnW3QOIvtkgTfr-qygqi5_IrjLwlM9HkVnEh3kCGm_zLaw-BwFRgZ_3xsJAgeWJwSQQ3-5pageEgfPdK35TElpGfgjuF0IyKimfNvyBW4GfqI0EvBmzFDezi3SbFGcv-E_YHoQbCVRG2gxEW55BsVSl1epMYwakeLstp9YQy6jT5uFUXUMxtGlSLQ"
GROUP_ID = 241064421
ADMIN_IDS = [447457340]
# ======================================================

MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"

queue = []
queue_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()
vk = None

def make_clickable_link(vk_link: str) -> str:
    if not vk_link:
        return vk_link
    if vk_link.startswith('http'):
        return vk_link
    return f"https://vk.com/{vk_link}"

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

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
                is_admin_post INTEGER DEFAULT 0
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
        print(f"❌ Ошибка БД: {e}")
    sys.stdout.flush()

def load_data():
    global queue, vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, user_id, timestamp, is_admin_post FROM queue ORDER BY id DESC LIMIT ?', (MAX_QUEUE_SIZE,))
        rows = cursor.fetchall()
        queue = []
        for row in reversed(rows):
            queue.append({'link': row[0], 'user_id': row[1], 'timestamp': datetime.fromisoformat(row[2]), 'is_admin_post': row[3] if len(row) > 3 else 0})
        cursor.execute('SELECT link, added_by, expires_at FROM vip_links')
        vip_rows = cursor.fetchall()
        vip_links = []
        now = datetime.now()
        for row in vip_rows:
            expires_at = datetime.fromisoformat(row[2])
            if expires_at > now:
                vip_links.append({'link': row[0], 'added_by': row[1], 'expires_at': expires_at})
        conn.close()
        print(f"📂 Загружено: {len(queue)} ссылок, {len(vip_links)} VIP")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки: {e}")
    sys.stdout.flush()

def save_queue():
    global queue
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM queue')
        for item in queue:
            cursor.execute('INSERT INTO queue (link, user_id, timestamp, is_admin_post) VALUES (?, ?, ?, ?)',
                          (item['link'], item['user_id'], item['timestamp'].isoformat(), item.get('is_admin_post', 0)))
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

def rate_limit():
    time.sleep(RATE_LIMIT_DELAY)

def extract_vk_link(text: str) -> Optional[str]:
    if not text:
        return None
    patterns = [r'(wall-?\d+_\d+)', r'(photo-?\d+_\d+)', r'(video-?\d+_\d+)', r'(clip-?\d+_\d+)']
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

def get_content_type(vk_link: str) -> Tuple[str, int, int]:
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
    return '', 0, 0

def check_like_v1(user_id: int, vk_link: str) -> bool:
    """Метод 1: execute + likes.getList с VK Script циклом"""
    global vk
    content_type, owner_id, item_id = get_content_type(vk_link)
    if not content_type:
        return True
    
    code = f'''
    var user_id = {user_id};
    var likes = API.likes.getList({{
        "type": "{content_type}",
        "owner_id": {owner_id},
        "item_id": {item_id},
        "count": 1000
    }});
    var found = 0;
    if (likes != null && likes.items != null) {{
        var i = 0;
        while (i < likes.items.length) {{
            if (likes.items[i] == user_id) {{
                found = 1;
            }}
            i = i + 1;
        }}
    }}
    return {{"found": found}};
    '''
    
    try:
        rate_limit()
        response = vk.execute(code=code)
        if isinstance(response, dict):
            return response.get('found', 0) == 1
        return False
    except:
        return False

def check_like_v2(user_id: int, vk_link: str) -> bool:
    """Метод 2: wall.get с filter='likes'"""
    global vk
    if not vk_link.startswith('wall'):
        return True
    parts = vk_link.split('_')
    owner_id = int(parts[0][4:])
    item_id = int(parts[1])
    
    code = f'''
    var owner_id = {owner_id};
    var item_id = {item_id};
    var result = {{"found": 0}};
    var wall = API.wall.get({{
        "owner_id": owner_id,
        "count": 100,
        "filter": "likes"
    }});
    if (wall != null && wall.items != null) {{
        var i = 0;
        while (i < wall.items.length) {{
            if (wall.items[i].id == item_id) {{
                result.found = 1;
            }}
            i = i + 1;
        }}
    }}
    return result;
    '''
    
    try:
        rate_limit()
        response = vk.execute(code=code)
        if isinstance(response, dict):
            return response.get('found', 0) == 1
        return False
    except:
        return False

def check_like_v3(user_id: int, vk_link: str) -> bool:
    """Метод 3: wall.getById с user_likes"""
    global vk
    if not vk_link.startswith('wall'):
        return True
    parts = vk_link.split('_')
    owner_id = int(parts[0][4:])
    item_id = int(parts[1])
    
    try:
        rate_limit()
        response = vk.wall.getById(posts=[f"{owner_id}_{item_id}"])
        if response and len(response) > 0:
            likes = response[0].get('likes', {})
            return likes.get('user_likes', 0) == 1
        return False
    except:
        return False

def check_like_v4(user_id: int, vk_link: str) -> bool:
    """Метод 4: wall.search с extended"""
    global vk
    if not vk_link.startswith('wall'):
        return True
    parts = vk_link.split('_')
    owner_id = int(parts[0][4:])
    item_id = int(parts[1])
    
    try:
        rate_limit()
        response = vk.wall.search(
            owner_id=owner_id,
            query='',
            count=100,
            extended=1,
            fields='likes'
        )
        items = response.get('items', [])
        for item in items:
            if item.get('id') == item_id:
                likes = item.get('likes', {})
                return likes.get('user_likes', 0) == 1
        return False
    except:
        return False

def check_like_v5(user_id: int, vk_link: str) -> bool:
    """Метод 5: users.get с activities"""
    global vk
    try:
        rate_limit()
        response = vk.users.get(
            user_ids=[user_id],
            fields='activities'
        )
        # Не работает для проверки лайков, но пробуем
        return False
    except:
        return False

def check_like_v6(user_id: int, vk_link: str) -> bool:
    """Метод 6: fave.get для проверки избранного"""
    global vk
    try:
        rate_limit()
        response = vk.fave.get(count=100)
        items = response.get('items', [])
        for item in items:
            if 'link_url' in item and vk_link in item.get('link_url', ''):
                return True
        return False
    except:
        return False

def check_like_v7(user_id: int, vk_link: str) -> bool:
    """Метод 7: wall.getComments для проверки комментария"""
    global vk
    if not vk_link.startswith('wall'):
        return True
    parts = vk_link.split('_')
    owner_id = int(parts[0][4:])
    item_id = int(parts[1])
    
    try:
        rate_limit()
        response = vk.wall.getComments(
            owner_id=owner_id,
            post_id=item_id,
            count=100
        )
        items = response.get('items', [])
        for comment in items:
            if comment.get('from_id') == user_id:
                return True
        return False
    except:
        return False

def check_user_like(user_id: int, vk_link: str) -> bool:
    """Проверка всеми методами"""
    methods = [
        check_like_v1,
        check_like_v2,
        check_like_v3,
        check_like_v4,
        check_like_v6,
        check_like_v7,
    ]
    
    for method in methods:
        try:
            if method(user_id, vk_link):
                print(f"   ✅ Лайк найден через {method.__name__}")
                return True
        except:
            continue
    
    print(f"   ❌ Лайк не найден ни одним методом")
    return False

def can_user_post(user_id: int) -> bool:
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id and not item.get('is_admin_post', 0)]
        if not user_posts:
            return True
        return len(queue) - user_posts[-1] - 1 >= 5

def get_posts_after_user(user_id: int) -> int:
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id and not item.get('is_admin_post', 0)]
        if not user_posts:
            return 0
        return len(queue) - user_posts[-1] - 1

def send_message(peer_id: int, text: str):
    global vk
    if vk is None:
        return None
    try:
        rate_limit()
        result = vk.messages.send(peer_id=peer_id, message=text, random_id=int(time.time() * 1000))
        print(f"✅ Отправлено: {text[:50]}...")
        return result
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None
    sys.stdout.flush()

def delete_message(peer_id: int, message_id: int) -> bool:
    global vk
    if vk is None or not message_id:
        return False
    try:
        rate_limit()
        if peer_id >= 2000000000:
            vk.messages.delete(peer_id=peer_id, cmids=[message_id], delete_for_all=True)
        else:
            vk.messages.delete(peer_id=peer_id, message_ids=[message_id], delete_for_all=True)
        return True
    except:
        return False

def handle_vip_commands(text: str, user_id: int, peer_id: int) -> bool:
    global vip_links
    if not is_admin(user_id):
        send_message(peer_id, "❌ Только администраторы!")
        return True
    if text.lower().startswith('!vip '):
        vk_link = extract_vk_link(text.split()[1])
        if vk_link:
            with vip_links_lock:
                vip_links.append({'link': vk_link, 'added_by': user_id, 'expires_at': datetime.now() + timedelta(hours=24)})
                save_vip_links()
            send_message(peer_id, f"⭐ VIP-ссылка добавлена!\n🔗 {make_clickable_link(vk_link)}")
        return True
    if text.lower() == '!vip_list':
        with vip_links_lock:
            if not vip_links:
                send_message(peer_id, "📭 VIP-ссылок нет")
                return True
            text = "⭐ VIP-ссылки:\n\n"
            for vip in vip_links:
                text += f"🔗 {make_clickable_link(vip['link'])}\n\n"
            send_message(peer_id, text)
        return True
    return False

def process_message(event):
    global queue
    try:
        message = event.object.message
        peer_id = message['peer_id']
        user_id = message['from_id']
        text = message.get('text', '').strip()
        message_id = message.get('conversation_message_id', message.get('id', 0))
    except:
        return
    
    print(f"\n📩 {user_id}: {text[:50]}")
    sys.stdout.flush()
    
    if user_id < 0:
        return
    
    admin = is_admin(user_id)
    
    if text.lower().startswith('!vip') or text.lower().startswith('!delvip'):
        if message_id:
            delete_message(peer_id, message_id)
        handle_vip_commands(text, user_id, peer_id)
        return
    
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        if admin:
            return
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Нужна ссылка!")
        return
    
    if admin:
        with queue_lock:
            queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 1})
            if len(queue) > MAX_QUEUE_SIZE:
                queue.pop(0)
            save_queue()
        send_message(peer_id, f"✅ Опубликовано!\n🔗 {make_clickable_link(vk_link)}")
        return
    
    # Проверка VIP-лайков
    with vip_links_lock:
        if vip_links:
            for vip in vip_links:
                if not check_user_like(user_id, vip['link']):
                    if message_id:
                        delete_message(peer_id, message_id)
                    send_message(peer_id, f"⭐ Нужен лайк на VIP:\n🔗 {make_clickable_link(vip['link'])}")
                    return
    
    # Проверка лайков на очередь
    with queue_lock:
        links_to_check = [item['link'] for item in queue[-10:]]
    if links_to_check:
        for link in links_to_check:
            if not check_user_like(user_id, link):
                if message_id:
                    delete_message(peer_id, message_id)
                send_message(peer_id, f"❌ Нужен лайк на:\n🔗 {make_clickable_link(link)}")
                return
    
    # Частота
    if not can_user_post(user_id):
        need = max(0, 5 - get_posts_after_user(user_id))
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"⏳ Жди {need} постов!")
        return
    
    if message_id:
        delete_message(peer_id, message_id)
    
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 0})
        if len(queue) > MAX_QUEUE_SIZE:
            queue.pop(0)
        save_queue()
    
    send_message(peer_id, f"✅ Опубликовано!\n🔗 {make_clickable_link(vk_link)}")

def main():
    global vk
    init_database()
    load_data()
    try:
        print("🔄 Подключение...")
        sys.stdout.flush()
        vk_session = vk_api.VkApi(token=GROUP_TOKEN)
        vk = vk_session.get_api()
        print("✅ VK API подключен")
        longpoll = VkBotLongPoll(vk_session, GROUP_ID)
        print("✅ Long Poll подключен")
        print("=" * 60)
        sys.stdout.flush()
        
        for event in longpoll.listen():
            if event.type == VkBotEventType.MESSAGE_NEW:
                process_message(event)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        traceback.print_exc()
        sys.stdout.flush()

if __name__ == "__main__":
    main()
