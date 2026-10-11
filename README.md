# Idk

## UptimeRobot health check

The bot serves a lightweight HTTP health endpoint alongside the Discord connection:

- `GET /` — responds with HTTP 200 and `OK`
- `GET /health` — responds with HTTP 200 and `OK`

The server binds to `0.0.0.0` and uses the hosting platform's `PORT` environment variable (default: `8080`). Set up an UptimeRobot **HTTP(s)** monitor using the public URL of the running service, optionally ending in `/health`. The hosting platform must expose that port publicly and keep the bot process running; the endpoint cannot make a stopped or sleeping host stay awake by itself.

## Discord HTTP 401

If Discord returns `401 Unauthorized`, the bot token configured in the running environment is invalid, revoked, or copied incorrectly. Create/reset the bot token in the Discord Developer Portal, replace the `DISCORD_TOKEN` secret in the hosting platform, then restart/redeploy the bot. The code trims accidental whitespace and an optional leading `Bot ` prefix, but cannot repair an expired or revoked token. Never commit or share the token.
