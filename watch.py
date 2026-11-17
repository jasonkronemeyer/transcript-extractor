"""Check YouTube channel feeds for new videos, summarize transcripts with GitHub Models,
write results to summaries/, and notify Microsoft Teams."""
import calendar
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from youtube_transcript_api import YouTubeTranscriptApi

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "processed.json"
OUT_DIR = ROOT / "summaries"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
CHUNK_CHARS = 12000


def llm(model, system, user):
    token = os.environ["GITHUB_TOKEN"]
    for attempt in range(4):
        r = requests.post(
            MODELS_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=180,
        )
        if r.status_code in (429, 500, 502, 503):
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    r.raise_for_status()


def chunk(text, size=CHUNK_CHARS):
    words, parts, cur, n = text.split(), [], [], 0
    for w in words:
        cur.append(w)
        n += len(w) + 1
        if n >= size:
            parts.append(" ".join(cur))
            cur, n = [], 0
    if cur:
        parts.append(" ".join(cur))
    return parts


def fetch_transcript(video_id):
    snippets = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
    text = " ".join(s.text for s in snippets)
    text = re.sub(r"\[[^\]]*\]", " ", text)  # [Music], [Applause]
    return re.sub(r"\s+", " ", text).strip()


def clean(model, raw):
    system = (
        "You clean auto-generated YouTube transcripts. Add punctuation, capitalization and "
        "paragraph breaks, fix obvious mis-transcriptions and remove filler. Do not summarize, "
        "add, or omit content. Return only the cleaned text."
    )
    return "\n\n".join(llm(model, system, c) for c in chunk(raw))


def analyze(model, title, cleaned, questions):
    text = cleaned
    if len(text) > CHUNK_CHARS * 2:  # long video: condense chunks into notes first
        notes = [
            llm(model, "Write detailed bullet-point notes of this transcript section. Keep names, numbers and claims.", c)
            for c in chunk(text)
        ]
        text = "\n\n".join(notes)
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    system = "You analyze video transcripts accurately. Only use information in the transcript; say 'Not covered' if absent."
    user = (
        f"Video title: {title}\n\nTranscript:\n{text}\n\n"
        "Respond in markdown with exactly these sections:\n"
        "## Summary\n(5-7 bullets)\n\n## Answers\n(answer each numbered question below, "
        f"repeating the question as a bold heading)\n\nQuestions:\n{qs}"
    )
    return llm(model, system, user)


def notify_teams(title, url, channel, summary_excerpt, file_url):
    hook = os.environ.get("TEAMS_WEBHOOK_URL")
    if not hook:
        print("TEAMS_WEBHOOK_URL not set; skipping notification")
        return
    actions = [{"type": "Action.OpenUrl", "title": "Watch", "url": url}]
    if file_url:
        actions.append({"type": "Action.OpenUrl", "title": "Full summary", "url": file_url})
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": f"New video: {title}", "weight": "Bolder", "size": "Medium", "wrap": True},
                    {"type": "TextBlock", "text": channel, "isSubtle": True, "wrap": True},
                    {"type": "TextBlock", "text": summary_excerpt, "wrap": True},
                ],
                "actions": actions,
            },
        }],
    }
    try:
        requests.post(hook, json=card, timeout=30).raise_for_status()
    except requests.RequestException as e:
        print(f"Teams notification failed: {e}", file=sys.stderr)


def summary_link(path):
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"{server}/{repo}/blob/{branch}/{path}" if repo else ""


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"seen": {}}
    OUT_DIR.mkdir(exist_ok=True)
    budget = cfg.get("max_videos_per_run", 5)
    give_up = cfg.get("transcript_give_up_hours", 48) * 3600
    now = time.time()

    for ch in cfg["channels"]:
        cid = ch["channel_id"]
        feed = feedparser.parse(FEED_URL.format(cid))
        if not feed.entries:
            print(f"Could not read feed for {ch['name']}", file=sys.stderr)
            continue
        ids = [e.yt_videoid for e in feed.entries]
        if cid not in state["seen"]:
            # First run: don't process the back catalog.
            state["seen"][cid] = ids
            print(f"Initialized {ch['name']} with {len(ids)} existing videos")
            save_state(state)
            continue

        for e in reversed(feed.entries):  # oldest first
            vid = e.yt_videoid
            if vid in state["seen"][cid] or budget <= 0:
                continue
            published = calendar.timegm(e.published_parsed)
            try:
                raw = fetch_transcript(vid)
            except Exception as err:  # transcripts disabled / not yet generated / blocked
                if now - published > give_up:
                    print(f"Giving up on {vid}: {err}")
                    state["seen"][cid].append(vid)
                else:
                    print(f"Transcript not ready for {vid}, will retry: {type(err).__name__}")
                continue

            budget -= 1
            cleaned = clean(cfg["model"], raw)
            analysis = analyze(cfg["model"], e.title, cleaned, cfg["questions"])
            date = datetime.fromtimestamp(published, timezone.utc).strftime("%Y-%m-%d")
            path = OUT_DIR / f"{date}-{slugify(e.title)}-{vid}.md"
            path.write_text(
                f"# {e.title}\n\n- Channel: {ch['name']}\n- Video: {e.link}\n- Published: {date}\n\n"
                f"{analysis}\n\n---\n\n<details><summary>Cleaned transcript</summary>\n\n{cleaned}\n\n</details>\n"
            )
            state["seen"][cid].append(vid)
            save_state(state)
            excerpt = analysis.split("## Answers")[0].replace("## Summary", "").strip()[:800]
            notify_teams(e.title, e.link, ch["name"], excerpt, summary_link(path.relative_to(ROOT).as_posix()))
            print(f"Processed {vid}: {e.title}")

    save_state(state)


if __name__ == "__main__":
    main()
