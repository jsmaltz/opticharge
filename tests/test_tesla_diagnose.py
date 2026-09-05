import base64
import json
import unittest

from tesla_diagnose import jwt_claims


class JwtClaimsTests(unittest.TestCase):
    def test_decodes_claims_without_printing_token(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"account_type": "person", "scp": ["energy_device_data"]}).encode()
        ).rstrip(b"=").decode()

        self.assertEqual(
            jwt_claims(f"header.{payload}.signature"),
            {"account_type": "person", "scp": ["energy_device_data"]},
        )

    def test_invalid_token_returns_empty_claims(self):
        self.assertEqual(jwt_claims("not-a-jwt"), {})


if __name__ == "__main__":
    unittest.main()
