# Idk

## UptimeRobot health check

The bot serves a lightweight HTTP health endpoint alongside the Discord connection:

- `GET /` — responds with HTTP 200 and `OK`
- `GET /health` — responds with HTTP 200 and `OK`

The server binds to `0.0.0.0` and uses the hosting platform's `PORT` environment variable (default: `8080`). Set up an UptimeRobot **HTTP(s)** monitor using the public URL of the running service, optionally ending in `/health`. The hosting platform must expose that port publicly and keep the bot process running; the endpoint cannot make a stopped or sleeping host stay awake by itself.

## Persistent player balances on Render

The player balances, inventories, cooldowns, and other game state are stored in SQLite. Render's normal service filesystem is ephemeral: redeploys, restarts, or instance replacement can discard a database stored beside the source code, making players appear to reset to the starting balance. The bot now accepts an absolute `DATABASE_PATH` and prints the selected path at startup; if a legacy `linh_thach.db` exists next to `bot.py`, it copies the database to the configured path once without overwriting an existing destination.

To keep data across deployments, attach a **Render Persistent Disk** to the service with mount path `/var/data`, then set the environment variable `DATABASE_PATH` to `/var/data/linh_thach.db` and redeploy. Merely setting the variable is not enough—the directory must be backed by the attached disk. Check startup logs for `SQLite database: /var/data/linh_thach.db`. If the bot has already reset and the old ephemeral file is gone, the code cannot reconstruct the lost balances; restore a prior backup if available. Keep the same disk and database path when redeploying.

## Discord HTTP 401

If Discord returns `401 Unauthorized`, the bot token configured in the running environment is invalid, revoked, or copied incorrectly. Create/reset the bot token in the Discord Developer Portal, replace the `DISCORD_TOKEN` secret in the hosting platform, then restart/redeploy the bot. The code trims accidental whitespace and an optional leading `Bot ` prefix, but cannot repair an expired or revoked token. Never commit or share the token.

## Blackjack role bet bonuses

Players receive an additional profit bonus when they win Blackjack, calculated from the original wager: Luyện Khí Kỳ 1%, Trúc Cơ Kỳ 3%, Kết Đan Kỳ 5%, Nguyên Anh Kỳ 8%, Hóa Thần Kỳ 10%, Luyện Hư 12%, Hợp Thể 15%, and Đại Thừa 20%. Only the highest listed role the player currently has in the server is used. The bonus is rounded down to a whole Linh Thạch and does not apply to losses, ties, or refunded/abandoned games. Natural Blackjack wins also receive the bonus.

For `/blackjack`, either enter a direct wager from 10 to 100,000 Linh Thạch, or choose one preset: **ALL** wagers the entire current Bảo khố, while **50% Bảo khố** wagers half rounded down to a whole Linh Thạch. Choose only one method per game; preset wagers must still meet the 10-Linh-Thạch minimum.

## Server red envelopes

Use `/phat_bao_li_xi tong_tien so_suat` in a server channel to create a public, clickable red envelope. The total pool is reserved from the creator's Bảo khố; each member can claim once for a random whole-number share (at least 1 Linh Thạch per slot). Envelopes expire after 10 minutes, and any unclaimed amount is returned to the creator. Active envelopes are also refunded and closed if the bot restarts before they expire.

## Item shop: Thunder and reward items

Use `/shop_vat_pham` to view all items and `/mua_vat_pham` to buy them. Use `/dung_vat_pham` and select an item from the dropdown. For **⚡ Thiên Lôi** (300,000 Linh Thạch), also select another non-bot member as the target; on a successful hit it removes 3% of their current Bảo khố, rounded down. If 3% rounds to zero or the target is immune, the Thunder item is not consumed.

**🛡️ Tỏa Tử Hoàng Kim Giáp** costs 500,000 Linh Thạch. Buying it only puts it in inventory; it has no effect until the owner selects it in `/dung_vat_pham`. Using it consumes one armor and activates one shield charge, which blocks the next Thunder attack; the attacker's Thunder item is also consumed by the blocked attempt. `/kho_vat_pham` displays activated shield charges. **🗡️ Độ Kiếp** costs 99,999,999 Linh Thạch and can only be purchased by a member who has the Discord role **Hợp Thể**. Buying it grants the server role **Độ Kiếp**, which grants an additional Blackjack win bonus of 50% of the original wager and permanently prevents Thunder attacks. An immune target does not consume the attacker's Thunder item. Create Discord roles named `Hợp Thể` and `Độ Kiếp`; the bot needs Manage Roles and its highest role must be above `Độ Kiếp`.

**🧪 Đan Dược Trường Sinh** costs 10,000 Linh Thạch. Use it via `/dung_vat_pham`; one pill is consumed and gives **+5%** to each successful reward claim for 10 minutes: `/diem_danh`, `/khai_thac`, `/nhiem_vu`, and chat rewards. The bonus is calculated from the randomized base reward and rounded down to a whole Linh Thạch. Using another pill while the effect is active refreshes the timer to 10 minutes; the percentage does not stack. These activities do not currently have win/loss odds, so the pill increases reward amounts and does not change Blackjack odds.
