import asyncio
import sqlite3
import aiosqlite
from config import DB_FILE

# ================= БАЗА ДАННЫХ =================

# Единый постоянный коннект + лок для защиты от гонок
_db_conn: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    
    global _db_conn
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(DB_FILE, check_same_thread=False)
        await _db_conn.execute("PRAGMA journal_mode=WAL")  # WAL улучшает конкуррентность
        await _db_conn.execute("PRAGMA synchronous=NORMAL")
    return _db_conn


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                group_name TEXT,
                notify_20 BOOLEAN DEFAULT 1,
                notify_10 BOOLEAN DEFAULT 1,
                notify_5 BOOLEAN DEFAULT 1,
                is_teacher INTEGER DEFAULT 0,
                teacher_surname TEXT,
                role_selected INTEGER DEFAULT 0
            )
        ''')
        for col, definition in [
            ("username", "TEXT"),
            ("group_name", "TEXT"),
            ("is_teacher", "INTEGER DEFAULT 0"),
            ("teacher_surname", "TEXT"),
            ("role_selected", "INTEGER DEFAULT 0"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass

        # Включаем WAL для лучшей конкуррентности при синхронной инициализации
        cursor.execute("PRAGMA journal_mode=WAL")
        conn.commit()


async def ensure_user(user_id, username=None):
    async with _db_lock:
        db = await get_db()
        await db.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        if username:
            await db.execute('UPDATE users SET username = ? WHERE user_id = ?', (username, user_id))
        await db.commit()


async def get_user_id_by_username(username: str):
    """Возвращает user_id по username (без @), если найден, иначе None."""
    username = username.lstrip('@')
    async with _db_lock:
        db = await get_db()
        async with db.execute('SELECT user_id FROM users WHERE username = ?', (username,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_user_group(user_id, group_name):
    await ensure_user(user_id)
    async with _db_lock:
        db = await get_db()
        await db.execute('UPDATE users SET group_name = ? WHERE user_id = ?', (group_name, user_id))
        await db.commit()


async def get_user_group(user_id):
    async with _db_lock:
        db = await get_db()
        async with db.execute('SELECT group_name FROM users WHERE user_id = ?', (user_id,)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else None


async def get_user_settings(user_id):

    async with _db_lock:
        db = await get_db()
        async with db.execute('SELECT notify_20, notify_10, notify_5 FROM users WHERE user_id = ?', (user_id,)) as cursor:
            res = await cursor.fetchone()
            if not res:
                return (1, 1, 1)
            return res


async def toggle_setting(user_id, setting_name):
    valid = ['notify_20', 'notify_10', 'notify_5']
    if setting_name not in valid:
        return

    async with _db_lock:
        db = await get_db()
        await db.execute(f'UPDATE users SET {setting_name} = 1 - {setting_name} WHERE user_id = ?', (user_id,))
        await db.commit()


async def get_all_users_for_notifier():
    async with _db_lock:
        db = await get_db()
        async with db.execute(
            'SELECT user_id, group_name, notify_20, notify_10, notify_5, is_teacher, teacher_surname '
            'FROM users WHERE notify_20=1 OR notify_10=1 OR notify_5=1'
        ) as cursor:
            return await cursor.fetchall()


# ================= РОЛЬ ПОЛЬЗОВАТЕЛЯ =================

async def get_user_role(user_id):

    async with _db_lock:
        db = await get_db()
        async with db.execute(
            'SELECT is_teacher, teacher_surname, role_selected FROM users WHERE user_id = ?',
            (user_id,)
        ) as cursor:
            res = await cursor.fetchone()
            if not res:
                return (0, None, 0)
            return res


async def set_user_role(user_id, is_teacher: int):
    await ensure_user(user_id)
    async with _db_lock:
        db = await get_db()
        await db.execute(
            'UPDATE users SET is_teacher = ?, role_selected = 1 WHERE user_id = ?',
            (is_teacher, user_id)
        )
        await db.commit()


async def set_teacher_surname(user_id, surname: str):
    await ensure_user(user_id)
    async with _db_lock:
        db = await get_db()
        await db.execute(
            'UPDATE users SET teacher_surname = ? WHERE user_id = ?',
            (surname, user_id)
        )
        await db.commit()


async def mark_role_selected(user_id):
    await ensure_user(user_id)
    async with _db_lock:
        db = await get_db()
        await db.execute(
            'UPDATE users SET role_selected = 1 WHERE user_id = ?',
            (user_id,)
        )
        await db.commit()
