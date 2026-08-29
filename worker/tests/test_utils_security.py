import json
import unittest

from utils.xianyu_utils import decrypt


class UtilitySecurityTests(unittest.TestCase):
    def test_decrypt_errors_do_not_echo_input(self):
        payload = "not-valid-opaque-payload"
        result = decrypt(payload)
        self.assertNotIn(payload, result)
        decoded = json.loads(result)
        self.assertIn(decoded.get("error"), {
            "base64_decode_failed",
            "messagepack_decode_failed",
            "decrypt_failed",
        })


if __name__ == "__main__":
    unittest.main()
