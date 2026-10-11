import os
import random
import sqlite3
import time
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, button
from dotenv import load_dotenv


# =========================================================
# 1. CAU HINH BOT
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_FILE = "linh_thach.db"
CURRENCY_NAME = "Linh Thạch"

STARTING_BALANCE = 1000
MIN_BET = 10
MAX_BET = 100000

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
    with sqlite3.connect(DB_FILE) as conn:
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
        f"💎 Số dư hiện tại: **{balance:,} {CURRENCY_NAME}**."
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

        if ket_qua:
            embed.add_field(name="📋 Kết quả", value=ket_qua, inline=False)

        embed.set_footer(
            text="Điểm trong trò chơi — không có giá trị tiền thật."
        )
        return embed

    def khoa_nut(self):
        for child in self.children:
            child.disabled = True

    async def ket_thuc(self, interaction, ket_qua, tien_hoan=0):
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

        if tien_hoan:
            update_balance(self.user.id, tien_hoan)

        balance = get_balance(self.user.id)
        ket_qua += f"\n💎 Số dư: **{balance:,} {CURRENCY_NAME}**."

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
        update_balance(self.user.id, self.bet)

        if self.message:
            balance = get_balance(self.user.id)
            result = (
                "⏰ Hết 120 giây thao tác. "
                "Ván chơi kết thúc và cược đã được hoàn lại.\n"
                f"💎 Số dư: **{balance:,} {CURRENCY_NAME}**."
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
            result = "🤝 Cả hai đều có Blackjack. Cược được hoàn lại."

        elif player_natural:
            # Tra lai cuoc va thuong lai 3:2
            profit = (self.bet * 3) // 2
            update_balance(self.user.id, self.bet + profit)
            result = (
                "🎉 Blackjack tự nhiên! "
                f"Bạn nhận lại cược và lãi **{profit:,} {CURRENCY_NAME}**."
            )

        else:
            result = (
                "😔 Nhà cái có Blackjack tự nhiên. "
                f"Bạn mất **{self.bet:,} {CURRENCY_NAME}**."
            )

        balance = get_balance(self.user.id)
        result += f"\n💎 Số dư: **{balance:,} {CURRENCY_NAME}**."

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
                f"🎉 Nhà cái quắc điểm ({dealer_score}). "
                f"Bạn thắng và lãi **{self.bet:,} {CURRENCY_NAME}**."
            )
            refund = self.bet * 2

        elif player_score > dealer_score:
            result = (
                f"🎉 Bạn thắng ({player_score} - {dealer_score})! "
                f"Lãi **{self.bet:,} {CURRENCY_NAME}**."
            )
            refund = self.bet * 2

        elif player_score < dealer_score:
            result = (
                f"😔 Bạn thua ({player_score} - {dealer_score}) "
                f"và mất **{self.bet:,} {CURRENCY_NAME}**."
            )
            refund = 0

        else:
            result = (
                f"🤝 Hai bên hòa ({player_score} điểm). "
                "Cược được hoàn lại."
            )
            refund = self.bet

        await self.ket_thuc(interaction, result, refund)


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
            f"Số dư hiện tại: **{balance:,} {CURRENCY_NAME}**.",
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
    description="Xem số dư Linh Thạch"
)
async def balance(interaction: discord.Interaction):
    amount = get_balance(interaction.user.id)
    await interaction.response.send_message(
        f"💎 Bạn đang có **{amount:,} {CURRENCY_NAME}**."
    )


@bot.tree.command(
    name="bang-chuc-nang",
    description="Xem các chức năng của bot"
)
async def bang_chuc_nang(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Bảng chức năng",
        description=(
            "`/blackjack cuoc` — Chơi Blackjack.\n"
            "`/balance` — Xem số dư.\n"
            "`/diem_danh` — Điểm danh mỗi 24 giờ.\n"
            "`/khai_thac` — Khai thác mỗi giờ.\n"
            "`/nhiem_vu` — Nhận thưởng mỗi 6 giờ.\n"
            "Nhắn tin hợp lệ — Có cơ hội nhận thưởng mỗi 60 giây.\n"
            "`/admin pay` — Cộng Linh Thạch.\n"
            "`/admin setbalance` — Đặt số dư.\n"
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
        f"Số dư mới: **{balance:,} {CURRENCY_NAME}**."
    )


@admin_group.command(
    name="setbalance",
    description="Đặt số dư Linh Thạch"
)
@app_commands.describe(
    nguoi_choi="Người cần chỉnh số dư",
    so_luong="Số dư mới"
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
        f"✅ Số dư của {nguoi_choi.mention} hiện là "
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
        f"Số dư mới: **{balance:,} {CURRENCY_NAME}**."
    )


bot.tree.add_command(admin_group)


# =========================================================
# 10. KHOI DONG BOT
# =========================================================

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

    async with bot:
        await bot.start("DISCORD_TOKEN")


if __name__ == "__main__":
    asyncio.run(main())
