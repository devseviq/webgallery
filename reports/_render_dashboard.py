#!/usr/bin/env python3
"""Render the operations dashboard from an explicit snapshot path."""

import argparse
import datetime
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence


def esc(s):
    return html.escape("" if s is None else str(s))


def fi(n):
    try: return format(int(n), ",")
    except Exception: return "0"


def fb(n):
    if n < 1024: return f"{n} B"
    if n < 1048576: return f"{n/1024:.1f} KB"
    if n < 1073741824: return f"{n/1048576:.1f} MB"
    return f"{n/1073741824:.2f} GB"


def fs(s):
    if s is None: return "-"
    if s < 60: return f"{s:.1f}s"
    if s < 3600: return f"{s/60:.1f}m"
    return f"{s/3600:.1f}h"


def hr(iso):
    if not iso: return "-"
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        n = datetime.datetime.now(datetime.timezone.utc)
        d = (n - dt).total_seconds()
        if d < 0: return dt.strftime("%Y-%m-%d %H:%M")
        if d < 60: return f"{int(d)}s ago"
        if d < 3600: return f"{int(d/60)}m ago"
        if d < 86400: return f"{int(d/3600)}h ago"
        return f"{int(d/86400)}d ago"
    except Exception: return iso or "-"


def sh(url):
    if not url: return ""
    try: return url.split("/")[2].replace("www.", "")
    except Exception: return ""


def _json_for_script(value: Any) -> str:
    """Serialize JSON without allowing data to terminate an HTML script tag."""
    return (
        json.dumps(value, default=str)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_dashboard(snapshot_path: Path, output_path: Path) -> Path:
    """Render one dashboard; importing this module performs no filesystem I/O."""
    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))

    # ----- Pull data out -----
    c = snap["counts"]
    lib = snap["library"]
    buf = snap["buffer"]
    movedir = snap["movedir"]
    daily = snap["daily"]
    hourly = snap["hourly"]
    durs = snap["durations"]
    tags = snap["tags"]
    wlog = snap["worker_log"]
    running = snap["running"]
    failed_jobs = snap["failed"]
    recent = snap["recent_completed"]
    pending_jobs = snap["pending"]
    gallery = snap.get("preview_gallery", {"items": []}) or {"items": []}
    worker_paused = bool(snap.get("worker_paused", False))

    total_jobs = c["total"]
    n_completed = c["completed"]
    n_failed = c["failed"]
    n_pending = c["pending"]
    n_running = c["running"]
    n_removed = c["removed"]
    pct_done = (n_completed / total_jobs * 100) if total_jobs else 0
    durs_stats = durs["stats"]
    img_count = lib["images"]
    lib_size = lib["size_bytes"]
    sidecars = lib["sidecars"]
    quarantine_images = lib.get("quarantine_images", 0)
    quarantine_size = lib.get("quarantine_size_bytes", 0)
    b_ap = buf["anime-pictures"]
    b_apf = buf["anime-pictures-full"]
    b_zc = buf["zerochan"]
    b_wh = buf["wallhaven"]
    b_gb = buf["gelbooru"]
    mdir = movedir["count"]


    CSS1 = """
      :root { color-scheme: dark; }
      * { box-sizing: border-box; }
      body {
        margin: 0; padding: 0; min-height: 100vh;
        font-family: 'Segoe UI', system-ui, -apple-system, Roboto, sans-serif;
        background: radial-gradient(ellipse at top, #161b2e 0%, #0a0e1a 60%, #050810 100%);
        color: #d8dee9; line-height: 1.5;
      }
      .page { max-width: 1500px; margin: 0 auto; padding: 24px 28px 80px; min-width: 0; }
      header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
      h1 { margin: 0; font-size: 26px; letter-spacing: 0.3px; color: #f0f6fc;
           text-shadow: 0 0 24px rgba(110, 168, 255, 0.25); }
      h2 { color: #f0f6fc; font-size: 18px; margin: 28px 0 10px;
           border-bottom: 1px solid #21262d; padding-bottom: 6px; display: flex; align-items: center; gap: 10px; }
      h2 .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: #1f2937; color: #8b949e; font-weight: 500; }
      h3 { color: #79c0ff; font-size: 13px; margin: 14px 0 6px; text-transform: uppercase; letter-spacing: 0.6px; }
      .sub { color: #8b949e; font-size: 12px; }
      .ctrls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      button, .btn {
        background: #161b22; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 12px;
        border-radius: 6px; font-size: 12px; cursor: pointer; font-family: inherit;
        text-decoration: none;
      }
      button:hover, .btn:hover { background: #1c2128; border-color: #58a6ff; }
      button.active { background: #1f6feb33; border-color: #58a6ff; color: #79c0ff; }
      button:disabled { opacity: 0.5; cursor: wait; }
      .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #f85149; display: inline-block; margin-right: 6px; }
      .live-dot.on { background: #3fb950; box-shadow: 0 0 8px #3fb95066; }
      .live-dot.spin { background: #d2a8ff; box-shadow: 0 0 8px #d2a8ff66; animation: pulse 0.9s ease-in-out infinite; }
      @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
      .interval-select {
        background: #0a0e1a; color: #c9d1d9; border: 1px solid #30363d; padding: 5px 8px;
        border-radius: 6px; font-size: 12px; font-family: inherit; cursor: pointer;
      }
      .interval-select:focus { outline: none; border-color: #58a6ff; }
      .last-refresh { color: #6e7681; font-size: 11px; margin-left: 4px; font-variant-numeric: tabular-nums; }
      .kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 18px; }
      .kpi {
        background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
        border: 1px solid #30363d; border-radius: 10px; padding: 14px 16px; position: relative; overflow: hidden;
      }
      .kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--accent, #58a6ff); }
      .kpi .label { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 0.7px; }
      .kpi .value { color: #f0f6fc; font-size: 28px; font-weight: 700; margin-top: 2px; line-height: 1.1; font-variant-numeric: tabular-nums; }
      .kpi .sub2 { color: #6e7681; font-size: 11px; margin-top: 4px; }
      .kpi.warn::before { background: #f0883e; } .kpi.warn .value { color: #ffa657; }
      .kpi.bad::before { background: #f85149; } .kpi.bad .value { color: #ff7b72; }
      .kpi.good::before { background: #3fb950; } .kpi.good .value { color: #56d364; }
      .kpi.cool::before { background: #d2a8ff; } .kpi.cool .value { color: #d2a8ff; }
      .kpi.gold::before { background: #ffd866; } .kpi.gold .value { color: #ffd866; }
      .row { display: grid; gap: 16px; }
      .row > * { min-width: 0; }
      .row-2 { grid-template-columns: 1.4fr 1fr; }
      .row-3 { grid-template-columns: 1fr 1fr 1fr; }
      .row-2eq { grid-template-columns: 1fr 1fr; }
      @media (max-width: 1000px) { .row-2, .row-3, .row-2eq { grid-template-columns: 1fr; } }
      .panel { min-width: 0; max-width: 100%; overflow-x: auto; background: #0d1117; border: 1px solid #21262d; border-radius: 10px; padding: 14px 16px; }
      .panel h3 { margin-top: 0; }
      table { width: 100%; max-width: 100%; border-collapse: collapse; font-size: 13px; }
      th { color: #8b949e; font-weight: 500; text-align: left; padding: 6px 8px; border-bottom: 1px solid #21262d; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
      td { padding: 5px 8px; border-bottom: 1px solid #1c2128; }
      tr:hover td { background: #161b2288; }
      td.num { text-align: right; color: #8b949e; font-variant-numeric: tabular-nums; }
      td.url { font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 11px; color: #79c0ff; word-break: break-all; }
      .pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px; white-space: nowrap; }
      .pill-completed { background: #23863633; color: #56d364; }
      .pill-running { background: #1f6feb33; color: #79c0ff; }
      .pill-pending { background: #6e768133; color: #c9d1d9; }
      .pill-failed { background: #da363333; color: #ff7b72; }
      .pill-removed { background: #6e768133; color: #8b949e; }
      .bar { background: #58a6ff; height: 14px; border-radius: 2px; min-width: 2px; transition: filter 0.1s; }
      .bar.warn { background: #f0883e; } .bar.bad { background: #f85149; } .bar.good { background: #3fb950; }
      .bar.cool { background: #d2a8ff; } .bar.gold { background: #ffd866; }
      .bar-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
      .bar-name { width: 200px; font-size: 12px; color: #c9d1d9; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .bar-track { flex: 1; background: #161b22; border-radius: 2px; height: 14px; overflow: hidden; }
      .bar-val { width: 90px; font-size: 11px; color: #8b949e; text-align: right; flex-shrink: 0; font-variant-numeric: tabular-nums; }
    """


    CSS2 = """
      .heatmap { display: grid; grid-template-columns: 32px repeat(24, 1fr); gap: 2px; font-size: 10px; }
      .heatmap .day { color: #8b949e; padding: 2px 4px; text-align: right; align-self: center; }
      .heatmap .cell { aspect-ratio: 1; border-radius: 2px; min-height: 16px; cursor: default; transition: transform 0.1s; }
      .heatmap .cell:hover { outline: 1px solid #79c0ff; transform: scale(1.3); z-index: 2; }
      .heatmap-header { display: grid; grid-template-columns: 32px repeat(24, 1fr); gap: 2px; margin-bottom: 4px; }
      .heatmap-scroll { overflow-x: auto; }
      .heatmap-scroll .heatmap, .heatmap-scroll .heatmap-header { min-width: 620px; }
      .heatmap-header .h { color: #8b949e; text-align: center; padding: 2px 0; font-size: 9px; }
      .heatmap-legend { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 11px; color: #8b949e; justify-content: flex-end; }
      .heatmap-legend .cell { width: 14px; height: 14px; aspect-ratio: auto; }
      .tag-cloud { display: flex; flex-wrap: wrap; gap: 6px; padding: 4px 0; }
      .tag { background: #161b22; border: 1px solid #30363d; padding: 3px 8px; border-radius: 12px; color: #c9d1d9; }
      .tag:hover { background: #1f2937; border-color: #58a6ff; }
      .tag.franchise { border-color: #8957e566; color: #d2a8ff; background: #2d1b6933; }
      .treemap { width: 100%; height: 460px; background: #0a0e1a; border-radius: 8px; display: block; }
      .treemap rect { stroke: #0a0e1a; stroke-width: 2; cursor: pointer; transition: filter 0.1s; }
      .treemap rect:hover { filter: brightness(1.4); }
      .treemap text { font-size: 11px; fill: #fff; pointer-events: none; font-family: inherit; }
      .treemap .small text { font-size: 9px; }
      .search-input {
        background: #0a0e1a; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
        padding: 6px 10px; font-size: 12px; width: 280px; font-family: inherit;
      }
      .search-input:focus { outline: none; border-color: #58a6ff; }
      .scroll-y { max-height: 420px; overflow-y: auto; }
      .scroll-y::-webkit-scrollbar { width: 8px; }
      .scroll-y::-webkit-scrollbar-track { background: #0a0e1a; }
      .scroll-y::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
      .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
      .stat { background: #161b22; padding: 8px 10px; border-radius: 6px; border-left: 2px solid #30363d; }
      .stat .l { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
      .stat .v { font-size: 18px; color: #f0f6fc; font-weight: 600; font-variant-numeric: tabular-nums; }
      .ribbon {
        background: linear-gradient(135deg, #1f6feb22 0%, #8957e522 100%);
        border: 1px solid #30363d; border-radius: 10px; padding: 14px 20px; margin-bottom: 18px;
        display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;
      }
      .ribbon .title { font-size: 14px; color: #c9d1d9; }
      .ribbon .title b { color: #f0f6fc; font-size: 18px; }
      .ribbon .progress { flex: 1; min-width: 240px; height: 8px; background: #0a0e1a; border-radius: 4px; overflow: hidden; }
      .ribbon .progress > div { height: 100%; background: linear-gradient(90deg, #3fb950, #58a6ff); transition: width 0.3s; }
      .ribbon .pct { font-size: 12px; color: #56d364; font-weight: 600; font-variant-numeric: tabular-nums; }
      footer { margin-top: 32px; padding-top: 14px; border-top: 1px solid #21262d; color: #6e7681; font-size: 11px; }
      [data-tt] { position: relative; }
      [data-tt]:hover::after {
        content: attr(data-tt); position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
        background: #1c2128; color: #f0f6fc; padding: 4px 8px; border-radius: 4px; font-size: 11px;
        white-space: pre; pointer-events: none; z-index: 100; border: 1px solid #30363d;
      }
      .muted { color: #6e7681; }
      .mono { font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 11px; }
      details summary { cursor: pointer; color: #8b949e; font-size: 12px; }
      details[open] summary { color: #c9d1d9; }
      details pre { background: #0a0e1a; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 11px; color: #8b949e; }
      pre { max-width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; }
      .snapshot-note { color: #8b949e; font-size: 11px; margin: 8px 0 0; }
      .legend { display: flex; gap: 12px; font-size: 11px; color: #8b949e; flex-wrap: wrap; margin: 6px 0; }
      .legend .it { display: flex; align-items: center; gap: 4px; }
      .legend .sw { width: 12px; height: 12px; border-radius: 2px; }
      .donut { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
      .donut svg { flex-shrink: 0; }
      .donut-legend { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
      .donut-legend .it { display: flex; align-items: center; gap: 6px; }
      .donut-legend .sw { width: 12px; height: 12px; border-radius: 2px; }
      .cmd { font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 10px; color: #8b949e; word-break: break-all; max-width: 600px; }
      .url-small { font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 10px; color: #79c0ff; word-break: break-all; }
      @media (max-width: 700px) {
        .page { padding: 16px 12px 56px; }
        h1 { font-size: 22px; }
        .kpi-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .kpi { padding: 12px; }
        .kpi .value { font-size: 24px; }
        .ribbon .progress { min-width: 100%; }
        .search-input { width: 100%; }
      }
      @media (max-width: 420px) {
        .kpi-row { grid-template-columns: 1fr; }
      }
    """


    def sub(s, **kwargs):
        """Substitute {{key}} placeholders. Avoids f-string and .format() clashes with CSS braces."""
        for k, v in kwargs.items():
            s = s.replace("{{" + k + "}}", str(v))
        return s


    def kpi(label, value, sub2="", cls="", key=""):
        extra = ""
        if sub2:
            extra = '<div class="sub2">' + esc(sub2) + '</div>'
        data_key = ' data-kpi="' + esc(key) + '"' if key else ""
        return (
            '<div class="kpi ' + cls + '"' + data_key + '>'
            '<div class="label">' + esc(label) + '</div>'
            '<div class="value">' + esc(value) + '</div>'
            + extra +
            '</div>'
        )


    # Today
    _today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    total_today = daily.get(_today_str, 0)

    total_buf = b_ap.get("files", 0) + b_apf.get("files", 0) + b_zc.get("files", 0) + b_wh.get("files", 0) + b_gb.get("files", 0)

    KPI_HTML = (
        kpi("Completed", fi(n_completed), f"of {fi(total_jobs)} jobs * {pct_done:.1f}%", "good", "completed")
        + kpi("Pending", fi(n_pending), "queued for next worker tick", key="pending")
        + kpi("Running", fi(n_running), "currently active", "warn", "running")
        + kpi("Failed", fi(n_failed),
              f"{c.get('failed_by_handler', {}).get('wallpaper-download', 0)} gallery-dl + {c.get('failed_by_handler', {}).get('wallpaper-anime-pictures', 0)} ap",
              "bad" if n_failed else "good", "failed")
        + kpi("Library", fi(img_count), f"{fb(lib_size)} * {fi(sidecars)} sidecars", "gold")
        + kpi("Quarantine", fi(quarantine_images), f"{fb(quarantine_size)} in _ExactDuplicates", "warn")
        + kpi("Jobs today", fi(total_today), "UTC calendar day", "cool", "today")
        + kpi("Incoming buffer", fi(total_buf), "files awaiting sort-downloads")
        + kpi("movedir", fi(mdir), ".url stubs pending move", "warn")
        + kpi("Median job", fs(durs_stats.get("median", 0)),
              f"avg {fs(durs_stats.get('avg', 0))} * max {fs(durs_stats.get('max', 0))}")
        + kpi("Last worker tick", hr(snap.get("last_worker_at")),
              (snap.get("last_message") or "")[:60], cls="", key="last-worker")
    )


    # Ribbon
    RIBBON_HTML = (
        '<div class="ribbon" id="ribbon">'
          '<div class="title">Queue progress: <b>' + fi(n_completed) + '</b> / <b>' + fi(total_jobs) + '</b> jobs complete'
          ' * <span class="pct">' + f"{pct_done:.1f}%" + '</span>'
          ' * <b>' + fi(n_pending) + '</b> pending'
          ' * <b>' + fi(n_running) + '</b> running'
          ' * <b>' + fi(n_failed) + '</b> failed</div>'
          '<div class="progress"><div style="width:' + f"{pct_done:.2f}%" + '"></div></div>'
        '</div>'
    )

    SUB_META = (
        f"Built {snap.get('generated', '?')[:19]}Z * queue snapshot updated {snap.get('state_updated', '?')[:19]}Z * "
        f"{fi(img_count)} images, {fb(lib_size)} on disk * {fi(n_pending)} jobs pending * {fi(n_failed)} failed"
    )


    # ===== Daily chart =====
    _today = datetime.datetime.now(datetime.timezone.utc).date()
    _days = [(_today - datetime.timedelta(days=i)) for i in range(13, -1, -1)]
    _day_keys = [d.strftime("%Y-%m-%d") for d in _days]
    _day_counts = [daily.get(k, 0) for k in _day_keys]
    _max_day = max(_day_counts) if _day_counts else 1

    _chart_w, _chart_h = 720, 220
    _pad_l, _pad_r, _pad_t, _pad_b = 30, 12, 12, 30
    _n = len(_day_keys)
    _bw = (_chart_w - _pad_l - _pad_r) / _n * 0.85
    _bgap = ((_chart_w - _pad_l - _pad_r) / _n) * 0.15

    _bars = []
    for i, (k, c_) in enumerate(zip(_day_keys, _day_counts)):
        x = _pad_l + i * (_bw + _bgap) + _bgap / 2
        h = (c_ / _max_day) * (_chart_h - _pad_t - _pad_b) if _max_day else 0
        y = _chart_h - _pad_b - h
        short = k[5:]
        _bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{_bw:.1f}" height="{h:.1f}" fill="#58a6ff">'
            f'<title>{k}: {c_} jobs</title></rect>'
            f'<text x="{x + _bw/2:.1f}" y="{_chart_h - _pad_b + 12}" fill="#8b949e" font-size="9" text-anchor="middle">{short}</text>'
        )

    _y_ticks = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = _chart_h - _pad_b - frac * (_chart_h - _pad_t - _pad_b)
        _y_ticks.append(f'<line x1="{_pad_l}" y1="{y:.1f}" x2="{_chart_w - _pad_r}" y2="{y:.1f}" stroke="#21262d" stroke-width="1"/>')
        _y_ticks.append(f'<text x="{_pad_l - 4}" y="{y + 3:.1f}" fill="#8b949e" font-size="9" text-anchor="end">{int(frac*_max_day)}</text>')

    DAILY_SVG = (
        f'<svg width="100%" viewBox="0 0 {_chart_w} {_chart_h}" preserveAspectRatio="xMidYMid meet">'
        + "".join(_y_ticks) + "".join(_bars)
        + '</svg>'
    )
    DAILY_HTML = (
        DAILY_SVG
        + f'<div class="sub" style="margin-top:4px">Each bar = UTC day * {sum(_day_counts)} jobs over the last {_n} days * max {_max_day}/day</div>'
    )


    # ===== Heatmap =====
    WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _heat_grid = [[0] * 24 for _ in range(7)]
    _max_heat = 0
    for _d_idx, _d_name in enumerate(WEEKDAYS):
        for _h, _v in hourly.get(_d_name, {}).items():
            try:
                _h_int = int(_h)
            except (TypeError, ValueError):
                continue
            if 0 <= _h_int < 24:
                _heat_grid[_d_idx][_h_int] = _v
                _max_heat = max(_max_heat, _v)


    def _heat_color(v, mx):
        if mx == 0 or v == 0:
            return "#161b22"
        pct = v / mx
        if pct < 0.2: return "#1f3a5f"
        if pct < 0.4: return "#1f6feb"
        if pct < 0.6: return "#3b82f6"
        if pct < 0.8: return "#79c0ff"
        return "#d2a8ff"


    _heat_header = '<div class="heatmap-header"><div></div>' + "".join(f'<div class="h">{h}</div>' for h in range(24)) + '</div>'
    _heat_body = '<div class="heatmap">'
    for _d_idx, _d_name in enumerate(WEEKDAYS):
        _heat_body += '<div class="day">' + _d_name + '</div>'
        for _h in range(24):
            v = _heat_grid[_d_idx][_h]
            clr = _heat_color(v, _max_heat)
            _heat_body += '<div class="cell" style="background:' + clr + '" title="' + _d_name + ' ' + ('%02d' % _h) + ':00 UTC * ' + str(v) + ' jobs"></div>'
    _heat_body += '</div>'

    HEATMAP_HTML = _heat_header + _heat_body


    # ===== Status donut =====
    _status_order = ["completed", "running", "pending", "failed", "removed"]
    _status_colors = {
        "completed": "#3fb950",
        "running": "#1f6feb",
        "pending": "#8b949e",
        "failed": "#f85149",
        "removed": "#6e7681",
    }
    _s_total = sum(c["by_status"].get(s, 0) for s in _status_order) or 1

    _donut_cx, _donut_cy, _donut_r, _donut_w = 110, 110, 80, 24
    _circumference = 2 * math.pi * _donut_r
    _donut_svg = []
    _acc = 0
    for s in _status_order:
        v = c["by_status"].get(s, 0)
        if v == 0:
            continue
        frac = v / _s_total
        dash = frac * _circumference
        gap = _circumference - dash
        offset = -_acc * _circumference
        color = _status_colors[s]
        _donut_svg.append(
            f'<circle cx="{_donut_cx}" cy="{_donut_cy}" r="{_donut_r}" fill="none" stroke="{color}" stroke-width="{_donut_w}" '
            f'stroke-dasharray="{dash:.2f} {gap:.2f}" stroke-dashoffset="{offset:.2f}" '
            f'transform="rotate(-90 {_donut_cx} {_donut_cy})" />'
        )
        _acc += frac

    _donut_svg.append(
        f'<text x="{_donut_cx}" y="{_donut_cy - 4}" text-anchor="middle" fill="#f0f6fc" font-size="20" font-weight="700">{fi(total_jobs)}</text>'
        f'<text x="{_donut_cx}" y="{_donut_cy + 14}" text-anchor="middle" fill="#8b949e" font-size="10">jobs</text>'
    )

    _donut_legend = []
    for s in _status_order:
        v = c["by_status"].get(s, 0)
        if v == 0:
            continue
        pct = v / _s_total * 100
        _donut_legend.append(
            '<div class="it"><span class="sw" style="background:' + _status_colors[s] + '"></span>'
            '<span class="pill pill-' + s + '">' + s + '</span>'
            '<span class="muted" style="margin-left:auto">' + fi(v) + ' * ' + f"{pct:.1f}%" + '</span></div>'
        )

    DONUT_HTML = (
        '<div class="donut">'
        '<svg width="220" height="220" viewBox="0 0 220 220">' + "".join(_donut_svg) + '</svg>'
        '<div class="donut-legend">' + "".join(_donut_legend) + '</div>'
        '</div>'
    )


    # ===== Duration histogram =====
    _dur_order = ["<5s", "5-30s", "30s-2m", "2-10m", "10-60m", "1-4h", ">4h"]
    _dur_b = durs["buckets"]
    _dur_vals = [_dur_b.get(k, 0) for k in _dur_order]
    _dur_max = max(_dur_vals) if _dur_vals else 1

    _dur_w, _dur_h = 360, 200
    _dur_pad_l, _dur_pad_r, _dur_pad_t, _dur_pad_b = 28, 12, 12, 40
    _n = len(_dur_order)
    _dur_bw = (_dur_w - _dur_pad_l - _dur_pad_r) / _n * 0.78
    _dur_bgap = ((_dur_w - _dur_pad_l - _dur_pad_r) / _n) * 0.22

    _dur_bars = []
    _dur_colors = ["#3fb950", "#56d364", "#79c0ff", "#58a6ff", "#1f6feb", "#d2a8ff", "#f85149"]
    for i, (k, v) in enumerate(zip(_dur_order, _dur_vals)):
        x = _dur_pad_l + i * (_dur_bw + _dur_bgap) + _dur_bgap / 2
        h = (v / _dur_max) * (_dur_h - _dur_pad_t - _dur_pad_b) if _dur_max else 0
        y = _dur_h - _dur_pad_b - h
        _dur_bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{_dur_bw:.1f}" height="{h:.1f}" fill="{_dur_colors[i]}">'
            f'<title>{k}: {v} jobs</title></rect>'
            f'<text x="{x + _dur_bw/2:.1f}" y="{_dur_h - _dur_pad_b + 12}" fill="#8b949e" font-size="9" text-anchor="middle">{k}</text>'
            f'<text x="{x + _dur_bw/2:.1f}" y="{y - 3:.1f}" fill="#c9d1d9" font-size="10" text-anchor="middle">{v}</text>'
        )

    DUR_SVG = f'<svg width="100%" viewBox="0 0 {_dur_w} {_dur_h}">{"".join(_dur_bars)}</svg>'
    DUR_STATS = (
        f"n={durs_stats['count']} * median {fs(durs_stats.get('median', 0))} * avg {fs(durs_stats.get('avg', 0))} * "
        f"min {fs(durs_stats.get('min', 0))} * max {fs(durs_stats.get('max', 0))}"
    )


    # ===== Handler mix =====
    _handler_data = sorted(c["by_handler"].items(), key=lambda x: -x[1])
    _h_max = _handler_data[0][1] if _handler_data else 1
    _h_colors = {"wallpaper-download": "#58a6ff", "wallpaper-anime-pictures": "#d2a8ff", "": "#6e7681"}

    HANDLER_HTML = ""
    for k, v in _handler_data:
        if v == 0:
            continue
        pct = v / total_jobs * 100
        color = _h_colors.get(k, "#79c0ff")
        HANDLER_HTML += (
            '<div class="bar-row">'
            '<div class="bar-name" title="' + esc(k or "(none)") + '">' + esc(k or "(none)") + '</div>'
            '<div class="bar-track"><div class="bar" style="width:' + f"{v/_h_max*100:.1f}" + '%; background:' + color + '"></div></div>'
            '<div class="bar-val">' + fi(v) + ' * ' + f"{pct:.1f}%" + '</div>'
            '</div>'
        )


    # ===== Preview gallery CSS =====
    CSS_GALLERY = """
      .gallery-section { margin-top: 28px; }
      .gallery-filters {
        display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
        margin: 6px 0 12px;
      }
      .gallery-filters .filter-group {
        display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center;
        padding: 4px 8px; background: #0d1117; border: 1px solid #21262d;
        border-radius: 8px;
      }
      .gallery-filters .filter-label {
        color: #6e7681; font-size: 10px; text-transform: uppercase;
        letter-spacing: .8px; margin-right: 4px;
      }
      .gallery-tools {
        display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
        margin: -4px 0 10px;
      }
      .gallery-tools label { color: #8b949e; font-size: 11px; }
      .gallery-tools select {
        margin-left: 4px; background: #0a0e1a; color: #c9d1d9;
        border: 1px solid #30363d; border-radius: 6px; padding: 4px 8px;
        font: inherit; cursor: pointer;
      }
      .gallery-visible { color: #79c0ff; font-size: 11px; font-variant-numeric: tabular-nums; }
      .chip {
        background: #161b22; color: #c9d1d9; border: 1px solid #30363d;
        padding: 3px 9px; border-radius: 999px; font-size: 11px;
        cursor: pointer; user-select: none; font-family: inherit;
      }
      .chip:hover { border-color: #58a6ff; }
      .chip.active { background: #1f6feb33; border-color: #58a6ff; color: #79c0ff; }
      .gallery-grid {
        display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
        gap: 8px;
      }
      figure.thumb {
        position: relative; margin: 0; border-radius: 8px; overflow: hidden;
        background: #0d1117; border: 1px solid #21262d;
        aspect-ratio: 1 / 1;
      }
      figure.thumb.missing-sidecar { border-color: #6e2c2c; opacity: 0.55; }
      figure.thumb a { display: block; width: 100%; height: 100%; }
      figure.thumb img, figure.thumb video {
        display: block; width: 100%; height: 100%; object-fit: cover;
      }
      figure.thumb img { transition: transform .15s ease; }
      figure.thumb:hover img { transform: scale(1.04); }
      figure.thumb video { background: #050810; }
      figure.thumb figcaption {
        position: absolute; left: 0; right: 0; bottom: 0; padding: 6px 8px;
        background: linear-gradient(to top, rgba(0,0,0,.85), rgba(0,0,0,0));
        color: #d8dee9; font-size: 10px; line-height: 1.35;
        display: flex; flex-direction: column; gap: 1px;
        pointer-events: none;
      }
      figure.thumb .src-dot {
        position: absolute; top: 6px; right: 6px; width: 8px; height: 8px;
        border-radius: 50%; border: 1px solid rgba(255,255,255,.4);
      }
      figure.thumb .src { color: #79c0ff; font-weight: 600; font-size: 11px; }
      figure.thumb .dim { color: #8b949e; }
      figure.thumb .when { color: #6e7681; }
      .gallery-empty {
        padding: 18px; color: #6e7681; text-align: center;
        background: #0d1117; border: 1px dashed #21262d; border-radius: 8px;
      }
      @media (max-width: 700px) {
        .gallery-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }
      }

      /* Pause worker button + banner */
      #btn-pause { border-color: #30363d; }
      #btn-pause.paused { background: #f8514933; border-color: #f85149; color: #ff7b72; font-weight: 600; }
      .pause-banner {
        display: none; margin: 0 0 12px; padding: 10px 16px; border-radius: 8px;
        background: #f8514922; border: 1px solid #f8514966; color: #ff7b72;
        font-size: 13px; font-weight: 600; text-align: center;
      }
      .pause-banner.visible { display: block; animation: pulse-pause 2s ease-in-out infinite; }
      @keyframes pulse-pause { 0%,100% { opacity: 1; } 50% { opacity: 0.7; } }
    """


    # ===== Treemap =====
    _RES_BUCKETS = ["4K", "1440p", "1080p", "720p", "SD"]
    _ORIENTS = ["portrait", "landscape", "square"]
    _SOURCES = ["anime-pictures", "wallhaven", "zerochan", "unknown"]
    _SOURCE_COLORS = {"anime-pictures": "#d2a8ff", "wallhaven": "#58a6ff", "zerochan": "#3fb950", "unknown": "#6e7681"}

    _treemap_data = []
    _buckets_dict = lib["buckets"]
    for _b in _RES_BUCKETS + ["_ExactDuplicates", "_UnknownResolution"]:
        for _o in _ORIENTS:
            for _s in _SOURCES:
                _n = _buckets_dict.get(_b + "|" + _o + "|" + _s, 0)
                if _n > 0:
                    _treemap_data.append({"bucket": _b, "orient": _o, "source": _s, "n": _n})

    _treemap_data.sort(key=lambda d: -d["n"])
    _total_n = sum(d["n"] for d in _treemap_data) or 1

    _TM_W, _TM_H = 1000, 460
    _orient_totals = Counter()
    for d in _treemap_data:
        _orient_totals[d["orient"]] += d["n"]
    _ototal = sum(_orient_totals.values()) or 1

    _tm_out = []
    _x_cursor = 0
    for _orient in ("portrait", "landscape", "square"):
        _o_n = _orient_totals.get(_orient, 0)
        if _o_n == 0:
            continue
        _col_w = (_o_n / _ototal) * _TM_W
        _by_bucket = defaultdict(list)
        for d in _treemap_data:
            if d["orient"] == _orient:
                _by_bucket[d["bucket"]].append(d)
        _b_total = sum(d["n"] for d in _treemap_data if d["orient"] == _orient) or 1
        _y_cursor = 0
        for _b in _RES_BUCKETS + ["_ExactDuplicates", "_UnknownResolution"]:
            _items = _by_bucket.get(_b, [])
            if not _items:
                continue
            _b_sum = sum(d["n"] for d in _items)
            _b_h = (_b_sum / _b_total) * _TM_H
            _item_x = 0
            for d in _items:
                _iw = (d["n"] / _b_sum) * _col_w
                _color = _SOURCE_COLORS.get(d["source"], "#6e7681")
                _show_label = _iw > 50 and _b_h > 28
                _show_sub = _iw > 35 and _b_h > 20
                _txt_parts = []
                if _show_label:
                    _txt_parts.append(
                        f'<text x="{_x_cursor + _item_x + 6:.1f}" y="{_y_cursor + 14:.1f}" font-weight="600">{esc(d["source"])}</text>'
                    )
                if _show_sub:
                    _txt_parts.append(
                        f'<text x="{_x_cursor + _item_x + 6:.1f}" y="{_y_cursor + 28:.1f}" fill="#c9d1d9" font-size="10">{d["n"]:,}</text>'
                    )
                _tm_out.append(
                    f'<g><rect x="{_x_cursor + _item_x:.1f}" y="{_y_cursor:.1f}" width="{_iw:.1f}" height="{_b_h:.1f}" fill="{_color}">'
                    f'<title>{_b}/{_orient}/{d["source"]}: {d["n"]:,} sidecars</title></rect>'
                    + "".join(_txt_parts) + '</g>'
                )
                _item_x += _iw
            _y_cursor += _b_h
        _tm_out.insert(0, f'<text x="{_x_cursor + 6:.1f}" y="14" fill="#8b949e" font-size="10">{fi(_orient_totals.get(_orient, 0))} sidecars</text>')
        _tm_out.insert(0, f'<text x="{_x_cursor + 6:.1f}" y="-2" fill="#f0f6fc" font-size="14" font-weight="600">{_orient}</text>')
        _x_cursor += _col_w

    TREEMAP_SVG = (
        f'<svg class="treemap" viewBox="-2 -10 {_TM_W + 4} {_TM_H + 12}" preserveAspectRatio="xMidYMid meet">'
        + "".join(_tm_out) + '</svg>'
    )


    # ===== Library bars (source, res, ext) =====
    _source_data = sorted(lib["by_source"].items(), key=lambda x: -x[1])
    _res_data = sorted(lib["by_res"].items(), key=lambda x: -x[1])

    _source_max = _source_data[0][1] if _source_data else 1
    SOURCE_BARS = ""
    for k, v in _source_data:
        pct = v / _total_n * 100
        color = _SOURCE_COLORS.get(k, "#6e7681")
        SOURCE_BARS += (
            '<div class="bar-row">'
            '<div class="bar-name" title="' + esc(k) + '">' + esc(k) + '</div>'
            '<div class="bar-track"><div class="bar" style="width:' + f"{v/_source_max*100:.1f}" + '%; background:' + color + '"></div></div>'
            '<div class="bar-val">' + fi(v) + ' * ' + f"{pct:.1f}%" + '</div>'
            '</div>'
        )

    RES_BARS = ""
    _res_colors = {"4K": "#d2a8ff", "1440p": "#58a6ff", "1080p": "#3fb950", "720p": "#79c0ff", "SD": "#6e7681",
                  "_ExactDuplicates": "#f0883e", "_UnknownResolution": "#f85149"}
    _res_max = max((v for k, v in _res_data), default=1)
    for k, v in _res_data:
        pct = v / _total_n * 100
        color = _res_colors.get(k, "#79c0ff")
        RES_BARS += (
            '<div class="bar-row">'
            '<div class="bar-name" title="' + esc(k) + '">' + esc(k) + '</div>'
            '<div class="bar-track"><div class="bar" style="width:' + f"{v/_res_max*100:.1f}" + '%; background:' + color + '"></div></div>'
            '<div class="bar-val">' + fi(v) + ' * ' + f"{pct:.1f}%" + '</div>'
            '</div>'
        )

    _lib_ext_max = max(lib["by_ext"].values(), default=1)
    EXT_BARS = ""
    _ext_palette = {".jpg": "#3fb950", ".png": "#58a6ff", ".webp": "#d2a8ff", ".jpeg": "#79c0ff", ".avif": "#f0883e"}
    for k in [".jpg", ".png", ".webp", ".jpeg", ".avif"]:
        v = lib["by_ext"].get(k, 0)
        if v == 0:
            continue
        EXT_BARS += (
            '<div class="bar-row"><div class="bar-name">' + k + '</div>'
            '<div class="bar-track"><div class="bar" style="width:' + f"{v/_lib_ext_max*100:.1f}" + '%; background:' + _ext_palette.get(k, '#79c0ff') + '"></div></div>'
            '<div class="bar-val">' + fi(v) + '</div></div>'
        )


    # ===== Tag cloud =====
    TAG_CLOUD = ""
    _all_tags = tags["all"]
    _max_t = max((c for _, c in _all_tags[:80]), default=1)
    _franchise_names = {n for n, _ in tags["franchise"][:200]}
    for name, cnt in _all_tags[:60]:
        font_sz = 9 + (cnt / _max_t) * 13
        cls = "franchise" if name in _franchise_names else ""
        TAG_CLOUD += (
            '<span class="tag ' + cls + '" style="font-size:' + f"{font_sz:.1f}px" + '" title="' + esc(name) + ': ' + str(cnt) + ' files">'
            + esc(name) + ' <span class="muted" style="font-size:9px">*' + str(cnt) + '</span></span>'
        )


    # ===== In-flight =====
    RUNNING_HTML = ""
    for r in running:
        RUNNING_HTML += (
            '<div style="margin-bottom:14px; padding-bottom:14px; border-bottom:1px solid #21262d">'
            '<div style="display:flex; align-items:center; gap:8px; margin-bottom:6px">'
            '<span class="pill pill-running">Running</span>'
            '<span class="mono muted">' + esc(r.get("id", "")) + '</span>'
            '<span class="muted" style="font-size:11px">line ' + esc(r.get("lineNumber", ""))
            + ' * attempts ' + esc(r.get("attempts", "")) + ' * started ' + esc(hr(r.get("startedAt"))) + '</span>'
            '</div>'
            '<div class="url-small" style="margin-bottom:4px">' + esc(r.get("url", "")) + '</div>'
            '<details><summary>Effective command</summary><pre>' + esc(r.get("effectiveCommand", "")) + '</pre></details>'
            '</div>'
        )
    if not RUNNING_HTML:
        RUNNING_HTML = '<div class="sub">No jobs currently running.</div>'


    # ===== Failed =====
    FAILED_ROWS = ""
    for f in failed_jobs:
        _u = f.get("url", "") or ""
        _u_short = _u[:120] + ("..." if len(_u) > 120 else "")
        _err = (f.get("lastError") or "")[:80]
        FAILED_ROWS += (
            '<tr>'
            '<td><span class="pill pill-failed">failed</span></td>'
            '<td class="url">' + esc(_u_short) + '</td>'
            '<td class="num">' + esc(f.get("attempts", "")) + '</td>'
            '<td class="num">' + esc(f.get("exitCode", "")) + '</td>'
            '<td class="muted" style="font-size:11px">' + esc(_err) + '</td>'
            '<td class="num">' + esc(hr(f.get("finishedAt"))) + '</td>'
            '</tr>'
        )
    FAILED_HTML = (
        '<table>'
        '<thead><tr><th>Status</th><th>URL</th><th class="num">Tries</th><th class="num">Exit</th><th>Error</th><th class="num">When</th></tr></thead>'
        '<tbody>' + FAILED_ROWS + '</tbody>'
        '</table>'
    )


    # ===== Recent =====
    RECENT_ROWS = ""
    for r in recent:
        _u = r.get("url", "") or ""
        _u_short = _u[:120] + ("..." if len(_u) > 120 else "")
        RECENT_ROWS += (
            '<tr>'
            '<td><span class="pill pill-completed">done</span></td>'
            '<td class="url">' + esc(_u_short) + '</td>'
            '<td class="muted mono" style="font-size:10px">' + esc(r.get("handler", "")) + '</td>'
            '<td class="num">' + esc(r.get("attempts", "")) + '</td>'
            '<td class="num">' + esc(hr(r.get("finishedAt"))) + '</td>'
            '</tr>'
        )
    RECENT_HTML = (
        '<table>'
        '<thead><tr><th>Status</th><th>URL</th><th>Handler</th><th class="num">Tries</th><th class="num">When</th></tr></thead>'
        '<tbody>' + RECENT_ROWS + '</tbody>'
        '</table>'
    )


    # ===== Buffer stat cards =====
    def _stat_card(label, value, sub=""):
        sub_html = '<div class="muted" style="font-size:10px">' + esc(sub) + '</div>' if sub else ''
        return (
            '<div class="stat"><div class="l">' + esc(label) + '</div>'
            '<div class="v">' + esc(value) + '</div>' + sub_html + '</div>'
        )


    def _buffer_stats(d, label):
        parts = [_stat_card(label + " total", fi(d.get("files", 0)), "files")]
        for ext, cnt in list(d.get("exts", {}).items())[:4]:
            parts.append(_stat_card(ext or "(none)", fi(cnt), "files"))
        return "".join(parts)


    BUF_AP_HTML = _buffer_stats(b_ap, "anime-pictures")
    BUF_APF_HTML = _buffer_stats(b_apf, "anime-pictures-full")
    BUF_ZC_HTML = _buffer_stats(b_zc, "zerochan")
    BUF_WH_HTML = _buffer_stats(b_wh, "wallhaven")
    BUF_GB_HTML = _buffer_stats(b_gb, "gelbooru")


    # ===== movedir =====
    _mdir_rows = ""
    for s in movedir["samples"]:
        _murl = (s.get("url") or "")[:160]
        _mdir_rows += (
            '<tr><td class="mono" style="font-size:10px">' + esc(s.get("name", ""))
            + '</td><td class="url-small">' + esc(_murl) + '</td></tr>'
        )
    MOVEDIR_HTML = (
        '<table><thead><tr><th>Stub</th><th>URL</th></tr></thead><tbody>' + _mdir_rows + '</tbody></table>'
    )


    # ===== Worker log =====
    _wlog_stats = wlog["stats"]
    WORKER_HTML = (
        _stat_card("Started", fi(_wlog_stats.get("started", 0)), "in last 2k log lines")
        + _stat_card("Completed", fi(_wlog_stats.get("completed", 0)), "in last 2k log lines")
        + _stat_card("Failed", fi(_wlog_stats.get("failed", 0)), "in last 2k log lines")
        + _stat_card("Deferred", fi(_wlog_stats.get("deferred", 0)), "other downloader active")
    )

    _wlog_lines = "\n".join(esc(l) for l in wlog["tail"])
    WORKER_TAIL = (
        '<pre style="background:#0a0e1a; padding:8px; border-radius:4px; max-height:200px; overflow-y:auto; font-size:10px; color:#8b949e">'
        + _wlog_lines + '</pre>'
    )


    # ===== Pending =====
    PENDING_ROWS = ""
    for p in pending_jobs:
        _u = p.get("url", "") or ""
        _u_short = _u[:130] + ("..." if len(_u) > 130 else "")
        _q = ((p.get("url") or "") + " " + (p.get("title") or "")).lower()
        PENDING_ROWS += (
            '<tr data-q="' + esc(_q) + '">'
            '<td><span class="pill pill-pending">pending</span></td>'
            '<td class="muted mono" style="font-size:10px">' + esc(sh(p.get("url", ""))) + '</td>'
            '<td><b>' + esc(p.get("title", "")) + '</b></td>'
            '<td class="url-small">' + esc(_u_short) + '</td>'
            '<td class="num muted">' + esc(hr(p.get("addedAt"))) + '</td>'
            '</tr>'
        )

    PENDING_HTML = (
        '<table>'
        '<thead><tr><th>Status</th><th>Host</th><th>Tag / Title</th><th>URL</th><th class="num">Added</th></tr></thead>'
        '<tbody id="pending-tbody">' + PENDING_ROWS + '</tbody>'
        '</table>'
    )




    # ===== Preview gallery HTML builder =====
    SOURCE_COLORS = {
        "wallhaven": "#3fb950",
        "zerochan": "#58a6ff",
        "anime-pictures": "#d2a8ff",
        "anime-pictures-full": "#bc8cff",
        "library": "#ffd866",
        "manual-intake": "#ffa657",
        "gelbooru": "#f0883e",
        "unknown": "#6e7681",
    }
    SOURCE_LABELS = {
        "anime-pictures": "Queue previews",
        "anime-pictures-full": "Queue originals",
        "library": "Sorted library / other",
        "manual-intake": "Manual intake",
    }


    def _gallery_rel_hr(iso):
        """Short relative time for the gallery caption."""
        if not iso:
            return "-"
        try:
            dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            d = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
            if d < 0:
                return dt.strftime("%Y-%m-%d %H:%M")
            if d < 60:    return f"{int(d)}s ago"
            if d < 3600:  return f"{int(d/60)}m ago"
            if d < 86400: return f"{int(d/3600)}h ago"
            return f"{int(d/86400)}d ago"
        except Exception:
            return iso or "-"


    def build_gallery(g):
        items = g.get("items", []) or []
        if not items:
            return (
                '<h2>Last downloaded <span class="badge">no recent buffer files</span></h2>'
                '<div class="gallery-empty">No files have arrived in the buffer in the last 14 days.</div>'
            )
        sources = sorted({(it.get("source") or "unknown") for it in items})
        orients = sorted({(it.get("orient") or "unknown") for it in items if it.get("orient")})
        reses   = sorted({(it.get("res") or "unknown") for it in items if it.get("res")})
        chips_src = (
            '<span class="filter-label">source</span>'
            + '<span class="chip active" data-filter-src="all">All</span>'
            + "".join(
                f'<span class="chip" data-filter-src="{esc(s)}">{esc(SOURCE_LABELS.get(s, s))}</span>'
                for s in sources
            )
        )
        chips_or = (
            '<span class="filter-label">orient</span>'
            + '<span class="chip active" data-filter-or="all">All</span>'
            + "".join(
                f'<span class="chip" data-filter-or="{esc(o)}">{esc(o)}</span>'
                for o in orients
            )
        )
        chips_res = (
            '<span class="filter-label">res</span>'
            + '<span class="chip active" data-filter-res="all">All</span>'
            + "".join(
                f'<span class="chip" data-filter-res="{esc(r)}">{esc(r)}</span>'
                for r in reses
            )
        )
        # Build the tile markup
        tiles = []
        for index, it in enumerate(items):
            media_url = it.get("media_url") or ""
            if not (
                media_url.startswith("/media/preview/")
                or media_url.startswith("/media/library/")
            ):
                continue
            src = it.get("source") or "unknown"
            orient = it.get("orient") or "unknown"
            res = it.get("res") or "unknown"
            w = it.get("w"); h = it.get("h")
            dim_txt = (f"{w}x{h} * {res} * {orient}" if w and h else f"{res} * {orient}")
            color = SOURCE_COLORS.get(src, SOURCE_COLORS["unknown"])
            sidecar_expected = bool(it.get("sidecar_expected", True))
            klass = "thumb" + ("" if (not sidecar_expected or it.get("sidecar_present")) else " missing-sidecar")
            title = esc(it.get("subdir") or "") + " / " + esc(it.get("name") or "")
            pixels = int(w or 0) * int(h or 0)
            media_html = (
                '<a href="' + esc(media_url) + '" target="_blank" rel="noopener">'
                '<img loading="lazy" decoding="async" src="' + esc(media_url) + '" alt="">'
                '</a>'
            )
            tiles.append(
                '<figure class="' + klass + '"'
                ' data-src="' + esc(src) + '"'
                ' data-or="'  + esc(orient) + '"'
                ' data-res="' + esc(res) + '"'
                ' data-time="' + esc(it.get("mtime") or "") + '"'
                ' data-size="' + str(int(it.get("size_bytes") or 0)) + '"'
                ' data-pixels="' + str(pixels) + '"'
                ' data-index="' + str(index) + '"'
                ' title="' + title + '">'
                + media_html +
                '<span class="src-dot" style="background:' + color + '"></span>'
                '<figcaption>'
                '<span class="src">' + esc(SOURCE_LABELS.get(src, src)) + '</span>'
                '<span class="dim">' + esc(dim_txt) + '</span>'
                '<span class="when">' + esc(_gallery_rel_hr(it.get("mtime"))) + ' * ' + esc(fb(int(it.get("size_bytes") or 0))) + '</span>'
                '</figcaption>'
                '</figure>'
            )
        sub = (
            f'balanced {len(items)} recent previews across temp_downloads/ and the sorted library'
            f' * built snapshot * {esc(_gallery_rel_hr(g.get("generated")))}'
        )
        return (
            '<h2>Last downloaded <span class="badge">built snapshot * '
            f'{len(items)} tiles</span></h2>'
            '<div class="gallery-section">'
            '<div class="gallery-filters">'
            f'<div class="filter-group">{chips_src}</div>'
            f'<div class="filter-group">{chips_or}</div>'
            f'<div class="filter-group">{chips_res}</div>'
            '</div>'
            '<div class="gallery-tools">'
            '<label>Sort <select id="gallery-sort">'
            '<option value="newest">Newest arrival</option>'
            '<option value="oldest">Oldest arrival</option>'
            '<option value="largest">Largest file</option>'
            '<option value="smallest">Smallest file</option>'
            '<option value="resolution">Highest resolution</option>'
            '<option value="source">Source / folder</option>'
            '</select></label>'
            '<span class="gallery-visible" id="gallery-visible"></span>'
            '</div>'
            f'<div class="sub" style="margin-bottom:8px">{sub}</div>'
            '<div class="gallery-grid" id="gallery-grid">' + "".join(tiles) + '</div>'
            '</div>'
        )



    # ===== BODY composition =====
    BODY = ""
    BODY += build_gallery(gallery)
    BODY += '<h2>Throughput & activity <span class="badge">built snapshot * last 14 days</span></h2>'
    BODY += '<div class="row row-2">'
    BODY += '<div class="panel"><h3>Per-day job completions</h3>' + DAILY_HTML + '</div>'
    BODY += '<div class="panel"><h3>Hour-of-day activity heatmap (UTC)</h3>'
    BODY += '<div class="heatmap-scroll">' + HEATMAP_HTML + '</div>'
    BODY += '<div class="heatmap-legend"><span>less</span>'
    BODY += '<span class="cell" style="background:#161b22"></span>'
    BODY += '<span class="cell" style="background:#1f3a5f"></span>'
    BODY += '<span class="cell" style="background:#1f6feb"></span>'
    BODY += '<span class="cell" style="background:#3b82f6"></span>'
    BODY += '<span class="cell" style="background:#79c0ff"></span>'
    BODY += '<span class="cell" style="background:#d2a8ff"></span>'
    BODY += '<span>more</span></div>'
    BODY += '<div class="sub" style="margin-top:4px">Each cell = jobs completed in that UTC hour-of-week * max ' + str(_max_heat) + ' in a single cell</div>'
    BODY += '</div></div>'

    BODY += '<h2>Queue status <span class="badge">built snapshot</span></h2>'
    BODY += '<div class="row row-3">'
    BODY += '<div class="panel"><h3>Status mix (all ' + fi(total_jobs) + ' jobs)</h3>' + DONUT_HTML + '</div>'
    BODY += '<div class="panel"><h3>Job duration distribution (' + str(durs_stats['count']) + ' completed)</h3>' + DUR_SVG + '<div class="sub" style="margin-top:6px">' + DUR_STATS + '</div></div>'
    BODY += '<div class="panel"><h3>Handler mix</h3>' + HANDLER_HTML + '</div>'
    BODY += '</div>'

    BODY += '<h2>Library breakdown <span class="badge">F:\\Wallpapers\\library * ' + fi(sidecars) + ' sidecars</span></h2>'
    BODY += '<div class="row row-2">'
    BODY += '<div class="panel">'
    BODY += '<h3>Resolution x orientation x source treemap</h3>'
    BODY += '<div class="sub" style="margin-bottom:6px">Hover any tile for the exact path &amp; count. Color = source. Size = sidecar count.</div>'
    BODY += '<div class="legend">'
    BODY += '<span class="it"><span class="sw" style="background:#d2a8ff"></span>anime-pictures</span>'
    BODY += '<span class="it"><span class="sw" style="background:#58a6ff"></span>wallhaven</span>'
    BODY += '<span class="it"><span class="sw" style="background:#3fb950"></span>zerochan</span>'
    BODY += '<span class="it"><span class="sw" style="background:#6e7681"></span>unknown</span>'
    BODY += '</div>'
    BODY += TREEMAP_SVG
    BODY += '</div>'
    BODY += '<div class="panel">'
    BODY += '<h3>Source share (by indexed sidecar)</h3>' + SOURCE_BARS
    BODY += '<h3 style="margin-top:14px">Resolution buckets</h3>' + RES_BARS
    BODY += '<h3 style="margin-top:14px">File extensions (real files on disk)</h3>' + EXT_BARS
    BODY += '</div></div>'

    BODY += '<h2>Tag / franchise cloud <span class="badge">sampled ' + fi(tags.get("sample_size", 0)) + ' of ' + fi(tags.get("population", 0)) + ' sidecars</span></h2>'
    BODY += '<div class="panel">'
    BODY += '<div class="tag-cloud">' + TAG_CLOUD + '</div>'
    BODY += '<div class="sub" style="margin-top:6px">Top ' + str(min(60, len(_all_tags))) + ' of ' + str(len(tags['all'])) + ' unique tags * purple = franchise type</div>'
    BODY += '</div>'

    BODY += '<h2>In-flight now <span class="badge" data-live-badge="running">built snapshot * ' + fi(n_running) + ' running</span></h2>'
    BODY += '<div class="panel">' + RUNNING_HTML + '</div>'

    BODY += '<h2>Failures <span class="badge" data-live-badge="failed">built snapshot * ' + fi(n_failed) + ' failed</span></h2>'
    BODY += '<div class="panel scroll-y">' + FAILED_HTML + '</div>'

    BODY += '<h2>Recent completions <span class="badge">last ' + str(len(recent)) + ' of ' + fi(n_completed) + '</span></h2>'
    BODY += '<div class="panel scroll-y">' + RECENT_HTML + '</div>'

    BODY += '<h2>Incoming buffer <span class="badge">temp_downloads</span></h2>'
    BODY += '<div class="row row-3">'
    BODY += '<div class="panel"><h3>anime-pictures staging</h3>'
    BODY += '<div class="stat-grid">' + BUF_AP_HTML + '</div>'
    BODY += '<div class="sub" style="margin-top:8px">Mixed .url sidecars + .avif/.jpg payloads queued for sort-downloads.ps1</div></div>'
    BODY += '<div class="panel"><h3>anime-pictures-full (queue-browser owned)</h3>'
    BODY += '<div class="stat-grid">' + BUF_APF_HTML + '</div></div>'
    BODY += '<div class="panel"><h3>zerochan staging</h3>'
    BODY += '<div class="stat-grid">' + BUF_ZC_HTML + '</div></div>'
    BODY += '<div class="panel"><h3>wallhaven staging</h3>'
    BODY += '<div class="stat-grid">' + BUF_WH_HTML + '</div></div>'
    BODY += '<div class="panel"><h3>gelbooru staging</h3>'
    BODY += '<div class="stat-grid">' + BUF_GB_HTML + '</div></div>'
    BODY += '</div>'

    BODY += '<div class="row row-2eq" style="margin-top:12px">'
    BODY += '<div class="panel">'
    BODY += '<h3>movedir * ' + fi(mdir) + ' pending .url stubs to be moved into library</h3>'
    BODY += '<div class="sub">Sample stubs (6 of ' + fi(mdir) + '):</div>'
    BODY += MOVEDIR_HTML
    BODY += '</div>'
    BODY += '<div class="panel">'
    BODY += '<h3>Worker heartbeat</h3>'
    BODY += '<div class="stat-grid">' + WORKER_HTML + '</div>'
    BODY += '<h3 style="margin-top:12px">Recent worker log tail</h3>'
    BODY += WORKER_TAIL
    BODY += '</div></div>'

    BODY += '<h2>Pending queue <span class="badge" data-live-badge="pending">built snapshot * ' + fi(n_pending) + ' pending</span></h2>'
    BODY += '<div class="panel">'
    BODY += '<div style="display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap">'
    BODY += '<input type="text" class="search-input" id="q-search" placeholder="Filter by tag / character / franchise">'
    BODY += '<span class="sub" id="q-count">showing all ' + fi(n_pending) + '</span>'
    BODY += '</div>'
    BODY += '<div class="scroll-y">' + PENDING_HTML + '</div>'
    BODY += '</div>'


    JS = r"""
    (function() {
      var inp = document.getElementById('q-search');
      var rows = document.querySelectorAll('#pending-tbody tr[data-q]');
      var cnt = document.getElementById('q-count');
      function filter() {
        var q = (inp && inp.value || '').toLowerCase().trim();
        var shown = 0;
        rows.forEach(function(r) {
          var t = (r.getAttribute('data-q') || '').toLowerCase();
          var match = !q || t.indexOf(q) >= 0;
          r.style.display = match ? '' : 'none';
          if (match) shown++;
        });
        if (cnt) cnt.textContent = q ? ('showing ' + shown + ' of ' + rows.length) : ('showing all ' + rows.length);
      }
      if (inp) inp.addEventListener('input', filter);

      var btnLive = document.getElementById('btn-live');
      var btnRefresh = document.getElementById('btn-refresh');
      var btnRebuild = document.getElementById('btn-rebuild');
      var intervalSelect = document.getElementById('interval-select');
      var lastRefreshEl = document.getElementById('last-refresh');
      var liveDot = document.getElementById('live-dot');
      var liveText = document.getElementById('live-text');
      var liveOn = false, timer = null, currentInterval = 60000;

      function relativeTime(iso) {
        var stamp = Date.parse(iso || '');
        if (!Number.isFinite(stamp)) return '-';
        var seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
        if (seconds < 60) return seconds + 's ago';
        if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
        if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
        return Math.floor(seconds / 86400) + 'd ago';
      }

      function updateKpi(key, value, detail) {
        var root = document.querySelector('[data-kpi="' + key + '"]');
        if (!root) return;
        var valueNode = root.querySelector('.value');
        var detailNode = root.querySelector('.sub2');
        if (valueNode) valueNode.textContent = Number(value || 0).toLocaleString();
        if (detailNode && detail !== undefined) detailNode.textContent = detail;
      }

      function updateBadge(key, text) {
        var badge = document.querySelector('[data-live-badge="' + key + '"]');
        if (badge) badge.textContent = text;
      }

      async function fetchOperationsStatus() {
        try {
          var r = await fetch('/api/operations/status', {
            cache: 'no-store',
            credentials: 'same-origin'
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return await r.json();
        } catch (e) {
          console.warn('operations status fetch failed:', e);
          return null;
        }
      }

      function applyState(s) {
        if (!s || s.ok === false || !s.counts) return false;
        var c = s.counts;
        var failedByHandler = s.failed_by_handler || {};
        var total = c.total || 0, done = c.completed || 0;
        var pct = total ? (done / total * 100) : 0;
        var rt = document.querySelector('#ribbon .title');
        var rb = document.querySelector('#ribbon .progress > div');
        if (rt) rt.innerHTML = 'Queue progress: <b>' + done.toLocaleString() + '</b> / <b>' + total.toLocaleString() + '</b> jobs complete * <span class="pct">' + pct.toFixed(1) + '%</span> * <b>' + (c.pending||0).toLocaleString() + '</b> pending * <b>' + (c.running||0).toLocaleString() + '</b> running * <b>' + (c.failed||0).toLocaleString() + '</b> failed';
        if (rb) rb.style.width = pct + '%';
        updateKpi('completed', done, 'of ' + total.toLocaleString() + ' jobs * ' + pct.toFixed(1) + '%');
        updateKpi('pending', c.pending, 'live total; table remains built snapshot');
        updateKpi('running', c.running, 'live total; details remain built snapshot');
        var failedBreakdown = (failedByHandler['wallpaper-download'] || 0) + ' gallery-dl + ' +
          (failedByHandler['wallpaper-anime-pictures'] || 0) + ' ap';
        updateKpi('failed', c.failed, failedBreakdown);
        updateKpi('today', s.completed_today_utc, 'UTC calendar day * live total');
        var workerRoot = document.querySelector('[data-kpi="last-worker"]');
        if (workerRoot) {
          var workerValue = workerRoot.querySelector('.value');
          var workerDetail = workerRoot.querySelector('.sub2');
          if (workerValue) workerValue.textContent = relativeTime(s.lastWorkerAt);
          if (workerDetail) workerDetail.textContent = String(s.lastMessage || '').slice(0, 80);
        }
        updateBadge('running', 'live total ' + (c.running || 0) + ' * details built snapshot');
        updateBadge('failed', 'live total ' + (c.failed || 0) + ' * table built snapshot');
        updateBadge('pending', 'live total ' + (c.pending || 0) + ' * table built snapshot');
        var meta = document.getElementById('snapshot-meta');
        if (meta) {
          var updated = s.updatedAt ? new Date(s.updatedAt).toLocaleString() : 'unknown time';
          meta.textContent = 'Live queue state ' + updated + ' * ' + total.toLocaleString() +
            ' jobs * charts, library, buffers, and tables remain at built snapshot';
        }
        return true;
      }

      async function refreshOnce() {
        if (liveText) liveText.textContent = 'Refreshing...';
        if (liveDot) liveDot.classList.add('spin');
        var state = await fetchOperationsStatus();
        if (liveDot) liveDot.classList.remove('spin');
        if (!applyState(state)) {
          if (liveDot) liveDot.classList.remove('on');
          if (liveText) liveText.textContent = 'Refresh failed * showing last good data';
          return false;
        }
        if (liveDot) liveDot.classList.add('on');
        if (liveText) {
          var sec = Math.round(currentInterval / 1000);
          liveText.textContent = liveOn
            ? 'Live * refreshing every ' + sec + 's'
            : 'Updated now * live paused';
        }
        tickClock();
        return true;
      }

      function setLive(on) {
        liveOn = on;
        if (timer) { clearInterval(timer); timer = null; }
        if (on) {
          if (btnLive) {
            btnLive.classList.add('active');
            btnLive.textContent = 'Pause Live';
          }
          refreshOnce();
          timer = setInterval(refreshOnce, currentInterval);
        } else {
          if (liveDot) liveDot.classList.remove('on');
          if (btnLive) {
            btnLive.classList.remove('active');
            btnLive.textContent = 'Play Live';
          }
          if (liveText) liveText.textContent = 'Live paused * showing last fetched totals';
        }
        try { localStorage.setItem('wpp.liveOn', on ? '1' : '0'); } catch (e) {}
      }

      function setIntervalMs(ms) {
        var n = parseInt(ms, 10);
        if (!Number.isFinite(n) || n < 5000) n = 60000;
        currentInterval = n;
        if (intervalSelect) intervalSelect.value = String(n);
        try { localStorage.setItem('wpp.intervalMs', String(n)); } catch (e) {}
        if (liveOn) {
          if (timer) { clearInterval(timer); timer = null; }
          timer = setInterval(refreshOnce, currentInterval);
        }
        if (liveText) {
          var sec = Math.round(n / 1000);
          liveText.textContent = liveOn
            ? ('Live * refreshing every ' + sec + 's')
            : 'Live paused * showing last fetched totals';
        }
      }

      function fmtClock(d) {
        function pad(x) { return x < 10 ? '0' + x : '' + x; }
        return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
      }

      function tickClock() {
        if (!lastRefreshEl) return;
        var d = new Date();
        lastRefreshEl.textContent = 'now ' + fmtClock(d);
      }

      async function rebuildAndReload() {
        if (!btnRebuild) return;
        btnRebuild.disabled = true;
        var origText = btnRebuild.textContent;
        btnRebuild.textContent = 'Rebuilding...';
        if (liveDot) liveDot.classList.add('spin');
        if (liveText) liveText.textContent = 'Rebuilding snapshot + HTML...';
        try {
          var r = await fetch('/_rebuild?wait=1', {
            method: 'POST',
            cache: 'no-store',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: '{}'
          });
          if (r && r.ok) {
            if (liveText) liveText.textContent = 'Rebuilt * reloading in 1.5s...';
            setTimeout(function() { location.reload(); }, 1500);
          } else {
            if (liveText) liveText.textContent = 'Rebuild endpoint missing * see watch_dashboard.ps1';
            if (liveDot) liveDot.classList.remove('spin');
            btnRebuild.disabled = false;
            btnRebuild.textContent = origText;
          }
        } catch (e) {
          if (liveText) liveText.textContent = 'Rebuild request failed: ' + e.message;
          if (liveDot) liveDot.classList.remove('spin');
          btnRebuild.disabled = false;
          btnRebuild.textContent = origText;
        }
      }

      if (btnLive) btnLive.addEventListener('click', function(){ setLive(!liveOn); });
      if (btnRefresh) btnRefresh.addEventListener('click', function(){
        if (liveDot) liveDot.classList.add('spin');
        refreshOnce().finally(function(){
          if (liveDot) liveDot.classList.remove('spin');
          tickClock();
        });
      });
      if (intervalSelect) intervalSelect.addEventListener('change', function(){
        setIntervalMs(this.value);
      });
      if (btnRebuild) btnRebuild.addEventListener('click', rebuildAndReload);

      // ===== Pause worker button =====
      (function() {
        var btn = document.getElementById('btn-pause');
        var banner = document.getElementById('pause-banner');
        if (!btn) return;
        var paused = false;

        function updateUI() {
          if (paused) {
            btn.textContent = 'Resume Worker';
            btn.classList.add('paused');
            if (banner) banner.classList.add('visible');
          } else {
            btn.textContent = 'Pause Worker';
            btn.classList.remove('paused');
            if (banner) banner.classList.remove('visible');
          }
        }

        // Initial state from embedded snapshot
        try {
          if (window.SNAPSHOT && window.SNAPSHOT.worker_paused) {
            paused = true;
            updateUI();
          }
        } catch (e) {}

        async function pollPause() {
          try {
            var r = await fetch('/_pause_status', {
              cache: 'no-store',
              credentials: 'same-origin'
            });
            if (r && r.ok) {
              var d = await r.json();
              if (d && typeof d.paused === 'boolean') {
                paused = d.paused;
                updateUI();
              }
            }
          } catch (e) { /* file:// or no server - silently skip */ }
        }

        btn.addEventListener('click', async function() {
          btn.disabled = true;
          var orig = btn.textContent;
          btn.textContent = '...';
          try {
            var endpoint = paused ? '/_resume' : '/_pause';
            var r = await fetch(endpoint, {
              method: 'POST',
              cache: 'no-store',
              credentials: 'same-origin',
              headers: {'Content-Type': 'application/json'},
              body: '{}'
            });
            if (r && r.ok) {
              var d = await r.json();
              if (d && typeof d.paused === 'boolean') {
                paused = d.paused;
                updateUI();
              }
            } else {
              btn.textContent = 'Error (server?)';
              setTimeout(function(){ btn.textContent = orig; }, 2000);
            }
          } catch (e) {
            btn.textContent = 'Error (file://?)';
            setTimeout(function(){ btn.textContent = orig; }, 2000);
          }
          btn.disabled = false;
        });

        setInterval(pollPause, 30000);
        pollPause();
      })();

      // Initialise interval from storage or query string
      var storedMs = null, storedOn = null;
      try { storedMs = parseInt(localStorage.getItem('wpp.intervalMs') || '', 10); } catch (e) {}
      try { storedOn = localStorage.getItem('wpp.liveOn'); } catch (e) {}
      var qLive = location.search.indexOf('live=1') >= 0;
      var qPause = location.search.indexOf('live=0') >= 0;
      var qMs = (function(){
        var m = location.search.match(/[?&]interval=(\d+)/);
        return m ? parseInt(m[1], 10) : null;
      })();
      if (qMs) setIntervalMs(qMs);
      else if (Number.isFinite(storedMs) && storedMs >= 5000) setIntervalMs(storedMs);
      else setIntervalMs(60000);

      tickClock();
      setInterval(tickClock, 1000);

      // Default ON, unless explicitly paused via ?live=0 or stored '0'

      // ===== Gallery filter chips =====
      (function() {
        var grid = document.getElementById('gallery-grid');
        if (!grid) return;
        var tiles = Array.prototype.slice.call(grid.querySelectorAll('figure.thumb'));
        var sortSelect = document.getElementById('gallery-sort');
        var visibleCount = document.getElementById('gallery-visible');
        var state = { src: 'all', or: 'all', res: 'all', sort: 'newest' };

        // ----- Read initial state: URL -> localStorage -> default -----
        function readQuery(name) {
          var m = location.search.match(new RegExp('[?&]' + name + '=([^&]+)'));
          return m ? decodeURIComponent(m[1]) : null;
        }
        function readStored(key) {
          try { return localStorage.getItem('wpp.gallery.' + key); } catch (e) { return null; }
        }
        function writeStored(key, val) {
          try { localStorage.setItem('wpp.gallery.' + key, val); } catch (e) {}
        }
        function isValid(key, val) {
          if (val === 'all') return true;
          var attr = { src: 'data-src', or: 'data-or', res: 'data-res' }[key];
          for (var i = 0; i < tiles.length; i++) {
            if (tiles[i].getAttribute(attr) === val) return true;
          }
          return false;
        }
        function resolveInitial() {
          var s = { src: 'all', or: 'all', res: 'all' };
          ['src', 'or', 'res'].forEach(function (k) {
            var fromUrl = readQuery('g_' + k);
            if (fromUrl && isValid(k, fromUrl)) { s[k] = fromUrl; return; }
            var fromLs = readStored(k);
            if (fromLs && isValid(k, fromLs)) { s[k] = fromLs; return; }
          });
          var allowedSorts = ['newest', 'oldest', 'largest', 'smallest', 'resolution', 'source'];
          var requestedSort = readQuery('g_sort') || readStored('sort') || 'newest';
          s.sort = allowedSorts.indexOf(requestedSort) >= 0 ? requestedSort : 'newest';
          return s;
        }

        // Mark the chip matching `state[k]` as .active, clear the rest in
        // the same group.
        function paintChips() {
          var groups = [
            { attr: 'filter-src', key: 'src' },
            { attr: 'filter-or',  key: 'or'  },
            { attr: 'filter-res', key: 'res' },
          ];
          groups.forEach(function (g) {
            var chips = document.querySelectorAll('.chip[data-' + g.attr + ']');
            chips.forEach(function (c) { c.classList.remove('active'); });
            chips.forEach(function (c) {
              if (c.getAttribute('data-' + g.attr) === state[g.key]) {
                c.classList.add('active');
              }
            });
          });
          if (sortSelect) sortSelect.value = state.sort;
        }

        // Count tiles that match the current state on every dimension
        // EXCEPT the one we're displaying the "All" count for. Lets the
        // "All" chip in each group show "how many would be visible if you
        // clicked me right now".
        function countForExcept(key) {
          var n = 0;
          for (var i = 0; i < tiles.length; i++) {
            var t = tiles[i];
            if (key !== 'src' && state.src !== 'all' && t.getAttribute('data-src') !== state.src) continue;
            if (key !== 'or'  && state.or  !== 'all' && t.getAttribute('data-or')  !== state.or)  continue;
            if (key !== 'res' && state.res !== 'all' && t.getAttribute('data-res') !== state.res) continue;
            n++;
          }
          return n;
        }

        function numericAttr(tile, name) {
          var n = Number(tile.getAttribute(name) || 0);
          return Number.isFinite(n) ? n : 0;
        }

        function compareTiles(a, b) {
          var ai = numericAttr(a, 'data-index');
          var bi = numericAttr(b, 'data-index');
          if (state.sort === 'oldest') {
            return Date.parse(a.getAttribute('data-time') || 0) - Date.parse(b.getAttribute('data-time') || 0) || ai - bi;
          }
          if (state.sort === 'largest') return numericAttr(b, 'data-size') - numericAttr(a, 'data-size') || ai - bi;
          if (state.sort === 'smallest') return numericAttr(a, 'data-size') - numericAttr(b, 'data-size') || ai - bi;
          if (state.sort === 'resolution') return numericAttr(b, 'data-pixels') - numericAttr(a, 'data-pixels') || ai - bi;
          if (state.sort === 'source') {
            var bySource = (a.getAttribute('data-src') || '').localeCompare(b.getAttribute('data-src') || '');
            return bySource || (a.title || '').localeCompare(b.title || '') || ai - bi;
          }
          return Date.parse(b.getAttribute('data-time') || 0) - Date.parse(a.getAttribute('data-time') || 0) || ai - bi;
        }

        function apply() {
          tiles.sort(compareTiles).forEach(function(t) { grid.appendChild(t); });
          var shown = 0;
          tiles.forEach(function(t) {
            var match = (state.src === 'all' || t.getAttribute('data-src') === state.src)
                     && (state.or  === 'all' || t.getAttribute('data-or')  === state.or)
                     && (state.res === 'all' || t.getAttribute('data-res') === state.res);
            t.style.display = match ? '' : 'none';
            if (match) shown++;
          });
          if (visibleCount) visibleCount.textContent = shown + ' of ' + tiles.length + ' previews';
          // Update the "All" chip in each group to show the count that
          // would result from clicking it.
          var labels = { src: 'source', or: 'orient', res: 'res' };
          ['src', 'or', 'res'].forEach(function (key) {
            var allChip = null;
            var groups = document.querySelectorAll('.gallery-filters .filter-group');
            for (var i = 0; i < groups.length; i++) {
              var lbl = groups[i].querySelector('.filter-label');
              if (lbl && lbl.textContent === labels[key]) {
                allChip = groups[i].querySelector('.chip[data-filter-' + key + '="all"]');
                break;
              }
            }
            if (!allChip) return;
            if (!allChip.dataset.baseLabel) allChip.dataset.baseLabel = allChip.textContent;
            var suffix = state[key] === 'all' ? '' : ' (' + countForExcept(key) + ')';
            allChip.textContent = allChip.dataset.baseLabel + suffix;
          });
        }

        function syncUrl() {
          try {
            var u = new URL(location.href);
            ['src', 'or', 'res'].forEach(function (k) {
              if (state[k] === 'all') u.searchParams.delete('g_' + k);
              else u.searchParams.set('g_' + k, state[k]);
            });
            if (state.sort === 'newest') u.searchParams.delete('g_sort');
            else u.searchParams.set('g_sort', state.sort);
            history.replaceState(null, '', u.toString());
          } catch (e) {
            // Old browsers / file://: skip URL sync; localStorage still
            // remembers state for next load.
          }
        }

        function wireGroup(attr, key) {
          var chips = document.querySelectorAll('.chip[data-' + attr + ']');
          chips.forEach(function(c) {
            c.addEventListener('click', function() {
              chips.forEach(function(o){
                if (o.getAttribute('data-' + attr) !== undefined
                    && o !== c
                    && o.parentNode === c.parentNode) {
                  o.classList.remove('active');
                }
              });
              c.classList.add('active');
              state[key] = c.getAttribute('data-' + attr);
              apply();
              writeStored(key, state[key]);
              syncUrl();
            });
          });
        }
        wireGroup('filter-src', 'src');
        wireGroup('filter-or',  'or');
        wireGroup('filter-res', 'res');
        if (sortSelect) sortSelect.addEventListener('change', function() {
          state.sort = this.value;
          writeStored('sort', state.sort);
          apply();
          syncUrl();
        });

        // Resolve initial state from URL / localStorage, paint chips, and
        // apply once so a deep link or refresh comes back to the same view.
        state = resolveInitial();
        paintChips();
        apply();
        // Sync URL once on load so a bookmark from a previous visit picks
        // up the same params; harmless if URL is already correct.
        syncUrl();
      })();

      if (qPause) setLive(false);
      else if (qLive) setLive(true);
      else if (storedOn === '0') setLive(false);
      else setLive(true);
    })();
    """


    # ===== Assemble final HTML =====
    _snapshot_json = _json_for_script(snap)

    HEADER_HTML = (
        '<header>'
        '<div>'
        '<h1>Wallpaper Download Queue - Live Dashboard</h1>'
        '<div class="sub" id="snapshot-meta">' + esc(SUB_META) + '</div>'
        '</div>'
        '<div class="ctrls">'
        '<a class="btn" href="/library">Browse Library</a>'
        '<span class="sub"><span class="live-dot" id="live-dot"></span><span id="live-text">Built snapshot</span></span>'
        '<span class="sub">every</span>'
        '<select class="interval-select" id="interval-select" title="Auto-refresh interval">'
        '<option value="10000">10s</option>'
        '<option value="15000">15s</option>'
        '<option value="30000">30s</option>'
        '<option value="60000">60s</option>'
        '<option value="120000">2m</option>'
        '<option value="300000">5m</option>'
        '</select>'
        '<button id="btn-live" title="Toggle automatic refresh of sanitized queue totals">Play Live</button>'
        '<button id="btn-refresh" title="Reload now">Refresh</button>'
        '<button id="btn-rebuild" title="Run the Python snapshot + HTML build pipeline, then reload">Rebuild &amp; Reload</button>'
        '<button id="btn-pause" title="Pause/resume the download worker (creates pause.flag)" class="btn-pause">Pause Worker</button>'
        '<span class="last-refresh" id="last-refresh"></span>'
        '</div>'
        '</header>'
    )

    FOOTER_HTML = (
        '<footer>'
        '<div>Snapshot generated ' + str(snap.get("generated", "?")[:19]) + 'Z from explicitly configured queue, library, preview, and report roots.</div>'
        '<div>Live mode is ON by default * picks a refresh interval from the dropdown (10s-5m) * charts and detailed tables remain labeled build-time snapshots. Pass <code>?live=0</code> to start paused or <code>?interval=15000</code> to set 15s.</div>'
        '<div>The local allowlisted server provides sanitized live totals and same-origin POST controls; raw runtime files are not HTTP resources.</div>'
        '</footer>'
    )

    HTML = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Wallpaper Download Queue - Live Dashboard</title>\n'
        '<style>' + CSS1 + CSS2 + CSS_GALLERY + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="page">\n'
        + HEADER_HTML + '\n'
        + RIBBON_HTML + '\n'
        + '<div class="snapshot-note" id="live-scope">Live refresh updates the top queue totals, handler failures, jobs today, and worker heartbeat. Charts and detailed tables remain at their labeled build time.</div>\n'
        + '<div class="kpi-row" id="kpi-row">' + KPI_HTML + '</div>\n'
        + '<div class="pause-banner" id="pause-banner">⏸ WORKER PAUSED - no new download jobs will start until you click Resume. Running jobs finish naturally.</div>\n'
        + BODY + '\n'
        + FOOTER_HTML + '\n'
        '</div>\n'
        '<script>window.SNAPSHOT = ' + _snapshot_json + ';</script>\n'
        '<script>' + JS + '</script>\n'
        '</body>\n'
        '</html>\n'
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(HTML, encoding="utf-8")
    print("HTML written: " + str(output_path) + " (" + str(output_path.stat().st_size) + " bytes, " + str(len(HTML)) + " chars)")
    return output_path


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the operations dashboard from an explicit snapshot."
    )
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; importing this module never reads or writes runtime state."""
    args = _argument_parser().parse_args(argv)
    render_dashboard(args.snapshot_path, args.output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
