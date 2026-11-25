# Fix: Không lấy được tên & ảnh đại diện Facebook dù công khai

## Nguyên nhân gốc rễ
1. **Header trình duyệt Chrome (`impersonate="chrome"` + `Sec-Fetch-*`) trên IP datacenter (Heroku/VPS/Docker)** bị Facebook chặn bằng login wall / cookie challenge / JS challenge khi gọi `www.facebook.com/profile.php?id={uid}` không cookie. Trong khi đó, crawler OpenGraph (`WhatsApp/2.23.20.0`, `default_headers=False`) được Facebook phục vụ trực tiếp HTML SSR siêu nhẹ (~2.3 KB cho profile cá nhân) có sẵn `og:title`, `og:image`, `twitter:title`, `twitter:image`.
2. **Cache kết quả rỗng `(None, None)` suốt 60 giây** khi gặp lỗi/login wall tạm thời, khiến bấm `Check lại` vẫn trả về "Không công khai".
3. **Giới hạn đọc `_read_head` 128 KB** sát với kích thước `<head>` của Facebook (~117–121 KB), dễ cắt cụt trước khi đọc xong metadata; thiếu tối ưu dừng sớm khi đã đủ `og:title` + `og:image`.
4. **Parser HTML** chưa fallback `twitter:title` / `twitter:image` và chưa lọc tiêu đề trang đăng nhập tiếng Việt (`Đăng nhập Facebook`, `Log in to Facebook`, ...).
5. **Telegram `send_photo(photo=avatar_url)`** dễ bị `TelegramBadRequest` khi server Telegram không tải được URL CDN `scontent.*.fbcdn.net` có chữ ký theo vùng; ngoài ra `cb_recheck_single` bỏ qua `_avatar_url` khi tin nhắn gốc chưa có ảnh.

## Kế hoạch triển khai
- [x] **Bước 1 (`src/core/checker.py`, `src/core/ports.py`)**:
  - Dùng `AsyncSession` riêng không `impersonate="chrome"` (`default_headers=False`, `discard_cookies=True`) với header crawler `WhatsApp/2.23.20.0` cho `_fetch_profile_direct` (vẫn dùng `self._session` khi inject session trong test).
  - Nếu `www.facebook.com/profile.php?id={uid}` trả HTML không có metadata hoặc HTTP >= 400, thử 1 lần qua `m.facebook.com/profile.php?id={uid}`.
  - Nâng giới hạn `_read_head` lên 256 KB và dừng sớm ngay khi thấy `</head>` hoặc đủ `og:title` + `og:image`.
  - Không cache `(None, None)` và log chi tiết (`status`, `url`) khi profile rỗng.
  - Mở rộng `parse_public_profile_html` hỗ trợ `twitter:title` / `twitter:image` và danh sách tiêu đề login/error tiếng Việt + tiếng Anh.
  - Thêm `fetch_avatar_bytes(url)` (giới hạn 5 MB) để tải ảnh khi Telegram không fetch được trực tiếp URL `fbcdn.net`.
- [x] **Bước 2 (`src/modules/bulk/service.py`, `src/modules/bulk/handlers.py`, `src/modules/watch/handlers.py`, `src/modules/watch/service.py`, `src/core/runtime.py`)**:
  - Khi `answer_photo` / `send_photo` bằng URL gặp `TelegramBadRequest`, tải bytes ảnh qua `fetch_avatar_bytes` và gửi `BufferedInputFile` trước khi rơi về text-only.
  - Cập nhật `cb_recheck_single`: nếu tin nhắn cũ không có ảnh mà check lại ra `LIVE` + có ảnh thì gửi ảnh mới và xóa tin nhắn text cũ.
- [x] **Bước 3 (`test/test_core.py`, `test/test_bulk.py`, `test/test_watch.py`)**:
  - Bổ sung unit test cho crawler headers, `m.facebook.com` fallback, không cache `(None, None)`, `twitter:title`/`twitter:image` + tiêu đề login tiếng Việt, `fetch_avatar_bytes`, upgrade ảnh khi `Check lại`, và fallback gửi `BufferedInputFile`.
- [x] **Bước 4 (Xác minh)**:
  - Live test UID `4` (`Mark Zuckerberg`, 0.86s, 26.6 KB JPEG) và `100004030621748` (`Vui Nguyễn`, 0.47s, 28.4 KB JPEG).
  - `uv run ruff check .`, `uv run pyright`, `uv run pytest -v`.
