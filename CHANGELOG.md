# Changelog

Ghi nhận các mốc thay đổi chính của **CheckLiveBot** (mới nhất ở trên).

## [1.3.0] — 2026-11-25 15:59–16:18 (`4e9ba6f` → `0cc6013`)
- **Fix lấy tên & ảnh công khai trên IP server:** Tách `_profile_session` sạch (`WhatsApp/2.23.20.0`, `default_headers=False`) gọi thẳng OpenGraph SSR, bỏ cache `(None, None)`, dừng đọc sớm khi đủ `og:title` + `og:image` (tối đa `256 KB`), lọc tiêu đề login tiếng Việt/Anh.
- **Loại bỏ fallback chồng chéo:** Chỉ gọi 1 URL duy nhất (`www.facebook.com/profile.php?id={uid}`), bỏ `m.facebook.com` và `twitter:*`; gửi ảnh Telegram trực tiếp bằng `BufferedInputFile` (hoặc tái dùng `avatar_file_id`), gộp helper `_resolve_watch_photo`; nút `🔄 Check lại` tự nâng cấp tin nhắn lên có ảnh.
- **Tăng cường test:** 49 → **65 unit tests** (độ phủ **97%**), bao phủ vòng đời `run_bot`, cache eviction, hủy task đang bay và các nhánh biên bulk/watch.

## [1.2.0] — 2026-11-25 00:18–11:57 (`fad0da6` → `3429a2d`)
- Thêm chính sách **TTL phân tầng Redis** (`WatchTtlPolicy`: DIE 90d, LIVE 60d, DONE 30d, CANCELLED 14d, pref 365d, seq vĩnh viễn) và lệnh `--sync-ttl` trong `test/test_redis.py`.
- Tinh gọn UI/handlers cho module `watch` và `bulk`.

## [1.1.0] — 2026-11-24 00:44–22:16 (`e312e08` → `e72303f`)
- Thêm lấy tên & avatar công khai, lưu `avatar_file_id` tái sử dụng.
- Thêm **Health-aware `ProxyPool`** (cooldown `15s → 60s`), **Singleflight** gộp request trùng UID, adaptive backoff `429` và **xác minh kép 2 lớp** (`check` + `verify_live`).
- Tối ưu Redis bằng `pipeline`/`MGET` (giảm 2–3× round-trip với Upstash) và mở rộng bộ unit tests.

## [1.0.1] — 2026-11-23 00:02–21:01 (`e23a9ae` → `3c2af8e`)
- Hoàn thiện `CurlCffiFacebookChecker`, **Bulk Check** tối đa 50.000 UID (`UID/s`, `ETA`, nút dừng job, xuất `result_<job_id>.txt`).
- Hoàn thiện **Wizard 4 bước** (`UID → Ghi chú → Giá → FAQ`), lọc/phân trang danh sách, spoiler ẩn thông tin và `🔄 Quét lại DIE`.
- Thêm CLI quản trị & sao lưu Upstash Redis (`test/test_redis.py`), cập nhật `Procfile` sang `worker: python main.py`.

## [1.0.0] — 2026-11-22 17:31–23:55 (`cd90d40` → `eaa11f0`)
- Khởi tạo **CheckLiveBot** theo kiến trúc **Modular Clean Architecture** (`src/core/`, `src/modules/bulk/`, `src/modules/watch/`).
- Thêm `ThrottlingMiddleware`, `RedisLock`, Docker/Compose/Heroku deploy và bộ QA (`ruff`, `pyright` strict, `pytest`).
