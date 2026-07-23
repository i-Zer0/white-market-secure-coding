import http.cookiejar
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import app


class WhiteMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.originals = (app.DB_PATH, app.BACKUP_DIR, app.CHAT_UPLOAD_DIR, app.PRODUCT_UPLOAD_DIR)
        self.original_env = {
            "WHITE_MARKET_ADMIN_PASSWORD": os.environ.get("WHITE_MARKET_ADMIN_PASSWORD"),
            "WHITE_MARKET_BACKUP_KEY": os.environ.get("WHITE_MARKET_BACKUP_KEY"),
        }
        os.environ["WHITE_MARKET_ADMIN_PASSWORD"] = "InitialAdmin!2468"
        os.environ["WHITE_MARKET_BACKUP_KEY"] = "TestBackupKey!2468-secure"
        app.DB_PATH = root / "test.db"
        app.BACKUP_DIR = root / "backups"
        app.CHAT_UPLOAD_DIR = root / "chat_uploads"
        app.PRODUCT_UPLOAD_DIR = root / "product_uploads"
        app.reset_rate_limits()
        app.init_db()
        self.member_id = self.create_user("member1", "Member!12345", "01020000001")
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        app.DB_PATH, app.BACKUP_DIR, app.CHAT_UPLOAD_DIR, app.PRODUCT_UPLOAD_DIR = self.originals
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        app.reset_rate_limits()
        self.temp_dir.cleanup()

    def create_user(self, username, password, phone):
        salt, digest = app.hash_password(password)
        with app.db() as conn:
            return conn.execute(
                """
                INSERT INTO users(username, password_salt, password_hash, display_name, phone, location, created_at)
                VALUES (?, ?, ?, ?, ?, '마포구', ?)
                """,
                (username, salt, digest, username, phone, app.now()),
            ).lastrowid

    def opener(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(self, opener, path, data=None):
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self.base_url + path, data=body)
        try:
            response = opener.open(request, timeout=5)
            return response.status, response.read().decode("utf-8"), response.headers
        except urllib.error.HTTPError as exc:
            result = (exc.code, exc.read().decode("utf-8"), exc.headers)
            exc.close()
            return result

    def login_member(self, opener=None, password="Member!12345", totp_code=""):
        opener = opener or self.opener()
        status, body, headers = self.request(
            opener,
            "/login",
            {"username": "member1", "password": password, "totp_code": totp_code},
        )
        return opener, status, body, headers

    def csrf_from(self, body):
        match = re.search(r'name="csrf" value="([^"]+)"', body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_totp_accepts_current_code(self):
        secret = app.generate_totp_secret()
        self.assertTrue(app.valid_totp(secret, app.totp_at(secret, time.time())))
        self.assertFalse(app.valid_totp(secret, "00000"))

    def test_recovery_code_cannot_be_reused(self):
        with app.db() as conn:
            secret = app.generate_totp_secret()
            conn.execute(
                "UPDATE users SET two_factor_enabled = 1, two_factor_secret = ? WHERE id = ?",
                (secret, self.member_id),
            )
            code = app.issue_recovery_codes(conn, self.member_id)[0]
        _, _, first_body, _ = self.login_member(self.opener(), totp_code=code)
        _, _, second_body, _ = self.login_member(self.opener(), totp_code=code)
        self.assertIn("White Market 중고거래", first_body)
        self.assertIn("2단계 인증번호 또는 복구 코드가 올바르지 않습니다.", second_body)

    def test_csrf_rejects_authenticated_post_without_token(self):
        opener, _, _, _ = self.login_member()
        status, body, _ = self.request(opener, "/block", {"id": "5"})
        self.assertEqual(403, status)
        self.assertIn("요청 보안 토큰", body)

    def test_non_admin_cannot_access_admin_page(self):
        opener, _, _, _ = self.login_member()
        status, body, _ = self.request(opener, "/admin")
        self.assertEqual(403, status)
        self.assertIn("관리자 권한", body)

    def test_user_cannot_read_another_users_transaction_evidence(self):
        with app.db() as conn:
            product = conn.execute("SELECT * FROM products WHERE seller_id != ? LIMIT 1", (self.member_id,)).fetchone()
            buyer = conn.execute("SELECT id FROM users WHERE id NOT IN (?, ?) AND is_admin = 0 LIMIT 1", (self.member_id, product["seller_id"])).fetchone()
            tx_id = conn.execute(
                """
                INSERT INTO transactions(product_id, seller_id, buyer_id, status, created_at, updated_at)
                VALUES (?, ?, ?, '거래요청', ?, ?)
                """,
                (product["id"], product["seller_id"], buyer["id"], app.now(), app.now()),
            ).lastrowid
        opener, _, _, _ = self.login_member()
        status, _, _ = self.request(opener, f"/transaction/evidence?id={tx_id}")
        self.assertEqual(404, status)

    def test_sql_injection_login_does_not_authenticate(self):
        status, body, _ = self.request(
            self.opener(),
            "/login",
            {"username": "' OR 1=1 --", "password": "anything", "totp_code": ""},
        )
        self.assertEqual(200, status)
        self.assertIn("아이디 또는 비밀번호가 올바르지", body)
        self.assertNotIn("로그아웃", body)

    def test_user_cannot_read_other_users_chat(self):
        with app.db() as conn:
            others = conn.execute(
                "SELECT id FROM users WHERE id != ? AND is_admin = 0 ORDER BY id LIMIT 2",
                (self.member_id,),
            ).fetchall()
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, body, created_at) VALUES (?, ?, ?, ?)",
                (others[0]["id"], others[1]["id"], "다른 사용자끼리의 비밀 대화", app.now()),
            )
        opener, _, _, _ = self.login_member()
        status, body, _ = self.request(opener, f"/chat?user={others[0]['id']}")
        self.assertEqual(200, status)
        self.assertNotIn("다른 사용자끼리의 비밀 대화", body)

    def test_xss_payload_is_escaped(self):
        with app.db() as conn:
            conn.execute(
                """
                INSERT INTO products(seller_id, title, description, category, price, created_at, updated_at)
                VALUES (?, ?, '안전한 설명입니다.', '기타', 1000, ?, ?)
                """,
                (self.member_id, "<script>alert(1)</script>", app.now(), app.now()),
            )
        status, body, _ = self.request(self.opener(), "/products?q=alert")
        self.assertEqual(200, status)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_five_failed_logins_lock_existing_account(self):
        for _ in range(5):
            self.login_member(self.opener(), password="Wrong!12345")
        with app.db() as conn:
            user = conn.execute(
                "SELECT failed_login_count, password_reset_required FROM users WHERE id = ?",
                (self.member_id,),
            ).fetchone()
        self.assertEqual(5, user["failed_login_count"])
        self.assertEqual(1, user["password_reset_required"])
        _, _, body, _ = self.login_member(self.opener())
        self.assertIn("계정이 잠겼습니다.", body)

    def test_rate_limit_rejects_excess_requests(self):
        app.enforce_rate_limit("test", ("ip:127.0.0.1", "user:1"), 2, 60)
        app.enforce_rate_limit("test", ("ip:127.0.0.1", "user:1"), 2, 60)
        with self.assertRaises(app.RateLimitExceeded):
            app.enforce_rate_limit("test", ("ip:127.0.0.1", "user:1"), 2, 60)

    def test_security_headers_are_present(self):
        status, _, headers = self.request(self.opener(), "/login")
        self.assertEqual(200, status)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])

    def test_encrypted_backup_round_trip_and_tamper_rejection(self):
        target = app.create_backup("manual")
        self.assertEqual(".wmbak", target.suffix)
        self.assertTrue(target.read_bytes().startswith(app.BACKUP_MAGIC))
        decrypted = app.decrypt_backup(target)
        try:
            connection = app.sqlite3.connect(decrypted)
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            connection.close()
        finally:
            decrypted.unlink(missing_ok=True)
        payload = bytearray(target.read_bytes())
        payload[-1] ^= 1
        target.write_bytes(payload)
        with self.assertRaises(ValueError):
            app.decrypt_backup(target)

    def test_initial_admin_requires_password_change(self):
        with app.db() as conn:
            admin = conn.execute("SELECT must_change_password FROM users WHERE is_admin = 1").fetchone()
        self.assertEqual(1, admin["must_change_password"])


if __name__ == "__main__":
    unittest.main()
