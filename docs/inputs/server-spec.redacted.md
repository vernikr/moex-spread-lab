# Server Specification & Deployment Guide (`<VPS_HOST>`)

## 1. General Server Information

- **IPv4 Address:** `<VPS_HOST>`
- **Location:** Poland (Gdańsk), Polish GeoIP
- **OS:** Ubuntu 24.04 LTS (`x86_64`, Linux kernel 6.8 with TCP BBR enabled)
- **Hardware:** 1 vCPU, 1 GB RAM + 2 GB Swap (`/swapfile`, `vm.swappiness=10`), 20 GB SSD
- **Primary Role:** Personal VPN server (`3X-UI` / `Xray`: VLESS + Reality) with spare capacity for lightweight background tasks (`tldr-digest`)
- **SSH Access:** Port `22` (`root@<VPS_HOST>` or `agent@<VPS_HOST>` with passwordless `sudo`)

---

## 2. CRITICAL: Do Not Touch (VPN Protection Rules)

The server runs a live VPN service in production. **Violating any of the rules below will break VPN connectivity or sever SSH access:**

1. **NEVER bind, close, or remap ports:**
   - `443/tcp` — `xray` (VLESS + Reality disguised as TLS)
   - `20724/tcp` — `3X-UI` management panel
2. **NEVER stop, disable, or modify:**
   - Systemd unit `x-ui.service`
   - Directory `/usr/local/x-ui` and its SQLite database `/etc/x-ui/x-ui.db`
3. **NEVER revert kernel/network tuning:**
   - `/etc/sysctl.d/99-vpn-bbr.conf` (TCP BBR + buffers)
   - `/etc/gai.conf` (IPv4 priority over IPv6: `precedence ::ffff:0:0/96  100`)
4. **NEVER modify `ufw` / `iptables` in a way that blocks ports `22`, `443`, or `20724`.**
5. **Memory & Disk Discipline:**
   - Background services must stay within **250–350 MB RAM** (`MemoryMax=350M` in `systemd`) so the Linux OOM killer never threatens `xray`.
   - Use **`uv`** (`uv venv` + `uv pip install --no-cache`) to avoid bloating the 20 GB SSD with pip caches or wheel archives.

---

## 3. Pre-Installed Environment

- **System Utilities:** `git`, `curl`, `wget`, `jq`, `unzip`, `htop`, `tmux`, `ripgrep (rg)`, `fd-find`, `tree`, `build-essential`, `ufw`, `fail2ban`
- **Python & Package Manager:**
  - Python `3.12` (`python3`, `python3-venv`, `python3-dev`)
  - **`uv`** (`/usr/local/bin/uv` or `/root/.local/bin/uv`) — fast Rust-based Python package/venv manager
  - Note: Ubuntu 24.04 enforces **PEP 668** (`externally-managed-environment`). Never run global `pip install`; always install into `/opt/apps/<project_name>/.venv` via `uv`.
- **Journald Cap:** `SystemMaxUse=100M`

---

## 4. `tldr-digest` Deployment Architecture

### Directory Layout (`/opt/apps/tldr-digest`)

```text
/opt/apps/tldr-digest/
├── .env                  # Secrets (chmod 600): GEMINI_API_KEYS, OPENROUTER_API_KEY, JINA_API_KEY, etc.
├── .venv/                # Virtual environment managed by uv (--no-cache)
├── config.yaml           # Pipeline configuration
├── requirements.txt      # Python dependencies (including curl_cffi and trafilatura)
├── prompts/              # LLM prompt templates
├── scripts/              # setup_server.sh, run_remote.sh, test_403.py
├── src/                  # Python source modules
├── data/                 # Persistent state: seen.json, sponsors.json, llm_state.json
└── digests/              # Generated Markdown digests: YYYY-MM-DD.md
```

> **Note:** `/opt/apps/tldr-digest` on `<VPS_HOST>` is deployed via `rsync` (not `git clone`). Always deploy code updates from your workstation using `bash scripts/run_remote.sh`.

### Systemd Service & Timer

Configured automatically by `scripts/setup_server.sh`:

- **`/etc/systemd/system/tldr-digest.service`**
  - Runs `/opt/apps/tldr-digest/.venv/bin/python -m src.main`
  - Loads `/opt/apps/tldr-digest/.env`
  - Enforces `MemoryMax=350M`
  - Logs to `journald` (`SyslogIdentifier=tldr-digest`)
- **`/etc/systemd/system/tldr-digest.timer`**
  - Schedule: `Mon..Fri *-*-* 15:00:00 UTC` (**18:00 MSK**)
  - `Persistent=true`

### Operational Findings from Production Runs

1. **Cloudflare / HTTP 403 on Datacenter IP (`AS174`):**
   - Plain `requests` was blocked (`HTTP 403`) on 10–15% of publisher URLs (OpenAI, PCMag, FastCompany, Medium/UX Collective, NYT).
   - Switching primary fetching to `curl_cffi` (`impersonate="chrome"`) recovered **50% of previously blocked URLs** directly from the server (`pcmag.com`, `openai.com`, `help.openai.com`, `nordicapis.com`, `lastmuseum.com`).
   - Adding `JINA_API_KEY` to `/opt/apps/tldr-digest/.env` enables `https://r.jina.ai/<url>` to bypass Turnstile/JS challenges on additional sites (`uxdesign.cc`, `fastcompany.com`).
2. **Gemini Quota Persistence & Multi-Key Pool:**
   - Daily `HTTP 429` cooldowns are persisted in `/opt/apps/tldr-digest/data/llm_state.json` (12-hour TTL per key+model) so subsequent runs immediately start on the strongest unexhausted Flash model.
   - Up to 10 Gemini keys can be configured in `.env` (`GEMINI_API_KEYS=key1,key2,...` or `GEMINI_API_KEY_1`..`10`). The client always picks the key with the strongest available Flash model first.
3. **Local Digest Storage Without SMTP:**
   - When `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, or `MAIL_TO` are unset, `src/main.py` skips SMTP delivery, writes `/opt/apps/tldr-digest/digests/YYYY-MM-DD.md`, and updates `data/seen.json` cleanly.

---

## 5. Useful Commands

```bash
# Deploy local code, run the service with live logs, and pull digests back:
bash scripts/run_remote.sh

# Force a fresh rebuild of today's digest (clears seen.json on server):
bash scripts/run_remote.sh --fresh

# Check timer schedule and service status on the server:
ssh root@<VPS_HOST> "systemctl status tldr-digest.timer tldr-digest.service"

# View recent logs on the server:
ssh root@<VPS_HOST> "journalctl -u tldr-digest.service -n 100 --no-pager"

# Provision the system-wide Real Chrome + X11 Mouse Browser Agent (/opt/apps/browser-agent, 127.0.0.1:8765):
bash scripts/run_remote.sh --setup-browser
```

See [`docs/browser-agent.md`](browser-agent.md) for full details on using the Real Chrome + X11 Mouse Browser Agent (`browser-agent` CLI, `http://127.0.0.1:8765` HTTP API, and Python SDK) from any project on `<VPS_HOST>`.
