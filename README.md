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
4. **Make the "Update now" button work**: create a fine-grained token at
   github.com/settings/personal-access-tokens → New token → Repository access:
   *only this repo* → Permissions → **Actions: Read and write** → Generate.
   Open the dashboard, click ⚙, paste the repo (`you/repo`) and token.
   The token never leaves your browser (stored in `localStorage`).

The scraper then runs automatically every day at 10:30 UTC and whenever you
click **Update now** (or press *Run workflow* in the Actions tab).

## Changing niches

Edit `niches` in `config.json` (any of the categories in `availableNiches`),
commit, and push. The next scrape picks them up. Region and timezone are also
set there.

### Custom product niches

`customNiches` defines hashtag-based niches (e.g. for UGC products like
MathGPT or Shapes). Each one lists `tags` — hashtags whose lifetime
view/video counts are snapshotted every run, giving exact day-over-day
growth — and `keywords` used to match related videos from the explore
sample for sound/posting-hour analysis. Tag pages are occasionally
captcha-walled; the scraper retries once and skips misses (gaps are fine).

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
