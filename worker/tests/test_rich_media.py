import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from context_manager import ChatContextManager
from main import XianyuLive


class RichMediaChatTextTests(unittest.TestCase):
    def test_plain_text_unchanged(self):
        text, had_image = XianyuLive._extract_chat_text({"reminderContent": "这个怎么用"})
        self.assertEqual(text, "这个怎么用")
        self.assertFalse(had_image)

    def test_image_url_replaced_with_placeholder(self):
        text, had_image = XianyuLive._extract_chat_text(
            {"reminderContent": "看这个 https://example.com/a.jpg 能用吗"}
        )
        self.assertEqual(text, "看这个 [图片] 能用吗")
        self.assertTrue(had_image)

    def test_image_only_message_becomes_placeholder(self):
        text, had_image = XianyuLive._extract_chat_text(
            {"reminderContent": "https://example.com/a.png"}
        )
        self.assertEqual(text, "[图片]")
        self.assertTrue(had_image)

    def test_non_string_content_with_image_field_becomes_placeholder(self):
        text, had_image = XianyuLive._extract_chat_text(
            {"reminderContent": {"x": 1}, "imageUrl": "https://example.com/a.jpg"}
        )
        self.assertEqual(text, "[图片]")
        self.assertTrue(had_image)

    def test_empty_content_without_image_dropped(self):
        text, had_image = XianyuLive._extract_chat_text({"reminderContent": ""})
        self.assertIsNone(text)
        self.assertFalse(had_image)

    def test_non_string_content_without_image_dropped(self):
        text, had_image = XianyuLive._extract_chat_text({"reminderContent": {"x": 1}})
        self.assertIsNone(text)
        self.assertFalse(had_image)

    def test_plain_urls_without_image_extension_kept(self):
        text, had_image = XianyuLive._extract_chat_text(
            {"reminderContent": "看链接 https://example.com/p/123"}
        )
        self.assertEqual(text, "看链接 https://example.com/p/123")
        self.assertFalse(had_image)

    def test_image_field_detection(self):
        self.assertTrue(XianyuLive._details_have_image({"imgUrl": "x"}))
        self.assertTrue(XianyuLive._details_have_image({"photo_id": "x"}))
        self.assertTrue(XianyuLive._details_have_image({"k": "https://x.webp"}))
        self.assertFalse(XianyuLive._details_have_image({"k": "hello"}))

    def test_structured_media_preserves_type_summary_and_safe_url(self):
        normalized = XianyuLive._normalize_chat_content(
            {
                "attachments": [
                    {"type": "audio", "url": "http://unsafe.invalid/a.mp3", "duration": 1200},
                    {"type": "video", "url": "https://cdn.example/video.mp4"},
                    {"type": "file", "name": "说明.pdf"},
                    {"type": "sticker", "id": "sticker-1"},
                ]
            }
        )
        self.assertEqual(normalized["content_type"], "rich")
        self.assertEqual([item["type"] for item in normalized["media"]], ["audio", "video", "file", "emoji"])
        self.assertEqual(normalized["media"][0]["url"], "")
        self.assertEqual(normalized["media"][1]["url"], "https://cdn.example/video.mp4")
        self.assertEqual(normalized["media"][2]["label"], "说明.pdf")
        self.assertIn("[音频]", normalized["text"])
        self.assertIn("[视频]", normalized["text"])
        self.assertIn("[文件]", normalized["text"])
        self.assertIn("[表情]", normalized["text"])

    def test_invalid_image_url_is_retained_as_placeholder(self):
        normalized = XianyuLive._normalize_chat_content(
            {"imageUrl": "http://unsafe.invalid/image.jpg"}
        )
        self.assertEqual(normalized["content_type"], "image")
        self.assertEqual(normalized["media"], [{
            "type": "image",
            "url": "",
            "alt": "[图片]",
            "width": 0,
            "height": 0,
            "duration_ms": 0,
            "label": "[图片]",
        }])
        self.assertEqual(normalized["text"], "[图片]")

    def test_unknown_attachment_is_not_dropped(self):
        normalized = XianyuLive._normalize_chat_content(
            {"media": {"opaqueId": "attachment-1"}}
        )
        self.assertEqual(normalized["content_type"], "unknown")
        self.assertEqual(len(normalized["media"]), 1)
        self.assertEqual(normalized["media"][0]["type"], "unknown")
        self.assertIn("[富媒体]", normalized["text"])

    def test_unpreviewable_media_placeholder_survives_context_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ChatContextManager(db_path=str(Path(directory) / "chat.db"))
            self.assertTrue(
                manager.add_message_by_chat(
                    "chat", "buyer", "1001", "user", "[音频]",
                    source_id="source-audio", content_type="audio",
                    media=[{"type": "audio", "url": "", "label": "[音频]"}],
                )
            )
            with sqlite3.connect(manager.db_path) as connection:
                row = connection.execute(
                    "SELECT content_type, media_json FROM messages WHERE source_id = ?",
                    ("source-audio",),
                ).fetchone()
            self.assertEqual(row[0], "audio")
            self.assertEqual(json.loads(row[1])[0]["label"], "[音频]")

    def test_legacy_database_migrates_and_persists_media(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "chat_history.db")
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        timestamp DATETIME,
                        chat_id TEXT,
                        source_id TEXT
                    )"""
                )
                connection.commit()
            manager = ChatContextManager(db_path=db_path)
            self.assertTrue(
                manager.add_message_by_chat(
                    "chat", "buyer", "1001", "user", "[图片]",
                    source_id="source-image", content_type="image",
                    media=[{"type": "image", "url": "https://cdn.example/image.jpg"}],
                )
            )
            with sqlite3.connect(db_path) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
                row = connection.execute(
                    "SELECT content_type, media_json FROM messages WHERE source_id = ?",
                    ("source-image",),
                ).fetchone()
            self.assertIn("content_type", columns)
            self.assertIn("media_json", columns)
            self.assertEqual(row[0], "image")
            self.assertEqual(json.loads(row[1])[0]["url"], "https://cdn.example/image.jpg")


if __name__ == "__main__":
    unittest.main()
