import os
import datetime
import pytz
from dotenv import load_dotenv

# Определение директории проекта (папка tgbot)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Загрузка переменных окружения из файла .env в папке tgbot
dotenv_path = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path)

# ================= КОНФИГУРАЦИЯ =================

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN") 
# ID админов (список) — могут обновлять расписание и управлять ролями
admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()]

ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else None

# Дата начала семестра (нужна для определения четности недели)
SEMESTER_START_DATE = datetime.date(2026, 1, 26) 
# Временная зона для всех операций с датой/временем
TZ_SARATOV = pytz.timezone('Europe/Saratov')

# База данных будет храниться в папке data в корне проекта
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_users.db")

# Кеш для расписания (хранит последние данные и время обновления)
schedule_cache = {"last_update": None, "data": {}}
