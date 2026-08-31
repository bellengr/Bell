import sys
print("Бот запущен")
sys.stdout.flush()

try:
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
    print("Библиотеки загружены")
except ImportError as e:
    print(f"Ошибка: {e}")
