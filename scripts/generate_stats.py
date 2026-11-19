"""Generate self-updating dark/light profile SVGs from GitHub GraphQL stats."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "assets"
SNAPSHOT_PATH = ROOT_DIR / "data" / "stats_snapshot.json"
USERNAME = os.environ.get("GITHUB_USERNAME", "leonkrm")

MARKUP_LANGS = {"HTML", "CSS", "SCSS", "Less", "Mako", "Batchfile", "Shell"}

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0d1117",
        "card_bg": "#161b22",
        "border": "#30363d",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "subtle": "#6e7681",
        "accent": "#58a6ff",
        "accent2": "#818cf8",
        "cyan": "#38bdf8",
        "green": "#39d353",
        "grid": "#21262d",
        "pill_bg": "#1f242c",
        "lvl0": "#21262d",
        "lvl1": "#0e4429",
        "lvl2": "#006d32",
        "lvl3": "#26a641",
        "lvl4": "#39d353",
        "snake_head": "#38bdf8",
        "snake_tail": "#818cf8",
    },
    "light": {
        "bg": "#ffffff",
        "card_bg": "#f6f8fa",
        "border": "#d0d7de",
        "text": "#1f2328",
        "muted": "#59636e",
        "subtle": "#6e7781",
        "accent": "#0969da",
        "accent2": "#6639ba",
        "cyan": "#0969da",
        "green": "#1a7f37",
        "grid": "#eaeef2",
        "pill_bg": "#eaeef2",
        "lvl0": "#ebedf0",
        "lvl1": "#9be9a8",
        "lvl2": "#40c463",
        "lvl3": "#30a14e",
        "lvl4": "#216e39",
        "snake_head": "#0969da",
        "snake_tail": "#8250df",
    },
}

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
          }
        }
      }
    }
    repositories(first: 50, ownerAffiliations: OWNER, isFork: false) {
      totalCount
      nodes {
        name
        isPrivate
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node {
              name
              color
            }
          }
        }
      }
    }
  }
}
"""


def fetch_graphql(token: str, login: str) -> dict[str, Any]:
    """Query GitHub GraphQL API for user contributions and repository languages."""
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": login}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{login}-profile-generator",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "errors" in body:
        raise RuntimeError(f"GraphQL error: {body['errors']}")
    return body["data"]["user"]


def normalize_snapshot(raw_user: dict[str, Any]) -> dict[str, Any]:
    """Convert raw GraphQL user object into normalized snapshot dictionary."""
    cal = raw_user["contributionsCollection"]["contributionCalendar"]
    weeks = cal["weeks"]
    repos = raw_user["repositories"]["nodes"]
    has_private = any(r.get("isPrivate") for r in repos)

    lang_bytes: dict[str, dict[str, Any]] = {}
    for repo in repos:
        for edge in repo.get("languages", {}).get("edges", []):
            name = edge["node"]["name"]
            color = edge["node"].get("color") or "#8b949e"
            size = int(edge["size"])
            if name not in lang_bytes:
                lang_bytes[name] = {"size": 0, "color": color}
            lang_bytes[name]["size"] += size

    return {
        "username": USERNAME,
        "updatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "hasPrivateScope": has_private,
        "totalContributions": int(cal["totalContributions"]),
        "weeks": weeks,
        "repoCount": int(raw_user["repositories"]["totalCount"]),
        "publicRepoCount": sum(1 for r in repos if not r.get("isPrivate")),
        "privateRepoCount": sum(1 for r in repos if r.get("isPrivate")),
        "totalStars": sum(int(r.get("stargazerCount", 0)) for r in repos),
        "languages": lang_bytes,
    }


def merge_public_with_snapshot(live: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Preserve private baseline history when Action runs with public-only GITHUB_TOKEN."""
    if live["hasPrivateScope"] or not baseline:
        return live

    base_map = {
        d["date"]: d["contributionCount"]
        for w in baseline.get("weeks", [])
        for d in w.get("contributionDays", [])
    }
    merged_weeks = []
    for w in live["weeks"]:
        new_days = []
        for d in w["contributionDays"]:
            cnt = max(int(d["contributionCount"]), int(base_map.get(d["date"], 0)))
            new_days.append({"contributionCount": cnt, "date": d["date"]})
        merged_weeks.append({"contributionDays": new_days})

    merged_total = sum(d["contributionCount"] for w in merged_weeks for d in w["contributionDays"])
    return {
        **baseline,
        "updatedAt": live["updatedAt"],
        "weeks": merged_weeks,
        "totalContributions": max(merged_total, int(baseline.get("totalContributions", 0))),
        "publicRepoCount": live["publicRepoCount"],
        "repoCount": max(live["repoCount"], int(baseline.get("repoCount", 0))),
        "totalStars": max(live["totalStars"], int(baseline.get("totalStars", 0))),
    }


def compute_metrics(snap: dict[str, Any]) -> dict[str, Any]:
    """Derive streaks, weekday rhythm, top code languages, and peaks."""
    days = [d for w in snap["weeks"] for d in w["contributionDays"]]
    days.sort(key=lambda d: d["date"])

    today_str = days[-1]["date"] if days else dt.date.today().isoformat()
    cur_streak = 0
    for d in reversed(days):
        if d["contributionCount"] > 0:
            cur_streak += 1
        elif d["date"] == today_str:
            continue
        else:
            break

    best_streak = 0
    running = 0
    for d in days:
        if d["contributionCount"] > 0:
            running += 1
            best_streak = max(best_streak, running)
        else:
            running = 0

    active_days = sum(1 for d in days if d["contributionCount"] > 0)
    peak_day = max(days, key=lambda d: d["contributionCount"], default={"date": today_str, "contributionCount": 0})

    weekday_totals = [0] * 7
    for d in days:
        wd = dt.date.fromisoformat(d["date"]).weekday()
        weekday_totals[wd] += d["contributionCount"]

    raw_langs = snap.get("languages", {})
    code_langs = {k: v for k, v in raw_langs.items() if k not in MARKUP_LANGS and v["size"] > 0}
    if not code_langs:
        code_langs = raw_langs
    sorted_langs = sorted(code_langs.items(), key=lambda kv: kv[1]["size"], reverse=True)[:5]
    total_lang_bytes = sum(v["size"] for _, v in sorted_langs) or 1
    lang_items = [
        {
            "name": name,
            "color": info["color"],
            "pct": round((info["size"] / total_lang_bytes) * 100, 1),
        }
        for name, info in sorted_langs
    ]

    return {
        "days": days,
        "weeks": snap["weeks"],
        "total_contributions": snap["totalContributions"],
        "cur_streak": cur_streak,
        "best_streak": best_streak,
        "active_days": active_days,
        "peak_count": peak_day["contributionCount"],
        "peak_date": peak_day["date"],
        "weekday_totals": weekday_totals,
        "langs": lang_items,
        "repo_count": snap["repoCount"],
        "public_repos": snap["publicRepoCount"],
        "private_repos": snap["privateRepoCount"],
        "updated_at": snap["updatedAt"],
    }


def render_bento_stats(metrics: dict[str, Any], theme_name: str) -> str:
    """Render 4-panel Bento Grid dashboard SVG."""
    t = THEMES[theme_name]
    wd_names = ["M", "T", "W", "T", "F", "S", "S"]
    wd_full = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd_vals = metrics["weekday_totals"]
    wd_max = max(wd_vals) or 1
    best_wd_idx = wd_vals.index(max(wd_vals))

    # Card 2 weekday bars
    bars_svg = []
    for idx, (label, val) in enumerate(zip(wd_names, wd_vals)):
        bx = 24 + idx * 36
        bar_h = max(8, int(round((val / wd_max) * 86)))
        by = 178 - bar_h
        fill = t["cyan"] if idx == best_wd_idx else t["border"]
        opacity = "0.95" if idx == best_wd_idx else "0.75"
        bars_svg.append(
            f'<rect x="{bx}" y="{by}" width="22" height="{bar_h}" rx="5" fill="{fill}" fill-opacity="{opacity}"/>'
            f'<text x="{bx + 11}" y="198" text-anchor="middle" class="mono" font-size="10" fill="{t["muted"]}">{label}</text>'
        )
        if idx == best_wd_idx:
            bars_svg.append(
                f'<text x="{bx + 11}" y="{by - 6}" text-anchor="middle" class="mono" font-size="10" font-weight="700" fill="{t["cyan"]}">{val}</text>'
            )

    # Card 3 stacked bar + language legend
    bar_segments = []
    legend_rows = []
    cx = 22.0
    total_bar_w = 246.0
    for idx, lang in enumerate(metrics["langs"]):
        seg_w = max(4.0, (lang["pct"] / 100.0) * total_bar_w)
        bar_segments.append(
            f'<rect x="{cx:.1f}" y="82" width="{seg_w:.1f}" height="10" fill="{lang["color"]}"/>'
        )
        cx += seg_w
        ly = 118 + idx * 24
        legend_rows.append(
            f'<circle cx="28" cy="{ly - 4}" r="4.5" fill="{lang["color"]}"/>'
            f'<text x="41" y="{ly}" class="sans" font-size="12" fill="{t["text"]}">{html.escape(lang["name"])}</text>'
            f'<text x="266" y="{ly}" text-anchor="end" class="mono" font-size="11.5" fill="{t["muted"]}">{lang["pct"]:.1f}%</text>'
        )

    total_str = str(metrics["total_contributions"])
    commits_label_x = 22 + len(total_str) * 28 + 10

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 260" width="1200" height="260" role="img" aria-label="Bento Engineering Telemetry">
  <defs>
    <clipPath id="langBarClip">
      <rect x="22" y="82" width="246" height="10" rx="5"/>
    </clipPath>
    <style>
      .sans {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
      @keyframes livePulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
      .live {{ animation: livePulse 2.2s ease-in-out infinite; }}
    </style>
  </defs>

  <rect width="1200" height="260" fill="{t["bg"]}"/>

  <!-- Card 1: Contribution Engine -->
  <g transform="translate(0, 8)">
    <rect x="0.75" y="0.75" width="286.5" height="242.5" rx="14" fill="{t["card_bg"]}" stroke="{t["border"]}" stroke-width="1.5"/>
    <text x="22" y="34" class="mono" font-size="11" fill="{t["muted"]}" letter-spacing="1.1">CONTRIBUTIONS</text>
    <circle cx="228" cy="30" r="3.5" fill="{t["green"]}" class="live"/>
    <text x="266" y="34" text-anchor="end" class="mono" font-size="10" fill="{t["green"]}">LIVE</text>
    <text x="22" y="88" class="sans" font-size="44" font-weight="700" fill="{t["text"]}">{total_str}</text>
    <text x="{commits_label_x}" y="86" class="mono" font-size="11.5" fill="{t["muted"]}">commits / yr</text>

    <line x1="22" y1="112" x2="266" y2="112" stroke="{t["border"]}" stroke-width="1"/>
    <text x="22" y="142" class="mono" font-size="10.5" fill="{t["muted"]}">CURRENT STREAK</text>
    <text x="22" y="164" class="sans" font-size="19" font-weight="600" fill="{t["text"]}">{metrics["cur_streak"]}d</text>
    <text x="154" y="142" class="mono" font-size="10.5" fill="{t["muted"]}">LONGEST</text>
    <text x="154" y="164" class="sans" font-size="19" font-weight="600" fill="{t["cyan"]}">{metrics["best_streak"]}d</text>

    <text x="22" y="198" class="mono" font-size="10.5" fill="{t["muted"]}">ACTIVE DAYS</text>
    <text x="22" y="220" class="sans" font-size="17" font-weight="600" fill="{t["text"]}">{metrics["active_days"]}</text>
    <text x="154" y="198" class="mono" font-size="10.5" fill="{t["muted"]}">PEAK DAY</text>
    <text x="154" y="220" class="sans" font-size="17" font-weight="600" fill="{t["text"]}">{metrics["peak_count"]} commits</text>
  </g>

  <!-- Card 2: Weekly Rhythm -->
  <g transform="translate(304, 8)">
    <rect x="0.75" y="0.75" width="286.5" height="242.5" rx="14" fill="{t["card_bg"]}" stroke="{t["border"]}" stroke-width="1.5"/>
    <text x="22" y="34" class="mono" font-size="11" fill="{t["muted"]}" letter-spacing="1.1">WEEKLY RHYTHM</text>
    <text x="22" y="58" class="sans" font-size="14" font-weight="600" fill="{t["text"]}">Peak day: {wd_full[best_wd_idx]}</text>
    {''.join(bars_svg)}
    <text x="22" y="224" class="mono" font-size="10.5" fill="{t["subtle"]}">Distribution across last 52 weeks</text>
  </g>

  <!-- Card 3: Code Languages -->
  <g transform="translate(608, 8)">
    <rect x="0.75" y="0.75" width="286.5" height="242.5" rx="14" fill="{t["card_bg"]}" stroke="{t["border"]}" stroke-width="1.5"/>
    <text x="22" y="34" class="mono" font-size="11" fill="{t["muted"]}" letter-spacing="1.1">CORE LANGUAGES</text>
    <text x="22" y="58" class="sans" font-size="14" font-weight="600" fill="{t["text"]}">{metrics["repo_count"]} repositories ({metrics["public_repos"]} pub)</text>
    <rect x="22" y="82" width="246" height="10" rx="5" fill="{t["grid"]}"/>
    <g clip-path="url(#langBarClip)">{''.join(bar_segments)}</g>
    {''.join(legend_rows)}
  </g>

  <!-- Card 4: System Node -->
  <g transform="translate(912, 8)">
    <rect x="0.75" y="0.75" width="287.5" height="242.5" rx="14" fill="{t["card_bg"]}" stroke="{t["border"]}" stroke-width="1.5"/>
    <text x="22" y="34" class="mono" font-size="11" fill="{t["muted"]}" letter-spacing="1.1">SYSTEM NODE</text>
    <rect x="196" y="19" width="70" height="20" rx="10" fill="{t["pill_bg"]}" stroke="{t["border"]}" stroke-width="1"/>
    <text x="231" y="33" text-anchor="middle" class="mono" font-size="10" fill="{t["cyan"]}">UTC+5</text>

    <text x="22" y="72" class="mono" font-size="11.5" fill="{t["muted"]}">HOST</text>
    <text x="266" y="72" text-anchor="end" class="sans" font-size="12.5" font-weight="600" fill="{t["text"]}">Al-Farabi KazNU</text>
    <line x1="22" y1="86" x2="266" y2="86" stroke="{t["grid"]}" stroke-width="1"/>

    <text x="22" y="108" class="mono" font-size="11.5" fill="{t["muted"]}">DOMAIN</text>
    <text x="266" y="108" text-anchor="end" class="sans" font-size="12.5" font-weight="600" fill="{t["text"]}">High-Load &amp; AI</text>
    <line x1="22" y1="122" x2="266" y2="122" stroke="{t["grid"]}" stroke-width="1"/>

    <text x="22" y="144" class="mono" font-size="11.5" fill="{t["muted"]}">PRIMARY</text>
    <text x="266" y="144" text-anchor="end" class="sans" font-size="12.5" font-weight="600" fill="{t["text"]}">Python · C#/.NET · CV</text>
    <line x1="22" y1="158" x2="266" y2="158" stroke="{t["grid"]}" stroke-width="1"/>

    <text x="22" y="180" class="mono" font-size="11.5" fill="{t["muted"]}">SYNCED</text>
    <text x="266" y="180" text-anchor="end" class="mono" font-size="11.5" fill="{t["accent"]}">{metrics["updated_at"]}</text>

    <!-- Subtle telemetry pulse polyline -->
    <polyline points="22,216 66,216 82,204 94,224 108,198 122,220 134,216 266,216" fill="none" stroke="{t["cyan"]}" stroke-width="1.6" stroke-opacity="0.7" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="108" cy="198" r="3" fill="{t["cyan"]}" class="live"/>
  </g>
</svg>'''


def catmull_rom_path(points: list[tuple[float, float]]) -> str:
    """Create smooth SVG cubic path through (x, y) points clamped against overshoot."""
    if len(points) < 2:
        return ""
    cmds = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]
    for i in range(len(points) - 1):
        p0 = points[max(0, i - 1)]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[min(len(points) - 1, i + 2)]
        cp1x = p1[0] + (p2[0] - p0[0]) / 6.0
        cp1y = p1[1] + (p2[1] - p0[1]) / 6.0
        cp2x = p2[0] - (p3[0] - p1[0]) / 6.0
        cp2y = p2[1] - (p3[1] - p1[1]) / 6.0
        # Clamp control point y between neighboring endpoints to stay clean above baseline
        min_y = min(p1[1], p2[1])
        max_y = max(p1[1], p2[1])
        cp1y = max(min_y, min(max_y, cp1y))
        cp2y = max(min_y, min(max_y, cp2y))
        cmds.append(f"C {cp1x:.2f} {cp1y:.2f}, {cp2x:.2f} {cp2y:.2f}, {p2[0]:.2f} {p2[1]:.2f}")
    return " ".join(cmds)


def render_activity_graph(metrics: dict[str, Any], theme_name: str) -> str:
    """Render smooth Contribution Activity Wave SVG for the last 65 days."""
    t = THEMES[theme_name]
    window = metrics["days"][-65:]
    n = len(window)
    left, right, top, bottom = 58.0, 1166.0, 90.0, 232.0
    max_val = max((d["contributionCount"] for d in window), default=1)
    y_ceil = max(4, int(math.ceil(max_val / 2.0) * 2))

    pts: list[tuple[float, float]] = []
    for idx, d in enumerate(window):
        x = left + (idx / max(1, n - 1)) * (right - left)
        y = bottom - (d["contributionCount"] / y_ceil) * (bottom - top)
        pts.append((x, y))

    line_d = catmull_rom_path(pts)
    area_d = f"{line_d} L {pts[-1][0]:.2f} {bottom:.2f} L {pts[0][0]:.2f} {bottom:.2f} Z"

    # Y-axis gridlines
    grid_svg = []
    for step in range(5):
        val = int(round((y_ceil / 4.0) * step))
        gy = bottom - (step / 4.0) * (bottom - top)
        grid_svg.append(
            f'<line x1="{left}" y1="{gy:.1f}" x2="{right}" y2="{gy:.1f}" stroke="{t["grid"]}" stroke-width="1" stroke-dasharray="4 4"/>'
            f'<text x="{left - 12}" y="{gy + 4:.1f}" text-anchor="end" class="mono" font-size="10.5" fill="{t["subtle"]}">{val}</text>'
        )

    # X-axis date labels every ~10 days
    x_labels = []
    for idx in range(0, n, 10):
        lx = pts[idx][0]
        label = window[idx]["date"][5:]
        x_labels.append(
            f'<text x="{lx:.1f}" y="{bottom + 24}" text-anchor="middle" class="mono" font-size="10.5" fill="{t["subtle"]}">{label}</text>'
        )
    if (n - 1) % 10 != 0:
        x_labels.append(
            f'<text x="{pts[-1][0]:.1f}" y="{bottom + 24}" text-anchor="end" class="mono" font-size="10.5" fill="{t["subtle"]}">{window[-1]["date"][5:]}</text>'
        )

    # Highlight non-zero points + peak annotation
    dots_svg = []
    peak_idx = max(range(n), key=lambda i: window[i]["contributionCount"]) if n else 0
    for idx, d in enumerate(window):
        if d["contributionCount"] > 0:
            px, py = pts[idx]
            r = 4.5 if idx == peak_idx else 3.0
            fill = t["green"] if idx == peak_idx else t["cyan"]
            dots_svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r}" fill="{fill}" stroke="{t["bg"]}" stroke-width="1.5"/>')

    pk_x, pk_y = pts[peak_idx]
    pk_cnt = window[peak_idx]["contributionCount"]
    pk_date = window[peak_idx]["date"][5:]
    pill_x = min(max(left + 12, pk_x - 62), right - 132)
    pill_y = max(54.0, pk_y - 34)

    total_window = sum(d["contributionCount"] for d in window)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 280" width="1200" height="280" role="img" aria-label="Contribution Activity Graph">
  <defs>
    <linearGradient id="waveStroke" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="{t["accent2"]}"/>
      <stop offset="55%" stop-color="{t["cyan"]}"/>
      <stop offset="100%" stop-color="{t["green"]}"/>
    </linearGradient>
    <linearGradient id="waveArea" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="{t["cyan"]}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="{t["cyan"]}" stop-opacity="0.01"/>
    </linearGradient>
    <style>
      .sans {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    </style>
  </defs>

  <rect x="0.75" y="0.75" width="1198.5" height="278.5" rx="14" fill="{t["card_bg"]}" stroke="{t["border"]}" stroke-width="1.5"/>

  <text x="28" y="34" class="sans" font-size="15" font-weight="600" fill="{t["text"]}">Contribution Activity Wave</text>
  <text x="234" y="34" class="mono" font-size="11.5" fill="{t["muted"]}">· Last {n} days ({total_window} commits)</text>

  <rect x="1032" y="16" width="140" height="24" rx="12" fill="{t["pill_bg"]}" stroke="{t["border"]}" stroke-width="1"/>
  <text x="1102" y="32" text-anchor="middle" class="mono" font-size="10.5" fill="{t["cyan"]}">@{USERNAME}</text>

  {''.join(grid_svg)}
  <path d="{area_d}" fill="url(#waveArea)"/>
  <path d="{line_d}" fill="none" stroke="url(#waveStroke)" stroke-width="2.6" stroke-linecap="round"/>
  {''.join(dots_svg)}
  {''.join(x_labels)}

  <!-- Peak badge -->
  <rect x="{pill_x:.1f}" y="{pill_y:.1f}" width="124" height="22" rx="11" fill="{t["bg"]}" stroke="{t["green"]}" stroke-width="1"/>
  <text x="{pill_x + 62:.1f}" y="{pill_y + 14.5:.1f}" text-anchor="middle" class="mono" font-size="10.5" fill="{t["green"]}">Peak: {pk_cnt} ({pk_date})</text>
</svg>'''


def contribution_level(count: int) -> int:
    """Map daily contribution count to GitHub heatmap level 0..4."""
    if count <= 0:
        return 0
    if count <= 2:
        return 1
    if count <= 4:
        return 2
    if count <= 6:
        return 3
    return 4


def render_contrib_snake(metrics: dict[str, Any], theme_name: str) -> str:
    """Render 53-week GitHub heatmap with an animated Cyber-Snake traversing active weeks."""
    t = THEMES[theme_name]
    weeks = metrics["weeks"][-53:]
    cell = 17
    gap = 4
    step = cell + gap
    x0 = 44
    y0 = 42

    rects = []
    active_coords: list[tuple[int, int, int]] = []
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week["contributionDays"]):
            cnt = int(day["contributionCount"])
            lvl = contribution_level(cnt)
            cx = x0 + wi * step
            cy = y0 + di * step
            fill = t[f"lvl{lvl}"]
            rects.append(f'<rect x="{cx}" y="{cy}" width="{cell}" height="{cell}" rx="4" fill="{fill}"/>')
            if cnt > 0:
                active_coords.append((wi, di, cnt))

    # Build strictly orthogonal 4-connected Manhattan loop over active columns
    min_w = max(0, active_coords[0][0] - 2) if active_coords else max(0, len(weeks) - 18)
    max_w = min(len(weeks) - 1, active_coords[-1][0] + 1) if active_coords else len(weeks) - 1
    if max_w - min_w < 10:
        min_w = max(0, len(weeks) - 16)
        max_w = len(weeks) - 1

    # Top-to-bottom / bottom-to-top boustrophedon sweep, then return along row 0 cleanly
    waypoints: list[tuple[int, int]] = []
    cur_r = 0
    for wi in range(min_w, max_w + 1):
        col_has_active = any(w == wi for (w, _, _) in active_coords)
        if col_has_active or (wi - min_w) % 4 == 0:
            target_rows = range(0, 7) if cur_r == 0 else range(6, -1, -1)
            for r in target_rows:
                waypoints.append((wi, r))
            cur_r = 6 if cur_r == 0 else 0
        else:
            waypoints.append((wi, cur_r))

    # Return cleanly back to (min_w, 0) via orthogonal steps so loop wraps seamlessly
    if cur_r != 0:
        for r in range(cur_r - 1, -1, -1):
            waypoints.append((max_w, r))
    for wi in range(max_w - 1, min_w - 1, -1):
        waypoints.append((wi, 0))

    # Build CSS keyframes along orthogonal waypoints
    duration_sec = max(12.0, round(len(waypoints) * 0.16, 1))
    step_sec = duration_sec / max(1, len(waypoints) - 1)
    kf_lines = []
    total_wp = max(1, len(waypoints) - 1)
    for idx, (wi, di) in enumerate(waypoints):
        pct = (idx / total_wp) * 100.0
        px = x0 + wi * step
        py = y0 + di * step
        kf_lines.append(f"      {pct:.2f}% {{ transform: translate({px}px, {py}px); }}")
    keyframes_css = "\n".join(kf_lines)

    snake_segs = []
    seg_styles = [
        (t["snake_head"], "1"),
        (t["cyan"], "0.86"),
        (t["accent"], "0.70"),
        (t["snake_tail"], "0.52"),
        (t["snake_tail"], "0.34"),
    ]
    for idx, (color, opacity) in enumerate(seg_styles):
        pad = idx * 1.1
        size = max(8.0, cell - pad * 2)
        delay = -idx * step_sec
        # Static initial placement: contiguous along the active cluster (or start of path)
        anchor_idx = max(4, min(len(waypoints) - 1, 10 - idx))
        wi_init, di_init = waypoints[anchor_idx]
        ix = x0 + wi_init * step
        iy = y0 + di_init * step
        snake_segs.append(
            f'<rect x="{pad:.1f}" y="{pad:.1f}" width="{size:.1f}" height="{size:.1f}" rx="4" '
            f'transform="translate({ix}, {iy})" '
            f'fill="{color}" fill-opacity="{opacity}" style="animation: snakeMove {duration_sec:.1f}s linear {delay:.2f}s infinite;"/>'
        )

    # Legend squares bottom-right
    legend = []
    for lvl in range(5):
        lx = 1046 + lvl * 18
        legend.append(f'<rect x="{lx}" y="198" width="12" height="12" rx="3" fill="{t[f"lvl{lvl}"]}"/>')

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 226" width="1200" height="226" role="img" aria-label="Contribution Matrix Snake">
  <defs>
    <style>
      .sans {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
      @keyframes snakeMove {{
{keyframes_css}
      }}
    </style>
  </defs>

  <rect x="0.75" y="0.75" width="1198.5" height="224.5" rx="14" fill="{t["card_bg"]}" stroke="{t["border"]}" stroke-width="1.5"/>

  <text x="44" y="28" class="sans" font-size="14" font-weight="600" fill="{t["text"]}">Contribution Matrix · Cyber Snake</text>
  <text x="1153" y="28" text-anchor="end" class="mono" font-size="11" fill="{t["muted"]}">53 weeks · {metrics["total_contributions"]} contributions</text>

  {''.join(rects)}
  {''.join(snake_segs)}

  <text x="44" y="208" class="mono" font-size="10.5" fill="{t["subtle"]}">Auto-updated via GitHub Actions</text>
  <text x="1038" y="208" text-anchor="end" class="mono" font-size="10" fill="{t["subtle"]}">Less</text>
  {''.join(legend)}
  <text x="1142" y="208" class="mono" font-size="10" fill="{t["subtle"]}">More</text>
</svg>'''


def write_outputs(snap: dict[str, Any]) -> None:
    """Save snapshot and render all dark + light SVG charts."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")

    metrics = compute_metrics(snap)
    for theme in ("dark", "light"):
        (ASSETS_DIR / f"bento-stats-{theme}.svg").write_text(render_bento_stats(metrics, theme), encoding="utf-8")
        (ASSETS_DIR / f"activity-graph-{theme}.svg").write_text(render_activity_graph(metrics, theme), encoding="utf-8")
        (ASSETS_DIR / f"contrib-snake-{theme}.svg").write_text(render_contrib_snake(metrics, theme), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate GitHub profile stats SVGs.")
    parser.add_argument("--live", action="store_true", help="Fetch live stats from GitHub GraphQL API.")
    args = parser.parse_args()

    baseline = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")) if SNAPSHOT_PATH.exists() else {}

    if args.live:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
            token = res.stdout.strip()
        if not token:
            raise SystemExit("Missing GH_TOKEN or GITHUB_TOKEN for --live mode.")
        try:
            raw = fetch_graphql(token, USERNAME)
            live_snap = normalize_snapshot(raw)
            snap = merge_public_with_snapshot(live_snap, baseline)
        except urllib.error.URLError as exc:
            if not baseline:
                raise SystemExit(f"Network error and no snapshot fallback: {exc}") from exc
            print(f"Warning: live fetch failed ({exc}); using snapshot.")
            snap = baseline
    else:
        if not baseline:
            raise SystemExit(f"Snapshot not found at {SNAPSHOT_PATH}")
        snap = baseline

    write_outputs(snap)
    print(f"Generated SVGs for {snap['username']} ({snap['totalContributions']} contributions, updated {snap['updatedAt']}).")


if __name__ == "__main__":
    main()
