# Clinical Trial Readout Monitor

Watches 207 tickers covering 375 tracked trial events. Polls Alpaca's
Benzinga-sourced news feed every 15 minutes, screens for readout announcements,
has Claude classify the survivors against your specific tracked events, and
pushes alerts to email and/or Slack.

Channels are configured by which secrets you set. Set the email ones and you get
email; add the Slack webhook later and you get both. No code changes either way.

Runs entirely on GitHub Actions. No server.

---

## Setup

Work through these in order. Budget about 45 minutes.

### 1. Alpaca API keys (free, ~5 min)

1. Sign up at <https://alpaca.markets> and open a **paper trading** account.
   No funding, no deposit, no identity verification beyond the basics.
2. From the dashboard, generate an API key pair.
3. Save the **Key ID** and **Secret Key**. The secret is shown once.

The free Basic market-data plan includes news. Note that news is served with a
~15-minute delay unless you have real-time entitlement — that's the single
biggest contributor to end-to-end latency.

### 2. Notification channel (free, ~5 min)

Pick email to start. Slack can be added later without touching code.

#### Email via Resend (recommended)

1. Sign up at <https://resend.com>. No credit card.
2. Create an API key. Save it.
3. Choose a sender based on who needs the alerts:

**Only you?** Send from `onboarding@resend.dev` to your Resend account email.
Zero setup. But note this sandbox sender can *only* reach the address you
registered with — it will 403 on anyone else.

**You and a colleague?** You need a verified sending domain. Two ways:

- *Verify a domain you control.* Register a cheap domain (~$12/yr), add the
  SPF/DKIM records Resend gives you, set `EMAIL_FROM=alerts@yourdomain.com`.
  About 20 minutes, no dependency on anyone else. This is the recommended path.
- *Verify the company domain.* Requires IT to add DNS records for
  `gatchealth.com`. Cleaner-looking sender, but it's another approval queue —
  the same problem you hit with Slack.

Set `EMAIL_TO` to a comma-separated list:
`jacob.s@gatchealth.com, mark.f@gatchealth.com`

Free tier is 3,000 emails/month capped at 100/day, far above what this generates.

Once running, add a mail rule so these bypass your normal inbox noise — filter on
the sender and mark as VIP/important so they push to your phone.

#### Email via your own SMTP (alternative)

No domain verification needed, and you can send to anyone — which makes this the
fastest way to get alerts to both you and a colleague today.

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, and
`EMAIL_TO`. For Gmail: `smtp.gmail.com`, port `587`, and an App Password
(requires 2FA on the account).

Caveat: many corporate Google Workspace tenants disable App Passwords, so a
`@gatchealth.com` account may not work. A personal Gmail will, and can send to
both work addresses without involving anyone's IT.

#### Slack (add whenever approval lands)

A private channel does **not** avoid admin approval — the gate is on installing
the app to the workspace, not on the channel. So there's no shortcut here.

1. Create a channel, e.g. `#trial-readouts`.
2. <https://api.slack.com/apps> → **Create New App** → From scratch.
3. **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**.
4. Pick the channel and authorize. On a restricted workspace this generates an
   admin request instead.
5. Add the resulting URL as the `SLACK_WEBHOOK_URL` secret. That's it — the next
   scheduled run picks it up and you'll get both email and Slack.

Treat the webhook URL as a credential — anyone holding it can post to that
channel. Never echo it in a workflow log.

### 3. Anthropic API key (~$1–3/month at this volume)

Get one at <https://console.anthropic.com>. Only articles that survive the
keyword filter reach the API — expect a handful per day, not hundreds.

### 4. Dead-man's switch (free, optional but recommended)

1. Create a check at <https://healthchecks.io>, period 15 minutes, grace 60 minutes.
2. Copy the ping URL.

Without this, a silently disabled workflow means you simply stop getting alerts
and have no way to notice.

### 5. Build the watchlist

```bash
pip install pandas openpyxl
python build_watchlist.py "Prospective_Prediction_Project_-_July_2026_-_v6.xlsx"
```

Writes `watchlist.json`. Re-run this whenever you update the spreadsheet.

### 6. Create the repo

Make it **public**. Public repos get unlimited Actions minutes; private repos on
the Free plan get 2,000/month, and a 15-minute cron burns roughly 2,880.

The repo contains no secrets and no proprietary data — `watchlist.json` holds
company names, tickers, and drug names, all of which are public information.
If your event list or predictions are sensitive, keep those in a separate
private repo and reconsider the frequency tradeoff.

```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/<you>/readout-monitor.git
git push -u origin main
```

### 7. Add secrets

Repo → Settings → Secrets and variables → Actions → New repository secret.

| Name | Value |
|---|---|
| `ALPACA_KEY_ID` | from step 1 |
| `ALPACA_SECRET_KEY` | from step 1 |
| `ANTHROPIC_API_KEY` | from step 3 |
| `RESEND_API_KEY` | from step 2 |
| `EMAIL_TO` | recipient(s), comma-separated |
| `EMAIL_FROM` | optional; defaults to `onboarding@resend.dev` |
| `SLACK_WEBHOOK_URL` | optional; add when Slack approval lands |
| `HEALTHCHECK_URL` | from step 4 |

### 8. First run

Actions tab → Readout Monitor → **Run workflow**. Watch the log.

On the first run `state.json` doesn't exist, so it scans back 90 minutes. You may
get a small burst of alerts. After that it only looks at genuinely new articles.

---

## Tuning

**Too noisy?** Tighten `KEYWORDS` in `monitor.py`, or make `notify()` skip
verdicts with `"confidence": "low"`.

**Missing things?** Loosen `KEYWORDS`. The keyword filter is the only place an
article can be dropped without Claude ever seeing it, so recall problems almost
always live there. Widen it before touching anything else.

**Want it faster?** Change the cron to `*/5 * * * *`. It won't help much — the
15-minute news delay on Alpaca's free tier dominates, and GitHub throttles
frequent scheduled runs anyway.

---

## Known limits

- **Latency is 30–60 minutes typical.** Free-tier news delay (~15 min) plus cron
  interval (15 min) plus GitHub scheduling lag (5–30 min, occasionally more).
- **Coverage is Benzinga's.** Strong on US-listed names. Thinner on foreign
  issuers — Abivax, Innate Pharma, Valneva, and MoonLake may publish to Euronext
  or their own IR page before Benzinga picks it up.
- **`Timing` in your sheet is coarse.** Most rows say "H2 2026" or "Q4 2026", so
  this monitors continuously rather than anticipating. The conference-abstract
  angle discussed separately is where anticipation actually comes from.
- **Fails open.** If the Claude call errors, the article is alerted anyway with
  low confidence rather than dropped.

---

## Maintenance

Over an 18-month horizon this list will drift. Every quarter:

- **Re-run `build_watchlist.py`** against the current spreadsheet.
- **Check for ticker changes.** Some of these 207 names will be acquired, merge,
  reverse-split, or delist. A dead ticker fails silently — it just never
  produces news. There is no error to catch.
- **Set a spend cap** on the Anthropic console so a runaway loop can't bill you.
