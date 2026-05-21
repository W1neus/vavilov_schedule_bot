import asyncio
import datetime
import logging
import sqlite3
import json
import os
import sys

from telegram.error import RetryAfter, Forbidden, BadRequest
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import TZ_SARATOV, schedule_cache, DB_FILE

logger = logging.getLogger(__name__)
update_lock = asyncio.Lock()

def load_schedule_from_json():
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = os.path.join(root_dir, "data", "schedule.json")
    
    if not os.path.exists(json_path):
        return None
        
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        final_data = {}

        FORM_NAMES = {
            "ochnaya-forma-obucheniya": "Очная форма",
            "zaochnaya-forma-obucheniya": "Заочная форма",
            "ochno-zaochnaya-forma-obucheniya": "Очно-заочная форма",
        }
        UK_NAMES = {
            "uk1": "Учебный комплекс №1",
            "uk2": "Учебный комплекс №2",
            "uk3": "Учебный комплекс №3",
        }

        for uk in data:
            uk_slug = uk.get("name", "")
            uk_name = UK_NAMES.get(uk_slug, uk_slug)
            for inst in uk.get("institutes", []):
                for form in inst.get("forms", []):
                    form_slug = form.get("name", "")
                    form_name = FORM_NAMES.get(form_slug, form_slug)
                    for group in form.get("groups", []):
                        g_name = group["name"]
                        schedule = group["schedule"]

                        schedule["_meta"] = {
                            "uk": uk_name,
                            "uk_slug": uk_slug,
                            "form": form_name,
                            "form_slug": form_slug,
                        }

                        final_data[g_name] = schedule

        schedule_cache['data'] = final_data
        
        mtime = os.path.getmtime(json_path)
        dt = datetime.datetime.fromtimestamp(mtime, tz=TZ_SARATOV)
        schedule_cache['last_update'] = dt
        
        return True
    except Exception as e:
        logger.error(f"Error loading JSON: {e}")
        return False

async def update_schedule_data(context: ContextTypes.DEFAULT_TYPE = None):
    if update_lock.locked():
        logger.info("Обновление уже запущено, пропускаю.")
        return False

    async with update_lock:
        logger.info("Запуск парсера...")
        
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scraper_path = os.path.join(root_dir, "main_scraper.py")
        
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", scraper_path,
            cwd=root_dir
        )
        await process.wait()
        
        if process.returncode == 0:
            logger.info("Парсинг успешно завершен.")
            load_schedule_from_json()
            return True
        else:
            logger.error(f"Ошибка парсинга (код {process.returncode}).")
            
        return False

# ================= УВЕДОМЛЕНИЯ ПЕРЕД ПАРОЙ =================

from config import SEMESTER_START_DATE

def get_week_parity(target_date=None):
    if not target_date:
        target_date = datetime.datetime.now(TZ_SARATOV).date()
    
    start_monday = SEMESTER_START_DATE - datetime.timedelta(days=SEMESTER_START_DATE.weekday())
    target_monday = target_date - datetime.timedelta(days=target_date.weekday())
    
    week_diff = (target_monday - start_monday).days // 7
    return week_diff % 2

async def safe_send_message(bot, user_id, text, parse_mode):
    while True:
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode=parse_mode)
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except (Forbidden, BadRequest):
            return
        except Exception as e:
            logger.error(f"Error sending message to {user_id}: {e}")
            return

def get_day_name_ru(date_obj):
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return days_ru[date_obj.weekday()]

_daily_teacher_cache = {}
_daily_teacher_cache_date = None

async def notifier(context: ContextTypes.DEFAULT_TYPE):
    global _daily_teacher_cache, _daily_teacher_cache_date

    if not schedule_cache['data']: return
    now = datetime.datetime.now(TZ_SARATOV)
    day_name = get_day_name_ru(now.date()).lower()
    parity = get_week_parity(now.date())
    parity_str = "denominator" if parity == 1 else "numerator"
    
    if _daily_teacher_cache_date != now.date():
        _daily_teacher_cache.clear()
        _daily_teacher_cache_date = now.date()

    from database import get_all_users_for_notifier
    users = await get_all_users_for_notifier()
        
    pending_notifications = []

    for u in users:
        uid, g_name, n20, n10, n5, is_teacher, teacher_surname = u
        
        valid_lessons = []
        
        if is_teacher and teacher_surname:
            if teacher_surname not in _daily_teacher_cache:
                from teacher_search import find_teacher_on_date
                results = await asyncio.to_thread(find_teacher_on_date, teacher_surname, schedule_cache['data'], now.date(), parity)
                flat_lessons = []
                for r in results:
                    group_name = r.get("group", "")
                    uk_name = r.get("uk", "")
                    for l in r.get('lessons', []):
                        l_copy = dict(l)
                        l_copy['group'] = group_name
                        l_copy['uk'] = uk_name
                        flat_lessons.append(l_copy)
                _daily_teacher_cache[teacher_surname] = flat_lessons
                
            valid_lessons = _daily_teacher_cache[teacher_surname]
            
        elif g_name:
            grp_data = schedule_cache['data'].get(g_name)
            if not grp_data: continue
            
            days_data = grp_data.get("days", [])
            is_zaochnaya = any("date" in d for d in days_data)
            
            if is_zaochnaya:
                now_date_str = now.date().strftime('%Y-%m-%d')
                target_day = next((d for d in days_data if d.get("date") == now_date_str), None)
                if not target_day: continue
                valid_lessons = target_day.get("lessons", [])
            else:
                target_day = next((d for d in days_data if d["name"].lower() == day_name), None)
                if not target_day: continue
                valid_lessons = [l for l in target_day.get("lessons", []) if l['week'] in ["all", parity_str]]
        else:
            continue
            
        if not valid_lessons:
            continue
            
        for l in valid_lessons:
            t_from = l['time_from']
            subj = l['subject']
            
            try:
                l_time = datetime.datetime.strptime(t_from, "%H:%M").time()
                target_dt = datetime.datetime.combine(now.date(), l_time)
                target_dt = TZ_SARATOV.localize(target_dt)
                
                diff = (target_dt - now).total_seconds() / 60
                
                txt = None
                if is_teacher:
                    grp = l.get("group", "")
                    uk = l.get("uk", "")
                    if 19.5 <= diff <= 20.5 and n20:
                        txt = f"👨‍🏫 <b>20 мин до пары:</b>\n👥 Группа: <b>{grp}</b>\n🏢 {uk}\n📚 {subj} ({t_from})"
                    elif 9.5 <= diff <= 10.5 and n10:
                        txt = f"⚠️ <b>10 мин до пары:</b>\n👥 Группа: <b>{grp}</b>\n🏢 {uk}\n📚 {subj} ({t_from})"
                    elif 4.5 <= diff <= 5.5 and n5:
                        txt = f"🏃 <b>5 мин до пары:</b>\n👥 Группа: <b>{grp}</b>\n🏢 {uk}\n📚 {subj} ({t_from})"
                else:
                    if 19.5 <= diff <= 20.5 and n20:
                        txt = f"🔔 <b>20 мин до пары:</b>\n{subj} ({t_from})"
                    elif 9.5 <= diff <= 10.5 and n10:
                        txt = f"⚠️ <b>10 мин до пары:</b>\n{subj} ({t_from})"
                    elif 4.5 <= diff <= 5.5 and n5:
                        txt = f"🏃 <b>5 мин до пары:</b>\n{subj} ({t_from})"
                    
                if txt:
                    pending_notifications.append((uid, txt))
            except Exception as e:
                logger.error(f"Time parsing error: {e}")

    # Отправка уведомлений с задержкой во избежание превышения лимитов Telegram (25-30 сообщений в секунду)
    for uid, txt in pending_notifications:
        await safe_send_message(context.bot, uid, txt, ParseMode.HTML)
        await asyncio.sleep(0.04)



