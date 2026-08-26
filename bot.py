import sys
import re
import time
import json
import sqlite3
from datetime import datetime, timedelta
import threading
import traceback
from typing import Optional, List, Tuple
from flask import Flask, request, Response

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ (Callback API)...")
print("=" * 60)
sys.stdout.flush()

try:
    import vk_api
    from vk_api.exceptions import ApiError
    print("✅ Библиотека vk-api загружена")
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    raise

# ====================== НАСТРОЙКИ ======================
TOKEN = "vk1.a.s5mgEVHWOVgpTPQ2AhN4hYF15Tc6vsHIsmavsZNDFTZkvKB-mwOR-f1aUuQ27AWpc5wfZLZH42iJy74xZDafcBZJwzmupX8OUN8MnlDxZYuHLk5NrJHDwIUuFiDy6S8OTbl0trJEUg77amTmVsgZPypu-EkumFvDiQFIkMt3twuGQD2PpnckpaASfFXLw0HMxp3CbBTZsLy1DEilvoJRbA"
GROUP_ID = 241064421
CONFIRMATION_CODE = "134086a7"
# ======================================================

MAX_QUEUE_SIZE = 10
RATE_LIMIT_DELAY = 0.34
DB_FILE = "bot_database.db"

app = Flask(__name__)

queue = []
queue_lock = threading.Lock()
vip_links = []
vip_links_lock = threading.Lock()

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

def load_data():
    global queue, vip_links
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT link, user_id, timestamp FROM queue ORDER BY id DESC LIMIT ?', (MAX_QUEUE_SIZE,))
        rows = cursor.fetchall()
        queue = []
        for row in reversed(rows):
            queue.append({
                'link': row[0],
                'user_id': row[1],
                'timestamp': datetime.fromisoformat(row[2])
            })
        
        cursor.execute('SELECT link, added_by, expires_at FROM vip_links WHERE expires_at > ?', 
                      (datetime.now().isoformat(),))
        vip_rows = cursor.fetchall()
        vip_links = []
        for row in vip_rows:
            vip_links.append({
                'link': row[0],
                'added_by': row[1],
                'expires_at': datetime.fromisoformat(row[2])
            })
        
        conn.close()
        print(f"📂 Загружено: {len(queue)} ссылок, {len(vip_links)} VIP")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки: {e}")

def save_queue():
    global queue
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM queue')
        for item in queue:
            cursor.execute(
                'INSERT INTO queue (link, user_id, timestamp) VALUES (?, ?, ?)',
                (item['link'], item['user_id'], item['timestamp'].isoformat())
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения: {e}")

def rate_limit():
    time.sleep(RATE_LIMIT_DELAY)

def is_group_admin(user_id: int) -> bool:
    try:
        rate_limit()
        response = vk.groups.getMembers(
            group_id=GROUP_ID,
            filter='managers',
            count=1000
        )
        return user_id in response.get('items', [])
    except:
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
        if items and 'chat_settings' in items[0]:
            return items[0]['chat_settings'].get('owner_id')
        return None
    except:
        return None

def is_admin_or_owner(peer_id: int, user_id: int) -> bool:
    if is_group_admin(user_id):
        return True
    owner_id = get_chat_owner(peer_id)
    return owner_id == user_id

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

def check_like(user_id: int, vk_link: str) -> bool:
    content_type, owner_id, item_id = get_content_type(vk_link)
    
    if not content_type or owner_id == 0 or item_id == 0:
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
            return response.get('liked', 0) == 1
        return response == 1
    except:
        return True

def can_user_post(user_id: int) -> bool:
    global queue
    with queue_lock:
        user_posts = [i for i, item in enumerate(queue) if item['user_id'] == user_id]
        if not user_posts:
            return True
        last_post_index = user_posts[-1]
        posts_after = len(queue) - last_post_index - 1
        return posts_after >= 5

def send_message(peer_id: int, text: str) -> Optional[int]:
    try:
        rate_limit()
        result = vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=int(time.time() * 1000)
        )
        
        print(f"✅ Отправлено: {text[:50]}... ID: {result}")
        
        if result and result > 0:
            schedule_delete(peer_id, result, 40)
        
        return result
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None

def schedule_delete(peer_id: int, message_id: int, delay: int):
    def delete_after_delay():
        time.sleep(delay)
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
            print(f"🗑️ Удалено сообщение бота {message_id}")
        except Exception as e:
            print(f"❌ Ошибка удаления {message_id}: {e}")
    
    thread = threading.Thread(target=delete_after_delay, daemon=True)
    thread.start()

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
    except Exception as e:
        print(f"❌ Ошибка удаления: {e}")
        return False

def process_message(peer_id: int, user_id: int, text: str, message_id: int):
    global queue
    
    print(f"\n📩 Сообщение от {user_id}: {text[:50] if text else '(пусто)'}")
    
    if user_id < 0:
        return
    
    if is_admin_or_owner(peer_id, user_id):
        print(f"👑 Админ/владелец, игнорируем")
        return
    
    vk_link = extract_vk_link(text)
    
    if not vk_link:
        print(f"🗑️ Нет ссылки, удаляем")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "🔗 Нужна ссылка на контент ВКонтакте!")
        return
    
    print(f"✅ Ссылка: {vk_link}")
    
    all_liked = True
    with queue_lock:
        for item in queue[-10:]:
            if not check_like(user_id, item['link']):
                all_liked = False
                break
    
    if not all_liked:
        print(f"❌ Не все лайки")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "❌ Поставь лайки на предыдущие ссылки!")
        return
    
    if not can_user_post(user_id):
        print(f"⏳ Слишком часто")
        if message_id:
            delete_message(peer_id, message_id)
        send_message(peer_id, "⏳ Подожди, нужно 5 чужих постов!")
        return
    
    with queue_lock:
        queue.append({
            'link': vk_link,
            'user_id': user_id,
            'timestamp': datetime.now()
        })
        if len(queue) > MAX_QUEUE_SIZE:
            queue = queue[-MAX_QUEUE_SIZE:]
        save_queue()
    
    print(f"✅ Опубликовано!")
    send_message(peer_id, f"✅ Ссылка {vk_link} опубликована!\n📊 В очереди: {len(queue)}")

@app.route('/', methods=['POST'])
def callback():
    try:
        data = request.json
        print(f"📥 Получен запрос: {data.get('type', 'unknown')}", flush=True)
        
        # ВАЖНО: Для подтверждения возвращаем ТОЛЬКО строку
        if data.get('type') == 'confirmation':
            print(f"🔑 Возвращаю: {CONFIRMATION_CODE}", flush=True)
            return CONFIRMATION_CODE  # Просто строка, без Response!
        
        if data.get('type') == 'message_new':
            message = data.get('object', {}).get('message', {})
            
            peer_id = message.get('peer_id', 0)
            user_id = message.get('from_id', 0)
            text = message.get('text', '')
            message_id = message.get('conversation_message_id', message.get('id', 0))
            
            thread = threading.Thread(
                target=process_message,
                args=(peer_id, user_id, text, message_id),
                daemon=True
            )
            thread.start()
        
        return 'ok'
    except Exception as e:
        print(f"❌ Ошибка Callback: {e}", flush=True)
        return 'ok'

if __name__ == "__main__":
    init_database()
    load_data()
    
    print("=" * 60)
    print("🚀 Бот запущен с Callback API")
    print(f"📌 ID группы: {GROUP_ID}")
    print(f"🔑 Код: {CONFIRMATION_CODE}")
    print("=" * 60)
    sys.stdout.flush()
    
    # Порт 80!
    app.run(host='0.0.0.0', port=80, debug=False)
