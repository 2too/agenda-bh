# Agenda BH

Every Friday morning this app reads 7 event websites and 2 Instagram profiles,
pulls out the cultural events for the next 14 days in Belo Horizonte, removes
duplicates, publishes a dashboard and e-mails you a newsletter. All of it runs
on free services.

```
sources.yaml ──► fetch (browser / API / Instagram) ──► extract (JSON-LD or Gemini AI)
             ──► clean dates & prices ──► merge duplicates ──► SQLite (data/events.db)
             ──► site/index.html (dashboard) + newsletter e-mail + run report
```

## What runs where (all free)

| Part | Service |
|---|---|
| Weekly schedule + running the code | GitHub Actions |
| Dashboard hosting | GitHub Pages (the repository must be **public** on the free plan) |
| Reading pages, captions and flyers | Google Gemini API, free tier |
| Newsletter | Your Gmail |
| Instagram | instaloader with a secondary account, or the official Graph API |

The repository being public means the code and the events database are
visible. Your passwords and keys are not: they live in GitHub Secrets.

---

## Setup, step by step

### 1. Put the project on GitHub
1. Create a free account at github.com.
2. Click **New repository**, name it `agenda-bh`, choose **Public**, create it.
3. Click **uploading an existing file** and drag in everything from this folder
   (including the `.github` folder). Commit.

### 2. Turn on the dashboard page
Repository **Settings → Pages → Source: GitHub Actions**.
Your dashboard address will be `https://YOUR-USERNAME.github.io/agenda-bh/`.

### 3. Get a free Gemini key
Go to <https://aistudio.google.com/apikey>, sign in with Google, **Create API key**, copy it.

### 4. Instagram: pick one route

**Route A — instaloader (simplest).** Use a *secondary* Instagram account, not
your personal one: Instagram may restrict accounts that automate access.
On your computer, with Python installed:

```
pip install instaloader
instaloader --login YOUR_SECONDARY_USERNAME
```

It asks for the password (and 2FA code) and saves a session file:
- Windows: `C:\Users\<you>\AppData\Local\Instaloader\session-YOUR_SECONDARY_USERNAME`
- Mac/Linux: `~/.config/instaloader/session-YOUR_SECONDARY_USERNAME`

Turn the file into text so it fits in a GitHub secret:
- Windows (PowerShell): `[Convert]::ToBase64String([IO.File]::ReadAllBytes("PATH-TO-FILE")) | Set-Clipboard`
- Mac: `base64 -i PATH-TO-FILE | pbcopy`

The session lasts weeks to months. When the Instagram rows in "Saúde das
fontes" turn orange, log in again and replace the secret. Instagram is more
suspicious of GitHub's servers than of your home connection, so if it keeps
failing, run the app from your own computer instead (see "Running on your
computer").

**Route B — official Graph API.** Needs your Instagram switched to a
Business/Creator account linked to a Facebook Page, plus a Meta developer app.
It only reads profiles that are also Business/Creator accounts. More setup,
but nothing gets blocked. Fill `IG_GRAPH_TOKEN` and `IG_GRAPH_USER_ID`.

If both are set, the app tries B first, then A.

### 5. Gmail app password
1. Turn on 2-Step Verification in your Google account.
2. Go to <https://myaccount.google.com/apppasswords>, create one called "Agenda BH", copy the 16 letters.

### 6. Add the secrets to GitHub
Repository **Settings → Secrets and variables → Actions**.

**Secrets** tab → *New repository secret*, one per line:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | key from step 3 |
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | 16 letters from step 5 |
| `NEWSLETTER_TO` | who receives it, comma-separated |
| `IG_USERNAME` | secondary account (route A) |
| `IG_SESSION_B64` | the text you copied in step 4 (route A) |
| `IG_GRAPH_TOKEN`, `IG_GRAPH_USER_ID` | route B only |

**Variables** tab:

| Name | Value |
|---|---|
| `SITE_URL` | `https://YOUR-USERNAME.github.io/agenda-bh/` |
| `GEMINI_MODEL` | optional; leave empty to use `gemini-flash-latest` |

### 7. First run
**Actions → Agenda BH semanal → Run workflow.** It takes 10–25 minutes. When it
turns green, open your dashboard address. From then on it runs every Friday at
06:17 and sends the newsletter.

---

## Running on your computer

Useful for testing a source or when Instagram blocks GitHub.

```
python -m venv .venv
.venv\Scripts\activate            # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env            # Mac/Linux: cp — then fill in .env
```

| Command | What it does |
|---|---|
| `python -m agenda.main --sample` | dashboard with sample data, no internet needed |
| `python -m agenda.main --source "Sympla"` | tests one source and prints what it found (saves nothing) |
| `python -m agenda.main --dry-run` | full run, saves and sends nothing |
| `python -m agenda.main` | full run, saves, builds `site/index.html` |
| `python -m agenda.main --email` | same, plus sends the newsletter |
| `python -m agenda.main --rebuild` | rebuilds the dashboard from saved data |
| `python -m pytest -q` | runs the tests |

Open `site/index.html` in your browser to see the dashboard, and
`site/newsletter-preview.html` to see the e-mail.

## Adding or changing sources

Edit `sources.yaml`. A new website is usually just:

```yaml
  - name: Nome do site
    type: web
    url: https://site.com.br/agenda
    enabled: true
```

For news-style sites where the agenda page only lists headlines, add
`follow_links` with a pattern for article links (see the existing entries).
A new Instagram profile:

```yaml
  - name: '@perfil'
    type: instagram
    handle: perfil
    enabled: true
```

Then test it: `python -m agenda.main --source "Nome do site"`.

## When something breaks

- **One source fails:** the run continues. The source shows as "falhou" in
  "Saúde das fontes", and last week's events from it stay up for 10 days.
  The reason is in the run log (Actions → the run → **run-log** download).
- **A site changed its layout:** most of the time nothing breaks, because the
  AI reads the page text instead of fixed HTML positions. If a news site's
  article links changed, fix the `include` pattern in `follow_links`.
- **"Gemini kept refusing (rate limit)":** the free tier has per-minute and
  per-day limits. The app already waits between calls and remembers pages it
  has read before (unchanged pages cost nothing). Lower `max` in
  `follow_links` or `instagram_max_posts` if it keeps happening.
- **Wrong or missing event details:** the AI can misread a flyer. Every event
  links to its source; the dashboard footer tells readers to confirm there.

## Project layout

```
sources.yaml              sources and settings
agenda/
  main.py                 the weekly run and command-line options
  config.py               settings, .env, time window
  fetchers/web.py         Playwright browser, JSON-LD, link following, robots.txt
  fetchers/mapas.py       Mapa Cultural BH API (falls back to the page)
  fetchers/instagram.py   Graph API / instaloader
  extract.py              JSON-LD conversion and the Gemini prompt
  normalize.py            Portuguese dates, prices, categories, neighbourhoods
  dedupe.py               merges the same event from different sources
  storage.py              SQLite: events, AI cache, run reports
  output.py               dashboard, CSV/JSON export, newsletter, Gmail
  templates/              dashboard and e-mail HTML
tests/test_core.py        30 tests, no internet needed
.github/workflows/weekly.yml
```
