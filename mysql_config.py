# mysql_config.py
# 📌 BAS ISME APNE DETAILS DALO - FIR FRIEND KO DEDO

import mysql.connector
import json

# =============== 🔥 YAHAN APNE CPANEL DETAILS DALO ===============
MYSQL_DETAILS = {
    'host': 'localhost',        # cPanel se milta hai (usually localhost)
    'port': 3306,               # MySQL port (usually 3306)
    'database': 'vipxoffic_vip',  # cPanel me banaya hua database name
    'user': 'vipxoffic_vip',      # cPanel database username
    'password': 'vipxoffic_vip',  # cPanel database password
    'charset': 'utf8mb4'
}
# ================================================================

class BotDB:
    """Common Database Class - Har bot ke liye alag table"""
    
    def __init__(self, bot_name):
        self.bot_name = bot_name
        self.table = f"bot_{bot_name}"  # bot_ftvdd, bot_main, bot_bot3 etc
        self._create_table()
    
    def _connect(self):
        return mysql.connector.connect(**MYSQL_DETAILS)
    
    def _create_table(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS `{self.table}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT UNIQUE,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                chat_id BIGINT,
                data JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    
    def save_user(self, user_id, username=None, first_name=None, last_name=None, chat_id=None, extra_data=None):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO `{self.table}` (user_id, username, first_name, last_name, chat_id, data)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            username = VALUES(username),
            first_name = VALUES(first_name),
            last_name = VALUES(last_name),
            chat_id = VALUES(chat_id),
            data = VALUES(data)
        """, (user_id, username, first_name, last_name, chat_id, 
              json.dumps(extra_data) if extra_data else None))
        conn.commit()
        cur.close()
        conn.close()
    
    def get_user(self, user_id):
        conn = self._connect()
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT * FROM `{self.table}` WHERE user_id = %s", (user_id,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user and user['data']:
            user['data'] = json.loads(user['data'])
        return user
    
    def update_data(self, user_id, key, value):
        user = self.get_user(user_id)
        if not user:
            return False
        data = user.get('data', {}) or {}
        data[key] = value
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"UPDATE `{self.table}` SET data = %s WHERE user_id = %s",
                   (json.dumps(data), user_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    
    def get_data(self, user_id, key=None):
        user = self.get_user(user_id)
        if not user:
            return None if key else {}
        if key:
            return user.get('data', {}).get(key)
        return user.get('data', {})
    
    def get_all_users(self):
        conn = self._connect()
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT * FROM `{self.table}` ORDER BY created_at DESC")
        users = cur.fetchall()
        cur.close()
        conn.close()
        for u in users:
            if u['data']:
                u['data'] = json.loads(u['data'])
        return users

# =============== 📌 FRIEND KE LIYE USAGE GUIDE ===============
"""
🤝 FRIEND! YE RAHA CONNECTION CODE:

TERA KAAM:
1. Ye file download kar
2. Apne bot ke folder me rakh
3. Apne bot me is tarah use kar:

------------------------------------------------
from mysql_config import BotDB

# 🔥 Har bot ke liye ALAG naam do:
db = BotDB('ftvdd')   # ftvdd bot ke liye - table: bot_ftvdd
# ya
db = BotDB('main')    # main bot ke liye - table: bot_main
# ya
db = BotDB('bot3')    # kisi aur bot ke liye - table: bot_bot3

# ✅ USER SAVE KARO:
db.save_user(
    user_id=update.effective_user.id,
    username=update.effective_user.username,
    first_name=update.effective_user.first_name,
    last_name=update.effective_user.last_name,
    chat_id=update.effective_chat.id,
    extra_data={
        'trial_used': False,
        'is_premium': False,
        'joined': str(datetime.now())
    }
)

# ✅ USER NIKALO:
user = db.get_user(user_id)

# ✅ DATA UPDATE KARO:
db.update_data(user_id, 'trial_used', True)
db.update_data(user_id, 'attack_count', 5)

# ✅ DATA NIKALO:
trial_used = db.get_data(user_id, 'trial_used')
attack_count = db.get_data(user_id, 'attack_count')

# ✅ SARE USERS DEKHO (ADMIN KE LIYE):
all_users = db.get_all_users()
for u in all_users:
    print(u['user_id'], u['data'])

------------------------------------------------
🤔 KUCH PROBLEM TO BATA DE!
"""