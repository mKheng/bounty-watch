# bounty-watch

Bot Telegram tự động báo khi có **program mới** hoặc **scope mở rộng** trên các nền tảng bug bounty.

---

## 1. Nghiên cứu nguồn dữ liệu

### `arkadiyt/bounty-targets-data` — ✅ dùng làm nguồn chính

Đây là repo duy nhất trong hai cái dùng được để tự động hoá.

| Đặc điểm | Chi tiết |
|---|---|
| Định dạng | JSON có cấu trúc, mỗi platform một file |
| Tần suất cập nhật | README ghi *"New changes (if any) are picked up every 30 minutes"* |
| Platform có sẵn | HackerOne, Bugcrowd, **Intigriti**, YesWeHack, Federacy |
| File tổng hợp | `domains.txt` (không wildcard), `wildcards.txt` |
| Code sinh dữ liệu | repo riêng `arkadiyt/bounty-targets` |

Đường dẫn raw:

```
https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data/intigriti_data.json
https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data/hackerone_data.json
```

**Điểm mấu chốt:** repo này là một Git repo, nên bản thân nó đã là một changelog. Có hai cách phát hiện thay đổi:

1. **Tự lưu snapshot rồi diff** ← cách bot này dùng. Đơn giản, không phụ thuộc GitHub API, không dính rate limit, và state nằm ngay trong repo của nhóm nên audit được.
2. Gọi GitHub API lấy diff giữa các commit. Gọn hơn nhưng dính rate limit và phải parse patch.

Shape dữ liệu Intigriti (đã verify):

```json
{
  "name": "Atolls Vulnerability Disclosure Program (VDP)",
  "company_handle": "atolls",
  "handle": "atollsvdp",
  "url": "https://www.intigriti.com/programs/atolls/atollsvdp/detail",
  "status": "open",
  "confidentiality_level": "public",
  "tacRequired": false,
  "twoFactorRequired": false,
  "min_bounty": { "value": 0, "currency": "EUR" },
  "max_bounty": { "value": 0, "currency": "EUR" },
  "targets": {
    "in_scope":  [ { "type": "url", "endpoint": "pepper.com", "impact": "No Bounty" } ],
    "out_of_scope": [ ... ]
  }
}
```

### `disclose/bug-bounty-platforms` — ❌ không dùng làm nguồn sự kiện

Đây là **catalog tĩnh** liệt kê 121+ nền tảng bug bounty/VDP trên thế giới, viết bằng bảng Markdown trong `README.md` (8 cột: tên, URL, khu vực, Twitter, loại program, có leaderboard không, URL leaderboard, URL danh sách program).

Nó **không chứa program hay scope**, chỉ chứa metadata về nền tảng, và thay đổi vài tháng một lần. Không có gì để "watch".

**Nhưng nó có giá trị dùng một lần**, cho hai việc:

1. **Mở rộng vùng săn.** Nhóm đang chỉ nhìn HackerOne / Intigriti / huntr. Repo này chỉ ra nhiều nền tảng khu vực gần như không ai cạnh tranh, đáng chú ý nhất với nhóm mình:
   - **Safevuln** (Việt Nam) — public, có leaderboard
   - **WhiteHub** (Việt Nam, CyStack) — private + public
   - **Secuna** (Philippines), **BBHunt Japan**, **Bug Zero** (Sri Lanka), **Bugbase** (Ấn Độ)
   - Nhóm chính phủ: **Singapore GovTech VDP**, **HITCON ZeroDay** (Đài Loan), **CERT-In RVDCP** (Ấn Độ)
2. **Nguồn URL danh sách program.** Cột *Public Programs URL* cho sẵn link danh sách program của từng nền tảng — chính là thứ cần để viết scraper cho các platform mà arkadiyt không cover (ví dụ **huntr.com**, mà Tâm và Đạt đang phụ trách).

**Kết luận:** đọc `disclose/bug-bounty-platforms` một lần để chọn nền tảng, rồi quên nó đi. Bot chạy trên `arkadiyt/bounty-targets-data`.

---

## 2. Kiến trúc bot

```
GitHub Actions (cron */30)
        │
        ├─► tải intigriti_data.json + hackerone_data.json
        │
        ├─► normalize về một struct Program chung
        │
        ├─► diff với state/<platform>.json của lần chạy trước
        │        ├─ new_program      🟢 program mới
        │        ├─ scope_added      🔵 scope mở rộng
        │        ├─ bounty_changed   💰 đổi mức thưởng
        │        └─ removed_program  ⚫ program đóng
        │
        ├─► chấm điểm newbie (rubric của nhóm) cho program mới
        │
        ├─► gộp block → gửi Telegram (HTML, tối đa 3900 ký tự/message)
        │
        └─► commit state mới vào repo
```

### Vì sao GitHub Actions thay vì VPS

- Miễn phí, không lo uptime, không cần cài gì.
- **State được version-control.** Muốn biết program X xuất hiện lúc nào chỉ cần `git log` file state — tự nhiên có luôn lịch sử.
- Khớp với hướng đi trong memo 26/07: chuyển toàn bộ workspace sang Git repo.

Nếu sau này cần chạy recon nặng (subfinder/httpx trên 50 domain của Atolls) thì mới cần VPS — nhưng phần watch thì không.

---

## 3. Cài đặt

### Bước 1 — Tạo bot Telegram

1. Nhắn `@BotFather` → `/newbot` → đặt tên → nhận **token**
2. Tạo group cho nhóm, thêm bot vào
3. Lấy `chat_id`: nhắn một tin bất kỳ trong group rồi mở
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, tìm `"chat":{"id":-100...}`
   (group id là **số âm** — nhớ giữ dấu trừ)

### Bước 2 — Đưa vào repo

```
repo-cua-nhom/
├── .github/workflows/bounty-watch.yml   ← copy từ bounty-watch.yml
└── bounty-watch/
    ├── watcher.py
    ├── README.md
    └── state/                            ← tự sinh sau lần chạy đầu
```

### Bước 3 — Thêm secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Giá trị |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token từ BotFather |
| `TELEGRAM_CHAT_ID` | id group (số âm) |

> Không bao giờ commit token vào repo.

### Bước 4 — Chạy lần đầu

Lần chạy đầu tiên **chỉ tạo baseline**, không gửi thông báo — nếu không bot sẽ spam vài trăm program một lúc. Từ lần thứ hai trở đi mới báo thay đổi.

```bash
# chạy tay ở máy trước cho chắc
python watcher.py --dry-run      # in ra màn hình, không gửi
python watcher.py --bootstrap    # chỉ tạo baseline
```

---

## 4. Cấu hình

| Biến môi trường | Mặc định | Ý nghĩa |
|---|---|---|
| `PLATFORMS` | `intigriti,hackerone` | Platform cần theo dõi, phân cách bằng dấu phẩy |
| `MIN_SCORE` | `0` | Chỉ báo program mới có điểm newbie ≥ giá trị này |
| `STATE_DIR` | `state` | Nơi lưu snapshot |

Ví dụ chỉ muốn nhận program thật sự hợp người mới, đỡ nhiễu:

```yaml
env:
  PLATFORMS: "intigriti"
  MIN_SCORE: "7"
```

### Cách chấm điểm (0–10)

| Yếu tố | Điểm |
|---|---|
| Không rào cản tham gia | +2 (1 rào cản: +1) |
| Có wildcard | +3 |
| ≥15 asset | +3 · ≥5 asset: +2 · ≥1: +1 |
| Là VDP | +3 |
| Bounty tối đa ≤ 2000 | +2 |
| Có wildcard trong scope | +1 |

🟢 ≥8 · 🟡 5–7 · ⚪ <5

Logic này nằm ở hàm `score_newbie()` — sửa trọng số ở đó khi nhóm đổi tiêu chí.

---

## 5. Việc cần làm trước khi đưa vào production

- [ ] **Verify field name của Bugcrowd / YesWeHack / Federacy.** Chỉ `intigriti` và `hackerone` đã được kiểm chứng thật. Ba platform còn lại dùng normalizer generic đoán tên field — chạy `--dry-run` và soi output trước khi bật.
- [ ] **Kiểm tra độ tươi của nguồn.** README của arkadiyt hiển thị dòng *"The last change was detected on..."*. Mở repo xem commit gần nhất cách đây bao lâu; nếu nguồn ngưng cập nhật thì bot im lặng mà không báo lỗi. Nên thêm cảnh báo nếu quá 24h không thấy thay đổi nào.
- [ ] **huntr.com không có trong arkadiyt.** Tâm và Đạt cần scraper riêng — lấy URL danh sách program từ `disclose/bug-bounty-platforms`.
- [ ] Cân nhắc tách kênh Telegram theo platform để mỗi nhóm chỉ nhận phần của mình.

---

## 6. Hướng mở rộng

| Ý tưởng | Ghi chú |
|---|---|
| Tự chạy recon khi có program mới | Bắn `subfinder` + `httpx` trên scope mới, gửi kèm kết quả. Cần VPS, không chạy trên Actions. |
| Lệnh `/targets` trong Telegram | Truy vấn state hiện tại thay vì chỉ nhận push |
| Diff `wildcards.txt` riêng | Wildcard mới xuất hiện thường là tín hiệu mạnh nhất |
| Ghi thẳng vào repo findings | Program mới → tự tạo folder `workspace/<target>/` theo cấu trúc đã thống nhất |
| Theo dõi platform Việt Nam | Safevuln + WhiteHub — cần scraper riêng, gần như không có cạnh tranh |

---

*Nội bộ nhóm Bug Hunter. Dữ liệu nghiên cứu: 27/07/2026.*
