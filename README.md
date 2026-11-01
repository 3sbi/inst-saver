# Instagram stories → Telegram

A Python worker polls one public Instagram account every five minutes (configurable),
downloads its currently visible photo/video stories oldest first, and posts them
to a Telegram channel through `aiogram/telegram-bot-api:10.1` in local mode.
Retrieval uses [StoryNavigation](https://storynavigation.com/) without an Instagram
account, password, API key, or browser. The worker obtains an anonymous website
session and CSRF token automatically, then calls the same story endpoint as its
web client. This depends on undocumented third-party endpoints and can stop
working if the service changes, blocks requests, or serves stale data.

SQLite remembers story IDs per destination channel across restarts. The first run
posts all currently visible stories; expired or deleted stories cannot be recovered.

## Setup

Requires Docker with Compose, a public source account, and a Telegram bot with
permission to post messages in the channel. Private and Close Friends stories
are not supported.

1. Copy `.env.example` to `.env` and fill in its values. `INSTAGRAM_HANDLE` is the
   source. No `INSTAGRAM_LOGIN` or login step is needed. Set
   `TELEGRAM_CHANNEL=@your_channel` (or a private channel's numeric `-100...` ID).
   Create the bot with [BotFather](https://t.me/BotFather); obtain the additional
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from
   [my.telegram.org/apps](https://my.telegram.org/apps).
2. Before first using this bot with a local Bot API server, call Telegram's cloud
   `logOut` method once. This does not send a channel message:

   ```sh
   docker compose build
   docker compose run --rm --no-deps --entrypoint python worker -c 'import os, requests; r = requests.post("https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/logOut", timeout=30); print(r.json())'
   ```

   Check that the response says `ok: true`. This is a one-time migration step,
   not something to run before every poll. See the official
   [local server documentation](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
   and [logOut reference](https://core.telegram.org/bots/api#logout).
3. Start polling:

   ```sh
   docker compose up -d
   docker compose logs -f worker
   ```

Both containers restart automatically. No Bot API port is published to the host.
Media is streamed to a shared Docker volume and passed as a local file path, so
large videos do not have to be buffered in the worker's memory. Successfully sent
media is removed. Keep the Docker volumes: `docker compose down -v` deletes the
posting history, causing visible stories to be reposted.

Set `POLL_INTERVAL_SECONDS` in `.env` to change the interval (minimum 60).
Recreate the worker with `docker compose up -d` after edits. Polling is sequential;
the interval is the delay after a completed pass. Outages, rate limits, viewer
caching, or a story disappearing between polls can cause missed stories. Expired
stories returned by the viewer are skipped. The worker retries failed polls and
definitive send rejections on later passes. Retrieval errors are logged and do not
mark any stories as sent. Malformed responses fail the poll instead of being
silently treated as an empty story list.

To upgrade from the login-based version, remove `INSTAGRAM_LOGIN` from `.env`
and run `docker compose up -d --build`. Keep the worker data volume: existing
story IDs are compatible and remain deduplicated. The old `instagram-session`
file is unused and can be removed from `/data` after the worker is stopped.

## Deduplication and uncertain delivery

Only one worker can hold the database lock. Sent IDs are retained indefinitely,
and the same story may be sent to a different channel if the destination changes.
Telegram does not provide an idempotency key for these sends: a lost response or
crash can leave delivery uncertain. Such stories are stored as `pending` and are
**not automatically resent**. Their downloaded files remain in the media volume.
This prioritizes avoiding duplicates; review them manually to avoid missing a post.

Stop the worker before inspecting or resolving pending items. The Bot API service
can stay running:

```sh
docker compose stop worker
docker compose run --rm --no-deps worker pending
# Inspect the channel; if STORY_ID was posted:
docker compose run --rm --no-deps worker resolve STORY_ID sent
# Only if it was NOT posted, allow another attempt:
docker compose run --rm --no-deps worker resolve STORY_ID retry
docker compose up -d worker
```

A retry fetches the media again, so the story must still be visible on Instagram.
Retained media for resolved pending stories can be removed from the `/media`
volume after verifying delivery. Back up `/data/stories.sqlite3` and protect `.env`
as credentials.

For one pass instead of the regular loop, stop the worker and run:

```sh
docker compose run --rm worker once
```

## Development checks

Ruff checks lint and formatting; Pyright checks both Python files in strict mode. The Pyright configuration also enables strict checking in VS Code/Pylance.
Select `.venv/bin/python` as the VS Code Python interpreter.
Tests mock both services; they do not log in or publish anything:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
.venv/bin/python -m unittest -v
```
