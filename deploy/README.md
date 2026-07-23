# 운영 배포 연습

## 준비

1. 도메인의 A/AAAA 레코드를 서버 주소로 연결합니다.
2. `.env.production.example`을 `.env.production`으로 복사하고 모든 예시 값을 교체합니다.
3. 방화벽에서 TCP 80/443과 UDP 443을 허용합니다.
4. SMS 공급자의 HTTPS Webhook을 `SMS_WEBHOOK_URL`에 설정합니다.

## 실행

```powershell
docker compose config
docker compose up -d --build
docker compose logs -f app caddy
```

Caddy는 도메인 DNS가 연결된 뒤 TLS 인증서를 자동 발급하고 갱신합니다. 앱은 운영 모드에서 HTTPS 플래그, 실제 SMS Webhook, 관리자 초기 비밀번호, 백업 암호화 키가 모두 유효해야 시작됩니다. 따라서 개발용 인증번호가 응답 화면에 노출되는 설정으로는 운영 서버를 시작할 수 없습니다.

## 운영 점검

```powershell
docker compose ps
docker compose exec app python -m py_compile app.py
curl.exe -I https://your-domain.example/
```

응답에서 HSTS, CSP, X-Frame-Options, Referrer-Policy를 확인하고 관리자 최초 로그인 직후 비밀번호를 변경합니다. `.env.production`, `market.db`, 업로드 이미지, 암호화 백업은 Git에 추가하지 않습니다.
