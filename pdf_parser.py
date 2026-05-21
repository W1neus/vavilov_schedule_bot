import json
import io
import sys
import requests
import pdfplumber
import re
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Временные рамки для пар
CLASS_TIMES = {
    1: {'start': "08:30", 'end': "10:00"},
    2: {'start': "10:10", 'end': "11:40"},
    3: {'start': "12:00", 'end': "13:30"},
    4: {'start': "13:40", 'end': "15:10"},
    5: {'start': "15:20", 'end': "16:50"},
    6: {'start': "17:00", 'end': "18:30"},
    7: {'start': "18:40", 'end': "20:10"},
    8: {'start': "20:20", 'end': "21:50"},
}

TIME_START_TO_PAIR_NUM = {
    "8.30": 1, "10.10": 2, "12.00": 3, 
    "13.40": 4, "15.20": 5, "17.00": 6, "18.40": 7, "20.20": 8
}

DAYS_MAP = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота"]

# Карта месяцев для парсинга заочных расписаний (вертикальный текст в родительном падеже)
MONTHS_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

def match_day_name(raw_cell):
    """Определяет день недели из ячейки с вертикальным текстом.
    
    pdfplumber извлекает вертикальный текст снизу вверх, иногда склеивая
    соседние символы (напр. 'к\\nи\\nн\\nь\\nл\\nде\\nне\\nо\\nП' для 'Понедельник').
    Это ломает простой реверс строки. Вместо этого сравниваем отсортированные 
    наборы букв (анаграмму) — это надёжно работает при любой группировке символов.
    """
    cleaned = raw_cell.replace('\n', '').replace(' ', '').lower()
    if not cleaned or len(cleaned) < 3:
        return -1
    sorted_cleaned = sorted(cleaned)
    for idx, d_name in enumerate(DAYS_MAP):
        if sorted(d_name) == sorted_cleaned:
            return idx
    return -1

def extract_date_from_cell(raw_cell, current_year=None, fallback_month=None):
    """Извлекает дату из вертикальной ячейки заочного расписания.
    
    В заочных расписаниях столбец 'Дата' содержит вертикальный текст:
    'я\nл\nе\nр\nп\nа\n6\n0' → 06 апреля
    'я\nл\nе\nр\nп\nа\n7\n0\nк\nи\nн\nр\nо\nт\nВ' → 07 апреля (с днём недели)
    
    Ячейка может содержать только дату+месяц, или дату+месяц+день_недели (когда столбцы совмещены).
    Возвращает строку ISO даты YYYY-MM-DD или None.
    """
    import datetime
    import calendar
    if not raw_cell:
        return None
    
    cleaned = raw_cell.replace('\n', '').replace(' ', '').lower()
    if not cleaned or len(cleaned) < 3:
        return None
    
    if current_year is None:
        current_year = datetime.datetime.now().year
    
    # Извлекаем цифры (день) и буквы (месяц + возможно день недели)
    digits = ''.join(c for c in cleaned if c.isdigit())
    all_letters = ''.join(c for c in cleaned if c.isalpha())
    
    if not digits:
        return None
    
    # Если в ячейке также есть день недели (merged cell), нужно убрать его буквы.
    # Пробуем каждый месяц — ищем, какой набор букв месяца является подмножеством all_letters.
    # Затем проверяем, что оставшиеся буквы — это день недели.
    month = None
    month_letters_used = ""
    
    # Сначала пробуем точное совпадение (без дня недели в ячейке)
    sorted_all = sorted(all_letters)
    for month_stem, month_num in MONTHS_MAP.items():
        if sorted(month_stem) == sorted_all:
            month = month_num
            month_letters_used = month_stem
            break
    
    if month is None:
        # Ячейка содержит буквы месяца + буквы дня недели
        # Пробуем найти месяц как подмножество букв, а остаток проверяем на день недели
        best_match = None
        for month_stem, month_num in MONTHS_MAP.items():
            remaining = list(all_letters)
            match = True
            for c in sorted(month_stem):
                if c in remaining:
                    remaining.remove(c)
                else:
                    match = False
                    break
            
            if match and remaining:
                # Проверяем, что оставшиеся буквы — день недели
                remaining_sorted = sorted(remaining)
                for day_name in DAYS_MAP:
                    if sorted(day_name) == remaining_sorted:
                        if best_match is None or len(month_stem) > len(best_match[1]):
                            best_match = (month_num, month_stem)
                        break
        
        if best_match:
            month = best_match[0]
            month_letters_used = best_match[1]
    
    if month is None and fallback_month is not None:
        month = fallback_month
        
    if month is None:
        return None
    
    # Пробуем вытащить полную дату из цифр (если они длинные)
    digits_rev = digits[::-1]
    
    if len(digits_rev) >= 8:
        try:
            d = int(digits_rev[:2])
            m = int(digits_rev[2:4])
            y = int(digits_rev[4:8])
            if 1 <= m <= 12 and 1 <= d <= 31:
                return datetime.date(y, m, d).isoformat()
        except ValueError:
            pass
            
    if len(digits_rev) >= 6:
        try:
            d = int(digits_rev[:2])
            m = int(digits_rev[2:4])
            y = 2000 + int(digits_rev[4:6])
            if 1 <= m <= 12 and 1 <= d <= 31:
                return datetime.date(y, m, d).isoformat()
        except ValueError:
            pass

    max_day = calendar.monthrange(current_year, month)[1]
    
    # Пытаемся найти корректный день.
    # Если цифр много, возможно день - это первые две цифры
    day = None
    candidates = [digits_rev, digits, digits_rev[:2], digits[:2]]
    for cand in candidates:
        if cand:
            try:
                cand_int = int(cand)
                if 1 <= cand_int <= max_day:
                    day = cand_int
                    break
            except ValueError:
                continue

    if day is None:
        return None
    
    try:
        return datetime.date(current_year, month, day).isoformat()
    except ValueError:
        return None

def extract_groups_map(table, filename=None):
    """Ищет названия групп в первых строках таблицы и возвращает словарь {индекс_колонки: название_группы}"""
    import urllib.parse
    prefix = ""
    if filename:
        decoded_filename = urllib.parse.unquote(filename).split('/')[-1]
        # Извлекаем префикс из названия файла (например, ВТ, Б-ЗиК)
        prefix_matches = re.findall(r'[А-ЯЁ][А-Яа-яЁёA-Za-z]*-[А-Яа-яЁёA-Za-z]+|[А-ЯЁ]{2,}', decoded_filename)
        if prefix_matches:
            prefix = prefix_matches[0]

    groups_map = {}
    is_garbled_pdf = False
    for row in table[:10]: # Ищем в первых 10 строках на случай сложных шапок
        for col_idx, cell in enumerate(row):
            if cell:
                cell_str = str(cell)
                # Основная регулярка для нормальных PDF (кириллица читается корректно):
                pattern = r'[А-Яа-яЁёA-Za-z]{1,2}\s*-[\s\-]*(?:[А-Яа-яЁёA-Za-z]+\s*-\s*)*\d{2,4}'
                matches = re.findall(pattern, cell_str)
                is_garbled = False
                if not matches:
                    # Фолбэк: PDF с битой кодировкой шрифта — Кириллица заменяется на U+FFFD (?).
                    pattern_fallback = r'[^\s\d\-]{1,4}\s*-[\s\-]*(?:[^\s\d\-]+\s*-\s*)*\d{2,4}'
                    matches = re.findall(pattern_fallback, cell_str)
                    if matches:
                        is_garbled = True
                        is_garbled_pdf = True
                
                if matches and col_idx not in groups_map:
                    # Удаляем случайные пробелы и лишние дефисы, чтобы привести к единому виду
                    cleaned = re.sub(r'\s+', '', matches[0])
                    cleaned = re.sub(r'-{2,}', '-', cleaned)
                    
                    if is_garbled and prefix:
                        # Заменяем всё до первого дефиса на префикс из имени файла
                        parts = cleaned.split('-', 1)
                        if len(parts) == 2:
                            cleaned = f"{prefix}-{parts[1]}"
                            
                    groups_map[col_idx] = cleaned
    return groups_map, is_garbled_pdf

def is_stars_only(val):
    """Проверяет, состоит ли ячейка только из звёздочек и пробелов — означает 'пары нет'"""
    if val is None:
        return False
    v = str(val).strip()
    if not v:
        return False
    return bool(re.match(r'^[\s\*]+$', v))

def is_clean_text(val):
    if val in (None, ""): return False
    v = str(val).strip()
    if not v: return False
    # Строка из звёздочек (*) означает "пары нет" — считаем как пустую
    if is_stars_only(val): return False
    # Исключаем попадание голых диапазонов времени вида "12.00-13.30" в ячейки дисциплин
    if re.match(r'^[\d\.\-:\s]+$', v): return False
    return True

def is_stream_lecture(text):
    if not is_clean_text(text): return False
    t = str(text).lower()
    # Ищем типичные маркеры общепоточных пар/лекций
    return "лек." in t or "лекция" in t or "кураторский" in t or "переход" in t

def clean_text(text):
    """Очищает текст от мусора из PDF: дубликатов слов и разорванных пробелами букв."""
    import re
    # 1. Убираем дублирование подряд идущих слов и фраз
    # Например: "ХИМИЯ ХИМИЯ ХИМИЯ" -> "ХИМИЯ"
    # Или: "ИСТОРИЯ РОССИИ ИСТОРИЯ РОССИИ" -> "ИСТОРИЯ РОССИИ"
    prev_text = None
    while text != prev_text:
        prev_text = text
        text = re.sub(r'\b(.+?)(?:\s+\1)+\b', r'\1', text, flags=re.IGNORECASE)
    
    # 2. Склеиваем слова, где каждая буква отделена пробелом: "О Р Г А Н И К А" -> "ОРГАНИКА"
    def replacer(match):
        s = match.group(0)
        s = s.replace("  ", "@@SPACE@@")
        s = s.replace(" ", "")
        s = s.replace("@@SPACE@@", " ")
        return s
        
    pattern = r'(?:\b[А-ЯЁA-Z]\b[ \t]+)+\b[А-ЯЁA-Z]\b'
    text = re.sub(pattern, replacer, text)
    
    return text

def process_time_block(rows, pair_num, day_idx, groups_data, groups_map):
    if not rows or pair_num == -1 or day_idx == -1: return

    # 1. Отсеиваем тайм-блоки, в которых нет ни одного реального текста (например, пустая суббота)
    has_any = False
    for r in rows:
        for c in range(2, len(r)):
            if is_clean_text(r[c]):
                has_any = True
    if not has_any: return

    # 2. Восстанавливаем объединенные ячейки (None -> реальный текст)
    grid = []
    for r in rows:
        grid.append(list(r))
        
    R = len(grid)
    if R == 0: return
    
    for r in range(R):
        for c in range(2, len(grid[r])):
            if grid[r][c] is None:
                above = grid[r-1][c] if r > 0 and c < len(grid[r-1]) else None
                left  = grid[r][c-1] if c > 2 and c-1 < len(grid[r]) else None
                
                has_above = is_clean_text(above)
                has_left  = is_clean_text(left)
                
                if has_above and has_left:
                    # Разрешение коллизии: если слева поток (лекция), она бьет вертикаль.
                    if is_stream_lecture(left):
                        grid[r][c] = left
                    else:
                        grid[r][c] = above
                elif has_left and is_stream_lecture(left):
                    grid[r][c] = left
                elif has_above:
                    grid[r][c] = above

    # 3. Раскидываем текст для каждой группы по числителю/знаменателю
    for col_idx, g_name in groups_map.items():
        texts = []
        for r in range(R):
            val = grid[r][col_idx] if col_idx < len(grid[r]) else None
            if is_clean_text(val):
                txt = str(val).strip().replace('\n', ' ')
                txt = clean_text(txt)
                texts.append(re.sub(r'\s+', ' ', txt))
            else:
                texts.append("")
                
        if not any(texts): continue

        # Если строка всего одна, дублируем её для числителя и знаменателя
        if len(texts) == 1:
            texts = [texts[0], texts[0]]
            
        # Если все строки одинаковые (одна дисциплина вертикально объединена на весь блок)
        if all(t == texts[0] for t in texts) and texts[0]:
            top_val = bot_val = texts[0]
        else:
            mid = max(1, len(texts) // 2)
            top_texts = [t for t in texts[:mid] if t]
            bot_texts = [t for t in texts[mid:] if t]
            
            top_val = " | ".join(filter(None, top_texts))
            bot_val = " | ".join(filter(None, bot_texts))

        if top_val and bot_val and top_val == bot_val:
            groups_data[g_name]["numerator"][day_idx][pair_num] = top_val
            groups_data[g_name]["denominator"][day_idx][pair_num] = top_val
        else:
            if top_val: groups_data[g_name]["numerator"][day_idx][pair_num] = top_val
            if bot_val: groups_data[g_name]["denominator"][day_idx][pair_num] = bot_val

def parse_zaochnaya_pdf_to_json(link):
    """Парсит заочное расписание — в нём вместо дней недели конкретные даты.
    
    Структура таблицы заочного расписания:
    Столбец 0: Дата (вертикальный текст: 'я\nл\nе\nр\nп\nа\n6\n0' → 06 апреля)
    Столбец 1: День недели (или Часы, если нет отдельного столбца дня)
    Столбец 2: Часы (8.30-10.00 и т.д.)
    Столбцы 3+: Группы
    
    Если столбцов всего 4: [Дата, День, Часы, Группа]
    Если столбцов 3: [Дата+День, Часы, Группа] — день совмещён с датой
    """
    import datetime
    groups_data = {}  # {имя_группы: {дата_строка: {номер_пары: предмет}}}
    groups_map = {}
    
    try:
        import urllib.parse
        decoded_filename = urllib.parse.unquote(link).split('/')[-1]
        
        response = requests.get(link, timeout=30, verify=False)
        response.raise_for_status()
        
        # Определяем год из ссылки или текста PDF
        current_year = datetime.datetime.now().year
        fallback_month = None
        start_dt = None
        is_garbled_pdf_global = False
        
        # Пытаемся вытащить стартовую дату из названия файла (напр. ...с 16.03.2026.pdf)
        date_match = re.search(r'(\d{2})\.(\d{2})(?:\.(\d{4}))?', decoded_filename)
        if date_match:
            day_str, month_str, year_str = date_match.groups()
            fallback_month = int(month_str)
            if year_str:
                current_year = int(year_str)
            try:
                start_dt = datetime.date(current_year, fallback_month, int(day_str))
            except ValueError:
                pass
        
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
                if not tables: continue
                
                for table in tables:
                    if not groups_map:
                        groups_map, is_garbled_pdf_global = extract_groups_map(table, filename=link)
                        for _, g in groups_map.items():
                            groups_data[g] = {}
                    
                    # Определяем структуру: ищем строку-заголовок с "Часы" / "Дата" / "Дни"
                    # В заочных PDF столбец 0 — Дата, столбец 1 — День (или Часы), столбец 2 — Часы
                    # Определяем, какой столбец содержит время
                    time_col = -1
                    for row in table[:5]:
                        for ci, cell in enumerate(row):
                            if cell and 'час' in str(cell).lower():
                                time_col = ci
                                break
                        if time_col != -1:
                            break
                    
                    if time_col == -1:
                        # По умолчанию: если 4+ столбцов → col2=Часы, если 3 → col1=Часы
                        max_cols = max(len(r) for r in table)
                        time_col = 2 if max_cols >= 4 else 1
                    
                    current_date = None  # дата в формате ISO
                    current_day_name = None  # день недели
                    current_pair_num = -1
                    
                    seq_dt = start_dt
                    is_first_0830 = True
                    
                    for row in table:
                        if len(row) < 2: continue
                        
                        raw_col1 = str(row[time_col]) if len(row) > time_col and row[time_col] else ""
                        
                        if seq_dt and (raw_col1.startswith('08.30') or raw_col1.startswith('8.30')):
                            if not is_first_0830:
                                seq_dt += datetime.timedelta(days=1)
                                if seq_dt.weekday() == 6: # Sunday
                                    seq_dt += datetime.timedelta(days=1)
                            is_first_0830 = False
                        
                        # Проверяем столбец 0 на дату
                        raw_col0 = str(row[0]) if row[0] else ""
                        
                        if start_dt and seq_dt:
                            # Если дата начала получена из имени файла, используем строго последовательный трекер даты.
                            # Это обходит ошибки распознавания вертикального текста в PDF, которые приводят к неверному парсингу дат.
                            current_date = seq_dt.isoformat()
                            dt = seq_dt
                            current_day_name = DAYS_MAP[dt.weekday()] if dt.weekday() < 6 else "суббота"
                        else:
                            date_val = extract_date_from_cell(raw_col0, current_year, fallback_month)
                            if date_val:
                                current_date = date_val
                                dt = datetime.date.fromisoformat(date_val)
                                current_day_name = DAYS_MAP[dt.weekday()] if dt.weekday() < 6 else "суббота"
                            elif current_date and raw_col0:
                                date_val_fallback = extract_date_from_cell(raw_col0, current_year, fallback_month)
                                if date_val_fallback:
                                    current_date = date_val_fallback
                                    dt = datetime.date.fromisoformat(date_val_fallback)
                                    current_day_name = DAYS_MAP[dt.weekday()] if dt.weekday() < 6 else "суббота"
                        
                        # Также проверяем day name в столбце 0 или 1 (для навигации внутри таблицы)
                        if not start_dt:
                            day_match_0 = match_day_name(raw_col0)
                            if day_match_0 != -1 and time_col >= 2:
                                pass
                            if time_col >= 2 and len(row) > 1:
                                raw_col1_val = str(row[1]) if row[1] else ""
                                day_match_1 = match_day_name(raw_col1_val)
                        
                        if current_date is None:
                            continue
                        
                        # Ищем время в столбце time_col
                        raw_time = str(row[time_col]).replace('\n', '').replace(' ', '').strip() if time_col < len(row) and row[time_col] else ""
                        new_pair_num = -1
                        for t_str, p_num in TIME_START_TO_PAIR_NUM.items():
                            if raw_time.startswith(t_str):
                                new_pair_num = p_num
                                break
                        
                        if new_pair_num != -1:
                            current_pair_num = new_pair_num
                        
                        if current_pair_num == -1:
                            continue
                        
                        # Извлекаем предметы для каждой группы
                        for col_idx, g_name in groups_map.items():
                            if col_idx < len(row) and is_clean_text(row[col_idx]):
                                txt = str(row[col_idx]).strip().replace('\n', ' ')
                                txt = clean_text(txt)
                                txt = re.sub(r'\s+', ' ', txt)
                                
                                if current_date not in groups_data[g_name]:
                                    groups_data[g_name][current_date] = {}
                                groups_data[g_name][current_date][current_pair_num] = txt
        
        # Сборка JSON
        json_groups = []
        for g_name, dates_data in groups_data.items():
            days_list = []
            for date_str in sorted(dates_data.keys()):
                pairs = dates_data[date_str]
                dt = datetime.date.fromisoformat(date_str)
                day_name = DAYS_MAP[dt.weekday()] if dt.weekday() < 6 else "суббота"
                
                lessons_list = []
                for pair_num in sorted(pairs.keys()):
                    time_info = CLASS_TIMES.get(pair_num, {'start': '', 'end': ''})
                    lessons_list.append({
                        "time_from": time_info['start'],
                        "time_to": time_info['end'],
                        "subject": pairs[pair_num],
                        "week": "all"
                    })
                
                if lessons_list:
                    days_list.append({
                        "name": day_name,
                        "date": date_str,
                        "lessons": lessons_list
                    })
            
            if days_list:
                json_groups.append({
                    "name": g_name,
                    "schedule": {
                        "days": days_list
                    }
                })
        
        return json_groups
    
    except Exception as e:
        print(f"Ошибка парсинга заочного расписания: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return []

def parse_pdf_to_json(link, form_type='ochnaya'):
    """Универсальный парсер PDF расписания.
    
    form_type:
      - 'ochnaya' или 'ochno-zaochnaya' — стандартный парсинг (дни недели, числитель/знаменатель)
      - 'zaochnaya' — парсинг с конкретными датами
    """
    if 'zaochn' in form_type and 'ochno' not in form_type:
        # Чистая заочная форма — используем специальный парсер
        return parse_zaochnaya_pdf_to_json(link)
    
    # Очная и очно-заочная форма — стандартный парсер
    groups_data = {}
    groups_map = {}

    try:
        response = requests.get(link, timeout=30, verify=False)
        response.raise_for_status()

        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
                if not tables: continue

                for table in tables:
                    # Если еще не нашли группы - ищем в первом попавшемся table
                    if not groups_map:
                        groups_map, _ = extract_groups_map(table, filename=link)
                        
                        # Инициализируем хранилище для найденных групп
                        for _, g in groups_map.items():
                            groups_data[g] = {
                                "numerator": {i: {} for i in range(6)},
                                "denominator": {i: {} for i in range(6)}
                            }

                    current_day_idx = -1
                    block_rows = []
                    current_pair_num = -1

                    for row in table:
                        if len(row) < 2: continue
                        
                        raw_day_0 = str(row[0]) if row[0] else ""
                        raw_day_1 = str(row[1]) if len(row) > 1 and row[1] else ""
                        
                        day_match = match_day_name(raw_day_0)
                        if day_match == -1:
                            day_match = match_day_name(raw_day_1)
                            
                        if day_match != -1:
                            process_time_block(block_rows, current_pair_num, current_day_idx, groups_data, groups_map)
                            block_rows = []
                            current_pair_num = -1
                            current_day_idx = day_match
                            
                        if current_day_idx == -1: continue

                        raw_time = str(row[1]).replace('\n', '').replace(' ', '').strip() if row[1] else ""
                        new_pair_num = -1
                        for t_str, p_num in TIME_START_TO_PAIR_NUM.items():
                            # Ищем точное совпадение начала строки, чтобы избежать бага: "8.30" в "17.00-18.30"
                            if raw_time.startswith(t_str): 
                                new_pair_num = p_num
                                break
                                
                        if new_pair_num == -1 and len(row) > 2:
                            raw_time2 = str(row[2]).replace('\n', '').replace(' ', '').strip() if row[2] else ""
                            for t_str, p_num in TIME_START_TO_PAIR_NUM.items():
                                if raw_time2.startswith(t_str):
                                    new_pair_num = p_num
                                    break
                        
                        if new_pair_num != -1:
                            process_time_block(block_rows, current_pair_num, current_day_idx, groups_data, groups_map)
                            current_pair_num = new_pair_num
                            block_rows = [row]
                        else:
                            if current_pair_num != -1:
                                block_rows.append(row)

                    process_time_block(block_rows, current_pair_num, current_day_idx, groups_data, groups_map)

        # Сборка финального JSON
        json_groups = []
        for g_name, g_weeks in groups_data.items():
            days_list = []
            for day_idx in range(6):
                day_name = DAYS_MAP[day_idx]
                lessons_list = []
                
                # Собираем уроки для текущего дня (объединяем числитель и знаменатель)
                all_pairs = set(g_weeks["numerator"][day_idx].keys()) | set(g_weeks["denominator"][day_idx].keys())
                for pair_num in sorted(all_pairs):
                    num_subj = g_weeks["numerator"][day_idx].get(pair_num)
                    den_subj = g_weeks["denominator"][day_idx].get(pair_num)
                    
                    time_info = CLASS_TIMES.get(pair_num, {'start': '', 'end': ''})
                    
                    if num_subj and num_subj == den_subj:
                        # Урок совпадает по числителю и знаменателю (вероятно идёт каждую неделю)
                        lessons_list.append({
                            "time_from": time_info['start'],
                            "time_to": time_info['end'],
                            "subject": num_subj,
                            "week": "all" # Или можно дублировать запись для числителя и знаменателя
                        })
                    else:
                        if num_subj:
                            lessons_list.append({
                                "time_from": time_info['start'],
                                "time_to": time_info['end'],
                                "subject": num_subj,
                                "week": "numerator"
                            })
                        if den_subj:
                            lessons_list.append({
                                "time_from": time_info['start'],
                                "time_to": time_info['end'],
                                "subject": den_subj,
                                "week": "denominator"
                            })
                
                if lessons_list:
                    days_list.append({
                        "name": day_name,
                        "lessons": lessons_list
                    })
            
            if days_list:
                json_groups.append({
                    "name": g_name,
                    "schedule": {
                        "days": days_list
                    }
                })

        return json_groups

    except Exception as e:
        print(f"Ошибка парсинга: {e}", file=sys.stderr)
        return []

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python pdf_parser.py <url_pdf_файла>")
        sys.exit(1)
        
    url = sys.argv[1]
    form_type = sys.argv[2] if len(sys.argv) > 2 else 'ochnaya'
    result_json = parse_pdf_to_json(url, form_type=form_type)
    
    # Сохраняем результат в файл
    import os
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    os.makedirs(DATA_DIR, exist_ok=True)
    output_filename = os.path.join(DATA_DIR, "schedule.json")
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
        
    print(f"Готово! Результат сохранен в файл: {output_filename}")
