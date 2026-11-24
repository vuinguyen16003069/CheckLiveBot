# CheckLiveBot

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram 3](https://img.shields.io/badge/aiogram-3.x-26A5E4?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Redis 8](https://img.shields.io/badge/Redis-RAM%2090d%20TTL-DC382D?logo=redis&logoColor=white)](https://redis.io/)
[![Ruff](https://img.shields.io/badge/Code%20Style-Ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![Pyright Strict](https://img.shields.io/badge/Types-Pyright%20Strict-3178C6?logo=typescript&logoColor=white)](https://github.com/microsoft/pyright)
[![Deploy](https://img.shields.io/badge/Deploy-Docker%20%26%20Heroku-430098?logo=docker&logoColor=white)](https://www.docker.com/)

Telegram Bot kiểm tra trạng thái **LIVE / DIE UID Facebook** tốc độ cao và **theo dõi kèo tự động (`DIE → LIVE`)**, thiết kế theo kiến trúc **Single-Process Asyncio Modular Clean Architecture** tối giản phụ thuộc thư viện, dùng **Redis** lưu kèo (tự dọn sau `90` ngày) và **`curl_cffi`** giả lập TLS trình duyệt Chrome.

---

## Mục lục

- [Tính năng nổi bật](#tính-năng-nổi-bật)
- [Tech Stack (Tối giản phụ thuộc)](#tech-stack-tối-giản-phụ-thuộc)
- [Kiến trúc Modular Clean Architecture](#kiến-trúc-modular-clean-architecture)
- [Luồng hoạt động chi tiết](#luồng-hoạt-động-chi-tiết)
  - [1. Tổng quan hệ thống & vòng đời khởi chạy](#1-tổng-quan-hệ-thống--vòng-đời-khởi-chạy)
  - [2. Luồng Check UID: Singleflight, ProxyPool & phân loại chính xác](#2-luồng-check-uid-singleflight-proxypool--phân-loại-chính-xác)
  - [3. Luồng Bulk Check 10.000 – 50.000 UID](#3-luồng-bulk-check-10000--50000-uid)
  - [4. Luồng theo dõi kèo tự động `DIE → LIVE` (xác minh 2 lớp)](#4-luồng-theo-dõi-kèo-tự-động-die--live-xác-minh-2-lớp)
- [Bộ lệnh & Nút bấm Telegram](#bộ-lệnh--nút-bấm-telegram)
- [Lấy Redis miễn phí từ Upstash](#lấy-redis-miễn-phí-từ-upstash)
- [Hướng dẫn cấu hình `.env` chuẩn](#hướng-dẫn-cấu-hình-env-chuẩn)
- [Chạy Local, Docker Compose & Heroku](#chạy-local-docker-compose--heroku)
- [Quản trị Redis & Kiểm thử](#quản-trị-redis--kiểm-thử)

---

## Tính năng nổi bật

- **Dùng ngay cho chat riêng & group**: Dữ liệu cô lập tuyệt đối theo `chat_id`, kèm `ThrottlingMiddleware` chống spam theo cửa sổ trượt trong RAM (`0` lệnh Redis).
- **Check nhanh & Check hàng loạt tối đa `50.000` UID**:
  - Dán thẳng **1 UID / link Facebook** hoặc `/check <UID>` để kiểm tra tức thì.
  - Dán **nhiều dòng UID** hoặc gửi file `.txt` (tối đa `10 MB`) → parse & lọc trùng siêu nhanh (`50.000` UID trong `~20 ms`, đẩy sang worker thread với file lớn để không khựng bot) → quét song song bằng worker pool trong RAM (`0` overhead Redis) → cập nhật thanh tiến độ kèm tốc độ `UID/s`, `ETA` và nút **`🛑 Dừng check`** → xuất gọn **1 file** `result_<job_id>.txt`.
- **Độ chính xác cao & Chống chặn (Anti-Ban)**:
  - `curl_cffi` giả lập Chrome TLS JA3/JA4, chế độ **stateless** (`discard_cookies=True`, `allow_redirects=False`), split timeout `(connect, read)` để fail-fast khi proxy treo.
  - **Health-aware `ProxyPool`**: tự động đưa proxy lỗi/`429` vào cooldown ngắn (`15s → 60s`), ưu tiên xoay vòng qua proxy khỏe.
  - **Singleflight**: tự động gộp các request trùng UID đang bay cùng thời điểm thành 1 HTTP request duy nhất (dữ liệu luôn realtime 100%).
  - Phân loại chuẩn **3 trạng thái `LIVE` / `DIE` / `ERROR`**: các lỗi Graph API tạm thời / rate-limit (`is_transient=True`, `code` `1/2/4/17/32/341/613`) tự retry đổi proxy và trả `ERROR`, **tuyệt đối không báo nhầm `DIE`**.
- **Theo dõi kèo tự động `DIE → LIVE`**:
  - Lên kèo bằng **Wizard 4 bước** (`UID → Ghi chú → Giá → FAQ`) hoặc lệnh `/add UID | ghi chú | giá | FAQ`.
  - Vòng quét nền độ trễ thấp (`10s`), gom trùng UID giữa nhiều chat, **xác minh kép 2 lớp độc lập (`check` + `verify_live`)**, khóa `RedisLock` chống gửi đúp giữa quét nền & quét tay, cô lập lỗi theo từng chat để **không bao giờ mất thông báo LIVE**.
  - Tối ưu pipeline/`MGET` giảm 2–3× số round-trip Redis (phản hồi nhanh rõ rệt khi dùng Upstash).

---

## Tech Stack (Tối giản phụ thuộc)

Dự án chỉ giữ lại đúng **4 thư viện runtime cốt lõi** (+ `pydantic` đi kèm sẵn bởi `aiogram`), loại bỏ hoàn toàn các tầng phụ thuộc thừa (`structlog`, `pydantic-settings`, `python-dotenv`, `ARQ`):

| Thành phần | Công nghệ | Vai trò |
|---|---|---|
| **Runtime & Packaging** | Python `3.12+`, `uv`, `uvloop` *(Linux)* | Quản lý lockfile `uv.lock`; `uvloop` tăng tốc I/O 2–4× trên Linux/Docker/Heroku |
| **Telegram Bot** | `aiogram 3.x` | Async 100%, Long Polling, FSM `MemoryStorage` có khôi phục draft từ tin nhắn |
| **HTTP & Anti-Ban** | `curl_cffi` | Giả lập Chrome TLS JA3/JA4, HTTP/2, stateless cookie, HTTP/SOCKS5 proxy |
| **Lưu trữ & Khóa** | `redis` (`redis.asyncio`) | Lưu kèo (`String`/`Hash`/`Set`/`ZSet`, TTL 90 ngày) & `RedisLock` (`SET NX EX`) |
| **Config & Logging** | `pydantic` + Python stdlib (`logging`, `pathlib`) | Validate `.env` ngay khi khởi động; formatter `console` (dev) & `json` (prod) |
| **Quality Assurance** *(dev)* | `ruff`, `pyright` (strict), `pytest`, `fakeredis` | Lint, format, kiểm tra kiểu nghiêm ngặt và 49 unit tests chạy offline |

---

## Kiến trúc Modular Clean Architecture

Mã nguồn tách rõ **hạ tầng lõi dùng chung (`src/core/`)** và **các feature module độc lập (`src/modules/`)**. Mọi module đều tuân thủ chiều phụ thuộc một chiều: **`handlers → service → domain / core.ports`**.

```text
CheckLiveBot/
├── main.py                         # Entrypoint duy nhất: uvloop + Settings + logging + run_bot()
├── src/
│   ├── core/                       # Hạ tầng lõi & Port dùng chung
│   │   ├── config.py               # Settings (.env stdlib + Pydantic) & console/json logging
│   │   ├── ports.py                # CheckOutcome (LIVE/DIE/ERROR), UidCheckResult & Protocols
│   │   ├── redis.py                # Redis client (tự động TLS rediss:// cho Upstash) & RedisLock
│   │   ├── checker.py              # curl_cffi stateless + Health-aware ProxyPool + Singleflight
│   │   ├── middleware.py           # ThrottlingMiddleware (in-memory sliding-window rate limiter)
│   │   └── runtime.py              # Khởi chạy aiogram Dispatcher polling + vòng quét watcher nền
│   └── modules/                    # Feature modules độc lập
│       ├── watch/                  # Module 1: Theo dõi kèo DIE -> LIVE
│       │   ├── domain.py           # WatchItem, WatchStats, parser & format HTML card/list
│       │   ├── service.py          # WatchService, WatcherScanService & RedisWatchRepository
│       │   ├── ui.py               # Reply/Inline keyboards, Wizard FSM & CallbackData
│       │   └── handlers.py         # /start, /help, /add, /list, /stats, /cancel & callbacks
│       └── bulk/                   # Module 2: Check nhanh & Bulk file .txt (tối đa 50k UID)
│           ├── domain.py           # BulkSummary, fast-path parser, progress (UID/s, ETA) & file kết quả
│           ├── service.py          # BulkCheckService (iter worker pool, progress task, hủy job)
│           └── handlers.py         # /check, smart plain-text, nhận file .txt & TelegramBulkNotifier
├── test/
│   ├── test_core.py                # Config, checker, ProxyPool cooldown, Singleflight, RedisLock, loop
│   ├── test_watch.py               # Watch domain/UI/service, cô lập lỗi, lock, TTL 90d & filters
│   ├── test_bulk.py                # Parse 10k+ UID, BulkCheckService, hủy job & flood cooldown
│   └── test_redis.py               # Công cụ CLI quản lý/sao lưu Redis + unit test offline
├── Dockerfile                      # Multi-stage build siêu gọn bằng uv (--no-dev)
├── docker-compose.yml              # bot + redis 7 (AOF)
├── Procfile                        # Heroku: worker (python main.py)
├── .python-version                 # Ghim Python 3.12
├── .env.example
├── pyproject.toml                  # Dependencies tối giản + cấu hình ruff, pyright, pytest
└── uv.lock                         # Chốt phiên bản & SHA256 toàn bộ cây phụ thuộc
```

---

## Luồng hoạt động chi tiết

### 1. Tổng quan hệ thống & vòng đời khởi chạy

Khi chạy `python main.py`:
1. Kích hoạt `uvloop` (nếu chạy trên Linux/Docker/Heroku), nạp `.env` + `os.environ` qua `Settings`, khởi tạo `logging`.
2. Kết nối Redis và kiểm tra `PING` (nếu lỗi sẽ tự đóng kết nối sạch trước khi thoát).
3. Khởi tạo các thành phần dùng chung (`CurlCffiFacebookChecker`, `RedisLock`, `RedisWatchRepository`) và bơm vào `WatchService`, `BulkCheckService`, `WatcherScanService`.
4. Đăng ký `ThrottlingMiddleware`, gắn `watch_router` và `bulk_router` vào `Dispatcher`, khởi chạy song song **Telegram Long Polling** và **vòng quét nền `watcher-loop`** trong cùng 1 event loop.
5. Khi nhận tín hiệu dừng (`SIGINT`/`SIGTERM`), tắt tuần tự `watcher-loop` → hủy các job bulk đang chạy → đóng `AsyncSession`, `Bot`, `Redis`.

```mermaid
flowchart LR
    U["Telegram User / Group"] --> MW["ThrottlingMiddleware (RAM Sliding Window)"]
    MW --> WR["watch/handlers.py"]
    MW --> BR["bulk/handlers.py"]

    WR --> WS["WatchService"]
    BR --> BS["BulkCheckService (In-Memory Pool)"]
    LOOP["watcher-loop (mỗi 10s)"] --> SCAN["WatcherScanService"]

    WS --> REPO["RedisWatchRepository + RedisLock"]
    SCAN --> REPO
    REPO --> REDIS[("Redis / Upstash (TTL 90 ngày)")]

    WS --> CHK["CurlCffiFacebookChecker (Singleflight + ProxyPool)"]
    BS --> CHK
    SCAN --> CHK
    CHK --> FB["Facebook Graph Picture API"]

    SCAN -->|"Báo kèo LIVE (xác minh 2 lớp)"| U
    BS -->|"Progress (UID/s, ETA) + result.txt"| U
```

### 2. Luồng Check UID: Singleflight, ProxyPool & phân loại chính xác

Mỗi yêu cầu `check(uid)` đi qua 4 lớp bảo vệ để vừa đạt tốc độ tối đa vừa không báo sai:

```mermaid
flowchart TD
    IN["check(uid)"] --> SF{"UID này đang có\nrequest bay?"}
    SF -- Có --> WAIT["asyncio.shield(task)\nDùng chung kết quả realtime"]
    SF -- Không --> POOL["Health-aware ProxyPool\nChọn proxy khỏe (né proxy cooldown)"]
    POOL --> HTTP["curl_cffi AsyncSession\ndiscard_cookies=True | allow_redirects=False\ntimeout=(connect<=2s, read)"]
    HTTP --> ST{"HTTP Status & JSON"}

    ST -- "Timeout / 3xx / 403 / 429 / 5xx\nhoặc Graph transient (code 1,2,4,17,32,341,613)" --> FAIL["report_failure(proxy)\nCooldown 15s→60s + Retry jitter"]
    FAIL -- "Còn lượt retry" --> POOL
    FAIL -- "Hết lượt retry" --> ERR["CheckOutcome.ERROR\n(Không bao giờ báo nhầm DIE)"]

    ST -- "HTTP 404 / Graph error 100,803\nhoặc thiếu kích thước / silhouette" --> DIE["report_success(proxy)\nCheckOutcome.DIE"]
    ST -- "Avatar hợp lệ (int > 0, URL thật)" --> LIVE["report_success(proxy)\nCheckOutcome.LIVE"]
```

> **Lưu ý về xác minh kép:** Hàm `verify_live(uid)` chờ `VERIFY_DELAY_SECONDS` (`0.5s`) rồi gọi thẳng `_check_direct(uid)` (**bỏ qua Singleflight**) để đảm bảo lần kiểm tra thứ 2 luôn là một HTTP request độc lập thực sự.

### 3. Luồng Bulk Check 10.000 – 50.000 UID

```mermaid
sequenceDiagram
    participant U as User / Group
    participant H as bulk/handlers.py
    participant S as BulkCheckService
    participant P as Progress Reporter Task
    participant F as Facebook (curl_cffi + ProxyPool)

    U->>H: Dán nhiều dòng UID hoặc gửi file .txt (<= 10 MB)
    H->>H: Kiểm tra is_chat_busy(chat_id)
    alt File > 64 KB
        H->>S: asyncio.to_thread(prepare_bulk_text) (0ms block event loop)
    else Text / File nhỏ
        H->>S: prepare_bulk_text (fast-path isdigit + pre-compiled regex)
    end
    H-->>U: Gửi tin nhắn tiến độ 0% kèm nút "🛑 Dừng check"
    H->>S: start_bulk_uids (chạy nền, khóa 1 job/chat)

    par Worker Pool (iter(uids), tối đa CONCURRENCY)
        loop Từng UID trong iterator
            S->>F: check(uid) (retry + xoay proxy khỏe)
            F-->>S: LIVE / DIE / ERROR
        end
    and Background Progress Reporter (không chặn worker)
        loop Mỗi PROGRESS_UPDATE_INTERVAL_SECONDS (1.5s)
            P-->>U: editMessageText: [██████░░░░] 60% | ⚡ UID/s | ETA
            Note over P: Gặp TelegramRetryAfter → tự cooldown, không spam API
        end
    end

    opt Người dùng bấm "🛑 Dừng check"
        U->>H: CancelBulkCallback(job_id)
        H->>S: cancel_bulk_job() → hủy task & nhả khóa chat ngay
    end

    S-->>U: Xóa tin nhắn tiến độ + gửi 1 file result_<job_id>.txt + tóm tắt
```

### 4. Luồng theo dõi kèo tự động `DIE → LIVE` (xác minh 2 lớp)

```mermaid
flowchart TD
    ADD["/add hoặc Wizard 4 bước"] --> STORE["Lưu Redis (TTL 90d):\nwatch:{id}, watches:die, watches:chat:{id}, chat_uid:{id}"]
    TICK["watcher-loop (mỗi POLL_INTERVAL_SECONDS)"] --> LIST["SMEMBERS watches:die + MGET\n(tự dọn ID cũ/hết hạn)"]
    LIST --> DEDUPE["Gom nhóm theo UID duy nhất\n(N chat chung 1 UID chỉ check 1 lần)"]
    DEDUPE --> L1["Lớp 1 (trong Semaphore): check(uid)"]
    L1 -- "DIE hoặc ERROR" --> SKIP["Bỏ qua (0 lệnh ghi Redis)"]
    L1 -- "LIVE" --> L2["Lớp 2 (trong Semaphore): verify_live(uid)\nRequest độc lập sau 0.5s"]
    L2 -- "Không phải LIVE" --> SKIP
    L2 -- "Xác nhận LIVE" --> REL["Nhả Semaphore HTTP ngay"]
    REL --> LOCK["RedisLock: SET lock:notify:{id} NX EX 300\n(dùng chung với nút Quét lại DIE)"]
    LOCK -- "Đã có khóa" --> SKIP
    LOCK -- "Giữ được khóa" --> NOTIFY["Gửi TelegramWatchNotifier.notify_live(item)"]
    NOTIFY -- "Thành công" --> UPD["Cập nhật Redis: DIE → LIVE"]
    NOTIFY -- "Forbidden / BadRequest\n(chặn bot / xóa chat)" --> UPD
    NOTIFY -- "Lỗi mạng tạm thời" --> UNLOCK["release_lock(notify:{id})\nGiữ DIE để chu kỳ sau gửi lại"]
```

---

## Bộ lệnh & Nút bấm Telegram

| Lệnh / Thao tác | Mô tả |
|---|---|
| `/start` | Gắn bàn phím nhanh 2×2 ở đáy màn hình & mở bảng điều khiển tổng quan |
| `/check <UID \| link FB>` | Kiểm tra nhanh 1 UID hoặc link trang cá nhân Facebook |
| `/add [UID \| ghi chú \| giá \| FAQ]` | Gõ `/add` để mở Wizard 4 bước, hoặc thêm nhanh 1 dòng |
| `/list` | Danh sách kèo có lọc (`ALL`/`DIE`/`LIVE`/`DONE`/`HỦY`), phân trang & chạm thẳng UID xem chi tiết |
| `/stats` | Thống kê số lượng kèo trong 90 ngày gần nhất theo trạng thái |
| `/cancel` | Hủy thao tác Wizard đang nhập dở |
| `/help` | Xem hướng dẫn sử dụng chi tiết |
| **Dán thẳng 1 UID / link FB** | Tự động kiểm tra nhanh ngay không cần gõ lệnh |
| **Dán nhiều dòng UID / gửi file `.txt`** | Tự động chạy Bulk Check + tiến độ (`UID/s`, `ETA`) + nút `🛑 Dừng check` + trả file kết quả |

**Bộ nút Inline trên thẻ kèo & thông báo LIVE:**
- `🙈 Ẩn thông tin` / `🐵 Hiện thông tin` *(che mờ bằng spoiler theo từng chat, lưu 1 pipeline)*
- `❌ Hủy kèo` | `✅ Done kèo` | `✏️ Sửa` | `🗑 Xóa` *(có bước xác nhận)*
- `🔄 Tiếp tục theo dõi` | `🔙 Về danh sách` | `🔄 Quét lại DIE`

---

## Lấy Redis miễn phí từ Upstash

Nếu không muốn cài Redis trên máy hoặc dùng add-on trả phí, bạn có thể tạo **Upstash Redis (Free tier)** trong 2 phút:

1. Truy cập [https://console.upstash.com/](https://console.upstash.com/) và đăng nhập.
2. Chọn tab **Redis** → **Create Database**:
   - **Name**: `checklivebot`
   - **Primary Region**: chọn gần nhất (VD: Singapore `ap-southeast-1` cho Việt Nam).
   - Giữ **TLS** bật → bấm **Create**.
3. Trong mục **Connect** → copy chuỗi kết nối:
   ```text
   rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
   ```
4. Dán vào `REDIS_URL` trong `.env` (Local) hoặc Config Vars (Heroku). *(Nếu lỡ dán `redis://...upstash.io`, bot tự nâng cấp lên `rediss://` TLS.)*

---

## Hướng dẫn cấu hình `.env` chuẩn

Sao chép file mẫu trước khi điền thông tin:

```powershell
# Windows (PowerShell / CMD)
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

> [!WARNING]
> **Bảo mật:** File `.env` chứa `BOT_TOKEN` và mật khẩu Redis, đã được bỏ qua trong `.gitignore`. Không commit hoặc chia sẻ công khai file `.env`.

### 1. Lấy `BOT_TOKEN` từ Telegram (`@BotFather`)

1. Mở Telegram, tìm **[@BotFather](https://t.me/BotFather)** và gửi `/newbot`.
2. Đặt tên hiển thị và username (kết thúc bằng `bot`, VD: `my_checklive_bot`).
3. Copy token dạng `1234567890:ABCdef...` dán vào `BOT_TOKEN`.
4. *(Nếu dùng trong Group để dán thẳng UID)*: vào `@BotFather` → `/mybots` → chọn bot → **Bot Settings** → **Group Privacy** → **Turn off**.

### 2. Bảng tham chiếu biến môi trường

Toàn bộ cấu hình được định nghĩa và kiểm tra hợp lệ tại [`src/core/config.py`](src/core/config.py):

| Biến | Bắt buộc | Mặc định | Giới hạn | Mô tả |
|---|---|---|---|---|
| `BOT_TOKEN` | **Có** | `""` | Không rỗng | Token bot lấy từ `@BotFather` |
| `REDIS_URL` | **Có** | `redis://localhost:6379/0` | URL Redis | Chuỗi kết nối Redis local (`redis://...`) hoặc Upstash TLS (`rediss://...`) |
| `REDIS_KEY_PREFIX` | Không | `checklivebot` | Chuỗi | Tiền tố key Redis khi chạy nhiều bot chung 1 DB |
| `PROXY_LIST` | Không | `""` | Phân tách dấu `,` | Danh sách proxy xoay IP (`http://`, `socks5://`, `socks5h://`) |
| `CONCURRENCY` | Không | `100` | `1 – 500` | Số request check Facebook chạy đồng thời tối đa |
| `POLL_INTERVAL_SECONDS` | Không | `10` | `>= 3` | Chu kỳ quét nền các kèo `DIE` (giây) |
| `DATA_TTL_DAYS` | Không | `90` | `>= 1` | Thời gian lưu kèo trên Redis (ngày) |
| `HTTP_TIMEOUT_SECONDS` | Không | `5.0` | `> 0.5` | Timeout tối đa mỗi request Facebook (connect tự động `<= 2.0s`) |
| `MAX_RETRIES` | Không | `2` | `1 – 10` | Số lần retry (đổi proxy khỏe + backoff jitter) khi lỗi mạng / `403` / `429` / `5xx` |
| `VERIFY_DELAY_SECONDS` | Không | `0.5` | `0.0 – 5.0` | Độ trễ giữa lớp kiểm tra 1 và lớp xác minh kép 2 khi phát hiện `LIVE` |
| `USER_RATE_LIMIT_PER_MINUTE` | Không | `30` | `>= 0` | Giới hạn thao tác/phút mỗi user trong RAM (`0` để tắt) |
| `MAX_ACTIVE_WATCHES_PER_CHAT` | Không | `100` | `>= 0` | Số kèo `DIE` tối đa mỗi chat (`0` để không giới hạn) |
| `MAX_BULK_UIDS` | Không | `50000` | `>= 1` | Số UID tối đa mỗi lần check hàng loạt |
| `PROGRESS_UPDATE_INTERVAL_SECONDS` | Không | `1.5` | `>= 0.5` | Chu kỳ cập nhật thanh tiến độ Bulk Check (giây) |
| `LOG_LEVEL` | Không | `INFO` | `DEBUG\|INFO\|WARNING\|ERROR` | Mức độ chi tiết log |
| `LOG_FORMAT` | Không | `console` | `console \| json` | `console` (dev) hoặc `json` (chuẩn Docker/Cloud) |

### 3. Mẫu `.env` nhanh

**Local cơ bản (dùng Upstash Redis, không cần Docker):**
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
REDIS_URL=rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
PROXY_LIST=
```

**Local tải lớn có Proxy xoay IP (`http://` / `socks5h://`):**
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
REDIS_URL=rediss://default:<PASSWORD>@<ENDPOINT>.upstash.io:6379
PROXY_LIST=http://user:pass@ip1:8080,socks5h://user:pass@ip2:1080
CONCURRENCY=150
MAX_RETRIES=3
```

**Docker / Production:**
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
REDIS_URL=redis://redis:6379/0
PROXY_LIST=
LOG_LEVEL=INFO
LOG_FORMAT=json
```

---

## Chạy Local, Docker Compose & Heroku

### 1. Chạy Local
```powershell
uv sync
uv run python main.py
```

### 2. Chạy bằng Docker Compose (Bot + Redis 7 AOF)
```powershell
docker compose up -d --build
docker compose logs -f bot
```

### 3. Deploy lên Heroku (Worker Dyno)
Dự án dùng [`Procfile`](Procfile) (`worker: python main.py`):
```bash
heroku create ten-app-cua-ban
heroku config:set BOT_TOKEN="<token>" REDIS_URL="rediss://..." LOG_FORMAT="json"
git push heroku main
heroku ps:scale worker=1
heroku logs --tail
```

---

## Quản trị Redis & Kiểm thử

### Công cụ CLI chẩn đoán & sao lưu Redis
```powershell
# Mở menu tương tác (xem dung lượng, duyệt nhóm key, xóa key, export JSON)
uv run python test/test_redis.py

# Hoặc chạy nhanh theo cờ lệnh
uv run python test/test_redis.py --info
uv run python test/test_redis.py --export backup.json
```

### Kiểm thử, chất lượng mã nguồn & cập nhật thư viện
```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -v

# Cập nhật thư viện lên bản mới nhất
uv sync --upgrade
```
