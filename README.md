# CheckLiveBot

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram 3](https://img.shields.io/badge/aiogram-3.x-26A5E4?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Redis RAM](https://img.shields.io/badge/Redis-RAM%20%2B%20ARQ-DC382D?logo=redis&logoColor=white)](https://redis.io/)
[![Ruff](https://img.shields.io/badge/Code%20Style-Ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![Heroku Ready](https://img.shields.io/badge/Deploy-Heroku%20%26%20Docker-430098?logo=heroku&logoColor=white)](https://www.heroku.com/)

Telegram Bot kiểm tra trạng thái **LIVE / DIE UID Facebook** tốc độ cao và **theo dõi kèo tự động (`DIE → LIVE`)**, thiết kế theo chuẩn **Modular Clean Architecture**, sử dụng **Redis làm RAM chính** và sẵn sàng chạy trên **Local**, **Docker Compose** và **Heroku**.

---

## Mục lục
- [Tính năng nổi bật](#tính-năng-nổi-bật)
- [Tech Stack](#tech-stack)
- [Kiến trúc Modular Clean Architecture](#kiến-trúc-modular-clean-architecture)
- [Luồng hoạt động](#luồng-hoạt-động)
- [Bộ lệnh & Nút bấm Telegram](#bộ-lệnh--nút-bấm-telegram)
- [Lấy Redis miễn phí từ Upstash](#lấy-redis-miễn-phí-từ-upstash)
- [Hướng dẫn cấu hình `.env` chuẩn](#hướng-dẫn-cấu-hình-env-chuẩn)
- [Chạy Local, Docker & Deploy Heroku](#chạy-local-docker--deploy-heroku)
- [Kiểm thử & Chất lượng mã nguồn](#kiểm-thử--chất-lượng-mã-nguồn)

---

## Tính năng nổi bật

- **Mở cho mọi người dùng**: Dùng ngay trong chat riêng hoặc group; dữ liệu cô lập hoàn toàn theo `chat_id` kèm `ThrottlingMiddleware` chống spam trên Redis.
- **Check hàng loạt 10.000 – 50.000 UID**: Gửi `/bulk` hoặc ném thẳng file `.txt` → lọc trùng UID → đẩy job vào **ARQ Worker** → cập nhật thanh tiến độ (**progress bar**) mỗi `2.5s` tránh rate-limit Telegram → xuất file `live.txt`, `die.txt`, `error.txt`.
- **Chống chặn (Anti-Ban) & Chống báo sai**: Dùng `curl_cffi` giả lập TLS JA3/JA4 chuẩn Chrome + xoay vòng `HTTP`/`SOCKS5`/`SOCKS5h` Proxy + `tenacity` retry backoff. Phân loại 3 trạng thái `LIVE` / `DIE` / `ERROR` — không bao giờ báo sai khi lỗi mạng hay `429`.
- **Theo dõi kèo `DIE → LIVE`**: Cron quét nền định kỳ trên Redis RAM, khóa `SET NX EX` chống gửi trùng thông báo, gắn sẵn bộ nút thao tác kèo (`🙈 Ẩn`, `🐵 Hiện`, `❌ Hủy`, `✅ Done`, `✅ Tiếp tục theo dõi`, `🔄 Kiểm tra lại`).
- **Sẵn sàng Production**: Tự kích hoạt `uvloop` trên Linux/Docker/Heroku, tự hỗ trợ `rediss://` TLS (Upstash / Heroku Key-Value Store), có endpoint `GET /health` và xác thực `X-Telegram-Bot-Api-Secret-Token`.

---

## Tech Stack

| Thành phần | Công nghệ | Vai trò |
|---|---|---|
| **Runtime & Packaging** | Python `3.12+`, `uvloop`, `uv` | Tăng throughput I/O 2–4× trên Linux/Docker/Heroku, quản lý lockfile chuẩn |
| **Telegram Bot** | `aiogram 3.x`, `aiohttp` | Async 100%, FSM `RedisStorage`, Polling (Local) & Webhook + `/health` (Prod) |
| **Network & Anti-Ban** | `curl_cffi`, `tenacity` | Giả lập TLS Fingerprint Chrome, hỗ trợ HTTP/SOCKS5h proxy, retry exponential backoff |
| **RAM Store & Queue** | `redis.asyncio`, `arq` | Watch Repo (`Hash`/`Set`/`ZSet`/`List`), Cache UID TTL, Lock, Rate-Limit, Background Queue |
| **Config & Logging** | `pydantic-settings`, `structlog` | Validate `.env` ngay khi boot; log màu ở dev, JSON chuẩn `stdout` ở Docker/Heroku |
| **Quality Assurance** | `ruff`, `pyright`, `pytest`, `fakeredis` | Lint, format, type-check chuẩn Clean Architecture và unit/integration test |

---

## Kiến trúc Modular Clean Architecture

Mã nguồn tách rõ **hạ tầng lõi dùng chung (`src/core/`)** và **các feature module độc lập (`src/modules/`)**. Mọi module đều tuân thủ chiều phụ thuộc một chiều: **`handlers → service → domain / core.ports`**.

```text
CheckLiveBot/
├── main.py                         # Entrypoint duy nhất: uvloop + Settings + structlog + run_bot()
├── src/
│   ├── core/                       # Hạ tầng lõi & Port dùng chung
│   │   ├── config.py               # Settings (.env) + structlog
│   │   ├── ports.py                # CheckOutcome (LIVE/DIE/ERROR), UidCheckResult & Protocols
│   │   ├── redis.py                # Redis RAM (tự động TLS rediss:// cho Upstash) & RedisLock
│   │   ├── checker.py              # curl_cffi (Chrome JA3/JA4) + ProxyPool + retry backoff jitter
│   │   ├── middleware.py           # ThrottlingMiddleware (in-memory sliding-window rate limiter)
│   │   └── runtime.py              # Khởi chạy aiogram Dispatcher polling + vòng quét watcher nền
│   └── modules/                    # Feature modules độc lập
│       ├── watch/                  # Module 1: Theo dõi kèo DIE -> LIVE
│       │   ├── domain.py           # WatchItem, WatchStats, parser & format HTML card/list
│       │   ├── service.py          # WatchService, WatcherScanService & RedisWatchRepository
│       │   ├── ui.py               # Reply/Inline keyboards, Wizard FSM & CallbackData
│       │   └── handlers.py         # /start, /help, /add, /list, /stats, /cancel & callbacks
│       └── bulk/                   # Module 2: Check nhanh & Bulk file .txt (tối đa 50k UID)
│           ├── domain.py           # BulkSummary, tách/deduplicate UID/URL FB, progress bar & xuất file
│           ├── service.py          # BulkCheckService (worker pool RAM, hủy job, guard theo chat)
│           └── handlers.py         # /check, smart plain-text, nhận file .txt & TelegramBulkNotifier
├── test/
│   ├── test_core.py                # Config, curl_cffi + ProxyPool, RedisLock, middleware & watcher loop
│   ├── test_watch.py               # Watch domain/UI/service, cô lập lỗi, lock & TTL 90 ngày
│   ├── test_bulk.py                # Parse 10k+ UID, BulkCheckService, hủy job & flood cooldown
│   └── test_redis.py               # Công cụ CLI quản lý/sao lưu Redis + unit test offline
├── Dockerfile                      # Multi-stage build siêu gọn bằng uv
├── docker-compose.yml              # bot + redis 7 (AOF)
├── Procfile                        # Heroku: worker (python main.py)
├── .python-version                 # Ghim Python 3.12
├── .env.example
└── pyproject.toml                  # uv, ruff, pyright, pytest
```

---

## Luồng hoạt động

### 1. Tổng quan hệ thống
```mermaid
flowchart LR
    U["Telegram User / Group"] --> H["modules/*/handlers.py (aiogram 3 + Throttle)"]
    H --> S["modules/*/service.py"]
    S --> R["core/redis.py (Redis RAM + Cache TTL + ARQ)"]
    W["main.py (ARQ Worker inline / Standalone)"] --> R
    W --> C["core/checker.py (curl_cffi + ProxyPool + tenacity)"]
    W -->|"Progress 2.5s / File kết quả / Báo kèo LIVE"| U
```

### 2. Luồng Bulk Check File `.txt` (10.000 – 50.000 UID)
```mermaid
sequenceDiagram
    participant U as User / Group
    participant B as Bot (handlers.py)
    participant R as Redis RAM + ARQ
    participant W as ARQ Worker
    participant F as Facebook (curl_cffi + Proxy)

    U->>B: Gửi file .txt hoặc /bulk
    B->>B: Tách & lọc trùng UID, tạo job_id
    B->>R: Lưu payload UID (TTL 1h) & enqueue ARQ job
    B-->>U: Phản hồi ngay: "Đã nhận N UID (Job #id)"
    W->>R: Nhận job & MGET check_cache (TTL 10p)
    loop Quét song song theo asyncio.Semaphore(CONCURRENCY)
        W->>F: GET graph.facebook.com/{uid}/picture?redirect=false
        F-->>W: LIVE / DIE / ERROR (retry + xoay proxy khi 429/timeout)
        W->>R: Lưu cache LIVE/DIE vào Redis
        opt Mỗi 2.5 giây (throttle)
            W-->>U: editMessageText thanh tiến độ [██████░░░░] 60%
        end
    end
    W-->>U: Gửi file live.txt, die.txt, error.txt + Tóm tắt
```

### 3. Luồng Cron theo dõi kèo (`DIE → LIVE`)
- Khi `/add` hoặc `/addlist`, kèo `DIE` được đưa vào Redis Set `watches:die` và ZSet phân trang `watches:chat:{chat_id}`.
- ARQ Cron quét song song tập `watches:die` định kỳ:
  - Gặp `ERROR` (lỗi mạng / `429`) → bỏ qua, **không báo sai**.
  - Chuyển `DIE → LIVE` → khóa `SET lock:notify:{id} NX EX 300` chống gửi trùng → cập nhật trạng thái `LIVE` → gửi thông báo kèm thời gian về LIVE và bộ nút inline.

---

## Bộ lệnh & Nút bấm Telegram

| Lệnh | Mô tả |
|---|---|
| `/start` | Mở Menu chính 2×2 |
| `/help` | Xem hướng dẫn sử dụng chi tiết |
| `/check <UID \| link FB>` | Kiểm tra nhanh 1 UID (tận dụng Redis Cache RAM) |
| `/bulk` *(hoặc gửi thẳng file `.txt`)* | Check hàng loạt tối đa 50.000 UID qua ARQ Worker + progress bar + trả file kết quả |
| `/add <UID> \| <ghi chú> \| <giá> [\| <FAQ>]` | Thêm 1 kèo theo dõi, kiểm tra tức thì và gắn bộ nút quản lý |
| `/addlist` | Thêm nhiều kèo cùng lúc (FSM nhận text nhiều dòng hoặc file `.txt`) |
| `/recheck [UID]` | Quét lại ngay các kèo đang `DIE` trong chat |
| `/list` | Danh sách kèo có lọc trạng thái (`ALL`/`DIE`/`LIVE`/`DONE`/`CANCELLED`) và phân trang `◀️` / `▶️` |
| `/stats` | Thống kê số lượng kèo theo từng trạng thái |
| `/history` | Xem 10 sự kiện biến động gần nhất |

**Bộ nút Inline trên thẻ kèo & thông báo LIVE**:
- Hàng 1: `🙈 Ẩn thông tin` | `🐵 Hiện thông tin`
- Hàng 2: `❌ Hủy kèo` | `✅ Done kèo`
- Hàng 3: `✅ Tiếp tục theo dõi` *(hoặc `🔄 Kiểm tra lại` khi đang `DIE`)*

---

## Lấy Redis miễn phí từ Upstash

Nếu không muốn cài Redis trên máy hoặc dùng add-on trả phí trên Heroku, bạn có thể dùng **Upstash Redis (Free tier)**:

1. Truy cập [https://console.upstash.com/](https://console.upstash.com/) và đăng nhập (Google / GitHub).
2. Chọn tab **Redis** → bấm **Create Database**:
   - **Name**: `checklivebot`
   - **Primary Region**: chọn gần nhất (VD: Singapore `ap-southeast-1` cho Việt Nam, hoặc cùng region với Heroku app).
   - Giữ **TLS** bật → bấm **Create**.
3. Trong mục **Connect** → chuyển sang tab **`redis-cli`** hoặc **`Python`** để copy chuỗi kết nối:
   ```text
   rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
   ```
4. Dán thẳng vào `REDIS_URL` trong `.env` (Local) hoặc Config Vars (Heroku). Bot đã tự hỗ trợ sẵn TLS `rediss://` cho cả Redis RAM lẫn ARQ Worker.

---

## Hướng dẫn cấu hình `.env` chuẩn

Sao chép file mẫu trước khi điền thông tin:
```powershell
copy .env.example .env
```

### 1. Lấy `BOT_TOKEN` từ Telegram
1. Mở Telegram, tìm **[@BotFather](https://t.me/BotFather)** và gửi `/newbot`.
2. Đặt tên hiển thị và username (kết thúc bằng `bot`, VD: `my_checklive_bot`).
3. Copy chuỗi token dạng `1234567890:ABCdef...` dán vào `BOT_TOKEN`.

### 2. Bảng tham chiếu biến môi trường

| Biến | Bắt buộc? | Mặc định | Mô tả |
|---|---|---|---|
| `BOT_TOKEN` | **Có** | — | Token bot lấy từ `@BotFather` |
| `REDIS_URL` | **Có** | `redis://localhost:6379/0` | URL kết nối Redis (`rediss://...` từ Upstash hoặc Redis local). Trên Heroku tự ưu tiên `REDIS_TLS_URL` nếu có |
| `PROXY_LIST` | Không | `[]` | Danh sách proxy xoay IP (`http://`, `socks5://`, `socks5h://`), hỗ trợ mảng JSON `["..."]` hoặc dấu phẩy |
| `BOT_MODE` | Khi lên Heroku | `polling` | `polling` cho Local; `webhook` cho Heroku / server có HTTPS |
| `WEBHOOK_BASE_URL` | Khi `webhook` | — | Domain app Heroku, VD: `https://ten-app.herokuapp.com` (không có `/` cuối) |
| `WEBHOOK_SECRET_TOKEN` | Khuyến nghị | — | Chuỗi bí mật bất kỳ (32+ ký tự) bảo vệ endpoint `/webhook` |
| `RUN_INLINE_WORKER` | Không | `true` | `true`: chạy gọn Bot + ARQ Worker trong 1 process/dyno; `false`: tách worker bằng `arq main.WorkerSettings` |
| `CONCURRENCY` | Không | `50` | Số request check Facebook chạy đồng thời tối đa |
| `POLL_INTERVAL_SECONDS` | Không | `30` | Chu kỳ quét nền các kèo `DIE` (giây) |
| `CHECK_CACHE_TTL_SECONDS` | Không | `600` | Thời gian cache kết quả check UID trên RAM (giây — mặc định 10 phút) |
| `HTTP_TIMEOUT_SECONDS` | Không | `10` | Timeout mỗi request Facebook (giây) |
| `MAX_RETRIES` | Không | `3` | Số lần retry (tự đổi proxy) khi lỗi mạng / `429` / `5xx` |
| `USER_RATE_LIMIT_PER_MINUTE` | Không | `30` | Số thao tác tối đa/phút mỗi user (`0` để tắt) |
| `MAX_ACTIVE_WATCHES_PER_CHAT` | Không | `100` | Số kèo `DIE` tối đa mỗi chat (`0` để tắt) |
| `MAX_BULK_UIDS` | Không | `50000` | Số UID tối đa mỗi lần check file `.txt` / `/bulk` |
| `LOG_FORMAT` | Không | `console` | `console` (in màu ở Local) hoặc `json` (chuẩn Docker/Heroku) |
| `LOG_LEVEL` | Không | `INFO` | Mức log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### 3. Mẫu `.env` nhanh

**Local cơ bản (dùng Upstash Redis, không cần Docker):**
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
REDIS_URL=rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
PROXY_LIST=[]
```

**Local có Proxy xoay IP (`socks5h://` / `http://`):**
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
REDIS_URL=rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
PROXY_LIST=["socks5h://user:pass@ip1:1080","http://user:pass@ip2:8080"]
CONCURRENCY=50
```

**Heroku Config Vars (`PORT` do Heroku tự cấp):**
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
REDIS_URL=rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
BOT_MODE=webhook
WEBHOOK_BASE_URL=https://ten-app-cua-ban.herokuapp.com
WEBHOOK_SECRET_TOKEN=thay-bang-chuoi-ngau-nhien-dai-32-ky-tu
LOG_FORMAT=json
RUN_INLINE_WORKER=true
```

---

## Chạy Local, Docker & Deploy Heroku

### Chạy Local
```powershell
uv sync
uv run python main.py
```

### Chạy bằng Docker Compose
```powershell
# Chế độ gọn (bot + inline worker + redis AOF)
docker compose up -d --build

# Chế độ scale ngang (đặt RUN_INLINE_WORKER=false trong .env)
docker compose --profile scale up -d --build --scale worker=3
```

### Deploy lên Heroku
```bash
heroku create ten-app-cua-ban
# Dùng Upstash REDIS_URL hoặc gắn add-on: heroku addons:create heroku-redis:mini
heroku config:set BOT_TOKEN="<token>" REDIS_URL="rediss://..." BOT_MODE="webhook" \
  WEBHOOK_BASE_URL="https://ten-app-cua-ban.herokuapp.com" \
  WEBHOOK_SECRET_TOKEN="<chuoi-bi-mat-32-ky-tu>" LOG_FORMAT="json" RUN_INLINE_WORKER="true"

git push heroku main
curl https://ten-app-cua-ban.herokuapp.com/health
```
*(Khi tải lớn cần tách riêng worker dyno: đặt `RUN_INLINE_WORKER=false` và chạy `heroku ps:scale web=1 worker=1`.)*

---

## Kiểm thử & Chất lượng mã nguồn

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -v
```
