import sys
import re
import time
import sqlite3
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ (Long Poll)...")
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

GROUP_TOKEN = "vk1.a.s5mgEVHWOVgpTPQ2AhN4hYF15Tc6vsHIsmavsZNDFTZkvKB-mwOR-f1aUuQ27AWpc5wfZLZH42iJy74xZDafcBZJwzmupX8OUN8MnlDxZYuHLk5NrJHDwIUuFiDy6S8OTbl0trJEUg77amTmVsgZPypu-EkumFvDiQFIkMt3twuGQD2PpnckpaASfFXLw0HMxp3CbBTZsLy1DEilvoJRbA"
GROUP_ID = 241064421

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
        print(f"❌ Ошибка БД: {e}")
    sys.stdout.flush()

def load_data():
    global queue, vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT link, user_id, timestamp FROM queue ORDER BY id DESC LIMIT ?', (MAX_QUEUE_SIZE,))
        rows = cursor.fetchall()
        queue = []
        for row in reversed(rows):
            queue.append({'link': row[0], 'user_id': row[1], 'timestamp': datetime.fromisoformat(row[2])})
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
            cursor.execute('INSERT INTO queue (link, user_id, timestamp) VALUES (?, ?, ?)',
                          (item['link'], item['user_id'], item['timestamp'].isoformat()))
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
    patterns = [
        r'(wall-?\d+_\d+)',
        r'(photo-?\d+_\d+)',
        r'(video-?\d+_\d+)',
        r'(clip-?\d+_\d+)',
        r'(audio-?\d+_\d+)',
        r'(topic-?\d+_\d+)',
        r'(market-?\d+_\d+)',
    ]
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
    elif type_and_owner.startswith('audio'):
        return 'audio', int(type_and_owner[5:]), item_id
    elif type_and_owner.startswith('market'):
        return 'market', int(type_and_owner[6:]), item_id
    elif type_and_owner.startswith('topic'):
        return 'topic', int(type_and_owner[5:]), item_id
    return '', 0, 0

def check_user_like_wall_filter(user_id: int, vk_link: str) -> bool:
    """Проверка через wall.get с filter='likes'"""
    global vk
    if not vk_link.startswith('wall'):
        return True  # Для не-wall пропускаем
    
    parts = vk_link.split('_')
    owner_id = int(parts[0][4:])
    item_id = int(parts[1])
    
    code = f'''
    var user_id = {user_id};
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
        print(f"   📦 wall.get+filter=likes: {response}")
        if isinstance(response, dict):
            return response.get('found', 0) == 1
        return False
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        return False

def check_like_direct(user_id: int, vk_link: str) -> bool:
    """Прямой вызов likes.isLiked через execute"""
    global vk
    content_type, owner_id, item_id = get_content_type(vk_link)
    if not content_type:
        return True
    
    code = f'''
    var result = API.likes.isLiked({{
        "user_id": {user_id},
        "type": "{content_type}",
        "owner_id": {owner_id},
        "item_id": {item_id}
    }});
    return result;
    '''
    
    try:
        rate_limit()
        response = vk.execute(code=code)
        print(f"   📦 likes.isLiked: {response}")
        if isinstance(response, dict):
            return response.get('liked', 0) == 1
        return response == 1
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        return False

def check_user_like(user_id: int, vk_link: str) -> bool:
    """Проверка лайка всеми методами"""
    # Метод 1: wall.get + filter='likes' (для wall)
    if vk_link.startswith('wall'):
        if check_user_like_wall_filter(user_id, vk_link):
            print(f"   ✅ Лайк найден (wall filter)")
            return True
    
    # Метод 2: прямой likes.isLiked
    if check_like_direct(user_id, vk_link):
        print(f"   ✅ Лайк найден (isLiked)")
        return True
    
    print(f"   ❌ Лайк не найден")
    return False

def can_user_post(user_id: int) -> bool:
    global queue
    with queue_lock:
        user_posts_indices = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        if not user_posts_indices:
            return True
        last_user_post_index = user_posts_indices[-1]
        posts_after = len(queue) - last_user_post_index - 1
        return posts_after >= 5

def get_posts_after_user(user_id: int) -> int:
    global queue
    with queue_lock:
        user_posts_indices = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        if not user_posts_indices:
            return 0
        last_user_post_index = user_posts_indices[-1]
        return len(queue) - last_user_post_index - 1

def send_message(peer_id: int, text: str):
    global vk
    if vk is None:
        return
    try:
        rate_limit()
        vk.messages.send(peer_id=peer_id, message=text, random_id=int(time.time() * 1000))
        print(f"✅ Отправлено: {text[:50]}...")
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
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
        print(f"🗑️ Удалено сообщение {message_id}")
        return True
    except Exception as e:
        print(f"❌ Ошибка удаления: {e}")
        return False

def handle_vip_commands(text: str, user_id: int, peer_id: int) -> bool:
    global vip_links
    if text.lower().startswith('!vip ') or text.lower() == '!vip':
        parts = text.split()
        if len(parts) < 2:
            send_message(peer_id, "❌ Формат: !vip wall-123_456")
            return True
        raw_link = parts[1]
        vk_link = extract_vk_link(raw_link)
        if not vk_link:
            send_message(peer_id, "❌ Не удалось распознать ссылку.")
            return True
        with vip_links_lock:
            for vip in vip_links:
                if vip['link'] == vk_link:
                    send_message(peer_id, f"⚠️ Ссылка {make_clickable_link(vk_link)} уже есть в VIP-списке.")
                    return True
            expires_at = datetime.now() + timedelta(hours=VIP_DURATION_HOURS)
            vip_links.append({'link': vk_link, 'added_by': user_id, 'expires_at': expires_at})
            save_vip_links()
        clickable = make_clickable_link(vk_link)
        send_message(peer_id, f"⭐ VIP-ссылка добавлена!\n🔗 {clickable}\n⏳ Действует до: {expires_at.strftime('%d.%m.%Y %H:%M')}")
        return True
    if text.lower().startswith('!delvip'):
        parts = text.split()
        if len(parts) < 2:
            send_message(peer_id, "❌ Формат: !delvip wall-123_456")
            return True
        vk_link = parts[1]
        with vip_links_lock:
            vip_to_remove = None
            for vip in vip_links:
                if vip['link'] == vk_link:
                    vip_to_remove = vip
                    break
            if not vip_to_remove:
                send_message(peer_id, f"⚠️ VIP-ссылка {make_clickable_link(vk_link)} не найдена.")
                return True
            vip_links.remove(vip_to_remove)
            save_vip_links()
        send_message(peer_id, "✅ VIP-ссылка удалена!")
        return True
    if text.lower() == '!vip_list' or text.lower() == '!viplist':
        with vip_links_lock:
            if not vip_links:
                send_message(peer_id, "📭 Активных VIP-ссылок нет.")
                return True
            vip_text = "⭐ Активные VIP-ссылки:\n\n"
            now = datetime.now()
            for i, vip in enumerate(vip_links, 1):
                remaining = vip['expires_at'] - now
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                clickable = make_clickable_link(vip['link'])
                vip_text += f"{i}. 🔗 {clickable}\n   ⏳ Осталось: {hours}ч {minutes}мин\n\n"
            send_message(peer_id, vip_text)
        return True
    return False

def process_message(event):
    global queue
    print("\n📩 Новое сообщение")
    try:
        message = event.object.message
        peer_id = message['peer_id']
        user_id = message['from_id']
        text = message.get('text', '').strip()
        message_id = message.get('conversation_message_id', message.get('id', 0))
    except Exception as e:
        print(f"⚠️ Ошибка чтения: {e}")
        return
    print(f"👤 Пользователь {user_id}: {text[:50] if text else '(пусто)'}")
    sys.stdout.flush()
    if user_id < 0:
        return
    if text.lower().startswith('!vip') or text.lower().startswith('!delvip'):
        if message_id:
            delete_message(peer_id, message_id)
        handle_vip_commands(text, user_id, peer_id)
        return
    vk_link = extract_vk_link(text)
    if not vk_link:
        print(f"🗑️ Нет ссылки, удаляем")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Нужна ссылка на контент ВКонтакте!")
        return
    print(f"✅ Ссылка: {vk_link}")
    
    # Проверка VIP-лайков
    with vip_links_lock:
        if vip_links:
            print(f"🔍 Проверяем VIP-лайки...")
            vip_ok = True
            missing_vip = []
            for vip in vip_links:
                has_like = check_user_like(user_id, vip['link'])
                if not has_like:
                    vip_ok = False
                    missing_vip.append(vip['link'])
            if not vip_ok:
                vip_text = "⭐ Поставь лайки на VIP-ссылки:\n\n"
                for i, link in enumerate(missing_vip, 1):
                    clickable = make_clickable_link(link)
                    vip_text += f"{i}. 🔗 {clickable}\n"
                print(f"❌ VIP-лайки не поставлены!")
                if message_id:
                    delete_message(peer_id, message_id)
                send_message(peer_id, vip_text)
                return
    
    # Проверка лайков на предыдущие 10 ссылок
    with queue_lock:
        links_to_check = [item['link'] for item in queue[-10:]]
    if links_to_check:
        all_liked = True
        missing_links = []
        for link in links_to_check:
            has_like = check_user_like(user_id, link)
            if not has_like:
                all_liked = False
                missing_links.append(link)
        if not all_liked:
            missing_text = "❌ Поставь лайки на эти ссылки:\n\n"
            for i, link in enumerate(missing_links, 1):
                clickable = make_clickable_link(link)
                missing_text += f"{i}. 🔗 {clickable}\n"
            print(f"❌ Не все лайки поставлены!")
            if message_id:
                delete_message(peer_id, message_id)
            send_message(peer_id, missing_text)
            return
    
    # Проверка частоты
    if not can_user_post(user_id):
        posts_after = get_posts_after_user(user_id)
        need_to_wait = max(0, 5 - posts_after)
        print(f"⏳ Пользователь {user_id} должен ждать {need_to_wait} постов")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"⏳ Подожди, нужно {need_to_wait} чужих постов!")
        return
    
    # Публикуем
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now()})
        if len(queue) > MAX_QUEUE_SIZE:
            removed = queue.pop(0)
            print(f"🗑️ Удалена старая ссылка: {removed['link']}")
        save_queue()
    clickable = make_clickable_link(vk_link)
    print(f"✅ Опубликовано!")
    send_message(peer_id, f"✅ Ссылка опубликована!\n🔗 {clickable}\n📊 В очереди: {len(queue)}")

def main():
    global vk
    init_database()
    load_data()
    try:
        print("🔄 Подключение к VK API...")
        sys.stdout.flush()
        vk_session = vk_api.VkApi(token=GROUP_TOKEN)
        vk = vk_session.get_api()
        print("✅ VK API подключен")
        sys.stdout.flush()
        print("🔄 Подключение Long Poll...")
        sys.stdout.flush()
        longpoll = VkBotLongPoll(vk_session, GROUP_ID)
        print("✅ Бот запущен!")
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
