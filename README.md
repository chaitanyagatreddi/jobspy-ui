# JobSpy UI

Scrape recent job postings from LinkedIn, Google, Indeed, Glassdoor, and ZipRecruiter — then score each role against your own profile with `gpt-4o-mini`.

Lightweight Flask + vanilla JS frontend on top of [JobSpy](https://github.com/speedyapply/JobSpy).

## What it does

- **Search** by keyword, location, hours-since-posted, and source sites
- **Auto-resolve company name → LinkedIn Company ID** (paste a name like "Animaker" and it finds the precise company filter for you)
- **Lazy-fetch full JD** from LinkedIn on demand when you ask to score a row
- **Score per role** (0–10 with verdict + outreach hook) against your profile
- **Toggle scoring off** for visitors — pure job board without exposing personal data
- Indian city typo normalization, strict location filtering, dedupe by (company + title)

## Run locally

```bash
cp .env.example .env       # then add OPENAI_API_KEY
pip install -r requirements.txt
python3 app.py             # http://localhost:7861
```

## Personalize the scorer

The scorer uses a `PROFILE` dict.

1. Copy `profile.py` → `profile_local.py` (already gitignored)
2. Replace placeholders with your real summary, proof of work, strengths, weak signals
3. The app auto-prefers `profile_local.py` when present

If `profile_local.py` doesn't exist, the public template in `profile.py` is used — scores will be generic.

## Deploy on Render

Render reads `render.yaml` and provisions a free web service. Set `OPENAI_API_KEY` in the Render dashboard before the first build. Free tier sleeps after inactivity; first request after sleep is slow.

## Credit

- Job scraping: [JobSpy](https://github.com/speedyapply/JobSpy) (MIT)
- LLM scoring: OpenAI `gpt-4o-mini`
- UI inspiration: [velt.dev](https://velt.dev) (dark canvas, single purple CTA, teal accent)
