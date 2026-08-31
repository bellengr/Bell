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

GROUP_TOKEN = "vk1.a.jZntWDtqu6rH1vOzLdQYx2cscCiR5Vd5Iw_enxlYXfDnhv_xMnid8zXyjVPmVZ_ZUyH68EmAGplxiyOInoZuZ577wvgabPxo7-zeeKDJoZ3VLxp3QfMEck3LRbQsqj5BTjIXs1i68q9_fpph0W6Dvh24Z2DCSbDz-t_nsjrojFOzWjOSdc479mrwY7y0gzTcpGWIkX6aMUHGEAsMZ0poBA"
USER_TOKEN = "vk1.a.D_SMkct4GN-Ul6v3iXU95eEIv78-QcxXKctIa0TihI4W3WJhpQ1TJGk8qVelJy-Krh6XiyNZP1dpE-53gxRgSwXANX5PbdQM1kkCPyBF5f2K9XffciXOcuQI-4RQno3n8sJbV_uR3czl_Xx-LjTM6XDG6bLCCnnHq1fQVAbQ51oZYwFNHFQXTuhCrBm7MKF-eSDHepS0xZY1AbYcFFN2nw"
GROUP_ID = 241064421
CONFIRMATION_CODE = "60a08f28"
PORT = int(os.getenv("PORT", 3000))
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
vk_group = None
vk_user = None

user_activity = {}
activity_lock = threading.Lock()

greeting_timer = None
greeting_lock = threading.Lock()

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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id INTEGER PRIMARY KEY,
                last_post_time TEXT,
                post_count INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
    sys.stdout.flush()

def load_data():
    global queue, vip_links, user_activity
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
        
        cursor.execute('SELECT user_id, last_post_time, post_count FROM user_activity')
        for row in cursor.fetchall():
            user_activity[row[0]] = {
                'last_post_time': datetime.fromisoformat(row[1]) if row[1] else None,
                'post_count': row[2]
            }
        
        conn.close()
        print(f"📂 Загружено: {len(queue)} ссылок, {len(vip_links)} VIP")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки: {e}")
    sys.stdout.flush()

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
    patterns = [r'(wall-?\d+_\d+)', r'(photo-?\d+_\d+)', r'(video-?\d+_\d+)', r'(clip-?\d+_\d+)', r'(audio-?\d+_\d+)', r'(topic-?\d+_\d+)', r'(market-?\d+_\d+)', r'(album-?\d+_\d+)', r'(poll-?\d+_\d+)', r'(note-?\d+_\d+)', r'(doc-?\d+_\d+)']
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
    """Отправка сообщения с авто-удалением через 5 минут"""
    global vk_group
    if vk_group is None:
        return None
    try:
        rate_limit()
        random_id = int(time.time() * 1000)
        result = vk_group.messages.send(peer_id=peer_id, message=text, random_id=random_id)
        print(f"✅ Отправлено (random_id: {random_id}): {text[:50]}...")
        
        def delete_later():
            time.sleep(DELETE_AFTER)
            try:
                rate_limit()
                history = vk_group.messages.getHistory(peer_id=peer_id, count=30)
                items = history.get('items', [])
                for msg in items:
                    if msg.get('random_id') == random_id:
                        msg_id = msg.get('conversation_message_id', msg.get('id', 0))
                        if msg_id:
                            if peer_id >= 2000000000:
                                vk_group.messages.delete(peer_id=peer_id, cmids=[msg_id], delete_for_all=True)
                            else:
                                vk_group.messages.delete(peer_id=peer_id, message_ids=[msg_id], delete_for_all=True)
                            print(f"🗑️ Удалено сообщение бота: {msg_id}")
                            return
                print(f"⚠️ Сообщение не найдено для удаления")
            except Exception as e:
                print(f"⚠️ Ошибка удаления: {e}")
        
        thread = threading.Thread(target=delete_later, daemon=True)
        thread.start()
        
        return result
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None
    sys.stdout.flush()

def delete_message(peer_id: int, message_id: int) -> bool:
    global vk_group
    if vk_group is None or not message_id:
        return False
    try:
        rate_limit()
        if peer_id >= 2000000000:
            vk_group.messages.delete(peer_id=peer_id, cmids=[message_id], delete_for_all=True)
        else:
            vk_group.messages.delete(peer_id=peer_id, message_ids=[message_id], delete_for_all=True)
        return True
    except:
        return False

def schedule_greeting(peer_id: int):
    global greeting_timer
    def send_greeting():
        time.sleep(3)
        text = "👋 Приветствуем Вас в чате B E L L | like!\n\n"
        text += "Это чат по взаимным лайкам на Ваши посты.\n"
        text += "Перед отправкой ссылки в чат - прочитайте закрепленное сообщение, это очень важно!"
        send_message(peer_id, text)
    
    with greeting_lock:
        if greeting_timer and greeting_timer.is_alive():
            greeting_timer.cancel()
        greeting_timer = threading.Thread(target=send_greeting, daemon=True)
        greeting_timer.start()

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
    global vip_links
    cleanup_expired_vip()
    
    if not is_admin(user_id):
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "❌ Только администраторы!")
        return True
    
    if text.lower().startswith('!vip '):
        vk_link = extract_vk_link(text.split()[1])
        if vk_link:
            with vip_links_lock:
                for vip in vip_links:
                    if vip['link'] == vk_link:
                        if message_id:
                            delete_message(peer_id, message_id)
                        send_message(peer_id, f"⚠️ Ссылка уже в VIP!")
                        return True
                vip_links.append({'link': vk_link, 'added_by': user_id, 'expires_at': datetime.now() + timedelta(hours=VIP_DURATION_HOURS)})
                save_vip_links()
            if message_id:
                delete_message(peer_id, message_id)
            send_message(peer_id, f"⭐ VIP-ссылка добавлена на 24 часа!\n🔗 {make_clickable_link(vk_link)}")
        return True
    
    if text.lower().startswith('!delvip'):
        parts = text.split()
        if len(parts) >= 2:
            with vip_links_lock:
                vip_links = [v for v in vip_links if v['link'] != parts[1]]
                save_vip_links()
            if message_id:
                delete_message(peer_id, message_id)
            send_message(peer_id, "✅ VIP-ссылка удалена!")
        return True
    
    if text.lower() == '!vip_list':
        with vip_links_lock:
            if not vip_links:
                if message_id:
                    delete_message(peer_id, message_id)
                send_message(peer_id, "📭 VIP-ссылок нет")
                return True
            text = "⭐ VIP-ссылки:\n\n"
            now = datetime.now()
            for vip in vip_links:
                remaining = vip['expires_at'] - now
                hours = int(remaining.total_seconds() // 3600)
                text += f"🔗 {make_clickable_link(vip['link'])}\n⏳ Осталось: {hours}ч\n\n"
            if message_id:
                delete_message(peer_id, message_id)
            send_message(peer_id, text)
        return True
    
    if text.lower() == '!inactive':
        if message_id:
            delete_message(peer_id, message_id)
        inactive_text = get_inactive_users(peer_id)
        send_message(peer_id, inactive_text)
        return True
    
    return False

def process_message(peer_id: int, user_id: int, text: str, message_id: int, event_id: str = ""):
    global queue
    
    print(f"\n📩 {user_id}: {text[:50]}")
    sys.stdout.flush()
    
    if user_id < 0:
        return
    
    admin = is_admin(user_id)
    
    # Команды админа
    if text.lower().startswith('!vip') or text.lower().startswith('!delvip') or text.lower() == '!inactive':
        handle_vip_commands(text, user_id, peer_id, message_id)
        return
    
    vk_link = extract_vk_link(text)
    
    # Если нет ссылки
    if not vk_link:
        if admin:
            return
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Сообщение должно содержать только ссылку на контент!")
        return
    
    # Проверяем, что сообщение содержит ТОЛЬКО ссылку
    if text != vk_link and not text.startswith('https://vk.com/') and not text.startswith('https://vk.ru/') and not text.startswith('http://vk.com/') and not text.startswith('http://vk.ru/'):
        if admin:
            return
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Сообщение должно содержать ТОЛЬКО ссылку на контент!")
        return
    
    if admin:
        with queue_lock:
            queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 1})
            if len(queue) > MAX_QUEUE_SIZE:
                queue.pop(0)
            save_queue()
        send_message(peer_id, f"✅ Ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}")
        return
    
    if not can_user_post(user_id):
        need = max(0, 5 - get_posts_after_user(user_id))
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"⏳ Ждем Вас через {need} ссылок!")
        return
    
    # Проверка VIP-лайков
    cleanup_expired_vip()
    with vip_links_lock:
        if vip_links:
            missing_vip = []
            for vip in vip_links:
                if not check_user_like(user_id, vip['link']):
                    missing_vip.append(vip['link'])
            
            if missing_vip:
                if message_id:
                    delete_message(peer_id, message_id)
                text = "⭐ Обязательно проставь лайки на VIP ссылки:\n\n"
                for link in missing_vip:
                    text += f"⭐ {make_clickable_link(link)}\n"
                text += f"\n{'─' * 30}\n"
                text += "⏳ На выполнение даётся 5 минут!\n"
                text += "✅ После того, как поставишь лайки, отправь свою ссылку снова.\n\n"
                text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
                send_message(peer_id, text)
                return
    
    # Проверка лайков на очередь
    with queue_lock:
        regular_links = [item['link'] for item in queue[-10:]]
    
    if regular_links:
        missing_regular = []
        for link in regular_links:
            if not check_user_like(user_id, link):
                missing_regular.append(link)
        
        if missing_regular:
            if message_id:
                delete_message(peer_id, message_id)
            text = "📋 Обязательно проставь лайки на предыдущие 10 ссылок:\n\n"
            for link in missing_regular:
                text += f"▫️ {make_clickable_link(link)}\n"
            text += f"\n{'─' * 30}\n"
            text += "⏳ На выполнение даётся 5 минут!\n"
            text += "✅ После того, как поставишь лайки, отправь свою ссылку снова.\n\n"
            text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
            send_message(peer_id, text)
            return
    
    # Публикация — НЕ удаляем сообщение участника!
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 0})
        if len(queue) > MAX_QUEUE_SIZE:
            queue.pop(0)
        save_queue()
    
    # Сохраняем активность
    with activity_lock:
        user_activity[user_id] = {
            'last_post_time': datetime.now(),
            'post_count': user_activity.get(user_id, {}).get('post_count', 0) + 1
        }
    save_user_activity(user_id)
    
    text = f"✅ Ваша ссылка опубликована!\n🔗 {make_clickable_link(vk_link)}\n📊 В очереди: {len(queue)}\n\n"
    text += "⏳ Ждем Вас через 5 ссылок!\n\n"
    text += "💎 Хочешь себе статус VIP? Обращайся к администратору: @bellengr"
    send_message(peer_id, text)
    print(f"   ✅ Опубликовано!")

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
            
            if event_type == 'confirmation':
                rb = CONFIRMATION_CODE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(rb)))
                self.end_headers()
                self.wfile.write(rb)
            
            elif event_type == 'message_new':
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
            
            elif event_type == 'chat_invite_user':
                obj = data.get('object', {})
                peer_id = obj.get('peer_id', 0)
                schedule_greeting(peer_id)
                
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
    print("✅ Групповой API подключен")
    
    vk_user_session = vk_api.VkApi(token=USER_TOKEN)
    vk_user = vk_user_session.get_api()
    print("✅ Пользовательский API подключен")
    
    print(f"📡 Порт: {PORT}")
    sys.stdout.flush()
    
    server = HTTPServer(('0.0.0.0', PORT), CallbackHandler)
    print(f"✅ Сервер запущен на 0.0.0.0:{PORT}")
    sys.stdout.flush()
    server.serve_forever()
