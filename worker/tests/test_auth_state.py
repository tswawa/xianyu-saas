import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from auth_state import AuthStateStore
from private_auth_storage import PrivateAuthStorage
from utils.xianyu_utils import generate_device_id


class AuthStateStoreTests(unittest.TestCase):
    def test_legacy_needs_human_status_is_migrated_without_platform_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_status.json"
            path.write_text(
                json.dumps(
                    {
                        "code": "risk_control",
                        "reauthorization_required": True,
                        "updated_at": 123,
                    }
                ),
                encoding="utf-8",
            )

            state = AuthStateStore(str(path)).read()

            self.assertEqual(state["version"], 2)
            self.assertEqual(state["phase"], "NEEDS_HUMAN")
            self.assertEqual(state["session"]["state"], "SECURITY_CHECK")
            self.assertEqual(state["code"], "risk_control")
            self.assertTrue(state["needs_human"])

    def test_flat_v2_clear_state_from_control_plane_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_status.json"
            payload = {
                "version": 2,
                "phase": "SESSION_VALID",
                "code": "ok",
                "failure_class": "NONE",
                "needs_human": False,
                "reauthorization_required": False,
                "updated_at": 123.0,
                "next_retry_at": 0.0,
                "failure_count": 0,
                "session": {"state": "VALID", "updated_at": 123.0},
                "mtop_token": {"state": "ABSENT", "updated_at": 123.0},
                "websocket": {"state": "DISCONNECTED", "updated_at": 123.0},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(AuthStateStore(str(path)).read(), payload)

    def test_early_nested_v2_state_is_migrated_to_flat_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_status.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "phase": "DEGRADED",
                        "session": {"state": "VALID", "updated_at": 1},
                        "mtop_token": {"state": "DEGRADED", "updated_at": 1},
                        "websocket": {"state": "REGISTERED", "updated_at": 1},
                        "failure": {
                            "code": "network_error",
                            "class": "TRANSIENT",
                            "count": 2,
                            "next_retry_at": 10,
                        },
                        "needs_human": False,
                        "updated_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            state = AuthStateStore(str(path)).read()
            self.assertNotIn("failure", state)
            self.assertEqual(state["code"], "network_error")
            self.assertEqual(state["failure_class"], "TRANSIENT")
            self.assertEqual(state["failure_count"], 2)
            self.assertEqual(state["next_retry_at"], 10)

    def test_v2_status_is_atomic_private_and_contains_no_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_status.json"
            store = AuthStateStore(str(path))
            state = store.update(
                phase="DEGRADED",
                session="VALID",
                mtop_token="DEGRADED",
                websocket="REGISTERED",
                failure_code="network_error",
                failure_class="TRANSIENT",
                failure_count=2,
                next_retry_at=456,
                needs_human=False,
                now=123,
            )

            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            self.assertEqual(payload, state)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                set(payload),
                {
                    "version",
                    "phase",
                    "session",
                    "mtop_token",
                    "websocket",
                    "code",
                    "failure_class",
                    "needs_human",
                    "reauthorization_required",
                    "updated_at",
                    "next_retry_at",
                    "failure_count",
                },
            )
            for forbidden in (
                "Cookie",
                "accessToken",
                "account",
                "message",
                "order",
                "secret-value",
            ):
                self.assertNotIn(forbidden, raw)
            self.assertEqual(list(Path(directory).glob(".auth_status.*.tmp")), [])


class PrivateAuthStorageTests(unittest.TestCase):
    def test_short_cookie_file_is_whitelisted_private_and_long_cookie_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            long_path = root / "cookies.txt"
            original = b"unb=seller; cookie2=long-lived; _m_h5_tk=old_1"
            long_path.write_bytes(original)
            os.chmod(long_path, 0o600)
            storage = PrivateAuthStorage(directory)

            storage.persist_short_cookies(
                {
                    "_m_h5_tk": "new_2",
                    "_m_h5_tk_enc": "encoded",
                    "unb": "must-not-be-written",
                    "cookie2": "must-not-be-written",
                }
            )

            short_path = root / "mtop_cookies.json"
            self.assertEqual(
                json.loads(short_path.read_text(encoding="utf-8")),
                {"_m_h5_tk": "new_2", "_m_h5_tk_enc": "encoded"},
            )
            self.assertEqual(stat.S_IMODE(short_path.stat().st_mode), 0o600)
            self.assertEqual(long_path.read_bytes(), original)
            merged = storage.merged_cookie_header("")
            self.assertIn("unb=seller", merged)
            self.assertIn("cookie2=long-lived", merged)
            self.assertIn("_m_h5_tk=new_2", merged)
            self.assertNotIn("old_1", merged)
            self.assertNotIn("must-not-be-written", short_path.read_text(encoding="utf-8"))
            self.assertEqual(list(root.glob(".mtop_cookies.*.tmp")), [])

    def test_short_cookie_reader_rejects_non_whitelisted_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mtop_cookies.json"
            path.write_text(json.dumps({"unb": "forbidden"}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                PrivateAuthStorage(directory).load_short_cookies()

    def test_device_id_is_stable_for_the_same_account(self):
        self.assertEqual(generate_device_id("seller"), generate_device_id("seller"))
        self.assertNotEqual(generate_device_id("seller"), generate_device_id("other"))


if __name__ == "__main__":
    unittest.main()
