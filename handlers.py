import datetime
import asyncio
import logging
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import (
    ADMIN_ID, TZ_SARATOV, schedule_cache, CLASS_TIMES, 
    TIME_START_TO_PAIR_NUM
)

logger = logging.getLogger(__name__)
from database import (
    check_access, grant_access, get_user_group, set_user_group, 
    get_user_settings, toggle_setting, revoke_access_delete_user,
    get_allowed_users_ids, get_all_users_info, get_user_style
)
from parser import get_week_parity
from tasks import update_schedule_data

# ================= ИНТЕРФЕЙС =================

async def send_group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("Б-ПИ-101", callback_data="setgroup_Б-ПИ-101")],
        [InlineKeyboardButton("Б-ПИ-102", callback_data="setgroup_Б-ПИ-102")]
    ]
    txt = " <b>Выберите вашу группу:</b>"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def group_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    grp = query.data.split("_")[1]
    set_user_group(query.from_user.id, grp)
    use_new_style = get_user_style(query.from_user.id)
    if use_new_style:
        await query.edit_message_text(f" <tg-emoji emoji-id='5427009714745517609'>✅</tg-emoji> Выбрана группа: <b>{grp}</b>", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(f"✅ Выбрана группа: <b>{grp}</b>", parse_mode=ParseMode.HTML)
    await start(update, context)

def get_day_name_ru(date_obj):
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[date_obj.weekday()]

async def generate_schedule_message(user_id, target_date):
    if target_date.weekday() == 6:  # Если воскресенье
        target_date += datetime.timedelta(days=1)  # Переходим на понедельник

    grp = get_user_group(user_id)
    if not grp: return "⚠️ Группа не выбрана. Нажмите /start", None
    
    use_new_style = get_user_style(user_id)

    if not schedule_cache['data']: 
        return ("<tg-emoji emoji-id='5451646226975955576'>⌛️</tg-emoji> Расписание загружается..." if use_new_style else "⏳ Расписание загружается..."), None
    
    grp_data = schedule_cache['data'].get(grp)
    if not grp_data: return f"❌ Данных для {grp} пока нет.", None

    parity = get_week_parity(target_date)
    weekday = target_date.weekday()
    w_type = "Нижняя" if parity == 1 else "Верхняя"
    pairs = grp_data.get(parity, {}).get(weekday, {})
    
    day_name = get_day_name_ru(target_date)
    date_str = target_date.strftime('%d.%m')
    
    upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M') if schedule_cache['last_update'] else "Неизвестно"
    
    if use_new_style:
        text = f"<tg-emoji emoji-id='5274055917766202507'>🗓</tg-emoji> <b>{day_name}</b> | {date_str}\n<tg-emoji emoji-id='5375163339154399459'>🎓</tg-emoji> {grp} ({w_type})\n<tg-emoji emoji-id='5451646226975955576'>⌛️</tg-emoji> Обновлено: {upd_time}\n{'='*25}"
    else:
        text = f"🗓 <b>{day_name}</b> | {date_str}\n🎓 {grp} ({w_type})\n🕒 Обновлено: {upd_time}\n{'='*25}"
    
    if not pairs:
        text += ("\n<tg-emoji emoji-id='5404743771059395517'>😴</tg-emoji> Пар нет!" if use_new_style else "\n😴 Пар нет!")
    else:
        for p in sorted(pairs.keys()):
            times = CLASS_TIMES.get(p)
            t_str = f"{times['start'][0]:02}:{times['start'][1]:02} - {times['end'][0]:02}:{times['end'][1]:02}" if times else "??"
            if use_new_style:
                text += f"\n\n<tg-emoji emoji-id='5413704112220949842'>⏰</tg-emoji> <b>{t_str}</b>\n<tg-emoji emoji-id='5373098009640836781'>📚</tg-emoji> {pairs[p]}"
            else:
                text += f"\n\n⏰ <b>{t_str}</b>\n📚 {pairs[p]}"
            
    prev_date = target_date - datetime.timedelta(days=1)
    if prev_date.weekday() == 6: 
        prev_date -= datetime.timedelta(days=1)

    next_date = target_date + datetime.timedelta(days=1)
    if next_date.weekday() == 6:  
        next_date += datetime.timedelta(days=1)
    
    prev_cb = f"sched_{prev_date.strftime('%Y-%m-%d')}"
    next_cb = f"sched_{next_date.strftime('%Y-%m-%d')}"
    today_cb = f"sched_{datetime.datetime.now(TZ_SARATOV).date().strftime('%Y-%m-%d')}"
    
    if use_new_style:
        kb = [
            [InlineKeyboardButton(f"⬅️ {get_day_name_ru(prev_date)}", callback_data=prev_cb, api_kwargs={"style": "primary"}),
             InlineKeyboardButton(f"{get_day_name_ru(next_date)} ➡️", callback_data=next_cb, api_kwargs={"style": "primary"})],
            [InlineKeyboardButton("Сегодня", callback_data=today_cb, api_kwargs={"icon_custom_emoji_id": "5274055917766202507"})]
        ]
    else:
        kb = [
            [InlineKeyboardButton(f"⬅️ {get_day_name_ru(prev_date)}", callback_data=prev_cb),
             InlineKeyboardButton(f"{get_day_name_ru(next_date)} ➡️", callback_data=next_cb)],
            [InlineKeyboardButton("📅 Сегодня", callback_data=today_cb)]
        ]
    return text, InlineKeyboardMarkup(kb)

async def schedule_navigation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        date_str = query.data.split("_")[1]
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        text, reply_markup = await generate_schedule_message(query.from_user.id, target_date)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest: pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    msg_func = update.callback_query.message.reply_text if update.callback_query else update.message.reply_text
    use_new_style = get_user_style(user_id)
    
    if not check_access(user_id):
        txt = "⛔️ <b>Доступ запрещен.</b>" if not use_new_style else " <tg-emoji emoji-id='5260293700088511294'>⛔️</tg-emoji> <b>Доступ запрещен.</b>"
        await msg_func(f"{txt}\nID: <code>{user_id}</code> \n для получения доступа перешлите сообщение администратору - @Grdfree", parse_mode=ParseMode.HTML)
        return
    grant_access(user_id)
    grp = get_user_group(user_id)
    if not grp:
        await send_group_selection(update, context)
        return
    
    if use_new_style:
        kb = [[KeyboardButton(text = "Расписание", api_kwargs={"style": "primary", "icon_custom_emoji_id": "5274055917766202507"})], [KeyboardButton("Настройки", api_kwargs={"icon_custom_emoji_id": "5818705028424141605"})]]
    else:
        kb = [[KeyboardButton("📅 Расписание")], [KeyboardButton("⚙️ Настройки")]]

    if user_id == ADMIN_ID: kb.append([KeyboardButton("🔄 Обновить")])
    upd_time = "..."
    if schedule_cache['last_update']:
        upd_time = schedule_cache['last_update'].strftime('%d.%m %H:%M')
        
    if use_new_style:
        welcome_text = (f"<tg-emoji emoji-id='5472055112702629499'>👋</tg-emoji> <b>Главное меню</b>\n\n<tg-emoji emoji-id='5375163339154399459'>🎓</tg-emoji> Твоя группа: <b>{grp}</b>\n<tg-emoji emoji-id='5451646226975955576'>⌛️</tg-emoji> Данные от: <b>{upd_time}</b>\n\n<tg-emoji emoji-id='5406745015365943482'>⬇️</tg-emoji>")
    else:
        welcome_text = (f"👋 <b>Главное меню</b>\n\n🎓 Твоя группа: <b>{grp}</b>\n🕒 Данные от: <b>{upd_time}</b>\n\n👇 Выбери действие:")
        
    await msg_func(welcome_text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode=ParseMode.HTML)
    if not schedule_cache['data']: 
        asyncio.create_task(update_schedule_data(context))

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    if not check_access(user_id): return
    txt = update.message.text
    if txt == "Расписание" or txt == "📅 Расписание":
        now = datetime.datetime.now(TZ_SARATOV).date()
        text, markup = await generate_schedule_message(user_id, now)
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif txt == "Настройки" or txt == "⚙️ Настройки": 
        await send_settings_menu(update, context)
    elif txt == "🔄 Обновить":
        if user_id != ADMIN_ID: return
        msg = await update.message.reply_text("⏳ Запущено фоновое обновление расписания...")
        
        res = await update_schedule_data(context)
        t = datetime.datetime.now(TZ_SARATOV).strftime('%H:%M')
        if res:
            await msg.edit_text(f"✅ Обновление завершено! ({t})")
        else:
            await msg.edit_text(f"❌ Обновление не удалось или уже запущено.")

async def send_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    s = get_user_settings(user_id)
    if not s: return
    n20, n10, n5, n_ch, use_new_style = s
    
    if use_new_style:
        kb = [
            [InlineKeyboardButton(f"Уведомлять за 20 мин", callback_data="toggle_20", 
                                  api_kwargs={"style": "success" if n20 else "danger"})],
            [InlineKeyboardButton(f"Уведомлять за 10 мин", callback_data="toggle_10", 
                                  api_kwargs={"style": "success" if n10 else "danger"})],
            [InlineKeyboardButton(f"Уведомлять за 5 мин", callback_data="toggle_5", 
                                  api_kwargs={"style": "success" if n5 else "danger"})],
            [InlineKeyboardButton(f"Уведомлять об изменениях", callback_data="toggle_changes", 
                                  api_kwargs={"style": "success" if n_ch else "danger"})],
            [InlineKeyboardButton("Сменить группу", callback_data="change_grp", api_kwargs={"icon_custom_emoji_id": "5375163339154399459", "style": "primary"})],
            [InlineKeyboardButton("Старый стиль", callback_data="toggle_new_style")]
        ]
        text_content = '''<tg-emoji emoji-id='5818705028424141605'>⚙️</tg-emoji> <b>Настройки пользователя</b>\n\nНастройка уведомлений:\nУведомлнение до пары за 20,10 и 5 минут, а также уведомление о изменении в расписании\n\nНастройки внешнего вида:\n<blockquote expandable>Новый стиль:
Анимированные эмодзи: Обычные смайлы заменил на красивые анимированные и цветные иконки. 
Цветные индикаторы: сделан акцент на ключевых кнопках. Понятно без чтения текста. 
Иконки на кнопках: В меню появились анимированные значки (календарь, шестеренка, стрелки).

Старый стиль:
Стиль по умолчанию без всех улучшений из нового.</blockquote>\n🟢 Включено | 🔴 Выключено'''
    else:
        kb = [
            [InlineKeyboardButton(f"{'✅' if n20 else '❌'} Уведомлять за 20 мин", callback_data="toggle_20")],
            [InlineKeyboardButton(f"{'✅' if n10 else '❌'} Уведомлять за 10 мин", callback_data="toggle_10")],
            [InlineKeyboardButton(f"{'✅' if n5 else '❌'} Уведомлять за 5 мин", callback_data="toggle_5")],
            [InlineKeyboardButton(f"{'✅' if n_ch else '❌'} Уведомлять об изменениях", callback_data="toggle_changes")],
            [InlineKeyboardButton("🎓 Сменить группу", callback_data="change_grp")],
            [InlineKeyboardButton("Новый стиль", callback_data="toggle_new_style")]
        ]
        text_content = '''⚙️ <b>Настройки пользователя</b>\n\nНастройка уведомлений:\nУведомлнение до пары за 20,10 и 5 минут, а также уведомление о изменении в расписании\n\nНастройки внешнего вида:\n<blockquote expandable>Новый стиль:
Анимированные эмодзи: Обычные смайлы заменил на красивые анимированные и цветные иконки. 
Цветные индикаторы: сделан акцент на ключевых кнопках. Понятно без чтения текста. 
Иконки на кнопках: В меню появились анимированные значки (календарь, шестеренка, стрелки).

Старый стиль:
Стиль по умолчанию без всех улучшений из нового.</blockquote>\n🟢 Включено | 🔴 Выключено'''

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
    await q.answer()
    
    user_id = q.from_user.id
    
    if q.data == "change_grp":
        await send_group_selection(update, context)
        return
    if q.data == "toggle_new_style":
        toggle_setting(user_id, "use_new_style")

        use_new_style = get_user_style(user_id)

        await send_settings_menu(update, context)
        
        if use_new_style:
            kb = [[KeyboardButton(text = "Расписание", api_kwargs={"style": "primary", "icon_custom_emoji_id": "5274055917766202507"})], [KeyboardButton("Настройки", api_kwargs={"icon_custom_emoji_id": "5818705028424141605"})]]
        else:
            kb = [[KeyboardButton("📅 Расписание")], [KeyboardButton("⚙️ Настройки")]]
            
        if user_id == ADMIN_ID: kb.append([KeyboardButton("🔄 Обновить")])
        
        try:
            msg = await context.bot.send_message(
                chat_id=user_id, 
                text="🎨 Стиль обновлен! Клавиатура изменена.", 
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
            )
        except Exception as e:
            logger.info(f"Failed to update keyboard: {e}")

        return
        
    if q.data.startswith("toggle_"):
        toggle_setting(user_id, q.data.replace("toggle_", "notify_"))
        await send_settings_menu(update, context)

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return 
    try:
        if not context.args:
            await update.message.reply_text("⚠️ `/add 123456`", parse_mode=ParseMode.MARKDOWN)
            return
        new_user_id = int(context.args[0])
        grant_access(new_user_id)
        await update.message.reply_text(f"✅ ID `{new_user_id}` добавлен.", parse_mode=ParseMode.MARKDOWN)
        try: await context.bot.send_message(new_user_id, "🔓 Доступ открыт! Жми /start")
        except: pass
    except ValueError:
        await update.message.reply_text("❌ ID - это число.")

async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return 
    try:
        if not context.args:
            await update.message.reply_text("⚠️ `/del 123456`", parse_mode=ParseMode.MARKDOWN)
            return
        del_target_id = int(context.args[0])
        if del_target_id == ADMIN_ID:
            await update.message.reply_text("❌ Себя удалить нельзя.")
            return
        success = revoke_access_delete_user(del_target_id)
        if success:
            await update.message.reply_text(f"🗑 ID `{del_target_id}` удален.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ ID `{del_target_id}` не найден.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ ID - это число.")

async def send_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID and context.args:
        txt = " ".join(context.args)
        ids = get_allowed_users_ids()
        for uid in ids:
            try: await context.bot.send_message(uid, f"<b>📢 Уведомление от администратора:</b>\n\n{txt}", parse_mode=ParseMode.HTML)
            except: pass
        await update.message.reply_text("📬 Отправлено")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    users = get_all_users_info()
    if not users:
        await update.message.reply_text("📭 База пуста.")
        return
    msg = "👥 <b>Список пользователей:</b>\n\n"
    for u in users:
        uid, is_allowed, group_name, n20, n10, n5, n_ch, use_new_style = u
        notif_time = []
        if n20: notif_time.append("20")
        if n10: notif_time.append("10")
        if n5: notif_time.append("5")
        time_icons = f"⏰{','.join(notif_time)}" if notif_time else "🔕"
        change_icon = "📝" if n_ch else ""
        msg += f"<code>{uid}</code> [{group_name or '?'}] - <a href='tg://user?id={uid}'>Ссылка 1</a> <a href='tg://openmessage?user_id={uid}'>Ссылка 2</a> {time_icons} {change_icon}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
