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


def get_role_bet_bonus(member, bet: int):
    """Return (bonus_amount, bonus_percent, role_name) for the highest role."""
    owned_role_names = {role.name for role in getattr(member, "roles", ())}
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


def claim_reward(user_id: int, reward_type: str):
    """
    Tra ve (True, so_thuong) neu thanh cong.
    Tra ve (False, so_giay_con_cho) neu chua het cooldown.
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
                return False, int(remaining) + 1

        reward = random.randint(
            settings["minimum"],
            settings["maximum"]
        )

        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (reward, user_id)
        )

        conn.execute("""
            INSERT INTO reward_cooldowns (user_id, reward_type, last_claim)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, reward_type)
            DO UPDATE SET last_claim = excluded.last_claim
        """, (user_id, reward_type, now))

        conn.commit()
        return True, reward


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
    success, value = claim_reward(interaction.user.id, reward_type)

    if not success:
        await interaction.response.send_message(
            f"⏳ Bạn đã nhận thưởng **{settings['name']}** gần đây.\n"
            f"Thời gian còn lại: **{dinh_dang_thoi_gian(value)}**.",
            ephemeral=True
        )
        return

    balance = get_balance(interaction.user.id)
    await interaction.response.send_message(
        f"🎉 Bạn nhận được **{value:,} {CURRENCY_NAME}** "
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


# =========================================================
# 6. LENH SLASH CO BAN
# =========================================================

@bot.tree.command(
    name="blackjack",
    description="Chơi Blackjack bằng Linh Thạch"
)
@app_commands.describe(cuoc="Số Linh Thạch muốn cược")
async def blackjack(
    interaction: discord.Interaction,
    cuoc: app_commands.Range[int, MIN_BET, MAX_BET]
):
    if not place_bet(interaction.user.id, cuoc):
        balance = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"❌ Bạn không đủ Linh Thạch để cược **{cuoc:,}**.\n"
            f"Bảo khố hiện tại: **{balance:,} {CURRENCY_NAME}**.",
            ephemeral=True
        )
        return

    view = BlackjackView(interaction.user, cuoc)

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
            "`/blackjack cuoc` — Chơi Blackjack.\n"
            "Sở hữu role shop sẽ tăng tiền lãi Blackjack khi thắng (chỉ áp dụng role bậc cao nhất).\n"
            "`/balance` — Xem Bảo khố.\n"
            "`/chuyen_linh_thach nguoi_nhan so_luong` — Tặng Linh Thạch cho người chơi trong server.\n"
            "`/leaderboard` — Xem top người chơi và hạng của bạn.\n"
            "`/shop` — Xem role tu luyện và giá.\n"
            "`/mua_role` — Mua role kế tiếp bằng Linh Thạch.\n"
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
        success, reward = claim_reward(message.author.id, "chat")

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
    print(f"Bot đã đăng nhập: {bot.user}")

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
