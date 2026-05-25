#!/usr/bin/env python3
"""
Media Server Duplicate Cleaner - Web Interface
FastAPI backend with real-time progress via Server-Sent Events
"""

import os
import sys
import stat
import json

try:
    import send2trash as _send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False
import uuid
import shutil
import logging
import threading
import subprocess
import requests
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict
from datetime import datetime
from queue import Queue, Empty

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import asyncio

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
PROTECTED_DIRS_FILE = BASE_DIR / "protected_dirs.json"
LOGS_DIR   = BASE_DIR / "logs"
STATIC_DIR = Path(__file__).parent / "static"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Media Deduplicator", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory job state  {job_id: {type, status, queue, result, error}}
jobs: Dict[str, dict] = {}

# ── Pydantic models ───────────────────────────────────────────────────────────
class ScanRequest(BaseModel):
    directories: List[str]
    use_sonarr: bool = True
    use_radarr: bool = True
    use_plex: bool = False        # Use Plex duplicate detection instead of folder-name scan
    scan_movies: bool = True      # Plex mode: include movie libraries
    scan_shows: bool = True       # Plex mode: include TV show libraries
    quality_pref: str = "best"    # "best" | "target" | "smallest"
    quality_target: str = "1080"  # resolution target when quality_pref = "target"

class CleanupRequest(BaseModel):
    scan_id: str
    dry_run: bool = True
    use_recycle_bin: bool = True   # move to OS recycle bin instead of permanent delete
    min_size_bytes: int = 0
    max_size_bytes: int = 9_223_372_036_854_775_807
    filter_pattern: str = ""
    selected_paths: Optional[List[str]] = None   # None = delete all non-official

class ProtectedDirEntry(BaseModel):
    path: str
    inherit: bool = True   # True = protect this dir AND all subdirectories

class ProtectedDirsPayload(BaseModel):
    protected_dirs: List[ProtectedDirEntry]

# ── Config helpers ────────────────────────────────────────────────────────────
DEFAULTS = {
    "sonarr": {"url": "http://localhost:8989", "api_key": ""},
    "radarr": {"url": "http://localhost:7878", "api_key": ""},
    "plex":   {"url": "http://localhost:32400", "token": ""},
    "scan_directories": []
}

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))   # deep copy of defaults
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        # Merge: saved values win, but defaults fill in any missing keys/sub-keys
        for key, default_val in DEFAULTS.items():
            if key not in saved:
                cfg[key] = default_val
            elif isinstance(default_val, dict):
                cfg[key] = {**default_val, **saved[key]}
            else:
                cfg[key] = saved[key]
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def load_protected() -> List[dict]:
    """
    Returns a list of dicts: {"path": str, "inherit": bool}.
    Handles both the old plain-string format (treated as inherit=True) and
    the new object format for backward compatibility.
    """
    if PROTECTED_DIRS_FILE.exists():
        with open(PROTECTED_DIRS_FILE) as f:
            raw = json.load(f).get("protected_dirs", [])
        result = []
        for item in raw:
            if isinstance(item, str):
                result.append({"path": item, "inherit": True})
            else:
                result.append({"path": item["path"], "inherit": item.get("inherit", True)})
        return result
    return []

def save_protected(entries: List[dict]):
    with open(PROTECTED_DIRS_FILE, "w") as f:
        json.dump({"protected_dirs": entries}, f, indent=2)

def is_path_protected(path: Path, protected: List[dict]) -> bool:
    """Check whether `path` is covered by any protected-dir entry."""
    for entry in protected:
        prot = Path(entry["path"])
        if path == prot:
            return True
        if entry.get("inherit", True) and is_subpath(path, prot):
            return True
    return False

# ── Utility ───────────────────────────────────────────────────────────────────
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".flv"}

def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def dir_size(path: str) -> int:
    total = 0
    try:
        for e in os.scandir(path):
            if e.is_file(follow_symlinks=False):
                try:
                    total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
            elif e.is_dir(follow_symlinks=False):
                total += dir_size(e.path)
    except (PermissionError, OSError):
        pass
    return total

def count_videos(path: str) -> int:
    count = 0
    try:
        for e in os.scandir(path):
            if e.is_file() and Path(e.name).suffix.lower() in VIDEO_EXTS:
                count += 1
            elif e.is_dir():
                count += count_videos(e.path)
    except (PermissionError, OSError):
        pass
    return count

def is_subpath(child: Path, parent: Path) -> bool:
    """Python 3.8-compatible is_relative_to."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False

# ── API fetchers ──────────────────────────────────────────────────────────────
def fetch_arr(url: str, api_key: str, endpoint: str) -> dict:
    """
    Returns {folder_name_lower: {title, path, source, id, monitored}}
    """
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/{endpoint}",
            headers={"X-Api-Key": api_key},
            timeout=10
        )
        if r.status_code != 200:
            return {}
        out = {}
        for item in r.json():
            path = item.get("path", "")
            if not path:
                continue
            folder = Path(path).name.lower()
            out[folder] = {
                "title":     item.get("title", ""),
                "path":      path,
                "source":    "Sonarr" if endpoint == "series" else "Radarr",
                "id":        item.get("id"),
                "monitored": item.get("monitored", True),
            }
        return out
    except Exception as e:
        logger.warning(f"Error fetching {url}/{endpoint}: {e}")
        return {}

# ── Plex helpers ─────────────────────────────────────────────────────────────

RESOLUTION_RANK = {"4k": 4, "2160": 4, "1080": 3, "720": 2, "480": 1, "sd": 0}

def plex_get(url: str, token: str, path: str, params: dict = None) -> dict:
    p = dict(params or {})
    p["X-Plex-Token"] = token
    r = requests.get(
        f"{url.rstrip('/')}{path}",
        params=p,
        headers={"Accept": "application/json"},
        timeout=20
    )
    r.raise_for_status()
    return r.json()

def plex_resolution_rank(res: str) -> int:
    return RESOLUTION_RANK.get((res or "").lower(), 0)

def fetch_plex_libraries(url: str, token: str) -> list:
    data = plex_get(url, token, "/library/sections")
    return [
        {"id": d["key"], "title": d["title"], "type": d["type"]}
        for d in data["MediaContainer"].get("Directory", [])
        if d["type"] in ("movie", "show")
    ]

def build_plex_file_entry(media: dict, part: dict, official_paths: dict,
                           protected: List[dict]) -> dict:
    """Turn a Plex Media/Part pair into a standardised path-detail dict."""
    fpath = part.get("file", "")
    pp    = Path(fpath)
    size  = int(part.get("size") or 0)
    if not size and pp.exists():
        try: size = pp.stat().st_size
        except OSError: pass

    is_prot = is_path_protected(pp, protected)

    # Check Sonarr/Radarr official match by parent-folder name
    folder   = pp.parent.name.lower()
    is_off   = False
    off_info = None
    if folder in official_paths:
        off_info = official_paths[folder]
        norm_p   = str(pp.parent).lower().rstrip("/\\")
        norm_off = str(Path(off_info["path"])).lower().rstrip("/\\")
        is_off   = (norm_p == norm_off or norm_p.endswith(norm_off)
                    or norm_off.endswith(norm_p))

    return {
        "path":        fpath,
        "size":        size,
        "size_human":  format_bytes(size),
        "video_count": 1,
        "is_official": is_off,
        "is_protected": is_prot,
        "exists":      pp.exists(),
        "official_info": off_info,
        # Plex quality metadata
        "resolution":   media.get("videoResolution", ""),
        "width":        media.get("width", 0),
        "height":       media.get("height", 0),
        "bitrate":      media.get("bitrate", 0),
        "video_codec":  media.get("videoCodec", ""),
        "audio_codec":  media.get("audioCodec", ""),
        "res_rank":     plex_resolution_rank(media.get("videoResolution", "")),
    }

def _pick_best(files: list, quality_pref: str, quality_target: str) -> dict:
    """
    Choose which copy to keep (inferred official) based on the user's
    quality preference.  Only called when Sonarr/Radarr have no match.

    quality_pref:
      "best"     – highest resolution, then largest file (default)
      "target"   – prefer the target resolution; nearest match if not exact
      "smallest" – smallest file that is still ≥ 720p; fall back to best
    """
    if quality_pref == "target":
        target_rank = RESOLUTION_RANK.get(quality_target.lower(), 3)
        at_target = [f for f in files if f["res_rank"] == target_rank]
        if at_target:
            return max(at_target, key=lambda f: f["size"])
        # Nothing at exact target — take closest (prefer stepping up, then down)
        above = sorted([f for f in files if f["res_rank"] > target_rank],
                       key=lambda f: f["res_rank"])
        below = sorted([f for f in files if f["res_rank"] < target_rank],
                       key=lambda f: -f["res_rank"])
        candidates = above or below or files
        return candidates[0]

    if quality_pref == "smallest":
        min_rank = RESOLUTION_RANK.get("720", 2)
        qualifying = [f for f in files if f["res_rank"] >= min_rank]
        pool = qualifying if qualifying else files
        return min(pool, key=lambda f: f["size"])

    # "best" (default)
    return max(files, key=lambda f: (f["res_rank"], f["size"]))


def fetch_plex_duplicates(url: str, token: str, official_paths: dict,
                           emit, scan_movies: bool = True,
                           scan_shows: bool = True,
                           quality_pref: str = "best",
                           quality_target: str = "1080") -> list:
    """
    Call Plex's duplicate filter for every library section.
    Returns groups in the same shape as the folder-name scan so the
    rest of the pipeline (cleanup, results view) is unchanged.
    """
    protected = load_protected()
    libraries = fetch_plex_libraries(url, token)
    emit(f"📚 Plex: {len(libraries)} libraries found")

    results = []

    for lib in libraries:
        # Apply movies / TV filter
        if lib["type"] == "movie" and not scan_movies:
            emit(f"⏭️  Skipping movie library: {lib['title']}")
            continue
        if lib["type"] == "show" and not scan_shows:
            emit(f"⏭️  Skipping TV library: {lib['title']}")
            continue

        # Plex type values: 1=movie, 4=episode
        item_type = "1" if lib["type"] == "movie" else "4"
        emit(f"🔍 Plex library: {lib['title']} ({lib['type']})")

        try:
            data = plex_get(url, token,
                            f"/library/sections/{lib['id']}/all",
                            {"duplicate": "1", "type": item_type})
        except Exception as e:
            emit(f"⚠️  Could not query {lib['title']}: {e}")
            continue

        items = data.get("MediaContainer", {}).get("Metadata", [])
        emit(f"   {len(items)} duplicate item(s) in {lib['title']}")

        for item in items:
            media_list = item.get("Media", [])
            if len(media_list) < 2:
                continue

            # Build one entry per file (a Media block can have multiple Parts
            # for e.g. multi-part movies, but usually just one)
            files = []
            for media in media_list:
                for part in media.get("Part", []):
                    if not part.get("file"):
                        continue
                    files.append(build_plex_file_entry(
                        media, part, official_paths, protected))

            if not files:
                continue

            # Snapshot which files arr considers official (before we override)
            for f in files:
                f["arr_official"] = f["is_official"]

            if quality_pref == "best":
                # Default behaviour: Sonarr/Radarr wins; fall back to quality
                # only when there is no arr match at all.
                if not any(f["arr_official"] for f in files):
                    best = _pick_best(files, quality_pref, quality_target)
                    best["is_official"]       = True
                    best["inferred_official"] = True
                # else: is_official already correct from arr match — leave it

            else:
                # Quality preference overrides arr: pick the best file by
                # quality, then re-stamp is_official across all files.
                best = _pick_best(files, quality_pref, quality_target)
                for f in files:
                    f["is_official"] = (f is best)

                if best["arr_official"]:
                    # Quality preference agrees with arr — normal official
                    best["inferred_official"] = False
                else:
                    # Quality preference picked something different from arr
                    best["inferred_official"] = True
                    # Tag any file that arr managed but quality is replacing
                    for f in files:
                        if f["arr_official"] and not f["is_official"]:
                            f["arr_override"] = True   # shown as warning in UI

            dup_size = sum(f["size"] for f in files if not f["is_official"])

            # Build a human-readable group name
            title = item.get("title", "Unknown")
            year  = item.get("year", "")
            if lib["type"] == "show":
                # For episodes include show name + S##E##
                show  = item.get("grandparentTitle", "")
                s_num = item.get("parentIndex", "")
                e_num = item.get("index", "")
                label = f"{show} — S{s_num:02}E{e_num:02} — {title}" if s_num else f"{show} — {title}"
            else:
                label = f"{title} ({year})" if year else title

            results.append({
                "folder_name":               label,
                "plex_title":                title,
                "plex_year":                 year,
                "plex_id":                   item.get("ratingKey"),
                "library_name":              lib["title"],
                "library_type":              lib["type"],
                "scan_mode":                 "plex",
                "official_info":             next(
                    (f["official_info"] for f in files
                     if f["is_official"] and f.get("official_info")), None),
                "paths":                     files,
                "total_duplicate_size":      dup_size,
                "total_duplicate_size_human": format_bytes(dup_size),
            })

    return results


# ── Background tasks ──────────────────────────────────────────────────────────
def run_scan(job_id: str, req: ScanRequest):
    job = jobs[job_id]
    q: Queue = job["queue"]

    def emit(msg: str):
        q.put(msg)

    try:
        job["status"] = "running"
        cfg = load_config()
        protected = load_protected()

        # 1. Fetch official paths
        official: dict = {}   # folder_name_lower -> info dict

        if req.use_sonarr and cfg.get("sonarr", {}).get("api_key"):
            emit("📡 Connecting to Sonarr…")
            s = fetch_arr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"], "series")
            official.update(s)
            emit(f"✅ Sonarr: {len(s)} series found")
        else:
            emit("⏭️  Sonarr skipped (no API key)")

        if req.use_radarr and cfg.get("radarr", {}).get("api_key"):
            emit("📡 Connecting to Radarr…")
            r = fetch_arr(cfg["radarr"]["url"], cfg["radarr"]["api_key"], "movie")
            official.update(r)
            emit(f"✅ Radarr: {len(r)} movies found")
        else:
            emit("⏭️  Radarr skipped (no API key)")

        # 2a. PLEX MODE — use Plex's built-in duplicate detection
        if req.use_plex:
            plex_cfg = cfg.get("plex", {})
            if not plex_cfg.get("token"):
                emit("❌ Plex token not configured — add it in Configuration")
                job["status"] = "error"
                job["error"]  = "Plex token missing"
                emit("__DONE__")
                return
            emit(f"📡 Connecting to Plex at {plex_cfg['url']}…")
            results = fetch_plex_duplicates(
                plex_cfg["url"], plex_cfg["token"], official, emit,
                scan_movies=req.scan_movies,
                scan_shows=req.scan_shows,
                quality_pref=req.quality_pref,
                quality_target=req.quality_target)
            total_savings = sum(r["total_duplicate_size"] for r in results)
            job["result"] = results
            job["status"] = "complete"
            emit(f"✅ Plex scan complete — {len(results)} duplicate groups, "
                 f"{format_bytes(total_savings)} recoverable")
            emit("__DONE__")
            return

        # 2b. FOLDER-NAME MODE — scan directories for same-named subfolders
        folder_map: dict = defaultdict(list)   # folder_name -> [full_paths]

        for directory in req.directories:
            dp = Path(directory)
            if not dp.exists():
                emit(f"⚠️  Not found: {directory}")
                continue
            emit(f"📂 Scanning: {directory}")
            try:
                for item in dp.iterdir():
                    if item.is_dir():
                        folder_map[item.name].append(str(item))
            except PermissionError:
                emit(f"⚠️  Permission denied: {directory}")

        duplicates = {n: p for n, p in folder_map.items() if len(p) > 1}
        emit(f"📊 {len(duplicates)} duplicate folder names across {len(req.directories)} directories")

        # 3. Build result groups (folder-name mode)
        results = []
        for folder_name, paths in sorted(duplicates.items(), key=lambda x: x[0].lower()):
            official_info = official.get(folder_name.lower())

            path_details = []
            for p in paths:
                pp = Path(p)
                is_prot = is_path_protected(pp, protected)
                size    = dir_size(p)
                videos  = count_videos(p)

                # Decide if this IS the official path
                is_off = False
                if official_info:
                    norm_p   = str(pp).lower().rstrip("/\\")
                    norm_off = str(Path(official_info["path"])).lower().rstrip("/\\")
                    is_off   = norm_p == norm_off or norm_p.endswith(norm_off) or norm_off.endswith(norm_p)

                path_details.append({
                    "path":        p,
                    "size":        size,
                    "size_human":  format_bytes(size),
                    "video_count": videos,
                    "is_official": is_off,
                    "is_protected": is_prot,
                    "exists":      pp.exists(),
                    "scan_mode":   "folder",
                })

            dup_size = sum(pd["size"] for pd in path_details if not pd["is_official"])
            results.append({
                "folder_name":          folder_name,
                "official_info":        official_info,
                "scan_mode":            "folder",
                "paths":                path_details,
                "total_duplicate_size": dup_size,
                "total_duplicate_size_human": format_bytes(dup_size),
            })

        total_savings = sum(r["total_duplicate_size"] for r in results)
        job["result"] = results
        job["status"] = "complete"
        emit(f"✅ Scan complete — {len(results)} duplicate groups, {format_bytes(total_savings)} recoverable")
        emit("__DONE__")

    except Exception as e:
        logger.exception("Scan error")
        job["status"] = "error"
        job["error"] = str(e)
        emit(f"❌ Error: {e}")
        emit("__DONE__")


ORPHAN_THRESHOLD = 10 * 1024 * 1024   # 10 MB — files smaller than this are "companion" files


def _remove_orphan_dir(file_path: Path, scan_dirs: set, protected: List[dict],
                        dry_run: bool, emit, log_lines: list,
                        use_recycle_bin: bool = False) -> None:
    """
    After deleting `file_path`, check whether its parent directory has become
    an orphan (no remaining files > ORPHAN_THRESHOLD).  If so, remove the
    whole directory (cleans up subtitles, .nfo, posters, etc.).

    Skipped if the parent is a scan-root or a protected directory.
    In dry-run mode only previews; never touches disk.
    """
    parent = file_path.parent

    # Safety: never delete scan roots or protected directories
    if str(parent) in scan_dirs:
        return
    if is_path_protected(parent, protected):
        return
    if not parent.exists():
        return   # already gone (e.g. rmtree already handled it)

    try:
        if dry_run:
            # In preview mode the file hasn't been deleted yet, so exclude it
            # from the "significant remaining" check
            has_other = any(
                item.is_file()
                and item.resolve() != file_path.resolve()
                and item.stat().st_size > ORPHAN_THRESHOLD
                for item in parent.rglob("*")
            )
        else:
            has_other = any(
                item.is_file() and item.stat().st_size > ORPHAN_THRESHOLD
                for item in parent.rglob("*")
            )
    except OSError:
        return   # can't inspect — leave well alone

    if has_other:
        return   # still has meaningful content

    verb = "recycle" if use_recycle_bin else "remove"
    if dry_run:
        emit(f"   📁 Would also {verb} orphaned folder: {parent.name}/")
        log_lines.append(f"  [DRY RUN] Would {verb} orphaned folder: {parent}")
    else:
        try:
            if use_recycle_bin and HAS_SEND2TRASH:
                _send2trash.send2trash(str(parent))
                emit(f"   ♻️  Recycled orphaned folder: {parent.name}/")
                log_lines.append(f"  Recycled orphaned folder: {parent}")
            else:
                # Strip read-only flags from remaining small files before rmtree
                for root, _, files_in in os.walk(str(parent)):
                    for fn in files_in:
                        try:
                            fp = Path(root) / fn
                            fp.chmod(fp.stat().st_mode | stat.S_IWRITE)
                        except OSError:
                            pass
                shutil.rmtree(str(parent))
                emit(f"   📁 Removed orphaned folder: {parent.name}/")
                log_lines.append(f"  Removed orphaned folder: {parent}")
        except Exception as exc:
            emit(f"   ⚠️  Could not {verb} folder {parent.name}/: {exc}")
            log_lines.append(f"  Could not {verb} folder {parent}: {exc}")


def run_cleanup(job_id: str, req: CleanupRequest):
    job = jobs[job_id]
    q: Queue = job["queue"]

    def emit(msg: str):
        q.put(msg)

    try:
        job["status"] = "running"
        if req.dry_run:
            mode = "DRY RUN"
        elif req.use_recycle_bin:
            mode = "RECYCLE BIN"
        else:
            mode = "PERMANENT DELETE"
        emit(f"🧹 Starting cleanup [{mode}]…")

        if req.scan_id not in jobs:
            raise ValueError(f"Scan ID {req.scan_id} not found")
        scan_job = jobs[req.scan_id]
        if scan_job["status"] != "complete":
            raise ValueError("Scan not complete yet")

        scan_results: list = scan_job["result"]
        protected = load_protected()
        cfg = load_config()
        scan_dirs = {str(Path(d)) for d in cfg.get("scan_directories", [])}

        stats = dict(processed=0, deleted=0, skipped=0, errors=0, space_saved=0)
        log_lines = [
            "=== Media Duplicate Cleanup ===",
            f"Date: {datetime.now().isoformat()}",
            f"Mode: {mode}",
            f"Filter: {req.filter_pattern or 'None'}",
            f"Min size: {format_bytes(req.min_size_bytes)}",
            "",
        ]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path  = LOGS_DIR / f"cleanup_{timestamp}.log"

        for group in scan_results:
            fname = group["folder_name"]

            # Filter
            if req.filter_pattern:
                pat = req.filter_pattern.lower()
                if pat not in fname.lower() and not any(pat in pd["path"].lower() for pd in group["paths"]):
                    continue

            to_delete = []
            for pd in group["paths"]:
                if pd["is_official"] or not pd["exists"]:
                    continue
                if pd["is_protected"]:
                    emit(f"🛡️  Protected, skipping: {pd['path']}")
                    stats["skipped"] += 1
                    continue
                if pd["size"] < req.min_size_bytes or pd["size"] > req.max_size_bytes:
                    stats["skipped"] += 1
                    continue
                if req.selected_paths is not None and pd["path"] not in req.selected_paths:
                    continue
                to_delete.append(pd)

            if not to_delete:
                continue

            official_list = [pd for pd in group["paths"] if pd["is_official"]]
            official_label = official_list[0]["path"] if official_list else "(unknown)"

            emit(f"\n📁 {fname}")
            emit(f"   ✅ Keeping : {official_label}")
            log_lines.append(f"\nFolder: {fname}")
            log_lines.append(f"  Keeping: {official_label}")

            for pd in to_delete:
                stats["processed"] += 1
                path = pd["path"]
                size = pd["size"]

                p = Path(path)

                if req.dry_run:
                    emit(f"   🔍 Would {'recycle' if req.use_recycle_bin else 'delete'}: {path}  ({pd['size_human']})")
                    log_lines.append(f"  [DRY RUN] Would {'recycle' if req.use_recycle_bin else 'delete'}: {path} ({pd['size_human']})")
                    stats["deleted"] += 1
                    stats["space_saved"] += size
                    if p.is_file():
                        _remove_orphan_dir(p, scan_dirs, protected, True, emit, log_lines,
                                           use_recycle_bin=req.use_recycle_bin)

                elif req.use_recycle_bin and HAS_SEND2TRASH:
                    # ── Recycle Bin ──────────────────────────────────────
                    try:
                        _send2trash.send2trash(str(p))
                        emit(f"   ♻️  Recycled: {path}  ({pd['size_human']})")
                        log_lines.append(f"  Recycled: {path} ({pd['size_human']})")
                        stats["deleted"] += 1
                        stats["space_saved"] += size
                        if p.is_file():
                            _remove_orphan_dir(p, scan_dirs, protected, False, emit, log_lines,
                                               use_recycle_bin=True)
                    except Exception as e:
                        stats["errors"] += 1
                        emit(f"   ❌ Recycle failed: {path}: {e}")
                        log_lines.append(f"  ERROR recycling {path}: {e}")

                else:
                    # ── Permanent delete ─────────────────────────────────
                    try:
                        if p.is_dir():
                            for root, dirs, files_in in os.walk(path):
                                for fn in files_in:
                                    try:
                                        fp = Path(root) / fn
                                        fp.chmod(fp.stat().st_mode | stat.S_IWRITE)
                                    except OSError:
                                        pass
                            shutil.rmtree(path)
                        else:
                            try:
                                p.chmod(p.stat().st_mode | stat.S_IWRITE)
                            except OSError:
                                pass
                            p.unlink()
                            _remove_orphan_dir(p, scan_dirs, protected, False, emit, log_lines,
                                               use_recycle_bin=False)
                        emit(f"   🗑️  Deleted: {path}  ({pd['size_human']})")
                        log_lines.append(f"  Deleted: {path} ({pd['size_human']})")
                        stats["deleted"] += 1
                        stats["space_saved"] += size
                    except PermissionError as e:
                        stats["errors"] += 1
                        err_code = getattr(e, 'winerror', None)
                        if err_code == 5:
                            hint = " (file is locked — Plex/Radarr/antivirus may have it open)"
                        elif err_code == 32:
                            hint = " (file is in use by another process)"
                        else:
                            hint = " (permission denied — try running as administrator)"
                        emit(f"   ❌ Cannot delete: {path}{hint}")
                        log_lines.append(f"  ERROR deleting {path}: {e}{hint}")
                    except Exception as e:
                        stats["errors"] += 1
                        emit(f"   ❌ Error deleting {path}: {e}")
                        log_lines.append(f"  ERROR deleting {path}: {e}")

        log_lines += [
            "",
            "=== Summary ===",
            f"Processed : {stats['processed']}",
            f"Deleted   : {stats['deleted']}",
            f"Skipped   : {stats['skipped']}",
            f"Errors    : {stats['errors']}",
            f"Space {'freed' if not req.dry_run else 'recoverable'}: {format_bytes(stats['space_saved'])}",
        ]
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))

        job["result"] = stats
        job["status"] = "complete"

        emit(f"\n📊 Done!  Processed: {stats['processed']}  |  "
             f"{'Would free' if req.dry_run else 'Freed'}: {format_bytes(stats['space_saved'])}")
        emit(f"📝 Log saved → {log_path}")
        emit("__DONE__")

    except Exception as e:
        logger.exception("Cleanup error")
        job["status"] = "error"
        job["error"] = str(e)
        emit(f"❌ Error: {e}")
        emit("__DONE__")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")

# Config
@app.get("/api/config")
async def get_config():
    return load_config()

@app.put("/api/config")
async def put_config(body: dict):
    save_config(body)
    return {"status": "saved"}

# Protected dirs
@app.get("/api/protected-dirs")
async def get_protected():
    return {"protected_dirs": load_protected()}

@app.put("/api/protected-dirs")
async def put_protected(body: ProtectedDirsPayload):
    save_protected([e.model_dump() for e in body.protected_dirs])
    return {"status": "saved"}

# Connection test
@app.get("/api/test/{service}")
async def test_service(service: str):
    if service not in ("sonarr", "radarr", "plex"):
        raise HTTPException(400, "Invalid service")

    cfg = load_config()

    # Plex test
    if service == "plex":
        pc = cfg.get("plex", {})
        try:
            data = plex_get(pc.get("url",""), pc.get("token",""), "/library/sections")
            libs = data["MediaContainer"].get("Directory", [])
            names = ", ".join(l["title"] for l in libs[:5])
            return {"ok": True, "message": f"Connected — {len(libs)} libraries: {names}"}
        except requests.ConnectionError:
            return {"ok": False, "message": "Connection refused — is Plex running?"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    if service not in ("sonarr", "radarr"):
        raise HTTPException(400, "Invalid service")
    cfg = load_config().get(service, {})
    endpoint = "series" if service == "sonarr" else "movie"
    try:
        r = requests.get(
            f"{cfg['url'].rstrip('/')}/api/v3/{endpoint}",
            headers={"X-Api-Key": cfg.get("api_key", "")},
            timeout=5
        )
        if r.status_code == 200:
            n = len(r.json())
            return {"ok": True, "message": f"Connected — {n} {'series' if service=='sonarr' else 'movies'} found"}
        return {"ok": False, "message": f"HTTP {r.status_code}"}
    except requests.ConnectionError:
        return {"ok": False, "message": "Connection refused — is the service running?"}
    except Exception as e:
        return {"ok": False, "message": str(e)}

# Jobs: start scan
@app.post("/api/scan/start")
async def start_scan(req: ScanRequest):
    jid = str(uuid.uuid4())
    jobs[jid] = {"type": "scan", "status": "starting", "queue": Queue(), "result": None, "error": None}
    threading.Thread(target=run_scan, args=(jid, req), daemon=True).start()
    return {"job_id": jid}

# Jobs: start cleanup
@app.post("/api/cleanup/start")
async def start_cleanup(req: CleanupRequest):
    if req.scan_id not in jobs:
        raise HTTPException(404, "Scan not found")
    jid = str(uuid.uuid4())
    jobs[jid] = {"type": "cleanup", "status": "starting", "queue": Queue(), "result": None, "error": None}
    threading.Thread(target=run_cleanup, args=(jid, req), daemon=True).start()
    return {"job_id": jid}

# SSE stream for any job
@app.get("/api/job/{job_id}/events")
async def job_events(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    job = jobs[job_id]
    q: Queue = job["queue"]

    async def generate():
        while True:
            try:
                msg = q.get_nowait()
                yield f"data: {json.dumps({'msg': msg})}\n\n"
                if msg == "__DONE__":
                    break
            except Empty:
                if job["status"] in ("complete", "error"):
                    break
                yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# Job result
@app.get("/api/job/{job_id}/result")
async def job_result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    j = jobs[job_id]
    return {"status": j["status"], "result": j["result"], "error": j.get("error")}

# Folder picker — opens a native Windows folder browser dialog on the server machine
@app.get("/api/pick-folder")
async def pick_folder():
    ps_script = """
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.TopMost = $true
$form.WindowState = 'Minimized'
$form.ShowInTaskbar = $false
$form.Show()
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = 'Select a directory'
$dlg.RootFolder = [System.Environment+SpecialFolder]::MyComputer
$dlg.ShowNewFolderButton = $true
$result = $dlg.ShowDialog($form)
$form.Hide()
$form.Dispose()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.SelectedPath }
"""
    try:
        r = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, text=True, timeout=120
        )
        path = r.stdout.strip()
        return {"path": path if path else None}
    except Exception as e:
        return {"path": None, "error": str(e)}

# Open path in Windows Explorer
@app.get("/api/open-path")
async def open_path(path: str):
    """Open Windows Explorer with the file or folder highlighted/selected.

    NOTE: do NOT use shell=True — cmd.exe misinterprets parentheses like (2011)
    in folder names and falls back to the drive root.  Pass args as a list so
    Python calls CreateProcess directly and lets explorer.exe parse them.
    """
    try:
        p = Path(path)
        if p.exists():
            if p.is_file():
                # /select, highlights the file inside its parent folder
                subprocess.Popen(['explorer', '/select,' + str(p)])
            else:
                # Directory — open it directly
                subprocess.Popen(['explorer', str(p)])
        else:
            # File/folder missing — open the nearest existing ancestor
            target = next((anc for anc in p.parents if anc.exists()), None)
            if target:
                subprocess.Popen(['explorer', str(target)])
            else:
                return {"ok": False, "error": "Path not found"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Logs
@app.get("/api/logs")
async def list_logs():
    out = []
    for f in sorted(LOGS_DIR.iterdir(), reverse=True):
        if f.suffix == ".log":
            stat = f.stat()
            out.append({"name": f.name, "size_human": format_bytes(stat.st_size), "mtime": stat.st_mtime})
    return out

@app.get("/api/logs/{filename}")
async def get_log(filename: str):
    p = LOGS_DIR / filename
    if not p.exists() or p.parent != LOGS_DIR:
        raise HTTPException(404)
    return FileResponse(str(p), media_type="text/plain")

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser, io, sys
    # Force UTF-8 output so emoji in logs don't crash on Windows cp1252 terminals
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    port = int(os.environ.get("PORT", 8181))
    print(f"\n{'='*55}")
    print(f"  Media Deduplicator  --  http://localhost:{port}")
    print(f"{'='*55}\n")
    webbrowser.open(f"http://localhost:{port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
