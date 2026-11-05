import time
import unittest

from bridge.auth import AuthenticationError, BridgeAuthenticator


class BridgeAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.auth = BridgeAuthenticator("s" * 32, "windows-dsh", 60)
        self.now = int(time.time())
        self.body = b'{"capabilities":[]}'
        self.path = "/v1/heartbeat"

    def headers(self, nonce="abcdefghijklmnop"):
        timestamp = str(self.now)
        signature = self.auth.sign("POST", self.path, timestamp, nonce, self.body)
        return {
            "X-Feiyu-Client-Id": "windows-dsh",
            "X-Feiyu-Timestamp": timestamp,
            "X-Feiyu-Nonce": nonce,
            "X-Feiyu-Signature": signature,
        }

    def test_valid_signature(self):
        self.assertEqual(
            self.auth.verify(
                "POST", self.path, self.headers(), self.body, now=self.now
            ),
            "windows-dsh",
        )

    def test_replay_is_rejected(self):
        headers = self.headers()
        self.auth.verify("POST", self.path, headers, self.body, now=self.now)
        with self.assertRaises(AuthenticationError) as raised:
            self.auth.verify("POST", self.path, headers, self.body, now=self.now)
        self.assertEqual(raised.exception.code, "AUTH_REPLAY")

    def test_body_tampering_is_rejected(self):
        with self.assertRaises(AuthenticationError) as raised:
            self.auth.verify(
                "POST", self.path, self.headers(), b"{}", now=self.now
            )
        self.assertEqual(raised.exception.code, "AUTH_INVALID_SIGNATURE")

    def test_expired_timestamp_is_rejected(self):
        with self.assertRaises(AuthenticationError) as raised:
            self.auth.verify(
                "POST", self.path, self.headers(), self.body, now=self.now + 61
            )
        self.assertEqual(raised.exception.code, "AUTH_TIMESTAMP_EXPIRED")


if __name__ == "__main__":
    unittest.main()

