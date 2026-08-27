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
        print(f"📂 Загружено: {len(queue)} ссылок в очереди, {len(vip_links)} VIP")
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

def check_like_wall(user_id: int, vk_link: str) -> bool:
    """
    Проверка лайка пользователя на ПОСТ через wall.get с filter='likes'
    """
    global vk
    if vk is None:
        return False
    
    if not vk_link.startswith('wall'):
        return True  # Пропускаем не-wall
    
    parts = vk_link.split('_')
    owner_id = int(parts[0][4:])
    item_id = int(parts[1])
    
    code = f'''
    var owner_id = {owner_id};
    var item_id = {item_id};
    var result = {{"found": 0, "total": 0}};
    
    var wall = API.wall.get({{
        "owner_id": owner_id,
        "count": 100,
        "filter": "likes"
    }});
    
    if (wall != null && wall.items != null) {{
        result.total = wall.items.length;
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
        print(f"   📦 Ответ: {response}")
        if isinstance(response, dict):
            found = response.get('found', 0)
            total = response.get('total', 0)
            print(f"   📊 {'✅ НАЙДЕН' if found == 1 else '❌ НЕ найден'} (всего лайкнутых: {total})")
            return found == 1
        return False
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
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
    print("\n" + "=" * 50)
    print("📩 НОВОЕ СООБЩЕНИЕ")
    print("=" * 50)
    
    try:
        message = event.object.message
        peer_id = message['peer_id']
        user_id = message['from_id']
        text = message.get('text', '').strip()
        message_id = message.get('conversation_message_id', message.get('id', 0))
    except Exception as e:
        print(f"⚠️ Ошибка чтения: {e}")
        return
    
    print(f"👤 Пользователь: {user_id}")
    print(f"💬 Текст: {text[:50] if text else '(пусто)'}")
    sys.stdout.flush()
    
    if user_id < 0:
        return
    
    # VIP-команды
    if text.lower().startswith('!vip') or text.lower().startswith('!delvip'):
        if message_id:
            delete_message(peer_id, message_id)
        handle_vip_commands(text, user_id, peer_id)
        return
    
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        print(f"🗑️ Нет ссылки — удаляем сообщение")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Нужна ссылка на контент ВКонтакте!")
        return
    
    print(f"✅ Ссылка: {vk_link}")
    
    # ПРОВЕРКА 1: VIP-лайки (по одному)
    with vip_links_lock:
        if vip_links:
            print(f"\n🔍 ПРОВЕРКА VIP-ЛАЙКОВ ({len(vip_links)} шт.):")
            for i, vip in enumerate(vip_links, 1):
                print(f"   [{i}/{len(vip_links)}] VIP: {vip['link']}")
                has_like = check_like_wall(user_id, vip['link'])
                if not has_like:
                    clickable = make_clickable_link(vip['link'])
                    print(f"   ❌ НЕТ ЛАЙКА на VIP: {vip['link']}")
                    if message_id:
                        delete_message(peer_id, message_id)
                    send_message(peer_id, f"⭐ Не хватает лайка на VIP-ссылку:\n🔗 {clickable}")
                    return
                else:
                    print(f"   ✅ Лайк есть")
            print(f"✅ Все VIP-лайки поставлены!")
    
    # ПРОВЕРКА 2: Лайки на последние 10 ссылок (по одному)
    with queue_lock:
        links_to_check = [item['link'] for item in queue[-10:]]
    
    if links_to_check:
        print(f"\n🔍 ПРОВЕРКА ЛАЙКОВ НА ОЧЕРЕДЬ ({len(links_to_check)} шт.):")
        for i, link in enumerate(links_to_check, 1):
            print(f"   [{i}/{len(links_to_check)}] {link}")
            has_like = check_like_wall(user_id, link)
            if not has_like:
                clickable = make_clickable_link(link)
                print(f"   ❌ НЕТ ЛАЙКА на: {link}")
                if message_id:
                    delete_message(peer_id, message_id)
                send_message(peer_id, f"❌ Не хватает лайка вот на эту ссылку:\n🔗 {clickable}")
                return
            else:
                print(f"   ✅ Лайк есть")
        print(f"✅ Все лайки на очередь поставлены!")
    
    # ПРОВЕРКА 3: Частота
    if not can_user_post(user_id):
        posts_after = get_posts_after_user(user_id)
        need_to_wait = max(0, 5 - posts_after)
        print(f"⏳ Нужно ждать {need_to_wait} постов")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"⏳ Подожди, нужно {need_to_wait} чужих постов!")
        return
    
    # ПУБЛИКАЦИЯ
    print(f"\n✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ — ПУБЛИКУЕМ!")
    
    # Удаляем сообщение пользователя
    if message_id:
        delete_message(peer_id, message_id)
    
    # Добавляем в очередь
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now()})
        if len(queue) > MAX_QUEUE_SIZE:
            removed = queue.pop(0)
            print(f"🗑️ Удалена старая ссылка: {removed['link']}")
        save_queue()
    
    clickable = make_clickable_link(vk_link)
    send_message(peer_id, f"✅ Ссылка опубликована!\n🔗 {clickable}\n📊 В очереди: {len(queue)}")
    print(f"✅ Опубликовано!")

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
        print("✅ Проверка лайков на ПОСТЫ (wall)")
        print("✅ VIP-ссылки (24 часа)")
        print("✅ Очередь: 10 ссылок")
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
