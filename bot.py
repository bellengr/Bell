import sys
import re
import time
import sqlite3
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ (Long Poll + Reactions + Auto-Delete)...")
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

GROUP_TOKEN = "vk1.a.jZntWDtqu6rH1vOzLdQYx2cscCiR5Vd5Iw_enxlYXfDnhv_xMnid8zXyjVPmVZ_ZUyH68EmAGplxiyOInoZuZ577wvgabPxo7-zeeKDJoZ3VLxp3QfMEck3LRbQsqj5BTjIXs1i68q9_fpph0W6Dvh24Z2DCSbDz-t_nsjrojFOzWjOSdc479mrwY7y0gzTcpGWIkX6aMUHGEAsMZ0poBA"
GROUP_ID = 241064421
ADMIN_IDS = [447457340]
DELETE_AFTER = 300

MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"

queue = []
queue_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()
vk = None

user_states = {}
states_lock = threading.Lock()

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
    patterns = [r'(wall-?\d+_\d+)', r'(photo-?\d+_\d+)', r'(video-?\d+_\d+)', r'(clip-?\d+_\d+)', r'(audio-?\d+_\d+)', r'(topic-?\d+_\d+)', r'(market-?\d+_\d+)']
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

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

def send_message(peer_id: int, text: str, delete_after: int = DELETE_AFTER):
    global vk
    if vk is None:
        return None
    try:
        rate_limit()
        random_id = int(time.time() * 1000)
        result = vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
        print(f"✅ Отправлено (ID: {result}): {text[:50]}...")
        
        if delete_after > 0:
            def delete_later():
                time.sleep(delete_after)
                try:
                    rate_limit()
                    
                    # Способ 1: по ID
                    if result and result > 0:
                        if peer_id >= 2000000000:
                            vk.messages.delete(peer_id=peer_id, cmids=[result], delete_for_all=True)
                        else:
                            vk.messages.delete(peer_id=peer_id, message_ids=[result], delete_for_all=True)
                        print(f"🗑️ Удалено по ID: {result}")
                        return
                    
                    # Способ 2: поиск в истории
                    history = vk.messages.getHistory(peer_id=peer_id, count=20)
                    items = history.get('items', [])
                    
                    for msg in items:
                        if msg.get('random_id') == random_id:
                            msg_id = msg.get('conversation_message_id', msg.get('id', 0))
                            if msg_id:
                                if peer_id >= 2000000000:
                                    vk.messages.delete(peer_id=peer_id, cmids=[msg_id], delete_for_all=True)
                                else:
                                    vk.messages.delete(peer_id=peer_id, message_ids=[msg_id], delete_for_all=True)
                                print(f"🗑️ Удалено через историю: {msg_id}")
                                return
                    
                    # Способ 3: последнее сообщение бота
                    for msg in items:
                        if msg.get('from_id', 0) < 0:
                            msg_id = msg.get('conversation_message_id', msg.get('id', 0))
                            if msg_id:
                                if peer_id >= 2000000000:
                                    vk.messages.delete(peer_id=peer_id, cmids=[msg_id], delete_for_all=True)
                                else:
                                    vk.messages.delete(peer_id=peer_id, message_ids=[msg_id], delete_for_all=True)
                                print(f"🗑️ Удалено последнее сообщение бота: {msg_id}")
                                return
                    
                    print(f"⚠️ Не найдено сообщение для удаления")
                    
                except Exception as e:
                    print(f"⚠️ Не удалось удалить: {e}")
            
            thread = threading.Thread(target=delete_later, daemon=True)
            thread.start()
        
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
    
    if text.lower().startswith('!vip'):
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
    
    if not can_user_post(user_id):
        need = max(0, 5 - get_posts_after_user(user_id))
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"⏳ Жди {need} постов!")
        return
    
    if message_id:
        delete_message(peer_id, message_id)
    
    with queue_lock:
        regular_links = [item['link'] for item in queue[-10:]]
    
    if regular_links:
        text = "📋 Обязательно проставь лайки на предыдущие 10 ссылок:\n\n"
        for i, link in enumerate(regular_links, 1):
            text += f"▫️ {make_clickable_link(link)}\n"
        text += f"\n{'─' * 30}\n"
        text += "⏳ На выполнение даётся 5 минут!\n"
        text += "✅ После того, как поставишь лайки, поставь ЛЮБУЮ реакцию на это сообщение и снова отправь свою ссылку.\n\n"
        text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
        
        bot_msg_id = send_message(peer_id, text)
        
        with states_lock:
            user_states[user_id] = {
                'state': 'awaiting_regular',
                'link': vk_link,
                'peer_id': peer_id,
                'bot_msg_id': bot_msg_id
            }
        return
    
    with vip_links_lock:
        if vip_links:
            text = "⭐ Теперь проставь лайки на VIP ссылки:\n\n"
            for vip in vip_links:
                text += f"⭐ {make_clickable_link(vip['link'])}\n"
            text += f"\n{'─' * 30}\n"
            text += "⏳ На выполнение даётся 5 минут!\n"
            text += "✅ После того, как поставишь лайки, поставь ЛЮБУЮ реакцию на это сообщение и снова отправь свою ссылку.\n\n"
            text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
            
            bot_msg_id = send_message(peer_id, text)
            
            with states_lock:
                user_states[user_id] = {
                    'state': 'awaiting_vip',
                    'link': vk_link,
                    'peer_id': peer_id,
                    'bot_msg_id': bot_msg_id
                }
            return
    
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 0})
        if len(queue) > MAX_QUEUE_SIZE:
            queue.pop(0)
        save_queue()
    
    text = f"✅ Ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}\n📊 В очереди: {len(queue)}\n\n"
    text += "⏳ Ждем Вас через 5 ссылок!\n\n"
    text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
    send_message(peer_id, text)

def process_reaction(peer_id: int, user_id: int, message_id: int):
    with states_lock:
        state_data = user_states.get(user_id)
    
    if not state_data:
        return
    
    if state_data['bot_msg_id'] and message_id == state_data['bot_msg_id']:
        vk_link = state_data['link']
        
        if state_data['state'] == 'awaiting_regular':
            with vip_links_lock:
                if vip_links:
                    text = "⭐ Теперь проставь лайки на VIP ссылки:\n\n"
                    for vip in vip_links:
                        text += f"⭐ {make_clickable_link(vip['link'])}\n"
                    text += f"\n{'─' * 30}\n"
                    text += "⏳ На выполнение даётся 5 минут!\n"
                    text += "✅ После того, как поставишь лайки, поставь ЛЮБУЮ реакцию на это сообщение и снова отправь свою ссылку.\n\n"
                    text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
                    
                    bot_msg_id = send_message(peer_id, text)
                    
                    user_states[user_id] = {
                        'state': 'awaiting_vip',
                        'link': vk_link,
                        'peer_id': peer_id,
                        'bot_msg_id': bot_msg_id
                    }
                    return
            
            with queue_lock:
                queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 0})
                if len(queue) > MAX_QUEUE_SIZE:
                    queue.pop(0)
                save_queue()
            
            text = f"✅ Ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}\n📊 В очереди: {len(queue)}\n\n"
            text += "⏳ Ждем Вас через 5 ссылок!\n\n"
            text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
            send_message(peer_id, text)
            
            del user_states[user_id]
        
        elif state_data['state'] == 'awaiting_vip':
            with queue_lock:
                queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 0})
                if len(queue) > MAX_QUEUE_SIZE:
                    queue.pop(0)
                save_queue()
            
            text = f"✅ Ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}\n📊 В очереди: {len(queue)}\n\n"
            text += "⏳ Ждем Вас через 5 ссылок!\n\n"
            text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
            send_message(peer_id, text)
            
            del user_states[user_id]

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
        print("✅ Реакции через MESSAGE_EVENT")
        print("✅ Авто-удаление через историю")
        sys.stdout.flush()
        
        for event in longpoll.listen():
            if event.type == VkBotEventType.MESSAGE_NEW:
                process_message(event)
            elif event.type == VkBotEventType.MESSAGE_EVENT:
                try:
                    payload = event.object.payload
                    if payload:
                        peer_id = event.object.peer_id
                        user_id = event.object.user_id
                        message_id = payload.get('cmid', payload.get('message_id', 0))
                        process_reaction(peer_id, user_id, message_id)
                        print(f"🔔 Реакция от {user_id} на сообщение {message_id}")
                except Exception as e:
                    print(f"⚠️ Ошибка реакции: {e}")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        traceback.print_exc()
        sys.stdout.flush()

if __name__ == "__main__":
    main()
