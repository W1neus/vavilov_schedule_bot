import logging
import asyncio
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_ID, schedule_cache
from database import init_db
from tasks import update_schedule_data, notifier
from handlers import (
    start, add_user_command, del_user_command, send_all_command,
    list_users_command, msg_handler, group_selection_handler,
    schedule_navigation_handler, settings_handler
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    init_db()
    req = HTTPXRequest(connection_pool_size=8, connect_timeout=60, read_timeout=60)
    app = Application.builder().token(BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_user_command))
    app.add_handler(CommandHandler("del", del_user_command))
    app.add_handler(CommandHandler("send_all", send_all_command))
    app.add_handler(CommandHandler("users", list_users_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    
    app.add_handler(CallbackQueryHandler(group_selection_handler, pattern="^setgroup_"))
    app.add_handler(CallbackQueryHandler(schedule_navigation_handler, pattern="^sched_"))
    app.add_handler(CallbackQueryHandler(settings_handler))

    app.job_queue.run_repeating(notifier, interval=60, first=10)
    app.job_queue.run_repeating(update_schedule_data, interval=3600)
    
    app.job_queue.run_once(update_schedule_data, 1)

    print(f"Bot started. Admin: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
