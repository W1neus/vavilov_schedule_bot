import datetime
import asyncio
import logging
import json
import os
import sys
import tempfile

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ADMIN_ID, ADMIN_IDS, TZ_SARATOV, schedule_cache
from database import (
    set_user_group, get_user_group,
    get_user_settings, toggle_setting,
    get_user_role, set_user_role, set_teacher_surname, mark_role_selected
)
from tasks import update_schedule_data, get_week_parity
from teacher_search import (
    search_teachers_by_surname,
    find_teacher_in_schedule,
    format_teacher_schedule,
    fetch_all_teachers_from_site,
    find_teacher_on_date,
    format_teacher_day,
    find_teacher_week,
    make_safe_callback_data,
)


root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from image_generator import create_schedule_image_vertical, create_schedule_image_horizontal

logger = logging.getLogger(__name__)

# Ограничиваем одновременную генерацию картинок (Pillow в потоках)
# 3 одновременных генерации достаточно для слабого железа
_image_semaphore = asyncio.Semaphore(3)

# ================= КЕШИРОВАНИЕ РОЛИ ПОЛЬЗОВАТЕЛЯ =================

def _invalidate_role_cache(context):
    """Сбрасывает кешированную роль — вызывать при любом изменении роли/фамилии."""
    context.user_data.pop('cached_role', None)

async def _get_role(user_id, context):
    """Возвращает роль из кеша (context.user_data) или из БД.
    Кеш сбрасывается через _invalidate_role_cache при смене роли/фамилии.
    """
    cached = context.user_data.get('cached_role')
    if cached is None:
        cached = await get_user_role(user_id)
        context.user_data['cached_role'] = cached
    return cached

# ================= ВСПОМОГАТЕЛЬНЫЕ =================

def get_day_name_ru(date_obj=None, week_day_idx=None):
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    if date_obj:
        return days[date_obj.weekday()]
    if week_day_idx is not None:
        return days[week_day_idx]
    return ""

_hierarchy_cache = None
_hierarchy_mtime = 0
_hierarchy_lock = asyncio.Lock()

def _load_json_file(path):
    """Синхронная загрузка JSON — запускается в asyncio.to_thread."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

async def load_full_hierarchy():
    """Кешированная загрузка иерархии расписания.
    Перезагружает файл через поток, чтобы не блокировать event loop.
    """
    global _hierarchy_cache, _hierarchy_mtime
    json_path = os.path.join(root_dir, "data", "schedule.json")
    if not os.path.exists(json_path):
        return []

    try:
        current_mtime = os.path.getmtime(json_path)
        # Быстрая проверка без лока — кеш актуален
        if _hierarchy_cache is not None and current_mtime == _hierarchy_mtime:
            return _hierarchy_cache

        # Кеш устарел — загружаем через поток, берём лок для защиты от гонок
        async with _hierarchy_lock:
            # Двойная проверка после получения лока
            current_mtime = os.path.getmtime(json_path)
            if _hierarchy_cache is not None and current_mtime == _hierarchy_mtime:
                return _hierarchy_cache

            data = await asyncio.to_thread(_load_json_file, json_path)
            _hierarchy_cache = data
            _hierarchy_mtime = current_mtime
            return _hierarchy_cache
    except Exception as e:
        logger.error(f"Error loading hierarchy: {e}")
        return _hierarchy_cache or []

# ================= ИНТЕРФЕЙС ВЫБОРА ГРУППЫ =================

TRANSLATIONS = {
    "uk1": "Учебный комплекс №1",
    "uk2": "Учебный комплекс №2",
    "uk3": "Учебный комплекс №3",
    "institut-genetiki-i-agronomii": "Институт генетики и агрономии",
    "institut-agrobiznesa": "Институт агробизнеса",
    "institut-veterinarnoi-mediciny-i-farmacii": "Институт ветеринарной медицины и фармации",
    "institut-injenerii-i-robototexniki": "Институт инженерии и робототехники",
    "institut-biotexnologii": "Институт биотехнологии",
    "ochnaya-forma-obucheniya": "Очная форма",
    "zaochnaya-forma-obucheniya": "Заочная форма",
    "ochno-zaochnaya-forma-obucheniya": "Очно-заочная форма"
}

def tr(name):
    return TRANSLATIONS.get(name.lower(), name)

def build_search_keyboard(matches, query, page):
    ITEMS_PER_PAGE = 10
    total_pages = (len(matches) - 1) // ITEMS_PER_PAGE + 1
    page_matches = matches[page*ITEMS_PER_PAGE : (page+1)*ITEMS_PER_PAGE]
    
    kb = []
    for m in page_matches:
        kb.append([InlineKeyboardButton(m, callback_data=f"setgroup_{m}")])
        
    nav_row = []
    q_safe = query[:20] 
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"spage_{page-1}_{q_safe}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"spage_{page+1}_{q_safe}"))
        
    if nav_row:
        kb.append(nav_row)
        
    kb.append([InlineKeyboardButton("⬅️ К списку институтов", callback_data="seluk_back")])
    return kb

async def send_group_selection_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await load_full_hierarchy()
    if not data:
        msg = (
            "⏳ <b>Данные расписания сейчас загружаются с сайта университета...</b>\n\n"
            "Это происходит только при первом запуске бота и обычно занимает около 1–2 минут.\n\n"
            "Пожалуйста, подождите немного и отправьте команду /start снова!"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return
        
    kb = []
    for i, uk in enumerate(data):
        kb.append([InlineKeyboardButton(tr(uk["name"]), callback_data=f"seluk_{i}")])
        
    kb.append([InlineKeyboardButton("🔍 Поиск группы по названию", callback_data="search_btn")])
        
    txt = "🏢 <b>Выберите ваш УК:</b>"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def group_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    full_data = await load_full_hierarchy()
    
    if data.startswith("setgroup_"):
        grp = data.replace("setgroup_", "")
        await set_user_group(query.from_user.id, grp)
        await query.edit_message_text(f"✅ Выбрана группа: <b>{grp}</b>", parse_mode=ParseMode.HTML)
        await start(update, context)
        return
        
    if not full_data: return
    
    if data == "search_btn":
        context.user_data['awaiting_search'] = True
        context.user_data['search_msg_id'] = query.message.message_id
        await query.edit_message_text("⌨️ <b>Введите название вашей группы:</b>\n<i>(Например: б-пи-101 или вт-201)</i>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="seluk_back")]]), parse_mode=ParseMode.HTML)
        return
        
    if data == "seluk_back":
        await send_group_selection_start(update, context)
        return
    elif data.startswith("seluk_"):
        uk_idx = int(data.split("_")[1])
        insts = full_data[uk_idx].get("institutes", [])
        kb = []
        for i, inst in enumerate(insts):
            kb.append([InlineKeyboardButton(tr(inst["name"]), callback_data=f"selinst_{uk_idx}_{i}")])
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="seluk_back")])
        await query.edit_message_text("🏫 <b>Выберите институт:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        
    elif data.startswith("selinst_"):
        parts = data.split("_")
        uk_idx, inst_idx = int(parts[1]), int(parts[2])
        forms = full_data[uk_idx]["institutes"][inst_idx].get("forms", [])
        kb = []
        for i, form in enumerate(forms):
            if not form.get("groups"):
                continue
            kb.append([InlineKeyboardButton(tr(form["name"]), callback_data=f"selform_{uk_idx}_{inst_idx}_{i}")])
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"seluk_{uk_idx}")])
        await query.edit_message_text("🎓 <b>Выберите форму обучения:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        
    elif data.startswith("selform_") or data.startswith("page_"):
        parts = data.split("_")
        is_page = data.startswith("page_")
        
        uk_idx = int(parts[1])
        inst_idx = int(parts[2])
        form_idx = int(parts[3])
        page = int(parts[4]) if is_page else 0
        
        groups = full_data[uk_idx]["institutes"][inst_idx]["forms"][form_idx].get("groups", [])
        groups = sorted(groups, key=lambda x: x["name"])
        
        ITEMS_PER_PAGE = 10
        total_pages = (len(groups) - 1) // ITEMS_PER_PAGE + 1
        page_groups = groups[page*ITEMS_PER_PAGE : (page+1)*ITEMS_PER_PAGE]
        
        kb = []
        for g in page_groups:
            kb.append([InlineKeyboardButton(g["name"], callback_data=f"setgroup_{g['name']}")])
            
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"page_{uk_idx}_{inst_idx}_{form_idx}_{page-1}"))
        if total_pages > 1:
            nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"page_{uk_idx}_{inst_idx}_{form_idx}_{page+1}"))
            
        if nav_row:
            kb.append(nav_row)
            
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"selinst_{uk_idx}_{inst_idx}")])
        
        if not groups:
            text = "📭 В данный момент для этой формы обучения нет расписания на сайте."
        else:
            text = "👥 <b>Выберите группу:</b>"
            
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        
    elif data.startswith("spage_"):
        parts = data.split("_", 2)
        page = int(parts[1])
        search_txt = parts[2].lower().replace("-", "")
        
        matches = []
        for g_name in schedule_cache['data'].keys():
            if search_txt in g_name.lower().replace("-", ""):
                matches.append(g_name)
                
        kb = build_search_keyboard(matches, parts[2], page)
        await query.edit_message_text("🔍 <b>Найдено несколько групп:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

# ================= РАСПИСАНИЕ =================

async def generate_schedule_message(user_id, target_date=None, week_offset=None):
    grp = await get_user_group(user_id)
    if not grp: return "⚠️ Группа не выбрана. Нажмите /start", None

    if not schedule_cache['data']: 
        return "⏳ Расписание загружается...", None
    
    grp_data = schedule_cache['data'].get(grp)
    if not grp_data: return f"❌ Данных для {grp} пока нет.", None

    days = grp_data.get("days", [])
    upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M') if schedule_cache['last_update'] else "Неизвестно"
    
    if week_offset is not None:
        now_date = datetime.datetime.now(TZ_SARATOV).date()
        target_week_date = now_date + datetime.timedelta(weeks=week_offset)
        
        is_zaochnaya = any("date" in d for d in days)
        
        if is_zaochnaya:
            sorted_days = sorted(days, key=lambda x: x.get('date', ''))
            
            page = week_offset if week_offset is not None else 0
            per_page = 5
            total_pages = max(1, (len(sorted_days) + per_page - 1) // per_page)
            
            page = max(0, min(page, total_pages - 1))
            
            start_idx = page * per_page
            end_idx = start_idx + per_page
            page_days = sorted_days[start_idx:end_idx]
            
            text = f"🗓 <b>Расписание (стр. {page+1}/{total_pages})</b>\n🎓 {grp}\n🕒 Обновлено: {upd_time}\n{'='*25}"
            for d in page_days:
                date_str = d.get('date', '')
                try:
                    dt = datetime.datetime.fromisoformat(date_str)
                    date_display = dt.strftime('%d.%m.%Y')
                except:
                    date_display = date_str
                    
                text += f"\n\n🔹 <b>{date_display} ({d['name'].capitalize()})</b>"
                if not d.get("lessons"):
                    text += " - Пар нет"
                else:
                    for l in d["lessons"]:
                        text += f"\n⏰ {l['time_from']}-{l['time_to']}\n📚 {l['subject']}\n"
            
            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"sched_week_{page - 1}"))
            if page < total_pages - 1:
                nav_buttons.append(InlineKeyboardButton("След. ➡️", callback_data=f"sched_week_{page + 1}"))
                
            kb = []
            if nav_buttons:
                kb.append(nav_buttons)
            kb.append([InlineKeyboardButton("📅 Сегодня", callback_data=f"sched_{datetime.datetime.now(TZ_SARATOV).date().strftime('%Y-%m-%d')}")] )
            
            return text, InlineKeyboardMarkup(kb)
            
        else:
            # Очная / Очно-заочная форма (с числителем/знаменателем)
            parity_idx = get_week_parity(target_week_date)
            parity_str = "denominator" if parity_idx == 1 else "numerator"
            w_type = "Нижняя" if parity_idx == 1 else "Верхняя"
            
            text = f"🗓 <b>Расписание на неделю ({w_type})</b>\n🎓 {grp}\n🕒 Обновлено: {upd_time}\n{'='*25}"
                
            for d in days:
                day_lessons = []
                for l in d.get("lessons", []):
                    if l['week'] in ["all", parity_str]:
                        day_lessons.append(l)
                        
                text += f"\n\n🔹 <b>{d['name'].capitalize()}</b>"
                if not day_lessons:
                    text += " - Пар нет"
                else:
                    for l in day_lessons:
                        text += f"\n⏰ {l['time_from']}-{l['time_to']}\n📚 {l['subject']}\n"
                        
            kb = [
                [InlineKeyboardButton("⬅️ Пред. Неделя", callback_data=f"sched_week_{week_offset - 1}"),
                 InlineKeyboardButton("След. Неделя ➡️", callback_data=f"sched_week_{week_offset + 1}")],
                [InlineKeyboardButton("Сегодня", callback_data=f"sched_{datetime.datetime.now(TZ_SARATOV).date().strftime('%Y-%m-%d')}")],
                [InlineKeyboardButton("🖼 Расписание картинкой", callback_data=f"img_week_{week_offset}")]
            ]
            return text, InlineKeyboardMarkup(kb)
        
    else:
        is_zaochnaya = any("date" in d for d in days)
        
        if is_zaochnaya:
            target_date_str = target_date.strftime('%Y-%m-%d')
            target_day_data = next((d for d in days if d.get("date") == target_date_str), None)
            
            day_name = get_day_name_ru(target_date)
            date_str = target_date.strftime('%d.%m.%Y')
            
            text = f"🗓 <b>{day_name}</b> | {date_str}\n🎓 {grp}\n🕒 Обновлено: {upd_time}\n{'='*25}"
            
            lessons = target_day_data.get("lessons", []) if target_day_data else []
            
            if not lessons:
                text += "\n😴 Пар нет!"
            else:
                for l in lessons:
                    t_str = f"{l['time_from']} - {l['time_to']}"
                    text += f"\n\n⏰ <b>{t_str}</b>\n📚 {l['subject']}"
            
            valid_dates = []
            for d in days:
                if "date" in d and d.get("lessons"):
                    try:
                        valid_dates.append(datetime.date.fromisoformat(d["date"]))
                    except: pass
            valid_dates.sort()
            
            if valid_dates:
                prev_date = max((d for d in valid_dates if d < target_date), default=target_date - datetime.timedelta(days=1))
                next_date = min((d for d in valid_dates if d > target_date), default=target_date + datetime.timedelta(days=1))
            else:
                prev_date = target_date - datetime.timedelta(days=1)
                next_date = target_date + datetime.timedelta(days=1)
            
            prev_cb = f"sched_{prev_date.strftime('%Y-%m-%d')}"
            next_cb = f"sched_{next_date.strftime('%Y-%m-%d')}"
            today_cb = f"sched_{datetime.datetime.now(TZ_SARATOV).date().strftime('%Y-%m-%d')}"
            
            kb = [
                [InlineKeyboardButton(f"⬅️ {prev_date.strftime('%d.%m')}", callback_data=prev_cb),
                 InlineKeyboardButton(f"{next_date.strftime('%d.%m')} ➡️", callback_data=next_cb)],
                [InlineKeyboardButton("📅 Сегодня", callback_data=today_cb)],
                [InlineKeyboardButton("Все даты", callback_data="sched_week_0")],
                [InlineKeyboardButton("🖼 Расписание картинкой", callback_data=f"img_{target_date_str}")]
            ]
            return text, InlineKeyboardMarkup(kb)
            
        else:
            if target_date.weekday() == 6:  # Если воскресенье
                target_date += datetime.timedelta(days=1)
                
            parity_idx = get_week_parity(target_date)
            w_type = "Нижняя" if parity_idx == 1 else "Верхняя"
            day_name_ru = get_day_name_ru(target_date).lower()
            
            target_day_data = next((d for d in days if d["name"].lower() == day_name_ru), None)
            
            day_name = get_day_name_ru(target_date)
            date_str = target_date.strftime('%d.%m')
            
            text = f"🗓 <b>{day_name}</b> | {date_str}\n🎓 {grp} ({w_type})\n🕒 Обновлено: {upd_time}\n{'='*25}"
            
            lessons = target_day_data.get("lessons", []) if target_day_data else []
            
            
            active_lessons = []
            parity_str = "denominator" if parity_idx == 1 else "numerator"
            for l in lessons:
                if l["week"] in ["all", parity_str]:
                    active_lessons.append(l)
                    
            if not active_lessons:
                text += "\n😴 Пар нет!"
            else:
                for l in active_lessons:
                    t_str = f"{l['time_from']} - {l['time_to']}"
                    text += f"\n\n⏰ <b>{t_str}</b>\n📚 {l['subject']}"
                    
            prev_date = target_date - datetime.timedelta(days=1)
            if prev_date.weekday() == 6: 
                prev_date -= datetime.timedelta(days=1)
    
            next_date = target_date + datetime.timedelta(days=1)
            if next_date.weekday() == 6:  
                next_date += datetime.timedelta(days=1)
            
            prev_cb = f"sched_{prev_date.strftime('%Y-%m-%d')}"
            next_cb = f"sched_{next_date.strftime('%Y-%m-%d')}"
            today_cb = f"sched_{datetime.datetime.now(TZ_SARATOV).date().strftime('%Y-%m-%d')}"
            img_cb = f"img_{target_date.strftime('%Y-%m-%d')}"
            
            kb = [
                [InlineKeyboardButton(f"⬅️ {get_day_name_ru(prev_date)}", callback_data=prev_cb),
                 InlineKeyboardButton(f"{get_day_name_ru(next_date)} ➡️", callback_data=next_cb)],
                [InlineKeyboardButton("📅 Сегодня", callback_data=today_cb)],
                [InlineKeyboardButton("Неделя", callback_data="sched_week_0")],
                [InlineKeyboardButton("🖼 Расписание картинкой", callback_data=img_cb)]
            ]
            return text, InlineKeyboardMarkup(kb)

async def schedule_navigation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("sched_week_"):
        offset = int(query.data.split("_")[2])
        text, reply_markup = await generate_schedule_message(query.from_user.id, week_offset=offset)
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except BadRequest: pass
        return
        
    if query.data.startswith("img_"):
        await send_schedule_image(update, context)
        return
        
    try:
        date_str = query.data.split("_")[1]
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        text, reply_markup = await generate_schedule_message(query.from_user.id, target_date=target_date)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest: pass

async def send_schedule_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    grp = await get_user_group(user_id)
    if not grp: return
    
    grp_data = schedule_cache['data'].get(grp)
    if not grp_data: return
    
    days = grp_data.get("days", [])
    
    msg = await query.message.reply_text("🖼 Генерирую изображение...")

    # tempfile гарантирует удаление файла даже при ошибке
    fd, filepath = tempfile.mkstemp(suffix='.png', prefix=f'sched_{user_id}_')
    os.close(fd)

    is_zaochnaya = any("date" in d for d in days)

    try:
        async with _image_semaphore:
            if query.data.startswith("img_week_"):
                offset = int(query.data.split("_")[2])
                now_date = datetime.datetime.now(TZ_SARATOV).date()
                target_week_date = now_date + datetime.timedelta(weeks=offset)
                parity_idx = get_week_parity(target_week_date)
                filter_parity = "denominator" if parity_idx == 1 else "numerator"

                await asyncio.to_thread(
                    create_schedule_image_horizontal,
                    grp, days, output_filename=filepath, filter_parity=filter_parity, is_zaochnaya=is_zaochnaya
                )
            else:
                date_str = query.data.split("_")[1]
                target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()

                if is_zaochnaya:
                    target_day_data = next((d for d in days if d.get("date") == date_str), None)
                else:
                    day_name_ru = get_day_name_ru(target_date).lower()
                    target_day_data = next((d for d in days if d["name"].lower() == day_name_ru), None)

                if target_day_data:
                    parity_idx = get_week_parity(target_date)
                    filter_parity = "denominator" if parity_idx == 1 else "numerator"

                    await asyncio.to_thread(
                        create_schedule_image_vertical,
                        grp, target_day_data, output_filename=filepath, filter_parity=filter_parity, is_zaochnaya=is_zaochnaya
                    )
                else:
                    await msg.edit_text("❌ Нет данных для этого дня.")
                    return

        await msg.delete()
        with open(filepath, 'rb') as f:
            await context.bot.send_photo(chat_id=user_id, photo=f)

    except Exception as e:
        logger.error(f"Image gen error: {e}")
        try:
            await msg.edit_text("❌ Произошла ошибка генерации.")
        except Exception:
            pass
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# ================= ОСНОВНЫЕ ХЕНДЛЕРЫ =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    from database import ensure_user
    await ensure_user(user_id, username=update.effective_user.username)
    _invalidate_role_cache(context)  # роль могла измениться извне — всегда актуализируем

    msg_func = update.callback_query.message.reply_text if update.callback_query else update.message.reply_text

    is_teacher, teacher_surname, role_selected = await _get_role(user_id, context)

    # Первый вход — предлагаем выбор роли
    if not role_selected:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Я студент", callback_data="role_student")],
            [InlineKeyboardButton("👨‍🏫 Я преподаватель", callback_data="role_teacher")],
        ])
        txt = "👋 <b>Добро пожаловать!</b>\n\nКто вы?"
        await msg_func(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if is_teacher:
        await send_teacher_main_menu(update, context)
        return

    # Режим студента
    grp = await get_user_group(user_id)
    if not grp:
        await send_group_selection_start(update, context)
        return

    kb = [
        [KeyboardButton("📅 Расписание")],
        [KeyboardButton("🔍 Поиск преподавателя")],
        [KeyboardButton("⚙️ Настройки")]
    ]
    if user_id in ADMIN_IDS:
        kb.append([KeyboardButton("🔄 Обновить")])
    upd_time = "..."
    if schedule_cache['last_update']:
        upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M')
    welcome_text = (f"👋 <b>Главное меню</b>\n\n🎓 Твоя группа: <b>{grp}</b>\n🕒 Данные от: <b>{upd_time}</b>\n\n👇 Выбери действие:")
    await msg_func(welcome_text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode=ParseMode.HTML)
    if not schedule_cache['data']:
        asyncio.create_task(update_schedule_data(context))


async def send_teacher_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    _, teacher_surname, _ = await _get_role(user_id, context)
    msg_func = update.callback_query.message.reply_text if update.callback_query else update.message.reply_text

    upd_time = "..."
    if schedule_cache['last_update']:
        upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M')

    kb = [
        [KeyboardButton("📋 Моё расписание")],
        [KeyboardButton("🔍 Поиск преподавателя"), KeyboardButton("👥 Поиск по группе")],
        [KeyboardButton("⚙️ Настройки")],
    ]
    if user_id == ADMIN_ID or user_id in ADMIN_IDS:
        kb.append([KeyboardButton("🔄 Обновить")])

    text = (
        f"👋 <b>Режим преподавателя</b>\n\n"
        f"👤 Фамилия: <b>{teacher_surname or 'не указана'}</b>\n"
        f"🕒 Данные от: <b>{upd_time}</b>\n\n"
        f"👇 Выбери действие:"
    )
    await msg_func(text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode=ParseMode.HTML)
    if not schedule_cache['data']:
        asyncio.create_task(update_schedule_data(context))

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    txt = update.message.text

    # Роль берём из кеша — нет запросов к БД при каждом сообщении
    is_teacher, teacher_surname, role_selected = await _get_role(user_id, context)

    # ======= Режим преподавателя =======
    if is_teacher:
        if txt == "🔄 Обновить" and user_id in ADMIN_IDS:
            msg = await update.message.reply_text("🔄 Запуск обновления...")
            result = await update_schedule_data(context)
            upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M') if schedule_cache['last_update'] else "?"
            await msg.edit_text(f"✅ Обновлено!\n🕒 Данные от: {upd_time}" if result else "❌ Ошибка обновления.")
            return

        if txt == "⚙️ Настройки":
            await send_teacher_settings_menu(update, context)
            return

        if txt == "📋 Моё расписание":
            if not teacher_surname:
                await update.message.reply_text("⚠️ Фамилия не указана. Перейдите в Настройки → Сменить фамилию.")
                return
            if not schedule_cache['data']:
                await update.message.reply_text("⏳ Расписание загружается...")
                return
            now = datetime.datetime.now(TZ_SARATOV).date()
            await _send_teacher_day_view(update.message, context, teacher_surname, now)
            return

        if txt == "🔍 Поиск преподавателя":
            context.user_data['teacher_awaiting_surname_search'] = True
            await update.message.reply_text("⌨️ Введите фамилию преподавателя:")
            return

        if txt == "👥 Поиск по группе":
            context.user_data['teacher_awaiting_group_search'] = True
            await update.message.reply_text("⌨️ Введите название группы:\n<i>(например: вт-21 или Б-ПИ-101)</i>", parse_mode=ParseMode.HTML)
            return

        # Состояния ожидания ввода
        if context.user_data.get('teacher_awaiting_surname_input'):
            context.user_data.pop('teacher_awaiting_surname_input')
            await _handle_teacher_surname_verification(update, context, txt)
            return

        if context.user_data.get('teacher_awaiting_surname_search'):
            context.user_data.pop('teacher_awaiting_surname_search')
            await _handle_teacher_surname_verification(update, context, txt, mode="search")
            return

        if context.user_data.get('teacher_awaiting_group_search'):
            context.user_data.pop('teacher_awaiting_group_search')
            if schedule_cache['data']:
                search_txt = txt.lower().replace("-", "")
                matches = sorted([g for g in schedule_cache['data'] if search_txt in g.lower().replace("-", "")])
                if not matches:
                    await update.message.reply_text("❌ Группа не найдена. Попробуйте ещё раз.")
                elif len(matches) == 1:
                    context.user_data['selected_group_teacher_view'] = matches[0]
                    now = datetime.datetime.now(TZ_SARATOV).date()
                    text, markup = await generate_schedule_message_for_group(matches[0], target_date=now)
                    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    kb = [[InlineKeyboardButton(g, callback_data=f"tgrp_{g}")] for g in matches[:20]]
                    await update.message.reply_text("👥 <b>Найденные группы:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
            return

        return

    # ======= Глобальные состояния (и для преподов, и для студентов) =======
    if txt == "🔍 Поиск преподавателя":
        context.user_data['teacher_awaiting_surname_search'] = True
        await update.message.reply_text("⌨️ Введите фамилию преподавателя:")
        return

    if context.user_data.get('teacher_awaiting_surname_search'):
        context.user_data.pop('teacher_awaiting_surname_search')
        await _handle_teacher_surname_verification(update, context, txt, mode="search")
        return

    # ======= Режим студента =======
    if txt == "Расписание" or txt == "📅 Расписание":
        now = datetime.datetime.now(TZ_SARATOV).date()
        text, markup = await generate_schedule_message(user_id, target_date=now)
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif txt == "Неделя":
        text, markup = await generate_schedule_message(user_id, week_offset=0)
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif txt == "Настройки" or txt == "⚙️ Настройки":
        await send_settings_menu(update, context)
    elif txt == "🔄 Обновить" and user_id in ADMIN_IDS:
        msg = await update.message.reply_text("🔄 Запуск обновления расписания...")
        result = await update_schedule_data(context)
        if result:
            upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M') if schedule_cache['last_update'] else "?"
            await msg.edit_text(f"✅ Расписание обновлено!\n🕒 Данные от: {upd_time}")
        else:
            await msg.edit_text("❌ Ошибка при обновлении расписания. Проверьте логи.")
    else:
        if context.user_data.get('awaiting_search'):
            context.user_data['awaiting_search'] = False
            search_msg_id = context.user_data.get('search_msg_id')
            try: await update.message.delete()
            except: pass
            if schedule_cache['data']:
                matches = []
                search_txt = txt.lower().replace("-", "")
                for g_name in schedule_cache['data'].keys():
                    if search_txt in g_name.lower().replace("-", ""):
                        matches.append(g_name)
                matches.sort()
                if len(matches) == 1 and matches[0].lower() == txt.lower():
                    await set_user_group(user_id, matches[0])
                    text = f"✅ Группа установлена: {matches[0]}"
                    if search_msg_id:
                        try: await context.bot.edit_message_text(chat_id=user_id, message_id=search_msg_id, text=text)
                        except: await update.message.reply_text(text)
                    else:
                        await update.message.reply_text(text)
                    await start(update, context)
                elif matches:
                    kb = build_search_keyboard(matches, txt, 0)
                    text = "🔍 <b>Найдено несколько групп:</b>"
                    if search_msg_id:
                        try: await context.bot.edit_message_text(chat_id=user_id, message_id=search_msg_id, text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
                        except: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
                    else:
                        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
                else:
                    text = "❌ Группа не найдена. Убедитесь в правильности ввода."
                    kb = [[InlineKeyboardButton("🔄 Повторный поиск", callback_data="search_btn")],
                          [InlineKeyboardButton("⬅️ К списку институтов", callback_data="seluk_back")]]
                    if search_msg_id:
                        try: await context.bot.edit_message_text(chat_id=user_id, message_id=search_msg_id, text=text, reply_markup=InlineKeyboardMarkup(kb))
                        except: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
                    else:
                        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def send_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    s = await get_user_settings(user_id)
    grp = await get_user_group(user_id)
    if not s: return
    n20, n10, n5 = s
    
    kb = [
        [InlineKeyboardButton(f"{'✅' if n20 else '❌'} Уведомлять за 20 мин", callback_data="toggle_20")],
        [InlineKeyboardButton(f"{'✅' if n10 else '❌'} Уведомлять за 10 мин", callback_data="toggle_10")],
        [InlineKeyboardButton(f"{'✅' if n5 else '❌'} Уведомлять за 5 мин", callback_data="toggle_5")],
        [InlineKeyboardButton("🎓 Сменить группу", callback_data="change_grp")]
    ]
    text_content = f'''⚙️ <b>Настройки пользователя</b>\n\nВаша группа - <b>{grp}</b>\n\nНастройка уведомлений:\nУведомлнение до пары за 20, 10 и 5 минут.\n\n🟢 Включено | 🔴 Выключено'''

    if update.callback_query:
        try: 
            await update.callback_query.edit_message_text(text_content, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        except BadRequest as e:
            if "Message is not modified" in str(e): pass
            else:
                try:
                    await update.callback_query.message.delete()
                    await update.callback_query.message.reply_text(text_content, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
                except: pass
    else:
        await update.message.reply_text(text_content, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q.data.startswith(("toggle_", "change_grp")):
        return
        
    await q.answer()
    
    user_id = q.from_user.id
    
    if q.data == "change_grp":
        await send_group_selection_start(update, context)
        return
        
    if q.data.startswith("toggle_"):
        await toggle_setting(user_id, q.data.replace("toggle_", "notify_"))
        is_teacher, _, _ = await _get_role(user_id, context)
        if is_teacher:
            await send_teacher_settings_menu(update, context)
        else:
            await send_settings_menu(update, context)


# ================= ВСПОМОГАТЕЛЬНЫЕ ДЛЯ РЕЖИМА ПРЕПОДА =================

async def generate_schedule_message_for_group(group_name: str, target_date=None, week_offset=None):
    """Генерация расписания для конкретной группы (для режима преподавателя)."""
    if not schedule_cache['data']:
        return "⏳ Расписание загружается...", None

    grp_data = schedule_cache['data'].get(group_name)
    if not grp_data:
        return f"❌ Данных для {group_name} пока нет.", None

    days = grp_data.get("days", [])
    upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M') if schedule_cache['last_update'] else "?"
    is_zaochnaya = any("date" in d for d in days)

    if week_offset is not None and is_zaochnaya:
        sorted_days = sorted(days, key=lambda x: x.get('date', ''))
        per_page = 5
        total_pages = max(1, (len(sorted_days) + per_page - 1) // per_page)
        page = max(0, min(week_offset, total_pages - 1))
        page_days = sorted_days[page * per_page:(page + 1) * per_page]

        text = f"🗓 <b>Расписание (стр. {page+1}/{total_pages})</b>\n🎓 {group_name}\n🕒 {upd_time}\n{'='*25}"
        for d in page_days:
            try:
                dt = datetime.date.fromisoformat(d.get('date', ''))
                date_display = dt.strftime('%d.%m.%Y')
            except Exception:
                date_display = d.get('date', '')
            text += f"\n\n🔹 <b>{date_display} ({d['name'].capitalize()})</b>"
            if not d.get("lessons"):
                text += " - Пар нет"
            else:
                for l in d["lessons"]:
                    text += f"\n⏰ {l['time_from']}-{l['time_to']}\n📚 {l['subject']}\n"

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"tgrp_page_{group_name}_{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"tgrp_page_{group_name}_{page+1}"))
        kb = [nav] if nav else []
        return text, InlineKeyboardMarkup(kb) if kb else None

    if target_date is not None:
        if is_zaochnaya:
            target_day = next((d for d in days if d.get("date") == target_date.strftime('%Y-%m-%d')), None)
            day_name = get_day_name_ru(target_date)
            text = f"🗓 <b>{day_name}</b> | {target_date.strftime('%d.%m.%Y')}\n🎓 {group_name}\n🕒 {upd_time}\n{'='*25}"
            lessons = target_day.get("lessons", []) if target_day else []
            if not lessons:
                text += "\n😴 Пар нет!"
            else:
                for l in lessons:
                    text += f"\n\n⏰ <b>{l['time_from']} - {l['time_to']}</b>\n📚 {l['subject']}"
            prev_d = target_date - datetime.timedelta(days=1)
            next_d = target_date + datetime.timedelta(days=1)
            kb = [
                [InlineKeyboardButton(f"⬅️ {prev_d.strftime('%d.%m')}", callback_data=f"tgrpsched_{group_name}_{prev_d}"),
                 InlineKeyboardButton(f"{next_d.strftime('%d.%m')} ➡️", callback_data=f"tgrpsched_{group_name}_{next_d}")],
                [InlineKeyboardButton("Все даты", callback_data=f"tgrp_page_{group_name}_0")]
            ]
            return text, InlineKeyboardMarkup(kb)
        else:
            from tasks import get_week_parity
            parity_idx = get_week_parity(target_date)
            parity_str = "denominator" if parity_idx == 1 else "numerator"
            w_type = "Нижняя" if parity_idx == 1 else "Верхняя"
            day_name_ru = get_day_name_ru(target_date).lower()
            target_day = next((d for d in days if d["name"].lower() == day_name_ru), None)
            lessons = [l for l in (target_day.get("lessons", []) if target_day else []) if l["week"] in ["all", parity_str]]
            text = f"🗓 <b>{get_day_name_ru(target_date)}</b> | {target_date.strftime('%d.%m')}\n🎓 {group_name} ({w_type})\n🕒 {upd_time}\n{'='*25}"
            if not lessons:
                text += "\n😴 Пар нет!"
            else:
                for l in lessons:
                    text += f"\n\n⏰ <b>{l['time_from']} - {l['time_to']}</b>\n📚 {l['subject']}"
            prev_d = target_date - datetime.timedelta(days=1)
            if prev_d.weekday() == 6: prev_d -= datetime.timedelta(days=1)
            next_d = target_date + datetime.timedelta(days=1)
            if next_d.weekday() == 6: next_d += datetime.timedelta(days=1)
            kb = [
                [InlineKeyboardButton(f"⬅️ {get_day_name_ru(prev_d).capitalize()}", callback_data=f"tgrpsched_{group_name}_{prev_d}"),
                 InlineKeyboardButton(f"{get_day_name_ru(next_d).capitalize()} ➡️", callback_data=f"tgrpsched_{group_name}_{next_d}")],
                [InlineKeyboardButton("📅 Сегодня", callback_data=f"tgrpsched_{group_name}_{datetime.datetime.now(TZ_SARATOV).date()}")]
            ]
            return text, InlineKeyboardMarkup(kb)

    return f"❌ Нет данных для {group_name}", None


async def _handle_teacher_surname_verification(update: Update, context: ContextTypes.DEFAULT_TYPE, surname: str, mode: str = "set"):
    """Проверяет фамилию по кадрам сайта и предлагает варианты.
       mode: "set" (установка своей фамилии), "search" (поиск чужой)"""
    user_id = update.effective_chat.id

    # Защита от слишком длинного ввода, который ломает callback_data (максимум 64 байта в Telegram)
    # Префиксы tsearchconf_ (12 байт) или tconfirm_ (9 байт) оставляют максимум 52 байта под surname.
    if len(surname.encode('utf-8')) > 50:
        if mode == "search":
            context.user_data['teacher_awaiting_surname_search'] = True
        else:
            context.user_data['teacher_awaiting_surname_input'] = True

        await update.message.reply_text(
            "❌ <b>Введённый текст слишком длинный.</b>\n"
            "Пожалуйста, введите фамилию преподавателя заново (например: <i>Иванов</i>):",
            parse_mode=ParseMode.HTML
        )
        return

    msg = await update.message.reply_text("🔍 Проверяю по кадрам университета...")

    found = await asyncio.to_thread(search_teachers_by_surname, surname)

    if found:
        kb = []
        for full_name in found[:10]:
            prefix = "tsearch_" if mode == "search" else "tset_"
            callback_val = make_safe_callback_data(prefix, full_name)
            kb.append([InlineKeyboardButton(full_name, callback_data=callback_val)])
        conf_prefix = "tsearchconf_" if mode == "search" else "tconfirm_"
        kb.append([InlineKeyboardButton("✅ Подтвердить как есть", callback_data=f"{conf_prefix}{surname}")])
        action = "Выберите преподавателя:" if mode == "search" else "Выберите себя или подтвердите вручную:"
        await msg.edit_text(
            f"🎓 <b>Найдено на сайте вуза:</b>\n{action}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    else:
        conf_prefix = "tsearchconf_" if mode == "search" else "tconfirm_"
        kb = [[InlineKeyboardButton("✅ Всё равно искать", callback_data=f"{conf_prefix}{surname}")]] if mode == "search" else \
             [[InlineKeyboardButton("✅ Всё равно подтвердить", callback_data=f"{conf_prefix}{surname}")],
              [InlineKeyboardButton("✏️ Ввести заново", callback_data="teacher_change_surname")]]
        action = "Искать расписание?" if mode == "search" else "Вы можете подтвердить вручную:"
        await msg.edit_text(
            f"⚠️ <b>Фамилия «{surname}» не найдена на сайте вуза.</b>\n"
            f"Возможно, опечатка. {action}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )


async def send_teacher_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    _, teacher_surname, _ = await _get_role(user_id, context)
    s = await get_user_settings(user_id)
    if not s:
        n20, n10, n5 = False, False, False
    else:
        n20, n10, n5 = s
        
    kb = [
        [InlineKeyboardButton(f"{'✅' if n20 else '❌'} Уведомлять за 20 мин", callback_data="toggle_20")],
        [InlineKeyboardButton(f"{'✅' if n10 else '❌'} Уведомлять за 10 мин", callback_data="toggle_10")],
        [InlineKeyboardButton(f"{'✅' if n5 else '❌'} Уведомлять за 5 мин", callback_data="toggle_5")],
        [InlineKeyboardButton("✏️ Сменить фамилию", callback_data="teacher_change_surname")],
        [InlineKeyboardButton("🔄 Сменить роль (стать студентом)", callback_data="role_student_reset")],
    ]
    text = f"⚙️ <b>Настройки преподавателя</b>\n\n👤 Фамилия: <b>{teacher_surname or 'не указана'}</b>\n\nНастройка уведомлений:\nУведомление до пары за 20, 10 и 5 минут.\n\n🟢 Включено | 🔴 Выключено"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def _send_teacher_day_view(message, context: ContextTypes.DEFAULT_TYPE, surname: str, target_date: datetime.date, is_edit=False):
    """Отправляет или обновляет сообщение с расписанием препода на день (или сообщает о неделе)."""
    from tasks import get_week_parity
    parity = get_week_parity(target_date)
    results = await asyncio.to_thread(find_teacher_on_date, surname, schedule_cache['data'], target_date, parity)
    
    text = format_teacher_day(surname, results, target_date)
    
    prev_d = target_date - datetime.timedelta(days=1)
    if prev_d.weekday() == 6:  # Пропускаем воскресенье
        prev_d -= datetime.timedelta(days=1)
        
    next_d = target_date + datetime.timedelta(days=1)
    if next_d.weekday() == 6:  # Пропускаем воскресенье
        next_d += datetime.timedelta(days=1)

    now = datetime.datetime.now(TZ_SARATOV).date()
    
    kb = [
        [
            InlineKeyboardButton(f"⬅️ {get_day_name_ru(prev_d).capitalize()}", callback_data=f"ts_d_{prev_d.isoformat()}_{surname}"),
            InlineKeyboardButton(f"{get_day_name_ru(next_d).capitalize()} ➡️", callback_data=f"ts_d_{next_d.isoformat()}_{surname}")
        ],
        [
            InlineKeyboardButton("📅 Сегодня", callback_data=f"ts_d_{now.isoformat()}_{surname}")
        ],
        [
            InlineKeyboardButton("🗓 Неделя", callback_data=f"ts_w_{target_date.isoformat()}_{surname}")
        ],
        [
            InlineKeyboardButton("🖼 Расписание картинкой", callback_data=f"timg_d_{target_date.isoformat()}_{surname}")
        ]
    ]
    
    markup = InlineKeyboardMarkup(kb)
    
    if is_edit:
        try:
            await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                pass
    else:
        await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def teacher_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все callback-кнопки режима преподавателя."""
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    # Выбор роли
    if data == "role_student":
        await set_user_role(user_id, 0)
        _invalidate_role_cache(context)
        await q.edit_message_text("✅ Режим студента активирован!")
        await start(update, context)
        return

    if data == "role_teacher":
        await set_user_role(user_id, 1)
        _invalidate_role_cache(context)
        context.user_data['teacher_awaiting_surname_input'] = True
        await q.edit_message_text(
            "👨‍🏫 <b>Режим преподавателя</b>\n\nВведите вашу фамилию для поиска в расписании:",
            parse_mode=ParseMode.HTML
        )
        return

    if data == "role_student_reset":
        await set_user_role(user_id, 0)
        _invalidate_role_cache(context)
        await q.edit_message_text("✅ Роль изменена на студента!")
        await start(update, context)
        return

    # Смена фамилии
    if data == "teacher_change_surname":
        context.user_data['teacher_awaiting_surname_input'] = True
        await q.edit_message_text("✏️ Введите новую фамилию:")
        return

    # Выбор преподавателя из списка (tset_Фамилия)
    if data.startswith("tset_"):
        surname = data[5:]
        await set_teacher_surname(user_id, surname)
        _invalidate_role_cache(context)  # teacher_surname входит в кешированный кортеж
        await q.edit_message_text(f"✅ Фамилия установлена: <b>{surname}</b>", parse_mode=ParseMode.HTML)
        await send_teacher_main_menu(update, context)
        return

    # Ручное подтверждение фамилии (tconfirm_Фамилия)
    if data.startswith("tconfirm_"):
        surname = data[9:]
        await set_teacher_surname(user_id, surname)
        _invalidate_role_cache(context)  # teacher_surname входит в кешированный кортеж
        await q.edit_message_text(f"✅ Фамилия сохранена: <b>{surname}</b>", parse_mode=ParseMode.HTML)
        await send_teacher_main_menu(update, context)
        return

    # Показ расписания группы (tgrp_НазваниеГруппы)
    if data.startswith("tgrp_page_"):
        parts = data.split("_")
        page = int(parts[-1])
        group_name = "_".join(parts[2:-1])
        text, markup = await generate_schedule_message_for_group(group_name, week_offset=page)
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        return

    if data.startswith("tgrp_"):
        group_name = data[5:]
        now = datetime.datetime.now(TZ_SARATOV).date()
        text, markup = await generate_schedule_message_for_group(group_name, target_date=now)
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        return

    # Навигация по расписанию группы (tgrpsched_ГРУППА_ДАТА)
    if data.startswith("tgrpsched_"):
        parts = data.split("_")
        date_str = parts[-1]
        group_name = "_".join(parts[1:-1])
        try:
            target_date = datetime.date.fromisoformat(date_str)
            text, markup = await generate_schedule_message_for_group(group_name, target_date=target_date)
            await q.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except (BadRequest, ValueError):
            pass
        return

    # Навигация по расписанию преподавателя (день)
    if data.startswith("ts_d_"):
        parts = data.split("_", 3)
        if len(parts) == 4:
            date_str = parts[2]
            surname = parts[3]
            try:
                target_date = datetime.date.fromisoformat(date_str)
                await _send_teacher_day_view(q.message, context, surname, target_date, is_edit=True)
            except ValueError:
                pass
        return

    # Навигация по расписанию преподавателя (неделя)
    if data.startswith("ts_w_"):
        parts = data.split("_", 3)
        if len(parts) == 4:
            date_str = parts[2]
            surname = parts[3]
            try:
                target_date = datetime.date.fromisoformat(date_str)
                # Находим понедельник этой недели
                monday = target_date - datetime.timedelta(days=target_date.weekday())
                from tasks import get_week_parity
                parity = get_week_parity(monday)
                
                text = await asyncio.to_thread(find_teacher_week, surname, schedule_cache['data'], monday, parity)
                
                # Кнопки для недели
                prev_w = monday - datetime.timedelta(days=7)
                next_w = monday + datetime.timedelta(days=7)
                now = datetime.datetime.now(TZ_SARATOV).date()
                kb = [
                    [
                        InlineKeyboardButton("⬅️ Пред. неделя", callback_data=f"ts_w_{prev_w.isoformat()}_{surname}"),
                        InlineKeyboardButton("След. неделя ➡️", callback_data=f"ts_w_{next_w.isoformat()}_{surname}")
                    ],
                    [
                        InlineKeyboardButton("📅 День", callback_data=f"ts_d_{now.isoformat()}_{surname}")
                    ],
                    [
                        InlineKeyboardButton("🖼 Расписание картинкой", callback_data=f"timg_w_{monday.isoformat()}_{surname}")
                    ]
                ]
                await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
            except (BadRequest, ValueError):
                pass
        return
        
    if data.startswith("timg_"):
        await send_teacher_schedule_image(update, context)
        return

    # Обработка поиска преподавателя (когда студент или препод ищет чужое расписание)
    if data.startswith("tsearch_") or data.startswith("tsearchconf_"):
        prefix_len = 8 if data.startswith("tsearch_") else 12
        surname = data[prefix_len:]
        now = datetime.datetime.now(TZ_SARATOV).date()
        await _send_teacher_day_view(q.message, context, surname, now, is_edit=True)
        return



# ================= ADMIN: /setrole =================

async def setrole_handler(update, context):
    from telegram.constants import ParseMode
    caller_id = update.effective_user.id
    if caller_id not in ADMIN_IDS:
        await update.message.reply_text('⛔ У вас нет прав для этой команды.')
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            'Использование:\n'
            '/setrole student\n'
            '/setrole teacher [Фамилия]\n'
            '/setrole <user_id> student\n'
            '/setrole <user_id> teacher [Фамилия]'
        )
        return
    target_id = caller_id
    role_arg_idx = 0
    if args[0].lstrip('-').isdigit():
        target_id = int(args[0])
        role_arg_idx = 1
    elif args[0].startswith('@'):
        from database import get_user_id_by_username
        found_id = await get_user_id_by_username(args[0])
        if not found_id:
            await update.message.reply_text(f"❌ Пользователь {args[0]} не найден в базе данных.")
            return
        target_id = found_id
        role_arg_idx = 1

    if role_arg_idx >= len(args):
        await update.message.reply_text('Укажите роль: student или teacher')
        return
    role_str = args[role_arg_idx].lower()
    surname = args[role_arg_idx + 1] if role_arg_idx + 1 < len(args) else None
    who = str(target_id) if target_id != caller_id else 'Vy'
    from database import ensure_user
    if role_str == 'student':
        await ensure_user(target_id)
        await set_user_role(target_id, 0)
        await mark_role_selected(target_id)
        await update.message.reply_text(who + ' теперь студент.', parse_mode=ParseMode.HTML)
    elif role_str == 'teacher':
        await ensure_user(target_id)
        await set_user_role(target_id, 1)
        await mark_role_selected(target_id)
        if surname:
            await set_teacher_surname(target_id, surname)
        msg_text = who + ' теперь преподаватель' + ((', фамилия: ' + surname) if surname else '') + '.'
        await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text('Неизвестная роль. Используйте student или teacher.')

async def send_teacher_schedule_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    
    parts = data.split("_", 3)
    if len(parts) < 4: return
    
    mode = parts[1] # 'd' (день) или 'w' (неделя)
    date_str = parts[2]
    surname = parts[3]
    
    msg = await query.message.reply_text("🖼 Генерирую изображение...")

    # tempfile гарантирует удаление файла даже при ошибке
    fd, filepath = tempfile.mkstemp(suffix='.png', prefix=f'tsched_{user_id}_')
    os.close(fd)
    
    try:
        target_date = datetime.date.fromisoformat(date_str)

        async with _image_semaphore:
            if mode == 'd':
                from tasks import get_week_parity
                parity = get_week_parity(target_date)
                results = await asyncio.to_thread(find_teacher_on_date, surname, schedule_cache['data'], target_date, parity)

                day_name = get_day_name_ru(target_date).lower()
                day_obj = {"name": f"{day_name} ({target_date.strftime('%d.%m.%Y')})", "lessons": []}

                for res in results:
                    grp = res["group"]
                    for l in res.get("lessons", []):
                        l_copy = dict(l)
                        l_copy["subject"] = f"[{grp}] {l_copy['subject']}"
                        day_obj["lessons"].append(l_copy)

                day_obj["lessons"].sort(key=lambda x: x["time_from"])

                if not day_obj["lessons"]:
                    await msg.edit_text("❌ Нет пар в этот день.")
                    return

                await asyncio.to_thread(
                    create_schedule_image_vertical,
                    f"Преподаватель {surname}", day_obj, output_filename=filepath, filter_parity=None, is_zaochnaya=True
                )

            elif mode == 'w':
                from tasks import get_week_parity
                monday = target_date - datetime.timedelta(days=target_date.weekday())
                parity = get_week_parity(monday)

                days_list = []
                for i in range(6):
                    d = monday + datetime.timedelta(days=i)
                    day_name = get_day_name_ru(d).lower()
                    day_obj = {"name": f"{day_name} ({d.strftime('%d.%m.%Y')})", "lessons": []}

                    results = await asyncio.to_thread(find_teacher_on_date, surname, schedule_cache['data'], d, parity)
                    for res in results:
                        grp = res["group"]
                        for l in res.get("lessons", []):
                            l_copy = dict(l)
                            l_copy["subject"] = f"[{grp}] {l_copy['subject']}"
                            day_obj["lessons"].append(l_copy)

                    day_obj["lessons"].sort(key=lambda x: x["time_from"])
                    if day_obj["lessons"]:
                        days_list.append(day_obj)

                if not days_list:
                    await msg.edit_text("❌ Нет пар на этой неделе.")
                    return

                await asyncio.to_thread(
                    create_schedule_image_horizontal,
                    f"Преподаватель {surname}", days_list, output_filename=filepath, filter_parity=None, is_zaochnaya=True
                )

        await msg.delete()
        with open(filepath, 'rb') as f:
            await context.bot.send_photo(chat_id=user_id, photo=f)

    except Exception as e:
        logger.error(f"Teacher image gen error: {e}")
        try:
            await msg.edit_text("❌ Произошла ошибка генерации.")
        except Exception:
            pass
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

