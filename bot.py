import sys
import re
import time
import sqlite3
import json
import os
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple
from http.server import HTTPServer, BaseHTTPRequestHandler

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ (Callback API + Reactions + Auto-Delete)...")
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

GROUP_TOKEN = "vk1.a.jZntWDtqu6rH1vOzLdQYx2cscCiR5Vd5Iw_enxlYXfDnhv_xMnid8zXyjVPmVZ_ZUyH68EmAGplxiyOInoZuZ577wvgabPxo7-zeeKDJoZ3VLxp3QfMEck3LRbQsqj5BTjIXs1i68q9_fpph0W6Dvh24Z2DCSbDz-t_nsjrojFOzWjOSdc479mrwY7y0gzTcpGWIkX6aMUHGEAsMZ0poBA"
GROUP_ID = 241064421
CONFIRMATION_CODE = "60a08f28"
PORT = int(os.getenv("PORT", 3000))
ADMIN_IDS = [447457340]
DELETE_AFTER = 300  # 5 минут

MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24  # VIP-ссылки на 24 часа
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"

queue = []
queue_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()
vk = None

user_states = {}
states_lock = threading.Lock()

processed_events = set()
processed_lock = threading.Lock()

# Для удаления сообщений бота
bot_messages = {}  # {peer_id: [random_ids]}
bot_messages_lock = threading.Lock()

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

def cleanup_expired_vip():
    """Очистка просроченных VIP-ссылок"""
    global vip_links
    with vip_links_lock:
        now = datetime.now()
        vip_links = [v for v in vip_links if v['expires_at'] > now]
        save_vip_links()

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

def send_message(peer_id: int, text: str):
    """Отправка сообщения с авто-удалением (работает даже при ID=0)"""
    global vk, bot_messages
    if vk is None:
        return None
    
    random_id = int(time.time() * 1000)
    
    try:
        rate_limit()
        result = vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
        print(f"✅ Отправлено (ID: {result}): {text[:50]}...")
        
        # Сохраняем random_id для удаления
        with bot_messages_lock:
            if peer_id not in bot_messages:
                bot_messages[peer_id] = []
            bot_messages[peer_id].append({'random_id': random_id, 'text': text[:30]})
        
        # Планируем удаление
        def delete_later():
            time.sleep(DELETE_AFTER)
            try:
                rate_limit()
                
                # Способ 1: если result > 0
                if result and result > 0:
                    if peer_id >= 2000000000:
                        vk.messages.delete(peer_id=peer_id, cmids=[result], delete_for_all=True)
                    else:
                        vk.messages.delete(peer_id=peer_id, message_ids=[result], delete_for_all=True)
                    print(f"🗑️ Удалено по ID: {result}")
                    return
                
                # Способ 2: поиск по random_id в истории
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
                            print(f"🗑️ Удалено через random_id: {msg_id}")
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
    cleanup_expired_vip()
    
    if not is_admin(user_id):
        send_message(peer_id, "❌ Только администраторы!")
        return True
    if text.lower().startswith('!vip '):
        vk_link = extract_vk_link(text.split()[1])
        if vk_link:
            with vip_links_lock:
                for vip in vip_links:
                    if vip['link'] == vk_link:
                        send_message(peer_id, f"⚠️ Ссылка уже в VIP!")
                        return True
                vip_links.append({'link': vk_link, 'added_by': user_id, 'expires_at': datetime.now() + timedelta(hours=VIP_DURATION_HOURS)})
                save_vip_links()
            send_message(peer_id, f"⭐ VIP-ссылка добавлена на 24 часа!\n🔗 {make_clickable_link(vk_link)}")
        return True
    if text.lower().startswith('!delvip'):
        parts = text.split()
        if len(parts) >= 2:
            with vip_links_lock:
                vip_links = [v for v in vip_links if v['link'] != parts[1]]
                save_vip_links()
            send_message(peer_id, "✅ VIP-ссылка удалена!")
        return True
    if text.lower() == '!vip_list':
        cleanup_expired_vip()
        with vip_links_lock:
            if not vip_links:
                send_message(peer_id, "📭 VIP-ссылок нет")
                return True
            text = "⭐ VIP-ссылки:\n\n"
            now = datetime.now()
            for vip in vip_links:
                remaining = vip['expires_at'] - now
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                text += f"🔗 {make_clickable_link(vip['link'])}\n⏳ Осталось: {hours}ч {minutes}мин\n\n"
            send_message(peer_id, text)
        return True
    return False

def process_message(peer_id: int, user_id: int, text: str, message_id: int, event_id: str = ""):
    global queue
    
    with processed_lock:
        if event_id and event_id in processed_events:
            return
        if event_id:
            processed_events.add(event_id)
    
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
    
    cleanup_expired_vip()
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

def process_reaction(peer_id: int, user_id: int, cmid: int):
    print(f"🔔 Реакция от {user_id} на cmid={cmid}")
    
    with states_lock:
        state_data = user_states.get(user_id)
    
    if not state_data:
        print(f"   ⚠️ Нет состояния для {user_id}")
        return
    
    print(f"   bot_msg_id={state_data['bot_msg_id']}, cmid={cmid}")
    
    if state_data['bot_msg_id'] and cmid == state_data['bot_msg_id']:
        vk_link = state_data['link']
        
        if state_data['state'] == 'awaiting_regular':
            cleanup_expired_vip()
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
    else:
        print(f"   ❌ cmid не совпадает")

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
        
        print(f"📥 POST: {body.decode('utf-8')}", flush=True)
        
        try:
            data = json.loads(body)
            
            if data.get('type') == 'confirmation':
                rb = CONFIRMATION_CODE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
                print(f"🔑 Код подтверждения отправлен", flush=True)
            
            elif data.get('type') == 'message_new':
                msg = data.get('object', {}).get('message', {})
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
            
            elif data.get('type') == 'message_reaction':
                obj = data.get('object', {})
                peer_id = obj.get('peer_id', 0)
                user_id = obj.get('user_id', 0)
                cmid = obj.get('cmid', 0)
                
                thread = threading.Thread(target=process_reaction, args=(peer_id, user_id, cmid), daemon=True)
                thread.start()
                
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
    
    vk_session = vk_api.VkApi(token=GROUP_TOKEN)
    vk = vk_session.get_api()
    print("✅ VK API подключен")
    
    print(f"📡 Порт: {PORT}")
    print(f"🔑 Код: {CONFIRMATION_CODE}")
    print(f"🗑️ Авто-удаление: {DELETE_AFTER} секунд (5 минут)")
    print(f"⭐ VIP-ссылки на {VIP_DURATION_HOURS} часа")
    sys.stdout.flush()
    
    server = HTTPServer(('0.0.0.0', PORT), CallbackHandler)
    print(f"✅ Сервер запущен на 0.0.0.0:{PORT}")
    sys.stdout.flush()
    server.serve_forever()
