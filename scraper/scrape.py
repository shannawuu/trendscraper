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
from datetime import datetime, timedelta, timezone
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


async def collect_explore(categories, scrolls, max_secs):
    """Returns {category_label: {video_id: item_dict}} from the explore feed.

    Explore batches are attributed to categories via the categoryType query
    param on the item_list requests. The page loads on "All" (categoryType
    120); each category chip click switches the feed to a new categoryType,
    which we discover as the first unseen categoryType after the click.
    """
    niches = categories

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
# Custom niche hashtag stats (tag pages)
# --------------------------------------------------------------------------

async def fetch_tag_stats(tags):
    """Fetch lifetime video/view counts for specific hashtags from their tag
    pages. TikTok sometimes throws a captcha at these; failed tags are retried
    once with a fresh browser and otherwise skipped (momentum tolerates gaps).

    Returns {tag: {"v": viewCount, "n": videoCount}}.
    """
    out = {}
    remaining = list(tags)
    async with async_playwright() as p:
        for attempt in range(2):
            if not remaining:
                break
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=UA, viewport={"width": 1440, "height": 900}, locale="en-US"
            )
            page = await ctx.new_page()
            detail = {}

            async def on_response(resp):
                if "/api/challenge/detail" not in resp.url:
                    return
                try:
                    body = await resp.json()
                except Exception:
                    return
                ci = body.get("challengeInfo") or {}
                title = ((ci.get("challenge") or {}).get("title") or "").lower()
                stats = ci.get("statsV2") or ci.get("stats") or {}
                if title:
                    detail[title] = {
                        "v": int(stats.get("viewCount") or 0),
                        "n": int(stats.get("videoCount") or 0),
                    }

            page.on("response", on_response)
            failed = []
            for tag in remaining:
                try:
                    await page.goto(f"https://www.tiktok.com/tag/{tag}?lang=en",
                                    wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    log(f"  tag page error for #{tag}:", e)
                if tag.lower() in detail:
                    out[tag] = detail[tag.lower()]
                    log(f"  #{tag}: {out[tag]['v']:,} views, {out[tag]['n']:,} videos")
                else:
                    failed.append(tag)
            await browser.close()
            remaining = failed
            if failed and attempt == 0:
                log(f"  retrying {len(failed)} tags with a fresh browser: {failed}")
    for tag in remaining:
        log(f"  WARNING: no stats captured for #{tag} (captcha or tag doesn't exist)")
    return out


def match_niche_videos(all_items, tags, keywords):
    """Videos from the explore sample that belong to a niche.

    Hashtags match exactly; caption keywords match on word boundaries only
    (so "homework" never matches "#athomeworkout").
    """
    tagset = {t.lower() for t in tags}
    kw_res = [re.compile(r"\b" + re.escape(k.lower()) + r"\b") for k in keywords]
    out = {}
    for vid, item in all_items.items():
        chs = {(c.get("title") or "").lower() for c in item.get("challenges") or []}
        desc = (item.get("desc") or "").lower()
        if chs & tagset or any(r.search(desc) for r in kw_res):
            out[vid] = item
    return out


# --------------------------------------------------------------------------
# Rolling video pool for hashtag-defined niches
# --------------------------------------------------------------------------
# Tag-matched videos are rare in any single day's explore sample, so they are
# accumulated in data/pool.json for `poolDays` days and niches aggregate over
# the whole pool.

POOL_FILE = None  # set in main() to DATA_DIR / "pool.json"


def compact_item(item):
    music = item.get("music") or {}
    stats = item.get("stats") or {}
    return {
        "id": item["id"],
        "desc": (item.get("desc") or "")[:200],
        "createTime": item.get("createTime"),
        "author": {"uniqueId": (item.get("author") or {}).get("uniqueId") or ""},
        "music": {k: music.get(k) for k in ("id", "title", "authorName", "original")},
        "challenges": [{"title": c.get("title") or ""} for c in item.get("challenges") or []],
        "stats": {"playCount": int(stats.get("playCount") or 0),
                  "diggCount": int(stats.get("diggCount") or 0)},
    }


def load_pool():
    try:
        return json.loads(POOL_FILE.read_text())
    except Exception:
        return {"niches": {}}


def update_pool(pool, niche, matched, today, pool_days, tags, keywords):
    """Merge today's matches into the niche's pool, prune stale entries, and
    return the pooled items ready for aggregation.

    Previously pooled videos are re-checked against the current matching
    rules, so tightening tags/keywords in config.json retroactively cleans
    the pool."""
    entry = pool["niches"].setdefault(niche, {})
    for vid, item in matched.items():
        entry[vid] = {"seen": today, "item": compact_item(item)}
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=pool_days)).strftime("%Y-%m-%d")
    still_valid = match_niche_videos(
        {vid: rec["item"] for vid, rec in entry.items()}, tags, keywords)
    for vid in [v for v, rec in entry.items()
                if rec.get("seen", "") < cutoff or v not in still_valid]:
        del entry[vid]
    return {vid: rec["item"] for vid, rec in entry.items()}


def tag_momentum(series):
    """Label growth of a tag's cumulative view count across snapshots."""
    if len(series) < 2:
        return {"label": "new", "deltaViews": None}
    deltas = [series[i]["v"] - series[i - 1]["v"] for i in range(1, len(series))]
    d_last = deltas[-1]
    if len(deltas) == 1:
        return {"label": "active" if d_last > 0 else "quiet", "deltaViews": d_last}
    d_prev = deltas[-2]
    if d_last > max(d_prev * 1.15, 0):
        label = "rising"
    elif d_last < d_prev * 0.7:
        label = "cooling"
    else:
        label = "steady"
    return {"label": label, "deltaViews": d_last}


def build_tracked_tags(stats, history, niche, today):
    out = []
    for tag, st in stats.items():
        series = []
        for snap in history:
            entry = (((snap.get("niches") or {}).get(niche) or {}).get("tags") or {}).get(tag)
            if entry and snap.get("date") != today:
                series.append({"date": snap["date"], "v": entry["v"], "n": entry["n"]})
        series.append({"date": today, "v": st["v"], "n": st["n"]})
        m = tag_momentum(series)
        out.append({
            "tag": tag, "views": st["v"], "videos": st["n"],
            "url": f"https://www.tiktok.com/tag/{tag}",
            "trend": {**m, "history": series},
        })
    out.sort(key=lambda t: t["views"], reverse=True)
    return out


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
    global POOL_FILE
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = datetime.now(timezone.utc)
    today = now.astimezone(tz).strftime("%Y-%m-%d")

    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)
    POOL_FILE = DATA_DIR / "pool.json"

    niche_cfg = cfg["niches"]  # ordered: config order = dashboard tab order

    # 1. scrape every explore category any niche uses, plus extra categories
    # that only feed the tag/keyword matching pool (not shown as tabs)
    categories = []
    for spec in niche_cfg.values():
        cat = spec.get("category")
        if cat and cat not in categories:
            categories.append(cat)
    for cat in cfg.get("extraCategories") or []:
        if cat not in categories:
            categories.append(cat)
    collected = await collect_explore(categories,
                                      int(cfg.get("scrollsPerCategory", 10)),
                                      int(cfg.get("maxSecondsPerCategory", 100)))
    official = await fetch_official_hashtags(cfg.get("region", "US"))

    # 2. tag stats for niches that track product hashtags
    tag_stats = {}
    for name, spec in niche_cfg.items():
        if spec.get("trackTags"):
            log(f"fetching tag stats for: {name}")
            tag_stats[name] = await fetch_tag_stats(spec["trackTags"])

    total_videos = sum(len(v) for v in collected.values())
    log(f"total videos collected: {total_videos}")
    if total_videos == 0:
        log("ERROR: no videos collected — TikTok may be blocking this IP or the page changed")
        sys.exit(1)

    history = load_history(int(cfg.get("historyDays", 14)))
    pool = load_pool()
    pool_days = int(cfg.get("poolDays", 7))

    all_explore = {}
    for items in collected.values():
        all_explore.update(items)

    # 3. build each niche: explore category items + pooled tag/keyword matches
    niches_out = {}
    snapshot_niches = {}
    for name, spec in niche_cfg.items():
        items = {}
        if spec.get("category"):
            items.update(collected.get(spec["category"]) or {})
        if spec.get("matchTags") or spec.get("keywords"):
            matched = match_niche_videos(all_explore, spec.get("matchTags") or [],
                                         spec.get("keywords") or [])
            pooled = update_pool(pool, name, matched, today, pool_days,
                                 spec.get("matchTags") or [], spec.get("keywords") or [])
            log(f"niche '{name}': {len(matched)} videos matched today, "
                f"{len(pooled)} in {pool_days}-day pool")
            items.update(pooled)
        if not items and not spec.get("trackTags"):
            log(f"WARNING: niche '{name}' has no videos, omitting")
            continue

        agg = aggregate_niche(items, tz) if items else None
        niches_out[name] = {
            "custom": not spec.get("category"),
            "videosSampled": len(items),
            "sounds": rank_sounds(agg, history, name, today) if agg else [],
            "hashtags": rank_hashtags(agg, history, name, today) if agg else [],
            "postingHours": agg["postingHours"] if agg else None,
            "topVideos": agg["topVideos"] if agg else [],
        }
        if spec.get("trackTags"):
            niches_out[name]["trackedTags"] = build_tracked_tags(
                tag_stats.get(name) or {}, history, name, today)
        snapshot_niches[name] = {
            "sounds": {mid: {"c": s["videoCount"], "p": s["totalPlays"]}
                       for mid, s in (agg["sounds"] if agg else {}).items()},
            "hashtags": {t: {"c": h["videoCount"], "p": h["totalPlays"]}
                         for t, h in (agg["hashtags"] if agg else {}).items()},
        }
        if spec.get("trackTags"):
            snapshot_niches[name]["tags"] = tag_stats.get(name) or {}

    POOL_FILE.write_text(json.dumps(pool, separators=(",", ":")))

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
