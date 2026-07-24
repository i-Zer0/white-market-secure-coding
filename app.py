import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
import tempfile
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WHITE_MARKET_DATA_DIR", BASE_DIR)).resolve()
DB_PATH = DATA_DIR / "market.db"
STATIC_DIR = BASE_DIR / "static"
PROFILE_DIR = STATIC_DIR / "profiles"
PRODUCT_UPLOAD_DIR = STATIC_DIR / "product_uploads"
CHAT_UPLOAD_DIR = STATIC_DIR / "chat_uploads"
BACKUP_DIR = DATA_DIR / "backups"
MAX_PROFILE_IMAGE_BYTES = 500_000
MAX_PRODUCT_IMAGE_BYTES = 900_000
MAX_CHAT_IMAGE_BYTES = 900_000
MAX_PRODUCT_IMAGES = 8
INITIAL_DEMO_BALANCE = 1_000_000
SESSION_SECONDS = 8 * 60 * 60
CAPTCHA_SECONDS = 5 * 60
PASSWORD_RESET_SECONDS = 5 * 60
APP_ENV = os.environ.get("WHITE_MARKET_ENV", "development").strip().lower()
APP_PRODUCTION = APP_ENV == "production"
APP_HTTPS = os.environ.get("WHITE_MARKET_HTTPS", "").strip() == "1"
BACKUP_MAGIC = b"WMBK1"
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS = {}
USERNAME_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{5,20}$")
PHONE_RE = re.compile(r"^01[016789][0-9]{7,8}$")
CATEGORIES = ["디지털기기", "생활가전", "가구/인테리어", "의류", "도서", "스포츠", "기타"]
PRODUCT_STATUS = ["판매중", "예약중", "거래완료"]
SAFETY_CHECKLIST = [
    ("identity", "상대방 프로필과 거래 이력을 확인했어요"),
    ("condition", "상품 상태와 구성품을 채팅으로 확인했어요"),
    ("place", "밝고 사람이 있는 거래 장소를 정했어요"),
    ("payment", "상품 확인 전 선입금을 요구받지 않았어요"),
]
GEOCODING_BASE_URL = os.environ.get("GEOCODING_BASE_URL", "https://nominatim.openstreetmap.org")
GEOCODING_USER_AGENT = os.environ.get("GEOCODING_USER_AGENT", "WhiteMarketStudentProject/1.0 (educational-local-app)")
SMS_WEBHOOK_URL = os.environ.get("SMS_WEBHOOK_URL", "").strip()
GEOCODE_LOCK = threading.Lock()
GEOCODE_CACHE = {}
LAST_GEOCODE_REQUEST_AT = 0.0
NOTIFICATION_PREFERENCES = {
    "chat": "notify_chat",
    "offer": "notify_chat",
    "appointment": "notify_chat",
    "price_drop": "notify_price",
    "transaction": "notify_transaction",
    "payment": "notify_transaction",
    "notice": "notify_notice",
    "security": "notify_security",
}


class RateLimitExceeded(Exception):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def rate_identity(value):
    return hashlib.sha256(str(value or "").strip().lower().encode("utf-8")).hexdigest()[:24]


def enforce_rate_limit(scope, identities, limit, window_seconds):
    current = time.monotonic()
    retry_after = 0
    with RATE_LIMIT_LOCK:
        for identity in identities:
            key = (scope, str(identity))
            timestamps = [stamp for stamp in RATE_LIMIT_BUCKETS.get(key, []) if current - stamp < window_seconds]
            RATE_LIMIT_BUCKETS[key] = timestamps
            if len(timestamps) >= limit:
                retry_after = max(retry_after, window_seconds - (current - timestamps[0]))
        if retry_after:
            raise RateLimitExceeded("요청이 너무 많습니다. 잠시 후 다시 시도해주세요.", retry_after)
        for identity in identities:
            RATE_LIMIT_BUCKETS[(scope, str(identity))].append(current)


def reset_rate_limits():
    with RATE_LIMIT_LOCK:
        RATE_LIMIT_BUCKETS.clear()


def backup_passphrase():
    passphrase = os.environ.get("WHITE_MARKET_BACKUP_KEY", "")
    if len(passphrase) < 16:
        raise ValueError("WHITE_MARKET_BACKUP_KEY를 16자 이상으로 설정해주세요.")
    return passphrase


def derive_backup_key(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 600_000, dklen=32)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def session_cookie(token, max_age):
    secure = "; Secure" if APP_HTTPS else ""
    return f"market_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}{secure}"


def relative_time(created_at, reference=None):
    try:
        created = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "등록일 정보 없음"

    elapsed = (reference or datetime.now()) - created
    total_seconds = max(0, int(elapsed.total_seconds()))
    if total_seconds < 60:
        return "방금 전"
    if total_seconds < 60 * 60:
        return f"{total_seconds // 60}분 전"
    if total_seconds < 24 * 60 * 60:
        return f"{total_seconds // (60 * 60)}시간 전"

    days = total_seconds // (24 * 60 * 60)
    if days < 30:
        return f"{days}일 전"
    months = days // 30
    if months < 12:
        return f"{months}달 전"
    return f"{months // 12}년 전"


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def won(value):
    return f"{int(value):,}원"


def profile_avatar(user, size=""):
    image_url = user["profile_image_url"] if "profile_image_url" in user.keys() else ""
    classes = "profile-avatar" + (f" {size}" if size else "")
    label = f'{user["display_name"]} 프로필 사진'
    if image_url:
        return f'<img class="{classes}" src="{esc(image_url)}" alt="{esc(label)}">'
    initial = (str(user["display_name"] or user["username"] or "?")[:1]).upper()
    return f'<span class="{classes} profile-avatar-fallback" role="img" aria-label="{esc(label)}">{esc(initial)}</span>'


def settings_navigation(active):
    links = [
        ("/settings", "notifications", "알림 설정"),
        ("/security", "security", "보안 설정"),
        ("/privacy", "privacy", "개인정보 설정"),
    ]
    items = "".join(
        f'<a href="{path}" class="{"active" if key == active else ""}"'
        f'{" aria-current=\"page\"" if key == active else ""}>{label}</a>'
        for path, key, label in links
    )
    return f'<nav class="settings-nav" aria-label="계정 설정">{items}</nav>'


def jpeg_dimensions(image_bytes):
    start_of_frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    position = 2
    while position + 3 < len(image_bytes):
        if image_bytes[position] != 0xFF:
            position += 1
            continue
        while position < len(image_bytes) and image_bytes[position] == 0xFF:
            position += 1
        if position >= len(image_bytes):
            break
        marker = image_bytes[position]
        position += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(image_bytes):
            break
        segment_length = int.from_bytes(image_bytes[position:position + 2], "big")
        if segment_length < 2 or position + segment_length > len(image_bytes):
            break
        if marker in start_of_frame_markers and segment_length >= 7:
            height = int.from_bytes(image_bytes[position + 3:position + 5], "big")
            width = int.from_bytes(image_bytes[position + 5:position + 7], "big")
            return width, height
        if marker == 0xDA:
            break
        position += segment_length
    return None


def png_dimensions(image_bytes):
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    position = 8
    dimensions = None
    saw_image_data = False
    while position + 12 <= len(image_bytes):
        chunk_length = int.from_bytes(image_bytes[position:position + 4], "big")
        chunk_type = image_bytes[position + 4:position + 8]
        chunk_end = position + 12 + chunk_length
        if chunk_length > MAX_PROFILE_IMAGE_BYTES or chunk_end > len(image_bytes):
            return None
        chunk_data = image_bytes[position + 8:position + 8 + chunk_length]
        expected_crc = int.from_bytes(image_bytes[position + 8 + chunk_length:chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return None
        if position == 8:
            if chunk_type != b"IHDR" or chunk_length != 13:
                return None
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            dimensions = (width, height)
        elif chunk_type == b"IDAT":
            saw_image_data = True
        elif chunk_type == b"IEND":
            if chunk_length != 0 or chunk_end != len(image_bytes):
                return None
            return dimensions if dimensions and saw_image_data else None
        position = chunk_end
    return None


def save_profile_image(data_url):
    formats = {
        "data:image/jpeg;base64,": (".jpg", b"\xff\xd8\xff", jpeg_dimensions),
        "data:image/png;base64,": (".png", b"\x89PNG\r\n\x1a\n", png_dimensions),
    }
    selected = next(((prefix, details) for prefix, details in formats.items() if data_url.startswith(prefix)), None)
    if not selected:
        raise ValueError("프로필 사진을 다시 선택해주세요.")
    prefix, (extension, signature, dimension_reader) = selected
    encoded = data_url[len(prefix):]
    if len(encoded) > ((MAX_PROFILE_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("프로필 사진 용량이 너무 큽니다.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("프로필 사진 형식을 확인해주세요.")
    if not image_bytes or len(image_bytes) > MAX_PROFILE_IMAGE_BYTES:
        raise ValueError("프로필 사진은 500KB 이하로 등록해주세요.")
    if not image_bytes.startswith(signature):
        raise ValueError("프로필 사진 형식을 확인해주세요.")
    if extension == ".jpg" and not image_bytes.endswith(b"\xff\xd9"):
        raise ValueError("JPEG 프로필 사진 형식을 확인해주세요.")
    dimensions = dimension_reader(image_bytes)
    if not dimensions or min(dimensions) < 1 or max(dimensions) > 512:
        raise ValueError("프로필 사진 크기를 확인해주세요.")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}{extension}"
    (PROFILE_DIR / filename).write_bytes(image_bytes)
    return f"/static/profiles/{filename}"


def delete_profile_image(image_url):
    prefix = "/static/profiles/"
    if not image_url or not image_url.startswith(prefix):
        return
    filename = image_url.removeprefix(prefix)
    if Path(filename).name != filename:
        return
    target = PROFILE_DIR / filename
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def save_product_image(data_url):
    prefix = "data:image/jpeg;base64,"
    if not data_url.startswith(prefix):
        raise ValueError("상품 사진을 다시 선택해주세요.")
    encoded = data_url[len(prefix):]
    if len(encoded) > ((MAX_PRODUCT_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("상품 사진 용량이 너무 큽니다.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("상품 사진 형식을 확인해주세요.")
    dimensions = jpeg_dimensions(image_bytes)
    if (
        not image_bytes
        or len(image_bytes) > MAX_PRODUCT_IMAGE_BYTES
        or not image_bytes.startswith(b"\xff\xd8\xff")
        or not image_bytes.endswith(b"\xff\xd9")
        or not dimensions
        or min(dimensions) < 1
        or max(dimensions) > 1200
    ):
        raise ValueError("상품 사진은 최대 1200px, 900KB 이하 JPEG여야 합니다.")
    PRODUCT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.jpg"
    (PRODUCT_UPLOAD_DIR / filename).write_bytes(image_bytes)
    return f"/static/product_uploads/{filename}"


def delete_product_image(image_url):
    prefix = "/static/product_uploads/"
    if not image_url or not image_url.startswith(prefix):
        return
    filename = image_url.removeprefix(prefix)
    if Path(filename).name != filename:
        return
    try:
        (PRODUCT_UPLOAD_DIR / filename).unlink(missing_ok=True)
    except OSError:
        pass


def save_chat_image(data_url):
    prefix = "data:image/jpeg;base64,"
    if not data_url.startswith(prefix):
        raise ValueError("채팅 사진을 다시 선택해주세요.")
    encoded = data_url[len(prefix):]
    if len(encoded) > ((MAX_CHAT_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("채팅 사진 용량이 너무 큽니다.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("채팅 사진 형식을 확인해주세요.")
    dimensions = jpeg_dimensions(image_bytes)
    if (
        not image_bytes
        or len(image_bytes) > MAX_CHAT_IMAGE_BYTES
        or not image_bytes.startswith(b"\xff\xd8\xff")
        or not image_bytes.endswith(b"\xff\xd9")
        or not dimensions
        or min(dimensions) < 1
        or max(dimensions) > 1600
    ):
        raise ValueError("채팅 사진은 최대 1600px, 900KB 이하 JPEG여야 합니다.")
    CHAT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.jpg"
    (CHAT_UPLOAD_DIR / filename).write_bytes(image_bytes)
    return f"/static/chat_uploads/{filename}"


def create_backup(label="auto"):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"market-{label}-{stamp}.wmbak"
    temporary = BACKUP_DIR / f".{secrets.token_hex(12)}.db"
    source_conn = sqlite3.connect(DB_PATH)
    target_conn = sqlite3.connect(temporary)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    try:
        plaintext = temporary.read_bytes()
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = derive_backup_key(backup_passphrase(), salt)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, BACKUP_MAGIC)
        target.write_bytes(BACKUP_MAGIC + salt + nonce + ciphertext)
    finally:
        temporary.unlink(missing_ok=True)
    backups = sorted(BACKUP_DIR.glob("market-*.wmbak"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old_backup in backups[14:]:
        old_backup.unlink(missing_ok=True)
    return target


def decrypt_backup(source):
    payload = source.read_bytes()
    if len(payload) < len(BACKUP_MAGIC) + 16 + 12 + 16 or not payload.startswith(BACKUP_MAGIC):
        raise ValueError("암호화된 White Market 백업 형식이 아닙니다.")
    offset = len(BACKUP_MAGIC)
    salt = payload[offset:offset + 16]
    nonce = payload[offset + 16:offset + 28]
    ciphertext = payload[offset + 28:]
    key = derive_backup_key(backup_passphrase(), salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, BACKUP_MAGIC)
    except Exception as exc:
        raise ValueError("백업 암호화 키가 다르거나 파일이 훼손되었습니다.") from exc
    handle, filename = tempfile.mkstemp(prefix="white-market-restore-", suffix=".db")
    os.close(handle)
    temporary = Path(filename)
    temporary.write_bytes(plaintext)
    try:
        connection = sqlite3.connect(temporary)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.close()
    except sqlite3.DatabaseError as exc:
        temporary.unlink(missing_ok=True)
        raise ValueError("백업 DB를 읽을 수 없습니다.") from exc
    if integrity != "ok":
        temporary.unlink(missing_ok=True)
        raise ValueError("백업 DB 무결성 검사에 실패했습니다.")
    return temporary


def migrate_plaintext_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("WHITE_MARKET_BACKUP_KEY"):
        return
    for source in BACKUP_DIR.glob("market-*.db"):
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = derive_backup_key(backup_passphrase(), salt)
        ciphertext = AESGCM(key).encrypt(nonce, source.read_bytes(), BACKUP_MAGIC)
        target = source.with_suffix(".wmbak")
        target.write_bytes(BACKUP_MAGIC + salt + nonce + ciphertext)
        source.unlink(missing_ok=True)


def ensure_daily_backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    migrate_plaintext_backups()
    if not any(BACKUP_DIR.glob(f"market-auto-{today}-*.wmbak")):
        return create_backup("auto")
    return None


def add_notification(conn, user_id, kind, title, body, product_id=None, link_url=""):
    preference_column = NOTIFICATION_PREFERENCES.get(kind)
    if preference_column:
        preference = conn.execute(f"SELECT {preference_column} AS enabled FROM users WHERE id = ?", (user_id,)).fetchone()
        if not preference or not preference["enabled"]:
            return
    if link_url and (not link_url.startswith("/") or link_url.startswith("//")):
        link_url = ""
    conn.execute(
        """
        INSERT INTO notifications(user_id, kind, title, body, product_id, link_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, kind, title[:80], body[:500], product_id, link_url[:500], now()),
    )


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, digest_hex):
    _, candidate = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, digest_hex)


def username_validation_error(username):
    if username.casefold() == "admin":
        return "admin은 사용할 수 없는 아이디입니다."
    if not USERNAME_RE.fullmatch(username):
        return "아이디는 영문과 숫자를 모두 포함한 5-20자로 입력하세요."
    return ""


def password_validation_error(password):
    if len(password) < 8 or len(password) > 128:
        return "비밀번호는 8-128자로 입력하세요."
    if not re.search(r"[A-Za-z]", password):
        return "비밀번호에 영문을 포함하세요."
    if not re.search(r"[0-9]", password):
        return "비밀번호에 숫자를 포함하세요."
    if not re.search(r"[^A-Za-z0-9\s]", password):
        return "비밀번호에 특수문자를 포함하세요."
    if re.search(r"\s", password):
        return "비밀번호에는 공백을 사용할 수 없습니다."
    return ""


def normalize_phone(value):
    phone = re.sub(r"[^0-9]", "", value or "")
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("휴대전화 번호를 010-1234-5678 형식으로 입력하세요.")
    return phone


def format_phone(phone):
    phone = re.sub(r"[^0-9]", "", phone or "")
    if len(phone) == 11:
        return f"{phone[:3]}-{phone[3:7]}-{phone[7:]}"
    if len(phone) == 10:
        return f"{phone[:3]}-{phone[3:6]}-{phone[6:]}"
    return phone


def mask_phone(phone):
    formatted = format_phone(phone)
    if len(formatted) >= 8:
        return formatted[:4] + "****" + formatted[-4:]
    return "등록된 번호"


def reset_code_digest(challenge_id, code):
    return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), challenge_id.encode("utf-8"), 50_000).hex()


def send_sms_verification(phone, code, purpose="비밀번호 재설정"):
    if not SMS_WEBHOOK_URL:
        if APP_PRODUCTION:
            raise ValueError("운영 환경의 SMS 발송 서비스가 설정되지 않았습니다.")
        return code
    payload = json.dumps(
        {"to": phone, "message": f"[White Market] {purpose} 인증번호는 {code}입니다. 5분 안에 입력하세요."},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        SMS_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": GEOCODING_USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:  # nosec B310
            if not 200 <= response.status < 300:
                raise ValueError("인증번호 발송 서비스가 응답하지 않습니다.")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError("인증번호를 발송하지 못했습니다. 잠시 후 다시 시도하세요.") from exc
    return None


def validate_runtime_config():
    if APP_ENV not in {"development", "test", "production"}:
        raise RuntimeError("WHITE_MARKET_ENV는 development, test, production 중 하나여야 합니다.")
    if not APP_PRODUCTION:
        return
    if not APP_HTTPS:
        raise RuntimeError("운영 환경에서는 WHITE_MARKET_HTTPS=1이 필요합니다.")
    if not SMS_WEBHOOK_URL.lower().startswith("https://"):
        raise RuntimeError("운영 환경에서는 HTTPS SMS_WEBHOOK_URL이 필요합니다.")
    admin_password = os.environ.get("WHITE_MARKET_ADMIN_PASSWORD", "")
    if len(admin_password) < 12:
        raise RuntimeError("운영 환경의 WHITE_MARKET_ADMIN_PASSWORD는 12자 이상이어야 합니다.")
    if len(os.environ.get("WHITE_MARKET_BACKUP_KEY", "")) < 24:
        raise RuntimeError("운영 환경의 WHITE_MARKET_BACKUP_KEY는 24자 이상이어야 합니다.")


def captcha_digest(challenge_id, answer):
    return hashlib.sha256(f"{challenge_id}:{answer}".encode("utf-8")).hexdigest()


def create_registration_captcha():
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 2
    challenge_id = secrets.token_urlsafe(24)
    created_at = int(time.time())
    with db() as conn:
        conn.execute("DELETE FROM registration_captchas WHERE expires_at < ? OR used = 1", (created_at,))
        conn.execute(
            """
            INSERT INTO registration_captchas(id, question, answer_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                challenge_id,
                f"{left} + {right} = ?",
                captcha_digest(challenge_id, str(left + right)),
                created_at,
                created_at + CAPTCHA_SECONDS,
            ),
        )
    return challenge_id


def consume_registration_captcha(challenge_id, answer):
    if not challenge_id or not answer:
        return False
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM registration_captchas WHERE id = ?",
            (challenge_id,),
        ).fetchone()
        if not row or row["used"] or row["expires_at"] < int(time.time()):
            return False
        conn.execute("UPDATE registration_captchas SET used = 1 WHERE id = ?", (challenge_id,))
        expected = captcha_digest(challenge_id, answer.strip())
        return hmac.compare_digest(row["answer_hash"], expected)


def reverse_geocode_district(latitude, longitude):
    global LAST_GEOCODE_REQUEST_AT

    lat_key = round(latitude, 3)
    lon_key = round(longitude, 3)
    cache_key = (lat_key, lon_key)
    cached = GEOCODE_CACHE.get(cache_key)
    if cached:
        return cached

    params = urlencode(
        {
            "format": "jsonv2",
            "lat": f"{latitude:.7f}",
            "lon": f"{longitude:.7f}",
            "zoom": "12",
            "addressdetails": "1",
            "accept-language": "ko",
            "layer": "address",
        }
    )
    request = urllib.request.Request(
        f"{GEOCODING_BASE_URL.rstrip('/')}/reverse?{params}",
        headers={"User-Agent": GEOCODING_USER_AGENT, "Accept": "application/json"},
    )

    with GEOCODE_LOCK:
        wait_seconds = 1.0 - (time.monotonic() - LAST_GEOCODE_REQUEST_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            LAST_GEOCODE_REQUEST_AT = time.monotonic()

    address = payload.get("address", {})
    candidates = [
        address.get("borough"),
        address.get("city_district"),
        address.get("district"),
        address.get("county"),
        address.get("municipality"),
        address.get("suburb"),
    ]
    district = next((value.strip() for value in candidates if value and value.strip().endswith("구")), "")
    if not district:
        display_parts = [part.strip() for part in payload.get("display_name", "").split(",")]
        district = next((part for part in display_parts if part.endswith("구")), "")
    if not district:
        raise ValueError("현재 위치에서 구 단위 행정구역을 찾지 못했습니다.")

    result = {"district": district, "attribution": "© OpenStreetMap contributors"}
    if len(GEOCODE_CACHE) >= 256:
        GEOCODE_CACHE.clear()
    GEOCODE_CACHE[cache_key] = result
    return result


def rating_sql(user_alias="u"):
    return f"""
    COALESCE((SELECT AVG(ur.score) FROM user_ratings ur WHERE ur.reviewee_id = {user_alias}.id), 0) AS rating_average,
    (SELECT COUNT(*) FROM user_ratings ur WHERE ur.reviewee_id = {user_alias}.id) AS rating_count
    """


def favorite_sql(viewer_id, product_alias="p"):
    viewer_id = int(viewer_id or 0)
    return f"""
    EXISTS(
        SELECT 1 FROM favorites f_card
        WHERE f_card.user_id = {viewer_id} AND f_card.product_id = {product_alias}.id
    ) AS is_favorite
    """


def engagement_sql(product_alias="p"):
    return f"""
    (SELECT COUNT(*) FROM favorites f_stats WHERE f_stats.product_id = {product_alias}.id) AS favorite_count,
    (
        SELECT COUNT(DISTINCT m_stats.sender_id)
        FROM messages m_stats
        WHERE m_stats.product_id = {product_alias}.id
          AND m_stats.receiver_id = {product_alias}.seller_id
    ) AS chat_count
    """


def write_audit_log(conn, admin_id, action, target_type, target_id=None, details="", ip_address=""):
    conn.execute(
        """
        INSERT INTO admin_audit_logs(admin_id, action, target_type, target_id, details, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (admin_id, action, target_type, target_id, details[:1000], ip_address[:64], now()),
    )


def write_account_log(conn, user_id, action, details="", ip_address=""):
    conn.execute(
        """
        INSERT INTO account_audit_logs(user_id, action, details, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, action[:80], details[:1000], ip_address[:64], now()),
    )


def seed_demo_market(conn):
    demo_users = [
        ("hana21", "하나", "01010000001", "마포구", "생활 소품과 전자기기를 정리하고 있어요."),
        ("minsu88", "민수", "01010000002", "성동구", "직거래 위주로 천천히 판매합니다."),
        ("jiyun24", "지윤", "01010000003", "송파구", "깨끗하게 사용한 물건만 올립니다."),
        ("seojun77", "서준", "01010000004", "영등포구", "카메라와 디지털 기기를 좋아합니다."),
        ("dami55", "다미", "01010000005", "강남구", "가구와 책을 주로 나눠요."),
    ]
    demo_ids = {}
    for username, display_name, phone, location, bio in demo_users:
        user = conn.execute("SELECT id, phone FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            salt, digest = hash_password("Demo!12345")
            user_id = conn.execute(
                """
                INSERT INTO users(username, password_salt, password_hash, display_name, bio, phone, location, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (username, salt, digest, display_name, bio, phone, location, now()),
            ).lastrowid
        else:
            user_id = user["id"]
            if not user["phone"]:
                conn.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))
        demo_ids[username] = user_id

    demo_products = [
        ("hana21", "재택근무용 노트북", "배터리 상태가 좋고 충전기를 함께 드립니다.", "디지털기기", 390000, 1.1, "/static/products/silver-laptop.png"),
        ("hana21", "블랙 무선 헤드폰", "출퇴근할 때 사용했으며 이어패드 상태가 좋습니다.", "디지털기기", 78000, 0.7, "/static/products/black-headphones.png"),
        ("hana21", "원목 독서 의자", "등받이가 편하고 흔들림 없는 원목 의자입니다.", "가구/인테리어", 72000, 1.8, "/static/products/walnut-chair.png"),
        ("minsu88", "영상 수업용 노트북", "화상 수업과 문서 작업에 사용한 노트북입니다.", "디지털기기", 360000, 2.4, "/static/products/silver-laptop.png"),
        ("minsu88", "편안한 패브릭 의자", "작업실에서 사용했고 쿠션 꺼짐이 거의 없습니다.", "가구/인테리어", 68000, 2.0, "/static/products/walnut-chair.png"),
        ("jiyun24", "여행용 미러리스 카메라", "여행 두 번에 사용한 카메라로 기본 렌즈가 포함됩니다.", "디지털기기", 295000, 3.0, "/static/products/mirrorless-camera.png"),
        ("jiyun24", "집중용 헤드폰", "노이즈 캔슬링이 잘 되고 구성품을 모두 보관 중입니다.", "디지털기기", 88000, 1.5, "/static/products/black-headphones.png"),
        ("seojun77", "입문 사진용 카메라", "셔터와 버튼 모두 정상이며 스트랩을 함께 드립니다.", "디지털기기", 275000, 2.7, "/static/products/mirrorless-camera.png"),
        ("seojun77", "휴대용 업무 노트북", "외관에 미세한 사용감이 있지만 기능은 정상입니다.", "디지털기기", 330000, 4.1, "/static/products/silver-laptop.png"),
        ("dami55", "거실 원목 암체어", "햇빛이 들지 않는 거실에서 사용한 의자입니다.", "가구/인테리어", 91000, 0.9, "/static/products/walnut-chair.png"),
        ("dami55", "무선 오버이어 헤드폰", "실내 음악 감상용으로만 사용했습니다.", "디지털기기", 83000, 1.2, "/static/products/black-headphones.png"),
        ("dami55", "취미 촬영 미러리스", "취미 촬영 입문용으로 사용했고 보관 상태가 좋습니다.", "디지털기기", 285000, 1.9, "/static/products/mirrorless-camera.png"),
    ]
    for username, title, description, category, price, distance, image_url in demo_products:
        seller_id = demo_ids[username]
        exists = conn.execute("SELECT 1 FROM products WHERE seller_id = ? AND title = ?", (seller_id, title)).fetchone()
        if not exists:
            conn.execute(
                """
                INSERT INTO products(seller_id, title, description, category, price, distance_km, status, image_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, '판매중', ?, ?, ?)
                """,
                (seller_id, title, description, category, price, distance, image_url, now(), now()),
            )

    demo_conversations = [
        ("hana21", "minsu88", "노트북 상태 문의드려요.", 5, "친절하게 설명해 주셨어요."),
        ("minsu88", "hana21", "직거래 장소를 조율해 볼까요?", 4, "약속 시간을 잘 지켜요."),
        ("jiyun24", "seojun77", "카메라 렌즈 상태가 궁금합니다.", 5, "답변이 빠르고 정확해요."),
        ("seojun77", "dami55", "헤드폰 구성품이 모두 있나요?", 4, "대화가 편안했습니다."),
        ("dami55", "jiyun24", "의자 크기를 확인하고 싶어요.", 5, "상세하게 알려주셨어요."),
    ]
    for reviewer_name, reviewee_name, message, score, review in demo_conversations:
        reviewer_id = demo_ids[reviewer_name]
        reviewee_id = demo_ids[reviewee_name]
        chatted = conn.execute(
            "SELECT 1 FROM messages WHERE sender_id = ? AND receiver_id = ? LIMIT 1",
            (reviewer_id, reviewee_id),
        ).fetchone()
        if not chatted:
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, body, created_at) VALUES (?, ?, ?, ?)",
                (reviewer_id, reviewee_id, message, now()),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_ratings(reviewer_id, reviewee_id, context_type, context_id, score, review, created_at, updated_at)
            VALUES (?, ?, 'chat', ?, ?, ?, ?, ?)
            """,
            (reviewer_id, reviewee_id, reviewee_id, score, review, now(), now()),
        )


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                profile_image_url TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '서울',
                is_admin INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                failed_login_count INTEGER NOT NULL DEFAULT 0,
                password_reset_required INTEGER NOT NULL DEFAULT 0,
                notify_chat INTEGER NOT NULL DEFAULT 1,
                notify_price INTEGER NOT NULL DEFAULT 1,
                notify_transaction INTEGER NOT NULL DEFAULT 1,
                notify_notice INTEGER NOT NULL DEFAULT 1,
                notify_security INTEGER NOT NULL DEFAULT 1,
                two_factor_enabled INTEGER NOT NULL DEFAULT 0,
                two_factor_secret TEXT NOT NULL DEFAULT '',
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase
            ON users(username COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS user_blocks (
                blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (blocker_id, blocked_id)
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                price INTEGER NOT NULL,
                distance_km REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT '판매중',
                image_url TEXT NOT NULL DEFAULT '',
                view_count INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS recent_views (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                viewed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS keyword_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                keyword TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, keyword)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                body TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                image_url TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(product_id, position)
            );

            CREATE TABLE IF NOT EXISTS chat_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                buyer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                seller_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action_type TEXT NOT NULL CHECK(action_type IN ('offer', 'appointment')),
                status TEXT NOT NULL DEFAULT 'pending',
                proposed_price INTEGER,
                meeting_place TEXT NOT NULL DEFAULT '',
                meeting_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_action_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id INTEGER NOT NULL REFERENCES chat_actions(id) ON DELETE CASCADE,
                actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                from_status TEXT NOT NULL DEFAULT '',
                to_status TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                seller_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                buyer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT '거래요청',
                agreed_price INTEGER NOT NULL DEFAULT 0 CHECK(agreed_price >= 0),
                review TEXT NOT NULL DEFAULT '',
                rating INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_code TEXT NOT NULL UNIQUE,
                transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE RESTRICT,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                amount INTEGER NOT NULL CHECK(amount > 0),
                created_at TEXT NOT NULL,
                CHECK(sender_id != receiver_id)
            );

            CREATE TABLE IF NOT EXISTS user_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reviewee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                context_type TEXT NOT NULL CHECK(context_type IN ('chat', 'transaction')),
                context_id INTEGER NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
                review TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(reviewer_id, reviewee_id, context_type, context_id)
            );

            CREATE INDEX IF NOT EXISTS idx_user_ratings_reviewee ON user_ratings(reviewee_id);

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                link_url TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                username_attempt TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                suspicious INTEGER NOT NULL DEFAULT 0,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS registration_captchas (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS password_reset_challenges (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS login_verification_challenges (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                target_type TEXT NOT NULL CHECK(target_type IN ('user', 'product')),
                target_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '접수',
                admin_note TEXT NOT NULL DEFAULT '',
                handled_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS report_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id INTEGER,
                details TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_views (
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                viewer_key TEXT NOT NULL,
                viewed_on TEXT NOT NULL,
                PRIMARY KEY(product_id, viewer_key, viewed_on)
            );

            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                query_text TEXT NOT NULL,
                filters_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transaction_checklists (
                transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                checked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(transaction_id, user_id, item_key)
            );

            CREATE TABLE IF NOT EXISTS transaction_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                from_status TEXT NOT NULL DEFAULT '',
                to_status TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT NOT NULL DEFAULT '',
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        product_columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "image_url" not in product_columns:
            conn.execute("ALTER TABLE products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        if "view_count" not in product_columns:
            conn.execute("ALTER TABLE products ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0")
        if "thumbnail_url" not in product_columns:
            conn.execute("ALTER TABLE products ADD COLUMN thumbnail_url TEXT NOT NULL DEFAULT ''")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "temperature" in user_columns:
            conn.execute("ALTER TABLE users DROP COLUMN temperature")
        if "phone" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
        if "failed_login_count" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0")
        if "password_reset_required" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN password_reset_required INTEGER NOT NULL DEFAULT 0")
        if "profile_image_url" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN profile_image_url TEXT NOT NULL DEFAULT ''")
        for column in ["notify_chat", "notify_price", "notify_transaction", "notify_notice", "notify_security"]:
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1")
        if "two_factor_enabled" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN two_factor_enabled INTEGER NOT NULL DEFAULT 0")
        if "two_factor_secret" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN two_factor_secret TEXT NOT NULL DEFAULT ''")
        if "last_active_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT NOT NULL DEFAULT ''")
        if "must_change_password" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        for column in ["created_at", "last_seen_at", "ip_address", "user_agent"]:
            if column not in session_columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        notification_columns = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        if "link_url" not in notification_columns:
            conn.execute("ALTER TABLE notifications ADD COLUMN link_url TEXT NOT NULL DEFAULT ''")
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "is_read" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")
        if "product_id" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN product_id INTEGER REFERENCES products(id) ON DELETE SET NULL")
        if "image_url" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(product_images)").fetchall()}
        if "thumbnail_url" not in image_columns:
            conn.execute("ALTER TABLE product_images ADD COLUMN thumbnail_url TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE product_images SET thumbnail_url = image_url WHERE thumbnail_url = ''")
        conn.execute("UPDATE products SET thumbnail_url = image_url WHERE thumbnail_url = ''")
        action_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_actions)").fetchall()}
        if "supersedes_id" not in action_columns:
            conn.execute("ALTER TABLE chat_actions ADD COLUMN supersedes_id INTEGER REFERENCES chat_actions(id) ON DELETE SET NULL")
        transaction_columns = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        if "agreed_price" not in transaction_columns:
            conn.execute("ALTER TABLE transactions ADD COLUMN agreed_price INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            UPDATE transactions
            SET agreed_price = COALESCE((SELECT price FROM products WHERE products.id = transactions.product_id), 0)
            WHERE agreed_price <= 0
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_product ON messages(product_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(sender_id, receiver_id, product_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_actions_conversation ON chat_actions(product_id, buyer_id, seller_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_search ON products(is_deleted, status, category, price, distance_km, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_seller ON products(seller_id, is_deleted, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_cleanup ON notifications(user_id, is_read, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_search_history_term ON search_history(query_text, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transaction_history_tx ON transaction_history(transaction_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_transfers_sender ON wallet_transfers(sender_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_transfers_receiver ON wallet_transfers(receiver_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_events_user ON login_events(user_id, id DESC)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone != ''")
        conn.execute("UPDATE users SET two_factor_enabled = 0, two_factor_secret = '' WHERE two_factor_enabled != 0 OR two_factor_secret != ''")
        conn.execute("DROP TABLE IF EXISTS user_recovery_codes")
        conn.execute(
            """
            INSERT OR IGNORE INTO product_images(product_id, image_url, position, is_primary, created_at)
            SELECT id, image_url, 0, 1, created_at FROM products
            WHERE image_url != ''
              AND NOT EXISTS (SELECT 1 FROM product_images pi WHERE pi.product_id = products.id)
            """
        )
        conn.execute(
            "DELETE FROM notifications WHERE is_read = 1 AND created_at < ?",
            ((datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M"),),
        )
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if count == 0:
            initial_password = os.environ.get("WHITE_MARKET_ADMIN_PASSWORD", "")
            validation_error = password_validation_error(initial_password)
            if validation_error:
                raise RuntimeError(f"WHITE_MARKET_ADMIN_PASSWORD 설정 필요: {validation_error}")
            salt, digest = hash_password(initial_password)
            admin_id = conn.execute(
                """
                INSERT INTO users(username, password_salt, password_hash, display_name, bio, phone, location, is_admin, must_change_password, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
                """,
                ("admin", salt, digest, "관리자", "플랫폼 운영 계정입니다.", "01000000000", "서울", now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO notices(admin_id, title, body, created_at) VALUES (?, ?, ?, ?)",
                (admin_id, "서비스 오픈 안내", "안전한 거래를 위해 채팅과 거래 내역을 꼭 남겨주세요.", now()),
            )
        admin = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
        if admin:
            sample_products = [
                ("가벼운 실버 노트북", "문서 작업과 온라인 강의용으로 사용한 노트북입니다. 충전기 포함이며 상태가 깨끗합니다.", "디지털기기", 420000, 1.4, "/static/products/silver-laptop.png"),
                ("원목 라운지 의자", "거실에서 사용한 원목 의자입니다. 흔들림 없고 패브릭 쿠션 오염도 거의 없습니다.", "가구/인테리어", 85000, 2.1, "/static/products/walnut-chair.png"),
                ("입문용 미러리스 카메라", "가볍게 여행 사진을 찍기 좋은 미러리스 카메라입니다. 기본 렌즈와 스트랩을 함께 드립니다.", "디지털기기", 310000, 3.2, "/static/products/mirrorless-camera.png"),
                ("노이즈 캔슬링 헤드폰", "실내에서만 사용한 오버이어 헤드폰입니다. 이어패드 상태가 좋고 보관 파우치가 있습니다.", "디지털기기", 95000, 0.8, "/static/products/black-headphones.png"),
            ]
            for title, description, category, price, distance, image_url in sample_products:
                exists = conn.execute(
                    "SELECT 1 FROM products WHERE seller_id = ? AND title = ?",
                    (admin["id"], title),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """
                        INSERT INTO products(seller_id, title, description, category, price, distance_km, status, image_url, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, '판매중', ?, ?, ?)
                        """,
                        (admin["id"], title, description, category, price, distance, image_url, now(), now()),
                    )

        seed_demo_market(conn)

        conn.execute(
            """
            INSERT OR IGNORE INTO wallets(user_id, balance, updated_at)
            SELECT id, ?, ? FROM users
            """,
            (INITIAL_DEMO_BALANCE, now()),
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO user_ratings(reviewer_id, reviewee_id, context_type, context_id, score, review, created_at, updated_at)
            SELECT buyer_id, seller_id, 'transaction', id, rating, review, created_at, updated_at
            FROM transactions WHERE status = '거래완료' AND rating BETWEEN 1 AND 5
            """
        )

        conn.execute(
            """
            UPDATE products
            SET status = '판매중', updated_at = ?
            WHERE status = '예약중'
              AND NOT EXISTS (
                SELECT 1 FROM transactions
                WHERE transactions.product_id = products.id AND transactions.status = '예약중'
              )
            """,
            (now(),),
        )


class App(BaseHTTPRequestHandler):
    server_version = "WhiteMarket/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = one_value(parse_qs(parsed.query))
            form = self.read_form() if method == "POST" else {}

            if path.startswith("/static/"):
                return self.serve_static(path.removeprefix("/static/"))
            if method == "POST" and not self.valid_csrf(form):
                return self.error_page(HTTPStatus.FORBIDDEN, "요청 보안 토큰이 올바르지 않습니다.")
            current_user = self.current_user()
            password_change_paths = {"/password-change-required", "/logout"}
            if current_user and current_user["must_change_password"] and path not in password_change_paths:
                return self.redirect("/password-change-required")

            routes = {
                ("GET", "/"): self.home,
                ("GET", "/register"): self.register_page,
                ("POST", "/register"): self.register,
                ("GET", "/api/username-check"): self.username_check,
                ("GET", "/api/location/reverse"): self.reverse_location,
                ("GET", "/captcha.svg"): self.captcha_image,
                ("GET", "/login"): self.login_page,
                ("POST", "/login"): self.login,
                ("POST", "/login/verify"): self.verify_conditional_login,
                ("GET", "/password-change-required"): self.password_change_required_page,
                ("POST", "/password-change-required"): self.password_change_required,
                ("GET", "/password-reset"): self.password_reset_page,
                ("POST", "/password-reset/request"): self.password_reset_request,
                ("POST", "/password-reset/confirm"): self.password_reset_confirm,
                ("POST", "/logout"): self.logout,
                ("GET", "/mypage"): self.mypage,
                ("POST", "/mypage"): self.update_mypage,
                ("GET", "/password-change"): self.password_change_page,
                ("POST", "/password-change"): self.change_password,
                ("GET", "/users"): self.users_page,
                ("GET", "/user"): self.user_detail,
                ("POST", "/block"): self.block_user,
                ("GET", "/report"): self.report_page,
                ("POST", "/report"): self.create_report,
                ("GET", "/products"): self.products_page,
                ("GET", "/product"): self.product_detail,
                ("GET", "/product/new"): self.product_form,
                ("POST", "/product/new"): self.create_product,
                ("GET", "/product/edit"): self.product_form,
                ("POST", "/product/edit"): self.update_product,
                ("POST", "/product/delete"): self.delete_product,
                ("POST", "/favorite"): self.toggle_favorite,
                ("POST", "/alerts"): self.add_alert,
                ("POST", "/alerts/delete"): self.delete_alert,
                ("GET", "/chat"): self.chat_page,
                ("GET", "/chat/stream"): self.chat_stream,
                ("POST", "/chat"): self.send_message,
                ("POST", "/chat/offer"): self.create_chat_offer,
                ("POST", "/chat/reserve"): self.reserve_chat_transaction,
                ("POST", "/chat/action"): self.update_chat_action,
                ("POST", "/rating/chat"): self.rate_chat_user,
                ("GET", "/transactions"): self.transactions_page,
                ("POST", "/transaction/status"): self.update_transaction_status,
                ("POST", "/transaction/payment"): self.transfer_transaction_payment,
                ("POST", "/transaction/review"): self.review_transaction,
                ("POST", "/transaction/checklist"): self.update_transaction_checklist,
                ("GET", "/transaction/evidence"): self.transaction_evidence,
                ("GET", "/notifications"): self.notifications_page,
                ("GET", "/notification/open"): self.open_notification,
                ("POST", "/notifications/read"): self.read_notifications,
                ("GET", "/settings"): self.settings_page,
                ("POST", "/settings/notifications"): self.update_notification_settings,
                ("GET", "/security"): self.security_page,
                ("POST", "/security/sessions/logout-others"): self.logout_other_sessions,
                ("GET", "/privacy"): self.privacy_page,
                ("GET", "/privacy/export"): self.export_personal_data,
                ("POST", "/account/delete"): self.delete_account,
                ("GET", "/notices"): self.notices_page,
                ("POST", "/notices"): self.create_notice,
                ("GET", "/notice/edit"): self.notice_edit_page,
                ("POST", "/notice/edit"): self.update_notice,
                ("POST", "/notice/delete"): self.delete_notice,
                ("GET", "/admin"): self.admin_page,
                ("GET", "/admin/reports"): self.admin_reports_page,
                ("POST", "/admin/report"): self.admin_update_report,
                ("GET", "/admin/audit"): self.admin_audit_page,
                ("GET", "/admin/operations"): self.admin_operations_page,
                ("POST", "/admin/backup"): self.admin_create_backup,
                ("POST", "/admin/restore"): self.admin_restore_backup,
                ("POST", "/admin/error/resolve"): self.admin_resolve_error,
                ("POST", "/admin/user"): self.admin_user,
                ("POST", "/admin/product"): self.admin_product,
            }
            handler = routes.get((method, path))
            if not handler:
                return self.error_page(HTTPStatus.NOT_FOUND, "페이지를 찾을 수 없습니다.")
            return handler(query, form)
        except RateLimitExceeded as exc:
            self.extra_headers = [("Retry-After", str(exc.retry_after))]
            return self.error_page(HTTPStatus.TOO_MANY_REQUESTS, str(exc))
        except ValueError as exc:
            return self.error_page(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            request_id = secrets.token_hex(6)
            print(f"Unhandled error [{request_id}]:", repr(exc))
            try:
                with db() as conn:
                    conn.execute(
                        """
                        INSERT INTO error_logs(request_id, method, path, error_type, message, traceback, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            method,
                            getattr(self, "path", "")[:500],
                            type(exc).__name__[:100],
                            str(exc)[:1000],
                            traceback.format_exc()[-12000:],
                            now(),
                        ),
                    )
            except Exception:
                pass
            return self.error_page(HTTPStatus.INTERNAL_SERVER_ERROR, f"서버 오류가 발생했습니다. 오류 번호: {request_id}")

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 12_000_000:
            raise ValueError("요청이 너무 큽니다.")
        raw = self.rfile.read(length).decode("utf-8")
        return one_value(parse_qs(raw, keep_blank_values=True))

    def serve_static(self, name):
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self.error_page(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
        content_types = {
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }
        content_type = content_types.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(target.read_bytes())

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_json_download(self, payload, filename):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def cookie_value(self, name):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def current_user(self):
        token = self.cookie_value("market_session")
        if not token:
            return None
        with db() as conn:
            user = conn.execute(
                """
                SELECT users.*, sessions.csrf, sessions.token AS session_token,
                       sessions.created_at AS session_created_at, sessions.last_seen_at AS session_last_seen_at
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token = ? AND sessions.expires_at > ?
                """,
                (token, int(time.time())),
            ).fetchone()
            if user:
                active_now = now()
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token = ? AND last_seen_at != ?",
                    (active_now, token, active_now),
                )
                conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (active_now, user["id"]))
        if not user or user["status"] != "active":
            return None
        return user

    def valid_csrf(self, form):
        user = self.current_user()
        if not user:
            return True
        return hmac.compare_digest(form.get("csrf", ""), user["csrf"])

    def csrf_input(self, user=None):
        user = user or self.current_user()
        if not user:
            return ""
        return f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'

    def require_user(self):
        user = self.current_user()
        if not user:
            self.redirect("/login?next=" + quote(self.path))
            return None
        return user

    def require_admin(self):
        user = self.require_user()
        if not user:
            return None
        if not user["is_admin"]:
            self.error_page(HTTPStatus.FORBIDDEN, "관리자 권한이 필요합니다.")
            return None
        return user

    def login_user(self, user_id):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = int(time.time()) + SESSION_SECONDS
        ip_address = self.client_address[0][:64]
        user_agent = self.headers.get("User-Agent", "")[:300]
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
            conn.execute(
                """
                INSERT INTO sessions(token, user_id, csrf, expires_at, created_at, last_seen_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (token, user_id, csrf, expires, now(), now(), ip_address, user_agent),
            )
        self.extra_headers = [("Set-Cookie", session_cookie(token, SESSION_SECONDS))]

    def logout(self, query, form):
        token = self.cookie_value("market_session")
        if token:
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self.extra_headers = [("Set-Cookie", session_cookie("deleted", 0))]
        self.redirect("/")

    def send_html(self, body, status=HTTPStatus.OK):
        user = self.current_user()
        html_doc = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>White Market</title>
  <link rel="icon" type="image/png" href="/static/white-market-icon.png">
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <a class="skip-link" href="#main-content">본문 바로가기</a>
  <header>
    <div class="header-main">
      <a class="brand" href="/">
        <img src="/static/white-market-icon.png" alt="" width="40" height="40">
        <span>White Market</span>
      </a>
      <form class="header-search" method="get" action="/products">
        <input name="q" aria-label="상품 검색" placeholder="상품명이나 키워드를 검색해보세요">
      </form>
      <nav>{self.nav(user)}</nav>
    </div>
    <div class="category-bar">
      <div class="category-inner">
        <div class="category-links">{''.join(f'<a href="/products?category={quote(category)}">{esc(category)}</a>' for category in CATEGORIES)}</div>
      </div>
    </div>
  </header>
  <main id="main-content" tabindex="-1">{body}</main>
</body>
</html>"""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        for key, value in getattr(self, "extra_headers", []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(html_doc.encode("utf-8"))
        self.extra_headers = []

    def nav(self, user):
        if user:
            admin = '<a href="/admin">관리자</a>' if user["is_admin"] else ""
            with db() as conn:
                counts = conn.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM messages WHERE receiver_id = ? AND is_read = 0) AS unread_messages,
                      (SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0) AS unread_notifications
                    """,
                    (user["id"], user["id"]),
                ).fetchone()
            chat_badge = f'<span class="nav-badge">{counts["unread_messages"]}</span>' if counts["unread_messages"] else ""
            notification_badge = f'<span class="nav-badge">{counts["unread_notifications"]}</span>' if counts["unread_notifications"] else ""
            return f"""
            <a href="/users">사용자</a>
            <a href="/chat" class="nav-count-link">채팅{chat_badge}</a>
            <a href="/transactions">거래내역</a>
            <a href="/notifications" class="nav-count-link">알림{notification_badge}</a>
            <a href="/notices">공지사항</a>
            <a href="/mypage">마이페이지</a>
            {admin}
            <form method="post" action="/logout">{self.csrf_input(user)}<button>로그아웃</button></form>
            """
        return '<a href="/notices">공지사항</a><a href="/login">로그인</a><a href="/register">회원가입</a>'

    def redirect(self, location):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_security_headers()
        for key, value in getattr(self, "extra_headers", []):
            self.send_header(key, value)
        self.end_headers()
        self.extra_headers = []

    def send_security_headers(self):
        policy = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        if APP_HTTPS:
            policy += "; upgrade-insecure-requests"
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if APP_HTTPS:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    def error_page(self, status, message):
        self.send_html(f'<section class="panel narrow"><h1>{status.value}</h1><p>{esc(message)}</p><a class="button" href="/">홈으로</a></section>', status)

    def home(self, query, form):
        user = self.current_user()
        account_summary = None
        with db() as conn:
            products = conn.execute(
                f"""
                SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'] if user else 0)}, {engagement_sql('p')}
                FROM products p JOIN users u ON u.id = p.seller_id
                WHERE p.is_deleted = 0 AND p.status != '거래완료' AND u.status = 'active'
                ORDER BY p.id DESC LIMIT 8
                """
            ).fetchall()
            notices = conn.execute("SELECT * FROM notices ORDER BY id DESC LIMIT 3").fetchall()
            if user:
                account_summary = conn.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM products WHERE seller_id = ? AND is_deleted = 0) AS product_count,
                      (SELECT COUNT(*) FROM favorites WHERE user_id = ?) AS favorite_count,
                      (SELECT COUNT(*) FROM transactions WHERE (seller_id = ? OR buyer_id = ?) AND status IN ('거래요청', '예약중')) AS active_trade_count,
                      (SELECT COUNT(*) FROM messages WHERE receiver_id = ? AND is_read = 0) AS received_message_count,
                      (SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0) AS unread_notification_count,
                      COALESCE((SELECT balance FROM wallets WHERE user_id = ?), 0) AS wallet_balance,
                      COALESCE((SELECT AVG(score) FROM user_ratings WHERE reviewee_id = ?), 0) AS rating_average,
                      (SELECT COUNT(*) FROM user_ratings WHERE reviewee_id = ?) AS rating_count
                    """,
                    (user["id"], user["id"], user["id"], user["id"], user["id"], user["id"], user["id"], user["id"], user["id"]),
                ).fetchone()
        card_csrf = self.csrf_input(user) if user else ""
        cards = "".join(product_card(p, viewer=user, csrf=card_csrf, return_to=self.path) for p in products) or "<p>등록된 상품이 없습니다.</p>"
        notice_list = "".join(f'<li><a href="/notices">[{esc(n["created_at"])}] {esc(n["title"])}</a></li>' for n in notices)
        account_box = ""
        if user:
            account_box = f"""
            <section class="panel account-summary">
              <p class="eyebrow">현재 로그인한 사람</p>
              <div class="account-heading">
                <div><h2>{esc(user["display_name"])}</h2><p class="muted">@{esc(user["username"])}</p></div>
                {rating_display(account_summary["rating_average"], account_summary["rating_count"], compact=True)}
              </div>
              <a class="button account-page-link" href="/mypage">마이페이지</a>
              <div class="account-stats">
                <a href="/mypage"><strong>{account_summary["product_count"]}</strong><span>내가 올린 상품</span></a>
                <a href="/mypage"><strong>{account_summary["favorite_count"]}</strong><span>찜한 상품</span></a>
                <a href="/transactions"><strong>{account_summary["active_trade_count"]}</strong><span>진행 중 거래</span></a>
                <a href="/transactions"><strong>{won(account_summary["wallet_balance"])}</strong><span>WM 포인트</span></a>
                <a href="/chat"><strong>{account_summary["received_message_count"]}</strong><span>읽지 않은 채팅</span></a>
                <a href="/notifications"><strong>{account_summary["unread_notification_count"]}</strong><span>새 알림</span></a>
              </div>
            </section>
            """
        body = f"""
        <h1 class="sr-only">White Market 중고거래</h1>
        <section class="education-banner" aria-label="정보보안 교육 프로그램">
          <div class="ad-track">
            <a class="ad-slide whs-ad" href="https://whsedu.kr/" target="_blank" rel="noopener noreferrer" data-ad-slide>
              <div class="ad-copy">
                <img class="ad-logo whs-logo" src="/static/ads/whs-logo.png" alt="WhiteHat School">
                <p>정보보안 입문자를 위한 교육 프로그램</p>
                <h2>보안의 첫걸음,<br>화이트햇 스쿨과 함께</h2>
                <span class="ad-cta">공식 홈페이지 보기</span>
              </div>
              <div class="ad-visual" aria-hidden="true">
                <img class="ad-icon ad-icon-one" src="/static/ads/whs-icon-1.png" alt="">
                <img class="ad-icon ad-icon-two" src="/static/ads/whs-icon-2.png" alt="">
                <img class="ad-icon ad-icon-three" src="/static/ads/whs-icon-3.png" alt="">
              </div>
            </a>
            <a class="ad-slide bob-ad" href="https://bobedu.kr/" target="_blank" rel="noopener noreferrer" data-ad-slide hidden>
              <div class="ad-copy">
                <img class="ad-logo bob-logo" src="/static/ads/bob-logo.png" alt="Best of the Best">
                <p>차세대 정보보안 리더 양성 프로그램</p>
                <h2>최고의 보안 리더를 향한 도전,<br>BoB에서 시작하세요</h2>
                <span class="ad-cta">공식 홈페이지 보기</span>
              </div>
              <div class="ad-visual" aria-hidden="true">
                <img class="ad-icon ad-icon-one" src="/static/ads/bob-icon-1.png" alt="">
                <img class="ad-icon ad-icon-two" src="/static/ads/bob-icon-2.png" alt="">
                <img class="ad-icon ad-icon-three" src="/static/ads/bob-icon-3.png" alt="">
              </div>
            </a>
          </div>
          <div class="ad-controls" aria-label="광고 슬라이드 제어">
            <button type="button" data-ad-prev aria-label="이전 광고">‹</button>
            <span><strong data-ad-current>1</strong> / 2</span>
            <button type="button" data-ad-next aria-label="다음 광고">›</button>
          </div>
        </section>
        <section class="split">
          <div>
            <div class="section-title">
              <h2>최근 상품</h2>
              <div class="actions">
                <a href="/products">전체 보기</a>
                <a class="primary" href="/product/new">상품 등록</a>
              </div>
            </div>
            <div class="grid">{cards}</div>
          </div>
          <aside class="sidebar-stack">
            {account_box}
            <section class="panel">
              <h2>공지사항</h2>
              <ul class="clean">{notice_list or '<li>공지사항이 없습니다.</li>'}</ul>
              {'<p class="muted">로그인하면 찜, 최근 본 상품, 키워드 알림을 사용할 수 있습니다.</p>' if not user else ''}
            </section>
          </aside>
        </section>
        <script src="/static/banner.js" defer></script>
        """
        self.send_html(body)

    def register_page(self, query, form):
        challenge_id = create_registration_captcha()
        username = esc(form.get("username", ""))
        display_name = esc(form.get("display_name", ""))
        phone = esc(form.get("phone", ""))
        location = esc(form.get("location", ""))
        error = esc(query.get("error", ""))
        error_html = f'<p class="form-error" role="alert">{error}</p>' if error else ""
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>회원가입</h1>
              {error_html}
              <form method="post" action="/register" id="register-form">
                <label>아이디
                  <span class="field-row">
                    <input id="username" name="username" value="{username}" required minlength="5" maxlength="20" pattern="(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]+" autocomplete="username" aria-describedby="username-help username-status">
                    <button type="button" id="username-check">중복확인</button>
                  </span>
                  <small id="username-help">영문과 숫자를 모두 포함한 5-20자</small>
                  <span id="username-status" class="field-status" aria-live="polite"></span>
                </label>
                <label>표시 이름<input name="display_name" value="{display_name}" required maxlength="30" autocomplete="nickname"></label>
                <label>휴대전화 번호
                  <input name="phone" value="{phone}" required maxlength="13" inputmode="tel" autocomplete="tel" placeholder="010-1234-5678">
                  <small>계정 잠금 해제와 비밀번호 재설정 본인인증에 사용합니다.</small>
                </label>
                <label>동네
                  <span class="field-row">
                    <input id="location" name="location" value="{location}" required maxlength="30" placeholder="거래할 구를 입력하세요" autocomplete="address-level2" aria-describedby="location-status location-source">
                    <button type="button" id="location-detect">현재 위치</button>
                  </span>
                  <span id="location-status" class="field-status" aria-live="polite"></span>
                  <small id="location-source">버튼을 누르면 위치 권한을 요청하고 구 단위로 자동 입력합니다. <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors</a></small>
                </label>
                <label>비밀번호
                  <input id="password" name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password" aria-describedby="password-help password-confirm-status">
                  <small id="password-help">영문, 숫자, 특수문자를 모두 포함한 8자 이상</small>
                </label>
                <label>비밀번호 확인
                  <input id="password-confirm" name="password_confirm" type="password" required minlength="8" maxlength="128" autocomplete="new-password" aria-describedby="password-confirm-status">
                  <span id="password-confirm-status" class="field-status" aria-live="polite"></span>
                </label>
                <details class="privacy-notice">
                  <summary>개인정보 수집 및 이용 안내</summary>
                  <p>White Market은 회원 식별, 계정 복구와 지역 기반 거래 제공을 위해 아이디, 표시 이름, 휴대전화 번호, 동네 정보를 수집합니다.</p>
                  <p>현재 위치 기능을 선택하면 좌표가 구 단위 주소 확인을 위해 OpenStreetMap Nominatim 서비스로 전송되며, White Market 데이터베이스에는 좌표를 저장하지 않습니다.</p>
                  <p>수집 정보는 회원 탈퇴 시까지 보관하며, 관계 법령에 따라 보존이 필요한 경우에는 해당 기간 동안 별도로 보관합니다.</p>
                  <p>동의를 거부할 수 있으나, 필수 정보 수집에 동의하지 않으면 회원가입을 진행할 수 없습니다.</p>
                </details>
                <label class="check-label"><input type="checkbox" name="privacy_agree" value="yes" required> 개인정보 수집 및 이용에 동의합니다.</label>
                <div class="captcha-box">
                  <div>
                    <strong>자동 가입 방지</strong>
                    <p>이미지에 표시된 계산 결과를 입력하세요.</p>
                  </div>
                  <img src="/captcha.svg?id={esc(challenge_id)}" alt="자동 가입 방지 계산 문제" width="210" height="72">
                  <input type="hidden" name="captcha_id" value="{esc(challenge_id)}">
                  <label>계산 결과<input name="captcha_answer" inputmode="numeric" required maxlength="3" autocomplete="off"></label>
                </div>
                <div class="website-field" aria-hidden="true">
                  <label>웹사이트<input name="website" tabindex="-1" autocomplete="off"></label>
                </div>
                <button class="primary">가입하기</button>
              </form>
            </section>
            <script src="/static/register.js" defer></script>
            """
        )

    def username_check(self, query, form):
        username = query.get("username", "").strip()
        validation_error = username_validation_error(username)
        if validation_error:
            return self.send_json({"available": False, "message": "사용 불가합니다", "reason": validation_error})
        with db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        if exists:
            return self.send_json({"available": False, "message": "사용 불가합니다", "reason": "이미 사용 중인 아이디입니다."})
        return self.send_json({"available": True, "message": "사용 가능합니다", "reason": ""})

    def reverse_location(self, query, form):
        try:
            latitude = float(query.get("lat", ""))
            longitude = float(query.get("lon", ""))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("위치 좌표가 올바르지 않습니다.")
            return self.send_json(reverse_geocode_district(latitude, longitude))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return self.send_json(
                {"error": "동네 검색 서비스에 연결하지 못했습니다. 직접 입력해 주세요."},
                HTTPStatus.BAD_GATEWAY,
            )
        except (TypeError, ValueError) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def captcha_image(self, query, form):
        challenge_id = query.get("id", "")
        with db() as conn:
            row = conn.execute(
                "SELECT question FROM registration_captchas WHERE id = ? AND used = 0 AND expires_at >= ?",
                (challenge_id, int(time.time())),
            ).fetchone()
        if not row:
            return self.error_page(HTTPStatus.NOT_FOUND, "보안문자가 만료되었습니다. 회원가입 페이지를 새로고침하세요.")
        lines = "".join(
            f'<line x1="{secrets.randbelow(210)}" y1="{secrets.randbelow(72)}" x2="{secrets.randbelow(210)}" y2="{secrets.randbelow(72)}" />'
            for _ in range(5)
        )
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="210" height="72" viewBox="0 0 210 72">
<rect width="210" height="72" rx="6" fill="#f4f7f5"/>
<g stroke="#b9cbc1" stroke-width="1">{lines}</g>
<text x="105" y="45" text-anchor="middle" font-family="Arial, sans-serif" font-size="28" font-weight="700" letter-spacing="3" fill="#17201b">{esc(row["question"])}</text>
</svg>""".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(svg)

    def register(self, query, form):
        username = form.get("username", "").strip()
        display_name = form.get("display_name", "").strip()
        phone_input = form.get("phone", "")
        location = form.get("location", "").strip()
        password = form.get("password", "")
        password_confirm = form.get("password_confirm", "")
        try:
            phone = normalize_phone(phone_input)
            validation_error = username_validation_error(username)
            if validation_error:
                raise ValueError(validation_error)
            if not (1 <= len(display_name) <= 30):
                raise ValueError("표시 이름을 입력하세요.")
            if not (1 <= len(location) <= 30):
                raise ValueError("거래할 동네를 입력하세요.")
            validation_error = password_validation_error(password)
            if validation_error:
                raise ValueError(validation_error)
            if password != password_confirm:
                raise ValueError("비밀번호가 일치하지 않습니다. 다시 입력해주세요.")
            if form.get("privacy_agree") != "yes":
                raise ValueError("개인정보 수집 및 이용에 동의해야 합니다.")
            if form.get("website", ""):
                raise ValueError("자동 가입 방지 검증에 실패했습니다.")
            with db() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE",
                    (username,),
                ).fetchone()
            if exists:
                raise ValueError("이미 사용 중인 아이디입니다.")
            if not consume_registration_captcha(form.get("captcha_id", ""), form.get("captcha_answer", "")):
                raise ValueError("자동 가입 방지 답이 올바르지 않거나 만료되었습니다.")

            salt, digest = hash_password(password)
            with db() as conn:
                user_id = conn.execute(
                    """
                    INSERT INTO users(username, password_salt, password_hash, display_name, phone, location, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username, salt, digest, display_name, phone, location, now()),
                ).lastrowid
                conn.execute(
                    "INSERT INTO wallets(user_id, balance, updated_at) VALUES (?, ?, ?)",
                    (user_id, INITIAL_DEMO_BALANCE, now()),
                )
        except sqlite3.IntegrityError:
            return self.register_page({"error": "이미 사용 중인 아이디 또는 휴대전화 번호입니다."}, form)
        except ValueError as exc:
            return self.register_page({"error": str(exc)}, form)
        self.login_user(user_id)
        self.redirect("/")

    def login_page(self, query, form):
        error = esc(query.get("error", ""))
        username = esc(form.get("username", ""))
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>로그인</h1>
              {f'<p class="form-error" role="alert">{error}</p>' if error else ''}
              <form method="post" action="/login">
                {self.csrf_input()}
                <label>아이디<input name="username" value="{username}" required autocomplete="username"></label>
                <label>비밀번호<input name="password" type="password" required autocomplete="current-password"></label>
                <div class="login-options">
                  <a href="/password-reset">비밀번호 찾기</a>
                </div>
                <button class="primary">로그인</button>
              </form>
            </section>
            """
        )

    def conditional_login_page(self, challenge_id, user, dev_code="", error=""):
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>추가 본인인증</h1>
              <p class="muted">로그인 실패가 5회 누적되어 등록된 휴대전화로 본인인증을 진행합니다.</p>
              <p>{esc(mask_phone(user["phone"]))} 번호로 인증번호를 발송했습니다.</p>
              {f'<p class="form-error" role="alert">{esc(error)}</p>' if error else ''}
              {f'<div class="notice"><strong>로컬 개발용 인증번호</strong><p class="dev-code">{esc(dev_code)}</p><small>실서비스에서는 SMS로만 발송됩니다.</small></div>' if dev_code else ''}
              <form method="post" action="/login/verify">
                {self.csrf_input()}
                <input type="hidden" name="challenge_id" value="{esc(challenge_id)}">
                <label>6자리 인증번호<input name="code" required pattern="[0-9]{{6}}" maxlength="6" inputmode="numeric" autocomplete="one-time-code"></label>
                <button class="primary">인증 후 로그인</button>
              </form>
              <p class="login-options"><a href="/login">로그인으로 돌아가기</a></p>
            </section>
            """
        )

    def login(self, query, form):
        username = form.get("username", "").strip()
        password = form.get("password", "")
        ip_address = self.client_address[0][:64]
        user_agent = self.headers.get("User-Agent", "")[:300]
        enforce_rate_limit(
            "login",
            (f"ip:{ip_address}", f"account:{rate_identity(username)}"),
            10,
            15 * 60,
        )
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
            if not user or user["status"] != "active":
                conn.execute(
                    "INSERT INTO login_events(user_id, username_attempt, success, ip_address, user_agent, created_at) VALUES (?, ?, 0, ?, ?, ?)",
                    (user["id"] if user else None, username[:50], ip_address, user_agent, now()),
                )
                return self.login_page({"error": "아이디 또는 비밀번호가 올바르지 않거나 정지된 계정입니다."}, form)
            if not verify_password(password, user["password_salt"], user["password_hash"]):
                failed_count = user["failed_login_count"] + 1
                locked = failed_count >= 5
                conn.execute(
                    "UPDATE users SET failed_login_count = ?, password_reset_required = ? WHERE id = ?",
                    (failed_count, int(locked), user["id"]),
                )
                conn.execute(
                    "INSERT INTO login_events(user_id, username_attempt, success, ip_address, user_agent, created_at) VALUES (?, ?, 0, ?, ?, ?)",
                    (user["id"], username[:50], ip_address, user_agent, now()),
                )
                if locked:
                    return self.password_reset_page(
                        {
                            "locked": "1",
                            "username": username,
                            "phone_hint": mask_phone(user["phone"]),
                        },
                        form,
                    )
                return self.login_page({"error": "아이디 또는 비밀번호가 올바르지 않습니다."}, form)
            if user["password_reset_required"]:
                if not user["phone"]:
                    return self.login_page({"error": "등록된 휴대전화가 없어 잠금을 해제할 수 없습니다. 관리자에게 문의하세요."}, form)
                return self.password_reset_page(
                    {
                        "locked": "1",
                        "username": username,
                        "phone_hint": mask_phone(user["phone"]),
                    },
                    form,
                )
            previous_success = conn.execute(
                "SELECT ip_address, user_agent FROM login_events WHERE user_id = ? AND success = 1 ORDER BY id DESC LIMIT 1",
                (user["id"],),
            ).fetchone()
            suspicious = bool(previous_success and (previous_success["ip_address"] != ip_address or previous_success["user_agent"] != user_agent))
            conn.execute("UPDATE users SET failed_login_count = 0 WHERE id = ?", (user["id"],))
            conn.execute(
                """
                INSERT INTO login_events(user_id, username_attempt, success, suspicious, ip_address, user_agent, created_at)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (user["id"], username[:50], int(suspicious), ip_address, user_agent, now()),
            )
            if suspicious:
                add_notification(conn, user["id"], "security", "새 환경에서 로그인", f"{ip_address}에서 새 로그인이 감지되었습니다.", None, "/security")
        self.login_user(user["id"])
        self.redirect("/password-change-required" if user["must_change_password"] else "/")

    def verify_conditional_login(self, query, form):
        challenge_id = form.get("challenge_id", "")
        code = form.get("code", "").strip()
        ip_address = self.client_address[0][:64]
        user_agent = self.headers.get("User-Agent", "")[:300]
        enforce_rate_limit(
            "login-verification",
            (f"ip:{ip_address}", f"challenge:{rate_identity(challenge_id)}"),
            8,
            15 * 60,
        )
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            challenge = conn.execute(
                """
                SELECT c.*, u.username, u.phone, u.status, u.must_change_password
                FROM login_verification_challenges c
                JOIN users u ON u.id = c.user_id
                WHERE c.id = ?
                """,
                (challenge_id,),
            ).fetchone()
            if (
                not challenge
                or challenge["used"]
                or challenge["expires_at"] < int(time.time())
                or challenge["attempts"] >= 5
                or challenge["status"] != "active"
                or challenge["ip_address"] != ip_address
                or challenge["user_agent"] != user_agent
            ):
                raise ValueError("인증번호가 만료되었거나 더 이상 사용할 수 없습니다.")
            conn.execute(
                "UPDATE login_verification_challenges SET attempts = attempts + 1 WHERE id = ?",
                (challenge_id,),
            )
            if not hmac.compare_digest(challenge["code_hash"], reset_code_digest(challenge_id, code)):
                conn.execute(
                    "INSERT INTO login_events(user_id, username_attempt, success, ip_address, user_agent, created_at) VALUES (?, ?, 0, ?, ?, ?)",
                    (challenge["user_id"], challenge["username"], ip_address, user_agent, now()),
                )
                return self.conditional_login_page(challenge_id, challenge, error="인증번호가 올바르지 않습니다.")

            previous_success = conn.execute(
                "SELECT ip_address, user_agent FROM login_events WHERE user_id = ? AND success = 1 ORDER BY id DESC LIMIT 1",
                (challenge["user_id"],),
            ).fetchone()
            suspicious = bool(previous_success and (previous_success["ip_address"] != ip_address or previous_success["user_agent"] != user_agent))
            conn.execute(
                "UPDATE users SET failed_login_count = 0, password_reset_required = 0 WHERE id = ?",
                (challenge["user_id"],),
            )
            conn.execute("UPDATE login_verification_challenges SET used = 1 WHERE id = ?", (challenge_id,))
            conn.execute(
                "DELETE FROM login_verification_challenges WHERE user_id = ? AND id != ?",
                (challenge["user_id"], challenge_id),
            )
            conn.execute(
                """
                INSERT INTO login_events(user_id, username_attempt, success, suspicious, ip_address, user_agent, created_at)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (challenge["user_id"], challenge["username"], int(suspicious), ip_address, user_agent, now()),
            )
            write_account_log(conn, challenge["user_id"], "조건부 휴대전화 인증 로그인", "로그인 실패 5회 후 본인인증 완료", ip_address)
            add_notification(conn, challenge["user_id"], "security", "추가 본인인증 로그인", "로그인 실패 누적 후 휴대전화 본인인증으로 로그인했습니다.", None, "/security")
        self.login_user(challenge["user_id"])
        self.redirect("/password-change-required" if challenge["must_change_password"] else "/")

    def password_change_required_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        if not user["must_change_password"]:
            return self.redirect("/")
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>초기 비밀번호 변경</h1>
              <div class="notice"><strong>관리자 계정 보호</strong><p>환경 변수로 발급된 초기 비밀번호는 한 번만 사용하고 새 비밀번호로 변경해야 합니다.</p></div>
              <form method="post" action="/password-change-required">
                {self.csrf_input(user)}
                <label>현재 비밀번호<input name="current_password" type="password" required autocomplete="current-password"></label>
                <label>새 비밀번호<input name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
                <label>새 비밀번호 확인<input name="password_confirm" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
                <button class="primary">비밀번호 변경</button>
              </form>
            </section>
            """
        )

    def password_change_required(self, query, form):
        user = self.require_user()
        if not user:
            return
        if not user["must_change_password"]:
            return self.redirect("/")
        current_password = form.get("current_password", "")
        password = form.get("password", "")
        if not verify_password(current_password, user["password_salt"], user["password_hash"]):
            raise ValueError("현재 비밀번호가 올바르지 않습니다.")
        if password != form.get("password_confirm", ""):
            raise ValueError("새 비밀번호 확인이 일치하지 않습니다.")
        validation_error = password_validation_error(password)
        if validation_error:
            raise ValueError(validation_error)
        if hmac.compare_digest(current_password, password):
            raise ValueError("초기 비밀번호와 다른 비밀번호를 사용해주세요.")
        salt, digest = hash_password(password)
        with db() as conn:
            conn.execute(
                "UPDATE users SET password_salt = ?, password_hash = ?, must_change_password = 0 WHERE id = ?",
                (salt, digest, user["id"]),
            )
            write_account_log(conn, user["id"], "초기 관리자 비밀번호 변경", "", self.client_address[0])
        self.redirect("/")

    def password_reset_page(self, query, form):
        username = esc(query.get("username", form.get("username", "")))
        error = esc(query.get("error", ""))
        locked = query.get("locked") == "1"
        phone_hint = esc(query.get("phone_hint", ""))
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>{'계정 잠금 해제' if locked else '비밀번호 재설정'}</h1>
              {f'<div class="notice"><strong>로그인 5회 실패로 계정이 잠겼습니다.</strong><p>{phone_hint}로 등록된 휴대전화 번호를 확인한 뒤 새 비밀번호를 설정하세요.</p></div>' if locked else '<p class="muted">가입할 때 등록한 휴대전화 번호로 본인인증을 진행합니다.</p>'}
              {f'<p class="form-error" role="alert">{error}</p>' if error else ''}
              <form method="post" action="/password-reset/request">
                {self.csrf_input()}
                <label>아이디<input name="username" value="{username}" required autocomplete="username"></label>
                <label>등록 휴대전화 번호<input name="phone" required maxlength="13" inputmode="tel" autocomplete="tel" placeholder="010-1234-5678"></label>
                <button class="primary">{'본인인증 시작' if locked else '인증번호 받기'}</button>
              </form>
              <p class="login-help"><a href="/login">로그인으로 돌아가기</a></p>
            </section>
            """
        )

    def password_reset_request(self, query, form):
        username = form.get("username", "").strip()
        try:
            phone = normalize_phone(form.get("phone", ""))
        except ValueError as exc:
            return self.password_reset_page({"error": str(exc)}, form)
        enforce_rate_limit(
            "password-reset",
            (f"ip:{self.client_address[0]}", f"account:{rate_identity(username + ':' + phone)}"),
            5,
            60 * 60,
        )
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND phone = ? AND status = 'active'",
                (username, phone),
            ).fetchone()
            if not user:
                return self.password_reset_page({"error": "아이디와 등록된 휴대전화 번호가 일치하지 않습니다."}, form)
            latest = conn.execute(
                "SELECT created_at FROM password_reset_challenges WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                (user["id"],),
            ).fetchone()
            current_time = int(time.time())
            if latest and latest["created_at"] > current_time - 60:
                return self.password_reset_page({"error": "인증번호는 1분 후 다시 요청할 수 있습니다."}, form)
            challenge_id = secrets.token_urlsafe(24)
            code = f"{secrets.randbelow(1_000_000):06d}"
            conn.execute(
                """
                INSERT INTO password_reset_challenges(id, user_id, code_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (challenge_id, user["id"], reset_code_digest(challenge_id, code), current_time, current_time + PASSWORD_RESET_SECONDS),
            )
        dev_code = send_sms_verification(phone, code)
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>인증번호 확인</h1>
              <p>{esc(mask_phone(phone))} 번호로 인증번호를 발송했습니다.</p>
              {f'<div class="notice"><strong>로컬 개발용 인증번호</strong><p class="dev-code">{esc(dev_code)}</p><small>실서비스에서는 이 번호를 화면에 노출하지 않고 SMS 공급자를 통해서만 발송해야 합니다.</small></div>' if dev_code else ''}
              <form method="post" action="/password-reset/confirm">
                {self.csrf_input()}
                <input type="hidden" name="challenge_id" value="{esc(challenge_id)}">
                <label>6자리 인증번호<input name="code" required pattern="[0-9]{{6}}" maxlength="6" inputmode="numeric" autocomplete="one-time-code"></label>
                <label>새 비밀번호<input name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
                <label>새 비밀번호 확인<input name="password_confirm" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
                <button class="primary">비밀번호 변경</button>
              </form>
            </section>
            """
        )

    def password_reset_confirm(self, query, form):
        challenge_id = form.get("challenge_id", "")
        code = form.get("code", "").strip()
        password = form.get("password", "")
        if password != form.get("password_confirm", ""):
            raise ValueError("새 비밀번호 확인이 일치하지 않습니다.")
        validation_error = password_validation_error(password)
        if validation_error:
            raise ValueError(validation_error)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            challenge = conn.execute(
                "SELECT * FROM password_reset_challenges WHERE id = ?",
                (challenge_id,),
            ).fetchone()
            if not challenge or challenge["used"] or challenge["expires_at"] < int(time.time()) or challenge["attempts"] >= 5:
                raise ValueError("인증번호가 만료되었거나 더 이상 사용할 수 없습니다.")
            conn.execute("UPDATE password_reset_challenges SET attempts = attempts + 1 WHERE id = ?", (challenge_id,))
            if not hmac.compare_digest(challenge["code_hash"], reset_code_digest(challenge_id, code)):
                raise ValueError("인증번호가 올바르지 않습니다.")
            salt, digest = hash_password(password)
            conn.execute(
                """
                UPDATE users SET password_salt = ?, password_hash = ?, failed_login_count = 0,
                                 password_reset_required = 0
                WHERE id = ?
                """,
                (salt, digest, challenge["user_id"]),
            )
            conn.execute("DELETE FROM login_verification_challenges WHERE user_id = ?", (challenge["user_id"],))
            conn.execute("UPDATE password_reset_challenges SET used = 1 WHERE id = ?", (challenge_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (challenge["user_id"],))
            write_account_log(
                conn,
                challenge["user_id"],
                "휴대전화 본인인증 비밀번호 재설정",
                "비밀번호 변경 및 로그인 실패 누적 초기화",
                self.client_address[0],
            )
        self.send_html('<section class="panel narrow"><h1>비밀번호 변경 완료</h1><p>새 비밀번호로 로그인할 수 있습니다.</p><a class="primary" href="/login">로그인</a></section>')

    def mypage(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            profile_rating = conn.execute(
                f"SELECT {rating_sql('u')} FROM users u WHERE u.id = ?",
                (user["id"],),
            ).fetchone()
            mine = conn.execute(
                f"""
                SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'])}, {engagement_sql('p')}
                FROM products p JOIN users u ON u.id = p.seller_id
                WHERE p.seller_id = ? AND p.is_deleted = 0
                ORDER BY p.id DESC
                """,
                (user["id"],),
            ).fetchall()
            favs = conn.execute(
                f"""
                SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'])}, {engagement_sql('p')}
                FROM favorites f JOIN products p ON p.id = f.product_id JOIN users u ON u.id = p.seller_id
                WHERE f.user_id = ? AND p.is_deleted = 0
                ORDER BY f.created_at DESC
                """,
                (user["id"],),
            ).fetchall()
            recent = conn.execute(
                f"""
                SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'])}, {engagement_sql('p')}
                FROM recent_views r JOIN products p ON p.id = r.product_id JOIN users u ON u.id = p.seller_id
                WHERE r.user_id = ? AND p.is_deleted = 0
                ORDER BY r.viewed_at DESC LIMIT 6
                """,
                (user["id"],),
            ).fetchall()
            alerts = conn.execute("SELECT * FROM keyword_alerts WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
            blocks = conn.execute(
                "SELECT u.* FROM user_blocks b JOIN users u ON u.id = b.blocked_id WHERE b.blocker_id = ?",
                (user["id"],),
            ).fetchall()
            wallet = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user["id"],)).fetchone()
        alert_items = "".join(
            f'<li>{esc(a["keyword"])} <form method="post" action="/alerts/delete" class="inline">{self.csrf_input(user)}<input type="hidden" name="id" value="{a["id"]}"><button>삭제</button></form></li>'
            for a in alerts
        )
        block_items = "".join(f'<li>{esc(b["display_name"])} (@{esc(b["username"])})</li>' for b in blocks)
        saved_notice = '<p class="success-message" role="status">프로필 정보를 저장했습니다.</p>' if query.get("saved") == "1" else ""
        self.send_html(
            f"""
            <section class="panel profile-overview">
              <div class="profile-edit-heading">
                {profile_avatar(user, "large profile-preview")}
                <div>
                  <p class="eyebrow">내 프로필</p>
                  <h1>{esc(user["display_name"])}</h1>
                  <p class="muted">@{esc(user["username"])} · {esc(user["location"])}</p>
                  {rating_display(profile_rating["rating_average"], profile_rating["rating_count"])}
                </div>
              </div>
              <div class="profile-stats" aria-label="내 활동 요약">
                <a href="#my-products"><strong>{len(mine)}</strong><span>등록 상품</span></a>
                <a href="#my-favorites"><strong>{len(favs)}</strong><span>찜한 상품</span></a>
                <a href="#recent-products"><strong>{len(recent)}</strong><span>최근 본 상품</span></a>
              </div>
            </section>
            {saved_notice}
            <section class="profile profile-workspace">
              <div class="panel profile-editor-panel">
                <div class="section-title">
                  <div><p class="eyebrow">프로필 관리</p><h2>내 정보 수정</h2></div>
                </div>
                <div class="profile-edit-heading">
                  {profile_avatar(user, "large profile-preview")}
                  <div><strong>{esc(user["display_name"])}</strong><p class="muted">상대방에게 공개되는 프로필입니다.</p></div>
                </div>
                <form method="post" action="/mypage" id="profile-form">
                  {self.csrf_input(user)}
                  <div class="profile-photo-editor">
                    <label>프로필 사진<input id="profile-image-file" type="file" accept="image/jpeg,image/png,image/webp"></label>
                    <input id="profile-image-data" type="hidden" name="profile_image_data">
                    <small id="profile-image-status">JPG, PNG, WebP 파일을 선택하면 정사각형에 맞춰 안전하게 축소합니다.</small>
                    {f'<label class="check"><input type="checkbox" name="remove_profile_image" value="1"> 현재 프로필 사진 삭제</label>' if user['profile_image_url'] else ''}
                  </div>
                  <label>표시 이름<input name="display_name" value="{esc(user["display_name"])}" maxlength="30" required></label>
                  <label>본인인증 휴대전화 번호<input name="phone" value="{esc(format_phone(user['phone']))}" maxlength="13" inputmode="tel" autocomplete="tel" placeholder="010-1234-5678" required></label>
                  <label>현재 비밀번호<input name="current_password" type="password" maxlength="128" autocomplete="current-password"><small>휴대전화 번호를 등록하거나 변경할 때만 입력하세요.</small></label>
                  <label>동네<input name="location" value="{esc(user["location"])}" maxlength="30" required></label>
                  <label>소개<textarea name="bio" maxlength="500">{esc(user["bio"])}</textarea></label>
                  <button class="primary">저장</button>
                </form>
              </div>
              <aside class="profile-sidebar">
                <section class="panel">
                  <div class="wallet-compact">
                    <span>내 WM 포인트</span>
                    <strong>{won(wallet["balance"] if wallet else 0)}</strong>
                    <small>실제 현금 가치가 없는 교육용 포인트입니다.</small>
                    <a class="button account-page-link" href="/transactions">송금·거래 내역</a>
                  </div>
                </section>
                <section class="panel">
                  <h2>계정 관리</h2>
                  <div class="account-settings-menu">
                    <a href="/password-change"><strong>비밀번호 변경</strong><span>현재 비밀번호를 확인하고 변경합니다.</span></a>
                    <a href="/settings"><strong>알림 설정</strong><span>알림 종류별 수신 여부를 관리합니다.</span></a>
                    <a href="/security"><strong>보안 설정</strong><span>로그인 기기와 접속 기록을 확인합니다.</span></a>
                    <a href="/privacy"><strong>개인정보 설정</strong><span>정보 다운로드와 회원 탈퇴를 관리합니다.</span></a>
                  </div>
                </section>
                <section class="panel">
                  <h2>키워드 알림</h2>
                  <form method="post" action="/alerts" class="inline">
                    {self.csrf_input(user)}
                    <input name="keyword" maxlength="30" placeholder="예: 자전거" required>
                    <button>추가</button>
                  </form>
                  <ul class="compact-list">{alert_items or '<li>등록된 키워드가 없습니다.</li>'}</ul>
                </section>
              </aside>
            </section>
            <section id="my-products"><div class="section-title"><h2>내 상품</h2><a href="/product/new">등록</a></div><div class="grid">{''.join(product_card(p, owner=True, viewer=user, csrf=self.csrf_input(user), return_to=self.path) for p in mine) or '<p>등록한 상품이 없습니다.</p>'}</div></section>
            <section id="my-favorites"><h2>찜한 상품</h2><div class="grid">{''.join(product_card(p, viewer=user, csrf=self.csrf_input(user), return_to=self.path) for p in favs) or '<p>찜한 상품이 없습니다.</p>'}</div></section>
            <section id="recent-products"><h2>최근 본 상품</h2><div class="grid">{''.join(product_card(p, viewer=user, csrf=self.csrf_input(user), return_to=self.path) for p in recent) or '<p>최근 본 상품이 없습니다.</p>'}</div></section>
            <section class="panel"><h2>차단한 사용자</h2><ul>{block_items or '<li>차단한 사용자가 없습니다.</li>'}</ul></section>
            <script src="/static/profile.js" defer></script>
            """
        )

    def update_mypage(self, query, form):
        user = self.require_user()
        if not user:
            return
        display_name = form.get("display_name", "").strip()
        location = form.get("location", "").strip()
        phone = normalize_phone(form.get("phone", ""))
        if not display_name or not location:
            raise ValueError("표시 이름과 동네를 입력하세요.")
        if phone != user["phone"] and not verify_password(form.get("current_password", ""), user["password_salt"], user["password_hash"]):
            raise ValueError("휴대전화 번호를 변경하려면 현재 비밀번호를 정확히 입력하세요.")
        previous_image_url = user["profile_image_url"]
        profile_image_url = "" if form.get("remove_profile_image") == "1" else previous_image_url
        created_image_url = ""
        if form.get("profile_image_data"):
            enforce_rate_limit(
                "image-upload",
                (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
                20,
                10 * 60,
            )
            created_image_url = save_profile_image(form["profile_image_data"])
            profile_image_url = created_image_url
        try:
            with db() as conn:
                conn.execute(
                    "UPDATE users SET display_name = ?, phone = ?, location = ?, bio = ?, profile_image_url = ? WHERE id = ?",
                    (display_name[:30], phone, location[:30], form.get("bio", "")[:500], profile_image_url, user["id"]),
                )
        except sqlite3.IntegrityError:
            delete_profile_image(created_image_url)
            raise ValueError("이미 다른 계정에서 사용하는 휴대전화 번호입니다.")
        except Exception:
            delete_profile_image(created_image_url)
            raise
        if previous_image_url != profile_image_url:
            delete_profile_image(previous_image_url)
        self.redirect("/mypage?saved=1")

    def password_change_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        changed_notice = '<p class="success-message" role="status">비밀번호를 변경하고 다른 기기의 로그인을 종료했습니다.</p>' if query.get("changed") == "1" else ""
        self.send_html(
            f"""
            <section class="panel narrow password-change-panel">
              <div class="section-title"><div><p class="eyebrow">계정 보호</p><h1>비밀번호 변경</h1></div><a class="button" href="/mypage">마이페이지</a></div>
              {changed_notice}
              <p class="muted">영문, 숫자, 특수문자를 섞어 8자 이상으로 입력하세요.</p>
              <form method="post" action="/password-change">
                {self.csrf_input(user)}
                <label>현재 비밀번호<input name="current_password" type="password" required maxlength="128" autocomplete="current-password"></label>
                <label>바꿀 비밀번호<input name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
                <label>바꿀 비밀번호 재입력<input name="password_confirm" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
                <button class="primary">비밀번호 변경</button>
              </form>
            </section>
            """
        )

    def change_password(self, query, form):
        user = self.require_user()
        if not user:
            return
        enforce_rate_limit(
            "password-change",
            (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
            8,
            15 * 60,
        )
        current_password = form.get("current_password", "")
        password = form.get("password", "")
        if not verify_password(current_password, user["password_salt"], user["password_hash"]):
            raise ValueError("현재 비밀번호가 올바르지 않습니다.")
        if password != form.get("password_confirm", ""):
            raise ValueError("바꿀 비밀번호 재입력이 일치하지 않습니다.")
        validation_error = password_validation_error(password)
        if validation_error:
            raise ValueError(validation_error)
        if hmac.compare_digest(current_password, password):
            raise ValueError("현재 비밀번호와 다른 비밀번호를 사용해주세요.")
        salt, digest = hash_password(password)
        with db() as conn:
            conn.execute(
                """
                UPDATE users SET password_salt = ?, password_hash = ?,
                                 failed_login_count = 0, password_reset_required = 0
                WHERE id = ?
                """,
                (salt, digest, user["id"]),
            )
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                (user["id"], user["session_token"]),
            )
            conn.execute("DELETE FROM login_verification_challenges WHERE user_id = ?", (user["id"],))
            write_account_log(conn, user["id"], "비밀번호 변경", "다른 로그인 세션 종료", self.client_address[0])
        self.redirect("/password-change?changed=1")

    def users_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        keyword = query.get("q", "").strip()
        args = [user["id"]]
        where = "u.id != ?"
        if keyword:
            where += " AND (u.username LIKE ? OR u.display_name LIKE ? OR u.location LIKE ?)"
            like = f"%{keyword}%"
            args += [like, like, like]
        with db() as conn:
            users = conn.execute(
                f"SELECT u.*, {rating_sql('u')} FROM users u WHERE {where} ORDER BY rating_average DESC, rating_count DESC, u.display_name",
                args,
            ).fetchall()
        items = "".join(user_card(u) for u in users)
        self.send_html(
            f"""
            <section>
              <div class="toolbar">
                <h1>사용자 조회</h1>
                <form method="get" action="/users" class="inline"><input name="q" value="{esc(keyword)}" placeholder="이름, 아이디, 동네"><button>검색</button></form>
              </div>
              <div class="user-list">{items or '<p>사용자가 없습니다.</p>'}</div>
            </section>
            """
        )

    def user_detail(self, query, form):
        viewer = self.require_user()
        if not viewer:
            return
        user_id = int(query.get("id", "0") or "0")
        with db() as conn:
            profile = conn.execute(
                f"SELECT u.*, {rating_sql('u')} FROM users u WHERE u.id = ?",
                (user_id,),
            ).fetchone()
            products = conn.execute(
                f"SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(viewer['id'])}, {engagement_sql('p')} FROM products p JOIN users u ON u.id = p.seller_id WHERE p.seller_id = ? AND p.is_deleted = 0",
                (user_id,),
            ).fetchall()
            blocked = conn.execute("SELECT 1 FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?", (viewer["id"], user_id)).fetchone()
        if not profile:
            return self.error_page(HTTPStatus.NOT_FOUND, "사용자를 찾을 수 없습니다.")
        block_label = "차단 해제" if blocked else "차단"
        body = f"""
        <section class="profile">
          <div class="panel">
            <div class="public-profile-heading">
              {profile_avatar(profile, "xlarge")}
              <div><h1>{esc(profile["display_name"])}</h1><p>@{esc(profile["username"])} · {esc(profile["location"])}</p></div>
            </div>
            {rating_display(profile["rating_average"], profile["rating_count"])}
            <p>{esc(profile["bio"]) or '소개글이 없습니다.'}</p>
            {'' if viewer["id"] == profile["id"] else f'<div class="actions"><a class="primary" href="/chat?user={profile["id"]}">채팅하기</a><form method="post" action="/block" class="inline">{self.csrf_input(viewer)}<input type="hidden" name="id" value="{profile["id"]}"><button>{block_label}</button></form><a class="button danger" href="/report?type=user&id={profile["id"]}">신고</a></div>'}
          </div>
          <div><h2>판매 상품</h2><div class="grid">{''.join(product_card(p, viewer=viewer, csrf=self.csrf_input(viewer), return_to=self.path) for p in products) or '<p>판매 중인 상품이 없습니다.</p>'}</div></div>
        </section>
        """
        self.send_html(body)

    def block_user(self, query, form):
        user = self.require_user()
        if not user:
            return
        blocked_id = int(form.get("id", "0") or "0")
        if blocked_id == user["id"]:
            raise ValueError("자기 자신은 차단할 수 없습니다.")
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], blocked_id)).fetchone()
            if exists:
                conn.execute("DELETE FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], blocked_id))
            else:
                conn.execute("INSERT INTO user_blocks(blocker_id, blocked_id, created_at) VALUES (?, ?, ?)", (user["id"], blocked_id, now()))
        self.redirect(f"/user?id={blocked_id}")

    def report_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        target_type = query.get("type", form.get("target_type", ""))
        target_id = int(query.get("id", form.get("target_id", "0")) or "0")
        with db() as conn:
            if target_type == "user":
                target = conn.execute("SELECT id, username, display_name FROM users WHERE id = ?", (target_id,)).fetchone()
                target_label = f'{target["display_name"]} (@{target["username"]})' if target else ""
            elif target_type == "product":
                target = conn.execute("SELECT id, title FROM products WHERE id = ? AND is_deleted = 0", (target_id,)).fetchone()
                target_label = target["title"] if target else ""
            else:
                target = None
                target_label = ""
        if not target:
            return self.error_page(HTTPStatus.NOT_FOUND, "신고 대상을 찾을 수 없습니다.")
        if query.get("submitted"):
            return self.send_html('<section class="panel narrow"><h1>신고 접수 완료</h1><p>관리자가 내용을 확인한 뒤 처리 상태와 기록을 보관합니다.</p><a class="button" href="/">홈으로</a></section>')
        reasons = ["사기 의심", "노쇼", "부적절한 콘텐츠", "욕설 또는 괴롭힘", "허위 정보", "기타"]
        selected_reason = query.get("reason", "")
        options = "".join(f'<option value="{esc(reason)}" {"selected" if reason == selected_reason else ""}>{esc(reason)}</option>' for reason in reasons)
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>신고하기</h1>
              <p><strong>{esc(target_label)}</strong></p>
              <form method="post" action="/report">
                {self.csrf_input(user)}
                <input type="hidden" name="target_type" value="{esc(target_type)}">
                <input type="hidden" name="target_id" value="{target_id}">
                <label>신고 사유<select name="reason" required>{options}</select></label>
                <label>상세 내용<textarea name="details" maxlength="1000" required></textarea></label>
                <div class="actions"><button class="danger">신고 접수</button><a class="button" href="{'/user?id=' + str(target_id) if target_type == 'user' else '/product?id=' + str(target_id)}">취소</a></div>
              </form>
            </section>
            """
        )

    def create_report(self, query, form):
        user = self.require_user()
        if not user:
            return
        enforce_rate_limit(
            "report",
            (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
            10,
            60 * 60,
        )
        target_type = form.get("target_type", "")
        target_id = int(form.get("target_id", "0") or "0")
        reason = form.get("reason", "").strip()
        details = form.get("details", "").strip()
        allowed_reasons = {"사기 의심", "노쇼", "부적절한 콘텐츠", "욕설 또는 괴롭힘", "허위 정보", "기타"}
        if reason not in allowed_reasons or not (5 <= len(details) <= 1000):
            raise ValueError("신고 사유와 상세 내용을 확인하세요.")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if target_type == "user":
                target = conn.execute("SELECT id FROM users WHERE id = ?", (target_id,)).fetchone()
                if not target or target_id == user["id"]:
                    raise ValueError("해당 사용자는 신고할 수 없습니다.")
            elif target_type == "product":
                target = conn.execute("SELECT seller_id FROM products WHERE id = ? AND is_deleted = 0", (target_id,)).fetchone()
                if not target or target["seller_id"] == user["id"]:
                    raise ValueError("해당 상품은 신고할 수 없습니다.")
            else:
                raise ValueError("신고 대상이 올바르지 않습니다.")
            duplicate = conn.execute(
                "SELECT 1 FROM reports WHERE reporter_id = ? AND target_type = ? AND target_id = ? AND status IN ('접수', '검토중')",
                (user["id"], target_type, target_id),
            ).fetchone()
            if duplicate:
                raise ValueError("이미 처리 중인 신고가 있습니다.")
            report_id = conn.execute(
                """
                INSERT INTO reports(reporter_id, target_type, target_id, reason, details, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], target_type, target_id, reason, details, now(), now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO report_history(report_id, actor_id, from_status, to_status, note, created_at) VALUES (?, ?, '', '접수', ?, ?)",
                (report_id, user["id"], "사용자 신고 접수", now()),
            )
        self.redirect(f"/report?submitted=1&type={target_type}&id={target_id}")

    def products_page(self, query, form):
        user = self.current_user()
        search_identities = [f"ip:{self.client_address[0]}"]
        if user:
            search_identities.append(f"user:{user['id']}")
        enforce_rate_limit("product-search", search_identities, 120, 60)
        keyword = query.get("q", "").strip()[:80]
        selected_categories = [category for index, category in enumerate(CATEGORIES) if query.get(f"cat_{index}") == "1"]
        legacy_category = query.get("category", "").strip()
        if legacy_category in CATEGORIES and legacy_category not in selected_categories:
            selected_categories.append(legacy_category)
        sort = query.get("sort", "latest")
        status_filter = query.get("status", "selling")
        try:
            min_price = max(0, int(query.get("min_price", "0") or "0"))
            max_price = min(100_000_000, int(query.get("max_price", "100000000") or "100000000"))
            max_distance = min(999.0, max(0.0, float(query.get("max_distance", "999") or "999")))
            page = max(1, int(query.get("page", "1") or "1"))
        except ValueError:
            raise ValueError("가격, 거리, 페이지 값을 확인해주세요.")
        if min_price > max_price:
            raise ValueError("최소 가격은 최대 가격보다 클 수 없습니다.")
        page_size = 12
        where = ["p.is_deleted = 0", "u.status = 'active'"]
        args = [min_price, max_price, max_distance]
        where += ["p.price BETWEEN ? AND ?", "p.distance_km <= ?"]
        if keyword:
            where.append("(p.title LIKE ? OR p.description LIKE ?)")
            args += [f"%{keyword}%", f"%{keyword}%"]
        if selected_categories:
            where.append(f"p.category IN ({','.join('?' for _ in selected_categories)})")
            args.extend(selected_categories)
        if status_filter == "selling":
            where.append("p.status = '판매중'")
        elif status_filter == "available":
            where.append("p.status != '거래완료'")
        elif status_filter != "all":
            status_filter = "selling"
            where.append("p.status = '판매중'")
        order = {
            "price_asc": "p.price ASC",
            "price_desc": "p.price DESC",
            "distance": "p.distance_km ASC",
        }.get(sort, "p.id DESC")
        with db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS count FROM products p JOIN users u ON u.id = p.seller_id WHERE {' AND '.join(where)}",
                args,
            ).fetchone()["count"]
            products = conn.execute(
                f"""
                SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'] if user else 0)}, {engagement_sql('p')}
                FROM products p JOIN users u ON u.id = p.seller_id
                WHERE {' AND '.join(where)}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
            popular_terms = conn.execute(
                """
                SELECT query_text, COUNT(*) AS search_count
                FROM search_history
                WHERE query_text != '' AND created_at >= ?
                GROUP BY lower(query_text)
                ORDER BY search_count DESC, MAX(id) DESC LIMIT 8
                """,
                ((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M"),),
            ).fetchall()
            my_searches = []
            alert_hits = []
            if user:
                filters_payload = {
                    "categories": selected_categories,
                    "min_price": min_price,
                    "max_price": max_price,
                    "max_distance": max_distance,
                    "status": status_filter,
                    "sort": sort,
                }
                if keyword or selected_categories or min_price or max_price < 100_000_000 or max_distance < 999:
                    conn.execute(
                        "INSERT INTO search_history(user_id, query_text, filters_json, created_at) VALUES (?, ?, ?, ?)",
                        (user["id"], keyword, json.dumps(filters_payload, ensure_ascii=False), now()),
                    )
                my_searches = conn.execute(
                    """
                    SELECT query_text, MAX(created_at) AS created_at FROM search_history
                    WHERE user_id = ? AND query_text != ''
                    GROUP BY lower(query_text) ORDER BY MAX(id) DESC LIMIT 5
                    """,
                    (user["id"],),
                ).fetchall()
                alert_hits = conn.execute(
                    """
                    SELECT DISTINCT p.title, a.keyword
                    FROM keyword_alerts a
                    JOIN products p ON p.title LIKE '%' || a.keyword || '%' OR p.description LIKE '%' || a.keyword || '%'
                    WHERE a.user_id = ? AND p.is_deleted = 0
                    ORDER BY p.id DESC LIMIT 5
                    """,
                    (user["id"],),
                ).fetchall()
        category_checks = "".join(
            f'<label class="filter-check"><input type="checkbox" name="cat_{index}" value="1" {"checked" if category in selected_categories else ""}> {esc(category)}</label>'
            for index, category in enumerate(CATEGORIES)
        )
        alert_box = ""
        if alert_hits:
            alert_box = '<div class="notice">키워드 알림: ' + ", ".join(f'{esc(h["keyword"])} → {esc(h["title"])}' for h in alert_hits) + "</div>"
        pages = max(1, (total + page_size - 1) // page_size)
        base_query = {
            key: value
            for key, value in query.items()
            if key != "page" and value != ""
        }
        pagination = "".join(
            f'<a class="{"current" if number == page else ""}" href="/products?{urlencode({**base_query, "page": number})}">{number}</a>'
            for number in range(max(1, page - 2), min(pages, page + 2) + 1)
        )
        popular_links = " ".join(f'<a href="/products?q={quote(row["query_text"])}">{esc(row["query_text"])}</a>' for row in popular_terms)
        recent_links = " ".join(f'<a href="/products?q={quote(row["query_text"])}">{esc(row["query_text"])}</a>' for row in my_searches)
        self.send_html(
            f"""
            <section>
              <div class="toolbar">
                <h1>상품 검색</h1>
                <a class="primary" href="/product/new">상품 등록</a>
              </div>
              <form method="get" action="/products" class="filters">
                <input name="q" value="{esc(keyword)}" placeholder="검색어">
                <fieldset class="category-filter"><legend>카테고리</legend>{category_checks}</fieldset>
                <label>최소 가격<input name="min_price" type="number" min="0" max="100000000" value="{min_price or ''}" placeholder="0"></label>
                <label>최대 가격<input name="max_price" type="number" min="0" max="100000000" value="{'' if max_price == 100000000 else max_price}" placeholder="제한 없음"></label>
                <label>최대 거리(km)<input name="max_distance" type="number" min="0" max="999" step="0.1" value="{'' if max_distance == 999 else max_distance}" placeholder="제한 없음"></label>
                <select name="sort">
                  {option('latest', '최신순', sort)}
                  {option('price_asc', '낮은 가격순', sort)}
                  {option('price_desc', '높은 가격순', sort)}
                  {option('distance', '가까운 거리순', sort)}
                </select>
                <select name="status">
                  {option('selling', '판매중 상품만', status_filter)}
                  {option('available', '거래완료 숨기기', status_filter)}
                  {option('all', '전체 거래 상태', status_filter)}
                </select>
                <button>검색</button>
              </form>
              <div class="search-suggestions">{f'<span><strong>인기 검색어</strong> {popular_links}</span>' if popular_links else ''}{f'<span><strong>최근 검색</strong> {recent_links}</span>' if recent_links else ''}</div>
              {alert_box}
              <div class="grid">{''.join(product_card(p, viewer=user, csrf=self.csrf_input(user) if user else '', return_to=self.path) for p in products) or '<p>상품이 없습니다.</p>'}</div>
              <nav class="pagination" aria-label="상품 목록 페이지">{pagination}</nav>
            </section>
            """
        )

    def product_form(self, query, form):
        user = self.require_user()
        if not user:
            return
        product = None
        is_edit = self.path.startswith("/product/edit")
        if is_edit:
            product_id = int(query.get("id", "0") or "0")
            with db() as conn:
                product = conn.execute("SELECT * FROM products WHERE id = ? AND seller_id = ? AND is_deleted = 0", (product_id, user["id"])).fetchone()
                images = conn.execute(
                    "SELECT * FROM product_images WHERE product_id = ? ORDER BY position, id",
                    (product_id,),
                ).fetchall()
            if not product:
                return self.error_page(HTTPStatus.NOT_FOUND, "수정할 상품을 찾을 수 없습니다.")
        else:
            images = []
        action = "/product/edit" if is_edit else "/product/new"
        hidden = f'<input type="hidden" name="id" value="{product["id"]}">' if product else ""
        category_options = "".join(f'<option value="{c}" {"selected" if product and product["category"] == c else ""}>{c}</option>' for c in CATEGORIES)
        status_note = f'<p class="notice">현재 거래 상태: <strong>{esc(product["status"])}</strong><br><small>판매자는 구매자와 채팅한 뒤 거래 상태를 변경할 수 있습니다.</small></p>' if product else ""
        image_items = "".join(
            f"""
            <li class="product-image-item" data-image-id="{image["id"]}" data-image-url="{esc(image["image_url"])}" data-primary="{int(image["is_primary"])}">
              <img src="{esc(image["image_url"])}" alt="등록된 상품 사진">
            </li>
            """
            for image in images
        )
        initial_manifest = json.dumps(
            [
                {
                    "kind": "existing",
                    "id": image["id"],
                    "primary": bool(image["is_primary"]),
                }
                for image in images
            ],
            ensure_ascii=False,
        )
        self.send_html(
            f"""
            <section class="panel product-form-panel">
              <h1>{'상품 수정' if is_edit else '상품 등록'}</h1>
              <form method="post" action="{action}" id="product-form">
                {self.csrf_input(user)}
                {hidden}
                <label>상품명<input name="title" value="{esc(product["title"] if product else "")}" maxlength="80" required></label>
                <label>가격<input name="price" type="number" min="0" max="100000000" value="{esc(product["price"] if product else "10000")}" required></label>
                <label>거리(km)<input name="distance_km" type="number" min="0" max="999" step="0.1" value="{esc(product["distance_km"] if product else "1.0")}" required></label>
                <label>카테고리<select name="category">{category_options}</select></label>
                {status_note}
                <label>설명<textarea name="description" maxlength="1000" required>{esc(product["description"] if product else "")}</textarea></label>
                <section class="product-image-editor">
                  <div class="section-title"><div><h2>상품 사진</h2><small>최대 {MAX_PRODUCT_IMAGES}장 · 첫 대표 사진이 목록에 표시됩니다.</small></div><span id="product-image-count">0 / {MAX_PRODUCT_IMAGES}</span></div>
                  <label class="image-add-button">사진 추가<input id="product-image-files" type="file" accept="image/jpeg,image/png,image/webp" multiple></label>
                  <input type="hidden" id="image-manifest" name="image_manifest" value="{esc(initial_manifest)}">
                  <p id="product-image-status" class="field-status" role="status"></p>
                  <ul id="product-image-list" class="product-image-list">{image_items}</ul>
                </section>
                <button class="primary">저장</button>
              </form>
            </section>
            <script src="/static/product-form.js" defer></script>
            """
        )

    def create_product(self, query, form):
        user = self.require_user()
        if not user:
            return
        data = product_data(form)
        manifest = product_image_manifest(form)
        if manifest:
            enforce_rate_limit(
                "image-upload",
                (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
                20,
                10 * 60,
            )
        created_urls = []
        try:
            image_rows = []
            for item in manifest:
                image_url = save_product_image(item["data"])
                thumbnail_url = save_product_image(item["thumbnail"])
                created_urls.extend([image_url, thumbnail_url])
                image_rows.append((image_url, thumbnail_url, bool(item.get("primary"))))
            with db() as conn:
                product_id = conn.execute(
                    """
                    INSERT INTO products(seller_id, title, description, category, price, distance_km, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, '판매중', ?, ?)
                    """,
                    (user["id"], *data, now(), now()),
                ).lastrowid
                primary_index = next((index for index, row in enumerate(image_rows) if row[2]), 0)
                for position, (image_url, thumbnail_url, _) in enumerate(image_rows):
                    conn.execute(
                        "INSERT INTO product_images(product_id, image_url, thumbnail_url, position, is_primary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (product_id, image_url, thumbnail_url, position, int(position == primary_index), now()),
                    )
                cover_url = image_rows[primary_index][0] if image_rows else ""
                cover_thumbnail = image_rows[primary_index][1] if image_rows else ""
                conn.execute("UPDATE products SET image_url = ?, thumbnail_url = ? WHERE id = ?", (cover_url, cover_thumbnail, product_id))
        except Exception:
            for image_url in created_urls:
                delete_product_image(image_url)
            raise
        self.redirect(f"/product?id={product_id}")

    def update_product(self, query, form):
        user = self.require_user()
        if not user:
            return
        product_id = int(form.get("id", "0") or "0")
        data = product_data(form)
        manifest = product_image_manifest(form)
        if any(item["kind"] == "new" for item in manifest):
            enforce_rate_limit(
                "image-upload",
                (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
                20,
                10 * 60,
            )
        created_urls = []
        removed_urls = []
        try:
            with db() as conn:
                existing_images = conn.execute(
                    """
                    SELECT pi.* FROM product_images pi JOIN products p ON p.id = pi.product_id
                    WHERE pi.product_id = ? AND p.seller_id = ? AND p.is_deleted = 0
                    """,
                    (product_id, user["id"]),
                ).fetchall()
                existing_by_id = {str(row["id"]): row for row in existing_images}
                image_rows = []
                for item in manifest:
                    if item["kind"] == "existing":
                        existing = existing_by_id.get(str(item["id"]))
                        if not existing:
                            raise ValueError("상품 사진 정보를 확인할 수 없습니다.")
                        image_rows.append((existing["image_url"], existing["thumbnail_url"], bool(item.get("primary"))))
                    else:
                        image_url = save_product_image(item["data"])
                        thumbnail_url = save_product_image(item["thumbnail"])
                        created_urls.extend([image_url, thumbnail_url])
                        image_rows.append((image_url, thumbnail_url, bool(item.get("primary"))))
                kept_urls = {row[0] for row in image_rows}
                removed_urls = [row["image_url"] for row in existing_images if row["image_url"] not in kept_urls]
                kept_thumbnails = {row[1] for row in image_rows}
                removed_urls.extend(row["thumbnail_url"] for row in existing_images if row["thumbnail_url"] and row["thumbnail_url"] not in kept_thumbnails)

                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute(
                    "SELECT title, price FROM products WHERE id = ? AND seller_id = ? AND is_deleted = 0",
                    (product_id, user["id"]),
                ).fetchone()
                if not previous:
                    raise ValueError("수정할 상품을 찾을 수 없습니다.")
                conn.execute(
                    """
                    UPDATE products
                    SET title = ?, description = ?, category = ?, price = ?, distance_km = ?, updated_at = ?
                    WHERE id = ? AND seller_id = ? AND is_deleted = 0
                    """,
                    (*data, now(), product_id, user["id"]),
                )
                conn.execute("DELETE FROM product_images WHERE product_id = ?", (product_id,))
                primary_index = next((index for index, row in enumerate(image_rows) if row[2]), 0)
                for position, (image_url, thumbnail_url, _) in enumerate(image_rows):
                    conn.execute(
                        "INSERT INTO product_images(product_id, image_url, thumbnail_url, position, is_primary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (product_id, image_url, thumbnail_url, position, int(position == primary_index), now()),
                    )
                cover_url = image_rows[primary_index][0] if image_rows else ""
                cover_thumbnail = image_rows[primary_index][1] if image_rows else ""
                conn.execute("UPDATE products SET image_url = ?, thumbnail_url = ? WHERE id = ?", (cover_url, cover_thumbnail, product_id))
                new_price = data[3]
                if new_price < previous["price"]:
                    body = f'{data[0]} 가격이 {won(previous["price"])}에서 {won(new_price)}으로 내려갔습니다.'
                    favorite_users = conn.execute(
                        "SELECT user_id FROM favorites WHERE product_id = ? AND user_id != ?",
                        (product_id, user["id"]),
                    ).fetchall()
                    for favorite_user in favorite_users:
                        add_notification(
                            conn,
                            favorite_user["user_id"],
                            "price_drop",
                            "찜한 상품 가격 인하",
                            body,
                            product_id,
                            f"/product?id={product_id}",
                        )
        except Exception:
            for image_url in created_urls:
                delete_product_image(image_url)
            raise
        for image_url in removed_urls:
            delete_product_image(image_url)
        self.redirect(f"/product?id={product_id}")

    def delete_product(self, query, form):
        user = self.require_user()
        if not user:
            return
        product_id = int(form.get("id", "0") or "0")
        with db() as conn:
            conn.execute("UPDATE products SET is_deleted = 1 WHERE id = ? AND seller_id = ?", (product_id, user["id"]))
        self.redirect("/mypage")

    def product_detail(self, query, form):
        product_id = int(query.get("id", "0") or "0")
        user = self.current_user()
        with db() as conn:
            if user:
                viewer_key = f'user:{user["id"]}'
            else:
                fingerprint = f'{self.client_address[0]}|{self.headers.get("User-Agent", "")}'
                viewer_key = "anon:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
            view_result = conn.execute(
                """
                INSERT OR IGNORE INTO product_views(product_id, viewer_key, viewed_on)
                SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM products WHERE id = ? AND is_deleted = 0)
                """,
                (product_id, viewer_key, datetime.now().strftime("%Y-%m-%d"), product_id),
            )
            if view_result.rowcount:
                conn.execute("UPDATE products SET view_count = view_count + 1 WHERE id = ?", (product_id,))
            p = conn.execute(
                f"""
                SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {engagement_sql('p')}
                FROM products p JOIN users u ON u.id = p.seller_id
                WHERE p.id = ? AND p.is_deleted = 0
                """,
                (product_id,),
            ).fetchone()
            images = conn.execute(
                "SELECT * FROM product_images WHERE product_id = ? ORDER BY position, id",
                (product_id,),
            ).fetchall()
            seller_products = []
            related_products = []
            if p:
                seller_products = conn.execute(
                    f"""
                    SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'] if user else 0)}, {engagement_sql('p')}
                    FROM products p JOIN users u ON u.id = p.seller_id
                    WHERE p.seller_id = ? AND p.id != ? AND p.is_deleted = 0
                    ORDER BY p.id DESC LIMIT 4
                    """,
                    (p["seller_id"], product_id),
                ).fetchall()
                related_products = conn.execute(
                    f"""
                    SELECT p.*, u.display_name, u.username, u.location, {rating_sql('u')}, {favorite_sql(user['id'] if user else 0)}, {engagement_sql('p')}
                    FROM products p JOIN users u ON u.id = p.seller_id
                    WHERE p.category = ? AND p.id != ? AND p.seller_id != ? AND p.is_deleted = 0
                    ORDER BY p.id DESC LIMIT 4
                    """,
                    (p["category"], product_id, p["seller_id"]),
                ).fetchall()
            favorite = None
            active_transaction = None
            if user:
                conn.execute("INSERT OR REPLACE INTO recent_views(user_id, product_id, viewed_at) VALUES (?, ?, ?)", (user["id"], product_id, now()))
                favorite = conn.execute("SELECT 1 FROM favorites WHERE user_id = ? AND product_id = ?", (user["id"], product_id)).fetchone()
                active_transaction = conn.execute(
                    """
                    SELECT id, status FROM transactions
                    WHERE product_id = ? AND buyer_id = ? AND status IN ('거래요청', '예약중')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (product_id, user["id"]),
                ).fetchone()
        if not p:
            return self.error_page(HTTPStatus.NOT_FOUND, "상품을 찾을 수 없습니다.")
        if not images and p["image_url"]:
            images = [{"image_url": p["image_url"], "is_primary": 1}]
        gallery_slides = "".join(
            f'<figure class="gallery-slide" {"hidden" if index else ""}><img src="{esc(image["image_url"])}" alt="{esc(p["title"])} 상품 사진 {index + 1}" {"loading=\"lazy\"" if index else ""}></figure>'
            for index, image in enumerate(images)
        )
        gallery_thumbnails = "".join(
            f'<button type="button" class="gallery-thumb {"selected" if index == 0 else ""}" data-gallery-index="{index}" aria-label="{index + 1}번 사진 보기"><img src="{esc(image["image_url"])}" alt=""></button>'
            for index, image in enumerate(images)
        )
        gallery = f"""
        <div class="product-gallery" data-gallery>
          <div class="gallery-stage">
            {gallery_slides or f'<div class="product-photo">{product_image(p, eager=True)}</div>'}
            {f'<button type="button" class="gallery-nav previous" data-gallery-prev aria-label="이전 사진">‹</button><button type="button" class="gallery-nav next" data-gallery-next aria-label="다음 사진">›</button><span class="gallery-counter"><strong data-gallery-current>1</strong> / {len(images)}</span>' if len(images) > 1 else ''}
          </div>
          {f'<div class="gallery-thumbnails">{gallery_thumbnails}</div>' if len(images) > 1 else ''}
        </div>
        """
        owner_tools = ""
        buyer_tools = ""
        if user and user["id"] == p["seller_id"]:
            owner_tools = f"""
            <a class="button" href="/product/edit?id={p["id"]}">수정</a>
            <form method="post" action="/product/delete" class="inline">{self.csrf_input(user)}<input type="hidden" name="id" value="{p["id"]}"><button>삭제</button></form>
            """
        elif user:
            if active_transaction:
                transaction_action = f'<a class="button" href="/transactions?focus={active_transaction["id"]}">{esc(active_transaction["status"])} 확인</a>'
            elif p["status"] == "판매중":
                transaction_action = ""
            else:
                transaction_action = f'<span class="status-badge">현재 {esc(p["status"])}</span>'
            buyer_tools = f"""
            <form method="post" action="/favorite" class="inline">{self.csrf_input(user)}<input type="hidden" name="id" value="{p["id"]}"><button>{'찜 취소' if favorite else '찜하기'}</button></form>
            <a class="button" href="/chat?user={p["seller_id"]}&product={p["id"]}">판매자와 채팅</a>
            {transaction_action}
            <a class="button danger" href="/report?type=product&id={p["id"]}">신고</a>
            """
        self.send_html(
            f"""
            <section class="detail">
              {gallery}
              <div class="product-info">
                <p class="eyebrow">{esc(p["category"])} · {esc(p["status"])} · {p["distance_km"]:.1f}km · <span title="등록일 {esc(p['created_at'])}">{relative_time(p["created_at"])}</span></p>
                <h1>{esc(p["title"])}</h1>
                <p class="price">{won(p["price"])}</p>
                <p class="product-detail-stats">관심 {p["favorite_count"]} · 채팅 {p["chat_count"]} · 조회 {p["view_count"]}</p>
                <p class="product-description">{esc(p["description"])}</p>
                <div class="seller-profile-line"><a href="/user?id={p["seller_id"]}">@{esc(p["username"])}</a>{rating_display(p["rating_average"], p["rating_count"])}<span>{esc(p["location"])}</span></div>
                <div class="actions">{owner_tools}{buyer_tools}<button type="button" id="share-product">공유하기</button><span id="share-status" class="muted" role="status"></span></div>
              </div>
            </section>
            <section><div class="section-title"><h2>@{esc(p["username"])}님의 다른 상품</h2></div><div class="grid">{''.join(product_card(item, viewer=user, csrf=self.csrf_input(user) if user else '', return_to=self.path) for item in seller_products) or '<p>다른 판매 상품이 없습니다.</p>'}</div></section>
            <section><div class="section-title"><h2>같은 카테고리 상품</h2></div><div class="grid">{''.join(product_card(item, viewer=user, csrf=self.csrf_input(user) if user else '', return_to=self.path) for item in related_products) or '<p>관련 상품이 없습니다.</p>'}</div></section>
            <script src="/static/product.js" defer></script>
            """
        )

    def toggle_favorite(self, query, form):
        user = self.require_user()
        if not user:
            return
        product_id = int(form.get("id", "0") or "0")
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM favorites WHERE user_id = ? AND product_id = ?", (user["id"], product_id)).fetchone()
            if exists:
                conn.execute("DELETE FROM favorites WHERE user_id = ? AND product_id = ?", (user["id"], product_id))
            else:
                conn.execute("INSERT INTO favorites(user_id, product_id, created_at) VALUES (?, ?, ?)", (user["id"], product_id, now()))
        return_to = form.get("next", "")
        if not return_to.startswith("/") or return_to.startswith("//"):
            return_to = f"/product?id={product_id}"
        self.redirect(return_to)

    def add_alert(self, query, form):
        user = self.require_user()
        if not user:
            return
        keyword = form.get("keyword", "").strip()[:30]
        if not keyword:
            raise ValueError("키워드를 입력하세요.")
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO keyword_alerts(user_id, keyword, created_at) VALUES (?, ?, ?)", (user["id"], keyword, now()))
        self.redirect("/mypage")

    def delete_alert(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            conn.execute("DELETE FROM keyword_alerts WHERE id = ? AND user_id = ?", (int(form.get("id", "0") or "0"), user["id"]))
        self.redirect("/mypage")

    def notifications_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            notifications = conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100",
                (user["id"],),
            ).fetchall()
        items = "".join(
            f"""
            <article class="notification {'unread' if not item['is_read'] else ''}">
              <div><strong>{esc(item['title'])}</strong><p>{esc(item['body'])}</p><small>{esc(item['created_at'])}</small></div>
              {f'<a class="button" href="/notification/open?id={item["id"]}">관련 내용 보기</a>' if item['link_url'] or item['product_id'] else ''}
            </article>
            """
            for item in notifications
        )
        self.send_html(
            f"""
            <section class="panel">
              <div class="section-title"><h1>알림</h1><form method="post" action="/notifications/read" class="inline">{self.csrf_input(user)}<button>모두 읽음</button></form></div>
              <div class="notification-list">{items or '<p>새 알림이 없습니다.</p>'}</div>
            </section>
            """
        )

    def open_notification(self, query, form):
        user = self.require_user()
        if not user:
            return
        notification_id = int(query.get("id", "0") or "0")
        with db() as conn:
            notification = conn.execute(
                "SELECT * FROM notifications WHERE id = ? AND user_id = ?",
                (notification_id, user["id"]),
            ).fetchone()
            if not notification:
                return self.error_page(HTTPStatus.NOT_FOUND, "알림을 찾을 수 없습니다.")
            conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
        destination = notification["link_url"]
        if not destination and notification["product_id"]:
            destination = f"/product?id={notification['product_id']}"
        if not destination.startswith("/") or destination.startswith("//"):
            destination = "/notifications"
        self.redirect(destination)

    def read_notifications(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user["id"],))
        self.redirect("/notifications")

    def settings_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        notification_options = [
            ("notify_chat", "채팅·가격 제안", "새 메시지와 채팅 안의 가격 제안을 알려드립니다."),
            ("notify_price", "찜 상품 가격 인하", "찜한 상품의 가격이 내려가면 알려드립니다."),
            ("notify_transaction", "거래 상태", "예약중, 판매중 전환과 거래 완료 상태를 알려드립니다."),
            ("notify_notice", "공지사항", "관리자가 새 공지를 등록하면 알려드립니다."),
            ("notify_security", "보안", "새 환경 로그인처럼 확인이 필요한 활동을 알려드립니다."),
        ]
        toggles = "".join(
            f"""
            <label class="setting-toggle">
              <span><strong>{label}</strong><small>{description}</small></span>
              <input type="checkbox" name="{column}" value="1" {"checked" if user[column] else ""}>
            </label>
            """
            for column, label, description in notification_options
        )
        self.send_html(
            f"""
            <section class="settings-page">
              <div class="section-title"><div><p class="eyebrow">내 계정</p><h1>알림 설정</h1></div><a class="button" href="/mypage">마이페이지</a></div>
              {settings_navigation("notifications")}
              {'<p class="success-message" role="status">알림 설정을 저장했습니다.</p>' if query.get("saved") == "1" else ""}
              <div class="panel">
                <form method="post" action="/settings/notifications">
                  {self.csrf_input(user)}
                  <div class="setting-list">{toggles}</div>
                  <button class="primary">알림 설정 저장</button>
                </form>
              </div>
            </section>
            """
        )

    def update_notification_settings(self, query, form):
        user = self.require_user()
        if not user:
            return
        values = [int(form.get(column) == "1") for column in ["notify_chat", "notify_price", "notify_transaction", "notify_notice", "notify_security"]]
        with db() as conn:
            conn.execute(
                """
                UPDATE users SET notify_chat = ?, notify_price = ?, notify_transaction = ?,
                                 notify_notice = ?, notify_security = ? WHERE id = ?
                """,
                (*values, user["id"]),
            )
        self.redirect("/settings?saved=1")

    def security_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY last_seen_at DESC",
                (user["id"], int(time.time())),
            ).fetchall()
            login_events = conn.execute(
                "SELECT * FROM login_events WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (user["id"],),
            ).fetchall()
        session_rows = "".join(
            f"""
            <tr>
              <td>{'<span class="status-badge">현재 기기</span>' if session["token"] == user["session_token"] else '다른 기기'}</td>
              <td>{esc(session["ip_address"] or "기록 없음")}</td>
              <td><span class="ua-text">{esc(session["user_agent"] or "기록 없음")}</span></td>
              <td>{esc(session["created_at"] or "-")}<br><small>최근 {esc(session["last_seen_at"] or "-")}</small></td>
            </tr>
            """
            for session in sessions
        )
        login_rows = "".join(
            f"""
            <tr class="{'suspicious-row' if event['suspicious'] else ''}">
              <td>{esc(event["created_at"])}</td>
              <td>{'성공' if event["success"] else '실패'}{' · 확인 필요' if event["suspicious"] else ''}</td>
              <td>{esc(event["ip_address"] or "-")}</td>
              <td><span class="ua-text">{esc(event["user_agent"] or "-")}</span></td>
            </tr>
            """
            for event in login_events
        )
        self.send_html(
            f"""
            <section class="settings-page">
              <div class="section-title"><div><p class="eyebrow">내 계정</p><h1>보안 설정</h1></div><a class="button" href="/mypage">마이페이지</a></div>
              {settings_navigation("security")}
              <div class="panel security-grid">
                <div class="security-setting">
                  <h2>로그인 실패 계정 잠금</h2>
                  <p>비밀번호 입력을 5회 실패하면 계정을 잠그고 등록된 휴대전화 본인인증과 새 비밀번호 설정을 요구합니다.</p>
                </div>
                <div class="security-setting">
                  <h2>비밀번호</h2>
                  <p>현재 비밀번호 확인 후 새 비밀번호로 변경할 수 있습니다.</p>
                  <a class="button" href="/password-change">비밀번호 변경</a>
                </div>
                <div class="security-setting">
                  <h2>로그인된 기기 관리</h2>
                  <p>현재 로그인된 세션은 {len(sessions)}개입니다.</p>
                  <form method="post" action="/security/sessions/logout-others">
                    {self.csrf_input(user)}
                    <button>다른 기기에서 모두 로그아웃</button>
                  </form>
                </div>
              </div>
              <section class="panel table-panel"><h2>로그인된 기기</h2><table><thead><tr><th>구분</th><th>IP</th><th>브라우저</th><th>접속 시간</th></tr></thead><tbody>{session_rows}</tbody></table></section>
              <section class="panel table-panel"><h2>최근 로그인 기록</h2><table><thead><tr><th>일시</th><th>결과</th><th>IP</th><th>브라우저</th></tr></thead><tbody>{login_rows or '<tr><td colspan="4">로그인 기록이 없습니다.</td></tr>'}</tbody></table></section>
            </section>
            """
        )

    def logout_other_sessions(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            deleted = conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                (user["id"], user["session_token"]),
            ).rowcount
            write_account_log(conn, user["id"], "다른 세션 로그아웃", f"{deleted}개 세션 종료", self.client_address[0])
        self.redirect("/security")

    def privacy_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            audit_logs = conn.execute(
                "SELECT * FROM account_audit_logs WHERE user_id = ? ORDER BY id DESC LIMIT 100",
                (user["id"],),
            ).fetchall()
        audit_rows = "".join(
            f'<tr><td>{esc(log["created_at"])}</td><td>{esc(log["action"])}</td><td>{esc(log["details"])}</td><td>{esc(log["ip_address"])}</td></tr>'
            for log in audit_logs
        )
        self.send_html(
            f"""
            <section class="settings-page">
              <div class="section-title"><div><p class="eyebrow">내 계정</p><h1>개인정보 설정</h1></div><a class="button" href="/mypage">마이페이지</a></div>
              {settings_navigation("privacy")}
              <div class="privacy-grid">
                <section class="panel">
                  <h2>내 개인정보 다운로드</h2>
                  <p>계정 정보와 내가 작성한 상품·메시지·거래·별점·로그인 기록을 JSON 파일로 내려받습니다.</p>
                  <a class="button" href="/privacy/export">개인정보 다운로드</a>
                </section>
                <section class="panel danger-zone">
                  <h2>회원 탈퇴</h2>
                  <p>상품은 즉시 비공개 처리되고 프로필과 연락처는 익명화됩니다. 거래·신고의 무결성을 위해 익명화된 최소 기록은 보관됩니다.</p>
                  <form method="post" action="/account/delete">
                    {self.csrf_input(user)}
                    <label>현재 비밀번호<input name="password" type="password" required autocomplete="current-password"></label>
                    <label class="check-label"><input type="checkbox" name="confirm_delete" value="yes" required> 상품 비공개와 계정 익명화 내용을 확인했습니다.</label>
                    <button class="danger">회원 탈퇴</button>
                  </form>
                </section>
              </div>
              <section class="panel table-panel"><h2>내 계정 감사 기록</h2><table><thead><tr><th>일시</th><th>작업</th><th>상세</th><th>IP</th></tr></thead><tbody>{audit_rows or '<tr><td colspan="4">기록이 없습니다.</td></tr>'}</tbody></table></section>
            </section>
            """
        )

    def export_personal_data(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            account = conn.execute(
                """
                SELECT id, username, display_name, bio, phone, location, status, created_at
                FROM users WHERE id = ?
                """,
                (user["id"],),
            ).fetchone()
            products = conn.execute("SELECT * FROM products WHERE seller_id = ? ORDER BY id", (user["id"],)).fetchall()
            sent_messages = conn.execute(
                "SELECT id, receiver_id, product_id, body, created_at FROM messages WHERE sender_id = ? ORDER BY id",
                (user["id"],),
            ).fetchall()
            transactions = conn.execute(
                "SELECT * FROM transactions WHERE seller_id = ? OR buyer_id = ? ORDER BY id",
                (user["id"], user["id"]),
            ).fetchall()
            ratings = conn.execute(
                "SELECT * FROM user_ratings WHERE reviewer_id = ? OR reviewee_id = ? ORDER BY id",
                (user["id"], user["id"]),
            ).fetchall()
            favorites = conn.execute("SELECT * FROM favorites WHERE user_id = ?", (user["id"],)).fetchall()
            notifications = conn.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
            login_events = conn.execute(
                "SELECT success, suspicious, ip_address, user_agent, created_at FROM login_events WHERE user_id = ? ORDER BY id",
                (user["id"],),
            ).fetchall()
            audit_logs = conn.execute(
                "SELECT action, details, ip_address, created_at FROM account_audit_logs WHERE user_id = ? ORDER BY id",
                (user["id"],),
            ).fetchall()
            wallet = conn.execute("SELECT balance, updated_at FROM wallets WHERE user_id = ?", (user["id"],)).fetchone()
            wallet_transfers = conn.execute(
                """
                SELECT reference_code, transaction_id, sender_id, receiver_id, amount, created_at
                FROM wallet_transfers WHERE sender_id = ? OR receiver_id = ? ORDER BY id
                """,
                (user["id"], user["id"]),
            ).fetchall()
            write_account_log(conn, user["id"], "개인정보 다운로드", "", self.client_address[0])

        def rows_to_dicts(rows):
            return [dict(row) for row in rows]

        payload = {
            "exported_at": now(),
            "account": dict(account),
            "products": rows_to_dicts(products),
            "sent_messages": rows_to_dicts(sent_messages),
            "transactions": rows_to_dicts(transactions),
            "ratings": rows_to_dicts(ratings),
            "favorites": rows_to_dicts(favorites),
            "notifications": rows_to_dicts(notifications),
            "login_events": rows_to_dicts(login_events),
            "account_audit_logs": rows_to_dicts(audit_logs),
            "wallet": dict(wallet) if wallet else {"balance": 0, "updated_at": ""},
            "wallet_transfers": rows_to_dicts(wallet_transfers),
        }
        self.send_json_download(payload, f"white-market-data-{user['id']}.json")

    def delete_account(self, query, form):
        user = self.require_user()
        if not user:
            return
        if user["is_admin"]:
            raise ValueError("관리자 계정은 일반 회원 탈퇴로 삭제할 수 없습니다.")
        if form.get("confirm_delete") != "yes":
            raise ValueError("회원 탈퇴 내용을 확인해주세요.")
        if not verify_password(form.get("password", ""), user["password_salt"], user["password_hash"]):
            raise ValueError("현재 비밀번호가 올바르지 않습니다.")
        profile_image_url = user["profile_image_url"]
        product_image_urls = []
        random_salt, random_hash = hash_password(secrets.token_urlsafe(48))
        anonymized_username = f"deleted_{user['id']}_{secrets.token_hex(5)}"
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            product_image_urls = [
                image_url
                for row in conn.execute(
                    "SELECT pi.image_url, pi.thumbnail_url FROM product_images pi JOIN products p ON p.id = pi.product_id WHERE p.seller_id = ?",
                    (user["id"],),
                ).fetchall()
                for image_url in (row["image_url"], row["thumbnail_url"])
                if image_url
            ]
            write_account_log(conn, user["id"], "회원 탈퇴", "상품 비공개·계정 익명화·잔여 WM 포인트 소멸", self.client_address[0])
            conn.execute("UPDATE products SET is_deleted = 1, image_url = '', updated_at = ? WHERE seller_id = ?", (now(), user["id"]))
            conn.execute("DELETE FROM product_images WHERE product_id IN (SELECT id FROM products WHERE seller_id = ?)", (user["id"],))
            conn.execute("DELETE FROM favorites WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM recent_views WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM keyword_alerts WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM notifications WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM password_reset_challenges WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM login_verification_challenges WHERE user_id = ?", (user["id"],))
            conn.execute("UPDATE wallets SET balance = 0, updated_at = ? WHERE user_id = ?", (now(), user["id"]))
            conn.execute("UPDATE login_events SET ip_address = '', user_agent = '', username_attempt = '' WHERE user_id = ?", (user["id"],))
            conn.execute(
                """
                UPDATE users
                SET username = ?, display_name = '탈퇴 사용자', bio = '', phone = '', location = '',
                    profile_image_url = '', status = 'deleted', password_salt = ?, password_hash = ?,
                    failed_login_count = 0, password_reset_required = 0,
                    notify_chat = 0, notify_price = 0, notify_transaction = 0, notify_notice = 0, notify_security = 0
                WHERE id = ?
                """,
                (anonymized_username, random_salt, random_hash, user["id"]),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        delete_profile_image(profile_image_url)
        for image_url in product_image_urls:
            delete_product_image(image_url)
        self.extra_headers = [("Set-Cookie", session_cookie("deleted", 0))]
        self.send_html(
            '<section class="panel narrow"><h1>회원 탈퇴 완료</h1><p>계정 정보가 익명화되고 등록 상품이 비공개 처리되었습니다.</p><a class="button" href="/">홈으로</a></section>'
        )

    def chat_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        peer_id = int(query.get("user", "0") or "0")
        product_id = int(query.get("product", "0") or "0")
        page = max(1, int(query.get("page", "1") or "1"))
        page_size = 50
        product_was_selected = "product" in query
        with db() as conn:
            conversations = conn.execute(
                """
                WITH conversation_messages AS (
                    SELECT m.*,
                           CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AS peer_id
                    FROM messages m
                    WHERE m.sender_id = ? OR m.receiver_id = ?
                ), ranked AS (
                    SELECT conversation_messages.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY peer_id, COALESCE(product_id, 0)
                               ORDER BY id DESC
                           ) AS row_number,
                           SUM(CASE WHEN receiver_id = ? AND is_read = 0 THEN 1 ELSE 0 END) OVER (
                               PARTITION BY peer_id, COALESCE(product_id, 0)
                           ) AS unread_count
                    FROM conversation_messages
                )
                SELECT ranked.*, u.display_name, u.username, u.profile_image_url,
                       p.title AS product_title
                FROM ranked
                JOIN users u ON u.id = ranked.peer_id
                LEFT JOIN products p ON p.id = ranked.product_id AND p.is_deleted = 0
                WHERE ranked.row_number = 1 AND u.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM user_blocks b
                    WHERE (b.blocker_id = ? AND b.blocked_id = u.id)
                       OR (b.blocker_id = u.id AND b.blocked_id = ?)
                  )
                ORDER BY ranked.id DESC
                """,
                (user["id"], user["id"], user["id"], user["id"], user["id"], user["id"]),
            ).fetchall()
            messages = []
            peer = None
            chat_rating = None
            product_context = None
            chat_transaction = None
            wallet_balance = conn.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
            wallet_balance = wallet_balance["balance"] if wallet_balance else 0
            chat_actions = []
            action_history = {}
            if peer_id:
                peer = conn.execute(
                    f"""
                    SELECT u.*, {rating_sql('u')} FROM users u
                    WHERE u.id = ? AND u.status = 'active'
                      AND NOT EXISTS (
                        SELECT 1 FROM user_blocks b
                        WHERE (b.blocker_id = ? AND b.blocked_id = u.id)
                           OR (b.blocker_id = u.id AND b.blocked_id = ?)
                      )
                    """,
                    (peer_id, user["id"], user["id"]),
                ).fetchone()
                if not peer:
                    return self.error_page(HTTPStatus.NOT_FOUND, "대화 상대를 찾을 수 없습니다.")
                if not product_id and not product_was_selected:
                    latest_product = conn.execute(
                        """
                        SELECT COALESCE(product_id, 0) AS product_id FROM messages
                        WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
                        ORDER BY id DESC LIMIT 1
                        """,
                        (user["id"], peer_id, peer_id, user["id"]),
                    ).fetchone()
                    product_id = latest_product["product_id"] if latest_product else 0
                if product_id:
                    product_context = conn.execute(
                        """
                        SELECT id, title, seller_id, image_url, price, status, category FROM products
                        WHERE id = ? AND is_deleted = 0 AND seller_id IN (?, ?)
                        """,
                        (product_id, user["id"], peer_id),
                    ).fetchone()
                    if not product_context:
                        product_id = 0
                conn.execute(
                    """
                    UPDATE messages SET is_read = 1
                    WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
                      AND COALESCE(product_id, 0) = ?
                    """,
                    (peer_id, user["id"], product_id),
                )
                messages = conn.execute(
                    """
                    SELECT m.*, s.display_name AS sender_name, s.username AS sender_username,
                           s.profile_image_url AS sender_profile_image_url, p.title AS product_title
                    FROM messages m JOIN users s ON s.id = m.sender_id
                    LEFT JOIN products p ON p.id = m.product_id
                    WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
                      AND COALESCE(m.product_id, 0) = ?
                    ORDER BY m.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (user["id"], peer_id, peer_id, user["id"], product_id, page_size, (page - 1) * page_size),
                ).fetchall()
                messages = list(reversed(messages))
                chat_rating = conn.execute(
                    """
                    SELECT score, review FROM user_ratings
                    WHERE reviewer_id = ? AND reviewee_id = ? AND context_type = 'chat' AND context_id = ?
                    """,
                    (user["id"], peer_id, peer_id),
                ).fetchone()
                if product_context:
                    chat_transaction = conn.execute(
                        """
                        SELECT t.*, COALESCE(NULLIF(t.agreed_price, 0), p.price) AS trade_price,
                               wt.amount AS payment_amount, wt.reference_code AS payment_reference,
                               wt.created_at AS payment_at
                        FROM transactions t
                        JOIN products p ON p.id = t.product_id
                        LEFT JOIN wallet_transfers wt ON wt.transaction_id = t.id
                        WHERE t.product_id = ?
                          AND ((t.buyer_id = ? AND t.seller_id = ?) OR (t.buyer_id = ? AND t.seller_id = ?))
                          AND t.status IN ('예약중', '거래완료')
                        ORDER BY t.id DESC LIMIT 1
                        """,
                        (product_id, user["id"], peer_id, peer_id, user["id"]),
                    ).fetchone()
                    chat_actions = conn.execute(
                        """
                        SELECT ca.*, actor.display_name AS actor_name
                        FROM chat_actions ca JOIN users actor ON actor.id = ca.actor_id
                        WHERE ca.product_id = ?
                          AND ((ca.buyer_id = ? AND ca.seller_id = ?) OR (ca.buyer_id = ? AND ca.seller_id = ?))
                          AND ca.action_type = 'offer'
                        ORDER BY ca.id DESC
                        """,
                        (product_id, user["id"], peer_id, peer_id, user["id"]),
                    ).fetchall()
                    for chat_action in chat_actions:
                        action_history[chat_action["id"]] = conn.execute(
                            """
                            SELECT h.*, u.display_name AS actor_name
                            FROM chat_action_history h JOIN users u ON u.id = h.actor_id
                            WHERE h.action_id = ? ORDER BY h.id
                            """,
                            (chat_action["id"],),
                        ).fetchall()
        conversation_items = ""
        for conversation in conversations:
            conversation_product_id = int(conversation["product_id"] or 0)
            product_query = f"&product={conversation_product_id}"
            is_selected = conversation["peer_id"] == peer_id and conversation_product_id == product_id
            unread_badge = f'<span class="conversation-unread">{conversation["unread_count"]}</span>' if conversation["unread_count"] and not is_selected else ""
            product_label = f'<span class="conversation-product">{esc(conversation["product_title"])}</span>' if conversation["product_title"] else ""
            conversation_items += f"""
            <a class="conversation-item {'selected' if is_selected else ''}" href="/chat?user={conversation['peer_id']}{product_query}">
              {profile_avatar(conversation, "medium")}
              <span class="conversation-copy">
                <span class="conversation-name">{esc(conversation['display_name'])}{unread_badge}</span>
                {product_label}
                <span class="conversation-preview">{esc(conversation['body'])}</span>
              </span>
              <time datetime="{esc(conversation['created_at'])}">{relative_time(conversation['created_at'])}</time>
            </a>
            """

        message_list = ""
        for message in messages:
            is_mine = message["sender_id"] == user["id"]
            sender = {
                "display_name": message["sender_name"],
                "username": message["sender_username"],
                "profile_image_url": message["sender_profile_image_url"],
            }
            message_list += f"""
            <li class="chat-message {'mine' if is_mine else 'theirs'}" data-message-id="{message['id']}">
              {'' if is_mine else profile_avatar(sender, 'small')}
              <div class="message-bubble">
                {f'<img class="chat-image" src="{esc(message["image_url"])}" alt="채팅으로 보낸 사진">' if message["image_url"] else ''}
                {f'<p>{esc(message["body"])}</p>' if message["body"] else ''}
                <span class="message-meta"><span class="read-state">{'읽음' if is_mine and message['is_read'] else ''}</span><time datetime="{esc(message['created_at'])}">{esc(message['created_at'])}</time></span>
              </div>
            </li>
            """

        product_banner = ""
        if product_context:
            product_banner = f"""
            <a class="chat-product-context" href="/product?id={product_context['id']}">
              <span class="chat-product-image">{product_image(product_context)}</span>
              <span><strong>{esc(product_context['title'])}</strong><small>{won(product_context['price'])} · {esc(product_context['status'])}</small></span>
              <span class="chat-product-link">상품 보기</span>
            </a>
            """
        action_cards = ""
        action_status_labels = {"pending": "응답 대기", "accepted": "수락됨", "rejected": "거절됨", "superseded": "변경 제안됨"}
        for chat_action in chat_actions:
            action_title = f"가격 제안 {won(chat_action['proposed_price'])}"
            action_detail = f"{esc(chat_action['actor_name'])}님이 희망 가격을 제안했습니다."
            response_buttons = ""
            if chat_action["status"] == "pending" and chat_action["actor_id"] != user["id"]:
                response_buttons = f"""
                <form method="post" action="/chat/action" class="inline">
                  {self.csrf_input(user)}
                  <input type="hidden" name="id" value="{chat_action["id"]}">
                  <button class="primary" name="decision" value="accept">수락</button>
                  <button name="decision" value="reject">거절</button>
                </form>
                """
            history_items = "".join(
                f'<li>{esc(history["created_at"])} · {esc(history["actor_name"])} · {esc(history["details"])}</li>'
                for history in action_history.get(chat_action["id"], [])
            )
            action_cards += f"""
            <article class="chat-action-card {esc(chat_action['status'])}">
              <div><span class="status-badge">{esc(action_status_labels.get(chat_action["status"], chat_action["status"]))}</span><h3>{action_title}</h3><p>{action_detail}</p></div>
              {response_buttons}
              <details><summary>변경 기록 {len(action_history.get(chat_action["id"], []))}건</summary><ul>{history_items}</ul></details>
            </article>
            """
        chat_action_panel = ""
        if product_context and peer:
            transaction_control = ""
            payment_panel = ""
            if chat_transaction:
                if chat_transaction["payment_amount"]:
                    payment_panel = f"""
                    <div class="chat-payment-panel complete">
                      <div>
                        <span class="status-badge">송금 완료</span>
                        <strong>WM 포인트 {won(chat_transaction["payment_amount"])}</strong>
                        <small>{esc(chat_transaction["payment_at"])} · 참조번호 <code>{esc(chat_transaction["payment_reference"])}</code></small>
                      </div>
                      <a class="button" href="/transaction/evidence?id={chat_transaction["id"]}">송금 기록</a>
                    </div>
                    """
                if chat_transaction["status"] == "예약중":
                    seller_controls = ""
                    if user["id"] == product_context["seller_id"]:
                        payment_received = bool(chat_transaction["payment_amount"])
                        seller_controls = f"""
                        <div class="inline">
                          <form method="post" action="/transaction/status" class="inline">
                            {self.csrf_input(user)}
                            <input type="hidden" name="id" value="{chat_transaction["id"]}">
                            <input type="hidden" name="return_to_chat" value="1">
                            <button name="action" value="reopen" {"disabled" if payment_received else ""}>판매중으로 변경</button>
                          </form>
                          <form method="post" action="/transaction/status" class="inline">
                            {self.csrf_input(user)}
                            <input type="hidden" name="id" value="{chat_transaction["id"]}">
                            <input type="hidden" name="return_to_chat" value="1">
                            <button class="primary" name="action" value="complete" {"disabled" if not payment_received else ""}>거래 완료</button>
                          </form>
                        </div>
                        """
                    transaction_control = f"""
                    <div class="chat-transaction-control">
                      <span><strong>거래 예약중</strong><small>{'송금이 완료되어 판매중으로 되돌릴 수 없습니다.' if seller_controls and chat_transaction["payment_amount"] else '구매자 송금 후 거래 완료로 변경할 수 있습니다.' if seller_controls else '판매자가 거래 상태를 관리하고 있습니다.'}</small></span>
                      {seller_controls}
                    </div>
                    """
                    if not chat_transaction["payment_amount"] and user["id"] == chat_transaction["buyer_id"]:
                        insufficient = wallet_balance < chat_transaction["trade_price"]
                        payment_panel = f"""
                        <div class="chat-payment-panel">
                          <div class="chat-payment-heading">
                            <div><p class="eyebrow">구매자 결제</p><h3>거래 대금 송금</h3></div>
                            <span class="payment-recipient">{esc(peer["display_name"])}님에게</span>
                          </div>
                          <dl class="chat-payment-summary">
                            <div><dt>확정 금액</dt><dd>{won(chat_transaction["trade_price"])}</dd></div>
                            <div><dt>보유 포인트</dt><dd>{won(wallet_balance)}</dd></div>
                          </dl>
                          {f'<p class="form-error">보유 포인트가 부족합니다.</p>' if insufficient else ''}
                          <form method="post" action="/transaction/payment">
                            {self.csrf_input(user)}
                            <input type="hidden" name="id" value="{chat_transaction["id"]}">
                            <input type="hidden" name="return_to_chat" value="1">
                            <button class="primary" {"disabled" if insufficient else ""}>WM 포인트 {won(chat_transaction["trade_price"])} 송금</button>
                          </form>
                          <small>교육용 가상 포인트이며 거래당 한 번만 송금할 수 있습니다.</small>
                        </div>
                        """
                    elif not chat_transaction["payment_amount"]:
                        payment_panel = f"""
                        <div class="chat-payment-panel waiting">
                          <div><p class="eyebrow">거래 대금</p><strong>구매자 송금 대기</strong><small>확정 금액 {won(chat_transaction["trade_price"])}</small></div>
                        </div>
                        """
                else:
                    transaction_control = """
                    <div class="chat-transaction-control complete">
                      <span><strong>거래 완료</strong><small>완료된 거래입니다.</small></span>
                    </div>
                    """
                    if not chat_transaction["payment_amount"]:
                        payment_panel = """
                        <div class="chat-payment-panel waiting">
                          <div><p class="eyebrow">거래 대금</p><strong>송금 기록 없음</strong><small>송금 기능 적용 전에 완료된 거래입니다.</small></div>
                        </div>
                        """
            elif user["id"] == product_context["seller_id"] and product_context["status"] == "판매중" and messages:
                transaction_control = f"""
                <div class="chat-transaction-control">
                  <span><strong>거래 상태</strong><small>구매자와 거래가 확정되면 예약중으로 변경하세요.</small></span>
                  <form method="post" action="/chat/reserve" class="inline">
                    {self.csrf_input(user)}
                    <input type="hidden" name="product_id" value="{product_id}">
                    <input type="hidden" name="buyer_id" value="{peer_id}">
                    <button class="primary">예약중으로 변경</button>
                  </form>
                </div>
                """
            elif user["id"] == product_context["seller_id"] and product_context["status"] == "판매중":
                transaction_control = """
                <div class="chat-transaction-control">
                  <span><strong>거래 상태</strong><small>구매자와 메시지를 주고받은 뒤 예약중으로 변경할 수 있습니다.</small></span>
                </div>
                """
            elif user["id"] == product_context["seller_id"] and product_context["status"] == "예약중":
                transaction_control = """
                <div class="chat-transaction-control">
                  <span><strong>다른 구매자와 예약중</strong><small>예약을 진행한 채팅방에서 상태를 변경할 수 있습니다.</small></span>
                </div>
                """
            buyer_offer_form = ""
            if user["id"] != product_context["seller_id"]:
                buyer_offer_form = f"""
                <details>
                  <summary>가격 제안</summary>
                  <form method="post" action="/chat/offer">
                    {self.csrf_input(user)}
                    <input type="hidden" name="product_id" value="{product_id}">
                    <input type="hidden" name="peer_id" value="{peer_id}">
                    <label>희망 가격<input name="price" type="number" min="1" max="100000000" value="{product_context["price"]}" required></label>
                    <button class="primary">제안 보내기</button>
                  </form>
                </details>
                """
            chat_action_panel = f"""
            <section class="chat-action-panel">
              {transaction_control}
              {payment_panel}
              {f'<div class="chat-action-tools">{buyer_offer_form}</div>' if buyer_offer_form else ''}
              {f'<details class="chat-action-history"><summary>가격 제안 기록 {len(chat_actions)}건</summary><div class="chat-action-list">{action_cards}</div></details>' if chat_actions else ''}
            </section>
            """
        rating_form = ""
        if peer and messages:
            selected_rating = str(chat_rating["score"] if chat_rating else 5)
            rating_form = f"""
            <section class="chat-review">
              <div class="section-title"><h2>{esc(peer["display_name"])}님 후기</h2>{rating_display(peer["rating_average"], peer["rating_count"])}</div>
              <form method="post" action="/rating/chat" class="review">
                {self.csrf_input(user)}
                <input type="hidden" name="peer_id" value="{peer_id}">
                <select name="rating" aria-label="별점">{''.join(option(str(i), f"{i}점", selected_rating) for i in range(1, 6))}</select>
                <input name="review" value="{esc(chat_rating['review'] if chat_rating else '')}" maxlength="500" placeholder="대화 후기를 남겨주세요">
                <button>별점 저장</button>
              </form>
            </section>
            """
        chat_pagination = ""
        if peer and (page > 1 or len(messages) == page_size):
            newer = f'<a href="/chat?user={peer_id}&product={product_id}&page={page - 1}">더 최신 메시지</a>' if page > 1 else ""
            older = f'<a href="/chat?user={peer_id}&product={product_id}&page={page + 1}">이전 메시지</a>' if len(messages) == page_size else ""
            chat_pagination = f'<nav class="chat-pagination">{older}{newer}</nav>'
        self.send_html(
            f"""
            <section class="chat-layout {'has-selection' if peer else ''}">
              <aside class="conversation-panel">
                <div class="conversation-heading"><h1>채팅</h1><span>{len(conversations)}개의 대화</span></div>
                <div class="conversation-list">{conversation_items or '<div class="conversation-empty"><strong>아직 대화가 없어요</strong><p>상품 상세에서 판매자와 채팅을 시작해보세요.</p></div>'}</div>
              </aside>
              <div class="chat-thread">
                {f'''<div class="chat-thread-header">
                  <a class="chat-back" href="/chat" aria-label="대화 목록으로 돌아가기">‹</a>
                  <a class="chat-peer" href="/user?id={peer_id}">{profile_avatar(peer, "medium")}<span><strong>{esc(peer['display_name'])}</strong><small>@{esc(peer['username'])} · <span data-presence>{'접속 중' if peer['last_active_at'] and datetime.now() - datetime.strptime(peer['last_active_at'], '%Y-%m-%d %H:%M') < timedelta(minutes=2) else '오프라인'}</span></small></span></a>
                  {rating_display(peer['rating_average'], peer['rating_count'], compact=True)}
                </div>
                {product_banner}
                {chat_action_panel}
                {chat_pagination}
                <ul class="messages" data-current-user="{user['id']}" data-peer-id="{peer_id}" data-product-id="{product_id}" data-last-id="{messages[-1]['id'] if messages else 0}">{message_list or '<li class="chat-day-divider">첫 메시지를 보내 대화를 시작해보세요.</li>'}</ul>
                <form method="post" action="/chat" class="chat-composer" data-live-chat>
                  {self.csrf_input(user)}
                  <input type="hidden" name="receiver_id" value="{peer_id}">
                  <input type="hidden" name="product_id" value="{product_id}">
                  <input type="hidden" name="image_data" value="">
                  <label class="chat-image-button" title="사진 보내기">＋<input type="file" accept="image/jpeg,image/png,image/webp" data-chat-image></label>
                  <span class="chat-image-preview" hidden></span>
                  <textarea name="body" maxlength="500" aria-label="메시지" placeholder="메시지를 입력하세요"></textarea>
                  <button class="primary">전송</button>
                </form>
                {rating_form}''' if peer else '''<div class="chat-thread-empty"><strong>대화를 선택해주세요</strong><p>왼쪽 목록에서 확인할 채팅을 선택할 수 있어요.</p></div>'''}
              </div>
            </section>
            <script src="/static/chat.js" defer></script>
            """
        )

    def send_message(self, query, form):
        user = self.require_user()
        if not user:
            return
        enforce_rate_limit(
            "chat",
            (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
            60,
            60,
        )
        receiver_id = int(form.get("receiver_id", "0") or "0")
        product_id = int(form.get("product_id", "0") or "0")
        body = form.get("body", "").strip()
        image_data = form.get("image_data", "")
        if image_data:
            enforce_rate_limit(
                "image-upload",
                (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
                20,
                10 * 60,
            )
        if not receiver_id:
            raise ValueError("대화 상대를 선택하세요.")
        if len(body) > 500 or (not body and not image_data):
            raise ValueError("메시지 또는 사진을 입력해주세요.")
        image_url = save_chat_image(image_data) if image_data else ""
        with db() as conn:
            blocked = conn.execute(
                "SELECT 1 FROM user_blocks WHERE (blocker_id = ? AND blocked_id = ?) OR (blocker_id = ? AND blocked_id = ?)",
                (user["id"], receiver_id, receiver_id, user["id"]),
            ).fetchone()
            if blocked:
                raise ValueError("차단 관계에서는 메시지를 보낼 수 없습니다.")
            if product_id:
                product = conn.execute(
                    "SELECT seller_id FROM products WHERE id = ? AND is_deleted = 0",
                    (product_id,),
                ).fetchone()
                if not product or product["seller_id"] not in (user["id"], receiver_id):
                    raise ValueError("채팅 상품 정보를 확인할 수 없습니다.")
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, product_id, body, image_url, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user["id"], receiver_id, product_id or None, body, image_url, now()),
            )
            add_notification(
                conn,
                receiver_id,
                "chat",
                f"{user['display_name']}님의 새 메시지",
                body or "사진을 보냈습니다.",
                product_id or None,
                f"/chat?user={user['id']}&product={product_id}",
            )
        product_query = f"&product={product_id}" if product_id else ""
        self.redirect(f"/chat?user={receiver_id}{product_query}")

    def chat_stream(self, query, form):
        user = self.require_user()
        if not user:
            return
        peer_id = int(query.get("user", "0") or "0")
        product_id = int(query.get("product", "0") or "0")
        after_id = max(0, int(query.get("after", "0") or "0"))
        if not peer_id:
            raise ValueError("대화 상대를 선택하세요.")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_security_headers()
        self.end_headers()
        deadline = time.time() + 22
        try:
            while time.time() < deadline:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE messages SET is_read = 1
                        WHERE sender_id = ? AND receiver_id = ? AND COALESCE(product_id, 0) = ? AND is_read = 0
                        """,
                        (peer_id, user["id"], product_id),
                    )
                    rows = conn.execute(
                        """
                        SELECT m.*, s.display_name AS sender_name, s.username AS sender_username,
                               s.profile_image_url AS sender_profile_image_url
                        FROM messages m JOIN users s ON s.id = m.sender_id
                        WHERE m.id > ? AND COALESCE(m.product_id, 0) = ?
                          AND ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))
                        ORDER BY m.id
                        """,
                        (after_id, product_id, user["id"], peer_id, peer_id, user["id"]),
                    ).fetchall()
                    peer = conn.execute("SELECT last_active_at FROM users WHERE id = ?", (peer_id,)).fetchone()
                    read_through = conn.execute(
                        """
                        SELECT COALESCE(MAX(id), 0) AS id FROM messages
                        WHERE sender_id = ? AND receiver_id = ? AND COALESCE(product_id, 0) = ? AND is_read = 1
                        """,
                        (user["id"], peer_id, product_id),
                    ).fetchone()["id"]
                online = False
                if peer and peer["last_active_at"]:
                    try:
                        online = datetime.now() - datetime.strptime(peer["last_active_at"], "%Y-%m-%d %H:%M") < timedelta(minutes=2)
                    except ValueError:
                        pass
                payload = {
                    "messages": [dict(row) for row in rows],
                    "online": online,
                    "read_through": read_through,
                }
                self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                if rows:
                    after_id = rows[-1]["id"]
                time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError):
            return

    def create_chat_offer(self, query, form):
        user = self.require_user()
        if not user:
            return
        product_id = int(form.get("product_id", "0") or "0")
        peer_id = int(form.get("peer_id", "0") or "0")
        price = int(form.get("price", "0") or "0")
        if not (1 <= price <= 100_000_000):
            raise ValueError("제안 가격을 확인해주세요.")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            product = conn.execute(
                "SELECT * FROM products WHERE id = ? AND is_deleted = 0 AND status != '거래완료'",
                (product_id,),
            ).fetchone()
            if not product or product["seller_id"] != peer_id or user["id"] == product["seller_id"]:
                raise ValueError("이 상품에는 가격을 제안할 수 없습니다.")
            action_id = conn.execute(
                """
                INSERT INTO chat_actions(product_id, buyer_id, seller_id, actor_id, action_type, proposed_price, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'offer', ?, ?, ?)
                """,
                (product_id, user["id"], peer_id, user["id"], price, now(), now()),
            ).lastrowid
            details = f"구매자가 {won(price)}을 제안함"
            conn.execute(
                "INSERT INTO chat_action_history(action_id, actor_id, event_type, to_status, details, created_at) VALUES (?, ?, 'created', 'pending', ?, ?)",
                (action_id, user["id"], details, now()),
            )
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], peer_id, product_id, f"가격을 {won(price)}으로 제안했습니다.", now()),
            )
            add_notification(conn, peer_id, "offer", "새 가격 제안", f"{user['display_name']}님이 {won(price)}을 제안했습니다.", product_id, f"/chat?user={user['id']}&product={product_id}")
        self.redirect(f"/chat?user={peer_id}&product={product_id}")

    def reserve_chat_transaction(self, query, form):
        user = self.require_user()
        if not user:
            return
        product_id = int(form.get("product_id", "0") or "0")
        buyer_id = int(form.get("buyer_id", "0") or "0")
        if not product_id or not buyer_id or buyer_id == user["id"]:
            raise ValueError("예약할 거래 상대를 확인해주세요.")

        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            product = conn.execute(
                "SELECT * FROM products WHERE id = ? AND seller_id = ? AND is_deleted = 0",
                (product_id, user["id"]),
            ).fetchone()
            buyer = conn.execute(
                "SELECT id, display_name FROM users WHERE id = ? AND status = 'active'",
                (buyer_id,),
            ).fetchone()
            if not product or not buyer:
                raise ValueError("예약할 상품이나 구매자를 찾을 수 없습니다.")
            if product["status"] != "판매중":
                raise ValueError("판매중인 상품만 예약중으로 변경할 수 있습니다.")
            conversation = conn.execute(
                """
                SELECT 1 FROM messages
                WHERE product_id = ?
                  AND ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
                LIMIT 1
                """,
                (product_id, user["id"], buyer_id, buyer_id, user["id"]),
            ).fetchone()
            if not conversation:
                raise ValueError("해당 상품에 대해 대화한 구매자만 예약할 수 있습니다.")

            transaction = conn.execute(
                """
                SELECT * FROM transactions
                WHERE product_id = ? AND seller_id = ? AND buyer_id = ? AND status = '거래요청'
                ORDER BY id DESC LIMIT 1
                """,
                (product_id, user["id"], buyer_id),
            ).fetchone()
            accepted_offer = conn.execute(
                """
                SELECT proposed_price FROM chat_actions
                WHERE product_id = ? AND buyer_id = ? AND seller_id = ?
                  AND action_type = 'offer' AND status = 'accepted' AND proposed_price > 0
                ORDER BY id DESC LIMIT 1
                """,
                (product_id, buyer_id, user["id"]),
            ).fetchone()
            agreed_price = accepted_offer["proposed_price"] if accepted_offer else product["price"]
            timestamp = now()
            if transaction:
                tx_id = transaction["id"]
                from_status = transaction["status"]
                conn.execute(
                    "UPDATE transactions SET status = '예약중', agreed_price = ?, updated_at = ? WHERE id = ?",
                    (agreed_price, timestamp, tx_id),
                )
            else:
                tx_id = conn.execute(
                    """
                    INSERT INTO transactions(product_id, seller_id, buyer_id, status, agreed_price, created_at, updated_at)
                    VALUES (?, ?, ?, '예약중', ?, ?, ?)
                    """,
                    (product_id, user["id"], buyer_id, agreed_price, timestamp, timestamp),
                ).lastrowid
                from_status = "판매중"

            rejected_requests = conn.execute(
                "SELECT id, buyer_id FROM transactions WHERE product_id = ? AND id != ? AND status = '거래요청'",
                (product_id, tx_id),
            ).fetchall()
            conn.execute(
                "UPDATE products SET status = '예약중', updated_at = ? WHERE id = ?",
                (timestamp, product_id),
            )
            conn.execute(
                "UPDATE transactions SET status = '거래거절', updated_at = ? WHERE product_id = ? AND id != ? AND status = '거래요청'",
                (timestamp, product_id, tx_id),
            )
            conn.execute(
                """
                INSERT INTO transaction_history(transaction_id, actor_id, from_status, to_status, details, created_at)
                VALUES (?, ?, ?, '예약중', '판매자가 채팅 후 거래를 예약중으로 변경', ?)
                """,
                (tx_id, user["id"], from_status, timestamp),
            )
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], buyer_id, product_id, "판매자가 상품을 예약중으로 변경했습니다.", timestamp),
            )
            add_notification(
                conn,
                buyer_id,
                "transaction",
                "거래 예약 확정",
                f"{product['title']} 상품이 예약중으로 변경되었습니다.",
                product_id,
                f"/chat?user={user['id']}&product={product_id}",
            )
            for rejected_request in rejected_requests:
                add_notification(
                    conn,
                    rejected_request["buyer_id"],
                    "transaction",
                    "거래 요청 종료",
                    "판매자가 다른 구매자와 거래를 예약했습니다.",
                    product_id,
                    f"/transactions?focus={rejected_request['id']}",
                )
        self.redirect(f"/chat?user={buyer_id}&product={product_id}")

    def update_chat_action(self, query, form):
        user = self.require_user()
        if not user:
            return
        action_id = int(form.get("id", "0") or "0")
        decision = form.get("decision", "")
        if decision not in {"accept", "reject"}:
            raise ValueError("응답 내용을 확인해주세요.")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            action = conn.execute(
                """
                SELECT ca.*, p.title FROM chat_actions ca JOIN products p ON p.id = ca.product_id
                WHERE ca.id = ? AND ca.action_type = 'offer'
                  AND (ca.buyer_id = ? OR ca.seller_id = ?)
                """,
                (action_id, user["id"], user["id"]),
            ).fetchone()
            if not action or action["status"] != "pending" or action["actor_id"] == user["id"]:
                raise ValueError("현재 상태에서는 응답할 수 없습니다.")
            status = "accepted" if decision == "accept" else "rejected"
            peer_id = action["seller_id"] if user["id"] == action["buyer_id"] else action["buyer_id"]
            label = "수락" if decision == "accept" else "거절"
            detail = f"가격 제안을 {label}함"
            conn.execute("UPDATE chat_actions SET status = ?, updated_at = ? WHERE id = ?", (status, now(), action_id))
            conn.execute(
                """
                INSERT INTO chat_action_history(action_id, actor_id, event_type, from_status, to_status, details, created_at)
                VALUES (?, ?, 'decision', 'pending', ?, ?, ?)
                """,
                (action_id, user["id"], status, detail, now()),
            )
            conn.execute(
                "INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], peer_id, action["product_id"], detail, now()),
            )
            add_notification(
                conn,
                peer_id,
                action["action_type"],
                f"{action['title']} 제안 응답",
                detail,
                action["product_id"],
                f"/chat?user={user['id']}&product={action['product_id']}",
            )
        self.redirect(f"/chat?user={peer_id}&product={action['product_id']}")

    def rate_chat_user(self, query, form):
        user = self.require_user()
        if not user:
            return
        peer_id = int(form.get("peer_id", "0") or "0")
        rating = int(form.get("rating", "5") or "5")
        review = form.get("review", "").strip()[:500]
        if peer_id == user["id"] or not (1 <= rating <= 5):
            raise ValueError("별점 정보를 확인해주세요.")
        with db() as conn:
            peer = conn.execute("SELECT 1 FROM users WHERE id = ? AND status = 'active'", (peer_id,)).fetchone()
            chatted = conn.execute(
                """
                SELECT 1 FROM messages
                WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
                LIMIT 1
                """,
                (user["id"], peer_id, peer_id, user["id"]),
            ).fetchone()
            if not peer or not chatted:
                raise ValueError("대화한 사용자에게만 별점을 남길 수 있습니다.")
            conn.execute(
                """
                INSERT INTO user_ratings(reviewer_id, reviewee_id, context_type, context_id, score, review, created_at, updated_at)
                VALUES (?, ?, 'chat', ?, ?, ?, ?, ?)
                ON CONFLICT(reviewer_id, reviewee_id, context_type, context_id)
                DO UPDATE SET score = excluded.score, review = excluded.review, updated_at = excluded.updated_at
                """,
                (user["id"], peer_id, peer_id, rating, review, now(), now()),
            )
        self.redirect(f"/chat?user={peer_id}")

    def transactions_page(self, query, form):
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            rows = conn.execute(
                """
                SELECT t.*, p.title,
                       COALESCE(NULLIF(t.agreed_price, 0), p.price) AS trade_price,
                       s.display_name AS seller, b.display_name AS buyer,
                       ur.score AS my_rating_score, ur.review AS my_rating_review,
                       wt.amount AS payment_amount, wt.reference_code AS payment_reference,
                       wt.created_at AS payment_at
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                JOIN users s ON s.id = t.seller_id
                JOIN users b ON b.id = t.buyer_id
                LEFT JOIN wallet_transfers wt ON wt.transaction_id = t.id
                LEFT JOIN user_ratings ur
                  ON ur.reviewer_id = ? AND ur.context_type = 'transaction' AND ur.context_id = t.id
                WHERE t.seller_id = ? OR t.buyer_id = ?
                ORDER BY t.id DESC
                """,
                (user["id"], user["id"], user["id"]),
            ).fetchall()
            checklist_rows = conn.execute(
                "SELECT transaction_id, item_key, checked FROM transaction_checklists WHERE user_id = ?",
                (user["id"],),
            ).fetchall()
            wallet = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user["id"],)).fetchone()
            transfers = conn.execute(
                """
                SELECT wt.*, p.title,
                       sender.display_name AS sender_name,
                       receiver.display_name AS receiver_name
                FROM wallet_transfers wt
                JOIN transactions t ON t.id = wt.transaction_id
                JOIN products p ON p.id = t.product_id
                JOIN users sender ON sender.id = wt.sender_id
                JOIN users receiver ON receiver.id = wt.receiver_id
                WHERE wt.sender_id = ? OR wt.receiver_id = ?
                ORDER BY wt.id DESC LIMIT 20
                """,
                (user["id"], user["id"]),
            ).fetchall()
        checklist_map = {(row["transaction_id"], row["item_key"]): bool(row["checked"]) for row in checklist_rows}
        items = "".join(transaction_row(t, user, self.csrf_input(user), checklist_map) for t in rows)
        transfer_rows = ""
        for transfer in transfers:
            sent = transfer["sender_id"] == user["id"]
            counterpart = transfer["receiver_name"] if sent else transfer["sender_name"]
            transfer_rows += f"""
            <tr>
              <td>{esc(transfer["created_at"])}</td>
              <td>{esc(transfer["title"])}</td>
              <td>{'보냄' if sent else '받음'} · {esc(counterpart)}</td>
              <td class="wallet-amount {'sent' if sent else 'received'}">{'-' if sent else '+'}{won(transfer["amount"])}</td>
              <td><code>{esc(transfer["reference_code"])}</code></td>
            </tr>
            """
        balance = wallet["balance"] if wallet else 0
        self.send_html(
            f"""
            <section class="wallet-overview">
              <div>
                <span>내 WM 포인트</span>
                <strong>{won(balance)}</strong>
                <small>교육용 가상 포인트이며 실제 현금 가치가 없습니다.</small>
              </div>
              <details>
                <summary>최근 송금 내역 {len(transfers)}건</summary>
                <div class="table-panel">
                  <table>
                    <thead><tr><th>일시</th><th>상품</th><th>구분</th><th>금액</th><th>참조번호</th></tr></thead>
                    <tbody>{transfer_rows or '<tr><td colspan="5">송금 내역이 없습니다.</td></tr>'}</tbody>
                  </table>
                </div>
              </details>
            </section>
            <section class="panel"><h1>거래 내역 조회</h1><div class="tx-list">{items or "<p>거래 내역이 없습니다.</p>"}</div></section>
            """
        )

    def transfer_transaction_payment(self, query, form):
        user = self.require_user()
        if not user:
            return
        enforce_rate_limit(
            "wallet-transfer",
            (f"ip:{self.client_address[0]}", f"user:{user['id']}"),
            10,
            10 * 60,
        )
        tx_id = int(form.get("id", "0") or "0")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tx = conn.execute(
                """
                SELECT t.*, p.title,
                       COALESCE(NULLIF(t.agreed_price, 0), p.price) AS trade_price
                FROM transactions t
                JOIN products p ON p.id = t.product_id
                WHERE t.id = ? AND t.buyer_id = ?
                """,
                (tx_id, user["id"]),
            ).fetchone()
            if not tx:
                raise ValueError("송금할 거래를 찾을 수 없습니다.")
            if tx["status"] != "예약중":
                raise ValueError("판매자가 예약중으로 확정한 거래에서만 송금할 수 있습니다.")
            if conn.execute("SELECT 1 FROM wallet_transfers WHERE transaction_id = ?", (tx_id,)).fetchone():
                raise ValueError("이미 송금이 완료된 거래입니다.")

            amount = int(tx["trade_price"])
            if not (0 < amount <= 100_000_000):
                raise ValueError("거래 금액을 확인해주세요.")
            timestamp = now()
            conn.execute(
                "INSERT OR IGNORE INTO wallets(user_id, balance, updated_at) VALUES (?, ?, ?)",
                (tx["buyer_id"], INITIAL_DEMO_BALANCE, timestamp),
            )
            conn.execute(
                "INSERT OR IGNORE INTO wallets(user_id, balance, updated_at) VALUES (?, ?, ?)",
                (tx["seller_id"], INITIAL_DEMO_BALANCE, timestamp),
            )
            deducted = conn.execute(
                """
                UPDATE wallets SET balance = balance - ?, updated_at = ?
                WHERE user_id = ? AND balance >= ?
                """,
                (amount, timestamp, tx["buyer_id"], amount),
            )
            if deducted.rowcount != 1:
                raise ValueError("WM 포인트 잔액이 부족합니다.")
            conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                (amount, timestamp, tx["seller_id"]),
            )
            reference_code = f"WM-{secrets.token_hex(6).upper()}"
            conn.execute(
                """
                INSERT INTO wallet_transfers(reference_code, transaction_id, sender_id, receiver_id, amount, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (reference_code, tx_id, tx["buyer_id"], tx["seller_id"], amount, timestamp),
            )
            conn.execute(
                """
                INSERT INTO transaction_history(transaction_id, actor_id, from_status, to_status, details, created_at)
                VALUES (?, ?, '예약중', '예약중', ?, ?)
                """,
                (tx_id, user["id"], f"WM 포인트 {won(amount)} 송금 완료 · {reference_code}", timestamp),
            )
            conn.execute(
                """
                INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    tx["buyer_id"],
                    tx["seller_id"],
                    tx["product_id"],
                    f"WM 포인트 {won(amount)} 송금을 완료했습니다. 참조번호 {reference_code}",
                    timestamp,
                ),
            )
            add_notification(
                conn,
                tx["seller_id"],
                "payment",
                "WM 포인트 입금",
                f"{user['display_name']}님이 {tx['title']} 거래 대금 {won(amount)}을 보냈습니다.",
                tx["product_id"],
                f"/chat?user={tx['buyer_id']}&product={tx['product_id']}",
            )
            write_account_log(
                conn,
                tx["buyer_id"],
                "WM 포인트 송금",
                f"거래 #{tx_id} · {won(amount)} · {reference_code}",
                self.client_address[0],
            )
            write_account_log(
                conn,
                tx["seller_id"],
                "WM 포인트 수취",
                f"거래 #{tx_id} · {won(amount)} · {reference_code}",
                self.client_address[0],
            )
        if form.get("return_to_chat") == "1":
            return self.redirect(f"/chat?user={tx['seller_id']}&product={tx['product_id']}")
        self.redirect(f"/transactions?focus={tx_id}")

    def update_transaction_status(self, query, form):
        user = self.require_user()
        if not user:
            return
        tx_id = int(form.get("id", "0") or "0")
        action = form.get("action", "")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tx = conn.execute("SELECT * FROM transactions WHERE id = ? AND (seller_id = ? OR buyer_id = ?)", (tx_id, user["id"], user["id"])).fetchone()
            if not tx:
                raise ValueError("거래를 찾을 수 없습니다.")
            product = conn.execute("SELECT * FROM products WHERE id = ? AND is_deleted = 0", (tx["product_id"],)).fetchone()
            if not product:
                raise ValueError("상품을 찾을 수 없습니다.")

            if action == "accept" and user["id"] == tx["seller_id"] and tx["status"] == "거래요청":
                if product["status"] != "판매중":
                    raise ValueError("이미 다른 거래가 진행 중인 상품입니다.")
                rejected_requests = conn.execute(
                    "SELECT id, buyer_id FROM transactions WHERE product_id = ? AND id != ? AND status = '거래요청'",
                    (tx["product_id"], tx_id),
                ).fetchall()
                conn.execute("UPDATE transactions SET status = '예약중', updated_at = ? WHERE id = ?", (now(), tx_id))
                conn.execute("UPDATE products SET status = '예약중', updated_at = ? WHERE id = ?", (now(), tx["product_id"]))
                conn.execute(
                    "UPDATE transactions SET status = '거래거절', updated_at = ? WHERE product_id = ? AND id != ? AND status = '거래요청'",
                    (now(), tx["product_id"], tx_id),
                )
                for rejected_request in rejected_requests:
                    add_notification(
                        conn,
                        rejected_request["buyer_id"],
                        "transaction",
                        "거래 요청 종료",
                        "판매자가 다른 구매자의 거래 요청을 승인했습니다.",
                        tx["product_id"],
                        f"/transactions?focus={rejected_request['id']}",
                    )
                add_notification(conn, tx["buyer_id"], "transaction", "거래 요청 승인", "판매자가 거래 요청을 승인했습니다.", tx["product_id"], f"/transactions?focus={tx_id}")
                new_status = "예약중"
            elif action == "reject" and user["id"] == tx["seller_id"] and tx["status"] == "거래요청":
                conn.execute("UPDATE transactions SET status = '거래거절', updated_at = ? WHERE id = ?", (now(), tx_id))
                add_notification(conn, tx["buyer_id"], "transaction", "거래 요청 거절", "판매자가 거래 요청을 거절했습니다.", tx["product_id"], f"/transactions?focus={tx_id}")
                new_status = "거래거절"
            elif action == "reopen" and user["id"] == tx["seller_id"] and tx["status"] == "예약중":
                payment_exists = conn.execute(
                    "SELECT 1 FROM wallet_transfers WHERE transaction_id = ?",
                    (tx_id,),
                ).fetchone()
                if payment_exists:
                    raise ValueError("송금이 완료된 거래는 판매중으로 되돌릴 수 없습니다.")
                timestamp = now()
                conn.execute("UPDATE transactions SET status = '예약취소', updated_at = ? WHERE id = ?", (timestamp, tx_id))
                conn.execute("UPDATE products SET status = '판매중', updated_at = ? WHERE id = ?", (timestamp, tx["product_id"]))
                conn.execute(
                    "INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user["id"], tx["buyer_id"], tx["product_id"], "판매자가 상품을 다시 판매중으로 변경했습니다.", timestamp),
                )
                add_notification(
                    conn,
                    tx["buyer_id"],
                    "transaction",
                    "예약 취소",
                    "판매자가 상품을 다시 판매중으로 변경했습니다.",
                    tx["product_id"],
                    f"/chat?user={user['id']}&product={tx['product_id']}",
                )
                new_status = "예약취소"
            elif action == "complete" and user["id"] == tx["seller_id"] and tx["status"] == "예약중":
                if not conn.execute(
                    "SELECT 1 FROM wallet_transfers WHERE transaction_id = ?",
                    (tx_id,),
                ).fetchone():
                    raise ValueError("구매자의 송금이 완료된 후 거래 완료로 변경할 수 있습니다.")
                timestamp = now()
                conn.execute("UPDATE transactions SET status = '거래완료', updated_at = ? WHERE id = ?", (timestamp, tx_id))
                conn.execute("UPDATE products SET status = '거래완료', updated_at = ? WHERE id = ?", (timestamp, tx["product_id"]))
                conn.execute(
                    "INSERT INTO messages(sender_id, receiver_id, product_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user["id"], tx["buyer_id"], tx["product_id"], "판매자가 상품을 거래 완료로 변경했습니다.", timestamp),
                )
                add_notification(
                    conn,
                    tx["buyer_id"],
                    "transaction",
                    "거래 완료",
                    "판매자가 거래 완료를 확인했습니다.",
                    tx["product_id"],
                    f"/transactions?focus={tx_id}",
                )
                new_status = "거래완료"
            else:
                raise ValueError("현재 사용자와 거래 단계에서는 수행할 수 없는 작업입니다.")
            conn.execute(
                """
                INSERT INTO transaction_history(transaction_id, actor_id, from_status, to_status, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tx_id, user["id"], tx["status"], new_status, f"거래 상태를 {new_status}(으)로 변경", now()),
            )
        if form.get("return_to_chat") == "1":
            peer_id = tx["seller_id"] if user["id"] == tx["buyer_id"] else tx["buyer_id"]
            return self.redirect(f"/chat?user={peer_id}&product={tx['product_id']}")
        self.redirect(f"/transactions?focus={tx_id}")

    def update_transaction_checklist(self, query, form):
        user = self.require_user()
        if not user:
            return
        tx_id = int(form.get("id", "0") or "0")
        item_key = form.get("item_key", "")
        allowed_keys = {key for key, _ in SAFETY_CHECKLIST}
        if item_key not in allowed_keys:
            raise ValueError("안전 체크 항목을 확인해주세요.")
        checked = int(form.get("checked") == "1")
        with db() as conn:
            tx = conn.execute(
                "SELECT 1 FROM transactions WHERE id = ? AND (seller_id = ? OR buyer_id = ?)",
                (tx_id, user["id"], user["id"]),
            ).fetchone()
            if not tx:
                raise ValueError("거래를 찾을 수 없습니다.")
            conn.execute(
                """
                INSERT INTO transaction_checklists(transaction_id, user_id, item_key, checked, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id, user_id, item_key)
                DO UPDATE SET checked = excluded.checked, updated_at = excluded.updated_at
                """,
                (tx_id, user["id"], item_key, checked, now()),
            )
        self.redirect(f"/transactions?focus={tx_id}")

    def transaction_evidence(self, query, form):
        user = self.require_user()
        if not user:
            return
        tx_id = int(query.get("id", "0") or "0")
        with db() as conn:
            tx = conn.execute(
                """
                SELECT t.*, p.title,
                       COALESCE(NULLIF(t.agreed_price, 0), p.price) AS trade_price,
                       s.display_name AS seller_name, b.display_name AS buyer_name,
                       wt.amount AS payment_amount, wt.reference_code AS payment_reference,
                       wt.created_at AS payment_at
                FROM transactions t JOIN products p ON p.id = t.product_id
                JOIN users s ON s.id = t.seller_id JOIN users b ON b.id = t.buyer_id
                LEFT JOIN wallet_transfers wt ON wt.transaction_id = t.id
                WHERE t.id = ? AND (t.seller_id = ? OR t.buyer_id = ? OR ? = 1)
                """,
                (tx_id, user["id"], user["id"], user["is_admin"]),
            ).fetchone()
            if not tx:
                return self.error_page(HTTPStatus.NOT_FOUND, "거래 증빙을 찾을 수 없습니다.")
            history = conn.execute(
                """
                SELECT h.*, u.display_name AS actor_name FROM transaction_history h
                JOIN users u ON u.id = h.actor_id WHERE h.transaction_id = ? ORDER BY h.id
                """,
                (tx_id,),
            ).fetchall()
            messages = conn.execute(
                """
                SELECT m.*, u.display_name AS sender_name FROM messages m JOIN users u ON u.id = m.sender_id
                WHERE m.product_id = ? AND ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))
                ORDER BY m.id
                """,
                (tx["product_id"], tx["seller_id"], tx["buyer_id"], tx["buyer_id"], tx["seller_id"]),
            ).fetchall()
            actions = conn.execute(
                """
                SELECT ca.*, u.display_name AS actor_name FROM chat_actions ca JOIN users u ON u.id = ca.actor_id
                WHERE ca.product_id = ? AND ca.buyer_id = ? AND ca.seller_id = ? ORDER BY ca.id
                """,
                (tx["product_id"], tx["buyer_id"], tx["seller_id"]),
            ).fetchall()
        history_rows = "".join(f"<tr><td>{esc(row['created_at'])}</td><td>{esc(row['actor_name'])}</td><td>{esc(row['from_status'] or '-')} → {esc(row['to_status'])}</td><td>{esc(row['details'])}</td></tr>" for row in history)
        message_rows = ""
        for row in messages:
            image_link = f'<a href="{esc(row["image_url"])}">사진 보기</a>' if row["image_url"] else ""
            message_rows += f"<tr><td>#{row['id']}</td><td>{esc(row['created_at'])}</td><td>{esc(row['sender_name'])}</td><td>{esc(row['body'] or '[사진]')} {image_link}</td></tr>"
        action_rows = "".join(f"<tr><td>{esc(row['created_at'])}</td><td>{esc(row['actor_name'])}</td><td>{esc(row['action_type'])}</td><td>{esc(row['status'])}</td><td>{esc(row['meeting_place'] or row['proposed_price'] or '-')}</td></tr>" for row in actions)
        payment_rows = (
            f"<tr><td>{esc(tx['payment_at'])}</td><td>{esc(tx['buyer_name'])}</td><td>{esc(tx['seller_name'])}</td>"
            f"<td>{won(tx['payment_amount'])}</td><td><code>{esc(tx['payment_reference'])}</code></td></tr>"
            if tx["payment_amount"]
            else '<tr><td colspan="5">WM 포인트 송금 기록이 없습니다.</td></tr>'
        )
        other_id = tx["seller_id"] if user["id"] == tx["buyer_id"] else tx["buyer_id"]
        self.send_html(
            f"""
            <section>
              <div class="section-title"><h1>거래 증빙 · {esc(tx['title'])}</h1><a class="button" href="/transactions?focus={tx_id}">거래 내역</a></div>
              <div class="evidence-actions"><a class="button danger" href="/report?type=user&id={other_id}&reason=사기 의심">사기 의심 신고</a><a class="button danger" href="/report?type=user&id={other_id}&reason=노쇼">노쇼 신고</a></div>
              <section class="panel table-panel"><h2>WM 포인트 송금</h2><p class="muted">확정 거래 금액 {won(tx["trade_price"])} · 실제 현금 가치가 없는 교육용 기록</p><table><thead><tr><th>일시</th><th>보낸 사용자</th><th>받은 사용자</th><th>금액</th><th>참조번호</th></tr></thead><tbody>{payment_rows}</tbody></table></section>
              <section class="panel table-panel"><h2>거래 상태 이력</h2><table><thead><tr><th>일시</th><th>처리자</th><th>변경</th><th>상세</th></tr></thead><tbody>{history_rows}</tbody></table></section>
              <section class="panel table-panel"><h2>채팅 거래 이력</h2><table><thead><tr><th>일시</th><th>제안자</th><th>종류</th><th>상태</th><th>내용</th></tr></thead><tbody>{action_rows or '<tr><td colspan="5">기록이 없습니다.</td></tr>'}</tbody></table></section>
              <section class="panel table-panel"><h2>거래 채팅</h2><table><thead><tr><th>ID</th><th>일시</th><th>작성자</th><th>내용</th></tr></thead><tbody>{message_rows or '<tr><td colspan="4">기록이 없습니다.</td></tr>'}</tbody></table></section>
            </section>
            """
        )

    def review_transaction(self, query, form):
        user = self.require_user()
        if not user:
            return
        tx_id = int(form.get("id", "0") or "0")
        rating = int(form.get("rating", "5") or "5")
        review = form.get("review", "").strip()[:500]
        if not (1 <= rating <= 5):
            raise ValueError("별점은 1-5점이어야 합니다.")
        with db() as conn:
            tx = conn.execute(
                "SELECT * FROM transactions WHERE id = ? AND (buyer_id = ? OR seller_id = ?) AND status = '거래완료'",
                (tx_id, user["id"], user["id"]),
            ).fetchone()
            if not tx:
                raise ValueError("후기를 작성할 수 없습니다.")
            reviewee_id = tx["seller_id"] if user["id"] == tx["buyer_id"] else tx["buyer_id"]
            conn.execute(
                """
                INSERT INTO user_ratings(reviewer_id, reviewee_id, context_type, context_id, score, review, created_at, updated_at)
                VALUES (?, ?, 'transaction', ?, ?, ?, ?, ?)
                ON CONFLICT(reviewer_id, reviewee_id, context_type, context_id)
                DO UPDATE SET score = excluded.score, review = excluded.review, updated_at = excluded.updated_at
                """,
                (user["id"], reviewee_id, tx_id, rating, review, now(), now()),
            )
            if user["id"] == tx["buyer_id"]:
                conn.execute("UPDATE transactions SET review = ?, rating = ?, updated_at = ? WHERE id = ?", (review, rating, now(), tx_id))
        self.redirect(f"/transactions?focus={tx_id}")

    def notices_page(self, query, form):
        user = self.current_user()
        with db() as conn:
            notices = conn.execute(
                "SELECT n.*, u.display_name FROM notices n JOIN users u ON u.id = n.admin_id ORDER BY n.id DESC"
            ).fetchall()
        notice_items = "".join(notice_card(n, user, self.csrf_input(user) if user else "") for n in notices)
        admin_form = ""
        if user and user["is_admin"]:
            admin_form = f"""
            <section class="panel">
              <h2>공지 작성</h2>
              <form method="post" action="/notices">
                {self.csrf_input(user)}
                <label>제목<input name="title" maxlength="80" required></label>
                <label>내용<textarea name="body" maxlength="1000" required></textarea></label>
                <button class="primary">등록</button>
              </form>
            </section>
            """
        self.send_html(f"<section><h1>공지사항</h1>{admin_form}<div>{notice_items or '<p>공지사항이 없습니다.</p>'}</div></section>")

    def create_notice(self, query, form):
        user = self.require_admin()
        if not user:
            return
        title = form.get("title", "").strip()
        body = form.get("body", "").strip()
        if not title or not body:
            raise ValueError("공지 제목과 내용을 입력하세요.")
        with db() as conn:
            notice_id = conn.execute("INSERT INTO notices(admin_id, title, body, created_at) VALUES (?, ?, ?, ?)", (user["id"], title[:80], body[:1000], now())).lastrowid
            write_audit_log(conn, user["id"], "공지 생성", "notice", notice_id, title[:80], self.client_address[0])
            recipients = conn.execute("SELECT id FROM users WHERE status = 'active' AND id != ?", (user["id"],)).fetchall()
            for recipient in recipients:
                add_notification(conn, recipient["id"], "notice", "새 공지사항", title[:80], None, "/notices")
        self.redirect("/notices")

    def notice_edit_page(self, query, form):
        user = self.require_admin()
        if not user:
            return
        notice_id = int(query.get("id", "0") or "0")
        with db() as conn:
            notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
        if not notice:
            return self.error_page(HTTPStatus.NOT_FOUND, "수정할 공지사항을 찾을 수 없습니다.")
        self.send_html(
            f"""
            <section class="panel narrow">
              <h1>공지사항 수정</h1>
              <form method="post" action="/notice/edit">
                {self.csrf_input(user)}
                <input type="hidden" name="id" value="{notice["id"]}">
                <label>제목<input name="title" value="{esc(notice["title"])}" maxlength="80" required></label>
                <label>내용<textarea name="body" maxlength="1000" required>{esc(notice["body"])}</textarea></label>
                <div class="actions"><button class="primary">수정 저장</button><a class="button" href="/notices">취소</a></div>
              </form>
            </section>
            """
        )

    def update_notice(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        notice_id = int(form.get("id", "0") or "0")
        title = form.get("title", "").strip()
        body = form.get("body", "").strip()
        if not title or not body:
            raise ValueError("공지 제목과 내용을 입력하세요.")
        with db() as conn:
            result = conn.execute(
                "UPDATE notices SET title = ?, body = ? WHERE id = ?",
                (title[:80], body[:1000], notice_id),
            )
            if result.rowcount:
                write_audit_log(conn, admin["id"], "공지 수정", "notice", notice_id, title[:80], self.client_address[0])
        if not result.rowcount:
            raise ValueError("수정할 공지사항을 찾을 수 없습니다.")
        self.redirect("/notices")

    def delete_notice(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        notice_id = int(form.get("id", "0") or "0")
        with db() as conn:
            notice = conn.execute("SELECT title FROM notices WHERE id = ?", (notice_id,)).fetchone()
            result = conn.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
            if result.rowcount:
                write_audit_log(conn, admin["id"], "공지 삭제", "notice", notice_id, notice["title"] if notice else "", self.client_address[0])
        if not result.rowcount:
            raise ValueError("삭제할 공지사항을 찾을 수 없습니다.")
        self.redirect("/notices")

    def admin_page(self, query, form):
        user = self.require_admin()
        if not user:
            return
        with db() as conn:
            users = conn.execute(f"SELECT u.*, {rating_sql('u')} FROM users u ORDER BY u.id").fetchall()
            products = conn.execute(
                "SELECT p.*, u.display_name FROM products p JOIN users u ON u.id = p.seller_id ORDER BY p.id DESC"
            ).fetchall()
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
            metrics = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM users) AS users_total,
                  (SELECT COUNT(*) FROM users WHERE created_at >= ?) AS users_new,
                  (SELECT COUNT(*) FROM products) AS products_total,
                  (SELECT COUNT(*) FROM products WHERE created_at >= ?) AS products_new,
                  (SELECT COUNT(*) FROM transactions) AS transactions_total,
                  (SELECT COUNT(*) FROM transactions WHERE created_at >= ?) AS transactions_new,
                  (SELECT COUNT(*) FROM reports) AS reports_total,
                  (SELECT COUNT(*) FROM reports WHERE created_at >= ?) AS reports_new,
                  (SELECT COUNT(*) FROM reports WHERE status IN ('접수', '검토중')) AS reports_pending,
                  (SELECT COUNT(*) FROM users WHERE status = 'suspended') AS users_suspended,
                  (SELECT COUNT(*) FROM users WHERE status = 'deleted') AS users_deleted,
                  (SELECT COUNT(*) FROM products WHERE is_deleted = 1) AS products_deleted
                """,
                (seven_days_ago, seven_days_ago, seven_days_ago, seven_days_ago),
            ).fetchone()
            trend = []
            for day_offset in range(13, -1, -1):
                day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
                counts = conn.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM users WHERE substr(created_at, 1, 10) = ?) AS users_new,
                      (SELECT COUNT(*) FROM products WHERE substr(created_at, 1, 10) = ?) AS products_new,
                      (SELECT COUNT(*) FROM transactions WHERE substr(created_at, 1, 10) = ?) AS transactions_new,
                      (SELECT COUNT(*) FROM reports WHERE substr(created_at, 1, 10) = ?) AS reports_new,
                      (SELECT COUNT(*) FROM admin_audit_logs WHERE substr(created_at, 1, 10) = ? AND action = '회원 상태 변경' AND details LIKE '%→ suspended%') AS suspended,
                      (SELECT COUNT(*) FROM account_audit_logs WHERE substr(created_at, 1, 10) = ? AND action = '회원 탈퇴') AS deleted
                    """,
                    (day, day, day, day, day, day),
                ).fetchone()
                trend.append((day, counts))
        user_rows = "".join(
            f'<tr><td>{u["id"]}</td><td>{esc(u["display_name"])}<br><small>@{esc(u["username"])}</small></td><td>{esc(status_label(u["status"]))}</td><td>{rating_display(u["rating_average"], u["rating_count"], compact=True)}</td><td>{f"""<form method="post" action="/admin/user" class="inline">{self.csrf_input(user)}<input type="hidden" name="id" value="{u["id"]}"><button name="action" value="toggle">정지/해제</button></form>""" if u["status"] != "deleted" else "<span class='muted'>익명화 완료</span>"}</td></tr>'
            for u in users
        )
        product_rows = "".join(
            f'<tr><td>{p["id"]}</td><td>{esc(p["title"])}</td><td>{esc(p["display_name"])}</td><td>{esc(p["status"])}</td><td>{"삭제됨" if p["is_deleted"] else "게시중"}</td><td><form method="post" action="/admin/product" class="inline">{self.csrf_input(user)}<input type="hidden" name="id" value="{p["id"]}"><button name="action" value="delete">게시글 삭제</button></form></td></tr>'
            for p in products
        )
        metric_cards = "".join(
            f"""
            <article class="admin-metric {'urgent' if key == 'reports_pending' and metrics[key] else ''}">
              <span>{label}</span><strong>{metrics[key]}</strong>{f'<small>최근 7일 +{metrics[new_key]}</small>' if new_key else ''}
            </article>
            """
            for key, label, new_key in [
                ("users_total", "전체 회원", "users_new"),
                ("products_total", "전체 상품", "products_new"),
                ("transactions_total", "전체 거래", "transactions_new"),
                ("reports_total", "전체 신고", "reports_new"),
                ("reports_pending", "미처리 신고", None),
                ("users_suspended", "정지 회원", None),
                ("users_deleted", "탈퇴 회원", None),
                ("products_deleted", "삭제 상품", None),
            ]
        )
        trend_rows = "".join(
            f"""
            <tr>
              <td>{day[5:]}</td><td>{counts["users_new"]}</td><td>{counts["products_new"]}</td>
              <td>{counts["transactions_new"]}</td><td>{counts["reports_new"]}</td>
              <td>{counts["suspended"]}</td><td>{counts["deleted"]}</td>
            </tr>
            """
            for day, counts in trend
        )
        self.send_html(
            f"""
            <section>
              <div class="section-title"><h1>관리자</h1><div class="actions"><a class="button {'danger' if metrics['reports_pending'] else ''}" href="/admin/reports">신고 관리 {f"({metrics['reports_pending']})" if metrics['reports_pending'] else ""}</a><a class="button" href="/admin/audit">감사 로그</a><a class="button" href="/admin/operations">운영·백업</a></div></div>
              <div class="admin-metrics">{metric_cards}</div>
              <section class="panel table-panel">
                <h2>최근 14일 증가 추이</h2>
                <table><thead><tr><th>날짜</th><th>회원</th><th>상품</th><th>거래</th><th>신고</th><th>정지</th><th>탈퇴</th></tr></thead><tbody>{trend_rows}</tbody></table>
              </section>
            </section>
            <section class="panel table-panel">
              <h2>모든 회원 관리</h2>
              <table><thead><tr><th>ID</th><th>회원</th><th>상태</th><th>별점</th><th>관리</th></tr></thead><tbody>{user_rows}</tbody></table>
              <h2>모든 상품 관리</h2>
              <table><thead><tr><th>ID</th><th>상품</th><th>판매자</th><th>거래 상태</th><th>게시 상태</th><th>관리</th></tr></thead><tbody>{product_rows}</tbody></table>
            </section>
            """
        )

    def admin_reports_page(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        with db() as conn:
            reports = conn.execute(
                """
                SELECT r.*, reporter.username AS reporter_username, handler.username AS handler_username,
                       CASE
                         WHEN r.target_type = 'user' THEN (SELECT '@' || username FROM users WHERE id = r.target_id)
                         WHEN r.target_type = 'product' THEN (SELECT title FROM products WHERE id = r.target_id)
                         ELSE '알 수 없음'
                       END AS target_label
                FROM reports r
                JOIN users reporter ON reporter.id = r.reporter_id
                LEFT JOIN users handler ON handler.id = r.handled_by
                ORDER BY CASE r.status WHEN '접수' THEN 0 WHEN '검토중' THEN 1 ELSE 2 END, r.id DESC
                """
            ).fetchall()
            history_by_report = {}
            for report in reports:
                history_by_report[report["id"]] = conn.execute(
                    """
                    SELECT h.*, u.username AS actor_username
                    FROM report_history h JOIN users u ON u.id = h.actor_id
                    WHERE h.report_id = ? ORDER BY h.id
                    """,
                    (report["id"],),
                ).fetchall()
        cards = ""
        for report in reports:
            status_options = "".join(option(status, status, report["status"]) for status in ["접수", "검토중", "처리완료", "기각"])
            history = "".join(
                f'<li><strong>{esc(item["from_status"] or "생성")} → {esc(item["to_status"])}</strong> · @{esc(item["actor_username"])} · {esc(item["created_at"])}<br><span>{esc(item["note"])}</span></li>'
                for item in history_by_report[report["id"]]
            )
            cards += f"""
            <article class="report-card">
              <div class="section-title"><h2>신고 #{report["id"]}</h2><span class="status-badge">{esc(report["status"])}</span></div>
              <p><strong>대상:</strong> {esc(report["target_label"] or '삭제된 대상')} · <strong>신고자:</strong> @{esc(report["reporter_username"])}</p>
              <p><strong>사유:</strong> {esc(report["reason"])}</p>
              <p>{esc(report["details"])}</p>
              <form method="post" action="/admin/report" class="report-action-form">
                {self.csrf_input(admin)}<input type="hidden" name="id" value="{report["id"]}">
                <label>처리 상태<select name="status">{status_options}</select></label>
                <label>관리자 메모<input name="note" value="{esc(report["admin_note"])}" maxlength="1000" required></label>
                <button class="primary">처리 기록 저장</button>
              </form>
              <details class="report-history"><summary>처리 이력 {len(history_by_report[report["id"]])}건</summary><ul>{history}</ul></details>
            </article>
            """
        self.send_html(f'<section><div class="section-title"><h1>신고 관리</h1><a class="button" href="/admin">관리자 홈</a></div>{cards or "<p>접수된 신고가 없습니다.</p>"}</section>')

    def admin_update_report(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        report_id = int(form.get("id", "0") or "0")
        new_status = form.get("status", "")
        note = form.get("note", "").strip()[:1000]
        if new_status not in {"접수", "검토중", "처리완료", "기각"} or not note:
            raise ValueError("처리 상태와 관리자 메모를 입력하세요.")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise ValueError("신고를 찾을 수 없습니다.")
            conn.execute(
                "UPDATE reports SET status = ?, admin_note = ?, handled_by = ?, updated_at = ? WHERE id = ?",
                (new_status, note, admin["id"], now(), report_id),
            )
            conn.execute(
                "INSERT INTO report_history(report_id, actor_id, from_status, to_status, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, admin["id"], report["status"], new_status, note, now()),
            )
            write_audit_log(conn, admin["id"], "신고 처리", "report", report_id, f'{report["status"]} → {new_status}: {note}', self.client_address[0])
        self.redirect("/admin/reports")

    def admin_audit_page(self, query, form):
        if not self.require_admin():
            return
        with db() as conn:
            logs = conn.execute(
                """
                SELECT l.*, u.username AS admin_username
                FROM admin_audit_logs l JOIN users u ON u.id = l.admin_id
                ORDER BY l.id DESC LIMIT 300
                """
            ).fetchall()
            account_logs = conn.execute(
                """
                SELECT l.*, u.username
                FROM account_audit_logs l LEFT JOIN users u ON u.id = l.user_id
                ORDER BY l.id DESC LIMIT 300
                """
            ).fetchall()
        rows = "".join(
            f'<tr><td>{log["id"]}</td><td>{esc(log["created_at"])}</td><td>@{esc(log["admin_username"])}</td><td>{esc(log["action"])}</td><td>{esc(log["target_type"])} #{esc(log["target_id"] or "-")}</td><td>{esc(log["details"])}</td><td>{esc(log["ip_address"])}</td></tr>'
            for log in logs
        )
        account_rows = "".join(
            f'<tr><td>{log["id"]}</td><td>{esc(log["created_at"])}</td><td>@{esc(log["username"] or "탈퇴 사용자")}</td><td>{esc(log["action"])}</td><td>{esc(log["details"])}</td><td>{esc(log["ip_address"])}</td></tr>'
            for log in account_logs
        )
        self.send_html(
            f"""
            <section class="panel table-panel">
              <div class="section-title"><h1>관리자 감사 로그</h1><a class="button" href="/admin">관리자 홈</a></div>
              <h2>관리자 작업</h2>
              <table><thead><tr><th>ID</th><th>일시</th><th>관리자</th><th>작업</th><th>대상</th><th>상세</th><th>IP</th></tr></thead><tbody>{rows or '<tr><td colspan="7">기록이 없습니다.</td></tr>'}</tbody></table>
              <h2>계정·탈퇴 작업</h2>
              <table><thead><tr><th>ID</th><th>일시</th><th>회원</th><th>작업</th><th>상세</th><th>IP</th></tr></thead><tbody>{account_rows or '<tr><td colspan="6">기록이 없습니다.</td></tr>'}</tbody></table>
            </section>
            """
        )

    def admin_operations_page(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backups = sorted(BACKUP_DIR.glob("market-*.wmbak"), key=lambda item: item.stat().st_mtime, reverse=True)[:20]
        with db() as conn:
            errors = conn.execute("SELECT * FROM error_logs ORDER BY resolved, id DESC LIMIT 200").fetchall()
        backup_rows = "".join(
            f"""
            <tr><td>{esc(item.name)}</td><td>{item.stat().st_size / 1024 / 1024:.2f} MB</td><td>{datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}</td>
            <td><form method="post" action="/admin/restore" class="backup-restore-form">{self.csrf_input(admin)}<input type="hidden" name="filename" value="{esc(item.name)}"><input name="password" type="password" required autocomplete="current-password" placeholder="관리자 비밀번호"><input name="confirmation" required autocomplete="off" placeholder="RESTORE 입력"><button class="danger">복원</button></form></td></tr>
            """
            for item in backups
        )
        error_rows = "".join(
            f"""
            <tr class="{'muted' if row['resolved'] else 'suspicious-row'}"><td>{row['id']}</td><td>{esc(row['created_at'])}</td><td><code>{esc(row['request_id'])}</code></td><td>{esc(row['method'])} {esc(row['path'])}</td><td>{esc(row['error_type'])}: {esc(row['message'])}</td>
            <td>{'처리됨' if row['resolved'] else f'''<form method="post" action="/admin/error/resolve" class="inline">{self.csrf_input(admin)}<input type="hidden" name="id" value="{row["id"]}"><button>처리 완료</button></form>'''}</td></tr>
            """
            for row in errors
        )
        self.send_html(
            f"""
            <section>
              <div class="section-title"><h1>운영 안정성</h1><a class="button" href="/admin">관리자 홈</a></div>
              <section class="panel"><h2>암호화 DB 백업</h2><p>AES-GCM으로 암호화하고 최근 14개를 보관합니다. 복원 전에는 암호화 키와 DB 무결성을 검사하고 현재 DB도 자동 백업합니다.</p><form method="post" action="/admin/backup">{self.csrf_input(admin)}<button class="primary">지금 백업</button></form></section>
              <section class="panel table-panel"><h2>백업 파일</h2><table><thead><tr><th>파일</th><th>크기</th><th>생성 시각</th><th>복원</th></tr></thead><tbody>{backup_rows or '<tr><td colspan="4">백업이 없습니다.</td></tr>'}</tbody></table></section>
              <section class="panel table-panel"><h2>오류 로그</h2><table><thead><tr><th>ID</th><th>일시</th><th>오류 번호</th><th>요청</th><th>오류</th><th>상태</th></tr></thead><tbody>{error_rows or '<tr><td colspan="6">오류 기록이 없습니다.</td></tr>'}</tbody></table></section>
            </section>
            """
        )

    def admin_create_backup(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        target = create_backup("manual")
        with db() as conn:
            write_audit_log(conn, admin["id"], "DB 백업 생성", "backup", None, target.name, self.client_address[0])
        self.redirect("/admin/operations")

    def admin_restore_backup(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        if not verify_password(form.get("password", ""), admin["password_salt"], admin["password_hash"]):
            raise ValueError("관리자 비밀번호가 올바르지 않습니다.")
        if form.get("confirmation", "") != "RESTORE":
            raise ValueError("복원 확인란에 RESTORE를 정확히 입력해주세요.")
        enforce_rate_limit(
            "backup-restore",
            (f"ip:{self.client_address[0]}", f"user:{admin['id']}"),
            5,
            60 * 60,
        )
        filename = form.get("filename", "")
        if Path(filename).name != filename or not re.fullmatch(r"market-[a-z]+-\d{8}-\d{6}\.wmbak", filename):
            raise ValueError("백업 파일명이 올바르지 않습니다.")
        source = (BACKUP_DIR / filename).resolve()
        if source.parent != BACKUP_DIR.resolve() or not source.is_file():
            raise ValueError("복원할 백업을 찾을 수 없습니다.")
        decrypted = decrypt_backup(source)
        try:
            before_restore = create_backup("prerestore")
            source_conn = sqlite3.connect(decrypted)
            target_conn = sqlite3.connect(DB_PATH)
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
                source_conn.close()
        finally:
            decrypted.unlink(missing_ok=True)
        init_db()
        with db() as conn:
            restored_admin = conn.execute("SELECT id FROM users WHERE id = ? AND is_admin = 1", (admin["id"],)).fetchone()
            if restored_admin:
                write_audit_log(conn, admin["id"], "암호화 DB 백업 복원", "backup", None, f"{filename}, 복원 전 {before_restore.name}", self.client_address[0])
            conn.execute("DELETE FROM sessions")
        self.extra_headers = [("Set-Cookie", session_cookie("deleted", 0))]
        self.redirect("/login")

    def admin_resolve_error(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        error_id = int(form.get("id", "0") or "0")
        with db() as conn:
            conn.execute("UPDATE error_logs SET resolved = 1 WHERE id = ?", (error_id,))
            write_audit_log(conn, admin["id"], "오류 로그 처리", "error", error_id, "", self.client_address[0])
        self.redirect("/admin/operations")

    def admin_user(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        user_id = int(form.get("id", "0") or "0")
        if user_id == admin["id"]:
            raise ValueError("관리자 본인은 정지할 수 없습니다.")
        with db() as conn:
            target = conn.execute("SELECT username, status FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise ValueError("회원을 찾을 수 없습니다.")
            if target["status"] == "deleted":
                raise ValueError("탈퇴한 회원의 상태는 변경할 수 없습니다.")
            conn.execute("UPDATE users SET status = CASE WHEN status = 'active' THEN 'suspended' ELSE 'active' END WHERE id = ?", (user_id,))
            new_status = "suspended" if target["status"] == "active" else "active"
            write_audit_log(conn, admin["id"], "회원 상태 변경", "user", user_id, f'@{target["username"]}: {target["status"]} → {new_status}', self.client_address[0])
        self.redirect("/admin")

    def admin_product(self, query, form):
        admin = self.require_admin()
        if not admin:
            return
        product_id = int(form.get("id", "0") or "0")
        with db() as conn:
            product = conn.execute("SELECT title FROM products WHERE id = ?", (product_id,)).fetchone()
            result = conn.execute("UPDATE products SET is_deleted = 1 WHERE id = ? AND is_deleted = 0", (product_id,))
            if result.rowcount:
                write_audit_log(conn, admin["id"], "상품 게시글 삭제", "product", product_id, product["title"] if product else "", self.client_address[0])
        self.redirect("/admin")


def one_value(params):
    return {k: v[0] for k, v in params.items()}


def option(value, label, selected):
    return f'<option value="{value}" {"selected" if value == selected else ""}>{label}</option>'


def status_label(status):
    return {"active": "활성", "suspended": "정지", "deleted": "탈퇴"}.get(status, status)


def rating_display(average, count, compact=False):
    average = max(0.0, min(5.0, float(average or 0)))
    count = int(count or 0)
    fill_level = max(0, min(10, round(average * 2)))
    classes = "rating-display compact" if compact else "rating-display"
    return f"""
    <span class="{classes}" title="별점 {average:.1f}점, 평가 {count}회" aria-label="별점 {average:.1f}점, 평가 {count}회">
      <span class="stars" aria-hidden="true">
        <span class="stars-base">★★★★★</span>
        <span class="stars-fill stars-fill-{fill_level}">★★★★★</span>
      </span>
      <span class="rating-average">{average:.1f}</span>
      <span class="rating-count">({count})</span>
    </span>
    """


def product_data(form):
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()
    category = form.get("category", "")
    price = int(form.get("price", "0") or "0")
    distance = float(form.get("distance_km", "0") or "0")
    if not (2 <= len(title) <= 80):
        raise ValueError("상품명은 2-80자여야 합니다.")
    if not (5 <= len(description) <= 1000):
        raise ValueError("설명은 5-1000자여야 합니다.")
    if category not in CATEGORIES:
        raise ValueError("카테고리가 올바르지 않습니다.")
    if not (0 <= price <= 100_000_000):
        raise ValueError("가격이 올바르지 않습니다.")
    if not (0 <= distance <= 999):
        raise ValueError("거리가 올바르지 않습니다.")
    return title, description, category, price, distance


def product_image_manifest(form):
    raw = form.get("image_manifest", "[]")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("상품 사진 정보를 다시 확인해주세요.")
    if not isinstance(manifest, list) or len(manifest) > MAX_PRODUCT_IMAGES:
        raise ValueError(f"상품 사진은 최대 {MAX_PRODUCT_IMAGES}장까지 등록할 수 있습니다.")
    parsed = []
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError("상품 사진 정보가 올바르지 않습니다.")
        kind = item.get("kind")
        if kind == "existing":
            try:
                image_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                image_id = 0
            if image_id <= 0:
                raise ValueError("기존 상품 사진 정보를 확인할 수 없습니다.")
            parsed.append({"kind": "existing", "id": image_id, "primary": bool(item.get("primary"))})
        elif kind == "new" and isinstance(item.get("data"), str) and isinstance(item.get("thumbnail"), str):
            parsed.append({"kind": "new", "data": item["data"], "thumbnail": item["thumbnail"], "primary": bool(item.get("primary"))})
        else:
            raise ValueError("상품 사진 정보가 올바르지 않습니다.")
    return parsed


def product_image(product, eager=False):
    thumbnail_url = product["thumbnail_url"] if "thumbnail_url" in product.keys() else ""
    image_url = thumbnail_url or (product["image_url"] if "image_url" in product.keys() else "")
    if image_url:
        loading = "eager" if eager else "lazy"
        return f'<img src="{esc(image_url)}" alt="{esc(product["title"])} 상품 사진" loading="{loading}">'
    return f'<span>{esc(product["category"])}</span>'


def product_card(p, owner=False, viewer=None, csrf="", return_to="/"):
    owner_tools = f'<div class="mini-actions"><a href="/product/edit?id={p["id"]}">수정</a></div>' if owner else ""
    is_favorite = bool(p["is_favorite"]) if "is_favorite" in p.keys() else False
    favorite_control = ""
    favorite_count = p["favorite_count"] if "favorite_count" in p.keys() else 0
    chat_count = p["chat_count"] if "chat_count" in p.keys() else 0
    if not owner:
        label = "찜 취소" if is_favorite else "찜하기"
        symbol = "♥" if is_favorite else "♡"
        if viewer:
            favorite_control = f"""
            <form method="post" action="/favorite" class="card-favorite-form">
              {csrf}<input type="hidden" name="id" value="{p["id"]}"><input type="hidden" name="next" value="{esc(return_to)}">
              <button class="favorite-heart {'active' if is_favorite else ''}" aria-label="{label}" title="{label}">{symbol}</button>
            </form>
            """
        else:
            favorite_control = f'<a class="favorite-heart" href="/login?next={quote(return_to)}" aria-label="로그인 후 찜하기" title="로그인 후 찜하기">♡</a>'
    return f"""
    <article class="card">
      <div class="photo-wrap"><a class="photo" href="/product?id={p["id"]}">{product_image(p)}</a>{favorite_control}</div>
      <div class="card-body">
        <a class="card-detail-link" href="/product?id={p["id"]}" aria-label="{esc(p['title'])} 상세 보기">
          <h3>{esc(p["title"])}</h3>
          <p class="price">{won(p["price"])}</p>
          <p class="muted">{esc(p["location"])} · {p["distance_km"]:.1f}km · {esc(p["status"])} · <span title="등록일 {esc(p['created_at'])}">{relative_time(p["created_at"])}</span></p>
          <p class="product-stats">관심 {favorite_count} · 채팅 {chat_count} · 조회 {p["view_count"]}</p>
        </a>
        <div class="seller-line"><a href="/user?id={p["seller_id"]}">@{esc(p["username"])}</a>{rating_display(p["rating_average"], p["rating_count"], compact=True)}</div>
        {owner_tools}
      </div>
    </article>
    """


def user_card(u):
    return f"""
    <article class="user-card">
      {profile_avatar(u, "medium")}
      <div>
        <h3><a href="/user?id={u["id"]}">{esc(u["display_name"])}</a></h3>
        <p class="muted">@{esc(u["username"])} · {esc(u["location"])} · {esc(status_label(u["status"]))}</p>
      </div>
      {rating_display(u["rating_average"], u["rating_count"])}
    </article>
    """


def transaction_row(t, user, csrf, checklist_map=None):
    checklist_map = checklist_map or {}
    is_buyer = user["id"] == t["buyer_id"]
    is_seller = user["id"] == t["seller_id"]
    role = "구매자" if is_buyer else "판매자"
    payment_amount = t["payment_amount"]
    payment_summary = ""
    if payment_amount:
        payment_summary = (
            f'<p class="payment-complete"><strong>WM 포인트 {won(payment_amount)} 송금 완료</strong>'
            f' · {esc(t["payment_at"])} · <code>{esc(t["payment_reference"])}</code></p>'
        )
    action_form = ""
    if is_seller and t["status"] == "거래요청":
        action_form = f"""
        <div class="tx-actions">
          <form method="post" action="/transaction/status" class="inline">
            {csrf}<input type="hidden" name="id" value="{t["id"]}">
            <button class="primary" name="action" value="accept">요청 승인</button>
            <button name="action" value="reject">요청 거절</button>
          </form>
          <small>승인하면 상품이 자동으로 예약중으로 변경됩니다.</small>
        </div>
        """
    elif t["status"] == "예약중" and (is_buyer or is_seller):
        if is_buyer:
            payment_action = ""
            if not payment_amount:
                payment_action = f"""
                <form method="post" action="/transaction/payment" class="inline">
                  {csrf}<input type="hidden" name="id" value="{t["id"]}">
                  <button class="primary">WM 포인트 {won(t["trade_price"])} 송금</button>
                </form>
                """
            action_form = f"""
            <div class="tx-actions">
              {payment_action}
              <small>{'송금이 완료되었습니다.' if payment_amount else '가상 포인트 송금은 거래당 한 번만 가능합니다.'} 판매자가 거래 상태를 관리합니다.</small>
            </div>
            """
        else:
            action_form = f"""
            <div class="tx-actions">
              <form method="post" action="/transaction/status" class="inline">
                {csrf}<input type="hidden" name="id" value="{t["id"]}">
                <button name="action" value="reopen" {'disabled' if payment_amount else ''}>판매중으로 변경</button>
                <button class="primary" name="action" value="complete" {'disabled' if not payment_amount else ''}>거래 완료</button>
              </form>
              <small>{'송금이 완료되어 판매중으로 되돌릴 수 없습니다.' if payment_amount else '구매자 송금 후 거래 완료로 변경할 수 있습니다.'}</small>
            </div>
            """
    elif is_buyer and t["status"] == "거래요청":
        action_form = '<p class="muted">판매자가 거래 요청을 확인하는 중입니다.</p>'
    review_form = ""
    if t["status"] == "거래완료":
        counterpart = t["seller"] if is_buyer else t["buyer"]
        selected_rating = str(t["my_rating_score"] or 5)
        review_form = f"""
        <form method="post" action="/transaction/review" class="review">
          {csrf}
          <input type="hidden" name="id" value="{t["id"]}">
          <strong>{esc(counterpart)}님 평가</strong>
          <select name="rating" aria-label="별점">{''.join(option(str(i), f"{i}점", selected_rating) for i in range(1, 6))}</select>
          <input name="review" value="{esc(t["my_rating_review"] or '')}" maxlength="500" placeholder="거래 후기를 남겨주세요">
          <button>별점 저장</button>
        </form>
        """
    checklist = "".join(
        f"""
        <form method="post" action="/transaction/checklist" class="checklist-row">
          {csrf}<input type="hidden" name="id" value="{t["id"]}"><input type="hidden" name="item_key" value="{key}">
          <label><input type="checkbox" name="checked" value="1" data-auto-submit {"checked" if checklist_map.get((t["id"], key)) else ""}> {esc(label)}</label>
        </form>
        """
        for key, label in SAFETY_CHECKLIST
    )
    counterpart_id = t["seller_id"] if is_buyer else t["buyer_id"]
    return f"""
    <article class="tx">
      <div>
        <h2>{esc(t["title"])}</h2>
        <p><span class="status-badge">{esc(t["status"])}</span> · 내 역할: {role}</p>
        <p>{won(t["trade_price"])} · 판매자 {esc(t["seller"])} · 구매자 {esc(t["buyer"])}</p>
        <p class="muted">생성 {esc(t["created_at"])} · 수정 {esc(t["updated_at"])}</p>
        {payment_summary}
      </div>
      {action_form}
      <details class="safety-checklist"><summary>거래 전 안전 체크리스트</summary>{checklist}</details>
      <div class="tx-links"><a href="/transaction/evidence?id={t["id"]}">거래 증빙·이력</a><a href="/report?type=user&id={counterpart_id}&reason=사기 의심">사기 의심 신고</a><a href="/report?type=user&id={counterpart_id}&reason=노쇼">노쇼 신고</a></div>
      {review_form}
    </article>
    """


def notice_card(notice, user, csrf):
    admin_actions = ""
    if user and user["is_admin"]:
        admin_actions = f"""
        <div class="notice-actions">
          <a class="button" href="/notice/edit?id={notice["id"]}">수정</a>
          <form method="post" action="/notice/delete" class="inline">
            {csrf}<input type="hidden" name="id" value="{notice["id"]}">
            <button class="danger">삭제</button>
          </form>
        </div>
        """
    return f"""
    <article class="notice-card">
      <div><h2>{esc(notice["title"])}</h2><p>{esc(notice["body"])}</p><small>{esc(notice["display_name"])} · {esc(notice["created_at"])}</small></div>
      {admin_actions}
    </article>
    """


if __name__ == "__main__":
    validate_runtime_config()
    init_db()
    ensure_daily_backup()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8010"))
    print(f"White Market 실행 중: http://{host}:{port}")
    ThreadingHTTPServer((host, port), App).serve_forever()
