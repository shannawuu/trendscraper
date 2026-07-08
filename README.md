# TikTok Trend Radar

Personal TikTok analytics dashboard. Scrapes real data from TikTok's public
Explore feed and Creative Center, updates daily via GitHub Actions (or on
demand with the **Update now** button), and shows:

- **Trending sounds** per niche, with day-over-day momentum and a
  rising/steady/cooling signal (predictions sharpen as daily snapshots accumulate)
- **Trending hashtags** — both computed from sampled videos and TikTok's
  official Creative Center list (with popularity curves)
- **Estimated best posting hours** per niche (engagement-weighted, heuristic)
- **Niche switching** — pick any of TikTok's 21 explore categories in `config.json`
- **My Videos** — analyze your own posts: paste your video URLs and get
  per-video stats, growth tracking, what correlates with your best videos, and
  concrete recommendations (posting time, length, sounds, hashtags)

## My Videos (analyze your own content)

Open the **📊 My Videos** tab, paste your TikTok video URLs (one per line),
and hit **Save & analyze**. Each run re-fetches every video's public stats
(views, likes, comments, shares, saves, post time, duration, hashtags, sound)
straight from its page — no account login required — and:

- tracks each video's view growth over time,
- compares your top vs bottom performers to surface what correlates with
  success (length, hashtag count, trending-sound use, trending-hashtag use),
- cross-references your videos against the live trend data (flags which of
  your sounds/hashtags are currently trending),
- gives prioritized recommendations grounded in *your* numbers.

The URL list lives in `my_videos.json` (the dashboard writes it for you via
the GitHub API; you can also edit it directly). Analysis is correlational —
with fewer than ~6 videos it's directional; it sharpens as you add more.

Optional: add private TikTok Studio metrics per video by making an entry an
object, e.g. `{"url": "...", "private": {"avgWatchTimeSec": 12, "watchedFullPct": 40}}`.
Auto-pulling those from your creator account (a separate, isolated login from
the trend-scraping account) is possible but off by default — it would put your
main account into automation; ask if you want to enable it.

## Setup (one time, ~5 minutes)

1. **Create a GitHub repo** and push this project:
   ```sh
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. **Enable GitHub Pages**: repo → Settings → Pages → Source: *Deploy from a
   branch* → Branch: `main`, folder `/ (root)` → Save.
   Your dashboard will be at `https://<you>.github.io/<repo>/`.
3. **Allow the workflow to push data**: repo → Settings → Actions → General →
   Workflow permissions → select **Read and write permissions** → Save.
4. **Make the "Update now" and "Save & analyze" buttons work**: create a
   fine-grained token at github.com/settings/personal-access-tokens → New token
   → Repository access: *only this repo* → Permissions → **Actions: Read and
   write** and **Contents: Read and write** → Generate. Open the dashboard,
   click ⚙, paste the repo (`you/repo`) and token. The token never leaves your
   browser (stored in `localStorage`).

The scraper then runs automatically every day at 10:30 UTC and whenever you
click **Update now** (or press *Run workflow* in the Actions tab).

## Changing niches

Each entry in `niches` in `config.json` becomes a dashboard tab (config order
= tab order). A niche can combine any of:

- `category` — one of TikTok's explore categories (see `availableCategories`);
  its whole feed is sampled every run.
- `matchTags` / `keywords` — pull matching videos from *all* scraped explore
  feeds into the niche. Hashtags match exactly and keywords match on word
  boundaries. Matches accumulate in a rolling pool (`data/pool.json`,
  `poolDays` days) so hashtag-defined product niches build up enough videos
  for sound/hashtag/posting-hour analysis even though any single day's
  explore sample contains few of them.
- `trackTags` — product hashtags whose lifetime view/video counts are
  snapshotted every run from their tag pages, giving exact day-over-day
  growth ("Product hashtags" card). Tag pages are occasionally
  captcha-walled; the scraper retries once and skips misses (gaps are fine).

Commit + push config changes; the next scrape picks them up. Region and
timezone are also set there.

## Logged-in mode (much richer niche data)

Logged out, TikTok only exposes a handful of niche videos through the Explore
feed. With a session, the scraper reads the **full video grid under each
hashtag** — hundreds of real videos per niche instead of a few — so trending
sounds, hashtags, posting hours, and sampled videos for product niches like
MathGPT and Shapes become accurate. The dashboard header shows 🔓 logged-in or
🔒 logged-out for each run.

To enable it, provide a TikTok session as a cookies JSON (`{ "sessionid": "…",
"ttwid": "…", … }`, the full cookie set from a logged-in tiktok.com):

- **Locally:** save it to `scraper/cookies.json` (gitignored — never commit it).
- **In GitHub Actions:** add a repo secret `TIKTOK_COOKIES` with the same JSON
  (Settings → Secrets and variables → Actions → New repository secret). The
  workflow passes it to the scraper as an env var; it never appears in code or
  logs.

Which hashtags each niche pulls videos from is set by its `gridTags` in
`config.json`. The scraper verifies the session on every run and silently
falls back to logged-out mode if it's missing or expired.

**Warnings:** a session cookie is a live login to that account — treat it like
a password. Use a **throwaway account**, not your creator account: automated
scraping can get an account restricted. Sessions expire after some weeks; when
runs flip to 🔒 logged-out, re-export the cookies.

## Run locally

```sh
pip install -r requirements.txt
python -m playwright install chromium
python scraper/scrape.py        # writes data/latest.json + data/history/<date>.json
python3 -m http.server 8000     # open http://localhost:8000
```

## How predictions work

Every run stores a compact snapshot in `data/history/`. For each sound and
hashtag, the scraper compares today's sampled video count and total plays
against previous snapshots: accelerating counts → **rising**, first
appearance → **new**, shrinking → **cooling**. With fewer than ~3 snapshots
everything is baseline, so give it a few days before trusting the signals.

## Honest caveats

- Scraping TikTok is against their ToS — this is for personal analytics only.
- TikTok changes its site regularly; if a run fails (red X in Actions), the
  scraper likely needs a selector/endpoint fix. Old data stays live meanwhile.
- GitHub Actions runners use datacenter IPs, which TikTok occasionally blocks.
  If runs consistently return zero videos, run the scraper locally
  (`python scraper/scrape.py`) and push, or wire in a proxy.
- "Best posting hours" is a heuristic derived from when trending videos were
  posted — TikTok publishes no official per-niche timing data.
