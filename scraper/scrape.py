#!/usr/bin/env python3
"""TikTok trend scraper.

Collects videos from TikTok's public Explore feed (per niche category) with a
headless browser, plus the official Creative Center trending-hashtag list, and
aggregates them into trend data for the dashboard:

  - trending sounds per niche (with day-over-day momentum / rising prediction)
  - trending hashtags per niche (computed) + official Creative Center hashtags
  - estimated best posting hours per niche (engagement-weighted histogram)

Outputs:
  data/latest.json            full dashboard payload
  data/history/YYYY-MM-DD.json  compact daily snapshot used for momentum
"""

import asyncio
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

EXPLORE_URL = "https://www.tiktok.com/explore?lang=en"
CC_HASHTAG_URL = "https://ads.tiktok.com/CreativeOne/KnowledgeAPI/GetHashtagList"


def load_config():
    with open(ROOT / "config.json") as f:
        return json.load(f)


def log(*args):
    print("[scrape]", *args, flush=True)


# --------------------------------------------------------------------------
# Explore feed collection
# --------------------------------------------------------------------------

async def dismiss_cookie_banner(page):
    try:
        clicked = await page.evaluate(
            """() => {
              const walk = (root, out) => {
                for (const el of root.querySelectorAll('button')) out.push(el);
                for (const el of root.querySelectorAll('*'))
                  if (el.shadowRoot) walk(el.shadowRoot, out);
                return out;
              };
              const btns = walk(document, []);
              const b = btns.find(x => /^(allow all|accept all)$/i.test((x.textContent || '').trim()));
              if (b) { b.click(); return true; }
              return false;
            }"""
        )
        if clicked:
            log("dismissed cookie banner")
            await page.wait_for_timeout(1500)
    except Exception as e:
        log("cookie banner check failed (continuing):", e)


async def click_category(page, label):
    """Click an explore category chip by its visible text. Returns True on success."""
    try:
        return await page.evaluate(
            """(label) => {
              const walk = (root, out) => {
                for (const el of root.querySelectorAll('button,[role="tab"],span,div')) out.push(el);
                for (const el of root.querySelectorAll('*'))
                  if (el.shadowRoot) walk(el.shadowRoot, out);
                return out;
              };
              const els = walk(document, []);
              // prefer exact text match on the smallest matching element
              const matches = els.filter(x => (x.textContent || '').trim() === label);
              if (!matches.length) return false;
              matches.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
              const target = matches[0].closest('button') || matches[0];
              target.scrollIntoView({block: 'center', inline: 'center'});
              target.click();
              return true;
            }""",
            label,
        )
    except Exception as e:
        log("category click error:", label, e)
        return False


async def collect_explore(cfg):
    """Returns {niche_label: {video_id: item_dict}} from the explore feed.

    Explore batches are attributed to niches via the categoryType query param
    on the item_list requests. The page loads on "All" (categoryType 120);
    each category chip click switches the feed to a new categoryType, which we
    discover as the first unseen categoryType after the click.
    """
    niches = cfg["niches"]
    scrolls = int(cfg.get("scrollsPerCategory", 10))
    max_secs = int(cfg.get("maxSecondsPerCategory", 100))

    buckets = defaultdict(dict)          # categoryType -> {video_id: item}
    cat_of = {"All": "120"}              # niche label -> categoryType

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900}, locale="en-US"
        )
        page = await ctx.new_page()

        async def on_response(resp):
            if ("/api/explore/item_list" not in resp.url
                    and "/api/prefetch/explore/item_list" not in resp.url):
                return
            m = re.search(r"categoryType=(\d+)", resp.url)
            cat = m.group(1) if m else "120"
            try:
                body = await resp.json()
            except Exception:
                return
            for item in body.get("itemList") or []:
                vid = item.get("id")
                if vid:
                    buckets[cat][vid] = item

        page.on("response", on_response)

        log("navigating to explore feed")
        await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(6000)
        await dismiss_cookie_banner(page)

        async def scroll_feed(label, start, loop):
            for _ in range(scrolls):
                if loop.time() - start > max_secs:
                    log(f"  time cap reached for {label}")
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.keyboard.press("End")
                await page.wait_for_timeout(2300)

        loop = asyncio.get_event_loop()
        for label in niches:
            start = loop.time()
            if label != "All":
                known = set(cat_of.values())
                ok = await click_category(page, label)
                if not ok:
                    log(f"WARNING: could not find category chip '{label}', skipping")
                    continue
                # discover which categoryType this chip switched the feed to
                for _ in range(20):
                    await page.wait_for_timeout(500)
                    new = [c for c, items in buckets.items()
                           if c not in known and items]
                    if new:
                        cat_of[label] = new[0]
                        break
                if label not in cat_of:
                    log(f"WARNING: no feed response after clicking '{label}', skipping")
                    continue
            log(f"collecting niche: {label} (categoryType={cat_of[label]})")
            await scroll_feed(label, start, loop)
            log(f"  -> {len(buckets[cat_of[label]])} videos")

        await browser.close()

    return {label: buckets[cat] for label, cat in cat_of.items() if label in niches}


# --------------------------------------------------------------------------
# Creative Center official hashtags
# --------------------------------------------------------------------------

async def fetch_official_hashtags(region):
    """Fetch TikTok Creative Center's official trending hashtag lists."""
    results = {}
    async with async_playwright() as p:
        req = await p.request.new_context(extra_http_headers={
            "user-agent": UA,
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "referer": "https://ads.tiktok.com/",
            "agw-js-conv": "str",
        })
        for period in (7, 30):
            try:
                resp = await req.post(CC_HASHTAG_URL, data=json.dumps({
                    "timeRange": period, "countryCode": region, "page": 1, "limit": 50,
                }))
                body = await resp.json()
                items = body.get("items") or []
                out = []
                for it in items:
                    curve = [
                        {"t": int(pt["timestamp"]), "v": round(float(pt["value"]), 2)}
                        for pt in it.get("popularityCurve") or []
                    ]
                    # slope over the last 3 curve points -> official "rising" flag
                    rising = False
                    if len(curve) >= 3:
                        tail = [pt["v"] for pt in curve[-3:]]
                        rising = tail[-1] > tail[0] and tail[-1] >= max(pt["v"] for pt in curve) * 0.85
                    out.append({
                        "tag": it.get("hashtagName"),
                        "rank": int(it.get("rankIndex") or 0),
                        "posts": int(it.get("publishCnt") or 0),
                        "views": int(it.get("vv") or 0),
                        "curve": curve,
                        "rising": rising,
                        "creators": [
                            {"name": c.get("nickname"), "handle": c.get("handleName"),
                             "avatar": c.get("avatarURL"), "followers": int(c.get("followedCnt") or 0)}
                            for c in (it.get("topCreators") or [])[:3]
                        ],
                    })
                results[f"{period}d"] = out
                log(f"official hashtags {period}d: {len(out)}")
            except Exception as e:
                log(f"WARNING: official hashtag fetch failed for {period}d:", e)
                results[f"{period}d"] = []
        await req.dispose()
    return results


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "sound").lower()).strip("-")
    return s or "sound"


def aggregate_niche(items, tz):
    """Aggregate raw explore items into sounds / hashtags / posting hours."""
    sounds = {}
    hashtags = defaultdict(lambda: {"videoCount": 0, "totalPlays": 0})
    hour_count = [0.0] * 24
    hour_weighted = [0.0] * 24
    dow_weighted = [0.0] * 7
    top_videos = []

    for item in items.values():
        stats = item.get("stats") or {}
        plays = int(stats.get("playCount") or 0)
        likes = int(stats.get("diggCount") or 0)
        create_ts = int(item.get("createTime") or 0)

        music = item.get("music") or {}
        mid = music.get("id")
        if mid:
            s = sounds.setdefault(mid, {
                "id": mid,
                "title": music.get("title") or "original sound",
                "author": music.get("authorName") or "",
                "original": bool(music.get("original")),
                "videoCount": 0, "totalPlays": 0, "totalLikes": 0,
                "newestPost": 0, "sampleVideos": [],
            })
            s["videoCount"] += 1
            s["totalPlays"] += plays
            s["totalLikes"] += likes
            s["newestPost"] = max(s["newestPost"], create_ts)
            if len(s["sampleVideos"]) < 3:
                author = (item.get("author") or {}).get("uniqueId") or ""
                s["sampleVideos"].append({
                    "url": f"https://www.tiktok.com/@{author}/video/{item['id']}",
                    "plays": plays,
                })

        for ch in item.get("challenges") or []:
            tag = (ch.get("title") or "").strip().lower()
            if tag:
                hashtags[tag]["videoCount"] += 1
                hashtags[tag]["totalPlays"] += plays

        if create_ts:
            dt = datetime.fromtimestamp(create_ts, tz=timezone.utc).astimezone(tz)
            w = math.log10(plays + 10)
            hour_count[dt.hour] += 1
            hour_weighted[dt.hour] += w
            dow_weighted[dt.weekday()] += w

        author = (item.get("author") or {}).get("uniqueId") or ""
        top_videos.append({
            "id": item["id"],
            "desc": (item.get("desc") or "")[:120],
            "author": author,
            "plays": plays,
            "likes": likes,
            "url": f"https://www.tiktok.com/@{author}/video/{item['id']}",
        })

    for s in sounds.values():
        s["url"] = f"https://www.tiktok.com/music/{slugify(s['title'])}-{s['id']}"

    total_w = sum(hour_weighted) or 1.0
    hours_norm = [round(v / total_w, 4) for v in hour_weighted]
    best_hours = sorted(range(24), key=lambda h: hour_weighted[h], reverse=True)[:4]

    top_videos.sort(key=lambda v: v["plays"], reverse=True)

    return {
        "videosSampled": len(items),
        "sounds": sounds,
        "hashtags": {t: dict(v) for t, v in hashtags.items()},
        "postingHours": {
            "byHourCount": [int(v) for v in hour_count],
            "byHourWeighted": hours_norm,
            "byWeekdayWeighted": [round(v / (sum(dow_weighted) or 1.0), 4) for v in dow_weighted],
            "bestHours": sorted(best_hours),
            "sampleSize": int(sum(hour_count)),
        },
        "topVideos": top_videos[:12],
    }


# --------------------------------------------------------------------------
# Momentum / prediction from history
# --------------------------------------------------------------------------

def load_history(days):
    files = sorted(HISTORY_DIR.glob("*.json"))[-days:]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def build_series(history, niche, kind, key):
    """Series of {date, c(videoCount), p(totalPlays)} for one sound/hashtag."""
    series = []
    for snap in history:
        entry = (((snap.get("niches") or {}).get(niche) or {}).get(kind) or {}).get(key)
        if entry:
            series.append({"date": snap["date"], "c": entry["c"], "p": entry["p"]})
    return series


def momentum(series, today_count, today_plays, today_date):
    """Score + label from historical series. Higher score = stronger upward trend."""
    past = [pt for pt in series if pt["date"] != today_date]
    if not past:
        return {"score": 1.0 if today_count >= 2 else 0.2, "label": "new"}

    prev = past[-1]
    count_growth = (today_count - prev["c"]) / max(prev["c"], 1)
    plays_growth = (today_plays - prev["p"]) / max(prev["p"], 1)
    streak = len(past) + 1

    score = count_growth * 1.5 + min(plays_growth, 3.0) + 0.1 * streak
    if count_growth > 0.25 or (count_growth >= 0 and plays_growth > 0.5):
        label = "rising"
    elif count_growth < -0.34:
        label = "cooling"
    else:
        label = "steady"
    return {"score": round(score, 3), "label": label}


def rank_sounds(agg, history, niche, today_date):
    ranked = []
    for mid, s in agg["sounds"].items():
        series = build_series(history, niche, "sounds", mid)
        m = momentum(series, s["videoCount"], s["totalPlays"], today_date)
        hist_out = [pt for pt in series if pt["date"] != today_date]
        hist_out.append({"date": today_date, "c": s["videoCount"], "p": s["totalPlays"]})
        ranked.append({**s, "trend": {**m, "history": hist_out}})
    # sort: momentum first, then reach
    ranked.sort(key=lambda s: (s["trend"]["score"], s["videoCount"], s["totalPlays"]), reverse=True)
    return ranked[:40]


def rank_hashtags(agg, history, niche, today_date):
    ranked = []
    for tag, h in agg["hashtags"].items():
        series = build_series(history, niche, "hashtags", tag)
        m = momentum(series, h["videoCount"], h["totalPlays"], today_date)
        hist_out = [pt for pt in series if pt["date"] != today_date]
        hist_out.append({"date": today_date, "c": h["videoCount"], "p": h["totalPlays"]})
        ranked.append({
            "tag": tag, **h, "trend": {**m, "history": hist_out},
            "url": f"https://www.tiktok.com/tag/{tag}",
        })
    ranked.sort(key=lambda h: (h["videoCount"], h["trend"]["score"], h["totalPlays"]), reverse=True)
    return ranked[:40]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = datetime.now(timezone.utc)
    today = now.astimezone(tz).strftime("%Y-%m-%d")

    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)

    collected = await collect_explore(cfg)
    official = await fetch_official_hashtags(cfg.get("region", "US"))

    total_videos = sum(len(v) for v in collected.values())
    log(f"total videos collected: {total_videos}")
    if total_videos == 0:
        log("ERROR: no videos collected — TikTok may be blocking this IP or the page changed")
        sys.exit(1)

    history = load_history(int(cfg.get("historyDays", 14)))

    niches_out = {}
    snapshot_niches = {}
    for label, items in collected.items():
        if not items:
            log(f"WARNING: niche '{label}' collected 0 videos, omitting")
            continue
        agg = aggregate_niche(items, tz)
        niches_out[label] = {
            "videosSampled": agg["videosSampled"],
            "sounds": rank_sounds(agg, history, label, today),
            "hashtags": rank_hashtags(agg, history, label, today),
            "postingHours": agg["postingHours"],
            "topVideos": agg["topVideos"],
        }
        snapshot_niches[label] = {
            "sounds": {mid: {"c": s["videoCount"], "p": s["totalPlays"]}
                       for mid, s in agg["sounds"].items()},
            "hashtags": {t: {"c": h["videoCount"], "p": h["totalPlays"]}
                         for t, h in agg["hashtags"].items()},
        }

    latest = {
        "generatedAt": now.isoformat(),
        "date": today,
        "region": cfg.get("region", "US"),
        "timezone": cfg.get("timezone", "UTC"),
        "niches": niches_out,
        "official": official,
        "snapshotCount": len(history) + (0 if any(h.get("date") == today for h in history) else 1),
    }

    (DATA_DIR / "latest.json").write_text(json.dumps(latest, separators=(",", ":")))
    (HISTORY_DIR / f"{today}.json").write_text(
        json.dumps({"date": today, "niches": snapshot_niches}, separators=(",", ":")))
    log(f"wrote data/latest.json and data/history/{today}.json")


if __name__ == "__main__":
    asyncio.run(main())
