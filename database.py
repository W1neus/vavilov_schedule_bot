import sqlite3
from config import DB_FILE, ADMIN_ID

# ================= БАЗА ДАННЫХ =================

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_allowed BOOLEAN DEFAULT 0,
                group_name TEXT,
                notify_20 BOOLEAN DEFAULT 1,
                notify_10 BOOLEAN DEFAULT 1,
                notify_5 BOOLEAN DEFAULT 1,
                notify_changes BOOLEAN DEFAULT 1,
                use_new_style BOOLEAN DEFAULT 0
            )
        ''')
        try: cursor.execute("ALTER TABLE users ADD COLUMN group_name TEXT")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE users ADD COLUMN notify_changes BOOLEAN DEFAULT 1")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE users ADD COLUMN use_new_style BOOLEAN DEFAULT 0")
        except sqlite3.OperationalError: pass
        
        cursor.execute('INSERT OR IGNORE INTO users (user_id, is_allowed) VALUES (?, 1)', (ADMIN_ID,))
        cursor.execute('UPDATE users SET is_allowed = 1 WHERE user_id = ?', (ADMIN_ID,))
        conn.commit()

def check_access(user_id):
    if user_id == ADMIN_ID: return True
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT is_allowed FROM users WHERE user_id = ?', (user_id,))
        res = cursor.fetchone()
        return res[0] if res else False

def grant_access(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (user_id, is_allowed) VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET is_allowed = 1
        ''', (user_id,))
        conn.commit()

def revoke_access_delete_user(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        conn.commit()
        return cursor.rowcount > 0

def get_all_users_info():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, is_allowed, group_name, notify_20, notify_10, notify_5, notify_changes, use_new_style FROM users')
        return cursor.fetchall()

def get_allowed_users_ids():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE is_allowed = 1')
        return [row[0] for row in cursor.fetchall()]

def set_user_group(user_id, group_name):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET group_name = ? WHERE user_id = ?', (group_name, user_id))
        conn.commit()

def get_user_group(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT group_name FROM users WHERE user_id = ?', (user_id,))
        res = cursor.fetchone()
        return res[0] if res else None

def get_user_settings(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT notify_20, notify_10, notify_5, notify_changes, use_new_style FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone()

def toggle_setting(user_id, setting_name):
    valid = ['notify_20', 'notify_10', 'notify_5', 'notify_changes', 'use_new_style']
    if setting_name not in valid: return
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(f'UPDATE users SET {setting_name} = 1 - {setting_name} WHERE user_id = ?', (user_id,))
        conn.commit()

def get_user_style(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT use_new_style FROM users WHERE user_id = ?', (user_id,))
        res = cursor.fetchone()
        return bool(res[0]) if res else False

def get_users_for_change_notification():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE is_allowed = 1 AND notify_changes = 1')
        return [row[0] for row in cursor.fetchall()]
