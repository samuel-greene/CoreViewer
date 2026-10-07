import os
import unittest
from unittest.mock import patch

from fastapi import Response

import main


class AuthTests(unittest.TestCase):
    def test_guest_login_sets_valid_cookie_for_protected_endpoints(self) -> None:
        response = Response()

        result = main.guest_login(response)

        self.assertEqual(result, {"email": "Guest", "role": "guest",
                                  "can_process": main.can_process({"role": "guest"})})
        self.assertIn(main.SESSION_COOKIE_NAME, response.headers["set-cookie"])
        token = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
        user = main.get_current_user(token)
        self.assertEqual(user["email"], "Guest")
        self.assertEqual(user["role"], "guest")

    def test_mixed_case_configured_email_can_log_in_case_insensitively(self) -> None:
        with patch.dict(
            os.environ,
            {
                "USERS_JSON": "",
                "ADMIN_EMAIL": "Admin@Example.COM",
                "ADMIN_PASSWORD": "demo-password",
            },
            clear=True,
        ):
            users = main._load_users_from_env()

        self.assertIn("admin@example.com", users)
        with patch.dict(main.USERS_DB, users, clear=True):
            response = Response()
            result = main.login(
                main.LoginRequest(email=" ADMIN@example.com ", password="demo-password"),
                response,
            )

        self.assertEqual(result["email"], "Admin@Example.COM")
        self.assertIn(main.SESSION_COOKIE_NAME, response.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
