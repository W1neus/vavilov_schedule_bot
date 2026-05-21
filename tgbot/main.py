import logging
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_ID, schedule_cache, TZ_SARATOV
from database import init_db
from tasks import update_schedule_data, notifier, load_schedule_from_json
from handlers import (
    start, msg_handler, group_selection_handler,
    schedule_navigation_handler, settings_handler,
    teacher_callback_handler, setrole_handler
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    init_db()
    load_schedule_from_json() 
    
    req = HTTPXRequest(connection_pool_size=4, connect_timeout=60, read_timeout=60)
    app = Application.builder().token(BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setrole", setrole_handler))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    
    
    app.add_handler(CallbackQueryHandler(teacher_callback_handler, pattern="^(role_|tset_|tconfirm_|tsearch_|tsearchconf_|ts_d_|ts_w_|tgrp|tgrpsched_|teacher_change_surname|role_student_reset|timg_)"))
    app.add_handler(CallbackQueryHandler(group_selection_handler, pattern="^(setgroup|seluk|selinst|selform|page|spage)_|^search_btn$"))
    app.add_handler(CallbackQueryHandler(schedule_navigation_handler, pattern="^(sched|img)_"))
    app.add_handler(CallbackQueryHandler(settings_handler))

    
    app.job_queue.run_repeating(notifier, interval=60, first=10)
    import datetime
    midnight_saratov = datetime.time(hour=0, minute=0, tzinfo=TZ_SARATOV)
    app.job_queue.run_daily(update_schedule_data, time=midnight_saratov)
    if not schedule_cache['data']:
        app.job_queue.run_once(update_schedule_data, 1)

    print(f"Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
