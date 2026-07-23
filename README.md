# White Market

시큐어 코딩 과제용 중고거래 플랫폼입니다. 회원, 상품, 거래, 관리자, 공지사항 기능을 한 번에 확인할 수 있도록 Python 표준 라이브러리와 SQLite로 구현했습니다.

GitHub: https://github.com/i-Zer0/white-market-secure-coding

## 구현 기능

- 유저: 휴대전화 번호를 포함한 회원가입, 아이디 중복확인, GPS 기반 구 단위 동네 입력, 로그인, 본인인증 비밀번호 재설정, 프로필 사진을 포함한 마이페이지, 사용자 조회, 사용자 차단, 5점 만점 별점
- 소통: SSE 실시간 메시지 수신, 읽음·접속 상태, 채팅 이미지, 상품별 1대1 채팅, 가격 제안, 거래 장소·시간 약속과 변경 제안, 전체 변경 이력
- 상품: 등록, 수정, 삭제, 최소·최대 가격, 복수 카테고리, 거리·상태 복합 필터, 검색 기록·인기 검색어, 12개 단위 페이지네이션, 최대 8장 사진, 대표 사진·순서 지정, 상세 슬라이드, 별도 썸네일
- 개인화: 키워드 알림, 찜, 찜 상품 가격 인하 알림, 최근 본 상품, 판매자의 다른 상품 및 같은 카테고리 추천
- 거래: 구매자 요청, 판매자 승인·거절, 구매자 거래 완료, 안전 체크리스트, 노쇼·사기 의심 신고 연결, 채팅·제안·상태 증빙 이력
- 신고: 사용자·상품 신고, 중복 신고 방지, 관리자 처리 상태·메모, 전체 처리 이력 보관
- 관리자: 모든 회원 관리, 모든 상품 관리, 게시글 삭제, 계정 정지, 신고 관리, 관리자 감사 로그
- 공지사항: 관리자 공지 등록·수정·삭제, 공지 목록 조회
- 알림: 채팅·가격 인하·거래 상태·공지·보안 유형별 수신 설정, 읽지 않은 개수, 관련 상품·채팅 바로가기, 일괄 읽음 처리
- 로그인 보안: 접속 기기와 최근 로그인 기록, 새 환경 로그인 감지, 다른 세션 일괄 종료, TOTP 2단계 인증, 일회용 복구 코드와 사용 감사 기록, 휴대전화 본인인증 초기화
- 개인정보: 내 데이터 JSON 다운로드, 비밀번호와 2단계 인증 재확인 탈퇴, 상품 비공개, 계정 익명화, 탈퇴 감사 기록
- 관리자 통계: 회원·상품·거래·신고 현황, 미처리 신고 강조, 최근 14일 증가 추이, 정지·탈퇴 기록
- 운영: DB 일일 자동 백업·관리자 수동 백업과 복원, 오류 번호별 로그와 처리 상태, 오래된 읽은 알림 정리, 검색·채팅 DB 인덱스
- 자동 검사: GitHub Actions의 Python 컴파일·Bandit 정적 보안 검사·PR 의존성 검토
- 홈 화면: 통합 상품 검색, 카테고리 바로가기, WhiteHat School·BoB 교육 광고 슬라이드

## 보안 반영 사항

- 비밀번호는 PBKDF2-HMAC-SHA256과 사용자별 랜덤 salt로 저장합니다.
- SQL 쿼리는 파라미터 바인딩을 사용합니다.
- 로그인 세션은 서버 측 세션 테이블과 난수 토큰으로 관리합니다.
- POST 요청은 CSRF 토큰을 검증합니다.
- 사용자 입력은 HTML 출력 전에 escape 처리합니다.
- 프로필 사진은 브라우저에서 최대 512px JPEG로 다시 인코딩하고, 서버에서 JPEG 형식과 500KB 용량 제한을 재검증한 뒤 난수 파일명으로 저장합니다.
- 상품 사진은 브라우저에서 최대 1200px JPEG로 다시 인코딩하고, 서버에서 장수·형식·크기·900KB 용량을 다시 검증합니다.
- 채팅 사진도 브라우저에서 축소하고 서버에서 JPEG 구조·해상도·용량을 다시 검증합니다.
- 관리자 화면은 로그인과 관리자 권한을 모두 확인합니다.
- 정지 계정은 로그인할 수 없습니다.
- 존재하는 계정의 로그인 실패만 누적하며, 5회 실패하면 비밀번호 재설정 전까지 로그인을 차단합니다.
- 비밀번호 재설정은 아이디와 가입 시 등록한 휴대전화가 일치하고 6자리 일회용 인증번호 검증을 통과해야 합니다.
- 인증번호는 5분 뒤 만료되고 최대 5회까지만 입력할 수 있으며, 재발급 요청은 1분 간격으로 제한합니다.
- 회원 아이디는 영문과 숫자를 포함한 5-20자로 제한하고, 대소문자를 구분하지 않고 중복을 검사합니다.
- `admin`은 대소문자와 관계없이 일반 회원 아이디로 사용할 수 없습니다.
- 신규 비밀번호는 영문, 숫자, 특수문자를 포함한 8자 이상으로 검증합니다.
- 회원가입 시 개인정보 수집 및 이용 동의를 필수로 확인합니다.
- 일회성·5분 만료 계산형 보안문자와 숨김 입력 필드로 자동 가입을 방어합니다.
- 거래 상태는 역할별 허용 단계에서만 서버가 변경하며, 사용자가 임의 상태를 선택할 수 없습니다.
- 별점은 실제 채팅 이력이 있거나 완료된 거래에 참여한 사용자만 상대방에게 남길 수 있습니다.
- 상품 조회수는 로그인 사용자 또는 익명 브라우저별로 하루에 한 번만 증가하며, 익명 식별값에는 원본 IP를 저장하지 않습니다.
- 상품의 채팅 수는 해당 상품을 기준으로 판매자에게 문의한 고유 사용자 수로 계산해 반복 메시지가 통계를 부풀리지 않도록 합니다.
- 신고 생성·상태 변경 이력을 별도 테이블에 보관하고 공지·회원·상품·신고 관련 관리자 작업은 감사 로그로 남깁니다.
- 가격 제안과 거래 약속은 참여자·상품 소유 관계를 서버에서 검증하고 생성·수락·거절 과정을 변경 이력 테이블에 보관합니다.
- 로그인 세션에는 생성·최근 활동·IP·User-Agent를 기록하며, 새 환경 로그인은 보안 알림으로 전달합니다.
- 2단계 인증은 표준 TOTP 방식과 ±30초 시간 오차 검증을 사용하며 활성화·해제 시 현재 비밀번호를 다시 확인합니다.
- 복구 코드는 원문 대신 SHA-256 해시만 저장하고 각 코드는 한 번만 사용할 수 있으며 사용 시각과 IP를 감사 기록에 남깁니다.
- 로그인·본인인증·채팅·검색·신고·이미지 업로드는 IP와 계정 기준 요청 횟수를 제한하며 초과 시 HTTP 429를 반환합니다.
- CSP, `X-Frame-Options`, `Referrer-Policy`, `Cross-Origin-Opener-Policy` 보안 헤더를 모든 화면에 적용합니다.
- DB 백업은 환경 변수의 키로 AES-GCM 암호화하며 복원 전에 인증 태그와 SQLite 무결성 검사를 모두 확인합니다.
- 회원 탈퇴는 계정 행을 삭제하는 대신 식별 정보와 인증 정보를 익명화해 기존 거래·신고 기록의 참조 무결성을 유지합니다.
- 홈 화면의 사용자 요약과 공지사항을 분리해 개인 정보와 운영 공지를 구분합니다.

## 휴대전화 본인인증 설정

로컬 개발 환경에서는 기능 확인을 위해 인증번호가 결과 화면에 표시됩니다. 실제 배포에서는 화면에 인증번호가 노출되지 않도록 `SMS_WEBHOOK_URL`을 설정해야 합니다. 서버는 아래 JSON을 해당 주소에 `POST`합니다.

```json
{
  "phone": "01012345678",
  "code": "123456",
  "service": "White Market"
}
```

```powershell
$env:SMS_WEBHOOK_URL="https://your-sms-service.example/send"
python app.py
```

SMS 공급자 측에서도 발송 빈도 제한, 요청 인증, 전송 구간 암호화와 발송 로그의 개인정보 마스킹을 적용해야 합니다.

## GPS 동네 찾기

회원가입 화면의 `현재 위치` 버튼을 누르면 브라우저가 위치 권한을 요청하고, 서버가 좌표를 구 단위 동네로 변환합니다. 좌표는 회원 데이터베이스에 저장하지 않습니다.

기본 역지오코딩 서비스는 OpenStreetMap Nominatim 공개 서버입니다. 공개 서버 정책에 따라 호출을 초당 1회 이하로 제한하고 결과를 메모리에 캐시합니다. 배포 환경에서는 아래 환경 변수로 별도 제공자 또는 자체 Nominatim 서버와 식별용 User-Agent를 설정하세요.

```powershell
$env:GEOCODING_BASE_URL="https://your-geocoder.example"
$env:GEOCODING_USER_AGENT="WhiteMarket/1.0 (contact@example.com)"
python app.py
```

- Nominatim 사용 정책: https://operations.osmfoundation.org/policies/nominatim/
- OpenStreetMap 저작권 및 라이선스: https://www.openstreetmap.org/copyright

## 홈 광고 이미지 출처

홈의 WhiteHat School과 BoB 광고는 각 프로그램의 공식 홈페이지 로고와 메인 일러스트를 사용한 과제용 시안입니다. 이미지와 브랜드에 관한 권리는 각 운영 기관에 있으며, 광고를 누르면 해당 공식 홈페이지로 이동합니다.

- WhiteHat School: https://whsedu.kr/
- Best of the Best: https://bobedu.kr/

## 실행 방법

Python 3.12 이상과 Git이 필요합니다. 아래 명령은 저장소를 처음 내려받는 경우를 기준으로 합니다.

초기 관리자 비밀번호는 영문·숫자·특수문자를 포함한 8자 이상으로 입력해야 합니다. 백업 암호화 키는 관리자 비밀번호와 다른 16자 이상의 값을 사용하세요.

### PowerShell

```powershell
git clone https://github.com/i-Zer0/white-market-secure-coding.git
cd white-market-secure-coding

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$adminPassword = Read-Host "초기 관리자 비밀번호" -AsSecureString
$backupKey = Read-Host "16자 이상의 백업 암호화 키" -AsSecureString
$env:WHITE_MARKET_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new("", $adminPassword).Password
$env:WHITE_MARKET_BACKUP_KEY = [System.Net.NetworkCredential]::new("", $backupKey).Password

python app.py
```

PowerShell의 실행 정책 때문에 가상환경 활성화가 차단되면 현재 창에서만 다음 명령을 먼저 실행합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### WSL

Ubuntu 계열 WSL에서 `python3-venv`가 없다면 먼저 `sudo apt install python3-venv`로 설치합니다.

```bash
git clone https://github.com/i-Zer0/white-market-secure-coding.git
cd white-market-secure-coding

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

read -rsp "초기 관리자 비밀번호: " WHITE_MARKET_ADMIN_PASSWORD
echo
read -rsp "16자 이상의 백업 암호화 키: " WHITE_MARKET_BACKUP_KEY
echo
export WHITE_MARKET_ADMIN_PASSWORD WHITE_MARKET_BACKUP_KEY

python app.py
```

서버가 시작되면 Windows 브라우저에서 아래 주소를 엽니다. 서버 종료는 실행한 터미널에서 `Ctrl+C`를 누릅니다.

```text
http://127.0.0.1:8010
```

HTTPS 리버스 프록시 뒤에 배포할 때는 아래 설정을 추가합니다. 세션 쿠키에 `Secure`가 적용되고 HSTS가 활성화됩니다.

```powershell
$env:WHITE_MARKET_HTTPS="1"
```

```bash
export WHITE_MARKET_HTTPS=1
```

## 초기 관리자 계정

첫 실행 시 아이디 `admin`과 `WHITE_MARKET_ADMIN_PASSWORD` 값으로 관리자 계정을 만듭니다. 기본 비밀번호는 코드에 존재하지 않으며 최초 로그인 직후 반드시 새 비밀번호로 변경해야 합니다.
