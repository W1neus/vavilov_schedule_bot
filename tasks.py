import asyncio
import datetime
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import TZ_SARATOV, schedule_cache, CLASS_TIMES, DB_FILE, MAX_WORKERS
from database import get_users_for_change_notification
from parser import find_all_pdf_links, parse_pdf_task, is_schedule_changed, get_week_parity

logger = logging.getLogger(__name__)
update_lock = asyncio.Lock()

# ================= АСИНХРОННАЯ ОБРАТКА (ЛОГИКА ОБНОВЛЕНИЯ) =================

async def notify_users_about_change(context: ContextTypes.DEFAULT_TYPE):
    users = get_users_for_change_notification()
    if not users: return
    msg = "⚠️ <b>Внимание! Расписание изменилось.</b>"
    for uid in users:
        try: await context.bot.send_message(uid, msg, parse_mode=ParseMode.HTML)
        except: pass

async def update_schedule_data(context: ContextTypes.DEFAULT_TYPE = None):

    if update_lock.locked():
        logger.info("Обновление уже запущено, пропускаю.")
        return False

    async with update_lock:

        
        logger.info("Поиск PDF на странице университета...")
        links = await asyncio.to_thread(find_all_pdf_links)
        
        if not links: 
            logger.warning("Ссылки на PDF не найдены.")
            return False
            
        logger.info(f"Найдено ссылок на PDF: {len(links)}. Начинаем параллельную обработку ({MAX_WORKERS} потоков)...")

        
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            tasks = [loop.run_in_executor(executor, parse_pdf_task, ln) for ln in links]
            results = await asyncio.gather(*tasks)
        
        
        final_data = { "Б-ПИ-101": {0: {}, 1: {}}, "Б-ПИ-102": {0: {}, 1: {}} }
        
        success_count = 0
        for res in results:
            if not res: continue
            success_count += 1
            for g in final_data: # g = Группа
                for w in [0, 1]: # w = Неделя
                    for day_idx, pairs in res[g][w].items():
                        if day_idx not in final_data[g][w]: 
                            final_data[g][w][day_idx] = {}
                        final_data[g][w][day_idx].update(pairs)

        if success_count > 0:
            old_data = schedule_cache.get('data')
            changed = is_schedule_changed(old_data, final_data)
            
            schedule_cache['data'] = final_data
            schedule_cache['last_update'] = datetime.datetime.now(TZ_SARATOV)
            
           

            if changed and old_data and context:
                await notify_users_about_change(context)
            return True
            
        return False

async def notifier(context: ContextTypes.DEFAULT_TYPE):
    if not schedule_cache['data']: return
    now = datetime.datetime.now(TZ_SARATOV)
    parity = get_week_parity()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, group_name, notify_20, notify_10, notify_5 FROM users WHERE is_allowed = 1')
        users = cursor.fetchall()
    for u in users:
        uid, g_name, n20, n10, n5 = u
        if not g_name: continue
        grp_data = schedule_cache['data'].get(g_name)
        if not grp_data: continue
        today_sched = grp_data.get(parity, {}).get(now.weekday(), {})
        if not today_sched: continue
        for p_num, times in CLASS_TIMES.items():
            h, m = times['start']
            start_dt = now.replace(hour=h, minute=m, second=0)
            if now > start_dt: continue
            minutes = int((start_dt - now).total_seconds() / 60)
            subj = today_sched.get(p_num)
            if not subj: continue
            txt = None
            if minutes == 20 and n20: txt = f"🔔 <b>20 мин до пары:</b>\n{subj}"
            elif minutes == 10 and n10: txt = f"⚠️ <b>10 мин до пары:</b>\n{subj}"
            elif minutes == 5 and n5: txt = f"🏃 <b>5 мин до пары:</b>\n{subj}"
            if txt:
                try: await context.bot.send_message(uid, txt, parse_mode=ParseMode.HTML)
                except: pass

