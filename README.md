# YouTube transcript summarizer

Hourly GitHub Actions job: checks channel RSS feeds → fetches transcripts of new videos → cleans, summarizes and answers your questions with GitHub Models → saves `summaries/*.md` → notifies Microsoft Teams.

## Setup
1. Push this folder to a GitHub repo.
2. Edit `config.yml`: add channels (`channel_id` starts with `UC`; find it in the channel page source or "Share → Copy channel ID") and your questions.
3. In Teams, create a **Workflows** webhook ("Post to a channel when a webhook request is received") and add its URL as the repo secret `TEAMS_WEBHOOK_URL`.
4. Run the workflow manually once (Actions → Check YouTube channels → Run workflow). The first run only records existing videos; only later uploads are processed.

## Notes
- LLM calls use the built-in `GITHUB_TOKEN` (`models: read`); subject to GitHub Models rate limits.
- Videos without captions yet are retried each run until `transcript_give_up_hours`.
- YouTube may block transcript requests from GitHub's datacenter IPs. If that happens, run on a self-hosted runner or add a proxy.
- Local test: `pip install -r requirements.txt && GITHUB_TOKEN=<PAT with models:read> python watch.py`.
