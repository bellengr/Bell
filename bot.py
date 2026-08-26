import sys
import os
import re
import time
import json
from datetime import datetime, timedelta
import threading
import traceback

print("=" * 60)
print("🚀 БОТ ЗАПУСКАЕТСЯ...")
print("=" * 60)
sys.stdout.flush()

try:
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
    print("✅ Библиотека vk-api загружена")
    sys.stdout.flush()
except ImportError as e:
    print(f"❌ Ошибка импорта vk-api: {e}")
    sys.stdout.flush()
    raise

# ====================== НАСТРОЙКИ ======================
TOKEN = "vk1.a.s5mgEVHWOVgpTPQ2AhN4hYF15Tc6vsHIsmavsZNDFTZkvKB-mwOR-f1aUuQ27AWpc5wfZLZH42iJy74xZDafcBZJwzmupX8OUN8MnlDxZYuHLk5NrJHDwIUuFiDy6S8OTbl0trJEUg77amTmVsgZPypu-EkumFvDiQFIkMt3twuGQD2PpnckpaASfFXLw0HMxp3CbBTZsLy1DEilvoJRbA"          # Замените на ваш токен
GROUP_ID = 241064421                 # ВАШ ID ГРУППЫ
# ======================================================

print(f"✅ Токен загружен: {TOKEN[:15]}...")
print(f"✅ ID группы: {GROUP_ID}")
sys.stdout.flush()

MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24
BOT_MESSAGE_DELAY = 40

queue = []
pending_links = {}
bot_messages = {}
vip_links = []

VIP_FILE = "vip_links.json"

pending_new_members = {}
greeting_timers = {}

try:
    print("🔄 Подключение к VK API...")
    sys.stdout.flush()
    
    vk_session = vk_api.VkApi(token=TOKEN)
    vk = vk_session.get_api()
    
    print("✅ VK API подключен")
    sys.stdout.flush()
    
    print("🔄 Подключение Long Poll...")
    sys.stdout.flush()
    
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)
    
    print("✅ Long Poll подключен")
    sys.stdout.flush()
    
except Exception as e:
    print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ К VK:")
    print(f"   {e}")
    print(traceback.format_exc())
    sys.stdout.flush()
    raise


def load_vip_links():
    global vip_links
    try:
        with open(VIP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            vip_links = []
            for item in data:
                item['expires_at'] = datetime.fromisoformat(item['expires_at'])
                if item['expires_at'] > datetime.now():
                    vip_links.append(item)
        print(f"📂 Загружено {len(vip_links)} VIP-ссылок")
    except FileNotFoundError:
        vip_links = []
        print("📂 Файл VIP-ссылок не найден, создан новый")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки VIP-ссылок: {e}")
        vip_links = []
    sys.stdout.flush()


def save_vip_links():
    try:
        data = []
        for item in vip_links:
            data.append({
                'link': item['link'],
                'added_by': item['added_by'],
                'expires_at': item['expires_at'].isoformat()
            })
        with open(VIP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения VIP-ссылок: {e}")
    sys.stdout.flush()


def cleanup_vip_links():
    global vip_links
    now = datetime.now()
    old_count = len(vip_links)
    vip_links = [item for item in vip_links if item['expires_at'] > now]
    if len(vip_links) < old_count:
        print(f"🗑️ Удалено {old_count - len(vip_links)} просроченных VIP-ссылок")
        save_vip_links()
    sys.stdout.flush()


def schedule_vip_cleanup():
    def cleanup_loop():
        while True:
            time.sleep(3600)
            cleanup_vip_links()
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    print("🔄 Запущен планировщик очистки VIP-ссылок")
    sys.stdout.flush()


def clean_queue():
    global queue
    if len(queue) > MAX_QUEUE_SIZE:
        removed = queue[:-MAX_QUEUE_SIZE]
        queue = queue[-MAX_QUEUE_SIZE:]
        removed_links = [item['link'] for item in removed]
        for user_id in list(pending_links.keys()):
            pending_links[user_id] = [link for link in pending_links[user_id] if link not in removed_links]
            if not pending_links[user_id]:
                del pending_links[user_id]
        print(f"🧹 Очищено {len(removed)} старых ссылок. В очереди: {len(queue)}")
    sys.stdout.flush()


def extract_vk_link(text):
    if not text:
        return None
    
    patterns = [
        r'(wall)(-?\d+)_(\d+)',
        r'(clip)(-?\d+)_(\d+)',
        r'(video)(-?\d+)_(\d+)',
        r'(photo)(-?\d+)_(\d+)',
        r'(album)(-?\d+)_(\d+)',
        r'(poll)(-?\d+)_(\d+)',
        r'(topic)(-?\d+)_(\d+)',
        r'(note)(-?\d+)_(\d+)',
        r'(audio)(-?\d+)_(\d+)',
        r'(doc)(-?\d+)_(\d+)',
        r'(market)(-?\d+)_(\d+)',
        r'(app)(-?\d+)_(\d+)',
        r'(page)(-?\d+)_(\d+)',
        r'(event)(-?\d+)_(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def check_like(user_id, vk_link):
    if not vk_link or '_' not in vk_link:
        return False
    
    parts = vk_link.split('_')
    if len(parts) != 2:
        return False
    
    type_and_owner = parts[0]
    try:
        item_id = int(parts[1])
    except ValueError:
        return False
    
    content_type = 'post'
    owner_part = type_and_owner
    
    if type_and_owner.startswith('wall'):
        content_type = 'post'
        owner_part = type_and_owner.replace('wall', '')
    elif type_and_owner.startswith('photo'):
        content_type = 'photo'
        owner_part = type_and_owner.replace('photo', '')
    elif type_and_owner.startswith('video'):
        content_type = 'video'
        owner_part = type_and_owner.replace('video', '')
    elif type_and_owner.startswith('clip'):
        content_type = 'video'
        owner_part = type_and_owner.replace('clip', '')
    else:
        content_type = 'post'
        owner_part = type_and_owner
    
    try:
        owner_id = int(owner_part)
    except ValueError:
        return False
    
    try:
        response = vk.likes.isLiked(
            user_id=user_id,
            type=content_type,
            owner_id=owner_id,
            item_id=item_id
        )
        if isinstance(response, dict):
            return response.get('liked', 0) == 1
        return response == 1
    except Exception as e:
        print(f"Ошибка проверки лайка: {e}")
        sys.stdout.flush()
        return False


def check_previous_likes(user_id):
    links_to_check = [item['link'] for item in queue[-10:]]
    if not links_to_check:
        return True, []
    missing = []
    for link in links_to_check:
        if not check_like(user_id, link):
            missing.append(link)
    return len(missing) == 0, missing


def check_vip_likes(user_id):
    cleanup_vip_links()
    if not vip_links:
        return True, []
    missing = []
    for vip in vip_links:
        if not check_like(user_id, vip['link']):
            missing.append(vip['link'])
    return len(missing) == 0, missing


def can_user_post(user_id):
    last_index = -1
    for i, item in enumerate(queue):
        if item['user_id'] == user_id:
            last_index = i
    if last_index == -1:
        return True
    if last_index >= len(queue) - 1:
        return False
    return (len(queue) - last_index - 1) >= 5


def is_group_admin(user_id):
    try:
        response = vk.groups.getMembers(
            group_id=GROUP_ID,
            filter='managers',
            count=100
        )
        admins = response.get('items', [])
        return user_id in admins
    except Exception:
        return False


def is_chat_owner(peer_id, user_id):
    try:
        response = vk.messages.getConversations(
            peer_id=peer_id,
            count=1
        )
        items = response.get('items', [])
        if not items:
            return False
        chat = items[0]
        if 'chat_settings' in chat:
            owner_id = chat['chat_settings'].get('owner_id')
            return owner_id == user_id
        return False
    except Exception as e:
        print(f"⚠️ Ошибка проверки владельца чата: {e}")
        sys.stdout.flush()
        return False


def is_admin_or_owner(peer_id, user_id):
    if is_group_admin(user_id):
        return True
    if is_chat_owner(peer_id, user_id):
        return True
    return False


def send_message(peer_id, text, save_for_deletion=True):
    try:
        result = vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=int(time.time() * 1000)
        )
        print(f"✅ Отправлено сообщение в {peer_id}: {text[:30]}...")
        if save_for_deletion and result:
            if peer_id not in bot_messages:
                bot_messages[peer_id] = []
            bot_messages[peer_id].append(result)
            print(f"💾 Сохранено сообщение {result} для удаления")
        sys.stdout.flush()
        return result
    except Exception as e:
        print(f"❌ Ошибка отправки сообщения: {e}")
        sys.stdout.flush()
        return None


def delete_message_immediately(peer_id, message_id, user_id=None):
    if not message_id or not peer_id:
        return False
    
    if user_id and is_admin_or_owner(peer_id, user_id):
        print(f"👑 Администратор {user_id} — сообщение НЕ удалено.")
        sys.stdout.flush()
        return False
    
    try:
        vk.messages.delete(
            peer_id=peer_id,
            message_ids=[message_id],
            delete_for_all=True
        )
        print(f"🗑️ Сообщение {message_id} удалено")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"❌ Не удалось удалить {message_id}: {e}")
        sys.stdout.flush()
        return False


def delete_bot_messages_with_delay(peer_id, delay=BOT_MESSAGE_DELAY):
    if not peer_id:
        return
    
    if peer_id not in bot_messages or not bot_messages[peer_id]:
        print(f"ℹ️ Нет сообщений бота для удаления")
        sys.stdout.flush()
        return
    
    messages_to_delete = bot_messages[peer_id].copy()
    bot_messages[peer_id] = []
    
    if not messages_to_delete:
        return
    
    def delete_after_delay():
        print(f"⏳ Ожидание {delay} секунд...")
        sys.stdout.flush()
        time.sleep(delay)
        
        deleted_count = 0
        for msg_id in messages_to_delete:
            try:
                vk.messages.delete(
                    peer_id=peer_id,
                    message_ids=[msg_id],
                    delete_for_all=True
                )
                deleted_count += 1
                print(f"🗑️ Удалено сообщение бота {msg_id}")
            except Exception as e:
                error_msg = str(e).lower()
                if 'already deleted' in error_msg or 'not found' in error_msg:
                    print(f"ℹ️ Сообщение {msg_id} уже удалено")
                else:
                    print(f"❌ Ошибка удаления {msg_id}: {e}")
            sys.stdout.flush()
        
        print(f"🗑️ Удалено {deleted_count} сообщений бота")
        sys.stdout.flush()
    
    thread = threading.Thread(target=delete_after_delay, daemon=True)
    thread.start()
    print(f"🔄 Запущен таймер удаления {len(messages_to_delete)} сообщений через {delay} секунд")
    sys.stdout.flush()


def send_greeting(peer_id):
    if peer_id not in pending_new_members or not pending_new_members[peer_id]:
        return
    
    pending_new_members[peer_id] = []
    
    greeting = "👋 Привет! Перед публикацией своей ссылки обязательно прочитай закреп в чате. Обязательно!"
    
    send_message(peer_id, greeting, save_for_deletion=True)
    print(f"👋 Отправлено приветствие в беседу {peer_id}")
    sys.stdout.flush()


def schedule_greeting(peer_id, user_id):
    if peer_id not in pending_new_members:
        pending_new_members[peer_id] = []
    
    if user_id not in pending_new_members[peer_id]:
        pending_new_members[peer_id].append(user_id)
    
    if peer_id in greeting_timers and greeting_timers[peer_id].is_alive():
        return
    
    def delayed_greeting():
        time.sleep(3)
        send_greeting(peer_id)
        if peer_id in greeting_timers:
            del greeting_timers[peer_id]
    
    timer = threading.Thread(target=delayed_greeting, daemon=True)
    timer.start()
    greeting_timers[peer_id] = timer
    print(f"⏳ Запланировано приветствие для беседы {peer_id}")
    sys.stdout.flush()


def handle_new_member(peer_id, user_id):
    print(f"👋 Новый участник {user_id} в беседе {peer_id}")
    sys.stdout.flush()
    schedule_greeting(peer_id, user_id)


def handle_vip_commands(text, user_id, peer_id, message_id):
    global vip_links

    vip_match = re.match(r'^!vip\s+(\S+)', text, re.IGNORECASE)
    if vip_match:
        raw_link = vip_match.group(1)
        vk_link = extract_vk_link(raw_link)
        
        if not vk_link:
            send_message(peer_id, "❌ Не удалось распознать ссылку.")
            return True

        for vip in vip_links:
            if vip['link'] == vk_link:
                send_message(peer_id, f"⚠️ Ссылка {vk_link} уже есть в VIP-списке.")
                return True

        expires_at = datetime.now() + timedelta(hours=VIP_DURATION_HOURS)
        vip_links.append({
            'link': vk_link,
            'added_by': user_id,
            'expires_at': expires_at
        })
        save_vip_links()

        send_message(peer_id, f"⭐ VIP-ссылка {vk_link} добавлена!\n⏳ Действует до: {expires_at.strftime('%d.%m.%Y %H:%M')}")
        return True

    if text.lower().startswith('!delvip'):
        parts = text.split()
        if len(parts) < 2:
            send_message(peer_id, "❌ Формат: !delvip wall-123_456")
            return True

        if not is_group_admin(user_id):
            send_message(peer_id, "❌ Только администраторы группы могут удалять VIP-ссылки.")
            return True

        vk_link = parts[1]
        vip_to_remove = None
        for vip in vip_links:
            if vip['link'] == vk_link:
                vip_to_remove = vip
                break

        if not vip_to_remove:
            send_message(peer_id, f"⚠️ VIP-ссылка {vk_link} не найдена.")
            return True

        vip_links.remove(vip_to_remove)
        save_vip_links()
        send_message(peer_id, f"✅ VIP-ссылка {vk_link} удалена.")
        return True

    if text.lower() == '!vip_list':
        cleanup_vip_links()
        if not vip_links:
            send_message(peer_id, "📭 Активных VIP-ссылок нет.")
            return True

        vip_text = "⭐ Активные VIP-ссылки:\n"
        for i, vip in enumerate(vip_links, 1):
            remaining = vip['expires_at'] - datetime.now()
            hours = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            vip_text += f"{i}. {vip['link']} (осталось {hours}ч {minutes}мин)\n"

        send_message(peer_id, vip_text)
        return True

    return False


def process_message(event):
    print("📩 Получено новое сообщение")
    sys.stdout.flush()
    
    try:
        peer_id = event.object.message['peer_id']
        user_id = event.object.message['from_id']
        text = event.object.message.get('text', '').strip()
        message_id = event.object.message['id']
    except AttributeError as e:
        print(f"⚠️ Ошибка чтения сообщения: {e}")
        sys.stdout.flush()
        return

    print(f"   От: {user_id}")
    print(f"   Текст: {text[:50]}..." if text else "   Текст: (пусто)")
    print(f"   ID сообщения: {message_id}")
    print(f"   Беседа: {peer_id}")
    sys.stdout.flush()

    if user_id < 0:
        print("   ⚠️ Сообщение от бота, игнорируем")
        return

    # VIP-команды
    if text.startswith('!vip') or text.lower() == '!vip_list':
        handle_vip_commands(text, user_id, peer_id, message_id)
        return

    vk_link = extract_vk_link(text)
    
    # =========================================================
    # ЕСЛИ НЕТ ССЫЛКИ — УДАЛЯЕМ СООБЩЕНИЕ
    # =========================================================
    if not vk_link:
        if is_admin_or_owner(peer_id, user_id):
            print(f"👑 Администратор {user_id} — сообщение НЕ удалено.")
            return
        
        print(f"🗑️ Удаляем текстовое сообщение пользователя {user_id}")
        delete_message_immediately(peer_id, message_id, user_id)
        
        send_message(peer_id, "🔗 Для публикации нужна ссылка на контент ВКонтакте.\n📌 Поддерживаются: посты, клипы, видео, фото, альбомы.")
        return

    # =========================================================
    # ЕСТЬ ССЫЛКА — ПРОВЕРЯЕМ УСЛОВИЯ
    # =========================================================
    print(f"   ✅ Найдена ссылка: {vk_link}")
    sys.stdout.flush()

    # 1. VIP-лайки
    vip_ok, vip_missing = check_vip_likes(user_id)
    if not vip_ok:
        vip_text = "\n".join([f"⭐ {link}" for link in vip_missing])
        print(f"   ❌ VIP-лайки не выполнены")
        delete_message_immediately(peer_id, message_id, user_id)
        send_message(peer_id, f"⭐ Ты должен поставить лайки на ВСЕ VIP-ссылки:\n{vip_text}")
        return

    # 2. Очередь (раз в 5 постов)
    if not can_user_post(user_id):
        last_index = -1
        for i, item in enumerate(queue):
            if item['user_id'] == user_id:
                last_index = i
        posts_after = len(queue) - last_index - 1
        need_to_wait = max(0, 5 - posts_after)

        print(f"   ⏳ Очередь: нужно ждать {need_to_wait} постов")
        delete_message_immediately(peer_id, message_id, user_id)
        send_message(peer_id, f"⏳ Ты можешь отправить новую ссылку только после {need_to_wait} чужих постов.\n📊 Сейчас прошло {posts_after}.")
        return

    # 3. Лайки на предыдущие 10 ссылок
    all_liked, missing_links = check_previous_likes(user_id)
    if not all_liked:
        missing_text = "\n".join([f"📌 {link}" for link in missing_links])
        print(f"   ❌ Пропущены лайки на {len(missing_links)} ссылок")
        delete_message_immediately(peer_id, message_id, user_id)
        send_message(peer_id, f"❌ Ты пропустил лайки на эти ссылки:\n{missing_text}\n\n📌 Поставь лайки и отправь ссылку заново!")
        return

    # =========================================================
    # ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ — ПУБЛИКУЕМ ССЫЛКУ
    # =========================================================
    print(f"   ✅ Все условия выполнены! Публикуем ссылку")
    sys.stdout.flush()

    queue.append({
        'link': vk_link,
        'user_id': user_id,
        'timestamp': datetime.now()
    })

    clean_queue()

    # Удаляем старые сообщения бота
    delete_bot_messages_with_delay(peer_id, BOT_MESSAGE_DELAY)

    # Отправляем финальное сообщение
    send_message(peer_id, f"✅ Ссылка {vk_link} опубликована!\n📊 В очереди: {len(queue)} ссылок\n⏳ Ждём тебя через 5 ссылок!")


def handle_event(event):
    if event.type == VkBotEventType.MESSAGE_NEW:
        process_message(event)
    
    elif event.type == VkBotEventType.MESSAGE_EVENT:
        try:
            payload = event.object.payload
            if payload:
                print(f"📦 Событие: {payload}")
                sys.stdout.flush()
                
                if payload.get('type') == 'chat_invite_user':
                    user_id = payload.get('user_id')
                    peer_id = event.object.peer_id
                    if user_id and peer_id:
                        handle_new_member(peer_id, user_id)
        except Exception as e:
            print(f"⚠️ Ошибка обработки события: {e}")
            sys.stdout.flush()


load_vip_links()
schedule_vip_cleanup()

print("=" * 60)
print("🚀 Бот запущен и слушает сообщения...")
print(f"📌 ID группы: {GROUP_ID}")
print(f"📌 Максимальный размер очереди: {MAX_QUEUE_SIZE} ссылок")
print(f"⭐ Активных VIP-ссылок: {len(vip_links)}")
print(f"⏳ Сообщения бота удаляются через {BOT_MESSAGE_DELAY} секунд")
print("=" * 60)
print("⏳ Ожидание сообщений...")
print("=" * 60)
sys.stdout.flush()

try:
    for event in longpoll.listen():
        handle_event(event)
except Exception as e:
    print(f"❌ КРИТИЧЕСКАЯ ОШИБКА:")
    print(f"   {e}")
    print(traceback.format_exc())
    sys.stdout.flush()
    raise