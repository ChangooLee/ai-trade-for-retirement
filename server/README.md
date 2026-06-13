# 동기화 API (구글 로그인 기기 간 동기화)

`sync_api.py` — 구글 ID 토큰을 검증해 사용자별 데이터(보유·현금흐름·매매일지·서킷브레이커 기준)를
본인 서버에 저장. 로그인 안 하면 브라우저 localStorage, 로그인하면 이 API로 동기화.

## 구성
- **백엔드**: `server/sync_api.py` (stdlib http.server + google-auth). `127.0.0.1:8799`.
  - `GET /api/userdata` · `PUT /api/userdata` (Authorization: Bearer <구글 ID 토큰>)
  - ID 토큰 로컬 검증(서명·만료·issuer·audience=GOOGLE_CLIENT_ID) → 숫자 sub 추출 → `state/userdata/{sub}.json`.
  - 본인 sub로만 read/write(교차 접근 불가), 원자적 저장, 2MB 상한.
- **systemd**: `server/leaders-sync.service` → `/etc/systemd/system/`. `GOOGLE_CLIENT_ID`를 Environment로 주입.
- **nginx**(sourceport.ai 443 블록): `location /trading/api/ { proxy_pass http://127.0.0.1:8799/api/; ... }`
  (정확매칭 `= /trading`·`= /trading/`보다 긴 prefix라 우선 매칭, `location /`(→:3000)로 새지 않음).
- **클라이언트**: `app/render/templates/trade_app.html`의 GIS 버튼(우상단). `build_webapp`가 `.env`의
  `GOOGLE_CLIENT_ID`를 `meta.google_client_id`로 주입. client_id 없으면 localStorage 폴백.

## 배포 (서버)
```bash
# 1) 코드 동기화 후
sudo cp server/leaders-sync.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now leaders-sync
sudo systemctl is-active leaders-sync          # active 확인
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8799/api/userdata   # 401 = 정상(무인증)

# 2) nginx: 443 server 블록의 '/trading/' 308 줄 뒤에 /trading/api/ location 추가 후
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w '%{http_code}' https://sourceport.ai/trading/api/userdata   # 401 = 라우팅 정상

# 3) .env에 GOOGLE_CLIENT_ID 설정 후 재빌드 → 페이지에 버튼 주입
.venv/bin/python -m app.batch.build_webapp --out /var/www/leaders/index.html
```

## OAuth (Google Cloud, 사용자 작업)
- 프로젝트 `leaders-desk-499303` · OAuth 동의화면 External + Test users에 본인 이메일.
- OAuth 클라이언트 ID(웹) · 승인된 JavaScript 원본 `https://sourceport.ai`. 리디렉션 URI 불필요.
- **클라이언트 보안 비밀(secret)은 이 방식(ID 토큰)에서 미사용** — client_id만 필요(공개값).

## 데이터/백업
- `state/userdata/{sub}.json` (gitignored). 백업하려면 이 디렉터리만 보관.
