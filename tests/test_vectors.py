import json
import sys
import unittest
from pathlib import Path

from bridge.auth import AuthenticationError, BridgeAuthenticator

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from mock_windows_client import bridge_hmac_signature


VECTOR_PATH = Path(__file__).parent / "vectors" / "hmac_v1.json"


class HmacVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vector = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))

    def new_auth(self):
        return BridgeAuthenticator(
            self.vector["secret"], self.vector["client_id"], 60
        )

    def headers(self):
        return {
            "X-Feiyu-Client-Id": self.vector["client_id"],
            "X-Feiyu-Timestamp": self.vector["timestamp"],
            "X-Feiyu-Nonce": self.vector["nonce"],
            "X-Feiyu-Signature": self.vector["signature_hex"],
        }

    def test_fixed_hash_and_signature(self):
        auth = self.new_auth()
        body = self.vector["body_utf8"].encode("utf-8")
        signature = auth.sign(
            self.vector["method"],
            self.vector["protocol_path"],
            self.vector["timestamp"],
            self.vector["nonce"],
            body,
        )
        self.assertEqual(signature, self.vector["signature_hex"])
        self.assertEqual(
            auth.canonical_message(
                self.vector["method"],
                self.vector["protocol_path"],
                self.vector["timestamp"],
                self.vector["nonce"],
                body,
            ).decode("utf-8"),
            self.vector["canonical_message_utf8"],
        )

    def test_independent_mock_client_signature(self):
        signature = bridge_hmac_signature(
            self.vector["secret"],
            self.vector["method"],
            self.vector["protocol_path"],
            self.vector["timestamp"],
            self.vector["nonce"],
            self.vector["body_utf8"].encode("utf-8"),
        )
        self.assertEqual(signature, self.vector["signature_hex"])

    def test_valid_vector(self):
        case = self.vector["cases"]["valid"]
        self.new_auth().verify(
            self.vector["method"],
            self.vector["protocol_path"],
            self.headers(),
            self.vector["body_utf8"].encode("utf-8"),
            now=case["verification_time"],
        )

    def test_tampered_body_vector(self):
        case = self.vector["cases"]["tampered_body"]
        with self.assertRaises(AuthenticationError) as raised:
            self.new_auth().verify(
                self.vector["method"],
                self.vector["protocol_path"],
                self.headers(),
                case["body_utf8"].encode("utf-8"),
                now=case["verification_time"],
            )
        self.assertEqual(raised.exception.code, case["expected_error"])

    def test_expired_timestamp_vector(self):
        case = self.vector["cases"]["expired_timestamp"]
        with self.assertRaises(AuthenticationError) as raised:
            self.new_auth().verify(
                self.vector["method"],
                self.vector["protocol_path"],
                self.headers(),
                self.vector["body_utf8"].encode("utf-8"),
                now=case["verification_time"],
            )
        self.assertEqual(raised.exception.code, case["expected_error"])

    def test_repeated_nonce_vector(self):
        case = self.vector["cases"]["repeated_nonce"]
        auth = self.new_auth()
        args = (
            self.vector["method"],
            self.vector["protocol_path"],
            self.headers(),
            self.vector["body_utf8"].encode("utf-8"),
        )
        auth.verify(*args, now=case["verification_time"])
        with self.assertRaises(AuthenticationError) as raised:
            auth.verify(*args, now=case["verification_time"])
        self.assertEqual(raised.exception.code, case["expected_second_error"])


if __name__ == "__main__":
    unittest.main()

