# Code tham chiếu: Shop vật phẩm và buff thưởng

`/dung_vat_pham` cho phép chọn Thiên Lôi, Tỏa Tử Hoàng Kim Giáp hoặc Đan Dược Trường Sinh. Đan Dược tiêu thụ một vật phẩm và cộng 5% vào mỗi khoản thưởng thành công trong 10 phút; giáp phải được dùng trước để kích hoạt lượt chắn.

Code đầy đủ nằm trong [`bot.py`](./bot.py). Hướng dẫn này trích các phần chính và cách nối chúng với hệ thống shop.

## 1. Khai báo shop

```python
ITEM_SHOP = {
    "thien_loi": {
        "name": "⚡ Giáng Thiên Phạt, Bảo Khổ Lung Lay",
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
    "bao_li_xi": {
        "name": "🧧 Bao Lì Xì",
        "price": 1_000,
        "description": "Quay Tay Vận May Sẽ Đến. Random 10–1.000.000 Linh Thạch.",
        "kind": "consumable",
    },
    "do_kiep": {
        "name": "🗡️ Độ Kiếp",
        "price": 99_999_999,
        "description": "Độ Kiếp Thành Công Nhập Ma hay Tu Tâm Thành Phật Do Bạn Chọn.",
        "kind": "role",
        "role_name": "Độ Kiếp",
    },
}
```

Role Độ Kiếp không phải consumable nên không xuất hiện trong danh sách chọn của `/dung_vat_pham`.

## 2. Lưu inventory và khiên đã kích hoạt

Đặt trong `init_db()`. Bảng inventory chứa đồ chưa dùng; bảng active protections lưu số lượt chắn đã kích hoạt. Khi bot chạy lại, `init_db()` tạo các bảng còn thiếu mà không xóa dữ liệu cũ.

```python
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
```

## 3. Mua vật phẩm

`buy_item()` trừ tiền và cộng số lượng trong cùng transaction `BEGIN IMMEDIATE`. Vì vậy hai lệnh mua đồng thời không thể tiêu vượt Bảo khố; nếu không đủ tiền thì rollback và không nhận item.

```python
def buy_item(user_id: int, item_key: str):
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
```

## 4. Dùng giáp để kích hoạt khiên

Hàm tiêu thụ một giáp từ inventory, sau đó cộng một lượt chắn vào bảng `active_protections`. Nếu người dùng không có giáp, trả về `None` và không tạo khiên.

```python
def activate_armor(user_id: int):
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
        charges = conn.execute(
            "SELECT charges FROM active_protections WHERE user_id = ? AND item_key = ?",
            (user_id, item_key)
        ).fetchone()[0]
        conn.commit()
        return int(charges)
```

Một lượt chắn đã kích hoạt sẽ không hết hạn khi bot restart. Người chơi xem số lượt đang chờ trong `/kho_vat_pham`.

## 5. Xử lý đòn Thiên Lôi

`use_thunder_item()` dùng transaction khóa ghi. Thứ tự ưu tiên:

1. Người tấn công phải có Thiên Lôi.
2. Role Độ Kiếp miễn nhiễm; không tiêu thụ Thiên Lôi hay lượt chắn.
3. Nếu mục tiêu có lượt chắn đã kích hoạt: trừ một lượt chắn và một Thiên Lôi; không trừ Bảo khố.
4. Nếu không có bảo vệ: trừ `floor(Bảo khố × 3 / 100)` và tiêu thụ Thiên Lôi.
5. Nếu 3% làm tròn thành 0, rollback; Thiên Lôi không bị tiêu thụ.

```python
def use_thunder_item(user_id: int, target_id: int, target_is_do_kiep: bool = False):
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
        shield = conn.execute(
            "SELECT charges FROM active_protections "
            "WHERE user_id = ? AND item_key = 'toa_tu_hoang_kim_giap'",
            (target_id,)
        ).fetchone()
        if shield and shield[0] > 0:
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
```

## 6. Slash command chọn vật phẩm

```python
@bot.tree.command(
    name="dung_vat_pham",
    description="Dùng vật phẩm đã mua; chọn mục tiêu nếu dùng Thiên Lôi"
)
@app_commands.describe(
    vat_pham="Chọn vật phẩm tiêu thụ để dùng",
    nguoi_choi="Chỉ chọn người chơi này khi dùng Thiên Lôi"
)
@app_commands.choices(vat_pham=[
    app_commands.Choice(name=item["name"], value=item_key)
    for item_key, item in ITEM_SHOP.items()
    if item.get("kind") == "consumable"
])
async def dung_vat_pham(
    interaction: discord.Interaction,
    vat_pham: app_commands.Choice[str],
    nguoi_choi: discord.Member | None = None
):
    # Trong handler:
    # - nếu vat_pham.value == "thien_loi", bắt buộc có nguoi_choi,
    #   kiểm tra target không phải bot/chính mình, kiểm tra role Độ Kiếp,
    #   rồi gọi use_thunder_item(...).
    # - nếu vat_pham.value == "toa_tu_hoang_kim_giap", không nhận target,
    #   gọi activate_armor(interaction.user.id), báo số lượt chắn đang kích hoạt.
    # - nếu vat_pham.value == "dan_duoc_truong_sinh", không nhận target,
    #   gọi activate_reward_potion(interaction.user.id).
    # - nếu vat_pham.value == "bao_li_xi", không nhận target,
    #   gọi open_lucky_envelope(quantity=so_luong); mỗi bao có một lần
    #   random 10–1.000.000 riêng, cộng role/buff cho từng bao.
    ...
```

Handler hoàn chỉnh với mọi phản hồi lỗi và thông báo thành công nằm trong `bot.py`; snippet trên thể hiện phần đăng ký lệnh và choices. Discord hiển thị tên command dạng ASCII `/dung_vat_pham` (tên slash command không chứa dấu hoặc khoảng trắng); menu `vat_pham` là nơi người chơi chọn vật phẩm.

## 7. Buff thưởng Đan Dược Trường Sinh

Khi dùng thuốc, `activate_reward_potion()` tiêu thụ một món và ghi thời điểm hết hạn vào `user_reward_buffs`. Dùng thêm khi buff đang chạy làm mới thời hạn thành 10 phút kể từ lần dùng mới nhất; mức cộng vẫn là 5%, không cộng dồn phần trăm.

```python
DAN_DUOC_DURATION_SECONDS = 10 * 60
DAN_DUOC_REWARD_BONUS_PERCENT = 5
REWARD_BUFFABLE_TYPES = frozenset(REWARD_SETTINGS)

# Trong claim_reward(), sau khi kiểm tra cooldown và rút thưởng gốc:
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
return True, reward, bonus
```

`REWARD_SETTINGS` hiện gồm điểm danh, khai thác, nhiệm vụ và chat. Các lệnh này không có trạng thái thắng/thua; Đan Dược tăng số Linh Thạch của mỗi lần nhận thưởng thành công, không thay đổi xác suất Blackjack. Phần trăm được tính trên khoản thưởng gốc đã random, chia nguyên xuống.

## 8. Dùng trong server

1. `/shop_vat_pham` — xem giá.
2. `/mua_vat_pham` — mua vật phẩm trong shop.
3. `/dung_vat_pham` — chọn Thiên Lôi để nhắm thành viên; giáp để kích hoạt lượt chắn; Đan Dược để bật buff +5% trong 10 phút; hoặc Bao Lì Xì và `so_luong` 1–50 để mở nhiều bao với lượt random độc lập. Kết quả từng bao nằm trong field riêng của embed; phần mô tả embed tóm tắt tổng thưởng và bonus.
4. `/kho_vat_pham` — xem vật phẩm chưa dùng, lượt chắn và thời gian buff còn lại.

Sau khi cập nhật, khởi động/redeploy bot để `bot.tree.sync()` đăng ký lựa chọn vật phẩm. Các bảng `active_protections` và `user_reward_buffs` được tạo tự động bởi `init_db()`.
