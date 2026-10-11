# Idk

## UptimeRobot health check

The bot serves a lightweight HTTP health endpoint alongside the Discord connection:

- `GET /` — responds with HTTP 200 and `OK`
- `GET /health` — responds with HTTP 200 and `OK`

The server binds to `0.0.0.0` and uses the hosting platform's `PORT` environment variable (default: `8080`). Set up an UptimeRobot **HTTP(s)** monitor using the public URL of the running service, optionally ending in `/health`. The hosting platform must expose that port publicly and keep the bot process running; the endpoint cannot make a stopped or sleeping host stay awake by itself.
