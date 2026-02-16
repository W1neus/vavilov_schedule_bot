import os
import datetime
import pytz
from telegram.request import HTTPXRequest

# ================= КОНФИГУРАЦИЯ =================

#токен бота
BOT_TOKEN = "" 
# ID админа (для доступа к командам управления пользователями) 
ADMIN_ID = 123456789

#ссылка на страницу с расписанием
SCHEDULE_URL = "https://www.vavilovsar.ru/upravlenie-obespecheniya-kachestva-obrazovaniya/struktura/otdel-organizacii-uchebnogo-processa/uk2/institut-injenerii-i-robototexniki/ochnaya-forma-obucheniya"
#фильтр для поиска PDF 
SEARCH_QUERY_PDF = "б-пи-101" 
#кол-во потоков для парсинга PDF
MAX_WORKERS = 8

#дата начала семестра (нужна для определения четности недели)
SEMESTER_START_DATE = datetime.date(2026, 1, 26) 
#временная зона для всех операций с датой/временем
TZ_SARATOV = pytz.timezone('Europe/Saratov')

#определение дирректории проекта 
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
#база данных будет храниться в файле рядом со скриптами
DB_FILE = os.path.join(BASE_DIR, "bot_users.db")

#временные рамки для классов (для определения текущей пары)
CLASS_TIMES = {
    1: {'start': (8, 30),  'end': (10, 0)},
    2: {'start': (10, 10), 'end': (11, 40)},
    3: {'start': (12, 0),  'end': (13, 30)},
    4: {'start': (13, 40), 'end': (15, 10)},
    5: {'start': (15, 20), 'end': (16, 50)},
    6: {'start': (17, 0),  'end': (18, 30)},
}

#время начала каждой пары для быстрого определения номера пары по времени
TIME_START_TO_PAIR_NUM = {
    "8.30": 1, "10.10": 2, "12.00": 3, 
    "13.40": 4, "15.20": 5, "17.00": 6
}

#заголовки для HTTP-запросов (для парсера)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

#кеш для расписания (хранит последние данные и время обновления)
schedule_cache = {"last_update": None, "data": {}}
