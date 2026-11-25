# Kế hoạch triển khai TTL phân tầng cho Redis (`CheckLiveBot`)

- [x] Cập nhật `src/core/config.py` với bộ biến môi trường TTL phân tầng
- [x] Triển khai `WatchTtlPolicy`, cập nhật `RedisWatchRepository`, `WatchService`, `WatcherScanService` trong `src/modules/watch/service.py`
- [x] Cập nhật `src/core/runtime.py` và `src/modules/watch/domain.py`
- [x] Bổ sung `sync_existing_ttls` / `--sync-ttl` trong `test/test_redis.py` và cập nhật `README.md`
- [x] Cập nhật & bổ sung đầy đủ unit tests trong `test/test_watch.py`, `test/test_core.py`, `test/test_redis.py`
- [x] Chạy toàn bộ test suite & lint/typecheck để xác minh không có lỗi

## Đánh giá kết quả
- `WatchTtlPolicy` làm Single Source of Truth cho toàn bộ TTL phân tầng (`DIE=90d`, `LIVE=60d`, `DONE=30d`, `CANCELLED=14d`, `pref:hidden=365d`, `seq:watch_id=0` vĩnh viễn, `lock:notify=300s`).
- Đã vá rò rỉ bộ nhớ `chat_uid:{chat_id}` bằng `compact_chat_uid()` cơ hội và đảm bảo `/stats` khớp tuyệt đối `total == die + live + done + cancelled`.
- Đã thêm `--sync-ttl` trong `test/test_redis.py` để đồng bộ TTL cho dữ liệu cũ trên Upstash Redis.
- Kiểm thử: `ruff check`, `pyright` (strict), `pytest` (60/60) đều vượt qua không lỗi.
