# MipMem — Tài liệu Tiếng Việt

[![CI Quality Gate](https://github.com/pageel/mipmem/actions/workflows/ci.yml/badge.svg)](https://github.com/pageel/mipmem/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md) | [Tiếng Việt](README.vi.md)

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with README.md, src/mipmem/, .github/workflows/ on 2026-11-27 -->

**Hệ thống Bộ nhớ Nhận thức Phân tầng 4 Lớp (`4-Layer Mipmapped Cognitive Memory`) thuần thư viện chuẩn Python `>= 3.9` (`dependencies = []`), `0` phụ thuộc ngoài, `0` chi phí token LLM, hỗ trợ tìm kiếm song ngữ Anh/Việt FTS5 `< 5ms`, đồng bộ đa máy không xung đột và giao thức Sub-App riêng tư từng máy.**

---

## Điểm nổi bật (`v1.1.2`)

- **Thuần Python `>= 3.9` Standard Library & `< 25ms` Hot-Path:** Đóng gói gọn trong PyPI package lẫn tệp thực thi đơn `.pyz`; nhánh nóng `health --json` và no-op hook chạy dưới `25ms` với zero import nặng.
- **Mipmap 4 Tầng (`L0 → L3`) & Brainstorm AST Nguyên bản:**
  - `L0`: Radar ngữ cảnh khởi đầu (`<= 300` tokens mặc định), tự gom hoặc bung cảnh báo Sub-App và xung đột đồng bộ.
  - `L1`: Thẻ cấu trúc đầu mục (`#Lstart-Lend`, `<= 450` tokens), cắt theo ranh giới dòng an toàn không bao giờ làm hở block code fence.
  - `L2 / L3`: Cắt đúng mục `§k` hoặc dòng `#Lstart-Lend` khi cần và lưu nguyên bản 100%.
- **Tìm kiếm Song ngữ Anh/Việt SQLite FTS5 (`< 5ms`):** Chuyển đổi đầy đủ `đ → d`, `Đ → D` giúp truy vấn tiếng Việt không dấu (ví dụ `"dong bo da may"`) khớp chính xác bản ghi có dấu.
- **Hợp đồng Per-Host Private Sub-Apps (`core/subapps.py`):**
  - Tách biệt hoàn toàn logic quản trị máy cá nhân khỏi lõi OSS. Script từng máy chỉ ghi JSON trạng thái vào `vault/hosts/<host_id>/telemetry/<app_id>.status.json` (`detail_file` được kiểm tra chặt chống path traversal).
  - Ủy quyền lệnh `/mem <app>` cục bộ thông qua `~/.config/mipmem/apps.d/*.json` (tuyệt đối không đọc từ Vault đồng bộ, kiểm tra chủ sở hữu UID + không ghi `group`/`other` `0o022`, `shell=False`, timeout tối đa `15s`, che bí mật trước khi hiển thị).
- **Đồng bộ Đa máy Không Xung đột (`GitSyncBackend` & `FolderSyncBackend`):**
  - Kiến trúc ghi đơn chủ (`single-writer`): mỗi máy chỉ tự ghi vào `hosts/<host_id>/` và `shared/brainstorms/by-host/<host_id>/`.
  - **`GitSyncBackend`:** Dùng `GIT_TERMINAL_PROMPT=0` (ủy quyền SSH-Agent / Credential Helper, không lưu PAT), khóa `fcntl.flock`, `git pull --rebase --autostash`, tự động `git rebase --abort` + báo cảnh báo lên `L0`/`/mem fleet` nếu sửa thủ công đụng nhau, và tự đồng bộ nền tách phiên lúc `Stop` không làm chậm giao diện chat (đồng thời ghi log lỗi đã che bí mật `CP-SYN-01` vào `<cache_dir>/mipmem.log` nếu worker nền gặp sự cố).
  - **`FolderSyncBackend`:** Đồng bộ hai chiều qua thư mục dùng chung (Syncthing, Tailscale Drive, NAS cục bộ) qua khóa `[sync].folder_path`, bỏ qua `.cache/` và `.git/`, chống lồng thư mục Vault và bỏ qua symlink thoát gốc.
- **Tự Nâng cấp Opt-In Bảo mật (`mipmem self-update`):** Chỉ hoạt động khi người dùng gọi tường minh; khóa HTTPS vào `pageel/mipmem` trên GitHub Releases, kiểm tra `SHA256SUMS`, `ZipFile.testzip()`, chạy kiểm tra `health --json` trên file tạm trước khi tráo nguyên tử `os.replace()`; từ chối ghi đè khi cài qua `uv`/`pipx`/`pip`/Plugin/`git-dev` và gợi ý đúng lệnh nâng cấp theo kênh cài.

---

## Cài đặt (4 Kênh)

### 1. Qua PyPI (`uv`, `pipx` hoặc `pip`)

```bash
uv tool install mipmem
# hoặc: pipx install mipmem
# hoặc: python3 -m pip install mipmem

mipmem health --json
mipmem init --host workstation-alpha --json
```

### 2. Tệp `.pyz` Độc lập (GitHub Releases)

Tải `mipmem.pyz` và `SHA256SUMS` từ [GitHub Releases](https://github.com/pageel/mipmem/releases):

```bash
sha256sum -c SHA256SUMS --ignore-missing
python3 ./mipmem.pyz health --json
python3 ./mipmem.pyz init --host workstation-alpha --json

# Kiểm tra hoặc nâng cấp tự chọn (chỉ dành cho bản zipapp)
python3 ./mipmem.pyz self-update --check --json
python3 ./mipmem.pyz self-update
```

### 3. Native Antigravity Plugin (CLI & IDE)

Gốc kho mã nguồn chứa sẵn cấu trúc Plugin (`plugin.json`, `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, `skills/mem/SKILL.md`, `bin/mipmem.pyz`). Liên kết hoặc cài vào thư mục plugins của Antigravity và chạy `python3 ./bin/mipmem.pyz init --host <tên-máy>` một lần. Mọi hook hoạt động độc lập trong phạm vi plugin, không ghi đè cấu hình toàn cục của người dùng.

### 4. MCP Server Cục bộ Đa Nền tảng (`mipmem mcp`)

Trỏ bất kỳ client MCP `stdio` nào (Antigravity, Claude Code, Cursor, Gemini CLI) tới `mipmem mcp` hoặc `python3 /path/to/mipmem/bin/mipmem.pyz mcp` để dùng ngay 6 công cụ: `mem_recall`, `mem_bs_list`, `mem_bs_slice`, `mem_bs_save`, `mem_handoff`, `mem_fleet_status`.

---

## Lệnh Slash `/mem` (0 Token LLM) & CLI Chính

- **Trong phiên chat (`/mem ...` hoặc alias plugin `/mipmem:mem ...`):** `/mem help`, `/mem status`, `/mem fleet`, `/mem sync [--pull-only|--push-only]`, `/mem recall <từ_khóa>`, `/mem list [1-10]`, `/mem bs [list|save|view|status]`, `/mem config [show|get|set]`, `/mem handoff [@host]`, cùng `/mem <app>` nếu cấu hình cục bộ trong `apps.d`.
- **Dòng lệnh CLI (`mipmem ...`):** `init [--refresh-guide]`, `join <vault_remote_url> [--host <id>] [--adopt-host]`, `config`, `health [--deep] [--json]`, `fleet [--json]`, `sync [--pull-only|--push-only|--detached|--if-configured] [--config <path>] [--json]`, `self-update [--check|--dry-run|--version <tag>] [--json]`, `migrate --from-v2`, `index`, `recall`, `bs`, `hook`, `mcp`.

---

## Cấu hình Đồng bộ Đa máy (`~/.config/mipmem/config.toml`)

Sao chép [config.example.toml](config.example.toml) làm mẫu khởi đầu:

```toml
[core]
locale = "vi"

[sync]
# Chọn "none", "git", hoặc "folder"
backend = "git"
vault_remote_url = "git@github.com:example-user/my-mipmem-vault.git"
folder_path = ""
auto_sync_on_stop = true
```

Nếu dùng Syncthing hoặc thư mục đồng bộ cục bộ, đặt `backend = "folder"` và trỏ `folder_path` tới thư mục chia sẻ bên ngoài Vault.

### Gia nhập Máy mới trong 2 Bước (`AGENTS.md` + `mipmem join`)

Mỗi Private Vault tự động chứa `AGENTS.md` và `README.md` chuẩn hóa không gắn thông tin máy (`mipmem init --refresh-guide` cho Vault tạo từ `v1.0.x`). Trên máy mới chỉ cần:

```bash
uv tool install mipmem
mipmem join git@github.com:example-user/my-mipmem-vault.git --host my-laptop
```

Lệnh sẽ kiểm tra `hosts/*/` tránh trùng `host_id` (chỉ nhận lại khi truyền `--adopt-host`), bật `core.filemode=false`, cấu hình `config.toml`, dựng lại chỉ mục FTS5 và đồng bộ lần đầu.

### Wrapper Đồng bộ khi Thoát `/exit` trên Linux (Bash / Zsh)

Phiên chat terminal `agy` kích hoạt hook `Stop` sau mỗi lượt trả lời, nhưng lệnh `/exit` thoát trực tiếp khỏi CLI mà không qua lượt model mới. Để đảm bảo flush đồng bộ lúc thoát `/exit` (hoặc gom toàn bộ phiên thành `1` commit sạch khi tắt sync từng turn), thêm hàm wrapper sau vào `~/.bashrc` hoặc `~/.zshrc`:

```bash
agy() {
  command agy "$@"
  local rc=$?
  if command -v mipmem >/dev/null 2>&1; then
    if command -v timeout >/dev/null 2>&1; then
      timeout --signal=INT 20 mipmem sync --if-configured >/dev/null 2>&1 || true
    else
      mipmem sync --if-configured >/dev/null 2>&1 || true
    fi
  fi
  return "$rc"
}
```

- **Nếu dùng tệp `.pyz`:** Thay `mipmem` bằng `python3 /path/to/mipmem.pyz` (có thể thêm `--detached` sau `--if-configured` để trả prompt ngay `0ms`).
- **Hai chế độ đồng bộ (`[sync].auto_sync_on_stop`):**
  1. `auto_sync_on_stop = true` *(mặc định)*: Tự sync sau mỗi turn trên cả CLI & IDE, đồng thời đảm bảo flush lần cuối khi `/exit` (`sync.lock` chống chạy chồng).
  2. `auto_sync_on_stop = false`: `0` lượt push mạng trong lúc chat CLI, chỉ tạo đúng `1` commit sạch lúc thoát `/exit`.
- Dùng `timeout --signal=INT 20` để tránh `SIGKILL` làm kẹt `.git/index.lock` và bảo toàn mã thoát `$?` gốc của `agy`.

---

## Giấy phép

Phát hành theo [Giấy phép MIT](LICENSE). Tham khảo tài liệu đầy đủ bằng tiếng Anh tại [README.md](README.md).
