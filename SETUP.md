# BLOY Dev Agent — Hướng dẫn cài đặt (Dev & Production)

Tài liệu này đi từ máy trắng tới lúc agent tự chạy pass đầu tiên. Toàn bộ
đường dẫn/mặc định lấy trực tiếp từ code (`setup_wizard.py`, `workspace.py`,
`pipeline.py`, `ecosystem.config.js`) — không phải mô tả lý thuyết.

> Không có khái niệm "dev khác production về code". Cùng 1 service, cùng 1
> cách chạy (`uv run python -m bloy_dev_agent.service`). Khác biệt duy nhất là
> **production chạy nền bằng PM2** thay vì chạy tay trong terminal, và
> **production luôn nên bật thêm staging-verify** (mục 6) để agent tự chụp
> ảnh xác nhận trên storefront/admin thật.

---

## 0. Kiến trúc tóm tắt

```
agent-manager (BAM)                     bloy_dev_agent — 2 process ĐỘC LẬP
┌─────────────────────┐                 ┌──────────────────────────────────┐
│ core.main  :8002     │  HTTP trigger   │ bloy_dev_agent.service    :8100  │
│  └─ plugin.py (mỏng) │ ───────────────▶│  UI + Twenty poll + pipeline     │
│     (menu + nút chạy)│                 │  → tạo sandbox (OpenSandbox)     │
└─────────────────────┘                 └──────────────────────────────────┘
                                         ┌──────────────────────────────────┐
                                         │ staging_control.service  :8110   │
                                         │  deploy/restart staging api+cms  │
                                         │  (chỉ sandbox nào opt-in mới gọi)│
                                         └──────────────────────────────────┘
```

Lý do tách hẳn khỏi BAM: nếu chạy chung tiến trình, mỗi lần BAM restart
(deploy code mới, rolling reload...) sẽ giết luôn agent đang chạy dở, để lại
container mồ côi và issue kẹt mãi ở cột "In Progress". Tách ra thì BAM restart
lúc nào cũng được, không ảnh hưởng run đang chạy.

`bloy_dev_agent` **không** dùng bảng/DB của BAM, không cần BAM migration. Nó
tự có SQLite riêng (`bloy_dev_agent/db/bloy_dev_agent.sqlite3`) và tự phục vụ
UI riêng ở port của nó. Phần duy nhất chạy *bên trong* BAM là `plugin.py` — 1
file mỏng chỉ thêm mục menu + 1 nút "chạy 1 pass" gọi HTTP sang service kia,
không import gì từ pipeline thật cả.

---

## 1. Yêu cầu trước khi bắt đầu

| Thứ cần | Vì sao |
|---|---|
| Đã có checkout `agent-manager` chạy được (`uv sync`, `uv run agent-manager`) | Service chạy chung venv với agent-manager |
| Docker, chạy được không cần `sudo` | Mỗi ticket được xử lý trong 1 container OpenSandbox |
| `uv`/`uvx` | Chạy service + `opensandbox-server` |
| Node qua `nvm` + Claude Code CLI (`npm install -g @anthropic-ai/claude-code`) | Sandbox mount `~/.nvm` (read-only) vào container để dùng CLI này |
| Đã `claude login` (hoặc dùng ví dụ ở mục 2.4) ít nhất 1 lần trên máy chủ | Sandbox copy `~/.claude.json` + `~/.claude/.credentials.json` (đã lọc) vào container — không có SSH vẫn login được, xem 2.4 |
| SSH key đã thêm vào GitLab | Agent `git fetch`/`push`/mở MR qua SSH |
| 1 API key Twenty (bot riêng, quyền hẹp) | Đọc/ghi ticket trên board |

---

## 2. Cài đặt Dev

### 2.1 Symlink plugin vào agent-manager

```bash
ln -s /path/to/bloy_dev_agent /path/to/agent-manager/community_plugins/bloy_dev_agent
```

Tên thư mục đích **phải** là `bloy_dev_agent` (gạch dưới, không gạch ngang) —
nó trở thành tên package Python. `agent-manager/.env` phải có:

```bash
PLUGINS_EXTERNAL_DIR=community_plugins
```

### 2.2 Cài dependency

```bash
cd agent-manager
uv sync
uv run setup-dependencies   # gộp requirements.txt của mọi plugin, gồm cả opensandbox
```

`requirements.txt` của plugin này chỉ thêm đúng 2 gói (`opensandbox`,
`opensandbox-code-interpreter`) — mọi thứ khác (fastapi, sqlalchemy, httpx...)
dùng chung venv với agent-manager.

### 2.3 Điền `.env` (đặt trong `agent-manager/.env` — dùng chung 1 chỗ)

```bash
BLOY_TWENTY_BASE_URL=https://<workspace>.twenty.com     # hoặc self-host
BLOY_TWENTY_API_KEY=<api key bot riêng>
```

Có thể để trống và điền sau qua UI ở bước 2.5 (trang `/setup` có form riêng
cho 2 giá trị này, ghi thẳng vào DB của service, không cần sửa `.env`).

### 2.4 Chạy thử lần đầu

```bash
cd agent-manager
PYTHONPATH=community_plugins uv run python -m bloy_dev_agent.service
```

Mặc định lắng nghe `127.0.0.1:8100` (đổi bằng `BLOY_AGENT_PORT`). Mở
`http://localhost:8100/setup`.

### 2.5 Đi theo wizard `/setup` — đúng thứ tự nó tự chẩn đoán

Trang này chạy lại các check sau **mỗi lần tải trang**, không cần đoán:

| Bước | Tự sửa được? | Chi tiết |
|---|---|---|
| **Docker** | Không | `curl -fsSL https://get.docker.com \| sudo sh && sudo usermod -aG docker $USER` rồi đăng xuất/đăng nhập lại |
| **Cấu hình OpenSandbox** (`~/.sandbox.toml`) | **Có** — nút "Tạo file cấu hình" | Tự sinh `api_key` ngẫu nhiên + điền `allowed_host_paths` đúng những thư mục service này cần mount |
| **OpenSandbox server** | **Có** — nút "Khởi động server" | Chạy `uvx opensandbox-server` (ưu tiên qua PM2 nếu có) |
| **Thư mục worktree** (`/home/<user>/bloy-worktrees`, đổi bằng `BLOY_WORKTREE_ROOT`) | Tự tạo | Nơi mỗi ticket được `git worktree add` ra 1 nhánh riêng |
| **Repo BLOY** (`/home/<user>/BLOY` mặc định, đổi bằng `BLOY_MONOREPO`) | **Có** — nút "Clone repo còn thiếu" | Cần đủ 3 repo: `shopify-app-loyalty-api`, `shopify-app-loyalty-cms`, `shopify-app-loyalty-headless-commerce` |
| **Mirror độc lập cho agent** (`~/bloy-dev-agent-repos`, đổi bằng `BLOY_AGENT_REPOS_ROOT`) | **Có** — nút "Clone mirror độc lập" | Bản clone RIÊNG, agent branch/fetch từ đây — không đụng vào checkout bạn đang code tay (tránh agent tự `git fetch` làm lệch remote-tracking ref của bạn) |
| **Claude CLI** | Không | `npm install -g @anthropic-ai/claude-code` (qua nvm) |
| **Claude login** (`~/.claude.json` tồn tại) | Không qua form — nhưng **không bắt buộc phải SSH**: xem khung dưới | |
| **GitLab SSH** | Không | `ssh-keygen -t ed25519 -C "$USER@bloy" && cat ~/.ssh/id_ed25519.pub` → thêm vào GitLab → SSH Keys |
| **Twenty** | Điền form ngay trên trang | Test bằng 1 lệnh ping thật, báo lỗi rõ nếu sai key/URL |
| **Egress mode** | **Có** — nút đổi `[egress].mode` thành `"dns+nft"` | Xem cảnh báo bảo mật ở mục 3.3 — phải tự bấm restart `opensandbox-server` sau khi đổi (cố ý không tự động, để không cắt ngang sandbox đang chạy) |

> **Đăng nhập Claude không cần SSH vào server**: nếu agent-manager của bạn đã
> có commit `feat: allow login with Claude`, vào
> `http://<bam-host>:<port>/ai-code/factory/claude-environments` (chỉ admin
> thấy), nhập `CLAUDE_CONFIG_DIR` = `$HOME/.claude` (đúng thư mục service này
> đọc), bấm "Set up & sign in" — server tự cài CLI, mở URL đăng nhập, bạn login
> bằng trình duyệt riêng, dán code lại vào UI. Không có tính năng này thì SSH
> vào chạy `claude login` một lần là đủ, không cần lặp lại trừ khi session hết
> hạn.

Trang hết lỗi (`ready: true`) là dùng được. Cùng dữ liệu ở dạng JSON:
`GET /api/setup`.

---

## 3. Cấu hình chi tiết

Có 3 nơi lưu cấu hình, đều là **DB của service này** (bảng `plugin_bloy_setting`),
không phải file — sửa qua UI thì lưu ngay, không cần restart. Biến môi trường
(`BLOY_TWENTY_BASE_URL`/`BLOY_TWENTY_API_KEY`) nếu có set thì **luôn thắng**
giá trị lưu trong DB.

### 3.1 Trang `/setup` — kết nối & vị trí thư mục

| Field | Mặc định | Ghi chú |
|---|---|---|
| Twenty base URL / API key | *(trống)* | Test bằng 1 lệnh ping thật ngay khi lưu |
| Monorepo | `BLOY_MONOREPO` hoặc `/home/<user>/BLOY` | Nơi có đủ 3 repo + `CLAUDE.md` gốc |
| Git remote | `git@sbc-gitlab.bsscommerce.com:sa-division/tc-team/shopify-app-loyalty` | Tiền tố dùng khi bấm nút tự clone repo/mirror còn thiếu |

### 3.2 Trang `/settings` — hành vi pipeline

| Field | Mặc định | Ghi chú |
|---|---|---|
| **Số lần thử lại tối đa** (`max_attempts`) | `5` | Hết số này, issue vào cột **Bị chặn**, không tự nhặt lại nữa cho tới khi người xem tự reset |
| **Twenty project id** (`project_id`) | *(trống, bắt buộc)* | UUID — lấy từ URL board Twenty |
| **Repo đích** (`target_repo`) | `shopify-app-loyalty-api` | Chỉ áp dụng khi ticket KHÔNG tự khai repo. Nếu mô tả ticket có dòng `Repos: shopify-app-loyalty-api, shopify-app-loyalty-cms` thì dòng đó thắng — và đây cũng là cách DUY NHẤT để 1 ticket sửa nhiều repo cùng lúc |
| **Timeout sandbox** (`timeout_minutes`) | `30` | Quá giờ này sandbox bị dừng bất kể đang làm gì |
| **Lấy issue từ** (`source_status`) | `Todo` | Cột chứa việc chờ làm |
| **Đang chạy** (`working_status`) | `In Progress` | Claim trước khi chạy, tránh 2 pass nhặt trùng 1 issue |
| **Thành công** (`done_status`) | `In Review` | Có merge request thật thì chuyển vào đây |
| **Thất bại** (`error_status`) | `Todo` | Còn lượt thử → quay lại đúng cột nguồn để pass sau nhặt lại (cố ý trùng `source_status`) |
| **Bị chặn** (`blocked_status`) | `Backlog` | Hết lượt thử. **Phải khác `source_status`** — nếu trùng, issue bị nhặt lại ngay pass sau rồi lại bị chặn, thành vòng lặp vô ích |

Tên cột phải khớp **chính xác** chuỗi hiển thị trên Twenty (phân biệt hoa
thường/khoảng trắng) — sai tên coi như cột đó không tồn tại với agent.

### 3.3 Cấu hình không có form — chỉnh qua DB

`sandbox_stage_retries` (mặc định `3`) tồn tại trong `store.py` và **có được
pipeline dùng thật** (retry lại đúng bước sandbox trong CÙNG 1 run khi gặp lỗi
tạm thời, ví dụ "AI gặp lỗi ở process 2" — khác với `max_attempts` ở trên, cái
đó tính theo *lượt thử mới* chứ không phải retry-tại-chỗ) — nhưng **chưa có ô
nào trên `/settings` để sửa**. Muốn đổi, sửa thẳng trong Python:

```python
from bloy_dev_agent import store
store.save_settings({store.SETTING_SANDBOX_STAGE_RETRIES: "5"})
```

### 3.4 Vận hành hằng ngày

- Trang chủ `/` (dashboard): danh sách run đang chạy, lịch sử, issue bị chặn
  do vượt số lần thử lại.
- Nút **"Run Now"** hoặc `POST /run` (form) / `POST /api/pipeline/run` (JSON)
  — kích 1 pass (tối đa 3 ticket cùng lúc). Trả lời ngay, chạy nền.
- BAM gọi đúng endpoint JSON đó qua `routine_actions()` của `plugin.py` mỗi
  vài phút — nếu BAM bị tắt/khởi động lại, service này **không hề hấn gì**,
  chỉ đơn giản không có ai kích pass mới cho tới khi BAM sống lại. Trang chủ
  tự báo "đã quá 15 phút không có ai trigger" nếu việc đó xảy ra.

---

## 4. Chạy Production (PM2)

File `ecosystem.config.js` đã có sẵn trong repo, dùng được luôn:

```bash
cd agent-manager
pm2 start /path/to/bloy_dev_agent/ecosystem.config.js
pm2 save
```

Nó khai báo **2 process tách biệt** — đừng gộp lại:

```js
{
  name: 'bloy-dev-agent',                 // UI + pipeline + Twenty poll
  cwd: '/path/to/agent-manager',
  script: 'uv',
  args: 'run python -m bloy_dev_agent.service',
  env: {
    PYTHONPATH: '.../agent-manager/community_plugins',
    BLOY_AGENT_PORT: '8100',
    BAM_URL: 'http://localhost:8002',     // sửa đúng port BAM thật đang chạy
  },
},
{
  name: 'bloy-staging-control',           // chỉ cần nếu dùng staging-verify (mục 6)
  args: 'run python -m bloy_dev_agent.staging_control.service',
  env: {
    BLOY_STAGING_CONTROL_HOST: '172.17.0.1',  // gateway Docker bridge, KHÔNG đổi thành 0.0.0.0
    BLOY_STAGING_CONTROL_PORT: '8110',
  },
},
```

Vì sao tách 2 process dù cùng 1 repo: `bloy-staging-control` là **surface
DUY NHẤT** mà 1 sandbox được cấp quyền mạng để gọi tới (xem mục 6.3) — 1
lượt deploy staging bị treo/chậm không được phép làm nghẽn luôn dashboard hay
vòng lặp poll Twenty ở `:8100`.

`.env` đọc theo thứ tự: biến môi trường PM2 đã set (không bao giờ bị ghi đè)
→ `bloy_dev_agent/.env` (nếu có) → `agent-manager/.env`. Khuyến nghị giữ mọi
secret ở `agent-manager/.env` cho gọn 1 chỗ (comment trong `ecosystem.config.js`
cũng ghi rõ lý do này).

Không dùng PM2? Chạy `bloy_dev_agent.service:app` bằng bất kỳ ASGI server nào
(`uvicorn`, `gunicorn -k uvicorn.workers.UvicornWorker`) đứng sau systemd —
chỉ cần đảm bảo 2 process (chính + staging-control) **restart độc lập nhau**
và biến `PYTHONPATH` trỏ đúng `community_plugins`.

### 4.1 Sau khi restart process

`_reap_on_boot()` tự chạy mỗi lần service khởi động lại:
- Đóng mọi run đang dở dang do process cũ bị giết giữa chừng.
- Dọn sandbox mồ côi (container vẫn chạy dù process quản lý nó đã chết).
- Thu hồi mọi token staging-control còn sống từ trước.
- Trả issue bị kẹt ở cột "In Progress" (không có run nào thật sự đang giữ)
  về lại cột nguồn.

Không cần thao tác tay gì thêm sau restart — kể cả sau 1 lần mất điện/khởi
động lại máy đột ngột.

---

## 5. Skill packs cho sandbox

Sandbox không đọc trực tiếp `.claude/skills/` của repo dev — nó đọc 1 kho
**dùng chung với BAM**:

```
<agent-manager checkout cha>/agent-manager-skill-packs/
├── shared/<tên-skill>/SKILL.md
└── sources/<slug>/...              (sync từ git hoặc upload ZIP)
```

Thêm skill mới theo đúng tinh thần "workspace tự quản": vào trang
**`http://<bam-host>:<port>/skill-packs`** của BAM, "Import from file" (ZIP có
`SKILL.md`) hoặc "Import from git" — **không cần Claude viết SKILL.md hộ**,
chỉ cần author/import bằng tay.

Sau khi có skill trong kho, bật cho sandbox ở trang riêng của service này:
`http://localhost:8100/skills` — tick chọn pack, lưu. Chỉ pack được tick mới
được copy vào `~/.claude/skills` bên trong container mỗi lần chạy.

---

## 6. Staging-verify (tuỳ chọn, khuyến nghị bật ở production)

Khi 1 ticket đụng tới `shopify-app-loyalty-cms`, pipeline tự cho sandbox thêm
1 trình duyệt Chromium thật (qua Playwright MCP) để tự chụp ảnh xác nhận trên
staging/storefront thật sau khi deploy — không cần đánh dấu gì trong ticket,
agent tự quyết định có đáng verify hay không.

### 6.1 Build image riêng có Chromium

```bash
cd bloy_dev_agent/sandbox_image
docker build -t bloy-dev-agent/sandbox-chromium:v1 .
```

Image build từ `opensandbox/code-interpreter:v1.0.2` (image mặc định mọi
ticket khác vẫn dùng) + `@playwright/mcp` + Chromium pin cứng version — chỉ
ticket có staging-verify mới kéo image này, build lỗi ở đây **không** ảnh
hưởng ticket thường.

### 6.2 Dựng checkout staging riêng + PM2

`staging_control` không deploy vào checkout bạn code tay — nó cần 1 bản
checkout RIÊNG dùng làm "staging thật":

```
~/bloy-staging/repos/
├── shopify-app-loyalty-api/     (PM2: bloy-stg-api, bloy-stg-webhook, bloy-stg-cron — port 9976)
└── shopify-app-loyalty-cms/     (PM2: bloy-stg-cms — port 3012)
```

`cms` deploy có 3 bước: build Admin SPA (`npm run build --prefix web/frontend`),
build bundle CDN headless (`pnpm --filter bloy-extensions run build-bloy`), và
`npx shopify app deploy --allow-updates` (đẩy theme-app-extension/checkout/
admin extension lên app Shopify dev đang link) — bước cuối cần
`npx shopify auth login` **chạy tay 1 lần** từ checkout đó trước, nếu không sẽ
403 thay vì hỏi lại đăng nhập.

`api` không có bước build (`nest start --watch` tự theo dõi thay đổi) — deploy
chỉ là rsync rồi restart PM2.

### 6.3 Vì sao cần thêm 1 tunnel domain

Sandbox **không thể** gọi thẳng `172.17.0.1:8110` (địa chỉ gateway Docker) dù
đó đúng là nơi `staging-control` lắng nghe — chính sách egress `dns+nft` chỉ
chấp nhận domain (FQDN), không bao giờ chấp nhận IP trần. Cách giải quyết đã
dùng thật: thêm 1 hostname Cloudflare Tunnel trỏ ngược lại đúng
`172.17.0.1:8110` đó, rồi liệt kê hostname này (không phải IP) trong
`sandbox_runner.STAGING_EGRESS_ALLOW`. Toàn bộ endpoint vẫn yêu cầu bearer
token như cũ, chỉ đổi đường sandbox gọi tới từ IP nội bộ sang 1 hostname HTTPS
công khai.

### 6.4 Capture session Shopify Admin đã đăng nhập (bắt buộc, làm bằng tay)

Container headless không tự vượt qua được 2FA/bot-check của Shopify — cần 1
người thật làm 1 lần trên màn hình thật:

```js
// Sau khi đăng nhập admin.shopify.com bằng trình duyệt thật (Playwright/Chrome)
await context.storageState({
  path: `${process.env.HOME}/.bloy-shopify-auth/storage-state.json`,
});
```

```bash
chmod 600 ~/.bloy-shopify-auth/storage-state.json
```

File này chỉ chứa cookie phiên, không phải mật khẩu — nhưng vẫn là credential
thật, giữ quyền 600. Sandbox chỉ nhận file này ở dạng mount read-only, không
bao giờ thấy mật khẩu gốc. Khi session hết hạn, báo cáo run sẽ ngừng ghi "ĐÃ
VERIFY TRÊN STAGING" và ghi rõ bị chặn ở màn hình đăng nhập — lặp lại bước
trên là xong, không có gì phải sửa code.

### 6.5 Verify trực tiếp trên storefront (không chỉ Admin)

Mặc định trỏ vào store test công khai (đổi được qua env, không cần sửa code):

```bash
BLOY_STOREFRONT_URL=https://test-bloy-loyalty.myshopify.com   # mặc định
BLOY_STOREFRONT_PASSWORD=1                                     # mặc định — chỉ là cổng chặn dev, không phải mật khẩu thật
```

---

## 7. Kiểm tra cuối cùng (health checks)

| Việc kiểm | URL |
|---|---|
| Setup còn thiếu gì | `GET /api/setup` (JSON) hoặc `/setup` (UI) |
| Service còn sống, đang bận hay rảnh | `GET /api/health` |
| Toàn cảnh preflight (mọi check gộp lại) | `GET /preflight` hoặc `/api/preflight` |
| Staging-control còn sống (chỉ có ý nghĩa nếu đã bật mục 6) | `GET http://172.17.0.1:8110/v1/status` (cần bearer token — 401 vẫn coi là "đang chạy") |
| Run đang chạy / lịch sử | `GET /api/runs/active`, `GET /api/runs/{id}` |

---

## 8. Sự cố thường gặp

| Triệu chứng | Nguyên nhân thật đã gặp | Cách xử lý |
|---|---|---|
| `ModuleNotFoundError: bloy_dev_agent` khi chạy lệnh tay | Thiếu `PYTHONPATH=community_plugins` hoặc cwd không phải `agent-manager` | Luôn `cd agent-manager` trước, set `PYTHONPATH` |
| Run treo mãi ở "In Progress", issue không tự nhả ra | Process bị kill giữa chừng (mất điện, restart) trước khi `_reap_on_boot()` kịp chạy lại | Chỉ cần khởi động lại service — tự dọn, không cần thao tác tay |
| Sandbox 403 khi push GitLab | SSH key máy chủ chưa thêm vào GitLab, hoặc thêm nhầm key khác user | `check_git_ssh` ở `/setup` báo đúng lỗi + lệnh sửa |
| Container vào được domain lạ / không vào được domain cần | `[egress].mode` đang là `dns` thường (chỉ lọc theo câu hỏi DNS, vẫn nối thẳng được bằng IP) thay vì `dns+nft` | Nút "Đổi mode" ở `/setup`, rồi tự restart `opensandbox-server` |
| Staging-verify báo "không kết nối được" tới domain riêng | Domain đó thiếu trong `STAGING_EGRESS_ALLOW`, hoặc là domain cũ/sai chưa từng tồn tại | Sửa `features/sandbox_runner.py`, đối chiếu đúng domain thật trong `.env`/`shopify.app.toml` của checkout staging, không đoán |
| Merge request có lẫn thay đổi lockfile/`extensions/cdn-dist` không liên quan | Agent chạy `npm install`/build làm bẩn working tree | Đã tự xử lý — `workspace.discard_generated_changes()` revert trước khi commit, không cần can thiệp |
| Skill mới thêm vào `/skill-packs` mà sandbox không thấy | Chưa tick bật ở `/skills` (2 trang khác nhau: import ở BAM, bật dùng ở service này) | Vào `/skills`, tick pack, lưu |

---

## Tổng hợp biến môi trường

| Biến | Mặc định | Ghi chú |
|---|---|---|
| `BLOY_AGENT_PORT` | `8100` | Port UI chính |
| `BLOY_AGENT_HOST` | `127.0.0.1` | Đổi thành `0.0.0.0` nếu cần truy cập từ máy khác |
| `BAM_URL` | `http://localhost:8000` | Chỉ dùng để hiện link + tính "BAM có còn trigger không" |
| `BLOY_AGENT_ENV_FILE` | *(rỗng)* | Ép đọc 1 file `.env` cụ thể thay vì dò `bloy_dev_agent/.env` → `agent-manager/.env` |
| `BLOY_AGENT_DB_URL` | `sqlite:///bloy_dev_agent/db/bloy_dev_agent.sqlite3` | DB riêng, không dùng chung với BAM |
| `BLOY_TWENTY_BASE_URL` / `BLOY_TWENTY_API_KEY` | *(rỗng, điền qua `/setup`)* | Ưu tiên hơn giá trị lưu trong Settings |
| `BLOY_MONOREPO` | `/home/<user>/BLOY` | |
| `BLOY_WORKTREE_ROOT` | `/home/<user>/bloy-worktrees` | |
| `BLOY_AGENT_REPOS_ROOT` | `~/bloy-dev-agent-repos` | Mirror riêng cho agent, tách khỏi checkout dev |
| `BLOY_STAGING_CONTROL_HOST` / `_PORT` | `172.17.0.1` / `8110` | Không đổi host thành `0.0.0.0` |
| `BLOY_STOREFRONT_URL` / `_PASSWORD` | `test-bloy-loyalty.myshopify.com` / `1` | Chỉ dùng cho staging-verify storefront |
