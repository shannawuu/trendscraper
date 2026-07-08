#!/usr/bin/env python3
"""Analysis of the user's own videos.

Pure functions (no network) that turn a list of fetched video records into
derived per-video metrics, factor insights ("what correlates with doing
well"), and concrete recommendations. Kept separate from scrape.py so it can
be unit-tested and reasoned about on its own.

A fetched video record (produced by scrape.fetch_user_videos) looks like:
  {
    "id", "url", "author", "desc", "createTime" (unix, UTC), "duration" (s),
    "views", "likes", "comments", "shares", "saves",
    "hashtags": [lowercase, ...], "sound": {"id","title","original"},
    "private": {...} | None, "error": None | str
  }
"""

import math
from datetime import datetime, timezone


def _rate(numer, denom):
    return (numer / denom) if denom else 0.0


def derive_metrics(v, tz):
    """Add engagement/quality ratios and local posting time to a video."""
    views = max(int(v.get("views") or 0), 0)
    likes = int(v.get("likes") or 0)
    comments = int(v.get("comments") or 0)
    shares = int(v.get("shares") or 0)
    saves = int(v.get("saves") or 0)
    engagements = likes + comments + shares + saves

    ct = int(v.get("createTime") or 0)
    local = datetime.fromtimestamp(ct, tz=timezone.utc).astimezone(tz) if ct else None

    return {
        **v,
        "views": views, "likes": likes, "comments": comments,
        "shares": shares, "saves": saves,
        "engagementRate": round(_rate(engagements, views), 4),
        "likeRate": round(_rate(likes, views), 4),
        "commentRate": round(_rate(comments, views), 4),
        "shareRate": round(_rate(shares, views), 4),
        "saveRate": round(_rate(saves, views), 4),
        "hashtagCount": len(v.get("hashtags") or []),
        "captionLen": len(v.get("desc") or ""),
        "postHour": local.hour if local else None,
        "postWeekday": local.weekday() if local else None,
        "postLabel": local.strftime("%a %H:%M") if local else "unknown",
    }


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _split_by(videos, key):
    """Sort by a key desc, return (top_half, bottom_half). Needs >= 4 videos
    to make a comparison meaningful."""
    ordered = sorted(videos, key=lambda v: v.get(key) or 0, reverse=True)
    n = len(ordered)
    half = n // 2
    return ordered[:half], ordered[half:]


def _fmt_int(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def build_context(niches):
    """Union of trending hashtags/sounds and per-niche best hours from the
    dashboard's niche data, used to relate the user's videos to live trends."""
    trending_tags, rising_tags, trending_sounds, best_hours = {}, set(), {}, []
    for niche in (niches or {}).values():
        for h in niche.get("hashtags") or []:
            tag = (h.get("tag") or "").lower()
            if tag:
                trending_tags[tag] = max(trending_tags.get(tag, 0), h.get("videoCount", 0))
                if (h.get("trend") or {}).get("label") == "rising":
                    rising_tags.add(tag)
        for s in niche.get("sounds") or []:
            if s.get("id"):
                trending_sounds[str(s["id"])] = s.get("title") or ""
        ph = niche.get("postingHours") or {}
        best_hours += ph.get("bestHours") or []
    return {
        "trendingTags": trending_tags,
        "risingTags": rising_tags,
        "trendingSoundIds": trending_sounds,
        "nicheBestHours": sorted(set(best_hours)),
    }


def analyze(raw_videos, tz, niches):
    """Main entry: returns the full analysis payload for the dashboard."""
    ok = [derive_metrics(v, tz) for v in raw_videos if not v.get("error") and v.get("views")]
    errored = [v for v in raw_videos if v.get("error")]
    ctx = build_context(niches)

    # tag each video with how it relates to live trends
    for v in ok:
        tags = set(v.get("hashtags") or [])
        v["trendingTagsUsed"] = sorted(tags & set(ctx["trendingTags"]))
        v["risingTagsUsed"] = sorted(tags & ctx["risingTags"])
        v["usedTrendingSound"] = str((v.get("sound") or {}).get("id") or "") in ctx["trendingSoundIds"]

    result = {
        "count": len(ok),
        "errored": [{"url": v.get("url"), "error": v.get("error")} for v in errored],
        "videos": sorted(ok, key=lambda v: v["views"], reverse=True),
        "summary": _summary(ok),
        "factors": _factors(ok) if len(ok) >= 4 else [],
        "recommendations": _recommendations(ok, ctx) if len(ok) >= 3 else [],
        "context": {
            "nicheBestHours": ctx["nicheBestHours"],
            "hasTrendData": bool(ctx["trendingTags"]),
        },
        "note": _sample_note(len(ok)),
    }
    return result


def _sample_note(n):
    if n == 0:
        return "No videos with stats yet — add your video URLs."
    if n < 4:
        return f"Only {n} videos analyzed — add more (6+) for reliable pattern detection."
    if n < 8:
        return f"{n} videos analyzed — patterns are directional; more videos sharpen them."
    return f"{n} videos analyzed."


def _summary(vids):
    if not vids:
        return {}
    return {
        "medianViews": _median([v["views"] for v in vids]),
        "medianEngagementRate": round(_median([v["engagementRate"] for v in vids]) or 0, 4),
        "bestVideo": max(vids, key=lambda v: v["views"])["url"],
        "totalViews": sum(v["views"] for v in vids),
        "medianDuration": _median([v["duration"] for v in vids if v.get("duration")]),
        "trendingSoundShare": round(_rate(sum(1 for v in vids if v["usedTrendingSound"]), len(vids)), 2),
    }


def _factors(vids):
    """Compare the top half vs bottom half (by views) on each *controllable*
    lever, and emit an insight when the gap is meaningful.

    Uses medians (robust to a single viral outlier). Engagement RATES are
    deliberately excluded here: they scale inversely with views (a small
    denominator inflates them), so a views-based split would always paint
    low-view videos as "more engaging" — misleading. Rate quality is surfaced
    in recommendations instead.
    """
    top, bottom = _split_by(vids, "views")
    factors = []

    def cmp_factor(name, key, unit, fmt=lambda x: f"{x:.0f}", min_gap_pct=0.15):
        t, b = _median([v.get(key) for v in top]), _median([v.get(key) for v in bottom])
        if t is None or b is None or b == 0:
            return
        gap = (t - b) / abs(b)
        if abs(gap) < min_gap_pct:
            return
        factors.append({
            "factor": name,
            "topValue": round(t, 2), "bottomValue": round(b, 2),
            "insight": f"{name}: your best videos run {fmt(t)}{unit} (median) "
                       f"vs {fmt(b)}{unit} for your weakest.",
            "direction": "higher" if t > b else "lower",
        })

    cmp_factor("Video length", "duration", "s")
    cmp_factor("Hashtag count", "hashtagCount", "")
    cmp_factor("Caption length", "captionLen", " chars")

    # categorical: trending sound usage top vs bottom
    t_sound = sum(1 for v in top if v["usedTrendingSound"])
    b_sound = sum(1 for v in bottom if v["usedTrendingSound"])
    if top and bottom and (t_sound / len(top)) - (b_sound / len(bottom)) >= 0.25:
        factors.append({
            "factor": "Trending sound",
            "insight": f"{t_sound} of your {len(top)} best videos used a trending sound, "
                       f"vs {b_sound} of your {len(bottom)} weakest — trending audio is helping you.",
            "direction": "higher",
        })

    # trending hashtag usage top vs bottom
    t_tags = _mean([len(v["trendingTagsUsed"]) for v in top])
    b_tags = _mean([len(v["trendingTagsUsed"]) for v in bottom])
    if t_tags is not None and b_tags is not None and t_tags - b_tags >= 0.5:
        factors.append({
            "factor": "Trending hashtags",
            "insight": f"Your best videos used {t_tags:.1f} trending hashtags on average "
                       f"vs {b_tags:.1f} for your weakest.",
            "direction": "higher",
        })

    return factors


def _recommendations(vids, ctx):
    """Concrete, prioritized advice grounded in the user's own numbers."""
    recs = []
    top = sorted(vids, key=lambda v: v["views"], reverse=True)[:max(3, len(vids) // 3)]

    # --- posting time (from the user's own best videos) ---
    hours = [v["postHour"] for v in top if v["postHour"] is not None]
    if hours:
        # cluster into the most common ±1h window
        from collections import Counter
        c = Counter(h // 3 for h in hours)  # 3-hour buckets
        best_bucket = c.most_common(1)[0][0]
        lo, hi = best_bucket * 3, best_bucket * 3 + 2
        recs.append({
            "area": "Posting time", "priority": "high",
            "text": f"Your best-performing videos cluster around {lo:02d}:00–{hi:02d}:59. "
                    f"Prioritise posting in that window."
                    + (f" (Niche activity also peaks at {', '.join(f'{h}:00' for h in ctx['nicheBestHours'][:3])}.)"
                       if ctx["nicheBestHours"] else ""),
        })

    # (Video length is reported as a factor, not duplicated here, to avoid
    # showing two length numbers computed over different groupings.)

    # --- trending sound adoption ---
    share = _rate(sum(1 for v in vids if v["usedTrendingSound"]), len(vids))
    if ctx["trendingSoundIds"] and share < 0.34:
        recs.append({"area": "Sounds", "priority": "high",
                     "text": f"Only {share*100:.0f}% of your videos use a currently-trending sound. "
                             f"Check the Trending sounds tab and reuse rising audio while it's hot."})

    # --- hashtag strategy ---
    med_tags = _median([v["hashtagCount"] for v in vids])
    if med_tags is not None:
        if med_tags > 7:
            recs.append({"area": "Hashtags", "priority": "medium",
                         "text": f"You use ~{med_tags:.0f} hashtags per video. Trim to 3–5 focused, "
                                 f"relevant tags — hashtag spam dilutes relevance signals."})
        elif med_tags < 2:
            recs.append({"area": "Hashtags", "priority": "medium",
                         "text": f"You use ~{med_tags:.0f} hashtags per video. Add 3–5 relevant ones "
                                 f"(mix a niche tag with a rising trend) to aid discovery."})
    # rising tags they're missing
    used = set()
    for v in vids:
        used |= set(v.get("hashtags") or [])
    missing_rising = sorted(ctx["risingTags"] - used)
    if missing_rising:
        recs.append({"area": "Hashtags", "priority": "medium",
                     "text": "Rising hashtags in your niche you haven't used: "
                             + ", ".join("#" + t for t in missing_rising[:6]) + "."})

    # --- engagement quality: what to lean into ---
    best = max(vids, key=lambda v: v["views"])
    signals = []
    if best["saveRate"] >= _median([v["saveRate"] for v in vids]) * 1.5:
        signals.append("high save rate (people bookmarking it to act on later)")
    if best["shareRate"] >= _median([v["shareRate"] for v in vids]) * 1.5:
        signals.append("high share rate (strong word-of-mouth)")
    if signals:
        recs.append({"area": "What's working", "priority": "high",
                     "text": f"Your best video ({_fmt_int(best['views'])} views) stands out for "
                             + " and ".join(signals) + ". Make more like it."})

    return recs


if __name__ == "__main__":
    # smoke test with synthetic data
    import json
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Toronto")
    fake = [
        {"id": "1", "url": "u1", "views": 500000, "likes": 60000, "comments": 800,
         "shares": 4000, "saves": 9000, "createTime": 1780000000, "duration": 14,
         "hashtags": ["mathgpt", "studytok", "fyp"], "sound": {"id": "s1", "original": False}},
        {"id": "2", "url": "u2", "views": 3000, "likes": 90, "comments": 3,
         "shares": 2, "saves": 5, "createTime": 1780050000, "duration": 45,
         "hashtags": ["math", "school", "homework", "study", "learn", "tips", "viral", "fyp", "foryou"],
         "sound": {"id": "s9", "original": True}},
    ]
    print(json.dumps(analyze(fake, tz, {}), indent=1, default=str)[:1200])
