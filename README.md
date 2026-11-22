# CheckLiveBot

Telegram Bot check LIVE/DIE UID Facebook theo chuẩn **Modular Clean Architecture** gọn nhẹ, dùng **Redis làm RAM chính**, hỗ trợ chạy **Local**, **Docker Compose** và **Heroku**.

## Tính năng chính
- **Mở cho mọi người dùng** (dùng được cả chat riêng và group, dữ liệu cô lập theo `chat_id`, có throttling chống spam trên Redis).
- **Check nhanh & Bulk 10.000 – 50.000 UID**: `/check <UID>`, `/bulk` hoặc gửi trực tiếp file `.txt` → đẩy vào **ARQ Queue**, kiểm tra song song (`curl_cffi` Chrome TLS JA3/JA4 + Proxy Pool + `tenacity` retry), tận dụng Redis Cache TTL (mặc định 10 phút), cập nhật **progress bar mỗi 2.5 giây** và trả file `live.txt`, `die.txt`, `error.txt`.
- **Theo dõi kèo tự động (`DIE → LIVE`)**: `/add <UID> | <ghi chú> | <giá> [| <FAQ>]`, `/addlist` (FSM nhiều dòng hoặc `.txt`), `/recheck`, `/list` (phân trang + lọc trạng thái), `/stats`, `/history`, nút inline (`🙈 Ẩn`, `🐵 Hiện`, `❌ Hủy`, `✅ Done`, `✅ Tiếp tục theo dõi`, `🔄 Kiểm tra lại`).
- **Chống báo sai**: Kết quả 3 trạng thái (`LIVE` / `DIE` / `ERROR`), không bao giờ báo sai khi lỗi mạng hay HTTP 429, khóa `SET NX EX` chống gửi trùng thông báo.

## Chạy Local

```powershell
# 1. Khởi chạy Redis bằng Docker (hoặc dùng Redis cài sẵn)
docker compose up -d redis

# 2. Tạo file .env và điền BOT_TOKEN
copy .env.example .env

# 3. Cài dependencies và chạy bot (kèm ARQ worker inline)
uv sync
uv run python main.py
```

## Chạy bằng Docker Compose

```powershell
# Chế độ gọn (1 container bot + inline worker + redis)
docker compose up -d --build

# Chế độ scale ngang (đặt RUN_INLINE_WORKER=false trong .env)
docker compose --profile scale up -d --build --scale worker=3
```

## Deploy lên Heroku

1. Tạo app và gắn add-on Redis:
   ```bash
   heroku create ten-app-cua-ban
   heroku addons:create heroku-redis:mini
   ```
2. Cấu hình Config Vars (Heroku tự cấp `PORT` và `REDIS_TLS_URL`):
   ```bash
   heroku config:set BOT_TOKEN="<token>"
   heroku config:set BOT_MODE="webhook"
   heroku config:set WEBHOOK_BASE_URL="https://ten-app-cua-ban.herokuapp.com"
   heroku config:set WEBHOOK_SECRET_TOKEN="<chuoi-bi-mat-32-ky-tu>"
   heroku config:set LOG_FORMAT="json"
   heroku config:set RUN_INLINE_WORKER="true"
   ```
3. Push code & kiểm tra health:
   ```bash
   git push heroku main
   curl https://ten-app-cua-ban.herokuapp.com/health
   ```
   *(Nếu muốn tách riêng worker dyno khi tải lớn: đặt `RUN_INLINE_WORKER=false` và chạy `heroku ps:scale web=1 worker=1`.)*

## Kiểm thử & Kiểm tra chất lượng

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -v
```
