# PlexArr Dedup 🎬♻️

A web-based duplicate media manager for **Plex + Sonarr/Radarr** users on Windows.  
Finds, previews, and safely removes duplicate movies and TV episodes — using Plex's own library metadata rather than fragile folder-name guessing.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-GPL%20v3-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

---

## Why does this exist?

Existing deduplication tools work on file hashes or folder names.  
That misses the actual problem: **two copies of the same film at different resolutions, in completely different folder structures, matched by IMDB/TMDB ID.**

PlexArr Dedup uses **Plex's built-in duplicate detection** (the same engine that powers the Duplicates filter in Plex Web) and cross-references results with **Sonarr and Radarr** to know which copy is the managed one — giving you resolution badges, codec info, and a one-click cleanup with full Recycle Bin support.

---

## Features

| | |
|---|---|
| 🎬 **Plex metadata matching** | Detects duplicates by IMDB/TMDB ID — catches copies in different folder structures |
| 📂 **Folder-name scan** | Fast fallback mode for libraries not in Plex |
| ✅ **Sonarr/Radarr cross-reference** | Identifies which copy is the officially managed one |
| 🏆 **Quality preference** | Keep best quality, target a specific resolution, or keep the smallest acceptable file |
| ♻️ **Recycle Bin support** | Move to Windows Recycle Bin instead of permanent deletion |
| 🔍 **Dry run + visual preview** | See exactly what will be removed before touching anything |
| 🛡️ **Protected directories** | Mark paths as never-delete, with optional child inheritance |
| 📁 **Orphan folder cleanup** | Automatically removes the parent folder when only companion files remain |
| 📂 **Click-to-open paths** | Click any file path in results to open it in Explorer |
| 📋 **Session logs** | Every cleanup run is logged to disk |

---

## Requirements

- **Windows 10/11**
- **Python 3.9+** — [python.org](https://www.python.org/downloads/)
- **Plex Media Server** (optional but recommended — enables metadata matching)
- **Sonarr** and/or **Radarr** (optional — enables official-path identification)

---

## Installation

```powershell
git clone https://github.com/shaszard/plexarr-dedup.git
cd plexarr-dedup

# Allow PowerShell to run local scripts (one-time)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Start the app (installs dependencies automatically)
.\start.ps1
```

The launcher installs all Python dependencies and opens **http://localhost:8181** in your browser automatically.

> **Run as Administrator** if you plan to delete files on network shares or drives with restricted permissions.

---

## Configuration

Open the **Configuration** tab in the web UI.

### Sonarr / Radarr
Enter your URL and API key for each service.  
API key: Settings → General → Security → API Key

### Plex
Enter your Plex URL (default `http://localhost:32400`) and your **X-Plex-Token**.  
[How to find your Plex token →](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

### Scan Directories
Add the root folders you want checked (e.g. `G:\Media\Movies`, `E:\Backup\TV`).  
Use the **📂 Browse…** button to pick folders from a Windows Explorer dialog.

### Protected Directories
Paths that will **never** be deleted, even if flagged as duplicates.  
- **Inherit checked** → subdirectories are also protected  
- **Inherit unchecked** → only the exact directory is protected (subdirs can still be cleaned)

---

## Scan Modes

### 🎬 Plex Metadata *(recommended)*
Uses Plex's library database to find items matched to the same IMDB/TMDB ID with multiple files.  
Returns resolution, codec, and bitrate for each copy.

**Options:**
- Filter by **Movies** and/or **TV Shows**
- Cross-reference with Sonarr and/or Radarr
- **Quality preference** — which copy to keep when there's no Sonarr/Radarr match:
  - 🏆 **Best quality** — highest resolution, then largest file
  - 🎯 **Target resolution** — prefer 4K / 1080p / 720p / 480p; nearest match if not exact
  - 📦 **Smallest acceptable** — smallest file still at or above 720p

### 📂 Folder Name Matching
Scans your directories for subdirectories with identical names.  
Faster but misses duplicates in different folder structures.

---

## Cleanup Modes

| Mode | What it does |
|---|---|
| 🔍 **Dry Run** | Simulates everything — nothing is moved or deleted |
| ♻️ **Recycle Bin** | Moves files to the Windows Recycle Bin — fully recoverable |
| ⚠️ **Permanent Delete** | Irreversible — double confirmation required |

After a file is removed, PlexArr Dedup checks whether the parent folder is now empty of significant content (no files > 10 MB). If so, the folder itself is also removed or recycled, cleaning up leftover subtitles, `.nfo` files, and poster images automatically.

---

## How the quality preference works

When Sonarr/Radarr identify a managed path, that always takes priority (regardless of quality preference — unless you choose **Target** or **Smallest**, in which case your preference wins even over the arr-managed copy, shown with an ⚠️ badge).

When there is no arr match, the quality preference determines which copy is kept.

---

## Troubleshooting

**"running scripts is disabled on this system"**  
Run once: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

**[WinError 5] Access is denied**  
Run `start.ps1` as Administrator (right-click → Run with PowerShell as Admin), or stop Plex/Radarr briefly if they have the file locked.

**Plex test fails**  
Check your token is correct and that Plex is running. The token can be found in any Plex API URL (open Plex Web, right-click a poster → Get Info → View XML — look for `X-Plex-Token` in the URL).

---

## Contributing

Pull requests welcome. Please keep the single-file architecture (`app.py` + `index.html`) — the goal is zero-dependency deployment beyond `pip install`.

---

## Licence

GNU General Public License v3 — see [LICENSE](LICENSE).  
Any fork or derivative **must remain open source** under the same licence.
