"""
teacher_search.py — модуль поиска преподавателей.

Функции:
- fetch_all_teachers_from_site()  : получить список преподавателей с сайта вуза
- search_teachers_by_surname()    : нечёткий поиск по фамилии
- find_teacher_in_schedule()      : найти препода в расписании (через regex + fuzzy)
- format_teacher_schedule()       : форматировать вывод для Telegram
"""

import re
import time
import logging
import difflib
import requests
from functools import lru_cache

logger = logging.getLogger(__name__)

KADRY_URL = "https://www.vavilovsar.ru/kadry"
CACHE_TTL = 60 * 60 * 24  # 24 часа

_staff_cache: list[str] = []
_staff_cache_time: float = 0.0

# Regex для поиска ФИО преподавателя в строке занятия.
# Ищет паттерн: [звание.] Фамилия И. О.
# Примеры: "доц. Гижов В.А.", "проф. Зирук И.В.", "Иванова Л.М."
_TEACHER_REGEX = re.compile(
    r'(?:(?:доц|проф|асс|ст\.?\s*пр(?:еп)?|преп|лаб)\.?\s+)?'
    r'([\u0400-\u04FF][\u0400-\u04FF\-]+)\s+'
    r'([\u0400-\u04FF])\.\s*([\u0400-\u04FF])\.',
    re.UNICODE
)


@lru_cache(maxsize=128)
def parse_teacher_query(query: str):
    """
    Парсит поисковый запрос (например "Цагареишвили Л.С." или "Цагареишвили")
    на фамилию и список инициалов.
    """
    query = query.strip()
    if not query:
        return "", ()
        
    parts = query.split()
    surname = parts[0]
    
    initials = []
    for part in parts[1:]:
        # Если это полное имя/отчество (длина > 2 и нет точек)
        if len(part) > 2 and '.' not in part:
            initials.append(part[0].lower())
        else:
            # Иначе это инициалы (например "Л.С." или "Л."), вытаскиваем все русские буквы
            letters = re.findall(r'[\u0400-\u04FF]', part, re.UNICODE)
            for l in letters:
                initials.append(l.lower())

    return surname.lower(), tuple(initials)


def get_short_name(full_name: str) -> str:
    """
    Возвращает компактное ФИО (например, "Цагареишвили Л.С.") из полного ФИО.
    """
    parts = full_name.strip().split()
    if not parts:
        return ""
    surname = parts[0]
    initials = ""
    if len(parts) >= 3:
        initials = f" {parts[1][0]}.{parts[2][0]}."
    elif len(parts) == 2:
        initials = f" {parts[1][0]}."
    return f"{surname}{initials}"


def make_safe_callback_data(prefix: str, full_name: str) -> str:
    """
    Формирует callback_data длиной строго менее 64 байт для Telegram.
    """
    short_name = get_short_name(full_name)
    prefix_bytes_len = len(prefix.encode('utf-8'))
    
    parts = short_name.split(maxsplit=1)
    surname = parts[0]
    initials = f" {parts[1]}" if len(parts) > 1 else ""
    
    initials_bytes_len = len(initials.encode('utf-8'))
    
    # Ограничиваем длину фамилии в байтах, чтобы вся строка с префиксом и инициалами была <= 63 байт
    max_surname_bytes = 63 - prefix_bytes_len - initials_bytes_len
    
    surname_bytes = surname.encode('utf-8')
    if len(surname_bytes) > max_surname_bytes:
        truncated = surname_bytes[:max_surname_bytes].decode('utf-8', errors='ignore')
        surname = truncated
        
    return f"{prefix}{surname}{initials}"


def fetch_all_teachers_from_site() -> list[str]:
    """
    Загружает страницу /kadry и возвращает список ФИО преподавателей.
    Кешируется на 24 часа.
    """
    global _staff_cache, _staff_cache_time

    now = time.time()
    if _staff_cache and (now - _staff_cache_time) < CACHE_TTL:
        return _staff_cache

    try:
        resp = requests.get(KADRY_URL, timeout=15, verify=False)
        resp.raise_for_status()
        html = resp.text

        # Каждый преподаватель — ссылка вида /kadry/фамилия-имя-...
        # Текст ссылки = полное ФИО
        pattern = re.compile(
            r'href=["\']https?://www\.vavilovsar\.ru/kadry/[^"\']+["\'][^>]*>\s*([А-ЯЁа-яёA-Za-z][^<]{3,60}?)\s*</a>',
            re.UNICODE
        )
        names = []
        seen = set()
        for m in pattern.finditer(html):
            name = m.group(1).strip()
            # Фильтруем мусор: должно содержать пробел (это ФИО), не быть навигационным пунктом
            if ' ' in name and name not in seen and len(name) < 80:
                names.append(name)
                seen.add(name)

        if names:
            _staff_cache = names
            _staff_cache_time = now
            logger.info(f"Staff cache updated: {len(names)} teachers loaded.")
        else:
            logger.warning("Staff cache: no names found on kadry page!")

    except Exception as e:
        logger.error(f"Error fetching kadry page: {e}")

    return _staff_cache


def search_teachers_by_surname(surname: str) -> list[str]:
    """
    Нечёткий поиск по фамилии среди кадров сайта.
    Возвращает список совпадающих ФИО.
    """
    all_teachers = fetch_all_teachers_from_site()
    surname_lower = surname.strip().lower()

    results = []
    for full_name in all_teachers:
        # Фамилия может быть составной (Федорова-Кузнецова), первое слово всегда фамилия
        last_name = full_name.split()[0].lower()

        # Точное вхождение фамилии (включая составные)
        if last_name.startswith(surname_lower) or surname_lower.startswith(last_name):
            results.append(full_name)
            continue
        
        # Дополнительная проверка частей составной фамилии
        parts = last_name.split('-')
        if any(p.startswith(surname_lower) or surname_lower.startswith(p) for p in parts if p):
            results.append(full_name)
            continue

        ratio = difflib.SequenceMatcher(None, surname_lower, last_name).ratio()
        if ratio >= 0.75:
            results.append(full_name)

    # Сортируем: сначала точные совпадения
    results.sort(key=lambda n: (
        0 if n.lower().startswith(surname_lower) else 1,
        n
    ))
    return results


def _surnames_match(target_surname: str, subject_text: str) -> bool:
    """
    Проверяет, есть ли фамилия target_surname в строке subject_text.
    Использует regex + fuzzy. Поддерживает составные фамилии и инициалы.
    """
    query_surname, query_initials = parse_teacher_query(target_surname)
    target_parts = query_surname.split('-')  # Части составной фамилии

    for m in _TEACHER_REGEX.finditer(subject_text):
        found_surname = m.group(1).lower()
        found_i1 = m.group(2).lower()
        found_i2 = m.group(3).lower()

        surname_ok = False
        # Прямое совпадение
        if found_surname == query_surname:
            surname_ok = True
        # Одна из частей составной фамилии совпадает
        elif any(p == found_surname for p in target_parts):
            surname_ok = True
        # Допускаем небольшие отклонения (нечёткое сравнение)
        else:
            ratio = difflib.SequenceMatcher(None, query_surname, found_surname).ratio()
            if ratio >= 0.80:
                surname_ok = True
            # Нечёткое сравнение с первой частью составной фамилии
            elif len(target_parts) > 1:
                ratio_part = difflib.SequenceMatcher(None, target_parts[0], found_surname).ratio()
                if ratio_part >= 0.85:
                    surname_ok = True

        # Если фамилия совпала, проверяем инициалы (если они заданы в поисковом запросе)
        if surname_ok:
            if not query_initials:
                # Если в запросе нет инициалов, совпадение засчитывается
                return True
            
            # Если в запросе один инициал, проверяем первый инициал
            if len(query_initials) == 1:
                if found_i1 == query_initials[0]:
                    return True
            
            # Если в запросе два или более инициалов, проверяем оба инициала
            if len(query_initials) >= 2:
                if found_i1 == query_initials[0] and found_i2 == query_initials[1]:
                    return True

    return False


def find_teacher_in_schedule(surname: str, schedule_data: dict) -> list[dict]:
    """
    Ищет преподавателя по фамилии во всём schedule_cache['data'].

    Возвращает список:
    [
      {
        "group": "ВТ-21",
        "uk": "Учебный комплекс №3",
        "form": "Заочная форма",
        "is_zaochnaya": True,
        "days": [
          {"date": ..., "name": ..., "lessons": [...]},  # только занятия этого препода
          ...
        ]
      },
      ...
    ]
    """
    results = []

    for group_name, group_info in schedule_data.items():
        meta = group_info.get("_meta", {})
        uk = meta.get("uk", "")
        form = meta.get("form", "")
        form_slug = meta.get("form_slug", "")
        days = group_info.get("days", [])

        is_zaochnaya = any("date" in d for d in days)

        matching_days = []
        for day in days:
            matching_lessons = []
            for lesson in day.get("lessons", []):
                subject = lesson.get("subject", "")
                if _surnames_match(surname, subject):
                    matching_lessons.append(lesson)

            if matching_lessons:
                matching_days.append({
                    "date": day.get("date", ""),
                    "name": day.get("name", ""),
                    "lessons": matching_lessons,
                })

        if matching_days:
            results.append({
                "group": group_name,
                "uk": uk,
                "form": form,
                "form_slug": form_slug,
                "is_zaochnaya": is_zaochnaya,
                "days": matching_days,
            })

    # Сортировка: УК → форма → группа
    _FORM_ORDER = {
        "ochnaya-forma-obucheniya": 0,
        "zaochnaya-forma-obucheniya": 1,
        "ochno-zaochnaya-forma-obucheniya": 2,
    }
    results.sort(key=lambda x: (
        x["uk"],
        _FORM_ORDER.get(x["form_slug"], 9),
        x["group"]
    ))

    return results


def format_teacher_schedule(results: list[dict], surname: str) -> str:
    """
    Форматирует найденное расписание для вывода в Telegram.
    Сортировка: УК → Форма → Группы.
    """
    if not results:
        return f"😔 Занятий преподавателя <b>{surname}</b> в расписании не найдено."

    lines = [f"📋 <b>Расписание преподавателя {surname}</b>\n"]

    # Группируем по дням
    day_slots = {}
    for entry in results:
        uk = entry["uk"] or "Неизвестный УК"
        form = entry["form"] or "Неизвестная форма"
        group = entry["group"]
        for day in entry["days"]:
            date_str = day.get("date", "")
            day_name = day.get("name", "").capitalize()

            if date_str:
                try:
                    import datetime
                    dt = datetime.date.fromisoformat(date_str)
                    date_display = f"{dt.strftime('%d.%m.%Y')} ({day_name})"
                except Exception:
                    date_display = f"{date_str} ({day_name})"
            else:
                date_display = day_name

            if date_display not in day_slots:
                day_slots[date_display] = {}
            
            for lesson in day["lessons"]:
                key = (lesson["time_from"], lesson["time_to"], lesson["subject"])
                if key not in day_slots[date_display]:
                    day_slots[date_display][key] = {}
                uk_form_key = (uk, form)
                if uk_form_key not in day_slots[date_display][key]:
                    day_slots[date_display][key][uk_form_key] = []
                if group not in day_slots[date_display][key][uk_form_key]:
                    day_slots[date_display][key][uk_form_key].append(group)

    for date_display, time_slots in day_slots.items():
        lines.append(f"🗓 <b>{date_display}</b>")
        sorted_slots = sorted(time_slots.items(), key=lambda x: x[0][0])
        for (t_from, t_to, subj), uk_form_groups in sorted_slots:
            lines.append(f"⏰ {t_from}-{t_to}")
            for (uk, form), groups in uk_form_groups.items():
                groups_str = ", ".join(groups)
                lines.append(f"🏢 <b>{uk}</b> | 📚 <i>{form}</i> | 👥 <b>{groups_str}</b>")
            lines.append(f"📚 {subj}")
            lines.append("")

    return "\n".join(lines).strip()


# ===== День и неделя для преподавателя =====

import datetime as _dt

_DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_DAYS_RU_CAP = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
_FORM_ORDER = {
    "ochnaya-forma-obucheniya": 0,
    "zaochnaya-forma-obucheniya": 1,
    "ochno-zaochnaya-forma-obucheniya": 2,
}


def _sort_key(entry):
    return (entry["uk"], _FORM_ORDER.get(entry["form_slug"], 9), entry["group"])


def find_teacher_on_date(surname: str, schedule_data: dict, target_date: _dt.date,
                          week_parity: int = 0) -> list[dict]:
    """
    Возвращает список записей вида:
    [{"group": ..., "uk": ..., "form": ..., "lessons": [...], "is_zaochnaya": ...}]
    только для конкретной даты.
    """
    parity_str = "denominator" if week_parity == 1 else "numerator"
    target_day_name = _DAYS_RU[target_date.weekday()]
    target_date_str = target_date.isoformat()

    results = []
    for group_name, group_info in schedule_data.items():
        meta = group_info.get("_meta", {})
        days = group_info.get("days", [])
        is_zaochnaya = any("date" in d for d in days)

        matching_lessons = []

        if is_zaochnaya:
            day_data = next((d for d in days if d.get("date") == target_date_str), None)
            if day_data:
                for lesson in day_data.get("lessons", []):
                    if _surnames_match(surname, lesson.get("subject", "")):
                        matching_lessons.append(lesson)
        else:
            day_data = next((d for d in days if d.get("name", "").lower() == target_day_name), None)
            if day_data:
                for lesson in day_data.get("lessons", []):
                    if lesson.get("week") in ["all", parity_str]:
                        if _surnames_match(surname, lesson.get("subject", "")):
                            matching_lessons.append(lesson)

        if matching_lessons:
            results.append({
                "group": group_name,
                "uk": meta.get("uk", ""),
                "form": meta.get("form", ""),
                "form_slug": meta.get("form_slug", ""),
                "is_zaochnaya": is_zaochnaya,
                "lessons": matching_lessons,
            })

    results.sort(key=_sort_key)
    return results


def format_teacher_day(surname: str, results: list[dict],
                        target_date: _dt.date) -> str:
    """Форматирует дневное расписание преподавателя."""
    day_name = _DAYS_RU_CAP[target_date.weekday()]
    date_display = target_date.strftime("%d.%m.%Y")
    header = f"📋 <b>{day_name}, {date_display}</b>\n👨‍🏫 <b>{surname}</b>\n{'='*25}"

    if not results:
        return header + "\n\n😴 Пар нет"

    lines = [header]
    time_slots = {}
    for entry in results:
        uk = entry["uk"] or "УК"
        form = entry["form"] or "Форма"
        group = entry["group"]
        for lesson in entry["lessons"]:
            key = (lesson["time_from"], lesson["time_to"], lesson["subject"])
            if key not in time_slots:
                time_slots[key] = {}
            uk_form_key = (uk, form)
            if uk_form_key not in time_slots[key]:
                time_slots[key][uk_form_key] = []
            if group not in time_slots[key][uk_form_key]:
                time_slots[key][uk_form_key].append(group)

    sorted_slots = sorted(time_slots.items(), key=lambda x: x[0][0])
    for (t_from, t_to, subj), uk_form_groups in sorted_slots:
        lines.append(f"⏰ {t_from}-{t_to}")
        for (uk, form), groups in uk_form_groups.items():
            groups_str = ", ".join(groups)
            lines.append(f"🏢 <b>{uk}</b> | 📚 <i>{form}</i> | 👥 <b>{groups_str}</b>")
        lines.append(f"📚 {subj}")
        lines.append("")

    return "\n".join(lines).strip()


def find_teacher_week(surname: str, schedule_data: dict,
                       week_start: _dt.date, week_parity: int = 0) -> str:
    """
    Возвращает форматированное расписание преподавателя за всю неделю
    (Mon-Sat), начиная с week_start.
    """
    parity_str = "denominator" if week_parity == 1 else "numerator"
    w_type = "Нижняя" if week_parity == 1 else "Верхняя"

    header = (
        f"📋 <b>Расписание на неделю ({w_type})</b>\n"
        f"👨‍🏫 <b>{surname}</b>\n{'='*25}"
    )
    lines = [header]
    has_any = False

    for day_offset in range(6):  # Пн-Сб
        day = week_start + _dt.timedelta(days=day_offset)
        day_name = _DAYS_RU[day.weekday()]
        day_name_cap = _DAYS_RU_CAP[day.weekday()]
        day_date_str = day.isoformat()

        day_results = []

        for group_name, group_info in schedule_data.items():
            meta = group_info.get("_meta", {})
            days = group_info.get("days", [])
            is_zaochnaya = any("date" in d for d in days)

            matching_lessons = []
            if is_zaochnaya:
                day_data = next((d for d in days if d.get("date") == day_date_str), None)
                if day_data:
                    for lesson in day_data.get("lessons", []):
                        if _surnames_match(surname, lesson.get("subject", "")):
                            matching_lessons.append(lesson)
            else:
                day_data = next((d for d in days if d.get("name", "").lower() == day_name), None)
                if day_data:
                    for lesson in day_data.get("lessons", []):
                        if lesson.get("week") in ["all", parity_str]:
                            if _surnames_match(surname, lesson.get("subject", "")):
                                matching_lessons.append(lesson)

            if matching_lessons:
                day_results.append({
                    "group": group_name,
                    "uk": meta.get("uk", ""),
                    "form": meta.get("form", ""),
                    "form_slug": meta.get("form_slug", ""),
                    "lessons": matching_lessons,
                })

        day_results.sort(key=_sort_key)

        lines.append(f"\n🔹 <b>{day_name_cap}, {day.strftime('%d.%m')}</b>")
        if not day_results:
            lines.append("😴 Пар нет\n")
        else:
            has_any = True
            time_slots = {}
            for entry in day_results:
                uk = entry["uk"] or "УК"
                form = entry["form"] or "Форма"
                group = entry["group"]
                for lesson in entry["lessons"]:
                    key = (lesson["time_from"], lesson["time_to"], lesson["subject"])
                    if key not in time_slots:
                        time_slots[key] = {}
                    uk_form_key = (uk, form)
                    if uk_form_key not in time_slots[key]:
                        time_slots[key][uk_form_key] = []
                    if group not in time_slots[key][uk_form_key]:
                        time_slots[key][uk_form_key].append(group)
            
            sorted_slots = sorted(time_slots.items(), key=lambda x: x[0][0])
            for (t_from, t_to, subj), uk_form_groups in sorted_slots:
                lines.append(f"⏰ {t_from}-{t_to}")
                for (uk, form), groups in uk_form_groups.items():
                    groups_str = ", ".join(groups)
                    lines.append(f"🏢 <b>{uk}</b> | 📚 <i>{form}</i> | 👥 <b>{groups_str}</b>")
                lines.append(f"📚 {subj}")
            lines.append("")

    if not has_any:
        lines.append(f"\n😔 Занятий <b>{surname}</b> на этой неделе не найдено.")

    return "\n".join(lines).strip()

