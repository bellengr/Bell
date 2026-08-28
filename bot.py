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
print("🚀 БОТ ЗАПУСКАЕТСЯ (Callback API)...")
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

# ====================== НАСТРОЙКИ ======================
GROUP_TOKEN = "vk1.a.yQ0EmzxYEcYJEoDfw_Fv3XxltfsHJkUWWkWJQfWe65HY_sWy5-4mufFdSk_VONZ6V5jrHsljtWMZm_UnKKsOIp7uhtMdSOCR-r0Qf8S3B5sYFMtgk2hBdo4IV_hgIzIDcJx8ZzSdEEBPC4cmMnIxrxBgtSUzw0x2xdydHTh5bsmhDaOWbRpo73wrT_bH7gbGLcpSv2v1S05IJnCsD5UrKw"
GROUP_ID = 241064421
GROUP_LINK = "https://vk.ru/bellbotgr"
CONFIRMATION_CODE = "b4f9f4a0"
PORT = int(os.getenv("PORT", 3000))
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

admin_cache = {}
admin_cache_lock = threading.Lock()
admin_cache_time = {}

member_cache = {}
member_cache_lock = threading.Lock()
member_cache_time = {}

greeting_timer = None
greeting_timer_lock = threading.Lock()

def make_clickable_link(vk_link: str) -> str:
    if not vk_link:
        return vk_link
    if vk_link.startswith('http'):
        return vk_link
    return f"https://vk.com/{vk_link}"

def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    global vk
    with admin_cache_lock:
        if user_id in admin_cache:
            cache_time = admin_cache_time.get(user_id, 0)
            if time.time() - cache_time < 300:
                return admin_cache[user_id]
    if vk is None:
        return False
    try:
        rate_limit()
        response = vk.groups.getMembers(group_id=GROUP_ID, filter='managers', count=1000)
        admins = response.get('items', [])
        is_admin = user_id in admins
        with admin_cache_lock:
            admin_cache[user_id] = is_admin
            admin_cache_time[user_id] = time.time()
        return is_admin
    except:
        return False

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
            queue.append({
                'link': row[0],
                'user_id': row[1],
                'timestamp': datetime.fromisoformat(row[2]),
                'is_admin_post': row[3] if len(row) > 3 else 0
            })
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

def is_group_member(user_id: int) -> bool:
    global vk
    with member_cache_lock:
        if user_id in member_cache:
            cache_time = member_cache_time.get(user_id, 0)
            if time.time() - cache_time < 300:
                return member_cache[user_id]
    if vk is None:
        return False
    try:
        rate_limit()
        response = vk.groups.isMember(group_id=GROUP_ID, user_id=user_id)
        is_member = response == 1
        with member_cache_lock:
            member_cache[user_id] = is_member
            member_cache_time[user_id] = time.time()
        return is_member
    except:
        return False

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
    elif type_and_owner.startswith('market'):
        return 'market', int(type_and_owner[6:]), item_id
    return '', 0, 0

def check_user_like(user_id: int, vk_link: str) -> bool:
    """Проверка: есть ли user_id в списке лайкнувших"""
    global vk
    if vk is None:
        return False
    
    content_type, owner_id, item_id = get_content_type(vk_link)
    if not content_type or owner_id == 0 or item_id == 0:
        return True  # Неподдерживаемый тип — пропускаем
    
    # Метод 1: likes.getList напрямую
    try:
        rate_limit()
        response = vk.likes.getList(
            type=content_type,
            owner_id=owner_id,
            item_id=item_id,
            count=1000,
            filter='likes'
        )
        users = response.get('items', [])
        has_like = user_id in users
        print(f"   📊 Лайк {'✅ НАЙДЕН' if has_like else '❌ НЕ найден'} (всего: {len(users)})")
        return has_like
    except ApiError as e:
        print(f"   ⚠️ API ошибка: {e}")
    except Exception as e:
        print(f"   ⚠️ Ошибка: {e}")
    
    # Метод 2: через execute
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
            found = response.get('found', 0)
            print(f"   📊 execute: {'✅ НАЙДЕН' if found == 1 else '❌ НЕ найден'}")
            return found == 1
        return False
    except Exception as e:
        print(f"   ❌ execute ошибка: {e}")
        return False

def can_user_post(user_id: int) -> bool:
    global queue
    with queue_lock:
        user_posts_indices = [i for i, item in enumerate(queue) if item['user_id'] == user_id and not item.get('is_admin_post', 0)]
        if not user_posts_indices:
            return True
        last_user_post_index = user_posts_indices[-1]
        posts_after = len(queue) - last_user_post_index - 1
        return posts_after >= 5

def get_posts_after_user(user_id: int) -> int:
    global queue
    with queue_lock:
        user_posts_indices = [i for i, item in enumerate(queue) if item['user_id'] == user_id and not item.get('is_admin_post', 0)]
        if not user_posts_indices:
            return 0
        last_user_post_index = user_posts_indices[-1]
        return len(queue) - last_user_post_index - 1

def send_message(peer_id: int, text: str, delete_after: int = 40):
    global vk
    if vk is None:
        return
    random_id = int(time.time() * 1000)
    try:
        rate_limit()
        result = vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
        print(f"✅ Отправлено (ID: {result}): {text[:50]}...")
        
        if delete_after > 0 and result and result > 0:
            def delete_later():
                time.sleep(delete_after)
                try:
                    rate_limit()
                    if peer_id >= 2000000000:
                        vk.messages.delete(peer_id=peer_id, cmids=[result], delete_for_all=True)
                    else:
                        vk.messages.delete(peer_id=peer_id, message_ids=[result], delete_for_all=True)
                    print(f"🗑️ Удалено сообщение бота {result}")
                except:
                    pass
            thread = threading.Thread(target=delete_later, daemon=True)
            thread.start()
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
    except:
        return False

def schedule_greeting(peer_id: int):
    global greeting_timer
    def send_greeting():
        time.sleep(10)
        send_message(peer_id, "👋 Привет! Перед публикацией своей ссылки обязательно прочитай закреп в чате. Обязательно!", delete_after=40)
    with greeting_timer_lock:
        if greeting_timer and greeting_timer.is_alive():
            greeting_timer.cancel()
        greeting_timer = threading.Thread(target=send_greeting, daemon=True)
        greeting_timer.start()

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

def process_message(peer_id: int, user_id: int, text: str, message_id: int):
    global queue
    print(f"\n📩 Сообщение от {user_id}: {text[:50] if text else '(пусто)'}")
    sys.stdout.flush()
    
    if user_id < 0:
        return
    
    admin = is_admin(user_id)
    print(f"   {'👑 Админ' if admin else '👤 Пользователь'}")
    
    if text.lower().startswith('!vip') or text.lower().startswith('!delvip'):
        if message_id:
            delete_message(peer_id, message_id)
        handle_vip_commands(text, user_id, peer_id)
        return
    
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        if admin:
            print(f"   👑 Админ — текст НЕ удаляем")
            return
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Нужна ссылка на контент ВКонтакте!")
        return
    
    print(f"   ✅ Ссылка: {vk_link}")
    
    if admin:
        print(f"   👑 Админ — публикуем без проверок")
        with queue_lock:
            queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 1})
            if len(queue) > MAX_QUEUE_SIZE:
                queue.pop(0)
            save_queue()
        clickable = make_clickable_link(vk_link)
        send_message(peer_id, f"✅ Ссылка опубликована!\n🔗 {clickable}\n📊 В очереди: {len(queue)}")
        return
    
    if not is_group_member(user_id):
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"❗ Для работы в чате нужно быть подписанным на это сообщество:\n\n🔗 {GROUP_LINK}")
        return
    
    # VIP-лайки
    with vip_links_lock:
        if vip_links:
            print(f"   🔍 Проверка VIP-лайков...")
            for vip in vip_links:
                if not check_user_like(user_id, vip['link']):
                    clickable = make_clickable_link(vip['link'])
                    if message_id:
                        delete_message(peer_id, message_id)
                    send_message(peer_id, f"⭐ Не хватает лайка на VIP-ссылку:\n🔗 {clickable}")
                    return
    
    # Лайки на очередь
    with queue_lock:
        links_to_check = [item['link'] for item in queue[-10:]]
    if links_to_check:
        print(f"   🔍 Проверка лайков на очередь...")
        for link in links_to_check:
            if not check_user_like(user_id, link):
                clickable = make_clickable_link(link)
                if message_id:
                    delete_message(peer_id, message_id)
                send_message(peer_id, f"❌ Не хватает лайка на:\n🔗 {clickable}")
                return
    
    # Частота
    if not can_user_post(user_id):
        posts_after = get_posts_after_user(user_id)
        need_to_wait = max(0, 5 - posts_after)
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, f"⏳ Подожди, нужно {need_to_wait} чужих постов!")
        return
    
    # Публикация
    if message_id:
        delete_message(peer_id, message_id)
    
    with queue_lock:
        queue.append({'link': vk_link, 'user_id': user_id, 'timestamp': datetime.now(), 'is_admin_post': 0})
        if len(queue) > MAX_QUEUE_SIZE:
            queue.pop(0)
        save_queue()
    
    clickable = make_clickable_link(vk_link)
    send_message(peer_id, f"✅ Ссылка опубликована!\n🔗 {clickable}\n📊 В очереди: {len(queue)}")

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/callback' or self.path == '/':
            response_body = CONFIRMATION_CODE.encode('utf-8')
        else:
            response_body = b'Bot is running'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)
    
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            
            if data.get('type') == 'confirmation':
                response_body = CONFIRMATION_CODE.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
                print(f"🔑 Код подтверждения отправлен", flush=True)
            
            elif data.get('type') == 'message_new':
                message = data.get('object', {}).get('message', {})
                peer_id = message.get('peer_id', 0)
                user_id = message.get('from_id', 0)
                text = message.get('text', '')
                message_id = message.get('conversation_message_id', message.get('id', 0))
                
                thread = threading.Thread(target=process_message, args=(peer_id, user_id, text, message_id), daemon=True)
                thread.start()
                
                response_body = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
            
            else:
                response_body = b'ok'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
                
        except Exception as e:
            print(f"❌ Ошибка: {e}", flush=True)
            response_body = b'ok'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
    
    def log_message(self, format, *args):
        print(f"📝 {format % args}", flush=True)

if __name__ == "__main__":
    init_database()
    load_data()
    
    print("=" * 60)
    print("🚀 Бот запущен с Callback API")
    print(f"📌 ID группы: {GROUP_ID}")
    print(f"🔗 Сообщество: {GROUP_LINK}")
    print(f"🔑 Код: {CONFIRMATION_CODE}")
    print(f"📡 Порт: {PORT}")
    print("=" * 60)
    sys.stdout.flush()
    
    vk_session = vk_api.VkApi(token=GROUP_TOKEN)
    vk = vk_session.get_api()
    print("✅ VK API подключен")
    sys.stdout.flush()
    
    server = HTTPServer(('0.0.0.0', PORT), CallbackHandler)
    print(f"✅ Сервер запущен на 0.0.0.0:{PORT}")
    sys.stdout.flush()
    server.serve_forever()
