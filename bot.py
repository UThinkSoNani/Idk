import os
import random
import sqlite3
import time
import asyncio

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from discord.ui import View, button
from dotenv import load_dotenv


# =========================================================
# 1. CAU HINH BOT
# =========================================================

load_dotenv()

def normalize_discord_token(token: str | None) -> str:
    """Normalize a pasted Discord bot token without logging its value."""
    token = (token or "").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    return token


TOKEN = normalize_discord_token(os.getenv("DISCORD_TOKEN"))
DB_FILE = "linh_thach.db"
CURRENCY_NAME = "Linh Thạch"

STARTING_BALANCE = 1000
MIN_BET = 10
MAX_BET = 100000

ROLE_SHOP = [
    ("Luyện Khí Kỳ", 10),
    ("Trúc Cơ Kỳ", 10_000),
    ("Kết Đan Kỳ", 30_000),
    ("Nguyên Anh Kỳ", 50_000),
    ("Hóa Thần Kỳ", 100_000),
    ("Luyện Hư", 500_000),
    ("Hợp Thể", 1_000_000),
    ("Đại Thừa", 5_000_000),
]
ROLE_BET_BONUS_PERCENT = (1, 3, 5, 8, 10, 12, 15, 20)
ITEM_SHOP = {
    "thien_loi": {
        "name": "⚡ Thiên Lôi",
        "price": 300_000,
        "description": "Dùng lên người khác để lấy 3% Bảo khố hiện tại.",
        "kind": "consumable",
    },
    "toa_tu_hoang_kim_giap": {
        "name": "🛡️ Tỏa Tử Hoàng Kim Giáp",
        "price": 500_000,
        "description": (
            "Giúp bảo vệ người dùng khỏi một lần ăn thiên phạt. "
            "Dùng `/dung_vat_pham` để tiêu thụ và kích hoạt trước khi bị nhắm tới."
        ),
        "kind": "consumable",
    },
    "dan_duoc_truong_sinh": {
        "name": "🧪 Đan Dược Trường Sinh",
        "price": 10_000,
        "description": "Vật phẩm bí ẩn, không có công dụng cụ thể.",
        "kind": "consumable",
    },
    "do_kiep": {
        "name": "🗡️ Độ Kiếp",
        "price": 99_999_999,
        "description": "Độ Kiếp Thành Công Nhập Ma hay Tu Tâm Thành Phật Do Bạn Chọn.",
        "kind": "role",
        "role_name": "Độ Kiếp",
    }
}


def get_role_bet_bonus(member, bet: int):
    """Return (bonus_amount, bonus_percent, role_name) for the highest role."""
    owned_role_names = {role.name for role in getattr(member, "roles", ())}
    if "Độ Kiếp" in owned_role_names:
        return bet * 50 // 100, 50, "Độ Kiếp"
    for (role_name, _), percent in reversed(
        list(zip(ROLE_SHOP, ROLE_BET_BONUS_PERCENT))
    ):
        if role_name in owned_role_names:
            return bet * percent // 100, percent, role_name
    return 0, 0, None


def tinh_tien_tra_ve_blackjack(cuoc: int, ket_qua: str, thuong_role: int = 0) -> int:
    """Return stake plus winnings, the stake on a push, or zero on a loss."""
    if ket_qua == "win":
        return cuoc * 2 + thuong_role
    if ket_qua == "push":
        return cuoc
    return 0


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
RED_ENVELOPE_RECOVERY_DONE = False
RED_ENVELOPE_EXPIRY_TASKS = set()
ROLE_PURCHASE_LOCKS = {}


# =========================================================
# 2. CO SO DU LIEU SQLITE
# =========================================================

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reward_cooldowns (
                user_id INTEGER NOT NULL,
                reward_type TEXT NOT NULL,
                last_claim REAL NOT NULL,
                PRIMARY KEY (user_id, reward_type)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS red_envelopes (
                envelope_id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                total_amount INTEGER NOT NULL,
                remaining_amount INTEGER NOT NULL,
                total_slots INTEGER NOT NULL,
                remaining_slots INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS red_envelope_claims (
                envelope_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                claimed_at REAL NOT NULL,
                PRIMARY KEY (envelope_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS item_inventory (
                user_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                PRIMARY KEY (user_id, item_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_protections (
                user_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                charges INTEGER NOT NULL DEFAULT 0 CHECK (charges >= 0),
                PRIMARY KEY (user_id, item_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_reward_buffs (
                user_id INTEGER NOT NULL,
                buff_key TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (user_id, buff_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS role_item_purchases (
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (user_id, guild_id, item_key)
            )
        """)
        conn.commit()


def get_balance(user_id: int) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        conn.commit()
        return int(row[0])


def get_leaderboard(limit: int = 10):
    """Return the highest balances and total number of players."""
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT user_id, balance FROM users "
            "ORDER BY balance DESC, user_id ASC LIMIT ?",
            (limit,)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return rows, int(total)


def get_player_rank(user_id: int):
    """Return (rank, balance), using user_id as a stable tie-breaker."""
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        balance = int(row[0])
        ahead = conn.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE balance > ? OR (balance = ? AND user_id < ?)",
            (balance, balance, user_id)
        ).fetchone()[0]
        return int(ahead) + 1, balance


def set_balance(user_id: int, amount: int) -> int:
    amount = max(0, int(amount))
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO users (user_id, balance)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET balance = excluded.balance
        """, (user_id, amount))
        conn.commit()
    return amount


def update_balance(user_id: int, amount: int) -> int:
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        conn.execute(
            "UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id = ?",
            (amount, user_id)
        )
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        conn.commit()
        return int(row[0])


def place_bet(user_id: int, amount: int) -> bool:
    """Tru cuoc trong mot giao dich SQLite an toan."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        cursor = conn.execute("""
            UPDATE users
            SET balance = balance - ?
            WHERE user_id = ? AND balance >= ?
        """, (amount, user_id, amount))
        conn.commit()
        return cursor.rowcount == 1


def spend_balance(user_id: int, amount: int):
    """Atomically deduct a purchase; return remaining balance or None."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        cursor = conn.execute(
            "UPDATE users SET balance = balance - ? "
            "WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        balance = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        conn.commit()
        return int(balance)


def buy_item(user_id: int, item_key: str):
    """Atomically charge the shop price and add one item to inventory."""
    item = ITEM_SHOP.get(item_key)
    if item is None or item.get("kind") == "role":
        return None
    price = item["price"]
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        cursor = conn.execute(
            "UPDATE users SET balance = balance - ? "
            "WHERE user_id = ? AND balance >= ?",
            (price, user_id, price)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.execute("""
            INSERT INTO item_inventory (user_id, item_key, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_key)
            DO UPDATE SET quantity = quantity + 1
        """, (user_id, item_key))
        balance = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        quantity = conn.execute(
            "SELECT quantity FROM item_inventory WHERE user_id = ? AND item_key = ?",
            (user_id, item_key)
        ).fetchone()[0]
        conn.commit()
        return int(balance), int(quantity)


def get_item_inventory(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT item_key, quantity FROM item_inventory "
            "WHERE user_id = ? AND quantity > 0 ORDER BY item_key",
            (user_id,)
        ).fetchall()
        return {item_key: int(quantity) for item_key, quantity in rows}


def reserve_role_item_purchase(user_id: int, guild_id: int, item_key: str, price: int):
    """Charge once and create/reuse a pending role grant for crash-safe retries."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT status FROM role_item_purchases "
            "WHERE user_id = ? AND guild_id = ? AND item_key = ?",
            (user_id, guild_id, item_key)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        balance = int(conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()[0])
        if existing:
            conn.rollback()
            return existing[0], balance

        cursor = conn.execute(
            "UPDATE users SET balance = balance - ? "
            "WHERE user_id = ? AND balance >= ?",
            (price, user_id, price)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return "insufficient", balance
        conn.execute(
            "INSERT INTO role_item_purchases (user_id, guild_id, item_key, status) "
            "VALUES (?, ?, ?, 'pending')",
            (user_id, guild_id, item_key)
        )
        balance -= price
        conn.commit()
        return "pending", balance


def complete_role_item_purchase(user_id: int, guild_id: int, item_key: str):
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            "UPDATE role_item_purchases SET status = 'granted' "
            "WHERE user_id = ? AND guild_id = ? AND item_key = ?",
            (user_id, guild_id, item_key)
        )
        conn.commit()


def refund_pending_role_item_purchase(
    user_id: int, guild_id: int, item_key: str, price: int
):
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "DELETE FROM role_item_purchases "
            "WHERE user_id = ? AND guild_id = ? AND item_key = ? AND status = 'pending'",
            (user_id, guild_id, item_key)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (price, user_id)
        )
        conn.commit()
        return True


def activate_armor(user_id: int):
    """Consume one armor item and activate one persistent shield charge."""
    item_key = "toa_tu_hoang_kim_giap"
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT quantity FROM item_inventory WHERE user_id = ? AND item_key = ?",
            (user_id, item_key)
        ).fetchone()
        if item is None or item[0] <= 0:
            conn.rollback()
            return None

        conn.execute(
            "UPDATE item_inventory SET quantity = quantity - 1 "
            "WHERE user_id = ? AND item_key = ? AND quantity > 0",
            (user_id, item_key)
        )
        conn.execute("""
            INSERT INTO active_protections (user_id, item_key, charges)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_key)
            DO UPDATE SET charges = charges + 1
        """, (user_id, item_key))
        charges = int(conn.execute(
            "SELECT charges FROM active_protections "
            "WHERE user_id = ? AND item_key = ?",
            (user_id, item_key)
        ).fetchone()[0])
        conn.commit()
        return charges


def get_active_protection_charges(user_id: int, item_key: str):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT charges FROM active_protections "
            "WHERE user_id = ? AND item_key = ?",
            (user_id, item_key)
        ).fetchone()
        return int(row[0]) if row else 0


def activate_reward_potion(user_id: int):
    """Consume one longevity pill and activate/refresh its 10-minute reward buff."""
    item_key = "dan_duoc_truong_sinh"
    now = time.time()
    expires_at = now + DAN_DUOC_DURATION_SECONDS
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT quantity FROM item_inventory WHERE user_id = ? AND item_key = ?",
            (user_id, item_key)
        ).fetchone()
        if item is None or item[0] <= 0:
            conn.rollback()
            return None

        conn.execute(
            "UPDATE item_inventory SET quantity = quantity - 1 "
            "WHERE user_id = ? AND item_key = ? AND quantity > 0",
            (user_id, item_key)
        )
        conn.execute("""
            INSERT INTO user_reward_buffs (user_id, buff_key, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, buff_key)
            DO UPDATE SET expires_at = excluded.expires_at
        """, (user_id, item_key, expires_at))
        conn.commit()
        return expires_at


def get_reward_buff_seconds(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT expires_at FROM user_reward_buffs "
            "WHERE user_id = ? AND buff_key = 'dan_duoc_truong_sinh'",
            (user_id,)
        ).fetchone()
    return max(0, int(row[0] - time.time())) if row else 0


def use_thunder_item(user_id: int, target_id: int, target_is_do_kiep: bool = False):
    """Resolve Thunder atomically against Do Kiep immunity or one-use armor."""
    if user_id == target_id:
        return "self_target", 0, 0
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT quantity FROM item_inventory "
            "WHERE user_id = ? AND item_key = 'thien_loi'",
            (user_id,)
        ).fetchone()
        if item is None or item[0] <= 0:
            conn.rollback()
            return "no_item", 0, 0
        if target_is_do_kiep:
            conn.rollback()
            return "do_kiep_immunity", 0, 0

        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (target_id, STARTING_BALANCE)
        )
        target_balance = int(conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (target_id,)
        ).fetchone()[0])

        armor = conn.execute(
            "SELECT charges FROM active_protections "
            "WHERE user_id = ? AND item_key = 'toa_tu_hoang_kim_giap'",
            (target_id,)
        ).fetchone()
        if armor and armor[0] > 0:
            conn.execute(
                "UPDATE active_protections SET charges = charges - 1 "
                "WHERE user_id = ? AND item_key = 'toa_tu_hoang_kim_giap' "
                "AND charges > 0",
                (target_id,)
            )
            conn.execute(
                "UPDATE item_inventory SET quantity = quantity - 1 "
                "WHERE user_id = ? AND item_key = 'thien_loi' AND quantity > 0",
                (user_id,)
            )
            conn.commit()
            return "protected", 0, target_balance

        damage = target_balance * 3 // 100
        if damage < 1:
            conn.rollback()
            return "target_balance_too_low", 0, target_balance

        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (damage, target_id)
        )
        conn.execute(
            "UPDATE item_inventory SET quantity = quantity - 1 "
            "WHERE user_id = ? AND item_key = 'thien_loi' AND quantity > 0",
            (user_id,)
        )
        balance_after = int(conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (target_id,)
        ).fetchone()[0])
        conn.commit()
        return "success", int(damage), balance_after


def transfer_balance(sender_id: int, recipient_id: int, amount: int):
    """Transfer currency atomically; return new balances, or None if invalid/poor."""
    if sender_id == recipient_id or amount <= 0:
        return None

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (sender_id, STARTING_BALANCE)
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (recipient_id, STARTING_BALANCE)
        )
        cursor = conn.execute(
            "UPDATE users SET balance = balance - ? "
            "WHERE user_id = ? AND balance >= ?",
            (amount, sender_id, amount)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None

        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, recipient_id)
        )
        sender_balance = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (sender_id,)
        ).fetchone()[0]
        recipient_balance = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (recipient_id,)
        ).fetchone()[0]
        conn.commit()
        return int(sender_balance), int(recipient_balance)


def create_red_envelope(creator_id: int, guild_id: int, channel_id: int,
                        total_amount: int, slots: int, expires_at: float):
    """Escrow a red-envelope pool and create its persistent record atomically."""
    if total_amount < slots or slots < 1:
        return None
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (creator_id, STARTING_BALANCE)
        )
        cursor = conn.execute(
            "UPDATE users SET balance = balance - ? "
            "WHERE user_id = ? AND balance >= ?",
            (total_amount, creator_id, total_amount)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        cursor = conn.execute("""
            INSERT INTO red_envelopes (
                creator_id, guild_id, channel_id, total_amount,
                remaining_amount, total_slots, remaining_slots,
                expires_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (creator_id, guild_id, channel_id, total_amount, total_amount,
              slots, slots, expires_at, time.time()))
        envelope_id = int(cursor.lastrowid)
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (creator_id,)
        ).fetchone()[0]
        conn.commit()
        return envelope_id, int(new_balance)


def set_red_envelope_message(envelope_id: int, message_id: int):
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            "UPDATE red_envelopes SET message_id = ? WHERE envelope_id = ?",
            (message_id, envelope_id)
        )
        conn.commit()


def get_red_envelope(envelope_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("""
            SELECT envelope_id, creator_id, guild_id, channel_id, message_id,
                   total_amount, remaining_amount, total_slots, remaining_slots,
                   expires_at, status
            FROM red_envelopes WHERE envelope_id = ?
        """, (envelope_id,)).fetchone()
        if row is None:
            return None
        state = dict(zip((
            "envelope_id", "creator_id", "guild_id", "channel_id", "message_id",
            "total_amount", "remaining_amount", "total_slots", "remaining_slots",
            "expires_at", "status"
        ), row))
        state["claimed_count"] = conn.execute(
            "SELECT COUNT(*) FROM red_envelope_claims WHERE envelope_id = ?",
            (envelope_id,)
        ).fetchone()[0]
        return state


def claim_red_envelope(envelope_id: int, guild_id: int, user_id: int):
    """Atomically award one random share; each server member can claim once."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT creator_id, guild_id, remaining_amount, remaining_slots,
                   expires_at, status
            FROM red_envelopes WHERE envelope_id = ?
        """, (envelope_id,)).fetchone()
        if row is None:
            conn.rollback()
            return "missing", 0
        creator_id, envelope_guild, remaining, slots, expires_at, status = row
        if envelope_guild != guild_id:
            conn.rollback()
            return "wrong_server", 0
        if status != "active":
            conn.rollback()
            return "closed", 0
        if time.time() >= expires_at:
            if remaining:
                conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (remaining, creator_id)
                )
            conn.execute("""
                UPDATE red_envelopes
                SET status = 'expired', remaining_amount = 0, remaining_slots = 0
                WHERE envelope_id = ? AND status = 'active'
            """, (envelope_id,))
            conn.commit()
            return "expired", int(remaining)
        already_claimed = conn.execute(
            "SELECT 1 FROM red_envelope_claims WHERE envelope_id = ? AND user_id = ?",
            (envelope_id, user_id)
        ).fetchone()
        if already_claimed:
            conn.rollback()
            return "duplicate", 0
        if slots <= 0 or remaining <= 0:
            conn.rollback()
            return "closed", 0

        amount = remaining if slots == 1 else random.randint(1, remaining - slots + 1)
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )
        conn.execute("""
            INSERT INTO red_envelope_claims (envelope_id, user_id, amount, claimed_at)
            VALUES (?, ?, ?, ?)
        """, (envelope_id, user_id, amount, time.time()))
        new_remaining = remaining - amount
        new_slots = slots - 1
        new_status = "completed" if new_slots == 0 else "active"
        conn.execute("""
            UPDATE red_envelopes
            SET remaining_amount = ?, remaining_slots = ?, status = ?
            WHERE envelope_id = ? AND status = 'active'
        """, (new_remaining, new_slots, new_status, envelope_id))
        conn.commit()
        return "claimed", int(amount)


def expire_red_envelope(envelope_id: int):
    """Close an active envelope and return its unclaimed balance to its creator."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT creator_id, remaining_amount FROM red_envelopes "
            "WHERE envelope_id = ? AND status = 'active'",
            (envelope_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return 0
        creator_id, remaining = row
        if remaining:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (remaining, creator_id)
            )
        conn.execute("""
            UPDATE red_envelopes
            SET status = 'expired', remaining_amount = 0, remaining_slots = 0
            WHERE envelope_id = ? AND status = 'active'
        """, (envelope_id,))
        conn.commit()
        return int(remaining)


def refund_open_red_envelopes_after_restart():
    """Refund all active escrows after a process restart and mark them cancelled."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("""
            SELECT envelope_id, creator_id, channel_id, message_id, remaining_amount
            FROM red_envelopes WHERE status = 'active'
        """).fetchall()
        for envelope_id, creator_id, _channel_id, _message_id, remaining in rows:
            if remaining:
                conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (remaining, creator_id)
                )
            conn.execute("""
                UPDATE red_envelopes
                SET status = 'cancelled', remaining_amount = 0, remaining_slots = 0
                WHERE envelope_id = ?
            """, (envelope_id,))
        conn.commit()
        return rows


# =========================================================
# 3. PHAN THUONG VA THOI GIAN CHO
# =========================================================

REWARD_SETTINGS = {
    "daily": {
        "name": "Điểm danh hằng ngày",
        "cooldown": 24 * 60 * 60,
        "minimum": 1000,
        "maximum": 2000,
    },
    "mine": {
        "name": "Khai thác Linh Thạch",
        "cooldown": 10 * 60,
        "minimum": 100,
        "maximum": 500,
    },
    "mission": {
        "name": "Nhiệm vụ định kỳ",
        "cooldown": 30 * 60,
        "minimum": 500,
        "maximum": 1000,
    },
    "chat": {
        "name": "Thưởng trò chuyện",
        "cooldown": 60,
        "minimum": 10,
        "maximum": 50,
    },
}
DAN_DUOC_DURATION_SECONDS = 10 * 60
DAN_DUOC_REWARD_BONUS_PERCENT = 5
REWARD_BUFFABLE_TYPES = frozenset(REWARD_SETTINGS)


def claim_reward(user_id: int, reward_type: str):
    """
    Return (success, base reward/remaining cooldown, potion bonus).
    """
    settings = REWARD_SETTINGS[reward_type]
    now = time.time()

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, STARTING_BALANCE)
        )

        row = conn.execute("""
            SELECT last_claim
            FROM reward_cooldowns
            WHERE user_id = ? AND reward_type = ?
        """, (user_id, reward_type)).fetchone()

        if row is not None:
            remaining = settings["cooldown"] - (now - row[0])
            if remaining > 0:
                conn.rollback()
                return False, int(remaining) + 1, 0

        reward = random.randint(
            settings["minimum"],
            settings["maximum"]
        )
        bonus = 0
        if reward_type in REWARD_BUFFABLE_TYPES:
            active_buff = conn.execute(
                "SELECT expires_at FROM user_reward_buffs "
                "WHERE user_id = ? AND buff_key = 'dan_duoc_truong_sinh'",
                (user_id,)
            ).fetchone()
            if active_buff and active_buff[0] > now:
                bonus = reward * DAN_DUOC_REWARD_BONUS_PERCENT // 100

        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (reward + bonus, user_id)
        )

        conn.execute("""
            INSERT INTO reward_cooldowns (user_id, reward_type, last_claim)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, reward_type)
            DO UPDATE SET last_claim = excluded.last_claim
        """, (user_id, reward_type, now))

        conn.commit()
        return True, reward, bonus


def dinh_dang_thoi_gian(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(f"{hours} giờ")
    if minutes:
        parts.append(f"{minutes} phút")
    if secs and not hours:
        parts.append(f"{secs} giây")

    return " ".join(parts) or "dưới 1 giây"


async def xu_ly_nhan_thuong(
    interaction: discord.Interaction,
    reward_type: str
):
    settings = REWARD_SETTINGS[reward_type]
    success, value, bonus = claim_reward(interaction.user.id, reward_type)

    if not success:
        await interaction.response.send_message(
            f"⏳ Bạn đã nhận thưởng **{settings['name']}** gần đây.\n"
            f"Thời gian còn lại: **{dinh_dang_thoi_gian(value)}**.",
            ephemeral=True
        )
        return

    balance = get_balance(interaction.user.id)
    reward_total = value + bonus
    bonus_text = (
        f" (gồm **{value:,}** + **{bonus:,}** thưởng Đan Dược)"
        if bonus else ""
    )
    await interaction.response.send_message(
        f"🎉 Bạn nhận được **{reward_total:,} {CURRENCY_NAME}**{bonus_text} "
        f"từ **{settings['name']}**!\n"
        f"💎 Bảo khố hiện tại: **{balance:,} {CURRENCY_NAME}**."
    )


# =========================================================
# 4. LOGIC BAI TAY
# =========================================================

def tao_bo_bai():
    ranks = [
        "2", "3", "4", "5", "6", "7", "8", "9",
        "10", "J", "Q", "K", "A"
    ]
    suits = ["♠", "♥", "♦", "♣"]
    deck = [(rank, suit) for suit in suits for rank in ranks]
    random.shuffle(deck)
    return deck


def tinh_diem(hand):
    total = 0
    aces = 0

    for rank, _ in hand:
        if rank in ("J", "Q", "K"):
            total += 10
        elif rank == "A":
            total += 11
            aces += 1
        else:
            total += int(rank)

    while total > 21 and aces > 0:
        total -= 10
        aces -= 1

    return total


def blackjack_tu_nhien(hand):
    return len(hand) == 2 and tinh_diem(hand) == 21


def hien_thi_bai(hand):
    return " ".join(f"`{rank}{suit}`" for rank, suit in hand)


# =========================================================
# 5. GIAO DIEN BLACKJACK
# =========================================================

class BlackjackView(View):
    def __init__(self, user: discord.User, bet: int):
        super().__init__(timeout=120)
        self.user = user
        self.bet = bet
        (
            self.role_bonus,
            self.role_bonus_percent,
            self.role_bonus_name,
        ) = get_role_bet_bonus(user, bet)
        self.deck = tao_bo_bai()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
        self.message = None
        self.finished_game = False

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Bàn chơi này không phải của bạn!",
                ephemeral=True
            )
            return False
        return True

    def tao_embed(self, ket_thuc=False, ket_qua=""):
        embed = discord.Embed(
            title="🃏 Blackjack — Đầu Tư Tương Lai Không Bao Giờ Sai",
            color=0x2ECC71 if ket_thuc else 0x3498DB
        )

        if ket_thuc:
            dealer_text = (
                f"{hien_thi_bai(self.dealer_hand)} "
                f"(**{tinh_diem(self.dealer_hand)} điểm**)"
            )
        else:
            dealer_text = (
                f"`{self.dealer_hand[0][0]}{self.dealer_hand[0][1]}` "
                "`🂠 ?`"
            )

        embed.add_field(
            name="🏠 Nhà cái",
            value=dealer_text,
            inline=False
        )
        embed.add_field(
            name=f"🧑 {self.user.display_name}",
            value=(
                f"{hien_thi_bai(self.player_hand)} "
                f"(**{tinh_diem(self.player_hand)} điểm**)"
            ),
            inline=False
        )
        embed.add_field(
            name="💎 Mức cược",
            value=f"**{self.bet:,} {CURRENCY_NAME}**",
            inline=True
        )
        bonus_text = (
            f"**+{self.role_bonus_percent}%** (role {self.role_bonus_name})\n"
            f"Thưởng nếu thắng: **+{self.role_bonus:,} {CURRENCY_NAME}**"
            if self.role_bonus_percent else "Không có role thưởng"
        )
        embed.add_field(name="✨ Thưởng role", value=bonus_text, inline=True)

        if ket_qua:
            embed.add_field(name="📋 Kết quả", value=ket_qua, inline=False)

        embed.set_footer(
            text="Thua Là Do Bạn - Không Phải Do Tôi"
        )
        return embed

    def khoa_nut(self):
        for child in self.children:
            child.disabled = True

    async def ket_thuc(self, interaction, ket_qua, tien_tra_ve=0):
        if self.finished_game:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Ván này đã kết thúc rồi.",
                    ephemeral=True
                )
            return

        self.finished_game = True
        self.stop()
        self.khoa_nut()

        if tien_tra_ve > 0:
            balance = update_balance(self.user.id, tien_tra_ve)
            ket_qua += (
                f"\n💰 Đã chuyển vào ngân khố **+{tien_tra_ve:,} {CURRENCY_NAME}** "
                "(gồm cược được hoàn và tiền thắng nếu có)."
            )
        else:
            balance = get_balance(self.user.id)
        ket_qua += f"\n💎 Bảo khố: **{balance:,} {CURRENCY_NAME}**."

        await interaction.response.edit_message(
            embed=self.tao_embed(True, ket_qua),
            view=self
        )

    async def on_timeout(self):
        if self.finished_game:
            return

        self.finished_game = True
        self.khoa_nut()

        # Hoan cuoc neu nguoi choi bo ngang van
        balance = update_balance(self.user.id, self.bet)

        if self.message:
            balance = get_balance(self.user.id)
            result = (
                "⏰ Hết 120 giây thao tác. "
                f"Ván chơi kết thúc; cược **+{self.bet:,} {CURRENCY_NAME}** "
                "đã được hoàn về ngân khố.\n"
                f"💎 Bảo khố: **{balance:,} {CURRENCY_NAME}**."
            )
            try:
                await self.message.edit(
                    embed=self.tao_embed(True, result),
                    view=self
                )
            except discord.HTTPException:
                pass

    async def xu_ly_blackjack_dau_van(self):
        player_natural = blackjack_tu_nhien(self.player_hand)
        dealer_natural = blackjack_tu_nhien(self.dealer_hand)

        if not player_natural and not dealer_natural:
            return

        self.finished_game = True
        self.stop()
        self.khoa_nut()

        if player_natural and dealer_natural:
            update_balance(self.user.id, self.bet)
            result = (
                "🤝 Cả hai đều có Blackjack. Cược được hoàn lại. "
                f"Đã chuyển **+{self.bet:,} {CURRENCY_NAME}** vào ngân khố."
            )

        elif player_natural:
            # Tra lai cuoc va thuong lai 3:2
            profit = (self.bet * 3) // 2
            total_profit = profit + self.role_bonus
            update_balance(self.user.id, self.bet + total_profit)
            result = (
                "🌟 Trời Độ - Bạn May Mắn Rút Được Thẻ SSR Siêu Hiếm. "
                "Nhà Cái Liền Gục Ngã Trước Năm Triệu Điểm Sức Mạnh Của Bạn.\n"
                f"Đã chuyển vào ngân khố **+{self.bet + total_profit:,} {CURRENCY_NAME}** "
                f"(gồm cược **{self.bet:,}** và lãi "
                f"**{total_profit:,} {CURRENCY_NAME}**)."
            )
            if self.role_bonus:
                result += (
                    f" (gồm **{self.role_bonus:,} {CURRENCY_NAME}** thưởng "
                    f"role **{self.role_bonus_name}**)."
                )

        else:
            result = (
                "😔 Nhà cái có Blackjack tự nhiên. "
                f"Bạn mất **{self.bet:,} {CURRENCY_NAME}**."
            )

        balance = get_balance(self.user.id)
        result += f"\n💎 Bảo khố: **{balance:,} {CURRENCY_NAME}**."

        if self.message:
            await self.message.edit(
                embed=self.tao_embed(True, result),
                view=self
            )

    @button(
        label="Rút bài",
        style=discord.ButtonStyle.primary,
        emoji="🃏"
    )
    async def hit(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if self.finished_game:
            await interaction.response.send_message(
                "Ván chơi đã kết thúc.",
                ephemeral=True
            )
            return

        self.player_hand.append(self.deck.pop())
        score = tinh_diem(self.player_hand)

        if score > 21:
            await self.ket_thuc(
                interaction,
                f"💥 Bạn quắc điểm ({score} điểm) và mất "
                f"**{self.bet:,} {CURRENCY_NAME}**."
            )
        else:
            await interaction.response.edit_message(
                embed=self.tao_embed(),
                view=self
            )

    @button(
        label="Dừng",
        style=discord.ButtonStyle.secondary,
        emoji="🛑"
    )
    async def stand(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if self.finished_game:
            await interaction.response.send_message(
                "Ván chơi đã kết thúc.",
                ephemeral=True
            )
            return

        while tinh_diem(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_score = tinh_diem(self.player_hand)
        dealer_score = tinh_diem(self.dealer_hand)

        if dealer_score > 21:
            result = (
                "⚡ Nhà Cái Ăn Thiên Lôi! Bạn May Mắn Thoát Nạn. "
                f"Nhận Về **{self.bet + self.role_bonus:,} {CURRENCY_NAME}** "
                "làm phần thưởng."
            )
            payout = tinh_tien_tra_ve_blackjack(self.bet, "win", self.role_bonus)

        elif player_score > dealer_score:
            result = (
                "⚡ Nhà Cái Ăn Thiên Lôi! Bạn May Mắn Thoát Nạn. "
                f"Nhận Về **{self.bet + self.role_bonus:,} {CURRENCY_NAME}** "
                "làm phần thưởng."
            )
            payout = tinh_tien_tra_ve_blackjack(self.bet, "win", self.role_bonus)

        elif player_score < dealer_score:
            result = (
                f"😔 Bạn thua ({player_score} - {dealer_score}) "
                f"và mất **{self.bet:,} {CURRENCY_NAME}**."
            )
            payout = tinh_tien_tra_ve_blackjack(self.bet, "loss", self.role_bonus)

        else:
            result = (
                f"🤝 Hai bên hòa ({player_score} điểm). "
                f"Cược **{self.bet:,} {CURRENCY_NAME}** được hoàn về ngân khố."
            )
            payout = tinh_tien_tra_ve_blackjack(self.bet, "push", self.role_bonus)

        if self.role_bonus and payout > self.bet:
            result += (
                f"\n✨ Thưởng **{self.role_bonus:,} {CURRENCY_NAME}** "
                f"từ role **{self.role_bonus_name}** "
                f"(+{self.role_bonus_percent}% tiền cược)."
            )

        await self.ket_thuc(interaction, result, tien_tra_ve=payout)


class RedEnvelopeView(View):
    def __init__(self, envelope_id: int, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.envelope_id = envelope_id
        self.message = None

    def schedule_expiry(self, expires_at: float):
        task = asyncio.create_task(self.expire_at_deadline(expires_at))
        RED_ENVELOPE_EXPIRY_TASKS.add(task)
        task.add_done_callback(RED_ENVELOPE_EXPIRY_TASKS.discard)

    async def expire_at_deadline(self, expires_at: float):
        await asyncio.sleep(max(0, expires_at - time.time()))
        await self.close_and_refund()

    async def close_and_refund(self):
        state = get_red_envelope(self.envelope_id)
        if state is None or state["status"] != "active":
            self.khoa_nut()
            self.stop()
            return
        refunded = expire_red_envelope(self.envelope_id)
        state = get_red_envelope(self.envelope_id)
        self.khoa_nut()
        self.stop()
        if self.message and state:
            try:
                await self.message.edit(
                    embed=self.tao_embed(state, refunded=refunded),
                    view=self
                )
            except discord.HTTPException:
                pass

    def tao_embed(self, state=None, refunded: int = 0):
        state = state or get_red_envelope(self.envelope_id)
        if state is None:
            return discord.Embed(title="🧧 Bao lì xì không còn tồn tại")

        status_text = {
            "active": f"Đang mở — kết thúc <t:{int(state['expires_at'])}:R>",
            "completed": "Đã phát hết các suất lì xì.",
            "expired": f"Đã hết hạn; **{refunded:,} {CURRENCY_NAME}** còn lại đã về ngân khố người phát.",
            "cancelled": "Đã đóng do bot khởi động lại; phần quà chưa nhận đã được hoàn.",
        }.get(state["status"], "Đã đóng.")
        embed = discord.Embed(
            title="🧧 Bao lì xì Linh Thạch",
            description=(
                f"<@{state['creator_id']}> phát **{state['total_amount']:,} {CURRENCY_NAME}** "
                f"cho **{state['total_slots']} người**.\n{status_text}"
            ),
            color=0xE74C3C if state["status"] == "active" else 0x95A5A6
        )
        embed.add_field(
            name="Quỹ còn lại",
            value=f"**{state['remaining_amount']:,} {CURRENCY_NAME}**",
            inline=True
        )
        embed.add_field(
            name="Suất còn lại",
            value=f"**{state['remaining_slots']} / {state['total_slots']}**",
            inline=True
        )
        embed.add_field(
            name="Đã nhận",
            value=f"**{state['claimed_count']} người**",
            inline=True
        )
        embed.set_footer(text="Mỗi người nhận một lần; phần chia ngẫu nhiên, tối thiểu 1 Linh Thạch.")
        return embed

    def khoa_nut(self):
        for child in self.children:
            child.disabled = True

    async def on_timeout(self):
        await self.close_and_refund()

    @button(label="Nhận lì xì", style=discord.ButtonStyle.success, emoji="🧧")
    async def claim(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ Chỉ có thể nhận lì xì trong server này.", ephemeral=True
            )
            return
        if interaction.user.bot:
            await interaction.response.send_message(
                "❌ Bot không thể nhận lì xì.", ephemeral=True
            )
            return

        status, amount = claim_red_envelope(
            self.envelope_id, interaction.guild_id, interaction.user.id
        )
        if status == "claimed":
            await interaction.response.send_message(
                f"🧧 Bạn nhận được **{amount:,} {CURRENCY_NAME}** từ bao lì xì!",
                ephemeral=True
            )
            state = get_red_envelope(self.envelope_id)
            if state and state["status"] != "active":
                self.khoa_nut()
                self.stop()
            if state and interaction.message:
                try:
                    await interaction.message.edit(embed=self.tao_embed(state), view=self)
                except discord.HTTPException:
                    pass
            return

        messages = {
            "missing": "❌ Bao lì xì không tồn tại.",
            "wrong_server": "❌ Bao lì xì này thuộc server khác.",
            "duplicate": "🎁 Bạn đã nhận lì xì này rồi.",
            "closed": "⏰ Bao lì xì đã đóng hoặc đã phát hết.",
            "expired": f"⏰ Bao lì xì đã hết hạn; **{amount:,} {CURRENCY_NAME}** còn lại đã được trả cho người phát.",
        }
        await interaction.response.send_message(
            messages.get(status, "❌ Không thể nhận bao lì xì này."),
            ephemeral=True
        )
        if status == "expired":
            self.khoa_nut()
            self.stop()
            state = get_red_envelope(self.envelope_id)
            if state and interaction.message:
                try:
                    await interaction.message.edit(
                        embed=self.tao_embed(state, refunded=amount), view=self
                    )
                except discord.HTTPException:
                    pass


# =========================================================
# 6. LENH SLASH CO BAN
# =========================================================

@bot.tree.command(
    name="blackjack",
    description="Chơi Blackjack bằng Linh Thạch"
)
@app_commands.describe(
    cuoc="Nhập số Linh Thạch muốn cược (từ 10 đến 100.000)",
    muc_cuoc="Chọn cược toàn bộ hoặc một nửa Bảo khố"
)
@app_commands.choices(muc_cuoc=[
    app_commands.Choice(name="ALL — 100% Bảo khố", value="all"),
    app_commands.Choice(name="50% Bảo khố", value="half")
])
async def blackjack(
    interaction: discord.Interaction,
    cuoc: int | None = None,
    muc_cuoc: app_commands.Choice[str] | None = None
):
    if (cuoc is None) == (muc_cuoc is None):
        await interaction.response.send_message(
            "❌ Hãy chọn đúng một cách cược: nhập `cuoc` hoặc chọn `muc_cuoc` "
            "(ALL/50% Bảo khố).",
            ephemeral=True
        )
        return

    if cuoc is not None:
        if not MIN_BET <= cuoc <= MAX_BET:
            await interaction.response.send_message(
                f"❌ Cược nhập trực tiếp phải từ **{MIN_BET:,}** đến "
                f"**{MAX_BET:,} {CURRENCY_NAME}**.",
                ephemeral=True
            )
            return
        so_tien_cuoc = cuoc
    else:
        bao_kho = get_balance(interaction.user.id)
        if muc_cuoc.value == "all":
            so_tien_cuoc = bao_kho
        else:
            so_tien_cuoc = bao_kho // 2
        if so_tien_cuoc < MIN_BET:
            await interaction.response.send_message(
                f"❌ Mức cược này chưa đạt tối thiểu **{MIN_BET:,} "
                f"{CURRENCY_NAME}**. Bảo khố hiện tại: **{bao_kho:,}**.",
                ephemeral=True
            )
            return

    if not place_bet(interaction.user.id, so_tien_cuoc):
        balance = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"❌ Bạn không đủ Linh Thạch để cược **{so_tien_cuoc:,}**.\n"
            f"Bảo khố hiện tại: **{balance:,} {CURRENCY_NAME}**.",
            ephemeral=True
        )
        return

    view = BlackjackView(interaction.user, so_tien_cuoc)

    await interaction.response.send_message(
        embed=view.tao_embed(),
        view=view
    )
    view.message = await interaction.original_response()

    await view.xu_ly_blackjack_dau_van()


@bot.tree.command(
    name="balance",
    description="Xem Bảo khố Linh Thạch"
)
async def balance(interaction: discord.Interaction):
    amount = get_balance(interaction.user.id)
    await interaction.response.send_message(
        f"💎 Bảo khố của bạn hiện có **{amount:,} {CURRENCY_NAME}**."
    )


@bot.tree.command(
    name="shop_vat_pham",
    description="Xem shop vật phẩm Linh Thạch"
)
async def shop_vat_pham(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛒 Shop vật phẩm",
        description="Dùng `/mua_vat_pham` để mua vật phẩm hoặc role.",
        color=0xF1C40F
    )
    for item in ITEM_SHOP.values():
        embed.add_field(
            name=f"{item['name']} — {item['price']:,} {CURRENCY_NAME}",
            value=item["description"],
            inline=False
        )
    embed.set_footer(
        text="Dùng /dung_vat_pham để kích hoạt giáp hoặc Đan Dược (+5% thưởng trong 10 phút); Độ Kiếp miễn nhiễm Thiên Lôi và tăng 50% thưởng Blackjack."
    )
    await interaction.response.send_message(embed=embed)


async def cap_do_kiep(interaction: discord.Interaction, item: dict):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ Chỉ có thể mua role Độ Kiếp trong server.", ephemeral=True
        )
        return

    member = interaction.user
    guild = interaction.guild
    role_name = item["role_name"]
    role = discord.utils.get(guild.roles, name=role_name)
    prerequisite = discord.utils.get(guild.roles, name="Hợp Thể")
    if role is None:
        refund_pending_role_item_purchase(
            member.id, guild.id, "do_kiep", item["price"]
        )
        await interaction.response.send_message(
            f"❌ Server chưa tạo role **{role_name}**.", ephemeral=True
        )
        return
    if prerequisite is None:
        refund_pending_role_item_purchase(
            member.id, guild.id, "do_kiep", item["price"]
        )
        await interaction.response.send_message(
            "❌ Server chưa tạo role tiền đề **Hợp Thể**.", ephemeral=True
        )
        return
    if not guild.me or not guild.me.guild_permissions.manage_roles or not role.is_assignable():
        refund_pending_role_item_purchase(
            member.id, guild.id, "do_kiep", item["price"]
        )
        await interaction.response.send_message(
            "❌ Bot cần quyền Manage Roles và role Độ Kiếp phải nằm dưới "
            "role cao nhất của bot.", ephemeral=True
        )
        return

    lock_key = (guild.id, member.id, "do_kiep")
    lock = ROLE_PURCHASE_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        if role in member.roles:
            complete_role_item_purchase(member.id, guild.id, "do_kiep")
            await interaction.response.send_message(
                "✅ Bạn đã sở hữu role **Độ Kiếp** rồi.", ephemeral=True
            )
            return
        if prerequisite not in member.roles:
            refund_pending_role_item_purchase(
                member.id, guild.id, "do_kiep", item["price"]
            )
            await interaction.response.send_message(
                "🔒 Để mua **🗡️ Độ Kiếp**, bạn phải sở hữu role **Hợp Thể** trước.",
                ephemeral=True
            )
            return

        status, balance = reserve_role_item_purchase(
            member.id, guild.id, "do_kiep", item["price"]
        )
        if status == "insufficient":
            await interaction.response.send_message(
                f"❌ Không đủ Linh Thạch mua **{item['name']}** "
                f"(giá **{item['price']:,}**). Bảo khố hiện có "
                f"**{balance:,} {CURRENCY_NAME}**.",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason="Mua role Độ Kiếp trong shop vật phẩm")
        except (discord.Forbidden, discord.HTTPException) as error:
            refunded = refund_pending_role_item_purchase(
                member.id, guild.id, "do_kiep", item["price"]
            )
            print(
                f"Không cấp được role Độ Kiếp cho {member.id}; "
                f"đã hoàn tiền={refunded}: {error}"
            )
            await interaction.response.send_message(
                "❌ Bot cấp role Độ Kiếp không thành công; Linh Thạch đã được "
                "hoàn lại nếu giao dịch mới được tạo.", ephemeral=True
            )
            return

        complete_role_item_purchase(member.id, guild.id, "do_kiep")
        await interaction.response.send_message(
            f"{member.mention} đã độ kiếp thành công từ Hợp Thể lên Độ Kiếp!"
        )


@bot.tree.command(
    name="mua_vat_pham",
    description="Dùng Linh Thạch mua vật phẩm trong shop"
)
@app_commands.describe(vat_pham="Chọn vật phẩm muốn mua")
@app_commands.choices(vat_pham=[
    app_commands.Choice(
        name=f"{item['name']} — {item['price']:,} Linh Thạch",
        value=item_key
    )
    for item_key, item in ITEM_SHOP.items()
])
async def mua_vat_pham(
    interaction: discord.Interaction,
    vat_pham: app_commands.Choice[str]
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Lệnh mua vật phẩm chỉ dùng được trong server.", ephemeral=True
        )
        return
    item = ITEM_SHOP.get(vat_pham.value)
    if item is None:
        await interaction.response.send_message(
            "❌ Vật phẩm này không tồn tại trong shop.", ephemeral=True
        )
        return
    if item.get("kind") == "role":
        await cap_do_kiep(interaction, item)
        return

    purchase = buy_item(interaction.user.id, vat_pham.value)
    if purchase is None:
        bao_kho = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"❌ Bảo khố không đủ mua **{item['name']}** "
            f"(giá **{item['price']:,} {CURRENCY_NAME}**). "
            f"Bảo khố hiện có: **{bao_kho:,} {CURRENCY_NAME}**.",
            ephemeral=True
        )
        return
    balance, quantity = purchase
    await interaction.response.send_message(
        f"✅ Đã mua **{item['name']}**. Túi đồ hiện có **{quantity}** chiếc; "
        f"Bảo khố còn **{balance:,} {CURRENCY_NAME}**.",
        ephemeral=True
    )


@bot.tree.command(
    name="kho_vat_pham",
    description="Xem vật phẩm bạn đang sở hữu"
)
async def kho_vat_pham(interaction: discord.Interaction):
    inventory = get_item_inventory(interaction.user.id)
    active_potion_seconds = get_reward_buff_seconds(interaction.user.id)
    if active_potion_seconds:
        inventory["active_dan_duoc_truong_sinh"] = active_potion_seconds
    active_armor = get_active_protection_charges(
        interaction.user.id, "toa_tu_hoang_kim_giap"
    )
    if active_armor:
        inventory["active_toa_tu_hoang_kim_giap"] = active_armor
    if isinstance(interaction.user, discord.Member) and any(
        role.name == "Độ Kiếp" for role in interaction.user.roles
    ):
        inventory["do_kiep_owned"] = 1
    if not inventory:
        await interaction.response.send_message(
            "🎒 Túi đồ của bạn hiện đang trống. Dùng `/shop_vat_pham` để xem shop.",
            ephemeral=True
        )
        return
    lines = []
    for key, quantity in inventory.items():
        if key == "active_dan_duoc_truong_sinh":
            lines.append(
                "**🧪 Đan Dược Trường Sinh đang hiệu lực** — tăng **5%** "
                f"thưởng, còn **{dinh_dang_thoi_gian(quantity)}**"
            )
        elif key == "active_toa_tu_hoang_kim_giap":
            lines.append(f"**🛡️ Khiên Tỏa Tử Hoàng Kim Giáp đang kích hoạt** × **{quantity} lượt chắn**")
        elif key == "do_kiep_owned":
            lines.append("**🗡️ Độ Kiếp** — role đang sở hữu")
        else:
            lines.append(
                f"**{ITEM_SHOP.get(key, {'name': key})['name']}** × **{quantity}**"
            )
    await interaction.response.send_message(
        "🎒 **Túi vật phẩm của bạn**\n" + "\n".join(lines),
        ephemeral=True
    )


@bot.tree.command(
    name="dung_vat_pham",
    description="Dùng vật phẩm đã mua; chọn mục tiêu nếu dùng Thiên Lôi"
)
@app_commands.describe(
    vat_pham="Chọn vật phẩm tiêu thụ để dùng",
    nguoi_choi="Chỉ chọn người chơi này khi dùng Thiên Lôi"
)
@app_commands.choices(vat_pham=[
    app_commands.Choice(
        name=item["name"],
        value=item_key
    )
    for item_key, item in ITEM_SHOP.items()
    if item.get("kind") == "consumable"
])
async def dung_vat_pham(
    interaction: discord.Interaction,
    vat_pham: app_commands.Choice[str],
    nguoi_choi: discord.Member | None = None
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ Lệnh dùng vật phẩm chỉ dùng được trong server.",
            ephemeral=True
        )
        return

    if vat_pham.value == "thien_loi":
        if nguoi_choi is None:
            await interaction.response.send_message(
                "❌ Khi dùng Thiên Lôi, hãy chọn người chơi mục tiêu.",
                ephemeral=True
            )
            return
        if nguoi_choi.bot:
            await interaction.response.send_message(
                "❌ Thiên Lôi chỉ có thể nhắm vào người chơi, không thể nhắm vào bot.",
                ephemeral=True
            )
            return
        if nguoi_choi.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Bạn không thể dùng Thiên Lôi lên chính mình.", ephemeral=True
            )
            return

        do_kiep_role = discord.utils.get(interaction.guild.roles, name="Độ Kiếp")
        target_is_do_kiep = do_kiep_role is not None and do_kiep_role in nguoi_choi.roles
        status, damage, _target_balance = use_thunder_item(
            interaction.user.id,
            nguoi_choi.id,
            target_is_do_kiep=target_is_do_kiep
        )
        if status == "protected":
            await interaction.response.send_message(
                f"⚡ Nhận thiên phạt từ {interaction.user.mention}, ý chí, kiên cường, "
                f"bất khuất của **🛡️ Tỏa Tử Hoàng Kim Giáp** đã bảo vệ "
                f"{nguoi_choi.mention} khỏi việc bị thoát kiếp!"
            )
            return
        if status != "success":
            messages = {
                "no_item": "❌ Bạn không có **⚡ Thiên Lôi** trong túi đồ. Hãy mua tại `/shop_vat_pham`.",
                "do_kiep_immunity": (
                    f"⚔️ {nguoi_choi.mention} sở hữu cảnh giới **Độ Kiếp** và miễn nhiễm "
                    "với Thiên Lôi. Vật phẩm của bạn chưa bị tiêu thụ."
                ),
                "target_balance_too_low": (
                    "❌ Bảo khố của mục tiêu quá thấp để 3% tạo ra ít nhất "
                    "1 Linh Thạch; vật phẩm chưa bị tiêu thụ."
                ),
            }
            await interaction.response.send_message(
                messages.get(status, "❌ Không thể sử dụng Thiên Lôi."),
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"⚡ {interaction.user.mention} đã thành công triệu hồi Lôi Thần "
            f"giáng lôi phạt xuống {nguoi_choi.mention}, khiến họ mất đi "
            f"**{damage:,} {CURRENCY_NAME}**!"
        )
        return

    if vat_pham.value == "toa_tu_hoang_kim_giap":
        if nguoi_choi is not None:
            await interaction.response.send_message(
                "❌ Không chọn mục tiêu khi dùng Tỏa Tử Hoàng Kim Giáp; "
                "giáp sẽ bảo vệ chính bạn khỏi lần Thiên Lôi tiếp theo.",
                ephemeral=True
            )
            return
        charges = activate_armor(interaction.user.id)
        if charges is None:
            await interaction.response.send_message(
                "❌ Bạn không có **🛡️ Tỏa Tử Hoàng Kim Giáp** trong túi đồ.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🛡️ Bạn đã dùng **Tỏa Tử Hoàng Kim Giáp**. Một chiếc đã được "
            f"tiêu thụ và sẽ chặn một lần Thiên Lôi. Số lượt chắn đang kích hoạt: **{charges}**.",
            ephemeral=True
        )
        return

    if vat_pham.value == "dan_duoc_truong_sinh":
        if nguoi_choi is not None:
            await interaction.response.send_message(
                "❌ Đan Dược Trường Sinh chỉ dùng cho chính bạn; không chọn mục tiêu.",
                ephemeral=True
            )
            return
        expires_at = activate_reward_potion(interaction.user.id)
        if expires_at is None:
            await interaction.response.send_message(
                "❌ Bạn không có **🧪 Đan Dược Trường Sinh** trong túi đồ.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🧪 {interaction.user.mention} đã sử dụng vật phẩm "
            "**Đan Dược Trường Sinh**.\n"
            "✨ Mỗi lần nhận thưởng thành công trong 10 phút tới được cộng thêm **5%**."
        )
        return

    await interaction.response.send_message(
        "❌ Vật phẩm này chưa thể dùng bằng lệnh này.", ephemeral=True
    )


@bot.tree.command(
    name="phat_bao_li_xi",
    description="Phát bao lì xì ngẫu nhiên cho người chơi trong server"
)
@app_commands.describe(
    tong_tien="Tổng quỹ Linh Thạch sẽ phát",
    so_suat="Số người tối đa được nhận (từ 1 đến 100)"
)
async def phat_bao_li_xi(
    interaction: discord.Interaction,
    tong_tien: app_commands.Range[int, 1, 1_000_000_000],
    so_suat: app_commands.Range[int, 1, 100]
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Lệnh phát bao lì xì chỉ dùng được trong server.",
            ephemeral=True
        )
        return
    if tong_tien < so_suat:
        await interaction.response.send_message(
            "❌ Tổng quỹ phải ít nhất bằng số suất để mỗi người nhận được "
            "tối thiểu 1 Linh Thạch.",
            ephemeral=True
        )
        return

    expires_at = time.time() + 10 * 60
    created = create_red_envelope(
        interaction.user.id,
        interaction.guild.id,
        interaction.channel_id,
        tong_tien,
        so_suat,
        expires_at
    )
    if created is None:
        bao_kho = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"❌ Bảo khố không đủ để phát **{tong_tien:,} {CURRENCY_NAME}**. "
            f"Bảo khố hiện tại: **{bao_kho:,} {CURRENCY_NAME}**.",
            ephemeral=True
        )
        return

    envelope_id, _remaining_balance = created
    view = RedEnvelopeView(envelope_id)
    embed = view.tao_embed()
    content = (
        f"🧧 {interaction.user.mention} phát bao lì xì **{tong_tien:,} "
        f"{CURRENCY_NAME}** cho tối đa **{so_suat} người**! "
        "Bấm nút bên dưới để nhận ngẫu nhiên — mỗi người nhận một lần. "
        "Bao lì xì hết hạn sau 10 phút."
    )
    try:
        await interaction.response.send_message(content=content, embed=embed, view=view)
    except discord.HTTPException:
        expire_red_envelope(envelope_id)
        return

    view.schedule_expiry(expires_at)
    try:
        view.message = await interaction.original_response()
        set_red_envelope_message(envelope_id, view.message.id)
    except discord.HTTPException as error:
        print(f"Không lưu được message bao lì xì {envelope_id}: {error}")


@bot.tree.command(
    name="chuyen_linh_thach",
    description="Tặng Linh Thạch cho một người chơi trong server"
)
@app_commands.describe(
    nguoi_nhan="Người chơi nhận Linh Thạch",
    so_luong="Số Linh Thạch muốn tặng"
)
async def chuyen_linh_thach(
    interaction: discord.Interaction,
    nguoi_nhan: discord.Member,
    so_luong: app_commands.Range[int, 1, 1_000_000_000]
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ Lệnh chuyển Linh Thạch chỉ dùng được trong server.",
            ephemeral=True
        )
        return

    nguoi_gui = interaction.user
    if nguoi_nhan.bot:
        await interaction.response.send_message(
            "❌ Bạn chỉ có thể tặng Linh Thạch cho người chơi, không thể tặng bot.",
            ephemeral=True
        )
        return
    if nguoi_nhan.id == nguoi_gui.id:
        await interaction.response.send_message(
            "❌ Bạn không thể tự chuyển Linh Thạch cho chính mình.",
            ephemeral=True
        )
        return

    balances = transfer_balance(nguoi_gui.id, nguoi_nhan.id, so_luong)
    if balances is None:
        bao_kho = get_balance(nguoi_gui.id)
        await interaction.response.send_message(
            f"❌ Bảo khố không đủ để tặng **{so_luong:,} {CURRENCY_NAME}**. "
            f"Bảo khố hiện tại: **{bao_kho:,} {CURRENCY_NAME}**.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"{nguoi_nhan.mention} đã nhận được **{so_luong:,} {CURRENCY_NAME}** "
        f"của {nguoi_gui.mention}, xem như là quà bố thí."
    )


@bot.tree.command(
    name="shop",
    description="Xem các role tu luyện và giá bằng Linh Thạch"
)
async def shop(interaction: discord.Interaction):
    lines = []
    for index, (role_name, price) in enumerate(ROLE_SHOP):
        prerequisite = (
            f" — cần role **{ROLE_SHOP[index - 1][0]}**"
            if index > 0 else " — chưa cần role trước"
        )
        lines.append(f"**{role_name}** — {price:,} {CURRENCY_NAME}{prerequisite}")

    embed = discord.Embed(
        title="🛒 Shop role tu luyện",
        description=(
            "Dùng `/mua_role` để mua role. Các bậc phải mua lần lượt; "
            "role đã có sẽ không bị gỡ khi thăng cấp.\n\n"
            + "\n".join(lines)
        ),
        color=0x9B59B6
    )
    embed.set_footer(text="Role cần được tạo sẵn trong server để bot có thể cấp.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="mua_role",
    description="Dùng Linh Thạch mua role tu luyện tiếp theo"
)
@app_commands.describe(role="Chọn role muốn mua trong shop")
@app_commands.choices(role=[
    app_commands.Choice(name=f"{role_name} — {price:,} Linh Thạch", value=role_name)
    for role_name, price in ROLE_SHOP
])
async def mua_role(
    interaction: discord.Interaction,
    role: app_commands.Choice[str]
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ Lệnh mua role chỉ dùng được trong server.", ephemeral=True
        )
        return

    member = interaction.user
    index = next(i for i, item in enumerate(ROLE_SHOP) if item[0] == role.value)
    role_name, price = ROLE_SHOP[index]
    target_role = discord.utils.get(interaction.guild.roles, name=role_name)

    if target_role is None:
        await interaction.response.send_message(
            f"❌ Server chưa có role **{role_name}**. Hãy nhờ quản trị viên "
            "tạo role này trước.", ephemeral=True
        )
        return
    if target_role in member.roles:
        await interaction.response.send_message(
            f"✅ Bạn đã có role **{role_name}** rồi.", ephemeral=True
        )
        return

    higher_owned = [name for name, _ in ROLE_SHOP[index + 1:]
                    if discord.utils.get(interaction.guild.roles, name=name)
                    in member.roles]
    if any(owned.name == "Độ Kiếp" for owned in member.roles):
        higher_owned.append("Độ Kiếp")
    if higher_owned:
        await interaction.response.send_message(
            "❌ Bạn đã sở hữu role bậc cao hơn nên không thể mua lại role thấp hơn.",
            ephemeral=True
        )
        return

    if index > 0:
        prerequisite_name = ROLE_SHOP[index - 1][0]
        prerequisite_role = discord.utils.get(
            interaction.guild.roles, name=prerequisite_name
        )
        if prerequisite_role is None:
            await interaction.response.send_message(
                f"❌ Server chưa có role tiền đề **{prerequisite_name}**. "
                "Hãy nhờ quản trị viên tạo role này trước.", ephemeral=True
            )
            return
        if prerequisite_role not in member.roles:
            await interaction.response.send_message(
                f"🔒 Để mua **{role_name}**, bạn cần có role "
                f"**{prerequisite_name}** trước.", ephemeral=True
            )
            return

    if not interaction.guild.me or not interaction.guild.me.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "❌ Bot cần quyền **Manage Roles** để cấp role.", ephemeral=True
        )
        return
    if not target_role.is_assignable():
        await interaction.response.send_message(
            f"❌ Bot không thể cấp role **{role_name}**. Hãy kiểm tra role "
            "có nằm dưới role cao nhất của bot hay không.", ephemeral=True
        )
        return

    remaining = spend_balance(member.id, price)
    if remaining is None:
        balance_now = get_balance(member.id)
        await interaction.response.send_message(
            f"❌ Bạn không đủ Linh Thạch để mua **{role_name}** "
            f"(giá **{price:,}**). Bảo khố: **{balance_now:,}**.",
            ephemeral=True
        )
        return

    try:
        await member.add_roles(target_role, reason=f"Mua role shop: {role_name}")
    except (discord.Forbidden, discord.HTTPException) as error:
        update_balance(member.id, price)
        print(f"Không cấp được role {role_name} cho {member.id}; đã hoàn tiền: {error}")
        await interaction.response.send_message(
            "❌ Bot cấp role không thành công; Linh Thạch đã được hoàn lại. "
            "Hãy thử lại hoặc liên hệ quản trị viên.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Chúc Mừng {member.mention} đã đạt đến cảnh giới **{role_name}**!"
    )


@bot.tree.command(
    name="leaderboard",
    description="Xem bảng xếp hạng người chơi theo Bảo khố Linh Thạch"
)
async def leaderboard(interaction: discord.Interaction):
    rows, total = get_leaderboard(10)
    player_rank = get_player_rank(interaction.user.id)

    if rows:
        medals = ("🥇", "🥈", "🥉")
        lines = []
        for rank, (user_id, amount) in enumerate(rows, start=1):
            marker = medals[rank - 1] if rank <= len(medals) else f"**{rank}.**"
            lines.append(
                f"{marker} <@{user_id}> — **{amount:,} {CURRENCY_NAME}**"
            )
        description = "\n".join(lines)
    else:
        description = "Chưa có dữ liệu người chơi."

    if player_rank:
        rank, amount = player_rank
        description += (
            f"\n\n**Hạng của bạn:** #{rank} / {total}"
            f" — **{amount:,} {CURRENCY_NAME}**"
        )
    else:
        description += "\n\nBạn chưa có trong bảng xếp hạng; hãy dùng `/balance` để bắt đầu."

    embed = discord.Embed(
        title="🏆 Bảng xếp hạng Linh Thạch",
        description=description,
        color=0xF1C40F
    )
    embed.set_footer(text="Xếp hạng toàn bộ người chơi của bot theo Bảo khố hiện tại.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="bang-chuc-nang",
    description="Xem các chức năng của bot"
)
async def bang_chuc_nang(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Bảng chức năng",
        description=(
            "`/blackjack` — Chơi Blackjack: nhập cược 10–100.000 hoặc chọn ALL/50% Bảo khố.\n"
            "Sở hữu role shop sẽ tăng tiền lãi Blackjack khi thắng (chỉ áp dụng role bậc cao nhất).\n"
            "`/balance` — Xem Bảo khố.\n"
            "`/chuyen_linh_thach nguoi_nhan so_luong` — Tặng Linh Thạch cho người chơi trong server.\n"
            "`/phat_bao_li_xi tong_tien so_suat` — Phát bao lì xì ngẫu nhiên, hạn 10 phút.\n"
            "`/leaderboard` — Xem top người chơi và hạng của bạn.\n"
            "`/shop` — Xem role tu luyện và giá.\n"
            "`/mua_role` — Mua role kế tiếp bằng Linh Thạch.\n"
            "`/shop_vat_pham` — Xem shop vật phẩm.\n"
            "`/mua_vat_pham` — Mua vật phẩm bằng Linh Thạch.\n"
            "`/kho_vat_pham` — Xem túi vật phẩm.\n"
            "`/dung_vat_pham` — Chọn Thiên Lôi, kích hoạt giáp hoặc dùng Đan Dược (+5% thưởng trong 10 phút).\n"
            "`/diem_danh` — Điểm danh mỗi 24 giờ.\n"
            "`/khai_thac` — Khai thác mỗi giờ.\n"
            "`/nhiem_vu` — Nhận thưởng mỗi 6 giờ.\n"
            "Nhắn tin hợp lệ — Có cơ hội nhận thưởng mỗi 60 giây.\n"
            "`/admin pay` — Cộng Linh Thạch.\n"
            "`/admin setbalance` — Đặt Bảo khố.\n"
            "`/admin take` — Trừ Linh Thạch."
        ),
        color=0x9B59B6
    )
    embed.set_footer(text="Linh Thạch là điểm, không phải tiền thật.")
    await interaction.response.send_message(embed=embed)


# =========================================================
# 7. CAC LENH KIEM LINH THACH
# =========================================================

@bot.tree.command(
    name="diem_danh",
    description="Điểm danh nhận Linh Thạch mỗi ngày"
)
async def diem_danh(interaction: discord.Interaction):
    await xu_ly_nhan_thuong(interaction, "daily")


@bot.tree.command(
    name="khai_thac",
    description="Khai thác Linh Thạch mỗi giờ"
)
async def khai_thac(interaction: discord.Interaction):
    await xu_ly_nhan_thuong(interaction, "mine")


@bot.tree.command(
    name="nhiem_vu",
    description="Nhận thưởng nhiệm vụ định kỳ"
)
async def nhiem_vu(interaction: discord.Interaction):
    await xu_ly_nhan_thuong(interaction, "mission")


# =========================================================
# 8. THUONG KHI TRO CHUYEN
# =========================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    # Chi xet tin nhan dai it nhat 5 ky tu
    if len(message.content.strip()) >= 5:
        success, reward, _bonus = claim_reward(message.author.id, "chat")

        # Khong gui thong bao moi tin nhan de tranh spam kenh
        # Nguoi choi co the dung /balance de xem so du.

    await bot.process_commands(message)


# =========================================================
# 9. LENH QUAN TRI
# =========================================================

admin_group = app_commands.Group(
    name="admin",
    description="Quản lý Linh Thạch"
)


def la_quan_tri(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.administrator
    )


@admin_group.command(
    name="pay",
    description="Cộng Linh Thạch cho người chơi"
)
@app_commands.describe(
    nguoi_choi="Người nhận",
    so_luong="Số Linh Thạch muốn cộng"
)
async def admin_pay(
    interaction: discord.Interaction,
    nguoi_choi: discord.Member,
    so_luong: app_commands.Range[int, 1, 1000000000]
):
    if not la_quan_tri(interaction):
        await interaction.response.send_message(
            "❌ Bạn cần quyền Administrator.",
            ephemeral=True
        )
        return

    balance = update_balance(nguoi_choi.id, so_luong)
    await interaction.response.send_message(
        f"✅ Đã cộng **{so_luong:,} {CURRENCY_NAME}** cho "
        f"{nguoi_choi.mention}.\n"
        f"Bảo khố mới: **{balance:,} {CURRENCY_NAME}**."
    )


@admin_group.command(
    name="setbalance",
    description="Đặt Bảo khố Linh Thạch"
)
@app_commands.describe(
    nguoi_choi="Người cần chỉnh Bảo khố",
    so_luong="Mức Bảo khố mới"
)
async def admin_setbalance(
    interaction: discord.Interaction,
    nguoi_choi: discord.Member,
    so_luong: app_commands.Range[int, 0, 1000000000]
):
    if not la_quan_tri(interaction):
        await interaction.response.send_message(
            "❌ Bạn không có đủ căn cơ.",
            ephemeral=True
        )
        return

    balance = set_balance(nguoi_choi.id, so_luong)
    await interaction.response.send_message(
        f"✅ Bảo khố của {nguoi_choi.mention} hiện là "
        f"**{balance:,} {CURRENCY_NAME}**."
    )


@admin_group.command(
    name="take",
    description="Trừ Linh Thạch của người chơi"
)
@app_commands.describe(
    nguoi_choi="Người bị trừ",
    so_luong="Số Linh Thạch cần trừ"
)
async def admin_take(
    interaction: discord.Interaction,
    nguoi_choi: discord.Member,
    so_luong: app_commands.Range[int, 1, 1000000000]
):
    if not la_quan_tri(interaction):
        await interaction.response.send_message(
            "❌ Bạn cần quyền Administrator.",
            ephemeral=True
        )
        return

    balance = update_balance(nguoi_choi.id, -so_luong)
    await interaction.response.send_message(
        f"✅ Đã trừ **{so_luong:,} {CURRENCY_NAME}** của "
        f"{nguoi_choi.mention}.\n"
        f"Bảo khố mới: **{balance:,} {CURRENCY_NAME}**."
    )


bot.tree.add_command(admin_group)


# =========================================================
# 10. KHOI DONG BOT
# =========================================================

async def health_check(request: web.Request) -> web.Response:
    """Simple HTTP 200 endpoint for UptimeRobot and host health checks."""
    return web.Response(text="OK\n", content_type="text/plain")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health-check đang chạy trên cổng {port} (GET / hoặc /health).")
    return runner


@bot.event
async def on_ready():
    global RED_ENVELOPE_RECOVERY_DONE
    print(f"Bot đã đăng nhập: {bot.user}")

    if not RED_ENVELOPE_RECOVERY_DONE:
        RED_ENVELOPE_RECOVERY_DONE = True
        recovered = refund_open_red_envelopes_after_restart()
        for envelope_id, _creator_id, channel_id, message_id, refunded in recovered:
            if not message_id:
                continue
            try:
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
                view = RedEnvelopeView(envelope_id)
                await message.edit(
                    content=(
                        "🧧 Bao lì xì đã đóng do bot khởi động lại. "
                        f"Phần chưa nhận **{refunded:,} {CURRENCY_NAME}** "
                        "đã được hoàn về ngân khố người phát."
                    ),
                    embed=view.tao_embed(),
                    view=None
                )
            except discord.HTTPException as error:
                print(f"Không cập nhật được bao lì xì {envelope_id} sau restart: {error}")

    try:
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ {len(synced)} lệnh slash.")
    except Exception as error:
        print(f"Lỗi đồng bộ lệnh: {error}")


async def main():
    if not TOKEN:
        raise RuntimeError(
            "Không tìm thấy DISCORD_TOKEN. "
            "Hãy tạo file .env cùng thư mục với bot.py."
        )

    init_db()
    web_runner = await start_health_server()

    try:
        async with bot:
            await bot.start(TOKEN)
    except discord.LoginFailure as error:
        raise RuntimeError(
            "Discord từ chối DISCORD_TOKEN. Hãy tạo lại Bot Token trong "
            "Discord Developer Portal, cập nhật secret DISCORD_TOKEN trên "
            "nền tảng deploy rồi khởi động lại bot. Không gửi token cho ai."
        ) from error
    except discord.HTTPException as error:
        if error.status == 401:
            raise RuntimeError(
                "Discord trả về HTTP 401: DISCORD_TOKEN không hợp lệ hoặc đã "
                "bị thu hồi. Hãy thay bằng Bot Token mới trong secret của môi "
                "trường deploy và khởi động lại bot. Không chia sẻ token."
            ) from error
        raise
    finally:
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
