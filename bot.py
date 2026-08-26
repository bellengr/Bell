import os
import re
import time
import json
from datetime import datetime, timedelta
import threading

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

TOKEN = os.environ.get('TOKEN')
GROUP_ID = int(os.environ.get('GROUP_ID', 0))

if not TOKEN:
    raise ValueError("Ошибка: переменная окружения TOKEN не найдена!")
if not GROUP_ID:
    raise ValueError("Ошибка: переменная окружения GROUP_ID не найдена!")

MAX_QUEUE_SIZE = 10
VIP_DURATION_HOURS = 24

queue = []
pending_links = {}
bot_messages = {}
vip_links = []

VIP_FILE = "vip_links.json"

vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)


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
        print(f"Загружено {len(vip_links)} VIP-ссылок")
    except FileNotFoundError:
        vip_links = []
        print("Файл VIP-ссылок не найден, создан новый")
    except Exception as e:
        print(f"Ошибка загрузки VIP-ссылок: {e}")
        vip_links = []


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
        print(f"Ошибка сохранения VIP-ссылок: {e}")


def cleanup_vip_links():
    global vip_links
    now = datetime.now()
    old_count = len(vip_links)
    vip_links = [item for item in vip_links if item['expires_at'] > now]
    if len(vip_links) < old_count:
        print(f"Удалено {old_count - len(vip_links)} просроченных VIP-ссылок")
        save_vip_links()


def schedule_vip_cleanup():
    def cleanup_loop():
        while True:
            time.sleep(3600)
            cleanup_vip_links()
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    print("Запущен планировщик очистки VIP-ссылок")


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
        print(f"Очищено {len(removed)} старых ссылок. В очереди: {len(queue)}")


def extract_wall_id(text):
    match = re.search(r'wall(-?\d+)_(\d+)', text)
    return match.group(0) if match else None


def check_post_exists(wall_id):
    parts = wall_id.replace('wall', '').split('_')
    owner_id = int(parts[0])
    post_id = int(parts[1])
    try:
        vk.wall.getById(posts=f"{owner_id}_{post_id}")
        return True
    except Exception:
        return False


def check_like(user_id, wall_id):
    parts = wall_id.replace('wall', '').split('_')
    owner_id = int(parts[0])
    item_id = int(parts[1])
    try:
        response = vk.likes.isLiked(
            user_id=user_id,
            type='post',
            owner_id=owner_id,
            item_id=item_id
        )
        if isinstance(response, dict):
            return response.get('liked', 0) == 1
        return response == 1
    except Exception as e:
        print(f"Ошибка проверки лайка: {e}")
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


def send_message(peer_id, text, save_for_deletion=False, user_id=None):
    try:
        result = vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=int(time.time() * 1000)
        )
        if save_for_deletion and user_id and user_id > 0:
            if user_id not in bot_messages:
                bot_messages[user_id] = []
            bot_messages[user_id].append(result)
        return result
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")
        return None


def delete_message(peer_id, message_id):
    if not message_id:
        return
    try:
        vk.messages.delete(
            peer_id=peer_id,
            message_ids=[message_id],
            delete_for_all=True
        )
    except Exception as e:
        print(f"Не удалось удалить сообщение {message_id}: {e}")


def delete_bot_messages(user_id, peer_id):
    if user_id in bot_messages:
        for msg_id in bot_messages[user_id]:
            delete_message(peer_id, msg_id)
        del bot_messages[user_id]
        print(f"Удалены сообщения для пользователя {user_id}")


def handle_vip_commands(text, user_id, peer_id, message_id):
    global vip_links

    vip_match = re.match(r'^!vip\s+(wall-?\d+_\d+)', text, re.IGNORECASE)
    if vip_match:
        wall_id = vip_match.group(1)

        if not check_post_exists(wall_id):
            send_message(
                peer_id,
                f"Пост {wall_id} не найден или закрыт.",
                save_for_deletion=True,
                user_id=user_id
            )
            return True

        for vip in vip_links:
            if vip['link'] == wall_id:
                send_message(
                    peer_id,
                    f"Ссылка {wall_id} уже есть в VIP-списке.",
                    save_for_deletion=True,
                    user_id=user_id
                )
                return True

        expires_at = datetime.now() + timedelta(hours=VIP_DURATION_HOURS)
        vip_links.append({
            'link': wall_id,
            'added_by': user_id,
            'expires_at': expires_at
        })
        save_vip_links()

        delete_message(peer_id, message_id)

        send_message(
            peer_id,
            f"VIP-ссылка {wall_id} добавлена!\n"
            f"Действует до: {expires_at.strftime('%d.%m.%Y %H:%M')}\n"
            f"Все участники обязаны поставить лайк на этот пост перед публикацией."
        )
        return True

    if text.lower().startswith('!delvip'):
        parts = text.split()
        if len(parts) < 2:
            send_message(
                peer_id,
                "Формат: !delvip wall-123_456",
                save_for_deletion=True,
                user_id=user_id
            )
            return True

        if not is_group_admin(user_id):
            send_message(
                peer_id,
                "Только администраторы группы могут удалять VIP-ссылки.",
                save_for_deletion=True,
                user_id=user_id
            )
            return True

        wall_id = parts[1]
        vip_to_remove = None
        for vip in vip_links:
            if vip['link'] == wall_id:
                vip_to_remove = vip
                break

        if not vip_to_remove:
            send_message(
                peer_id,
                f"VIP-ссылка {wall_id} не найдена.",
                save_for_deletion=True,
                user_id=user_id
            )
            return True

        vip_links.remove(vip_to_remove)
        save_vip_links()
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"VIP-ссылка {wall_id} удалена вручную."
        )
        return True

    if text.lower() == '!vip_list':
        cleanup_vip_links()
        if not vip_links:
            send_message(
                peer_id,
                "Активных VIP-ссылок нет."
            )
            return True

        vip_text = "Активные VIP-ссылки (на все нужно поставить лайк):\n"
        for i, vip in enumerate(vip_links, 1):
            remaining = vip['expires_at'] - datetime.now()
            hours = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            vip_text += f"{i}. {vip['link']} (осталось {hours}ч {minutes}мин)\n"

        send_message(peer_id, vip_text)
        return True

    return False


def process_message(event):
    peer_id = event.object.message['peer_id']
    user_id = event.object.message['from_id']
    text = event.object.message.get('text', '').strip()
    message_id = event.object.message['id']

    if user_id < 0:
        return

    if text.startswith('!vip') or text.lower() == '!vip_list':
        if handle_vip_commands(text, user_id, peer_id, message_id):
            return

    wall_id = extract_wall_id(text)

    if not wall_id:
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"@id{user_id}, для публикации нужна ссылка на открытый пост ВК.\nФормат: wall-123_456",
            save_for_deletion=True,
            user_id=user_id
        )
        return

    if not check_post_exists(wall_id):
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"@id{user_id}, пост {wall_id} не найден или закрыт.\nНужна ссылка на открытый пост ВК.",
            save_for_deletion=True,
            user_id=user_id
        )
        return

    vip_ok, vip_missing = check_vip_likes(user_id)
    if not vip_ok:
        pending_links[user_id] = vip_missing
        delete_message(peer_id, message_id)

        vip_text = "\n".join([f" {link}" for link in vip_missing])
        send_message(
            peer_id,
            f"@id{user_id}, ты должен поставить лайки на ВСЕ VIP-ссылки:\n{vip_text}\n\n"
            f"Это обязательно для публикации любой ссылки!",
            save_for_deletion=True,
            user_id=user_id
        )
        return

    if not can_user_post(user_id):
        last_index = -1
        for i, item in enumerate(queue):
            if item['user_id'] == user_id:
                last_index = i
        posts_after = len(queue) - last_index - 1
        need_to_wait = max(0, 5 - posts_after)

        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"@id{user_id}, ты можешь отправить новую ссылку только после {need_to_wait} чужих постов.\nСейчас после твоей последней ссылки прошло {posts_after} постов.",
            save_for_deletion=True,
            user_id=user_id
        )
        return

    all_liked, missing_links = check_previous_likes(user_id)

    if not all_liked:
        pending_links[user_id] = missing_links
        delete_message(peer_id, message_id)

        missing_text = "\n".join([f" {link}" for link in missing_links])
        send_message(
            peer_id,
            f"@id{user_id}, ты пропустил лайки на эти посты:\n{missing_text}\n\n"
            f"Поставь лайки на все пропущенные посты и отправь ссылку заново.\n"
            f"Твоё сообщение со ссылкой удалено до выполнения условий.",
            save_for_deletion=True,
            user_id=user_id
        )
        return

    queue.append({
        'link': wall_id,
        'user_id': user_id,
        'timestamp': datetime.now()
    })

    clean_queue()
    delete_bot_messages(user_id, peer_id)

    if user_id in pending_links:
        del pending_links[user_id]

    last_index = -1
    for i, item in enumerate(queue):
        if item['user_id'] == user_id:
            last_index = i

    if last_index < len(queue) - 1:
        posts_after = len(queue) - last_index - 1
        wait_for = max(0, 5 - posts_after)
        if wait_for == 0:
            extra_msg = "Ты уже можешь отправить следующую ссылку!"
        else:
            extra_msg = f"Осталось ждать {wait_for} чужих постов."
    else:
        extra_msg = "Твоя ссылка последняя в очереди. Жди чужих постов."

    send_message(
        peer_id,
        f"@id{user_id}, всё выполнено! Твоя ссылка принята.\n"
        f"В очереди: {len(queue)} постов (хранятся последние {MAX_QUEUE_SIZE})\n"
        f"{extra_msg}"
    )


load_vip_links()
schedule_vip_cleanup()

print("Бот запущен и слушает сообщения...")
print(f"ID группы: {GROUP_ID}")
print(f"Максимальный размер очереди: {MAX_QUEUE_SIZE} ссылок")
print(f"Активных VIP-ссылок: {len(vip_links)}")
print("Ожидание сообщений...")

for event in longpoll.listen():
    if event.type == VkBotEventType.MESSAGE_NEW:
        process_message(event)