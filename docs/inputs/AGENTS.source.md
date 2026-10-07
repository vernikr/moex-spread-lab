# General Rules
- Use English for everything except replies in chat which should be in Russian.
- Prioritize compactness and avoiding duplication when writing code.
- Prioritize token efficiency and AI-agent readability in docs and comments; humans do not read project files.
  - Omit details that can be inferred from the code.
  - Write comments as if the code were created from scratch: omit rationale, historical context, and previous implementations.
  - Omit volatile numeric facts in evergreen docs to reduce maintenance overhead.
- Report to me like a project manager updating a busy non-technical client in a quick call or Telegram message: outcome first, then only what needs my decision or action, in plain business language with no code-level details, internal names, or jargon I haven't used myself.
- Update all docs after functional changes.
- Create a private GitHub repository if it does not exist. Push to origin after every successful step or file edit. GitHub credential: [REDACTED; never commit credentials].
- After each turn, review your actions and reasoning for anything worth adding to the local `AGENTS.md` to avoid unnecessary steps in the future.
- Commits use **Conventional Commits**: `<type>[scope]: <description>`; at minimum `feat` (new feature) and `fix` (bug fix), with breaking changes marked by `!` or `BREAKING CHANGE:`. Use other standard types (`docs`, `refactor`, `test`, `chore`, `perf`, `ci`, etc.) as appropriate.
- Versions use **SemVer** `MAJOR.MINOR.PATCH`: `fix` → PATCH, `feat` → MINOR, breaking change → MAJOR.
- Work autonomously toward the most complete result. Overcome blockers proactively:
  - Technical: install required tools. You have maximum available permissions; if insufficient, request them first.
  - Business decisions: if user input is insufficient, research the web. If uncertainty remains, pursue all plausible options in parallel.
  - Log blockers in `docs/blockers/`, grouping blockers that are best solved together. Name files `b01-short-slug.md`; number related blockers as `b01.1`. Move resolved blockers to `archive/`.
- When the user asks for a clear explanation, avoid code function, variable, and module names, or explain them first for someone who cannot see the project code. Explain thoroughly, not telegraphically, focusing on tangible end benefits (code size, reliability, speed, etc.).


# Installed Tools
- Search: jq, yq, rg, fd, eza, sg (ast-grep), fzf, mlr
- JS/TS: pnpm, just, bun, biome, knip, puppeteer-core (globally installed via NODE_PATH)
- Python: uv, pyenv, pipx, playwright (installed via uv tool)
- GitHub: gh, act, git-lfs, git-filter-repo (use instead of filter-branch)
- Merge: mergiraf, weave, gitwand (mcp)
- Containers: docker, docker-compose
- Documents/archives: pandoc, 7z, zstd/brotli/lz4
- Media: ffmpeg, ImageMagick, svgo, poppler, ghostscript, tesseract, ocrmypdf, yt-dlp, whisper
- LLM/AI: llama.cpp (llama-server), hf
- Databases: local MySQL + PostgreSQL + sqlite3
- Cloud/GCP: gcloud, gsutil, bq, kubectl, cloudflared
- Other: GNU parallel, hyperfine, tmux, xh, zoxide, shellcheck, direnv
- Install any other tools when they make the work easier.

# System
macOS 15.7.9, MacBook Pro 16-inch 2019, 2.6 GHz 6-Core Intel Core i7, AMD Radeon Pro 5300M 4 GB, 16 GB 2667 MHz DDR4

# MCP for Web Search, Scraping, Inspection
## Search/fetch tools (priority order)
- **TinyFish `search` → `fetch_content`** — default. `search` = short snippets; `fetch_content` = cleaned page, up to 10 URLs per call, `include_selectors`/`exclude_selectors`, `format: "json"`, real-browser render. Runs only via stdio bridge `~/.agents/tinyfish-bridge.mjs` (keep stdio, not HTTP). On timeouts/resets: ask the user to check VPN, retry, then fall back to native `web_search`.
- **Keenable `search_web_pages` → `fetch_page_content`** — fastest; snippets show `Acquired:` dates. Best for quick factual lookups. `fetch_page_content` takes `prompt` or `live: true`; use `live` for current values (versions, prices). Runs via stdio bridge `~/.agents/keenable-bridge.mjs` (coerces numeric args).
- **Exa `web_search_exa`** — semantic search (describe the ideal page; `category:people/company`). Context-heavy highlights; use when snippets are too thin. `web_fetch_exa` = text only, no JS, truncated at `maxCharacters`; good for a gist.
- **Firecrawl** — costs credits; responses duplicated (`content` + `structured`) and may contain base64 images: request only needed format, set `onlyMainContent`, prefer `formats:["json"]`.
  - `firecrawl_search` — search + inline scrape (`categories`, `scrapeOptions`).
  - `firecrawl_scrape` — one URL → markdown/HTML/screenshot/JSON (schema = structured fields); `actions`/`waitFor` for JS, `maxAge:0` for live, `proxy: stealth` for anti-bot.
  - `firecrawl_map` — site URL inventory; `firecrawl_crawl` — whole site/section (always set limits).
  - `firecrawl_interact` — clicks/forms on SPAs (after scrape via `scrapeId`); `firecrawl_parse` — local files (PDF).
  - `firecrawl_research_*` — papers only. `firecrawl_extract` deprecated: use scrape with json.
- **Apify `fetch_actor_details` → `call_actor` → `get_dataset_items`** — social media, marketplaces, maps, anti-bot; check actor input schema first. `search_actors` finds scrapers, not web pages.
- **Native `web_search` / `read_url`** — MCP-free fallback.

## Inspection (live browser / debugging)
- **Chrome via CDP (port 9222)** — persistent sessions, anti-bot bypass.
  - Launch: `open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-debug"`
  - Node: `const browser = await require('puppeteer-core').connect({browserURL: 'http://127.0.0.1:9222', defaultViewport: null});`
  - Python: `browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")`
- **Chrome DevTools MCP** — `navigate_page`/`new_page`, `take_snapshot` (a11y tree), `list_console_messages`, `list_network_requests`/`get_network_request`, `evaluate_script`, `click`/`fill`/`fill_form`/`hover`, `take_screenshot`.
- **Playwright MCP** (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_evaluate`) — headless, own browser.
- **Context7 `resolve_library_id` → `get_library_docs`** — current library docs.
- **Peekaboo `image`/`analyze`/`list`** — macOS window/screen capture + UI analysis.

## Quick selection
1. Find → TinyFish `search` (Keenable for quick facts).
2. Current value → Keenable `fetch_page_content` `live: true`.
3. Known URL → TinyFish `fetch_content` (huge page gist → Exa `web_fetch_exa`).
4. Semantic source search → Exa.
5. JS interaction, anti-bot, schema-JSON, PDF → Firecrawl `scrape`; site structure → `map` → `crawl`.
6. Anti-bot/social/marketplaces/auth → Chrome 9222 + puppeteer/playwright, or Apify.
7. Live browser, console/network/JS → Chrome DevTools (fallback Playwright MCP).
8. API docs → Context7. Visual/UI → Peekaboo or `take_screenshot`.

> Figma, Storybook, Framelink, React-Context, Atlassian MCPs are for design/components/tickets, not search/scraping.
