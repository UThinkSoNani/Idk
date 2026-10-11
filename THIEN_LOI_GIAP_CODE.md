# Code tham chiếu: Thiên Lôi và Tỏa Tử Hoàng Kim Giáp

**Lệnh dùng vật phẩm mới:** `/dung_vat_pham` — người chơi chọn Thiên Lôi hoặc Tỏa Tử Hoàng Kim Giáp trong danh sách. Giáp trong inventory **không tự bảo vệ**: người chơi phải dùng để tiêu thụ và kích hoạt trước; trạng thái chắn còn hiệu lực qua lần bot khởi động lại.

Code đầy đủ nằm trong [`bot.py`](./bot.py). Hướng dẫn này trích các phần chính và cách nối chúng với hệ thống shop.

## 1. Khai báo shop

```python
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
    ...
```

Handler hoàn chỉnh với mọi phản hồi lỗi và thông báo thành công nằm trong `bot.py`; snippet trên thể hiện phần đăng ký lệnh và choices. Discord hiển thị tên command dạng ASCII `/dung_vat_pham` (tên slash command không chứa dấu hoặc khoảng trắng); menu `vat_pham` là nơi người chơi chọn vật phẩm.

## 7. Dùng trong server

1. `/shop_vat_pham` — xem giá.
2. `/mua_vat_pham` — mua Thiên Lôi hoặc giáp.
3. `/dung_vat_pham` — chọn Thiên Lôi rồi chọn người chơi mục tiêu; hoặc chọn giáp để tiêu thụ và kích hoạt một lượt chắn cho chính mình.
4. `/kho_vat_pham` — xem vật phẩm chưa dùng và lượt chắn đang kích hoạt.

Sau khi cập nhật, khởi động/redeploy bot để `bot.tree.sync()` đăng ký lệnh mới và gỡ lệnh `/dung_thien_loi` cũ. Bảng `active_protections` được tạo tự động bởi `init_db()`.
