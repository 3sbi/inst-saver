import base64
import html
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

import worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name)
        self.database: Path = self.media / "stories.sqlite3"
        self.db = worker.open_database(self.database)
        self.addCleanup(self.db.close)
        self.item = SimpleNamespace(
            mediaid=123,
            is_video=False,
            url="https://example.org/story.jpg",
            date_utc=datetime.now(),
        )
        response = MagicMock()
        response.iter_content.return_value = [b"image"]
        self.get = patch("worker.requests.get")
        self.get_mock = self.get.start()
        self.get_mock.return_value.__enter__.return_value = response
        self.addCleanup(self.get.stop)

    def publish(self, channel: str = "@channel"):
        worker.publish(self.db, channel, self.item, "secret", self.media)

    @patch("worker.send_story", return_value=42)
    def test_sent_story_is_skipped_after_reopening_database(self, send):
        self.publish()
        with worker.open_database(self.database) as reopened:
            worker.publish(reopened, "@channel", self.item, "secret", self.media)
        send.assert_called_once()
        self.assertEqual(
            self.db.execute("SELECT status, message_id FROM stories").fetchone(),
            ("sent", 42),
        )
        self.assertFalse((self.media / "123.jpg").exists())

    @patch("worker.send_story", side_effect=requests.Timeout)
    def test_uncertain_send_is_held_without_reposting(self, send):
        with self.assertRaises(requests.Timeout):
            self.publish()
        self.publish()
        send.assert_called_once()
        self.assertEqual(
            self.db.execute("SELECT status FROM stories").fetchone(), ("pending",)
        )
        self.assertTrue((self.media / "123.jpg").exists())

    @patch("worker.send_story", side_effect=worker.TelegramRejected)
    def test_definitive_rejection_can_be_retried(self, send):
        with self.assertRaises(worker.TelegramRejected):
            self.publish()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM stories").fetchone()[0], 0
        )
        send.side_effect = None
        send.return_value = 42
        self.publish()
        self.assertEqual(send.call_count, 2)

    @patch("worker.send_story", return_value=42)
    def test_failed_download_does_not_claim_story(self, send):
        self.get_mock.return_value.__enter__.return_value.raise_for_status.side_effect = requests.HTTPError
        with self.assertRaises(requests.HTTPError):
            self.publish()
        send.assert_not_called()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM stories").fetchone()[0], 0
        )

    @patch("worker.send_story", return_value=42)
    def test_video_and_separate_channel(self, send):
        self.item.is_video = True
        self.item.video_url = "https://example.org/story.mp4"
        self.publish()
        self.publish("@other")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[2:], (self.media / "123.mp4", True))
        self.get_mock.assert_called_with(
            self.item.video_url, stream=True, timeout=(10, 60)
        )

    @patch("worker.requests.post")
    def test_sends_local_file_to_requested_api(self, post):
        post.return_value.json.return_value = {"ok": True, "result": {"message_id": 42}}
        path = self.media / "123.mp4"
        self.assertEqual(worker.send_story("secret", "@channel", path, True), 42)
        post.assert_called_once_with(
            "http://telegram-bot-api:8081/botsecret/sendVideo",
            data={"chat_id": "@channel", "video": path.as_uri()},
            timeout=(10, 300),
        )

    @patch("worker.requests.post")
    def test_telegram_error_classification(self, post):
        for code, error in [
            (400, worker.TelegramRejected),
            (429, worker.TelegramRejected),
            (500, RuntimeError),
        ]:
            with self.subTest(code=code):
                post.return_value.json.return_value = {"ok": False, "error_code": code}
                with self.assertRaises(error):
                    worker.send_story(
                        "secret", "@channel", self.media / "123.jpg", False
                    )

    @patch("worker.publish")
    @patch("worker.fetch_stories")
    def test_poll_posts_oldest_first_and_continues_after_failure(self, fetch, publish):
        older = SimpleNamespace(mediaid=1, date_utc=datetime(2026, 1, 1))
        newer = SimpleNamespace(mediaid=2, date_utc=datetime(2026, 1, 2))
        fetch.return_value = [newer, older]
        publish.side_effect = [requests.Timeout, None]
        self.assertFalse(worker.poll(self.db, "source", "@channel", "secret"))
        self.assertEqual(
            [call.args[2].mediaid for call in publish.call_args_list], [1, 2]
        )


class FetchStoriesTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("worker.requests.Session")
        self.session = patcher.start().return_value.__enter__.return_value
        self.addCleanup(patcher.stop)
        clock = patch("worker.time.time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)
        self.profile = {"id": 456, "username": "source", "isPrivate": False}
        self.page()
        self.record = {
            "id": "123_456",
            "type": "video",
            "taken_at": 900,
            "expiring_at": 2000,
            "url": base64.b64encode(
                b"https://scontent.cdninstagram.com/story.mp4"
            ).decode(),
        }
        self.session.post.return_value.json.return_value = {
            "lastStories": [self.record]
        }

    def page(self, needs_profile=False):
        self.session.get.return_value.text = (
            '<meta name="csrf-token" content="anonymous-csrf">'
            '<profile-page user-found-prop="1" need-to-parse-user-info-prop="'
            + ("1" if needs_profile else "")
            + '" v-bind:user-info-prop="'
            + html.escape(json.dumps(self.profile), quote=True)
            + '"></profile-page>'
        )
        self.session.get.return_value.url = "https://storynavigation.com/user/source"

    def test_fetch_uses_anonymous_session_and_preserves_instagram_id(self):
        (item,) = worker.fetch_stories("source")
        self.assertEqual(
            (item.mediaid, item.is_video, item.date_utc), ("123", True, 900)
        )
        self.assertEqual(item.video_url, "https://scontent.cdninstagram.com/story.mp4")
        self.session.headers.update.assert_called_once()
        self.session.post.assert_called_once_with(
            "https://storynavigation.com/get-user-last-stories",
            json={"userName": "source", "isPrivate": False, "instagramUserId": 456},
            timeout=(10, 60),
        )

    def test_photo_and_expired_stories(self):
        self.record["type"] = "image"
        self.assertFalse(worker.fetch_stories("source")[0].is_video)
        self.record["expiring_at"] = 999
        self.assertEqual(worker.fetch_stories("source"), [])

    def test_empty_list_is_valid_but_error_response_is_not(self):
        self.session.post.return_value.json.return_value = {"lastStories": []}
        self.assertEqual(worker.fetch_stories("source"), [])
        self.session.post.return_value.json.return_value = {
            "error": "temporarily unavailable"
        }
        with self.assertRaises(KeyError):
            worker.fetch_stories("source")

    def test_uncached_profile_is_resolved_before_loading_stories(self):
        self.page(needs_profile=True)
        profile_response = MagicMock()
        profile_response.json.return_value = {
            "found": True,
            "accountInfo": self.profile,
        }
        stories_response = MagicMock()
        stories_response.json.return_value = {"lastStories": []}
        self.session.post.side_effect = [profile_response, stories_response]
        self.assertEqual(worker.fetch_stories("source"), [])
        self.assertEqual(
            self.session.post.call_args_list[0].args[0],
            "https://storynavigation.com/get-user-profile",
        )

    def test_private_profile_and_changed_page_are_rejected(self):
        self.profile["isPrivate"] = True
        self.page()
        with self.assertRaises(ValueError):
            worker.fetch_stories("source")
        self.session.post.assert_not_called()
        self.session.get.return_value.text = "<html>challenge</html>"
        with self.assertRaises(ValueError):
            worker.fetch_stories("source")

    def test_invalid_id_type_and_media_host_are_rejected(self):
        for field, value in [
            ("id", "../other"),
            ("type", "unknown"),
            ("url", base64.b64encode(b"http://127.0.0.1/private").decode()),
            ("url", "not base64"),
        ]:
            with self.subTest(field=field, value=value):
                original = self.record[field]
                self.record[field] = value
                with self.assertRaises(ValueError):
                    worker.fetch_stories("source")
                self.record[field] = original


if __name__ == "__main__":
    unittest.main()
