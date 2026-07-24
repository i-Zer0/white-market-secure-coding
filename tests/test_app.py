import http.cookiejar
import base64
import os
import re
import tempfile
import threading
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
        self.originals = (app.DB_PATH, app.BACKUP_DIR, app.CHAT_UPLOAD_DIR, app.PRODUCT_UPLOAD_DIR, app.PROFILE_DIR)
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
        app.PROFILE_DIR = root / "profiles"
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
        app.DB_PATH, app.BACKUP_DIR, app.CHAT_UPLOAD_DIR, app.PRODUCT_UPLOAD_DIR, app.PROFILE_DIR = self.originals
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
            user_id = conn.execute(
                """
                INSERT INTO users(username, password_salt, password_hash, display_name, phone, location, created_at)
                VALUES (?, ?, ?, ?, ?, '마포구', ?)
                """,
                (username, salt, digest, username, phone, app.now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO wallets(user_id, balance, updated_at) VALUES (?, ?, ?)",
                (user_id, app.INITIAL_DEMO_BALANCE, app.now()),
            )
            return user_id

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

    def login_member(self, opener=None, password="Member!12345"):
        opener = opener or self.opener()
        status, body, headers = self.request(
            opener,
            "/login",
            {"username": "member1", "password": password},
        )
        return opener, status, body, headers

    def csrf_from(self, body):
        match = re.search(r'name="csrf" value="([^"]+)"', body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_registration_rejects_mismatched_password_confirmation(self):
        status, body, _ = self.request(
            self.opener(),
            "/register",
            {
                "username": "newmember2",
                "display_name": "새회원",
                "phone": "010-2000-0099",
                "location": "마포구",
                "password": "Member!12345",
                "password_confirm": "Different!12345",
            },
        )
        self.assertEqual(200, status)
        self.assertIn("비밀번호가 일치하지 않습니다. 다시 입력해주세요.", body)
        with app.db() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM users WHERE username = 'newmember2'").fetchone())

    def test_login_uses_username_and_password_only_by_default(self):
        status, body, _ = self.request(self.opener(), "/login")
        self.assertEqual(200, status)
        self.assertIn("비밀번호 찾기", body)
        self.assertNotIn("totp_code", body)
        self.assertNotIn("2단계 인증 로그인", body)

    def test_normal_login_does_not_request_extra_verification(self):
        _, status, body, _ = self.login_member()
        self.assertEqual(200, status)
        self.assertIn("White Market 중고거래", body)
        self.assertNotIn("추가 본인인증", body)

    def test_home_has_product_registration_button(self):
        status, body, _ = self.request(self.opener(), "/")
        self.assertEqual(200, status)
        self.assertIn('<a class="primary" href="/product/new">상품 등록</a>', body)

    def test_account_settings_are_split_into_three_pages(self):
        opener, _, _, _ = self.login_member()
        for path, heading in [
            ("/settings", "알림 설정"),
            ("/security", "보안 설정"),
            ("/privacy", "개인정보 설정"),
        ]:
            status, body, _ = self.request(opener, path)
            self.assertEqual(200, status)
            self.assertIn(f"<h1>{heading}</h1>", body)
            self.assertIn('aria-label="계정 설정"', body)

    def test_profile_image_accepts_valid_png(self):
        opener, _, _, _ = self.login_member()
        _, page, _ = self.request(opener, "/mypage")
        csrf = self.csrf_from(page)
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        status, body, _ = self.request(
            opener,
            "/mypage",
            {
                "csrf": csrf,
                "display_name": "member1",
                "phone": "010-2000-0001",
                "location": "마포구",
                "bio": "",
                "profile_image_data": f"data:image/png;base64,{png}",
            },
        )
        self.assertEqual(200, status)
        self.assertIn("프로필 정보를 저장했습니다.", body)
        with app.db() as conn:
            image_url = conn.execute(
                "SELECT profile_image_url FROM users WHERE id = ?",
                (self.member_id,),
            ).fetchone()["profile_image_url"]
        self.assertTrue(image_url.endswith(".png"))
        self.assertTrue((app.PROFILE_DIR / image_url.rsplit("/", 1)[-1]).is_file())

    def test_authenticated_user_can_change_password(self):
        opener, _, _, _ = self.login_member()
        _, page, _ = self.request(opener, "/password-change")
        csrf = self.csrf_from(page)

        status, body, _ = self.request(
            opener,
            "/password-change",
            {
                "csrf": csrf,
                "current_password": "Member!12345",
                "password": "Changed!12345",
                "password_confirm": "Changed!12345",
            },
        )
        self.assertEqual(200, status)
        self.assertIn("비밀번호를 변경하고 다른 기기의 로그인을 종료했습니다.", body)

        _, _, old_body, _ = self.login_member(self.opener())
        self.assertIn("아이디 또는 비밀번호가 올바르지 않습니다.", old_body)
        _, new_status, new_body, _ = self.login_member(self.opener(), password="Changed!12345")
        self.assertEqual(200, new_status)
        self.assertIn("White Market 중고거래", new_body)

    def test_locked_account_reset_code_cannot_be_reused(self):
        opener = self.opener()
        for _ in range(4):
            self.login_member(opener, password="Wrong!12345")
        _, _, locked_body, _ = self.login_member(opener, password="Wrong!12345")
        self.assertIn("계정 잠금 해제", locked_body)
        _, challenge_body, _ = self.request(
            opener,
            "/password-reset/request",
            {"username": "member1", "phone": "010-2000-0001"},
        )
        challenge_id = re.search(r'name="challenge_id" value="([^"]+)"', challenge_body).group(1)
        code = re.search(r'<p class="dev-code">([0-9]{6})</p>', challenge_body).group(1)
        verify_status, verify_body, _ = self.request(
            opener,
            "/password-reset/confirm",
            {
                "challenge_id": challenge_id,
                "code": code,
                "password": "Reset!12345",
                "password_confirm": "Reset!12345",
            },
        )
        self.assertEqual(200, verify_status)
        self.assertIn("비밀번호 변경 완료", verify_body)

        reuse_status, _, _ = self.request(
            opener,
            "/password-reset/confirm",
            {
                "challenge_id": challenge_id,
                "code": code,
                "password": "Again!12345",
                "password_confirm": "Again!12345",
            },
        )
        self.assertEqual(400, reuse_status)

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
            {"username": "' OR 1=1 --", "password": "anything"},
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

    def test_five_failed_logins_require_phone_verification(self):
        opener = self.opener()
        for _ in range(4):
            self.login_member(opener, password="Wrong!12345")
        _, _, locked_body, _ = self.login_member(opener, password="Wrong!12345")
        self.assertIn("계정 잠금 해제", locked_body)
        self.assertIn("로그인 5회 실패로 계정이 잠겼습니다.", locked_body)
        self.assertIn("010-****0001", locked_body)
        self.assertIn("등록 휴대전화 번호", locked_body)
        with app.db() as conn:
            user = conn.execute(
                "SELECT failed_login_count, password_reset_required FROM users WHERE id = ?",
                (self.member_id,),
            ).fetchone()
        self.assertEqual(5, user["failed_login_count"])
        self.assertEqual(1, user["password_reset_required"])

        _, challenge_body, _ = self.request(
            opener,
            "/password-reset/request",
            {"username": "member1", "phone": "010-2000-0001"},
        )
        challenge_id = re.search(r'name="challenge_id" value="([^"]+)"', challenge_body).group(1)
        code = re.search(r'<p class="dev-code">([0-9]{6})</p>', challenge_body).group(1)
        _, changed_body, _ = self.request(
            opener,
            "/password-reset/confirm",
            {
                "challenge_id": challenge_id,
                "code": code,
                "password": "Unlocked!12345",
                "password_confirm": "Unlocked!12345",
            },
        )
        self.assertIn("비밀번호 변경 완료", changed_body)
        with app.db() as conn:
            unlocked = conn.execute(
                "SELECT failed_login_count, password_reset_required FROM users WHERE id = ?",
                (self.member_id,),
            ).fetchone()
        self.assertEqual(0, unlocked["failed_login_count"])
        self.assertEqual(0, unlocked["password_reset_required"])
        _, login_status, login_body, _ = self.login_member(self.opener(), password="Unlocked!12345")
        self.assertEqual(200, login_status)
        self.assertIn("White Market 중고거래", login_body)

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

    def test_wallet_transfer_is_atomic_and_single_use(self):
        with app.db() as conn:
            product = conn.execute(
                "SELECT id, seller_id FROM products WHERE seller_id != ? AND is_deleted = 0 LIMIT 1",
                (self.member_id,),
            ).fetchone()
            amount = 125_000
            tx_id = conn.execute(
                """
                INSERT INTO transactions(product_id, seller_id, buyer_id, status, agreed_price, created_at, updated_at)
                VALUES (?, ?, ?, '예약중', ?, ?, ?)
                """,
                (product["id"], product["seller_id"], self.member_id, amount, app.now(), app.now()),
            ).lastrowid
            buyer_before = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (self.member_id,)).fetchone()["balance"]
            seller_before = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (product["seller_id"],)).fetchone()["balance"]

        opener, _, _, _ = self.login_member()
        _, page, _ = self.request(opener, f"/chat?user={product['seller_id']}&product={product['id']}")
        self.assertIn("거래 대금 송금", page)
        self.assertIn("보유 포인트", page)
        self.assertIn(f"WM 포인트 {amount:,}원 송금", page)
        csrf = self.csrf_from(page)
        status, body, _ = self.request(
            opener,
            "/transaction/payment",
            {"csrf": csrf, "id": str(tx_id), "return_to_chat": "1"},
        )
        self.assertEqual(200, status)
        self.assertIn("송금 완료", body)
        self.assertIn("참조번호", body)

        with app.db() as conn:
            transfer = conn.execute("SELECT * FROM wallet_transfers WHERE transaction_id = ?", (tx_id,)).fetchone()
            buyer_after = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (self.member_id,)).fetchone()["balance"]
            seller_after = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (product["seller_id"],)).fetchone()["balance"]
        self.assertEqual(amount, transfer["amount"])
        self.assertEqual(buyer_before - amount, buyer_after)
        self.assertEqual(seller_before + amount, seller_after)

        duplicate_status, _, _ = self.request(opener, "/transaction/payment", {"csrf": csrf, "id": str(tx_id)})
        self.assertEqual(400, duplicate_status)
        with app.db() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM wallet_transfers WHERE transaction_id = ?", (tx_id,)).fetchone()[0])
            self.assertEqual(buyer_after, conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (self.member_id,)).fetchone()["balance"])
            self.assertEqual(seller_after, conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (product["seller_id"],)).fetchone()["balance"])

    def test_wallet_transfer_rejects_non_buyer_and_insufficient_balance(self):
        actual_buyer_id = self.create_user("buyer22", "Buyer!12345", "01020000002")
        with app.db() as conn:
            product = conn.execute(
                "SELECT id, seller_id FROM products WHERE seller_id NOT IN (?, ?) AND is_deleted = 0 LIMIT 1",
                (self.member_id, actual_buyer_id),
            ).fetchone()
            unauthorized_tx = conn.execute(
                """
                INSERT INTO transactions(product_id, seller_id, buyer_id, status, agreed_price, created_at, updated_at)
                VALUES (?, ?, ?, '예약중', 50000, ?, ?)
                """,
                (product["id"], product["seller_id"], actual_buyer_id, app.now(), app.now()),
            ).lastrowid

        opener, _, _, _ = self.login_member()
        _, page, _ = self.request(opener, "/transactions")
        csrf = self.csrf_from(page)
        unauthorized_status, _, _ = self.request(
            opener,
            "/transaction/payment",
            {"csrf": csrf, "id": str(unauthorized_tx)},
        )
        self.assertEqual(400, unauthorized_status)

        with app.db() as conn:
            own_product = conn.execute(
                "SELECT id, seller_id FROM products WHERE seller_id != ? AND is_deleted = 0 AND id != ? LIMIT 1",
                (self.member_id, product["id"]),
            ).fetchone()
            insufficient_tx = conn.execute(
                """
                INSERT INTO transactions(product_id, seller_id, buyer_id, status, agreed_price, created_at, updated_at)
                VALUES (?, ?, ?, '예약중', 100000, ?, ?)
                """,
                (own_product["id"], own_product["seller_id"], self.member_id, app.now(), app.now()),
            ).lastrowid
            conn.execute("UPDATE wallets SET balance = 1000 WHERE user_id = ?", (self.member_id,))
            seller_before = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (own_product["seller_id"],)).fetchone()["balance"]

        insufficient_status, _, _ = self.request(
            opener,
            "/transaction/payment",
            {"csrf": csrf, "id": str(insufficient_tx)},
        )
        self.assertEqual(400, insufficient_status)
        with app.db() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM wallet_transfers WHERE transaction_id = ?", (insufficient_tx,)).fetchone()[0])
            self.assertEqual(1000, conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (self.member_id,)).fetchone()["balance"])
            self.assertEqual(seller_before, conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (own_product["seller_id"],)).fetchone()["balance"])

    def test_seller_controls_chat_trade_status(self):
        seller_id = self.create_user("seller22", "Seller!12345", "01020000022")
        with app.db() as conn:
            product_id = conn.execute(
                """
                INSERT INTO products(
                    seller_id, title, description, category, price, distance_km,
                    status, created_at, updated_at
                ) VALUES (?, '채팅 거래 테스트 상품', '판매자 상태 전이 테스트 상품입니다.', '기타', 45000, 1.0, '판매중', ?, ?)
                """,
                (seller_id, app.now(), app.now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.member_id, seller_id, product_id, "이 상품을 구매하고 싶습니다.", app.now()),
            )

        buyer_opener, _, _, _ = self.login_member()
        _, chat_page, _ = self.request(buyer_opener, f"/chat?user={seller_id}&product={product_id}")
        buyer_csrf = self.csrf_from(chat_page)
        denied_status, _, _ = self.request(
            buyer_opener,
            "/chat/reserve",
            {"csrf": buyer_csrf, "product_id": str(product_id), "buyer_id": str(self.member_id)},
        )
        self.assertEqual(400, denied_status)

        seller_opener = self.opener()
        self.request(
            seller_opener,
            "/login",
            {"username": "seller22", "password": "Seller!12345"},
        )
        _, seller_chat_page, _ = self.request(seller_opener, f"/chat?user={self.member_id}&product={product_id}")
        self.assertIn("예약중으로 변경", seller_chat_page)
        self.assertNotIn("거래 약속", seller_chat_page)
        seller_csrf = self.csrf_from(seller_chat_page)
        reserve_status, reserve_body, _ = self.request(
            seller_opener,
            "/chat/reserve",
            {
                "csrf": seller_csrf,
                "product_id": str(product_id),
                "buyer_id": str(self.member_id),
            },
        )
        self.assertEqual(200, reserve_status)
        self.assertIn("거래 예약중", reserve_body)
        self.assertIn("판매중으로 변경", reserve_body)

        with app.db() as conn:
            first_tx = conn.execute(
                "SELECT * FROM transactions WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                (product_id,),
            ).fetchone()
            self.assertEqual("예약중", first_tx["status"])
            self.assertEqual(
                "예약중",
                conn.execute("SELECT status FROM products WHERE id = ?", (product_id,)).fetchone()["status"],
            )

        buyer_complete_status, _, _ = self.request(
            buyer_opener,
            "/transaction/status",
            {"csrf": buyer_csrf, "id": str(first_tx["id"]), "action": "complete"},
        )
        self.assertEqual(400, buyer_complete_status)

        reopen_status, reopen_body, _ = self.request(
            seller_opener,
            "/transaction/status",
            {
                "csrf": self.csrf_from(reserve_body),
                "id": str(first_tx["id"]),
                "action": "reopen",
                "return_to_chat": "1",
            },
        )
        self.assertEqual(200, reopen_status)
        self.assertIn("예약중으로 변경", reopen_body)
        with app.db() as conn:
            self.assertEqual(
                "예약취소",
                conn.execute("SELECT status FROM transactions WHERE id = ?", (first_tx["id"],)).fetchone()["status"],
            )
            self.assertEqual(
                "판매중",
                conn.execute("SELECT status FROM products WHERE id = ?", (product_id,)).fetchone()["status"],
            )

        _, second_reserve_body, _ = self.request(
            seller_opener,
            "/chat/reserve",
            {
                "csrf": self.csrf_from(reopen_body),
                "product_id": str(product_id),
                "buyer_id": str(self.member_id),
            },
        )
        with app.db() as conn:
            second_tx = conn.execute(
                "SELECT * FROM transactions WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                (product_id,),
            ).fetchone()
        premature_status, _, _ = self.request(
            seller_opener,
            "/transaction/status",
            {
                "csrf": self.csrf_from(second_reserve_body),
                "id": str(second_tx["id"]),
                "action": "complete",
                "return_to_chat": "1",
            },
        )
        self.assertEqual(400, premature_status)

        _, buyer_payment_page, _ = self.request(
            buyer_opener,
            f"/chat?user={seller_id}&product={product_id}",
        )
        self.assertIn("거래 대금 송금", buyer_payment_page)
        payment_status, payment_body, _ = self.request(
            buyer_opener,
            "/transaction/payment",
            {
                "csrf": self.csrf_from(buyer_payment_page),
                "id": str(second_tx["id"]),
                "return_to_chat": "1",
            },
        )
        self.assertEqual(200, payment_status)
        self.assertIn("송금 완료", payment_body)

        complete_status, complete_body, _ = self.request(
            seller_opener,
            "/transaction/status",
            {
                "csrf": self.csrf_from(second_reserve_body),
                "id": str(second_tx["id"]),
                "action": "complete",
                "return_to_chat": "1",
            },
        )
        self.assertEqual(200, complete_status)
        self.assertIn("거래 완료", complete_body)
        with app.db() as conn:
            self.assertEqual(
                "거래완료",
                conn.execute("SELECT status FROM transactions WHERE id = ?", (second_tx["id"],)).fetchone()["status"],
            )
            self.assertEqual(
                "거래완료",
                conn.execute("SELECT status FROM products WHERE id = ?", (product_id,)).fetchone()["status"],
            )


if __name__ == "__main__":
    unittest.main()
