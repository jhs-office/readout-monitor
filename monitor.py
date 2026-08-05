"""
Clinical trial readout monitor.

Polls Alpaca's Benzinga-sourced news feed for a watchlist of tickers, filters for
likely trial-readout announcements, has Claude classify the survivors against the
specific events you're tracking, and pushes alerts to Slack and/or email.

Designed to run on a GitHub Actions cron. State lives in state.json, which is
committed back to the repo every run (this also resets GitHub's 60-day
scheduled-workflow inactivity timer).
"""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config

ALPACA_KEY = os.environ["ALPACA_KEY_ID"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL")  # optional dead-man's switch

# Notification channels. Configure either or both — whatever is set gets used.
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")

if not (SLACK_WEBHOOK or (RESEND_API_KEY and EMAIL_TO) or (SMTP_HOST and EMAIL_TO)):
    sys.exit("No notification channel configured. Set SLACK_WEBHOOK_URL, "
             "or RESEND_API_KEY + EMAIL_TO, or SMTP_HOST + SMTP_USER + "
             "SMTP_PASS + EMAIL_TO.")

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

WATCHLIST_PATH = "watchlist.json"
STATE_PATH = "state.json"

SYMBOL_CHUNK = 50        # symbols per API request
LOOKBACK_FLOOR_MIN = 90  # on first run / stale state, look back this far
OVERLAP_MIN = 5          # re-scan window to tolerate feed reordering
MAX_SEEN_IDS = 3000      # cap state file growth

# Stage-1 cheap filter. An article must hit at least one of these to be worth
# spending a Claude call on. Deliberately loose — recall matters more than
# precision here, since stage 2 does the real work.
KEYWORDS = [
    "topline", "top-line", "primary endpoint", "secondary endpoint",
    "met the", "did not meet", "failed to meet", "statistically significant",
    "phase 1", "phase 2", "phase 3", "phase i", "phase ii", "phase iii",
    "interim", "readout", "read out", "data from", "results from",
    "clinical data", "trial results", "study results", "efficacy",
    "announces positive", "announces negative", "reports positive",
    "pivotal", "registrational", "presented at", "late-breaking",
    "durable response", "complete response", "well tolerated",
    "clinical hold lifted",
]


# ---------------------------------------------------------------- helpers

def http_json(url, headers=None, data=None, method="GET", timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_run": None, "seen_ids": []}


def save_state(state):
    # Always written, even on a no-news run — this is what keeps the
    # scheduled workflow from being auto-disabled after 60 days.
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["seen_ids"] = state["seen_ids"][-MAX_SEEN_IDS:]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- fetch

def fetch_news(symbols, start_iso):
    """Pull all articles for the given symbols since start_iso, following pagination."""
    articles = []
    for batch in chunks(symbols, SYMBOL_CHUNK):
        page_token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "start": start_iso,
                "sort": "asc",
                "limit": 50,
                "include_content": "true",
            }
            if page_token:
                params["page_token"] = page_token
            url = f"{NEWS_URL}?{urllib.parse.urlencode(params)}"
            try:
                resp = http_json(url, headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                })
            except Exception as e:
                print(f"[warn] news fetch failed for {batch[0]}...: {e}", file=sys.stderr)
                break
            articles.extend(resp.get("news", []))
            page_token = resp.get("next_page_token")
            if not page_token:
                break
    return articles


# ---------------------------------------------------------------- filter

class _TextExtractor(HTMLParser):
    """Pulls plain text out of Benzinga's HTML article body."""

    SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


def strip_html(raw):
    if not raw:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(raw)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw).strip()


def article_body(article):
    """Full article text when Alpaca's include_content gave us one, else the
    short Benzinga summary. Filtering on summary alone misses readout details
    that only appear in the body."""
    return strip_html(article.get("content")) or (article.get("summary") or "")


def keyword_hit(article):
    blob = " ".join([
        article.get("headline") or "",
        article_body(article)[:20000],
    ]).lower()
    return any(k in blob for k in KEYWORDS)


def relevant_events(article, watchlist):
    """The events we're tracking for whichever watchlist tickers this article tags."""
    out = []
    for sym in article.get("symbols", []):
        for ev in watchlist.get(sym.upper(), []):
            out.append({"ticker": sym.upper(), **ev})
    return out


# ---------------------------------------------------------------- classify

CLASSIFY_PROMPT = """You are screening financial news for a clinical trial readout tracker.

Below is a news article and the specific trial events we are tracking for the \
company mentioned. Decide whether this article is announcing an actual clinical \
trial data readout for one of the tracked events.

Do NOT flag: financing, offerings, conference-attendance announcements, executive \
appointments, index inclusion, analyst ratings, general corporate updates, or \
trial *initiations* and enrollment starts. Those are not readouts.

DO flag: topline results, interim analyses, primary/secondary endpoint outcomes, \
detailed data presentations at medical conferences, and trial-stopping decisions \
(futility, early success).

<article>
Headline: {headline}
Source: {source}
Published: {created_at}
Symbols: {symbols}
Article text: {body}
</article>

<tracked_events>
{events}
</tracked_events>

Respond with ONLY a JSON object, no markdown fences, no preamble:
{{
  "is_readout": true or false,
  "matched_product": "the drug name from tracked_events this concerns, or null",
  "matched_indication": "the indication from tracked_events this concerns, or null",
  "outcome": "positive" | "negative" | "mixed" | "unclear",
  "confidence": "high" | "medium" | "low",
  "one_line": "one sentence describing what was announced"
}}"""


def classify(article, events):
    prompt = CLASSIFY_PROMPT.format(
        headline=article.get("headline", ""),
        source=article.get("source", ""),
        created_at=article.get("created_at", ""),
        symbols=", ".join(article.get("symbols", [])),
        body=article_body(article)[:4000],
        events=json.dumps(events, indent=2)[:6000],
    )
    try:
        resp = http_json(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
            },
            data={
                "model": MODEL,
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        text = "".join(
            b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"
        )
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[warn] classify failed: {e}", file=sys.stderr)
        # Fail open: a missed alert is worse than a false one.
        return {
            "is_readout": True,
            "matched_product": None,
            "matched_indication": None,
            "outcome": "unclear",
            "confidence": "low",
            "one_line": "Classifier unavailable — flagged for manual review.",
        }


# ---------------------------------------------------------------- notify

OUTCOME_ICON = {"positive": "🟢", "negative": "🔴", "mixed": "🟡", "unclear": "⚪"}


def build_alert(article, verdict, events):
    """Normalise everything the channels need into one dict."""
    product = verdict.get("matched_product")
    indication = verdict.get("matched_indication")
    # A company often has several tracked events for the same drug in different
    # indications, so match on both before falling back.
    tracked = next(
        (e for e in events
         if product and e.get("product") == product
         and indication and e.get("indication") == indication),
        None,
    ) or next(
        (e for e in events if product and e.get("product") == product),
        events[0] if events else {},
    )
    # Only surface tickers we actually track — Benzinga tags peers and indices too.
    tickers = ", ".join(sorted({e["ticker"] for e in events}))

    return {
        "icon": OUTCOME_ICON.get(verdict.get("outcome"), "⚪"),
        "company": tracked.get("company") or "Unknown",
        "tickers": tickers,
        "headline": article.get("headline", ""),
        "url": article.get("url", ""),
        "one_line": verdict.get("one_line") or "",
        "source": article.get("source", "news"),
        "published": article.get("created_at", ""),
        "fields": [
            ("Drug", product or tracked.get("product") or "—"),
            ("Indication", tracked.get("indication") or "—"),
            ("Phase", tracked.get("phase") or "—"),
            ("Event type", tracked.get("event_type") or "—"),
            ("Guided timing", tracked.get("timing") or "—"),
            ("Read confidence", verdict.get("confidence") or "—"),
        ],
    }


def subject_line(a):
    return f"{a['icon']} {a['company']} ({a['tickers']}) — {a['headline']}"


def esc_html(v):
    """Article/model text is untrusted — never interpolate it into HTML raw."""
    return html.escape(str(v), quote=True)


def esc_mrkdwn(v):
    """Slack mrkdwn gives special meaning to &, <, > (e.g. <!channel>, <@user>)."""
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---- Slack -----------------------------------------------------------

def send_slack(a):
    # header blocks are plain_text — Slack doesn't parse mrkdwn syntax there,
    # so no escaping needed. Every other block below is mrkdwn and must escape
    # article/model text, since &, <, > have special meaning (e.g. <!channel>).
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"{a['icon']} {a['company']} ({a['tickers']})"[:150]}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*<{esc_mrkdwn(a['url'])}|{esc_mrkdwn(a['headline'])}>*"}},
    ]
    if a["one_line"]:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": esc_mrkdwn(a["one_line"])}]})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*{esc_mrkdwn(k)}*\n{esc_mrkdwn(v)}"} for k, v in a["fields"]]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"{esc_mrkdwn(a['source'])} · {esc_mrkdwn(a['published'])}"}]})

    payload = {"text": subject_line(a), "blocks": blocks}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(SLACK_WEBHOOK, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    # Slack webhooks reply with the plain string "ok", not JSON — don't parse.
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---- Email -----------------------------------------------------------

def email_html(a):
    rows = "".join(
        f"<tr><td style='padding:4px 16px 4px 0;color:#666;white-space:nowrap'>{esc_html(k)}</td>"
        f"<td style='padding:4px 0'><b>{esc_html(v)}</b></td></tr>"
        for k, v in a["fields"]
    )
    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,sans-serif;max-width:620px">
  <p style="font-size:20px;margin:0 0 4px">{a['icon']} <b>{esc_html(a['company'])}</b>
     <span style="color:#666">({esc_html(a['tickers'])})</span></p>
  <p style="font-size:17px;line-height:1.4;margin:12px 0">
     <a href="{esc_html(a['url'])}" style="color:#0b5cff;text-decoration:none">{esc_html(a['headline'])}</a></p>
  <p style="color:#444;font-style:italic;margin:12px 0">{esc_html(a['one_line'])}</p>
  <table style="font-size:14px;border-collapse:collapse;margin-top:16px">{rows}</table>
  <p style="color:#999;font-size:12px;margin-top:20px">{esc_html(a['source'])} · {esc_html(a['published'])}</p>
</div>"""


def email_text(a):
    fields = "\n".join(f"{k}: {v}" for k, v in a["fields"])
    return (f"{a['icon']} {a['company']} ({a['tickers']})\n\n"
            f"{a['headline']}\n\n{a['one_line']}\n\n{fields}\n\n"
            f"{a['url']}\n\n{a['source']} · {a['published']}")


def send_email_resend(a):
    http_json(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        data={
            "from": EMAIL_FROM,
            "to": [x.strip() for x in EMAIL_TO.split(",")],
            "subject": subject_line(a)[:200],
            "html": email_html(a),
            "text": email_text(a),
        },
        method="POST",
    )


def send_email_smtp(a):
    import smtplib
    from email.message import EmailMessage

    recipients = [x.strip() for x in EMAIL_TO.split(",") if x.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject_line(a)[:200]
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(email_text(a))
    msg.add_alternative(email_html(a), subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg, to_addrs=recipients)


# ---- Dispatch --------------------------------------------------------

def notify(article, verdict, events):
    a = build_alert(article, verdict, events)

    channels = []
    if SLACK_WEBHOOK:
        channels.append(("slack", send_slack))
    if RESEND_API_KEY and EMAIL_TO:
        channels.append(("resend", send_email_resend))
    elif SMTP_HOST and EMAIL_TO:
        channels.append(("smtp", send_email_smtp))

    for name, fn in channels:
        try:
            fn(a)
        except Exception as e:
            print(f"[warn] {name} send failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------- main

def main():
    with open(WATCHLIST_PATH) as f:
        watchlist = json.load(f)
    symbols = sorted(watchlist.keys())

    state = load_state()
    seen = set(state["seen_ids"])

    now = datetime.now(timezone.utc)
    floor = now - timedelta(minutes=LOOKBACK_FLOOR_MIN)
    if state["last_run"]:
        last = datetime.fromisoformat(state["last_run"])
        start = max(last - timedelta(minutes=OVERLAP_MIN), floor)
    else:
        start = floor

    print(f"scanning {len(symbols)} symbols since {start.isoformat()}")
    articles = fetch_news(symbols, start.isoformat())
    print(f"  {len(articles)} articles returned")

    new = [a for a in articles if a.get("id") not in seen]
    print(f"  {len(new)} new")

    candidates = [a for a in new if keyword_hit(a)]
    print(f"  {len(candidates)} passed keyword filter")

    alerted = 0
    for article in candidates:
        events = relevant_events(article, watchlist)
        if not events:
            continue
        verdict = classify(article, events)
        if verdict.get("is_readout"):
            notify(article, verdict, events)
            alerted += 1
            print(f"  ALERT: {article.get('headline')}")

    print(f"  {alerted} alerts sent")

    state["seen_ids"] = state["seen_ids"] + [a["id"] for a in new if "id" in a]
    save_state(state)

    if HEALTHCHECK_URL:
        try:
            urllib.request.urlopen(HEALTHCHECK_URL, timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
