"""Poll Instagram stories and send new media through a local Telegram Bot API."""

import argparse
import base64
import fcntl
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlsplit

import requests

LOG = logging.getLogger(__name__)
DATA = Path("/data")
MEDIA = Path("/media")
API = "http://telegram-bot-api:8081"


class TelegramRejected(Exception):
    """Telegram definitively refused a request, so it may be retried."""


class StoryRecord(TypedDict):
    id: str
    type: str
    url: str
    taken_at: str | float
    expiring_at: str | float


@dataclass
class Story:
    mediaid: str
    is_video: bool
    url: str
    video_url: str
    date_utc: float


class ViewerPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.csrf: str | None = None
        self.profile: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "csrf-token":
            self.csrf = attributes.get("content")
        elif tag == "profile-page":
            self.profile = attributes


def fetch_stories(handle: str) -> list[Story]:
    # shortcut: undocumented viewer endpoints may change; update on a fetch/schema error.
    origin = "https://storynavigation.com"
    with requests.Session() as session:
        page = session.get(f"{origin}/user/{handle}", timeout=(10, 60))
        page.raise_for_status()
        parser = ViewerPage()
        parser.feed(page.text)
        if not parser.csrf or parser.profile is None:
            raise ValueError("Viewer page changed or blocked the request")
        session.headers.update(
            {
                "X-CSRF-TOKEN": parser.csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page.url,
                "Accept": "application/json",
            }
        )
        attrs = parser.profile
        if attrs.get("need-to-parse-user-info-prop"):
            response = session.post(
                f"{origin}/get-user-profile",
                json={"userName": handle},
                timeout=(10, 60),
            )
            response.raise_for_status()
            result = response.json()
            if result.get("found") is not True:
                raise ValueError("Public Instagram account not found")
            profile = result["accountInfo"]
        else:
            if not attrs.get("user-found-prop"):
                raise ValueError("Public Instagram account not found")
            profile_json = attrs.get("v-bind:user-info-prop")
            if profile_json is None:
                raise ValueError("Viewer page is missing profile data")
            profile = json.loads(profile_json)
        if profile.get("isPrivate") is not False:
            raise ValueError("Only public Instagram accounts are supported")
        if profile["username"].lower() != handle.lower():
            raise ValueError("Viewer returned a different account")
        response = session.post(
            f"{origin}/get-user-last-stories",
            json={
                "userName": handle,
                "isPrivate": False,
                "instagramUserId": profile["id"],
            },
            timeout=(10, 60),
        )
        response.raise_for_status()
        raw_records = response.json()["lastStories"]
    if not isinstance(raw_records, list):
        raise TypeError("Viewer returned an invalid story list")
    records = cast(list[StoryRecord], raw_records)
    items: list[Story] = []
    for record in records:
        identifier = re.fullmatch(r"([0-9]+)(?:_[0-9]+)?", record["id"])
        if identifier is None or record["type"] not in ("image", "video"):
            raise ValueError("Viewer returned an invalid story ID or media type")
        url = base64.b64decode(record["url"], validate=True).decode("utf-8")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith((".cdninstagram.com", ".fbcdn.net"))
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Viewer returned an unexpected media host")
        if float(record["expiring_at"]) <= time.time():
            continue
        items.append(
            Story(
                mediaid=identifier[1],
                is_video=record["type"] == "video",
                url=url,
                video_url=url,
                date_utc=float(record["taken_at"]),
            )
        )
    return items


def open_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS stories (
        channel TEXT NOT NULL, story_id TEXT NOT NULL,
        status TEXT NOT NULL, message_id INTEGER,
        PRIMARY KEY (channel, story_id)
    )""")
    db.commit()
    return db


def send_story(token: str, channel: str, path: Path, is_video: bool) -> int:
    kind = "video" if is_video else "photo"
    response = requests.post(
        f"{API}/bot{token}/send{kind.title()}",
        data={"chat_id": channel, kind: path.as_uri()},
        timeout=(10, 300),
    )
    result = response.json()
    if result.get("ok") is not True:
        code = result.get("error_code", response.status_code)
        # A server error can occur after delivery; keep it pending for review.
        if isinstance(code, int) and 400 <= code < 500:
            retry_after = result.get("parameters", {}).get("retry_after", 0)
            if retry_after:
                time.sleep(min(int(retry_after), 3600))
            raise TelegramRejected(f"Telegram rejected story (error {code})")
        raise RuntimeError("Uncertain Telegram response; inspect pending stories")
    message_id = result["result"]["message_id"]
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise ValueError("Telegram returned an invalid message ID")
    return message_id


def publish(
    db: sqlite3.Connection,
    channel: str,
    item: Story,
    token: str,
    media_dir: Path = MEDIA,
) -> None:
    story_id = str(item.mediaid)
    key = (channel, story_id)
    row = db.execute(
        "SELECT status FROM stories WHERE channel=? AND story_id=?", key
    ).fetchone()
    if row:
        if row[0] == "pending":
            LOG.warning("Story %s is pending; inspect channel and resolve it", story_id)
        return

    is_video = item.is_video
    url = item.video_url if is_video else item.url
    if not url:
        raise ValueError("Story has no media URL")
    path = media_dir / (story_id + (".mp4" if is_video else ".jpg"))
    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    output.write(chunk)
        path.chmod(0o644)  # The Bot API container runs as a different user.
    except Exception:
        path.unlink(missing_ok=True)
        raise

    # Commit before sending: crashes and lost replies must not trigger duplicates.
    with db:
        db.execute("INSERT INTO stories VALUES (?, ?, 'pending', NULL)", key)
    try:
        message_id = send_story(token, channel, path, is_video)
    except TelegramRejected:
        with db:
            db.execute("DELETE FROM stories WHERE channel=? AND story_id=?", key)
        path.unlink(missing_ok=True)
        raise
    # shortcut: Telegram has no send idempotency key; uncertain sends need manual review.
    with db:
        db.execute(
            "UPDATE stories SET status='sent', message_id=? WHERE channel=? AND story_id=?",
            (message_id, *key),
        )
    path.unlink(missing_ok=True)
    LOG.info("Posted story %s as message %s", story_id, message_id)


def poll(db: sqlite3.Connection, handle: str, channel: str, token: str) -> bool:
    items = fetch_stories(handle)
    failed = False
    for item in sorted(items, key=lambda item: item.date_utc):
        try:
            publish(db, channel, item, token)
        except Exception as error:  # noqa: BLE001 -- Keep polling without logging credential-bearing errors.
            failed = True
            # Request exceptions include URLs with tokens/cookies; never print them.
            LOG.error(
                "Story %s failed (%s); will check again next poll",
                item.mediaid,
                type(error).__name__,
            )
    LOG.info("Poll finished: %s currently visible stories", len(items))
    return not failed


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "once", "pending", "resolve"],
    )
    parser.add_argument("story_id", nargs="?")
    parser.add_argument("resolution", nargs="?", choices=["sent", "retry"])
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    DATA.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    with (DATA / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Another worker is running. Stop it before this command.\n")
        with open_database(DATA / "stories.sqlite3") as db:
            channel = required_env("TELEGRAM_CHANNEL")
            if args.command == "pending":
                for row in db.execute(
                    "SELECT story_id FROM stories WHERE channel=? AND status='pending'",
                    (channel,),
                ):
                    print(row[0])
                return
            if args.command == "resolve":
                if not args.story_id or not args.resolution:
                    parser.error("resolve requires STORY_ID sent|retry")
                with db:
                    if args.resolution == "sent":
                        db.execute(
                            "UPDATE stories SET status='sent' WHERE channel=? AND story_id=? AND status='pending'",
                            (channel, args.story_id),
                        )
                    else:
                        db.execute(
                            "DELETE FROM stories WHERE channel=? AND story_id=? AND status='pending'",
                            (channel, args.story_id),
                        )
                return

            handle = required_env("INSTAGRAM_HANDLE").lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
                parser.error("INSTAGRAM_HANDLE must be an Instagram username")
            token = required_env("TELEGRAM_BOT_TOKEN")
            interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
            if interval < 60:
                parser.error("POLL_INTERVAL_SECONDS must be at least 60")
            while True:
                try:
                    success = poll(db, handle, channel, token)
                    if args.command == "once" and not success:
                        raise SystemExit(1)
                except Exception as error:  # noqa: BLE001 -- Keep polling without logging credential-bearing errors.
                    LOG.error(
                        "Poll failed (%s); check viewer availability and connectivity",
                        type(error).__name__,
                    )
                    if args.command == "once":
                        raise SystemExit(1) from None
                if args.command == "once":
                    return
                time.sleep(interval)


if __name__ == "__main__":
    main()
