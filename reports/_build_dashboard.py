#!/usr/bin/env python3
"""Build a self-contained HTML dashboard for the wallpaper download queue."""
import json, os, re, datetime
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"F:\Wallpapers")
STATE = ROOT / ".wallpaper-download-queue" / "state.json"
WORKER_LOG = ROOT / ".wallpaper-download-queue" / "worker.log"
LIBRARY = ROOT / "library"
TEMP_AP = ROOT / "temp_downloads" / "anime-pictures"
TEMP_APF = ROOT / "temp_downloads" / "anime-pictures-full"
TEMP_ZC = ROOT / "temp_downloads" / "zerochan"
TEMP_WH = ROOT / "temp_downloads" / "wallhaven"
TEMP_GB = ROOT / "temp_downloads" / "gelbooru"
MOVEDIR = ROOT / "movedir"
OUT = ROOT / "reports" / "download-queue-dashboard.html"

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
GALLERY_EXT = IMG_EXT | {".gif", ".webm", ".mp4"}
SOURCES = ["anime-pictures", "wallhaven", "zerochan", "unknown"]
RES_BUCKETS = ["4K", "1440p", "1080p", "720p", "SD"]
ORIENTS = ["portrait", "landscape", "square"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def safe_load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def fmt_bytes(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/(1024*1024):.1f} MB"
    return f"{n/(1024*1024*1024):.2f} GB"


def hr_relative(iso):
    if not iso:
        return "-"
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return iso
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = (now - dt).total_seconds()
    if delta < 0:
        return dt.strftime("%Y-%m-%d %H:%M")
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta/60)}m ago"
    if delta < 86400:
        return f"{int(delta/3600)}h ago"
    return f"{int(delta/86400)}d ago"


def short_host(url):
    try:
        h = url.split("/")[2] if "://" in url else ""
        return h.replace("www.", "")
    except Exception:
        return ""


def derive_title(url):
    if not url:
        return ""
    try:
        if "search_tag=" in url:
            m = re.search(r"search_tag=([^&]+)", url)
            if m:
                return m.group(1).replace("+", " ")
        if "wallhaven.cc" in url:
            m = re.search(r"\?q=([^&]+)", url)
            if m:
                return m.group(1).replace("%20", " ").replace("+", " ")
        if "zerochan.net" in url:
            m = re.search(r"zerochan\.net/([^?]+)", url)
            if m:
                return m.group(1).replace("+", " ")
        if "anime-pictures.net/posts/" in url:
            m = re.search(r"posts/(\d+)", url)
            if m:
                return f"#{m.group(1)}"
    except Exception:
        pass


# ===== Load state.json =====
state = safe_load(STATE) or {}
jobs = state.get("jobs", [])
print(f"Loaded {len(jobs)} jobs from state.json")
by_status = Counter(j.get("status") for j in jobs)
by_handler = Counter(j.get("handler") or "" for j in jobs)
completed = [j for j in jobs if j.get("status") == "completed"]
failed = [j for j in jobs if j.get("status") == "failed"]
pending = [j for j in jobs if j.get("status") == "pending"]
running = [j for j in jobs if j.get("status") == "running"]
removed = [j for j in jobs if j.get("status") == "removed"]
print(f"  by_status: {dict(by_status)}")
print(f"  by_handler: {dict(by_handler)}")

# Daily completions
daily = Counter()
for j in completed:
    fa = j.get("finishedAt")
    if fa:
        daily[fa[:10]] += 1

# Hourly (UTC) by day-of-week
hourly = defaultdict(lambda: Counter())
for j in completed:
    fa = j.get("finishedAt")
    if fa:
        try:
            dt = datetime.datetime.fromisoformat(fa.replace("Z", "+00:00"))
            hourly[dt.weekday()][dt.hour] += 1
        except Exception:
            pass

# Durations
durations = []
for j in completed:
    try:
        s = datetime.datetime.fromisoformat(j["startedAt"].replace("Z", "+00:00"))
        e = datetime.datetime.fromisoformat(j["finishedAt"].replace("Z", "+00:00"))
        durations.append((e - s).total_seconds())
    except Exception:
        pass

dur_buckets = Counter()
for d in durations:
    if d < 5:
        dur_buckets["<5s"] += 1
    elif d < 30:
        dur_buckets["5-30s"] += 1
    elif d < 120:
        dur_buckets["30s-2m"] += 1
    elif d < 600:
        dur_buckets["2-10m"] += 1
    elif d < 3600:
        dur_buckets["10-60m"] += 1
    elif d < 14400:
        dur_buckets["1-4h"] += 1
    else:
        dur_buckets[">4h"] += 1

dur_stats = {
    "count": len(durations),
    "median": sorted(durations)[len(durations)//2] if durations else 0,
    "avg": sum(durations)/len(durations) if durations else 0,
    "max": max(durations) if durations else 0,
    "min": min(durations) if durations else 0,
}


# ===== Library scan =====
print("Scanning library...")
lib_buckets = Counter()
lib_total_sidecars = 0
lib_total_images = 0
lib_canonical_images = 0
lib_quarantine_images = 0
lib_by_ext = Counter()
lib_total_size = 0
lib_canonical_size = 0
lib_quarantine_size = 0
source_totals = Counter()
res_totals = Counter()
orient_totals = Counter()
all_json = []

if LIBRARY.is_dir():
    try:
        library_files = LIBRARY.rglob("*")
        for f in library_files:
            try:
                if not f.is_file():
                    continue
                rel = f.relative_to(LIBRARY)
                parts = rel.parts
                quarantined = bool(parts and parts[0] == "_ExactDuplicates")

                if f.name.endswith(".wallpaper.json"):
                    if quarantined:
                        continue
                    lib_total_sidecars += 1
                    all_json.append(f)
                    if len(parts) >= 4:
                        bucket, orient, source = parts[0], parts[1], parts[2]
                        if bucket in RES_BUCKETS + ["_UnknownResolution"] and orient in ORIENTS:
                            source = source if source in SOURCES else "unknown"
                            lib_buckets[(bucket, orient, source)] += 1
                            source_totals[source] += 1
                            res_totals[bucket] += 1
                            orient_totals[orient] += 1
                    continue

                ext = f.suffix.lower()
                if ext not in IMG_EXT:
                    continue
                size = f.stat().st_size
                lib_total_images += 1
                lib_total_size += size
                if quarantined:
                    lib_quarantine_images += 1
                    lib_quarantine_size += size
                else:
                    lib_canonical_images += 1
                    lib_canonical_size += size
                    lib_by_ext[ext] += 1
            except (PermissionError, OSError, ValueError):
                continue
    except (PermissionError, OSError):
        pass

print(
    f"  canonical sidecars: {lib_total_sidecars}, "
    f"canonical images: {lib_canonical_images}, "
    f"quarantine images: {lib_quarantine_images}, "
    f"all images: {lib_total_images}, size: {fmt_bytes(lib_total_size)}"
)


# ===== Tag cloud =====
print("Sampling tag frequencies from sidecars...")
import random
tag_freq = Counter()
franchise_freq = Counter()
tag_sample = (
    all_json
    if len(all_json) <= 6000
    else random.Random(42).sample(all_json, 6000)
)
for f in tag_sample:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        for t in data.get("tags", []):
            n = t.get("name", "")
            if not n:
                continue
            tag_freq[n] += 1
            if t.get("type") == "franchise":
                franchise_freq[n] += 1
    except Exception:
        pass


# ===== Incoming buffer =====
print("Counting incoming buffer...")

def count_dir(p, recursive=True):
    if not p.is_dir():
        return {"files": 0, "exts": {}}
    exts = Counter()
    total = 0
    try:
        it = p.rglob("*") if recursive else p.iterdir()
        for f in it:
            try:
                if f.is_file():
                    exts[f.suffix.lower()] += 1
                    total += 1
            except (PermissionError, OSError):
                pass
    except (PermissionError, OSError):
        pass
    return {"files": total, "exts": dict(exts.most_common(5))}


buf_ap = count_dir(TEMP_AP)
buf_apf = count_dir(TEMP_APF)
buf_zc = count_dir(TEMP_ZC)
buf_wh = count_dir(TEMP_WH)
buf_gb = count_dir(TEMP_GB)

movedir_files = 0
movedir_samples = []
if MOVEDIR.is_dir():
    for f in MOVEDIR.iterdir():
        if f.is_file():
            movedir_files += 1
            if len(movedir_samples) < 6:
                try:
                    movedir_samples.append({"name": f.name, "url": f.read_text(encoding="utf-8").strip()})
                except Exception:
                    pass
print(f"  movedir files: {movedir_files}")

worker_log_stats = {"deferred": 0, "started": 0, "completed": 0, "failed": 0}
worker_log_tail = []
if WORKER_LOG.is_file():
    try:
        lines = WORKER_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
        worker_log_tail = lines[-15:]
        for line in lines[-2000:]:
            if "Deferred because" in line:
                worker_log_stats["deferred"] += 1
            elif "] Starting job" in line:
                worker_log_stats["started"] += 1
            elif "] Completed job" in line:
                worker_log_stats["completed"] += 1
            elif "failed" in line.lower() and "] Job" in line:
                worker_log_stats["failed"] += 1
    except Exception:
        pass


# ===== Preview gallery (most-recent files across buffer + library) =====

def _gallery_wxh_from_name(name):
    """Pull WxH out of a filename like wallhaven_01l27w_2433x3475.jpg.
    Returns (w, h) or (None, None)."""
    m = re.search(r"(?:^|[_-])(\d{2,5})x(\d{2,5})(?=\.)", name)
    if not m:
        return (None, None)
    try:
        return (int(m.group(1)), int(m.group(2)))
    except Exception:
        return (None, None)


def _gallery_wxh_from_file(path):
    """Stdlib-only fallback that reads WxH from a JPEG/PNG/WebP header.
    Returns (w, h) or (None, None). Cheap: reads at most 64 bytes for
    PNG, 32 KB for JPEG, 32 bytes for WebP."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        # PNG: 8-byte signature, then 8-byte IHDR length+type, then 4-byte width
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            w, h = struct.unpack(">II", head[16:24])
            return (w, h)
        # WebP: "RIFF....WEBP" then a chunk. Read more to find VP8 / VP8L / VP8X.
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            with open(path, "rb") as f:
                f.read(12)  # RIFF + size + WEBP
                while True:
                    chunk = f.read(8)
                    if len(chunk) < 8:
                        return (None, None)
                    ctype = chunk[4:8]
                    clen = struct.unpack("<I", chunk[0:4])[0]
                    if ctype in (b"VP8 ", b"VP8L", b"VP8X"):
                        data = f.read(clen)
                        if ctype == b"VP8 ":
                            # Skip 3 bytes, then 2-byte width, 2-byte height (little-endian)
                            w = struct.unpack("<H", data[6:8])[0] & 0x3FFF
                            h = struct.unpack("<H", data[8:10])[0] & 0x3FFF
                            return (w, h)
                        if ctype == b"VP8L":
                            # 1-byte signature + 4 bytes: w-1 (14 bits) | h-1 (14 bits) | ...
                            bits = struct.unpack("<I", data[1:5])[0]
                            w = (bits & 0x3FFF) + 1
                            h = ((bits >> 14) & 0x3FFF) + 1
                            return (w, h)
                        if ctype == b"VP8X":
                            w = 1 + (data[0] | (data[1] << 8) | (data[2] << 16))
                            h = 1 + (data[3] | (data[4] << 8) | (data[5] << 16))
                            return (w, h)
                    else:
                        f.seek(clen + (clen & 1), 1)  # chunks are word-padded
        # JPEG: scan markers until SOF0/SOF2 (0xC0/0xC2) for big-endian w,h
        if head.startswith(b"\xff\xd8"):
            with open(path, "rb") as f:
                if f.read(2) != b"\xff\xd8":
                    return (None, None)
                while True:
                    byte = f.read(1)
                    if not byte or byte != b"\xff":
                        return (None, None)
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3",
                                  b"\xc5", b"\xc6", b"\xc7", b"\xc9",
                                  b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"):
                        f.read(3)  # length (2) + precision (1)
                        h, w = struct.unpack(">HH", f.read(4))
                        return (w, h)
                    # Skip segment
                    seg_len_bytes = f.read(2)
                    if len(seg_len_bytes) < 2:
                        return (None, None)
                    seg_len = struct.unpack(">H", seg_len_bytes)[0]
                    f.seek(seg_len - 2, 1)
    except Exception:
        return (None, None)
    return (None, None)


def _gallery_source_from_path(rel):
    """Map a path's first component to a known source name."""
    p = rel.replace("\\", "/").lstrip("/")
    head = p.split("/", 1)[0] if "/" in p else p
    for s in ("wallhaven", "zerochan", "anime-pictures-full",
              "anime-pictures", "gelbooru"):
        if head == s or head.startswith(s + " "):
            return s
    return head or "unknown"


def _gallery_orient(w, h):
    if not w or not h: return "unknown"
    if abs(w - h) <= max(w, h) * 0.02: return "square"
    return "landscape" if w > h else "portrait"


def _gallery_res(w, h):
    """Same bucketing convention as the rest of the dashboard."""
    long_side = max(w or 0, h or 0)
    if long_side >= 3000: return "4K"
    if long_side >= 2400: return "1440p"
    if long_side >= 1900: return "1080p"
    if long_side >= 1200: return "720p"
    if long_side > 0:     return "SD"
    return "unknown"


def collect_preview_gallery(limit=160, per_source_cap=80, buffer_window_days=14, min_per_source=24):
    """Collect recent images from every temp_downloads source and the library.

    Selection is bounded and balanced by source so Queue Browser previews,
    Queue Browser originals, list-driven sources, manual intake, and sorted
    non-queue/non-list wallpapers all remain represented.

    Returns a list of dicts, newest-arrival first, capped at `limit`.
    Each entry: abs_path, rel_url, mtime, source, subdir, w, h, orient,
    res, ext, size_bytes, sidecar_present.
    """
    import os as _os
    cutoff = datetime.datetime.now().timestamp() - buffer_window_days * 86400
    items = []

    def _add_file(fp, source_hint=None):
        try:
            st = fp.stat()
        except OSError:
            return
        if not _os.path.isfile(fp):
            return
        ext = fp.suffix.lower()
        if ext not in GALLERY_EXT:
            return
        # Download tools may preserve the source server's old modified time.
        # On Windows, ctime is the local creation/arrival time, so use the
        # newer timestamp to keep freshly downloaded Queue Browser originals
        # visible and correctly ordered.
        arrival_time = max(st.st_mtime, st.st_ctime)
        if arrival_time < cutoff:
            return
        w, h = _gallery_wxh_from_name(fp.name)
        if w is None:
            # Stdlib-only header parse (PNG / JPEG / WebP). Cheap and
            # doesn't depend on PIL being installed.
            w, h = _gallery_wxh_from_file(str(fp))
        rel = str(fp)
        rel_lower = rel.lower().replace("\\", "/")
        # Compute URL path relative to reports/ dir
        # e.g. F:\Wallpapers\temp_downloads\wallhaven\red\foo.jpg
        #      -> ../temp_downloads/wallhaven/red/foo.jpg
        rel_url = None
        if rel_lower.startswith("f:/wallpapers/"):
            tail = rel[len("F:\\Wallpapers\\"):].replace("\\", "/")
            rel_url = "../" + tail
        source = source_hint or _gallery_source_from_path(fp.parts[-3] if len(fp.parts) >= 3 else fp.name)
        subdir = fp.parent.name if fp.parent else ""
        sidecar = fp.with_suffix(fp.suffix + ".json")
        sidecar_expected = source not in {"anime-pictures-full", "library", "manual-intake"}
        try:
            sidecar_present = sidecar.is_file() if sidecar_expected else True
        except OSError:
            sidecar_present = False
        items.append({
            "abs_path": str(fp),
            "rel_url": rel_url,
            "mtime": datetime.datetime.fromtimestamp(arrival_time, tz=datetime.timezone.utc).isoformat(),
            "source": source,
            "subdir": subdir,
            "w": w, "h": h,
            "orient": _gallery_orient(w, h),
            "res": _gallery_res(w, h),
            "ext": ext,
            "media_type": "video" if ext in {".webm", ".mp4"} else "image",
            "size_bytes": int(st.st_size),
            "sidecar_present": bool(sidecar_present),
            "sidecar_expected": bool(sidecar_expected),
        })

    # Discover every top-level temp_downloads folder dynamically so manual or
    # non-list intake is never omitted. Add a bounded canonical-library sample
    # as its own source for wallpapers that did not arrive through the active
    # queue/list folders.
    temp_root = ROOT / "temp_downloads"
    source_roots = []
    if temp_root.exists():
        try:
            source_roots.extend(
                (p, p.name) for p in sorted(temp_root.iterdir(), key=lambda p: p.name.lower())
                if p.is_dir()
            )
            for fp in temp_root.iterdir():
                if fp.is_file() and fp.suffix.lower() in GALLERY_EXT:
                    _add_file(fp, source_hint="manual-intake")
        except OSError:
            pass
    if LIBRARY.exists():
        source_roots.append((LIBRARY, "library"))

    # Keep only the newest bounded candidates from each discovered source.
    for src_dir, source_name in source_roots:
        candidates = []
        for root, dirs, files in _os.walk(src_dir):
            if source_name == "library":
                dirs[:] = [d for d in dirs if d not in {"_ExactDuplicates", "_metadata"}]
            for fn in files:
                fp = Path(root) / fn
                if fp.suffix.lower() not in GALLERY_EXT:
                    continue
                try:
                    st = fp.stat()
                except OSError:
                    continue
                arrival_time = max(st.st_mtime, st.st_ctime)
                if arrival_time < cutoff:
                    continue
                candidates.append((arrival_time, fp))
        candidates.sort(key=lambda t: t[0], reverse=True)
        for _mtime, fp in candidates[:per_source_cap]:
            _add_file(fp, source_hint=source_name)

    # Dedupe by path, then reserve a small newest slice for every source so
    # Queue Browser previews and downloaded originals cannot crowd each other
    # out. Fill the remaining capacity by arrival time across all sources.
    seen = set()
    uniq = []
    for it in sorted(items, key=lambda x: x["mtime"], reverse=True):
        if it["abs_path"] in seen:
            continue
        seen.add(it["abs_path"])
        uniq.append(it)

    by_source = defaultdict(list)
    for it in uniq:
        by_source[it["source"]].append(it)

    selected = []
    selected_paths = set()
    reserve = max(1, min(min_per_source, limit // max(1, len(by_source))))
    for source in sorted(by_source):
        for it in by_source[source][:reserve]:
            selected.append(it)
            selected_paths.add(it["abs_path"])

    for it in uniq:
        if len(selected) >= limit:
            break
        if it["abs_path"] in selected_paths:
            continue
        selected.append(it)
        selected_paths.add(it["abs_path"])

    return sorted(selected[:limit], key=lambda x: x["mtime"], reverse=True)


# ===== Build gallery + assemble snapshot =====

gallery_items = collect_preview_gallery(limit=160, per_source_cap=80, buffer_window_days=14, min_per_source=24)
print(f"Preview gallery: {len(gallery_items)} items")

# (gallery assignment is appended to `snapshot` below, after the dict literal)

snapshot = {
    "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "state_updated": state.get("updatedAt"),
    "last_worker_at": state.get("lastWorkerAt"),
    "last_message": state.get("lastMessage"),
    "counts": {
        "total": len(jobs),
        "completed": len(completed),
        "failed": len(failed),
        "pending": len(pending),
        "running": len(running),
        "removed": len(removed),
        "by_status": dict(by_status),
        "by_handler": dict(by_handler),
        "failed_by_handler": dict(Counter(j.get("handler") or "(none)" for j in failed)),
    },
    "library": {
        "sidecars": lib_total_sidecars,
        "images": lib_canonical_images,
        "size_bytes": lib_canonical_size,
        "all_images": lib_total_images,
        "all_size_bytes": lib_total_size,
        "quarantine_images": lib_quarantine_images,
        "quarantine_size_bytes": lib_quarantine_size,
        "by_ext": dict(lib_by_ext.most_common()),
        "by_source": dict(source_totals),
        "by_res": dict(res_totals),
        "by_orient": dict(orient_totals),
        "buckets": {f"{b}|{o}|{s}": n for (b, o, s), n in lib_buckets.items()},
    },
    "buffer": {
        "anime-pictures": buf_ap,
        "anime-pictures-full": buf_apf,
        "zerochan": buf_zc,
        "wallhaven": buf_wh,
        "gelbooru": buf_gb,
    },
    "movedir": {"count": movedir_files, "samples": movedir_samples},
    "daily": dict(sorted(daily.items())),
    "hourly": {WEEKDAYS[k]: dict(v) for k, v in hourly.items()},
    "durations": {"buckets": dict(dur_buckets), "stats": dur_stats},
    "tags": {
        "all": tag_freq.most_common(120),
        "franchise": franchise_freq.most_common(60),
        "sample_size": len(tag_sample),
        "population": len(all_json),
    },
    "worker_log": {"stats": worker_log_stats, "tail": worker_log_tail},
    "running": [
        {"id": j.get("id"), "url": j.get("url"), "startedAt": j.get("startedAt"),
         "attempts": j.get("attempts"), "lineNumber": j.get("lineNumber"),
         "handler": j.get("handler"), "effectiveCommand": j.get("effectiveCommand")}
        for j in running
    ],
    "failed": [
        {"id": j.get("id"), "url": j.get("url"), "finishedAt": j.get("finishedAt"),
         "attempts": j.get("attempts"), "exitCode": j.get("exitCode"),
         "handler": j.get("handler"), "lastError": j.get("lastError")}
        for j in sorted(failed, key=lambda x: x.get("finishedAt") or "", reverse=True)
    ],
    "recent_completed": [
        {"id": j.get("id"), "url": j.get("url"), "finishedAt": j.get("finishedAt"),
         "exitCode": j.get("exitCode"), "handler": j.get("handler"), "attempts": j.get("attempts"),
         "effectiveCommand": j.get("effectiveCommand")}
        for j in sorted(completed, key=lambda x: x.get("finishedAt") or "", reverse=True)[:30]
    ],
    "pending": [
        {"id": j.get("id"), "url": j.get("url"), "addedAt": j.get("addedAt"),
         "handler": j.get("handler"), "title": derive_title(j.get("url"))}
        for j in pending
    ],
}

# Attach the preview gallery block to the snapshot now that `snapshot` exists.
snapshot["preview_gallery"] = {
    "items": gallery_items,
    "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "buffer_window_days": 14,
    "per_source_cap": 120,
    "limit": 60,
    "by_source": dict(Counter(it["source"] for it in gallery_items)),
}

# Worker pause flag (read by the dashboard's Pause Worker button)
snapshot["worker_paused"] = (ROOT / ".wallpaper-download-queue" / "pause.flag").exists()



snap_path = ROOT / "reports" / "_snapshot.json"
snap_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
print(f"Snapshot written: {snap_path} ({snap_path.stat().st_size:,} bytes)")
print("Now run _render_dashboard.py to build the HTML.")
