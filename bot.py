import os
import os
import re
import time
from datetime import datetime
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
# ======================= ЗАГРУЗКА НАСТРОЕК ИЗ ОКРУЖЕНИЯ =======================
TOKEN = os.environ.get('TOKEN')
GROUP_ID = int(os.environ.get('GROUP_ID', 0))
# Проверка, что переменные загружены
if not TOKEN:
    raise ValueError("Ошибка: переменная окружения TOKEN не найдена!")
if not GROUP_ID:
    raise ValueError("Ошибка: переменная окружения GROUP_ID не найдена!")
MAX_QUEUE_SIZE = 10  # Максимальное количество ссылок в очереди
# ============================================================================
# Хранилища
queue = []  # {'link': 'wall-123_456', 'user_id': 123, 'timestamp': ...}
pending_links = {}  # {user_id: ['wall-1_1', 'wall-2_2']}
bot_messages = {}  # {user_id: [message_id1, message_id2]}
# Подключение к VK
vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)
def clean_queue():
    """Оставляет только последние MAX_QUEUE_SIZE ссылок"""
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
def extract_wall_id(text):
    """Извлекает wall-123_456 из текста"""
    match = re.search(r'wall(-?\d+)_(\d+)', text)
    return match.group(0) if match else None
def check_post_exists(wall_id):
    """Проверяет, существует ли пост"""
    parts = wall_id.replace('wall', '').split('_')
    owner_id = int(parts[0])
    post_id = int(parts[1])
    try:
        vk.wall.getById(posts=f"{owner_id}_{post_id}")
        return True
    except Exception:
        return False
def check_like(user_id, wall_id):
    """Проверяет, поставил ли пользователь лайк"""
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
    """Проверяет лайки на последние 10 ссылок"""
    links_to_check = [item['link'] for item in queue[-10:]]
    if not links_to_check:
        return True, []
    missing = []
    for link in links_to_check:
        if not check_like(user_id, link):
            missing.append(link)
    return len(missing) == 0, missing
def can_user_post(user_id):
    """Проверяет условие 'раз в 5 чужих постов'"""
    last_index = -1
    for i, item in enumerate(queue):
        if item['user_id'] == user_id:
            last_index = i
    if last_index == -1:
        return True
    if last_index >= len(queue) - 1:
        return False
    return (len(queue) - last_index - 1) >= 5
def send_message(peer_id, text, save_for_deletion=False, user_id=None):
    """Отправляет сообщение. Если save_for_deletion=True — сохраняет ID для удаления"""
    result = vk.messages.send(
        peer_id=peer_id,
        message=text,
        random_id=int(time.time() * 1000)
    )
    if save_for_deletion and user_id:
        if user_id not in bot_messages:
            bot_messages[user_id] = []
        bot_messages[user_id].append(result)
    return result
def delete_message(peer_id, message_id):
    """Безопасно удаляет сообщение"""
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
    """Удаляет все служебные сообщения бота для пользователя"""
    if user_id in bot_messages:
        for msg_id in bot_messages[user_id]:
            delete_message(peer_id, msg_id)
        del bot_messages[user_id]
        print(f"🗑 Удалены сообщения для пользователя {user_id}")
def process_message(event):
    """Обрабатывает новое сообщение"""
    peer_id = event.object.message['peer_id']
    user_id = event.object.message['from_id']
    text = event.object.message.get('text', '').strip()
    message_id = event.object.message['id']
    # Игнорируем сообщения от бота
    if user_id < 0:
        return
    wall_id = extract_wall_id(text)
    # Нет ссылки — удаляем и пишем
    if not wall_id:
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"❌ @id{user_id}, для публикации нужна ссылка на открытый пост ВК.\nФормат: wall-123_456",
            save_for_deletion=True,
            user_id=user_id
        )
        return
    # Пост не существует или закрыт
    if not check_post_exists(wall_id):
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"❌ @id{user_id}, пост {wall_id} не найден или закрыт.\nНужна ссылка на открытый пост ВК.",
            save_for_deletion=True,
            user_id=user_id
        )
        return
    # Проверка условия "раз в 5 чужих"
    if not can_user_post(user_id):
        last_index = -1
        for i, item in enumerate(queue):
            if item['user_id'] == user_id:
                last_index = i
        posts_after = len(queue) - last_index - 1
        need_to_wait = 5 - posts_after
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"❌ @id{user_id}, ты можешь отправить новую ссылку только после {need_to_wait} чужих постов.\nСейчас после твоей последней ссылки п
            save_for_deletion=True,
            user_id=user_id
        )
        return
    # Проверка лайков на предыдущие 10 ссылок
    all_liked, missing_links = check_previous_likes(user_id)
    if not all_liked:
        pending_links[user_id] = missing_links
        delete_message(peer_id, message_id)
        missing_text = "\n".join([f"· {link}" for link in missing_links])
        send_message(
            peer_id,
            f"❌ @id{user_id}, ты пропустил лайки на эти посты:\n{missing_text}\n\n
📌 Поставь лайки на все пропущенные посты и отправь ссылку заново.\nТвоё сообщение со ссылкой удалено до выполнения условий.",
            save_for_deletion=True,
            user_id=user_id
        )
        return
    # ✅ ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ — публикуем ссылку
    queue.append({
        'link': wall_id,
        'user_id': user_id,
        'timestamp': datetime.now()
    })
    clean_queue()
    # Удаляем все старые сообщения бота для этого пользователя
    delete_bot_messages(user_id, peer_id)
    if user_id in pending_links:
        del pending_links[user_id]
    # Рассчитываем, через сколько ссылок можно будет отправить новую
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
    # Финальное сообщение (НЕ сохраняется для удаления)
    send_message(
        peer_id,
        f"✅ @id{user_id}, всё выполнено! Твоя ссылка принята.\n📊 В очереди: {len(queue)} постов (хранятся последние {MAX_QUEUE_SIZE})\n
⏳ {extra_msg}"
    )
# ======================= ЗАПУСК =======================
print("🚀 Бот запущен и слушает сообщения...")
print(f"📌 ID группы: {GROUP_ID}")
print(f"📌 Максимальный размер очереди: {MAX_QUEUE_SIZE} ссылок")
print("⏳ Ожидание сообщений...")
for event in longpoll.listen():
    if event.type == VkBotEventType.MESSAGE_NEW:
        process_message(event