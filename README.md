# Social Autopilot — LinkedIn + X poster for Codepaper / UpliftAI

Auto-generates and posts 3 value-driven posts/day to LinkedIn and X with CTAs
that route leads into your GHL funnel. You feed it ideas via Telegram; it can
also pull viral posts in your niche from LinkedIn + X to inform the writing.

```
Telegram (you)  ─► SQLite queue
                       ▼
            APScheduler 3x/day
                       ▼
         Claude (writes LI + X variant)
                       ▼
              Postsyncer API
                       ▼
            LinkedIn + Twitter/X
                       ▼
         GHL funnel (UTMs auto-captured)
```

Postsyncer is a unified social posting platform — you connect your LinkedIn
and X accounts inside Postsyncer once, and this app just calls Postsyncer's
API. **You do not need direct LinkedIn or X API access.**

---

## 1. One-time setup

### 1.1 Postsyncer
1. Sign in to [postsyncer.com](https://postsyncer.com), connect your **LinkedIn**
   and **X** accounts inside its dashboard.
2. From Settings → API Integrations: generate an **API key** and paste it as
   `POSTSYNCER_API_KEY` in `.env`.
3. Run `python -m scripts.bootstrap_postsyncer` — it prints
   `POSTSYNCER_WORKSPACE_ID`, `POSTSYNCER_LINKEDIN_ACCOUNT_ID`, and
   `POSTSYNCER_X_ACCOUNT_ID` ready to paste into `.env`.

### 1.2 Telegram bot (free, 2 minutes)
1. In Telegram, message **@BotFather**, send `/newbot`, follow prompts.
2. Copy the bot token → `TELEGRAM_BOT_TOKEN` in `.env`.
3. Message **@userinfobot** to get your numeric Telegram user ID
   → `TELEGRAM_ALLOWED_USER_ID` in `.env`. This is the only ID the bot will
   accept ideas from.
4. Tap your new bot, hit **Start** to open the chat.

### 1.3 GHL funnel
- Paste the **full URL** of the GHL form/funnel/calendar you want leads to land
  on → `CTA_URL`. UTMs are appended per-post for attribution.
- ⚠ GHL auto-captures UTMs **only on native GHL forms** and **only when the
  form submission happens on the same page that received the UTMs**. See §4.

### 1.4 Apify (viral discovery — optional)
1. Sign up at [apify.com](https://apify.com), grab a token from Settings → Integrations.
2. Default actors (overrideable in `.env`):
   - LinkedIn: `scarletapi/linkedin-viral-posts-finder`
   - X: `apidojo/twitter-scraper-lite`
3. Cost: ~**$1–$3 per 1,000 posts**. Default config (≈100/day) ≈ a few $/month.
4. Disable any time with `APIFY_ENABLED=false`.

---

## 2. Local dev

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# …edit .env with your credentials…

# Smoke test: fires ONE post cycle right now (Claude + Postsyncer)
python -m scripts.run_once

# Run the full app (Telegram bot + scheduler)
python -m src.main
```

When running, message your Telegram bot any text — it gets queued and used on
the next scheduled slot.

### Telegram commands
- *(any text)* — queue an idea
- `/list` — show queued ideas
- `/recent` — show recent posts (status + snippet)
- `/skip <id>` — drop an idea from the queue
- `/status` — system status

---

## 3. Deploy to Railway

1. Push this repo to GitHub.
2. New Railway project → **Deploy from GitHub repo**.
3. Add a **Volume** mounted at `/data` (SQLite lives there so it survives
   redeploys). Default `DB_PATH=/data/autopilot.db` matches.
4. Paste all env vars from `.env.example`.
5. Deploy. Logs should show:
   ```
   DB ready at /data/autopilot.db
   Scheduler started
   Scheduled post cycle at 09:00 America/Toronto
   Scheduled post cycle at 13:00 America/Toronto
   Scheduled post cycle at 18:00 America/Toronto
   Telegram bot starting (long-polling)
   ```

No public URL or webhook needed — the bot connects out to Telegram via long-polling.

---

## 4. GHL attribution gotchas (read once)

UTM auto-capture works **only if**:
- The CTA URL points at a **native** GHL funnel / form / calendar / survey /
  chat widget / order form — not an external embed.
- The form submission happens **on the same page** the user landed on. If your
  funnel redirects before showing the form, UTMs are lost. Use a popup form on
  the landing page if you have a multi-step flow.

To check attribution: GHL → Contacts → filter by
`utm_campaign = social_autopilot`.

---

## 5. How content works

Each scheduled cycle:
1. Pull the oldest queued idea (or fall back to a viral-inspired prompt).
2. Read the 4 most recent high-engagement LinkedIn + X posts in your niche
   from the DB (populated by daily Apify refresh).
3. Claude generates **two variants**: LinkedIn long-form (~1,200 chars) +
   X short-form (≤240 chars + URL), following the brand voice rules in
   [`config.yaml`](config.yaml).
4. CTA URL with `utm_source={linkedin|x}&utm_campaign=social_autopilot&utm_content=<timestamp>`
   is appended to each.
5. Sent to Postsyncer → posted/scheduled to the connected accounts.

Tune voice, audience, offers, and viral keywords in `config.yaml` — no code
changes needed.

---

## 6. Files

```
social-autopilot/
├── config.yaml              ← brand voice, audience, offers, keywords (edit me)
├── .env.example             ← copy to .env, fill in credentials
├── requirements.txt
├── runtime.txt              ← pins Python 3.11 for Railway
├── Procfile / railway.toml  ← deploy config
├── src/
│   ├── main.py              ← entry point (Telegram + scheduler)
│   ├── config.py            ← loads env + yaml
│   ├── db.py                ← SQLite schema + helpers
│   ├── telegram_bot.py
│   ├── scheduler.py         ← post cycle + viral refresh jobs
│   ├── content/
│   │   ├── generator.py     ← Claude prompts (with prompt caching)
│   │   └── viral_discovery.py ← Apify actors → DB
│   └── publishers/
│       └── postsyncer.py
└── scripts/
    ├── run_once.py             ← fire one post cycle manually
    ├── refresh_viral.py        ← run Apify discovery manually
    └── bootstrap_postsyncer.py ← print workspace + account IDs for .env
```

---

## 7. Day-to-day flow

- **Morning:** message your Telegram bot 1–3 raw ideas (a phrase, a story
  fragment, a client win). Bot queues them.
- **9 / 13 / 18 (ET):** scheduler fires. Queue drains first, viral fallback
  kicks in if empty.
- **Evening:** `/recent` in Telegram to skim what went out. Edit `config.yaml`
  if the voice drifts.
- **Weekly:** check GHL → Contacts → filter by `utm_campaign=social_autopilot`
  to see leads attributed to the autopilot.
