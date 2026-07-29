# AI Daily Digest

Collects the day's AI/tech news from eight leading sources, summarizes each
article with **Google Gemini** (in Vietnamese), and emails a responsive HTML
digest every morning at **07:00 ICT (UTC+7)**.

- **Fetch** — RSS + BeautifulSoup scraping, 24-hour window, URL and fuzzy-title dedup
- **Summarize** — `gemini-2.5-flash` returns structured JSON: TL;DR, 3 takeaways, a 1–5 importance score, and tags
- **Deliver** — Jinja2 HTML email with light/dark support, a Top-3 "must-read" block, and per-publisher sections
- **Automate** — GitHub Actions cron, or run it locally

A failure anywhere degrades instead of stopping: a dead feed is logged and
skipped, and if Gemini is missing or rate-limited every article still gets an
extractive summary.

---

## Quick start

```bash
git clone <your-repo-url> ai-daily-digest
cd ai-daily-digest
python -m venv .venv
```

Activate the virtualenv:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Then install and preview — `--dry-run` sends nothing and needs no credentials:

```bash
pip install -r requirements.txt
python main.py --dry-run
```

The preview prints to the terminal and writes `output/digest-YYYY-MM-DD.html`.
Open that file in a browser to check the layout.

---

## Configuration

Copy `.env.example` to `.env` and fill it in. Every variable is also read
straight from the environment, which is how GitHub Actions supplies them.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | for AI summaries | — | [Google AI Studio](https://aistudio.google.com/app/apikey) key. Missing → extractive fallback |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | Any Gemini model id |
| `SMTP_SERVER` | to send | `smtp.gmail.com` | SMTP host |
| `SMTP_PORT` | to send | `465` | `465` = implicit TLS, anything else = STARTTLS |
| `SENDER_EMAIL` | to send | — | Account that sends the digest |
| `SENDER_PASSWORD` | to send | — | Gmail **App Password**, not your login password |
| `RECEIVER_EMAIL` | to send | — | Recipient(s), comma-separated |
| `SENDER_NAME` | no | `AI Daily Digest` | From: display name |
| `LOOKBACK_HOURS` | no | `24` | Collection window |
| `MAX_ARTICLES` | no | `25` | Cap on articles sent to Gemini (cost control) |
| `SUMMARY_WORKERS` | no | `4` | Parallel Gemini calls — lower it if rate-limited |
| `SUMMARY_MAX_RETRIES` | no | `3` | Attempts before falling back for that article |
| `SEND_WHEN_EMPTY` | no | `false` | Send even when nothing was found |

### Gmail App Password

Gmail rejects your normal password over SMTP. You need a 16-character App
Password:

1. Turn on 2-Step Verification at <https://myaccount.google.com/security>.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create a password named e.g. `ai-daily-digest`.
4. Put the 16 characters (spaces optional) in `SENDER_PASSWORD`.

Verify it end-to-end before trusting the schedule:

```bash
python main.py --test-email
```

Other providers work too — set `SMTP_SERVER` / `SMTP_PORT` accordingly
(Outlook: `smtp.office365.com:587`, Zoho: `smtp.zoho.com:465`).

---

## CLI

```bash
python main.py                      # fetch -> summarize -> send
python main.py --dry-run            # preview: terminal + HTML file, sends nothing
python main.py --test-email         # verify SMTP credentials only
python main.py --no-llm             # skip Gemini, use the extractive fallback
python main.py --hours 48           # widen the collection window
python main.py --limit 10           # cap articles (useful while testing)
python main.py --dry-run --verbose  # debug logging
```

| Flag | Effect |
|---|---|
| `--dry-run` | Fetch and summarize, print and write HTML, send nothing |
| `--test-email` | Send a short credential test, then exit |
| `--no-llm` | Bypass Gemini entirely |
| `--limit N` | Max articles to summarize |
| `--hours H` | Lookback window |
| `--sources PATH` | Alternate `sources.yaml` |
| `--env PATH` | Alternate `.env` |
| `--output PATH` | Where to write the HTML |
| `-v` / `-q` | Debug / warnings-only logging |

Exit codes: `0` success, `1` runtime error, `2` configuration error.

---

## Sources

Edit `config/sources.yaml` to add, remove, or disable publishers
(`enabled: false`). Each entry is `type: rss` or `type: html`, and may declare
a `fallback:` block used only when the primary comes up empty.

| Publisher | Transport | Notes |
|---|---|---|
| OpenAI | RSS | `openai.com/news/rss.xml` |
| Anthropic | **HTML scrape** | No RSS exists — every feed URL 404s |
| Google DeepMind | RSS | Posts in bursts, not daily |
| Meta AI & Engineering | RSS | `engineering.fb.com` — see below |
| Hugging Face | RSS | Community blog, high volume |
| Microsoft AI | RSS | `news.microsoft.com` AI topic feed |
| MIT Technology Review | RSS | AI section |
| arXiv cs.AI | RSS | Capped at 5/day to avoid flooding |

Three URLs from the original spec are dead as of 2026-07-29 and were replaced
with verified-live endpoints:

| Original | Status | Replacement |
|---|---|---|
| `anthropic.com/rss.xml` | 404 | HTML scrape of `/news` |
| `ai.meta.com/blog/rss/` | 404 | `engineering.fb.com/feed/` |
| `blogs.microsoft.com/ai/feed/` | 410 Gone | `news.microsoft.com/source/topics/ai/feed/` |

**Anthropic** is scraped because no feed exists. Its cards carry a date, but
only to day precision, so a post from yesterday afternoon can fall outside a
24-hour window — use `--hours 48` if you would rather over-collect. Scrapers
break when markup changes; if Anthropic goes quiet in the digest, re-check the
selectors in `sources.yaml`.

**Meta** publishes no feed on `ai.meta.com/blog` *and* its cards carry no
dates, so scraping it would re-surface the same old posts every single day.
The digest uses Meta's engineering blog instead, which is properly dated.

---

## Automation

### GitHub Actions

`.github/workflows/daily_digest.yml` runs at `0 0 * * *` UTC = **07:00 ICT**.

Add repository secrets under **Settings → Secrets and variables → Actions →
Secrets**:

- `GEMINI_API_KEY`
- `SENDER_EMAIL`
- `SENDER_PASSWORD`
- `RECEIVER_EMAIL`

Optionally add non-secret **Variables** to override defaults: `GEMINI_MODEL`,
`SMTP_SERVER`, `SMTP_PORT`, `SENDER_NAME`, `MAX_ARTICLES`, `SUMMARY_WORKERS`.

Trigger a manual run from the **Actions** tab — the `workflow_dispatch` inputs
let you tick *dry run* or *no LLM* to test without emailing anyone. Every run
uploads the rendered HTML as a downloadable artifact.

> GitHub's cron is best-effort and can lag by several minutes under load. If
> you need the mail to land at exactly 07:00, schedule it a little earlier.

### Local cron

```bash
0 7 * * * cd /path/to/ai-daily-digest && .venv/bin/python main.py >> digest.log 2>&1
```

Cron uses the machine's local timezone, so this assumes the host is on ICT.

### Windows Task Scheduler

```powershell
schtasks /Create /SC DAILY /ST 07:00 /TN "AI Daily Digest" /TR "C:\path\to\ai-daily-digest\.venv\Scripts\python.exe C:\path\to\ai-daily-digest\main.py"
```

---

## Project layout

```
ai-daily-digest/
├── main.py                        # CLI entry point
├── config/sources.yaml            # publishers + fetch defaults
├── src/
│   ├── config.py                  # .env + sources.yaml loading
│   ├── fetcher.py                 # RSS/HTML collection, 24h window, dedup
│   ├── summarizer.py              # Gemini client + extractive fallback
│   └── emailer.py                 # digest assembly, rendering, SMTP
├── templates/email_template.html  # Jinja2 email
├── .github/workflows/daily_digest.yml
├── requirements.txt
└── .env.example
```

**Pipeline:** `collect()` fetches every enabled source (isolating failures),
filters to the window, and dedups by canonical URL then fuzzy title →
`summarize_articles()` runs Gemini in a small thread pool, degrading per
article → `build_digest()` ranks by importance, picks the top 3, groups the
rest by publisher → `send_digest()` renders and sends `multipart/alternative`.

---

## How it degrades

| Failure | Behaviour |
|---|---|
| One feed 404s or times out | Logged, skipped, named in the email footer |
| A scraper's markup changed | That source returns 0 articles; the rest still ship |
| `GEMINI_API_KEY` missing | All summaries use the extractive fallback |
| Gemini rate-limits (429) | Exponential backoff, then per-article fallback |
| 3 consecutive Gemini failures | Circuit breaker skips the API for the rest of the run |
| No articles found | Nothing is sent unless `SEND_WHEN_EMPTY=true` |
| SMTP credentials wrong | Checked *before* any Gemini spend; exits `2` with a specific message |

The extractive fallback keeps the source language (usually English) rather than
translating — without a model, "translating" would mean inventing text.

---

## Notes

- Requires **Python 3.10+** (verified running on 3.9.13 as well; no 3.10-only syntax is used).
- Summaries are AI-generated. Read the original before acting on anything.
- `.env` is gitignored. Never commit credentials.
