#!/usr/bin/env python3
"""
launcher.py - a Steam-style library for your own projects.

    Double-click "Start Launcher.bat"   (or: python launcher.py)

"Scan folder..."  -> point it at the folder where your repos live. Every
                     subfolder becomes an entry with an auto-detected launch
                     command (run*.bat, .exe, main.py, .ps1, npm start,
                     cargo run, ...) plus extra commands like tests and builds.
"Add project"     -> any folder + any command, Python or not.

Per project: Play/Stop, extra commands, one-click setup (venv / npm install),
playtime, run logs, git status, cover art, favorites, notes.
List or grid view. Ctrl+K quick launch, F5 refresh git status.

Stdlib only (tkinter). Your library is saved next to this file in
launcher_config.json, so keep the two together.
"""

import base64
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zlib
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "launcher_config.json"
LOG_DIR = APP_DIR / "logs"
IS_WIN = sys.platform.startswith("win")
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}

# ---- theme (Steam-ish blues) ---------------------------------------------
BG, PANEL, PANEL2 = "#171d25", "#1f2a37", "#2f4a68"
FG, DIM, ACCENT = "#c7d5e0", "#8f98a0", "#66c0f4"
PLAY, PLAY_HOT = "#4c8b2f", "#6ab04c"
STOP, STOP_HOT = "#a33a3a", "#c44c4c"
FONT = ("Segoe UI", 10)
MONO = ("Consolas", 9)
TILE_W, TILE_H, ART = 140, 172, 96  # grid view tile size and cover size
TILE_COLORS = ["#3a6ea5", "#7a4fa3", "#a5523a", "#3a8f6e", "#a58a3a", "#3a8fa5", "#a53a6b", "#5a6b7a"]

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", "env",
             ".idea", ".vscode", "dist", "build"}
MAX_DEPTH = 3  # projects can sit at most this many folder levels below the scanned folder

# (glob patterns tried in order, command template, windows-only?)
LAUNCH_RULES = [
    (["run*.bat", "start*.bat", "launch*.bat", "play*.bat",
      "run*.cmd", "start*.cmd", "launch*.cmd", "play*.cmd"], "call {f}", True),
    (["*.exe"], "{f}", True),
    (["main.py", "app.py", "game.py", "run.py", "start.py", "__main__.py"],
     "{py} {f}", False),
    (["*.ps1"], "powershell -ExecutionPolicy Bypass -File {f}", True),
    (["*.sh"], "bash {f}", False),
    (["package.json"], "npm start", False),
    (["Cargo.toml"], "cargo run", False),
    (["go.mod"], "go run .", False),
]
# Tried only after everything above *and* the "<folder>.py / lone .py" checks,
# so a stray setup.bat or build.bat doesn't beat the project's real entry point.
LAST_RESORT_RULES = [
    (["*.bat", "*.cmd"], "call {f}", True),
]
# .exe files that are almost never the thing you want to "play"
SKIP_EXE = re.compile(r"^(unins\d*|uninstall|setup|install|.*installer|vc_?redist)", re.I)

# characters that make cmd.exe or bash misread an unquoted file name
SHELL_SPECIAL = set(" &()[]{}^=;!'+,`~%$")

# where cover art is looked for (png/gif only: that's what tkinter can show)
COVER_NAMES = ("cover", "icon", "logo", "banner", "screenshot", "preview")
COVER_DIRS = ("", "assets", "images", "img", "media", "docs", "res", "resources", ".github")


# ---- helpers ---------------------------------------------------------------
def _q(name: str) -> str:
    return f'"{name}"' if any(c in SHELL_SPECIAL for c in name) else name


def norm_key(path) -> str:
    """Comparable form of a path, for spotting duplicates."""
    return os.path.normcase(os.path.normpath(str(path)))


def is_repo(folder: Path) -> bool:
    # .git is a *file* in submodules and worktrees, so don't insist on a folder
    return (folder / ".git").exists()


def has_user_data(p: dict) -> bool:
    return bool(p.get("notes") or p.get("tags") or p.get("runs") or p.get("favorite"))


def git_env() -> dict:
    # fail fast instead of waiting on a password prompt nobody can see
    return dict(os.environ, GIT_TERMINAL_PROMPT="0")


def find_python(folder: Path) -> str:
    """Prefer a project-local venv, otherwise whatever 'python' is on PATH."""
    for v in ("venv", ".venv", "env"):
        exe = folder / v / ("Scripts/python.exe" if IS_WIN else "bin/python")
        if exe.exists():
            return f'"{exe}"'
    return "python" if IS_WIN else "python3"


def package_scripts(folder: Path) -> dict:
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        s = data.get("scripts") if isinstance(data, dict) else None
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _first_match(folder: Path, rules):
    for patterns, template, win_only in rules:
        if win_only and not IS_WIN:
            continue
        for pat in patterns:
            hits = sorted(p for p in folder.glob(pat) if p.is_file()
                          and not (pat == "*.exe" and SKIP_EXE.match(p.name)))
            if hits:
                return template.format(f=_q(hits[0].name), py=find_python(folder))
    return ""


def detect_command(folder: Path) -> str:
    """Best guess at how to run whatever lives in `folder`. '' if no idea."""
    cmd = _first_match(folder, LAUNCH_RULES)
    if cmd == "npm start":
        scripts = package_scripts(folder)
        if "start" not in scripts and "dev" in scripts:
            return "npm run dev"
    if cmd:
        return cmd
    same = folder / f"{folder.name}.py"
    if same.is_file():
        return f"{find_python(folder)} {_q(same.name)}"
    pys = [p for p in folder.glob("*.py") if p.is_file()]
    if len(pys) == 1:
        return f"{find_python(folder)} {_q(pys[0].name)}"
    return _first_match(folder, LAST_RESORT_RULES)


def detect_options(folder: Path, main: str = "") -> list:
    """Extra named commands (tests, builds, dev servers) that show up under the
    arrow next to Play. Skips anything identical to the main command."""
    opts = []

    def add(name, cmd):
        if cmd != main and all(o["command"] != cmd for o in opts):
            opts.append({"name": name, "command": cmd})

    py = find_python(folder)
    has_py = any(p.is_file() for p in folder.glob("*.py")) or (folder / "pyproject.toml").is_file()
    if has_py and ((folder / "tests").is_dir() or (folder / "test").is_dir()
                   or any(folder.glob("test_*.py"))):
        hints = ""
        for f in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.cfg"):
            try:
                hints += (folder / f).read_text(errors="ignore")
            except OSError:
                pass
        use_pytest = "pytest" in hints or any(
            (folder / f).is_file() for f in ("pytest.ini", "conftest.py", "tests/conftest.py"))
        add("Run tests", f"{py} -m pytest" if use_pytest else f"{py} -m unittest discover")
    if (folder / "package.json").is_file():
        scripts = package_scripts(folder)
        if "start" in scripts:
            add("Start", "npm start")
        for s, label in (("dev", "Dev server"), ("test", "Run tests"), ("build", "Build")):
            if s in scripts:
                add(label, "npm test" if s == "test" else f"npm run {s}")
    if (folder / "Cargo.toml").is_file():
        add("Run tests", "cargo test")
        add("Build release", "cargo build --release")
    if (folder / "go.mod").is_file():
        add("Run tests", "go test ./...")
    if (folder / "Makefile").is_file():
        add("Make", "make")
    return opts


def setup_command(folder: Path) -> str:
    """What has to run before a fresh clone will start, or '' if nothing."""
    has_venv = any((folder / v).is_dir() for v in ("venv", ".venv", "env"))
    if (folder / "requirements.txt").is_file() and not has_venv:
        py = "python" if IS_WIN else "python3"
        venv_py = r".venv\Scripts\python" if IS_WIN else ".venv/bin/python"
        return f"{py} -m venv .venv && {venv_py} -m pip install -r requirements.txt"
    if (folder / "package.json").is_file() and not (folder / "node_modules").is_dir():
        return "npm install"
    return ""


def parse_options(text: str) -> list:
    """'Name: command' per line. A line without 'Name: ' is used as both."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, cmd = line.partition(": ")
        if not sep or not cmd.strip():
            name, cmd = line, line
        out.append({"name": name.strip()[:40], "command": cmd.strip()})
    return out


def parse_env(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip():
            env[k.strip()] = v.strip()
    return env


def kind_of(cmd: str) -> str:
    c = cmd.lower()
    if not c:
        return "?"
    if ".bat" in c or ".cmd" in c:
        return "Batch"
    if "python" in c or c.endswith(".py") or ".py " in c:
        return "Python"
    if "powershell" in c or ".ps1" in c:
        return "PowerShell"
    if ".exe" in c:
        return "App"
    if c.startswith(("npm", "node", "yarn")):
        return "Node"
    if c.startswith("cargo"):
        return "Rust"
    if c.startswith("go "):
        return "Go"
    if "bash" in c or ".sh" in c:
        return "Shell"
    return "Other"


def github_url(folder: Path) -> str:
    """Web URL of the repo's origin remote, with any user:token@ stripped out."""
    cfg = folder / ".git" / "config"
    if not cfg.is_file():
        return ""
    try:
        text = cfg.read_text(errors="ignore")
    except OSError:
        return ""
    # prefer [remote "origin"]; fall back to the first url anywhere in the file
    m = re.search(r'^\s*\[remote "origin"\](.*?)(?=^\s*\[|\Z)', text, re.S | re.M)
    m = re.search(r"^\s*url\s*=\s*(\S+)", m.group(1) if m else text, re.M)
    if not m:
        return ""
    url = m.group(1)
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    url = re.sub(r"^ssh://git@([^/:]+)(:\d+)?/", r"https://\1/", url)
    url = re.sub(r"^(https?://)[^/@]+@", r"\1", url)  # drop credentials
    url = re.sub(r"\.git$", "", url)
    return url if url.startswith("http") else ""


def git_info(folder: Path):
    """Uncommitted changes, ahead/behind (as of the last fetch) and last commit time."""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "-b"], cwd=str(folder),
                           capture_output=True, text=True, timeout=15, env=git_env(), **NO_WINDOW)
        if r.returncode:
            return None
        lines = r.stdout.splitlines()
        head = lines[0] if lines and lines[0].startswith("##") else ""
        ahead = re.search(r"ahead (\d+)", head)
        behind = re.search(r"behind (\d+)", head)
        r2 = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=str(folder),
                            capture_output=True, text=True, timeout=15, env=git_env(), **NO_WINDOW)
        ts = r2.stdout.strip()
        return {"changed": sum(1 for line in lines if not line.startswith("##")),
                "ahead": int(ahead.group(1)) if ahead else 0,
                "behind": int(behind.group(1)) if behind else 0,
                "commit": datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
                if r2.returncode == 0 and ts.isdigit() else ""}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def git_short(info) -> str:
    if not info:
        return ""
    parts = []
    if info["changed"]:
        parts.append(f"✎{info['changed']}")
    if info["behind"]:
        parts.append(f"↓{info['behind']}")
    if info["ahead"]:
        parts.append(f"↑{info['ahead']}")
    return " ".join(parts) or "✓"


def git_long(info) -> str:
    parts = []
    n = info["changed"]
    if n:
        parts.append(f"{n} uncommitted change{'s' if n != 1 else ''}")
    if info["ahead"]:
        parts.append(f"{info['ahead']} commit{'s' if info['ahead'] != 1 else ''} to push")
    if info["behind"]:
        parts.append(f"{info['behind']} to pull")
    s = ", ".join(parts) or "clean"
    if info["commit"]:
        s += f", last commit {fmt_date(info['commit'])}"
    return s


def readme_file(folder: Path):
    try:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.stem.lower() == "readme":
                return p
    except OSError:
        pass
    return None


def readme_text(folder: Path, limit: int = 3000) -> str:
    p = readme_file(folder)
    if not p:
        return ""
    try:
        txt = p.read_text(errors="ignore").strip()
    except OSError:
        return ""
    return txt[:limit] + ("\n\n[...]" if len(txt) > limit else "")


def find_cover(folder: Path):
    """icon.png / logo.png / cover.png etc. in common spots, else the first local
    png/gif the README shows. None if nothing usable."""
    for d in COVER_DIRS:
        base = folder / d if d else folder
        try:
            files = {p.name.lower(): p for p in base.iterdir() if p.is_file()}
        except OSError:
            continue
        for n in COVER_NAMES:
            for ext in (".png", ".gif"):
                if n + ext in files:
                    return files[n + ext]
    readme = readme_file(folder)
    if readme:
        try:
            text = readme.read_text(errors="ignore")
        except OSError:
            return None
        srcs = re.findall(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)", text) + \
            re.findall(r"<img[^>]+src=[\"']([^\"']+)", text, re.I)
        root = folder.resolve()
        for src in srcs:
            if src.lower().startswith(("http:", "https:", "data:")):
                continue
            if not src.lower().split("?")[0].endswith((".png", ".gif")):
                continue
            f = (folder / src.split("?")[0].lstrip("/")).resolve()
            if f.is_file() and root in f.parents:
                return f
    return None


def initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else name[:2] or "?").upper()


def color_for(name: str) -> str:
    return TILE_COLORS[zlib.crc32(name.encode("utf-8")) % len(TILE_COLORS)]


def fuzzy_score(q: str, text: str):
    """Higher is better; None if the letters of q don't appear in order in text."""
    if not q:
        return 0
    t = text.lower()
    score, ti, prev = 0, 0, -2
    for ch in q:
        i = t.find(ch, ti)
        if i < 0:
            return None
        score += 10 if i == prev + 1 else 1
        if i == 0 or not t[i - 1].isalnum():
            score += 5
        prev, ti = i, i + 1
    return score - len(t) * 0.1


def fmt_date(iso: str) -> str:
    if not iso:
        return "never"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    days = (datetime.now() - d).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return d.strftime("%b %d, %Y")


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min"
    hours = minutes / 60
    return f"{hours:.1f} hours"


def utc_to_local(stamp: str) -> str:
    """GitHub's '2026-10-06T18:13:00Z' -> naive local-time ISO string for fmt_date."""
    if not stamp:
        return ""
    try:
        d = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return d.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except ValueError:
        return ""


def guess_projects_dir() -> str:
    """For the welcome screen: the folder this launcher was cloned into, if it
    sits next to other repos, else a usual spot under your home folder."""
    parent = APP_DIR.parent
    try:
        if any(d.is_dir() and d != APP_DIR and is_repo(d) for d in parent.iterdir()):
            return str(parent)
    except OSError:
        pass
    home = Path.home()
    for rel in ("Documents/GitHub", "source/repos", "Projects", "projects", "Code", "code",
                "repos", "dev", "GitHub"):
        if (home / rel).is_dir():
            return str(home / rel)
    return ""


def default_config() -> dict:
    return {"projects_dir": "", "editor": "code", "github_user": "", "github_token": "",
            "projects": []}


def load_config():
    """Returns (config, warning, safe_to_save). A broken config file is backed up
    rather than silently replaced, so a bad write can't wipe the library."""
    if not CONFIG_FILE.exists():
        return default_config(), "", True
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("not a JSON object")
        return cfg, "", True
    except (OSError, ValueError) as e:
        backup = CONFIG_FILE.with_name(
            f"{CONFIG_FILE.stem}.broken-{datetime.now():%Y%m%d-%H%M%S}.json")
        try:
            shutil.copy2(CONFIG_FILE, backup)
        except OSError:
            return (default_config(),
                    f"Couldn't read {CONFIG_FILE.name} ({e}) or back it up, so the launcher "
                    "won't save anything this session. Your file has been left untouched.",
                    False)
        return (default_config(),
                f"Couldn't read {CONFIG_FILE.name} ({e}). Starting with an empty library; "
                f"the old file was copied to {backup.name} so nothing is lost.",
                True)


def save_config(cfg: dict) -> None:
    # write to a temp file and swap it in, so a crash mid-write can't corrupt the library
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def _get_json(url: str, headers: dict):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_github_repos(user: str, token: str = ""):
    """Returns (repos, login). The token is only used when `user` is blank or is the
    token's owner, so nobody else's list gets your private repos mixed into it.
    `login` is the token owner's username, or '' without a token."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "launcher.py"}
    login = ""
    base = f"https://api.github.com/users/{user}/repos?sort=pushed&per_page=100"
    if token:
        auth = dict(headers, Authorization=f"Bearer {token}")
        login = _get_json("https://api.github.com/user", auth).get("login", "")
        if not user or user.lower() == login.lower():
            headers = auth
            base = "https://api.github.com/user/repos?affiliation=owner&sort=pushed&per_page=100"
    repos = []
    for page in range(1, 6):  # up to 500 repos
        batch = _get_json(f"{base}&page={page}", headers)
        repos.extend(batch)
        if len(batch) < 100:
            break
    return repos, login


def new_project(name, path, command="", **extra) -> dict:
    p = {"name": name, "path": str(path), "command": command, "group": "",
         "tags": "", "notes": "", "console": True, "runs": 0, "last_run": "",
         "playtime": 0, "last_exit": None, "options": [], "env": "", "image": "",
         "favorite": False, "hidden": False}
    p.update(extra)
    return p


def subdirs(folder: Path):
    try:
        return sorted((s for s in folder.iterdir() if s.is_dir()
                       and not s.name.startswith(".") and s.name not in SKIP_DIRS),
                      key=lambda s: s.name.lower())
    except OSError:
        return []


def plan_scan(root: Path, known: dict, skip=()) -> list:
    """The slow half of a scan; runs on a worker thread and only reads the disk.

    `known` maps norm_key(path) of each library entry to 'project' (has a command),
    'placeholder' (no command, no notes/tags/plays) or 'kept' (no command, but has
    your data). Folders whose key is in `skip` are ignored. Returns
    (step, key, folder, command, group, options) tuples for the main thread to apply."""
    steps = []

    def walk(folder, group, depth):
        for d in subdirs(folder):
            key = norm_key(d)
            if key in skip:
                continue
            cmd = detect_command(d)
            kids = subdirs(d)
            # A folder is a *group* (not a project) if nothing runs at its top level,
            # it isn't a repo itself, there's room to go deeper, and it is either a
            # direct child of the scan root or holds something that looks like a project.
            is_group = bool(
                not cmd and kids and depth < MAX_DEPTH - 1 and not is_repo(d) and
                (depth == 0 or any(is_repo(k) or detect_command(k) for k in kids)))

            state = known.get(key)
            if state is not None:
                if is_group and state == "placeholder":
                    # an older scan added this as a '?' entry; its contents are the real projects
                    steps.append(("replace", key, d, "", group, []))
                elif is_group and state == "kept":
                    steps.append(("kept", key, d, "", group, []))
                    continue
                else:
                    steps.append(("existing", key, d, "", group, []))
                    continue

            if is_group:
                walk(d, f"{group}/{d.name}" if group else d.name, depth + 1)
            else:
                steps.append(("new", key, d, cmd, group, detect_options(d, cmd)))

    walk(root, "", 0)
    return steps


def make_icon(path: Path, size: int = 128) -> None:
    """Write a .ico: dark blue disc with a light play triangle. Stdlib only."""
    cx = cy = size / 2
    rr = (size * 0.47) ** 2
    (x1, y1), (x2, y2), (x3, y3) = [(size * .40, size * .27), (size * .40, size * .73), (size * .74, size * .5)]

    def in_tri(px, py):
        d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
        d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
        d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))

    disc, tri = (0x38, 0x28, 0x1b), (0xf4, 0xc0, 0x66)  # BGR
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            cov = hit = 0
            for sx in (.25, .75):  # 2x2 supersampling for smooth edges
                for sy in (.25, .75):
                    if (x + sx - cx) ** 2 + (y + sy - cy) ** 2 <= rr:
                        cov += 1
                        hit += in_tri(x + sx, y + sy)
            t = hit / cov if cov else 0
            row += bytes(int(a * (1 - t) + b * t) for a, b in zip(disc, tri)) + bytes([cov * 255 // 4])
        rows.append(bytes(row))
    xor = b"".join(reversed(rows))  # BMP rows are bottom-up
    mask = bytes(((size + 31) // 32) * 4 * size)
    bmp = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    img = bmp + xor + mask
    s = 0 if size >= 256 else size
    path.write_bytes(struct.pack("<HHH", 0, 1, 1) +
                     struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(img), 22) + img)


def make_desktop_shortcut() -> str:
    """Create Launcher.lnk on the desktop (Windows). Returns the shortcut path."""
    script = Path(__file__).resolve()
    pyw = Path(sys.executable).with_name("pythonw.exe")  # same Python, no console window
    if not pyw.exists():
        pyw = Path(sys.executable)
    ico = APP_DIR / "launcher.ico"
    try:
        make_icon(ico)
    except OSError:
        ico = None

    def q(s):  # PowerShell single-quoted literal
        return "'" + str(s).replace("'", "''") + "'"

    lines = [
        "$d = [Environment]::GetFolderPath('Desktop')",
        "$p = Join-Path $d 'Launcher.lnk'",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p)",
        f"$s.TargetPath = {q(pyw)}",
        f"$s.Arguments = [char]34 + {q(script)} + [char]34",
        f"$s.WorkingDirectory = {q(APP_DIR)}",
        "$s.Description = 'Project launcher'",
        "$s.Save()",
        "Write-Output $p",
    ]
    if ico:
        lines.insert(-2, f"$s.IconLocation = {q(ico)}")
    encoded = base64.b64encode("\n".join(lines).encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", encoded],
                       capture_output=True, text=True, timeout=60, **NO_WINDOW)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "PowerShell couldn't create the shortcut")
    return r.stdout.strip()


# ---- first-run welcome -----------------------------------------------------
class WelcomeDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Welcome to Launcher")
        self.configure(bg=BG)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text="Welcome!", foreground=ACCENT,
                  font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, foreground=DIM, text="A couple of quick things and your library is ready.").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 16))

        ttk.Label(body, text="1.  Where do your projects live?").grid(row=2, column=0, columnspan=2, sticky="w")
        self.folder = tk.StringVar(value=guess_projects_dir())
        ttk.Entry(body, textvariable=self.folder, width=52).grid(row=3, column=0, sticky="ew", pady=4)
        ttk.Button(body, text="Browse", command=self.browse).grid(row=3, column=1, padx=(6, 0))

        ttk.Label(body, text="2.  Your GitHub username (optional, for Get from GitHub)").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.gh = tk.StringVar(value=app.cfg.get("github_user", ""))
        ttk.Entry(body, textvariable=self.gh).grid(row=5, column=0, sticky="ew", pady=4)

        self.pin = tk.BooleanVar(value=IS_WIN)
        if IS_WIN:
            ttk.Checkbutton(body, variable=self.pin,
                            text="Put a Launcher shortcut on my desktop, so next time it's one double-click").grid(
                row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))

        btns = ttk.Frame(body)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(20, 0))
        ttk.Button(btns, text="Skip", command=lambda: self.finish(False)).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Get started", style="Accent.TButton",
                   command=lambda: self.finish(True)).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", lambda: self.finish(False))
        self.transient(app)
        self.grab_set()

    def browse(self):
        d = filedialog.askdirectory(title="Folder that holds all your projects", parent=self)
        if d:
            self.folder.set(d)

    def finish(self, go):
        cfg = self.app.cfg
        cfg["setup_done"] = True
        folder = self.folder.get().strip() if go else ""
        if go and self.gh.get().strip():
            cfg["github_user"] = self.gh.get().strip()
        pin = go and IS_WIN and self.pin.get()
        self.app.save()
        self.destroy()
        if pin:
            self.app.pin_to_desktop(quiet=True)
        if folder:
            if Path(folder).is_dir():
                self.app.scan_folder(folder)
            else:
                messagebox.showwarning("Folder not found",
                                       f"{folder}\n\nUse Scan folder... to pick it later.")


# ---- add / edit dialog -----------------------------------------------------
class ProjectDialog(tk.Toplevel):
    def __init__(self, master, project=None):
        super().__init__(master)
        self.title("Edit project" if project else "Add project")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None
        p = project or {}

        self.v = {
            "name": tk.StringVar(value=p.get("name", "")),
            "path": tk.StringVar(value=p.get("path", "")),
            "group": tk.StringVar(value=p.get("group", "")),
            "command": tk.StringVar(value=p.get("command", "")),
            "tags": tk.StringVar(value=p.get("tags", "")),
            "image": tk.StringVar(value=p.get("image", "")),
            "console": tk.BooleanVar(value=p.get("console", True)),
            "hidden": tk.BooleanVar(value=p.get("hidden", False)),
        }

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        rows = [("Name", "name"), ("Folder", "path"), ("Group", "group"),
                ("Command", "command"), ("Tags", "tags"), ("Cover image", "image")]
        for row, (label, key) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            ttk.Entry(body, textvariable=self.v[key], width=54).grid(row=row, column=1, sticky="ew", pady=4)
            extra = {"path": ("Browse", self.browse), "command": ("Detect", self.detect),
                     "image": ("Browse", self.browse_image)}.get(key)
            if extra:
                ttk.Button(body, text=extra[0], command=extra[1]).grid(row=row, column=2, padx=(6, 0))

        r = len(rows)
        checks = ttk.Frame(body)
        checks.grid(row=r, column=1, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(checks, variable=self.v["console"],
                        text="Show a console window (untick for GUI apps; output goes to the log)").pack(anchor="w")
        ttk.Checkbutton(checks, variable=self.v["hidden"],
                        text="Hide from the library (still reachable with Ctrl+K)").pack(anchor="w")

        opts = "\n".join(f"{o['name']}: {o['command']}" for o in p.get("options", []))
        self.txt_options = self._text(body, r + 1, "More commands", 3, opts)
        self.txt_env = self._text(body, r + 2, "Environment", 2, p.get("env", ""))
        self.notes = self._text(body, r + 3, "Notes", 4, p.get("notes", ""))

        ttk.Label(body, foreground=DIM, wraplength=460, justify="left",
                  text="Commands run inside the project folder, e.g. call run.bat, python main.py, "
                       "npm start, game.exe. More commands: one per line as Name: command "
                       "(e.g. Run tests: python -m pytest); they're under the ▾ next to Play. "
                       "Environment: KEY=value per line. Cover image: a .png or .gif; leave blank "
                       "to find one automatically. Group is the heading it sits under.").grid(
            row=r + 4, column=1, columnspan=2, sticky="w", pady=(4, 0))

        btns = ttk.Frame(body)
        btns.grid(row=r + 5, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self.ok).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(master)
        self.grab_set()
        self.wait_window()

    def _text(self, body, row, label, height, value):
        ttk.Label(body, text=label).grid(row=row, column=0, sticky="nw", pady=4, padx=(0, 12))
        t = tk.Text(body, height=height, width=54, bg=PANEL, fg=FG, insertbackground=FG,
                    relief="flat", wrap="word", font=FONT, padx=6, pady=4)
        t.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        t.insert("1.0", value)
        return t

    def browse(self):
        d = filedialog.askdirectory(title="Pick the project folder", parent=self)
        if d:
            self.v["path"].set(d)
            if not self.v["name"].get():
                self.v["name"].set(Path(d).name)
            if not self.v["command"].get():
                self.detect(quiet=True)

    def browse_image(self):
        f = filedialog.askopenfilename(title="Pick a cover image", parent=self,
                                       filetypes=[("Images", "*.png *.gif"), ("All files", "*.*")])
        if f:
            self.v["image"].set(f)

    def detect(self, quiet=False):
        folder = Path(self.v["path"].get())
        if not folder.is_dir():
            messagebox.showwarning("No folder", "Pick a folder first.", parent=self)
            return
        cmd = detect_command(folder)
        if cmd:
            self.v["command"].set(cmd)
        if not self.txt_options.get("1.0", "end").strip():
            opts = detect_options(folder, cmd)
            self.txt_options.insert("1.0", "\n".join(f"{o['name']}: {o['command']}" for o in opts))
        if not cmd and not quiet:
            messagebox.showinfo("Nothing obvious",
                                "Couldn't spot a run.bat, main.py, .exe, etc. Type the command by hand.",
                                parent=self)

    def ok(self):
        name = self.v["name"].get().strip()
        path = self.v["path"].get().strip()
        if not name or not path:
            messagebox.showwarning("Missing info", "Name and folder are required.", parent=self)
            return
        if not Path(path).is_dir():
            messagebox.showwarning("Folder not found", path, parent=self)
            return
        self.result = {
            "name": name, "path": path,
            "group": self.v["group"].get().strip().strip("/"),
            "command": self.v["command"].get().strip(),
            "tags": self.v["tags"].get().strip(),
            "image": self.v["image"].get().strip(),
            "console": self.v["console"].get(),
            "hidden": self.v["hidden"].get(),
            "options": parse_options(self.txt_options.get("1.0", "end")),
            "env": self.txt_env.get("1.0", "end").strip(),
            "notes": self.notes.get("1.0", "end").strip(),
        }
        self.destroy()


# ---- quick launch (Ctrl+K) -------------------------------------------------
class QuickLaunch(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Quick launch")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.q = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.q, width=48, font=("Segoe UI", 12))
        self.entry.pack(fill="x")
        self.lb = tk.Listbox(frame, height=9, bg=PANEL, fg=FG, selectbackground=PANEL2,
                             selectforeground="#ffffff", relief="flat", highlightthickness=0,
                             borderwidth=0, font=FONT, activestyle="none")
        self.lb.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(frame, foreground=DIM, text="Enter to play  ·  ↑ ↓ to pick  ·  Esc to close").pack(
            anchor="w", pady=(6, 0))

        self.matches = []
        self.q.trace_add("write", lambda *_: self.update_list())
        for w in (self.entry, self.lb):
            w.bind("<Return>", self.go)
            w.bind("<Escape>", lambda e: self.destroy())
        self.entry.bind("<Down>", lambda e: self.move(1))
        self.entry.bind("<Up>", lambda e: self.move(-1))
        self.lb.bind("<Double-1>", self.go)

        self.update_list()
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        self.geometry(f"+{max(x, 0)}+{app.winfo_rooty() + 80}")
        self.entry.focus_set()

    def update_list(self):
        q = self.q.get().strip().lower()
        scored = []
        for p in self.app.projects:
            s = fuzzy_score(q, p["name"])
            if s is None:
                s = fuzzy_score(q, f"{p.get('group', '')} {p.get('tags', '')}")
                if s is None:
                    continue
                s -= 20
            scored.append((s, p.get("last_run", ""), p))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        self.matches = [t[2] for t in scored[:50]]
        self.lb.delete(0, "end")
        for p in self.matches:
            text = ("★ " if p.get("favorite") else "") + p["name"]
            if p.get("group"):
                text += f"      {p['group']}"
            if norm_key(p["path"]) in self.app.running:
                text += "      ▶ running"
            self.lb.insert("end", text)
        if self.matches:
            self.lb.selection_set(0)

    def move(self, d):
        if self.matches:
            cur = self.lb.curselection()
            i = max(0, min(len(self.matches) - 1, (cur[0] if cur else 0) + d))
            self.lb.selection_clear(0, "end")
            self.lb.selection_set(i)
            self.lb.see(i)
        return "break"

    def go(self, _=None):
        if not self.matches:
            return
        cur = self.lb.curselection()
        p = self.matches[cur[0] if cur else 0]
        self.destroy()
        self.app.select_project(p)
        self.app.launch(p=p)


# ---- get-from-GitHub dialog ------------------------------------------------
class GitHubDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Get projects from GitHub")
        self.configure(bg=BG)
        self.geometry("780x540")
        self.minsize(640, 400)
        self.repos = []
        self.busy = False

        top = ttk.Frame(self, padding=(16, 14, 16, 4))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="GitHub user").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.user = tk.StringVar(value=app.cfg.get("github_user") or app.guess_github_user())
        self.user_box = e = ttk.Combobox(top, textvariable=self.user, values=app.cfg.get("github_users", []))
        e.grid(row=0, column=1, sticky="ew", pady=4)
        e.bind("<Return>", lambda _: self.load())
        e.bind("<<ComboboxSelected>>", lambda _: self.load())
        ttk.Button(top, text="Load repos", style="Accent.TButton",
                   command=self.load).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(top, text="Download into").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.dest = tk.StringVar(value=app.cfg.get("projects_dir", ""))
        ttk.Entry(top, textvariable=self.dest).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="Browse", command=self.browse).grid(row=1, column=2, padx=(6, 0))

        ttk.Label(top, foreground=DIM, wraplength=700, justify="left",
                  text="Any GitHub username works here, not just yours. Shows public repos; to include "
                       f"your private ones, put a GitHub token in \"github_token\" in {CONFIG_FILE.name}. "
                       "Downloading needs git installed.").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))

        mid = ttk.Frame(self, padding=(16, 8))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("lang", "updated", "status"),
                                 show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Repo", anchor="w")
        self.tree.heading("lang", text="Language")
        self.tree.heading("updated", text="Last push")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=260, anchor="w")
        self.tree.column("lang", width=100, stretch=False)
        self.tree.column("updated", width=110, stretch=False)
        self.tree.column("status", width=120, stretch=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        self.desc = ttk.Label(self, foreground=DIM, wraplength=740, justify="left", padding=(16, 0))
        self.desc.pack(fill="x")

        bottom = ttk.Frame(self, padding=(16, 10, 16, 14))
        bottom.pack(fill="x")
        self.msg = ttk.Label(bottom, foreground=DIM, wraplength=440, justify="left")
        self.msg.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right", padx=(6, 0))
        self.btn_get = ttk.Button(bottom, text="Download selected", style="Accent.TButton",
                                  command=self.download, state="disabled")
        self.btn_get.pack(side="right")

        self.transient(app)
        if self.user.get():
            self.load()

    def set_msg(self, text):
        self.msg.config(text=text)

    def browse(self):
        d = filedialog.askdirectory(title="Download repos into", parent=self)
        if d:
            self.dest.set(d)

    def on_select(self, _=None):
        sel = self.tree.selection()
        if sel:
            r = self.repos[int(sel[0])]
            self.desc.config(text=(r.get("description") or "No description.") +
                             f"    {r['html_url']}")

    def load(self):
        user = self.user.get().strip()
        token = self.app.cfg.get("github_token", "")
        if not user and not token:
            messagebox.showwarning("Who?", "Type a GitHub username first.", parent=self)
            return
        self.set_msg(f"Loading repos for {user or 'your token'}...")
        self.btn_get.config(state="disabled")

        def work():
            try:
                repos, login = fetch_github_repos(user, token)
                self.app.call_soon(self.loaded, user, repos, login)
            except urllib.error.HTTPError as e:
                msg = {404: f"No GitHub user called '{user}'.",
                       403: "GitHub is rate-limiting this computer (60 lookups an hour without a token). Try again later.",
                       401: f"GitHub rejected the token in {CONFIG_FILE.name}."}.get(
                    e.code, f"GitHub answered {e.code} {e.reason}.")
                self.app.call_soon(self.fail, msg)
            except Exception as e:
                self.app.call_soon(self.fail, f"Couldn't reach GitHub: {e}")

        threading.Thread(target=work, daemon=True).start()

    def loaded(self, user, repos, login):
        cfg = self.app.cfg
        if login:
            # the token's owner is, by definition, you
            cfg["github_user"] = login
        elif user and not cfg.get("github_user") and not cfg.get("github_token"):
            cfg["github_user"] = user
        name = user or login
        users = cfg.setdefault("github_users", [])
        if name and name not in users:
            users.append(name)
        self.app.save()
        self.user_box.config(values=users)
        if not user and login:
            self.user.set(login)
        self.show_repos(repos)

    def fail(self, msg):
        self.set_msg(msg)
        messagebox.showerror("Couldn't load repos", msg, parent=self)

    def show_repos(self, repos):
        self.repos = repos
        self.tree.delete(*self.tree.get_children())
        have_urls, loose_names = set(), set()
        for p in self.app.projects:
            u = github_url(Path(p["path"])).lower()
            if u:
                have_urls.add(u)
            else:
                loose_names.add(p["name"].lower())
        for i, r in enumerate(repos):
            in_lib = r["html_url"].lower() in have_urls or r["name"].lower() in loose_names
            self.tree.insert("", "end", iid=str(i), text=r["name"],
                             values=(r.get("language") or "",
                                     fmt_date(utc_to_local(r.get("pushed_at") or "")),
                                     "In library" if in_lib else "Not downloaded"))
        self.btn_get.config(state="normal")
        self.set_msg(f"{len(repos)} repos. Select the ones you want, then Download selected.")

    def download(self):
        if self.busy:
            return
        picked = [self.repos[int(i)] for i in self.tree.selection()]
        if not picked:
            messagebox.showinfo("Nothing selected", "Select one or more repos first.", parent=self)
            return
        dest = Path(self.dest.get().strip())
        if not dest.is_dir():
            messagebox.showwarning("Pick a folder", "Choose a folder to download into.", parent=self)
            return
        if not shutil.which("git"):
            messagebox.showerror("git not found",
                                 "git isn't installed or isn't on PATH. Get it from git-scm.com.",
                                 parent=self)
            return

        self.busy = True
        self.btn_get.config(state="disabled")

        def work():
            results = []
            for n, r in enumerate(picked, 1):
                target = dest / r["name"]
                self.app.call_soon(self.set_msg, f"Downloading {r['name']} ({n} of {len(picked)})...")
                if target.exists():
                    results.append((r, target, "ok"))  # already on disk; just add it
                    continue
                try:
                    res = subprocess.run(["git", "clone", r["clone_url"], str(target)],
                                         capture_output=True, text=True, timeout=900,
                                         env=git_env(), **NO_WINDOW)
                    err = res.stderr.strip().splitlines()
                    results.append((r, target, "ok" if res.returncode == 0
                                    else (err[-1] if err else "git clone failed")))
                except Exception as e:
                    results.append((r, target, f"failed: {e}"))
            self.app.call_soon(self.finish, results)

        threading.Thread(target=work, daemon=True).start()

    def finish(self, results):
        # library work first, so it still happens if this dialog was closed mid-download
        new, problems = [], []
        for r, target, result in results:
            if result == "ok":
                proj = self.app.add_folder(target, commit=False)
                if proj:
                    new.append(proj)
            else:
                problems.append(f"{r['name']}: {result}")
        if new:
            self.app.save()
            self.app.refresh()
            self.app.refresh_git()
        self.busy = False
        added = len(new)
        msg = f"Added {added} project{'s' if added != 1 else ''} to the library."
        if problems:
            msg += "\n\nProblems:\n" + "\n".join(problems)
        alive = self.winfo_exists()
        parent = self if alive else self.app
        if alive:
            self.btn_get.config(state="normal")
            self.set_msg(msg.splitlines()[0])
            self.show_repos(self.repos)
        else:
            self.app.status.set(msg.splitlines()[0])

        # first run of a fresh clone: offer to install its dependencies right away
        needs = [p for p in new if setup_command(Path(p["path"]))]
        if needs:
            names = "\n".join(f"   {p['name']}:  {setup_command(Path(p['path']))}" for p in needs[:10])
            more = f"\n   ...and {len(needs) - 10} more" if len(needs) > 10 else ""
            if messagebox.askyesno(
                    "Set up new projects",
                    f"{msg}\n\n{len(needs)} of them need their dependencies installed before "
                    f"they'll run:\n\n{names}{more}\n\nDo that now? Each one opens its own window.",
                    parent=parent):
                for p in needs:
                    self.app.run_setup(p)
            return
        messagebox.showinfo("Download finished", msg, parent=parent)


# ---- main window -----------------------------------------------------------
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Launcher")
        self.geometry("1200x700")
        self.minsize(900, 540)
        self.configure(bg=BG)

        self.cfg, config_warning, self.can_save = load_config()
        self.projects = self.cfg.setdefault("projects", [])
        self.filtered = []
        self.selected = None
        self.scanning = False
        self.pulling = False
        self.running = {}  # norm_key(path) -> info about the process started from here
        self.git_info = {}  # norm_key(path) -> git_info() result
        self.git_busy = self.git_again = False
        self._cover_paths = {}  # norm_key(path) -> cover file (or None)
        self._images = {}  # (file, size) -> PhotoImage (or None)
        self.view = self.cfg.get("view", "list")
        self._cols = 4
        self._tiles, self._grid_items = {}, []

        # worker threads hand results back through this; only the main thread touches tkinter
        self._calls = queue.Queue()
        self.after(50, self._pump)

        self._style()
        self._build()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(400, self.refresh_git)
        self.after(15000, self._tick)

        if config_warning:
            self.after(200, lambda: messagebox.showwarning("Library file problem", config_warning))
        elif not self.cfg.get("setup_done"):
            if self.projects:  # upgrading from an older version: no need for the welcome
                self.cfg["setup_done"] = True
                self.save()
            else:
                self.after(300, lambda: WelcomeDialog(self))

    # -- threading -----------------------------------------------------------
    def call_soon(self, fn, *args):
        """Safe to call from any thread: runs fn(*args) on the tkinter thread."""
        self._calls.put((fn, args))

    def _pump(self):
        try:
            while True:
                fn, args = self._calls.get_nowait()
                try:
                    fn(*args)
                except tk.TclError:
                    pass  # the window it was meant for has been closed
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _tick(self):
        # keep "running for N min" current without redrawing anything else
        p = self.selected
        if p and norm_key(p["path"]) in self.running:
            self.lbl_meta.config(text=self._meta_text(p))
        self.after(15000, self._tick)

    def on_close(self):
        n = len(self.running)
        if n and not messagebox.askyesno(
                "Still running",
                f"{n} thing{'s are' if n != 1 else ' is'} still running. Close the launcher anyway?\n\n"
                f"{'They' if n != 1 else 'It'}'ll keep running, but the playtime won't be recorded."):
            return
        self.destroy()

    # -- looks ---------------------------------------------------------------
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, font=FONT)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Dim.TLabel", background=PANEL, foreground=DIM)
        s.configure("Cmd.TLabel", background=PANEL, foreground=ACCENT, font=MONO)
        s.configure("Title.TLabel", background=PANEL, foreground="#ffffff", font=("Segoe UI", 18, "bold"))
        s.configure("Brand.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 13, "bold"))
        s.configure("Status.TLabel", background=BG, foreground=DIM, font=("Segoe UI", 9))
        s.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0, padding=(10, 6))
        s.map("TButton", background=[("active", ACCENT), ("disabled", PANEL)],
              foreground=[("active", "#000000"), ("disabled", DIM)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#000000")
        s.map("Accent.TButton", background=[("active", "#8fd3ff")])
        for name, bg, hot, pad in (("Play", PLAY, PLAY_HOT, (20, 8)), ("PlayMore", PLAY, PLAY_HOT, (6, 8)),
                                   ("Stop", STOP, STOP_HOT, (20, 8))):
            s.configure(f"{name}.TButton", background=bg, foreground="#ffffff",
                        font=("Segoe UI", 11, "bold"), padding=pad)
            s.map(f"{name}.TButton", background=[("active", hot), ("disabled", PANEL)],
                  foreground=[("active", "#ffffff"), ("disabled", DIM)])
        s.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG, borderwidth=0, padding=4)
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL2, foreground=FG,
                    arrowcolor=FG, borderwidth=0)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=30, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL2, foreground=FG, relief="flat",
                    font=("Segoe UI", 9, "bold"), padding=6)
        s.map("Treeview.Heading", background=[("active", PANEL2)])
        s.map("Treeview", background=[("selected", PANEL2)], foreground=[("selected", "#ffffff")])
        s.configure("TPanedwindow", background=BG)
        s.configure("Sash", sashthickness=6)
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", PANEL2)

    # -- layout --------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self, padding=(14, 12))
        top.pack(fill="x")
        ttk.Label(top, text="Library", style="Brand.TLabel").pack(side="left")

        ttk.Label(top, text="Search").pack(side="left", padx=(24, 6))
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh())
        self.search_entry = ttk.Entry(top, textvariable=self.search, width=22)
        self.search_entry.pack(side="left")

        ttk.Label(top, text="Sort").pack(side="left", padx=(16, 6))
        self.sort = tk.StringVar(value="Name")
        cb = ttk.Combobox(top, textvariable=self.sort, state="readonly", width=15,
                          values=["Name", "Recently played", "Most played", "Playtime", "Kind", "Last commit"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        self.show_hidden = tk.BooleanVar(value=self.cfg.get("show_hidden", False))
        ttk.Checkbutton(top, text="Hidden", variable=self.show_hidden,
                        command=self.toggle_hidden).pack(side="left", padx=(14, 0))
        self.btn_view = ttk.Button(top, command=self.toggle_view,
                                   text="List view" if self.view == "grid" else "Grid view")
        self.btn_view.pack(side="left", padx=(10, 0))

        ttk.Button(top, text="Add project", command=self.add_project).pack(side="right")
        btn_scan = ttk.Button(top, text="Scan folder...", command=self.scan_folder)
        btn_scan.pack(side="right", padx=6)
        btn_rescan = ttk.Button(top, text="Rescan", command=self.rescan)
        btn_rescan.pack(side="right")
        self.scan_btns = [btn_scan, btn_rescan]
        ttk.Button(top, text="Get from GitHub...",
                   command=lambda: GitHubDialog(self)).pack(side="right", padx=(0, 6))

        self.status = tk.StringVar(value="")
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status, style="Status.TLabel",
                  padding=(14, 4)).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Pin to desktop", command=self.pin_to_desktop).pack(side="right", padx=14, pady=4)
        ttk.Button(bar, text="Remove missing", command=self.remove_missing).pack(side="right", pady=4)
        ttk.Button(bar, text="Pull all", command=self.pull_all).pack(side="right", padx=6, pady=4)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        left = ttk.Frame(pane)
        self._build_list(left)
        self._build_grid(left)
        (self.grid_frame if self.view == "grid" else self.list_frame).pack(fill="both", expand=True)

        right = ttk.Frame(pane, style="Panel.TFrame", padding=20)
        head = ttk.Frame(right, style="Panel.TFrame")
        head.pack(fill="x")
        self.lbl_img = ttk.Label(head, style="Panel.TLabel")
        self.head_text = ttk.Frame(head, style="Panel.TFrame")
        self.head_text.pack(side="left", fill="x", expand=True)
        self.lbl_name = ttk.Label(self.head_text, style="Title.TLabel", wraplength=520)
        self.lbl_name.pack(anchor="w")
        self.lbl_meta = ttk.Label(self.head_text, style="Dim.TLabel", wraplength=520, justify="left")
        self.lbl_meta.pack(anchor="w", pady=(4, 12))

        row1 = ttk.Frame(right, style="Panel.TFrame")
        row1.pack(fill="x", pady=(0, 8))
        self.btn_play = ttk.Button(row1, text="▶ Play", style="Play.TButton", command=self.play_or_stop)
        self.btn_play.pack(side="left")
        self.btn_more = ttk.Button(row1, text="▾", style="PlayMore.TButton", width=2,
                                   command=self._show_options_menu)
        self.btn_more.pack(side="left", padx=(2, 0))
        self.btn_setup = ttk.Button(row1, text="Set up", style="Accent.TButton", command=self.run_setup)

        self.action_btns = []
        rows = ((("Open folder", self.open_folder), ("VS Code", self.open_editor),
                 ("Git pull", self.git_pull), ("GitHub", self.open_github)),
                (("Log", self.open_log), ("☆ Favorite", self.toggle_favorite),
                 ("Edit", self.edit_project), ("Remove", self.remove_project)))
        for spec in rows:
            row = ttk.Frame(right, style="Panel.TFrame")
            row.pack(fill="x", pady=(0, 6))
            for i, (text, cmd) in enumerate(spec):
                b = ttk.Button(row, text=text, command=cmd)
                b.pack(side="left", padx=(0 if i == 0 else 6, 0))
                self.action_btns.append(b)
        self.btn_fav = self.action_btns[5]

        self.lbl_cmd = ttk.Label(right, style="Cmd.TLabel", wraplength=560, justify="left")
        self.lbl_cmd.pack(anchor="w", pady=(6, 10))

        self.txt = tk.Text(right, bg=BG, fg=FG, relief="flat", wrap="word", padx=12, pady=10,
                           state="disabled", font=FONT, highlightthickness=0)
        self.txt.pack(fill="both", expand=True)

        pane.add(left, weight=2)
        pane.add(right, weight=3)

        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Control-n>", lambda e: self.add_project())
        self.bind("<Control-k>", lambda e: self.quick_launch())
        self.bind("<F5>", lambda e: self.refresh_git())
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        self.bind_all("<Button-4>", self._wheel, add="+")
        self.bind_all("<Button-5>", self._wheel, add="+")

    def _build_list(self, parent):
        self.list_frame = ttk.Frame(parent)
        self.tree = ttk.Treeview(self.list_frame, columns=("kind", "git", "commit", "last"),
                                 show="tree headings", selectmode="browse")
        for col, text, width in (("#0", "Project", 220), ("kind", "Kind", 70), ("git", "Git", 70),
                                 ("commit", "Last commit", 95), ("last", "Last played", 95)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, anchor="w", stretch=(col == "#0"))
        self.tree.tag_configure("running", foreground="#8fe36a")
        self.tree.tag_configure("hidden", foreground=DIM)
        self.tree.tag_configure("missing", foreground="#e07a6a")
        sb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", lambda e: self.launch())
        self.tree.bind("<Delete>", lambda e: self.remove_project())

    def _build_grid(self, parent):
        self.grid_frame = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.grid_frame, bg=PANEL, highlightthickness=0)
        gsb = ttk.Scrollbar(self.grid_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=gsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        gsb.pack(side="right", fill="y")
        self.grid_inner = tk.Frame(self.canvas, bg=PANEL)
        self._grid_win = self.canvas.create_window((0, 0), window=self.grid_inner, anchor="nw")
        self.grid_inner.bind("<Configure>", self._grid_inner_resized)
        self.canvas.bind("<Configure>", self._grid_resized)
        self.canvas.bind("<Return>", lambda e: self.launch())
        self.canvas.bind("<Delete>", lambda e: self.remove_project())

    def toggle_view(self):
        self.view = "list" if self.view == "grid" else "grid"
        self.cfg["view"] = self.view
        self.save()
        if self.view == "grid":
            self.list_frame.pack_forget()
            self.grid_frame.pack(fill="both", expand=True)
            self.btn_view.config(text="List view")
        else:
            self.grid_frame.pack_forget()
            self.list_frame.pack(fill="both", expand=True)
            self.btn_view.config(text="Grid view")
        self.refresh()

    def toggle_hidden(self):
        self.cfg["show_hidden"] = self.show_hidden.get()
        self.save()
        self.refresh()

    # -- list + details ------------------------------------------------------
    def refresh(self):
        q = self.search.get().lower().strip()
        show_hidden = self.show_hidden.get()
        items = [p for p in self.projects
                 if (show_hidden or not p.get("hidden")) and (not q or q in
                 f"{p['name']} {p.get('group', '')} {p.get('tags', '')} {p['path']}".lower())]
        key = self.sort.get()
        if key == "Recently played":
            items.sort(key=lambda p: p.get("last_run", ""), reverse=True)
        elif key == "Most played":
            items.sort(key=lambda p: p.get("runs", 0), reverse=True)
        elif key == "Playtime":
            items.sort(key=lambda p: p.get("playtime", 0), reverse=True)
        elif key == "Kind":
            items.sort(key=lambda p: (kind_of(p["command"]), p["name"].lower()))
        elif key == "Last commit":
            items.sort(key=lambda p: (self.git_info.get(norm_key(p["path"])) or {}).get("commit", ""),
                       reverse=True)
        else:
            items.sort(key=lambda p: p["name"].lower())
        items.sort(key=lambda p: not p.get("favorite"))  # stable: favorites float to the top
        self.filtered = items

        groups, loose = {}, []
        for p in items:
            g = p.get("group", "")
            (groups.setdefault(g, []) if g else loose).append(p)
        sections = [(g, groups[g]) for g in sorted(groups, key=str.lower)]
        if loose:
            sections.append(("", loose))

        if not any(p is self.selected for p in items):
            self.selected = None
        if self.view == "grid":
            self._fill_grid(sections)
        else:
            self._fill_list(sections)
        self.show_details(self.selected)
        n, total = len(items), len(self.projects)
        self.status.set(f"{n} of {total} projects" if n != total else f"{total} projects")

    def _decorated_name(self, p):
        name = ("★ " if p.get("favorite") else "") + p["name"]
        if not Path(p["path"]).is_dir():
            name += "  (missing)"
        if p.get("hidden"):
            name += "  (hidden)"
        if norm_key(p["path"]) in self.running:
            name += "   ▶"
        return name

    def _fill_list(self, sections):
        self.tree.delete(*self.tree.get_children())
        self.by_iid = {}  # tree row id -> project (group headers are not in here)
        i = 0
        for n, (g, ps) in enumerate(sections):
            parent = ""
            if g:
                parent = self.tree.insert("", "end", iid=f"g{n}", open=True,
                                          text=f"{g}  ({len(ps)})", values=("", "", "", ""))
            for p in ps:
                key = norm_key(p["path"])
                info = self.git_info.get(key)
                tags = ("running",) if key in self.running else \
                    ("missing",) if not Path(p["path"]).is_dir() else \
                    ("hidden",) if p.get("hidden") else ()
                iid = f"p{i}"
                i += 1
                self.tree.insert(parent, "end", iid=iid, text=self._decorated_name(p), tags=tags,
                                 values=(kind_of(p["command"]), git_short(info),
                                         fmt_date(info["commit"]) if info and info["commit"] else "",
                                         fmt_date(p.get("last_run", ""))))
                self.by_iid[iid] = p
        sel = next((k for k, v in self.by_iid.items() if v is self.selected), None)
        if sel:
            self.tree.selection_set(sel)
            self.tree.see(sel)

    def _fill_grid(self, sections):
        for w in self.grid_inner.winfo_children():
            w.destroy()
        self._grid_items, self._tiles = [], {}
        if not sections:
            self._grid_items.append(("h", tk.Label(
                self.grid_inner, bg=PANEL, fg=DIM, font=FONT,
                text="Nothing matches." if self.projects else
                "Your library is empty. Click Scan folder to fill it.")))
        named = any(g for g, _ in sections)
        for g, ps in sections:
            if named:
                self._grid_items.append(("h", tk.Label(self.grid_inner, text=f"{g or 'Other'}  ({len(ps)})",
                                                       bg=PANEL, fg=ACCENT, font=("Segoe UI", 10, "bold"))))
            for p in ps:
                t = self._make_tile(p)
                self._grid_items.append(("t", t))
                self._tiles[id(p)] = t
        self._layout_grid()

    def _make_tile(self, p):
        folder = Path(p["path"])
        key = norm_key(folder)
        running = key in self.running
        f = tk.Frame(self.grid_inner, bg=PANEL, width=TILE_W - 12, height=TILE_H, cursor="hand2",
                     highlightthickness=2,
                     highlightbackground=ACCENT if p is self.selected else PANEL)
        f.pack_propagate(False)
        img = self.cover_for(p, ART) if folder.is_dir() else None
        box_bg = PANEL if img else color_for(p["name"])
        box = tk.Frame(f, width=ART, height=ART, bg=box_bg)
        box.pack_propagate(False)
        box.pack(pady=(8, 4))
        if img:
            art = tk.Label(box, image=img, bg=PANEL)
        else:
            art = tk.Label(box, text=initials(p["name"]), bg=box_bg, fg="#ffffff",
                           font=("Segoe UI", 26, "bold"))
        art.pack(expand=True, fill="both")
        name = tk.Label(f, text=("★ " if p.get("favorite") else "") + p["name"], bg=PANEL,
                        fg=DIM if p.get("hidden") else FG, font=FONT,
                        wraplength=TILE_W - 24, justify="center")
        name.pack()
        pt, runs = p.get("playtime", 0), p.get("runs", 0)
        sub_text = ("▶ running" if running else "missing" if not folder.is_dir()
                    else fmt_duration(pt) if pt else f"played {runs}×" if runs else "never played")
        sub = tk.Label(f, text=sub_text, bg=PANEL, fg=PLAY_HOT if running else DIM, font=("Segoe UI", 8))
        sub.pack()
        for w in (f, box, art, name, sub):
            w.bind("<Button-1>", lambda e, p=p: self._grid_click(p))
            w.bind("<Double-1>", lambda e, p=p: self._grid_double(p))
        return f

    def _layout_grid(self):
        cols = self._cols
        r = c = 0
        for kind, w in self._grid_items:
            if kind == "h":
                if c:
                    r, c = r + 1, 0
                w.grid(row=r, column=0, columnspan=cols, sticky="w", padx=10, pady=(12, 2))
                r += 1
            else:
                w.grid(row=r, column=c, padx=6, pady=6, sticky="n")
                c += 1
                if c >= cols:
                    r, c = r + 1, 0

    def _grid_resized(self, e):
        self.canvas.itemconfigure(self._grid_win, width=e.width)
        cols = max(1, e.width // TILE_W)
        if cols != self._cols:
            self._cols = cols
            self._layout_grid()

    def _grid_inner_resized(self, _=None):
        x1, y1, x2, y2 = self.canvas.bbox(self._grid_win) or (0, 0, 0, 0)
        # never smaller than the visible area, so short content doesn't scroll about
        self.canvas.configure(scrollregion=(0, 0, max(x2, self.canvas.winfo_width()),
                                            max(y2, self.canvas.winfo_height())))

    def _wheel(self, e):
        if self.view != "grid":
            return
        w = self.winfo_containing(e.x_root, e.y_root)
        if not w or not str(w).startswith(str(self.canvas)):
            return
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step * 2, "units")

    def _grid_click(self, p):
        self.selected = p
        for pid, tile in self._tiles.items():
            tile.config(highlightbackground=ACCENT if pid == id(p) else PANEL)
        self.show_details(p)
        self.canvas.focus_set()

    def _grid_double(self, p):
        self._grid_click(p)
        self.launch(p=p)

    def on_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        p = self.by_iid.get(sel[0])
        if p is None:  # a group header
            self.selected = None
            self.show_details(None, group=self.tree.item(sel[0], "text"))
        else:
            self.selected = p
            self.show_details(p)

    def _on_double(self, e):
        # double-click on a header row just expands/collapses it (default behaviour)
        if self.tree.identify_row(e.y) in self.by_iid and \
                self.tree.identify_region(e.x, e.y) != "heading":
            self.launch()

    def select_project(self, p):
        self.selected = p
        self.refresh()

    def _set_text(self, s):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", s)
        self.txt.config(state="disabled")

    def cover_for(self, p, size):
        key = norm_key(p["path"])
        if key not in self._cover_paths:
            path = None
            chosen = (p.get("image") or "").strip()
            if chosen:
                c = Path(chosen)
                if not c.is_absolute():
                    c = Path(p["path"]) / c
                path = c if c.is_file() else None
            if path is None and Path(p["path"]).is_dir():
                path = find_cover(Path(p["path"]))
            self._cover_paths[key] = path
        path = self._cover_paths[key]
        return self.load_image(path, size) if path else None

    def load_image(self, path, box):
        key = (str(path), box)
        if key not in self._images:
            try:
                img = tk.PhotoImage(master=self, file=str(path))
                m = max(img.width(), img.height())
                if m > box:
                    img = img.subsample(-(-m // box))  # ceil division: shrink to fit
                elif 0 < m and m * 2 <= box:
                    img = img.zoom(box // m)  # blow up tiny icons
                self._images[key] = img
            except tk.TclError:
                self._images[key] = None
        return self._images[key]

    def _meta_text(self, p):
        folder = Path(p["path"])
        key = norm_key(folder)
        missing = not folder.is_dir()
        lines = []
        run = self.running.get(key)
        if run:
            what = "" if run["name"] == "Play" else run["name"] + " "
            lines.append(f"▶ {what}running for {fmt_duration(time.time() - run['start'])}")
        runs, pt = p.get("runs", 0), p.get("playtime", 0)
        if runs:
            s = f"Played {runs} time{'s' if runs != 1 else ''}"
            if pt:
                s += f", {fmt_duration(pt)} total"
            lines.append(s + f", last {fmt_date(p.get('last_run', ''))}")
        else:
            lines.append("Never played")
        code = p.get("last_exit")
        if code not in (None, 0) and not run:
            lines.append(f"Last run exited with code {code}. Click Log to see why.")
        if p.get("group"):
            lines.append("In " + p["group"])
        if p.get("tags"):
            lines.append("Tags: " + p["tags"])
        remote = github_url(folder) if not missing else ""
        if remote:
            lines.append("From " + remote.replace("https://", ""))
        info = self.git_info.get(key)
        if info:
            lines.append("Git: " + git_long(info))
        lines.append(str(folder) + ("   (folder not found)" if missing else ""))
        return "\n".join(lines)

    def show_details(self, p, group=""):
        state = "normal" if p else "disabled"
        for b in [self.btn_play, self.btn_more] + self.action_btns:
            b.config(state=state)
        self.btn_setup.pack_forget()
        self.lbl_img.pack_forget()

        if not p:
            self.btn_play.config(text="▶ Play", style="Play.TButton")
            self.btn_fav.config(text="☆ Favorite")
            if group:
                self.lbl_name.config(text=group)
                self.lbl_meta.config(text="A folder of projects. Pick one inside it to play.")
            else:
                self.lbl_name.config(text="Nothing selected" if self.projects else "Your library is empty")
                self.lbl_meta.config(text="Ctrl+K to find and play anything quickly." if self.projects else
                                     "Click Scan folder and point it at the folder that holds your repos. "
                                     "Each one becomes an entry here with a launch command guessed for it.")
            self.lbl_cmd.config(text="")
            self._set_text("")
            return

        folder = Path(p["path"])
        missing = not folder.is_dir()
        run = self.running.get(norm_key(folder))
        self.btn_play.config(text="■ Stop" if run else "▶ Play",
                             style="Stop.TButton" if run else "Play.TButton")
        self.btn_more.config(state="disabled" if run else "normal")
        self.btn_fav.config(text="★ Favorited" if p.get("favorite") else "☆ Favorite")

        img = self.cover_for(p, 64) if not missing else None
        if img:
            self.lbl_img.config(image=img)
            self.lbl_img.pack(side="left", padx=(0, 14), anchor="n", before=self.head_text)

        setup = setup_command(folder) if not missing and not run else ""
        if setup:
            self.btn_setup.pack(side="left", padx=(8, 0))

        self.lbl_name.config(text=p["name"])
        self.lbl_meta.config(text=self._meta_text(p))
        cmd_lines = ["> " + p["command"] if p["command"]
                     else "> no launch command yet. Click Edit and set one."]
        if p.get("options"):
            cmd_lines.append("  ▾ " + ", ".join(o["name"] for o in p["options"]))
        if setup:
            cmd_lines.append(f"  Needs setup first: {setup}")
        self.lbl_cmd.config(text="\n".join(cmd_lines))

        body = ""
        if p.get("notes"):
            body += p["notes"] + "\n\n" + "-" * 48 + "\n\n"
        if not missing:
            body += readme_text(folder)
        self._set_text(body.strip() or "No README or notes yet. Add notes with Edit.")

    # -- running things ------------------------------------------------------
    def save(self):
        if not self.can_save:
            self.status.set(f"Not saved: {CONFIG_FILE.name} couldn't be read at startup")
            return
        try:
            save_config(self.cfg)
        except OSError as e:
            messagebox.showerror("Couldn't save library", str(e))

    def log_path(self, p) -> Path:
        safe = re.sub(r"[^\w.-]+", "_", p["name"]).strip("_") or "project"
        return LOG_DIR / f"{safe}-{zlib.crc32(norm_key(p['path']).encode('utf-8')):08x}.log"

    def play_or_stop(self):
        p = self.selected
        if p and norm_key(p["path"]) in self.running:
            self.stop(p)
        else:
            self.launch()

    def _show_options_menu(self):
        p = self.selected
        if not p:
            return
        m = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT,
                    activeforeground="#000000", borderwidth=0)
        m.add_command(label="▶ Play", command=self.launch)
        if p.get("options"):
            m.add_separator()
            for o in p["options"]:
                m.add_command(label=o["name"], command=lambda o=o: self.launch(option=o))
        m.add_separator()
        m.add_command(label="Add or change commands...", command=self.edit_project)
        b = self.btn_more
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def launch(self, option=None, p=None):
        p = p or self.selected
        if not p:
            return
        key = norm_key(p["path"])
        if key in self.running:
            self.status.set(f"{p['name']} is already running. Stop it first.")
            return
        is_setup = bool(option and option.get("setup"))
        name = option["name"] if option else "Play"
        cmd = (option["command"] if option else p["command"]).strip()
        if not cmd:
            if not option and messagebox.askyesno("No command", "No launch command set. Edit it now?"):
                self.edit_project()
            return
        folder = Path(p["path"])
        if not folder.is_dir():
            messagebox.showerror("Folder not found", p["path"])
            return

        env = dict(os.environ)
        env.update(parse_env(p.get("env", "")))
        log = self.log_path(p)
        try:
            LOG_DIR.mkdir(exist_ok=True)
            if log.exists():
                os.replace(log, log.with_suffix(".prev.log"))  # keep the run before this one too
        except OSError:
            pass
        header = (f"== {p['name']}: {name}\n== {cmd}\n== in {folder}\n"
                  f"== started {datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
        # setup always gets a window on Windows so you can watch pip / npm work
        console = IS_WIN and (is_setup or p.get("console", True))
        logf = None
        try:
            if console:
                with open(log, "w", encoding="utf-8") as f:
                    f.write(header + "(Output went to its console window, so it isn't in this log.)\n")
                # own console window; stays open only if the program fails
                proc = subprocess.Popen(
                    f"{cmd} || (echo. & echo It stopped with an error. & pause & exit 1)",
                    cwd=str(folder), shell=True, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                env.setdefault("PYTHONUNBUFFERED", "1")  # so Python output reaches the log as it happens
                logf = open(log, "w", encoding="utf-8", errors="replace")
                logf.write(header)
                logf.flush()
                kw = dict(NO_WINDOW) if IS_WIN else {"start_new_session": True}
                proc = subprocess.Popen(cmd, cwd=str(folder), shell=True, env=env,
                                        stdin=subprocess.DEVNULL, stdout=logf,
                                        stderr=subprocess.STDOUT, **kw)
        except OSError as e:
            if logf:
                logf.close()
            messagebox.showerror("Launch failed", str(e))
            return

        start = time.time()
        self.running[key] = {"proc": proc, "start": start, "name": name, "project": p,
                             "setup": is_setup, "stopped": False}
        if not is_setup:
            p["runs"] = p.get("runs", 0) + 1
            p["last_run"] = datetime.now().isoformat(timespec="seconds")
            self.save()
        self.refresh()
        if is_setup:
            self.status.set(f"Setting up {p['name']}...")
        else:
            self.status.set(f"Launched {p['name']}" + ("" if name == "Play" else f" ({name})"))
        threading.Thread(target=self._wait, args=(key, proc, logf, log, start), daemon=True).start()

    def _wait(self, key, proc, logf, log, start):
        code = proc.wait()
        if logf:
            logf.close()
        dur = time.time() - start
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n== exited with code {code} after {fmt_duration(dur)}\n")
        except OSError:
            pass
        self.call_soon(self._run_ended, key, code, dur)

    def _run_ended(self, key, code, dur):
        run = self.running.pop(key, None)
        if run is None:
            return
        p = run["project"]
        if any(q is p for q in self.projects):
            p["last_exit"] = code
            if not run["setup"]:
                p["playtime"] = p.get("playtime", 0) + dur
            elif code == 0:
                self._after_setup(p)
            self.save()
        self.refresh()
        if run["stopped"]:
            self.status.set(f"Stopped {p['name']} after {fmt_duration(dur)}")
        elif code == 0:
            self.status.set(f"{p['name']} is set up and ready to play" if run["setup"]
                            else f"{p['name']} finished after {fmt_duration(dur)}")
        else:
            self.status.set(f"{p['name']} exited with code {code} after {fmt_duration(dur)}")
            if run["setup"]:
                messagebox.showerror(
                    "Setup didn't finish",
                    f"Setting up {p['name']} stopped with an error (code {code}). "
                    "Check the window it ran in, or click Log.")

    def _after_setup(self, p):
        """A fresh venv now exists, so point plain 'python ...' commands at it."""
        folder = Path(p["path"])
        py = find_python(folder)

        def swap(c):
            for prefix in ("python ", "python3 "):
                if c.startswith(prefix):
                    return f"{py} {c[len(prefix):]}"
            return c

        p["command"] = swap(p.get("command", "")) or detect_command(folder)
        for o in p.get("options", []):
            o["command"] = swap(o["command"])

    def stop(self, p=None):
        p = p or self.selected
        run = self.running.get(norm_key(p["path"])) if p else None
        if not run:
            return
        run["stopped"] = True
        proc = run["proc"]
        try:
            if IS_WIN:  # kill the whole tree: the shell, and whatever it started
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15, **NO_WINDOW)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        self.status.set(f"Stopping {p['name']}...")

    def run_setup(self, p=None):
        p = p or self.selected
        if not p:
            return
        cmd = setup_command(Path(p["path"]))
        if not cmd:
            messagebox.showinfo("Nothing to set up", f"{p['name']} already looks ready to run.")
            return
        self.launch(option={"name": "Set up", "command": cmd, "setup": True}, p=p)

    # -- other actions -------------------------------------------------------
    def open_path(self, path):
        path = str(path)
        if IS_WIN:
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_log(self):
        p = self.selected
        if not p:
            return
        log = self.log_path(p)
        if not log.exists():
            messagebox.showinfo("No log yet", f"{p['name']} hasn't been run from here yet.")
            return
        try:
            self.open_path(log)
        except OSError as e:
            messagebox.showerror("Couldn't open the log", str(e))

    def open_folder(self):
        p = self.selected
        if not p:
            return
        if not Path(p["path"]).is_dir():
            messagebox.showerror("Folder not found", p["path"])
            return
        self.open_path(p["path"])

    def open_editor(self):
        """Open the project folder in VS Code (or whatever 'editor' is set to in the config)."""
        p = self.selected
        if not p:
            return
        if not Path(p["path"]).is_dir():
            messagebox.showerror("Folder not found", p["path"])
            return
        editor = self.cfg.get("editor") or "code"
        if not shutil.which(editor) and not Path(editor).is_file():
            messagebox.showerror(
                "Editor not found",
                f"Couldn't find '{editor}'. Reinstall VS Code with 'Add to PATH' ticked, or put "
                f"the full path to your editor in the \"editor\" entry of {CONFIG_FILE.name}.")
            return
        try:
            subprocess.Popen(f'{_q(editor)} "{p["path"]}"', shell=True, **NO_WINDOW)
        except OSError as e:
            messagebox.showerror("Couldn't open editor", str(e))
            return
        self.status.set(f"Opened {p['name']} in {Path(editor).stem}")

    def open_github(self):
        p = self.selected
        if not p:
            return
        url = github_url(Path(p["path"]))
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("No remote found",
                                "This folder has no .git/config with a GitHub remote.")

    def toggle_favorite(self):
        p = self.selected
        if not p:
            return
        p["favorite"] = not p.get("favorite")
        self.save()
        self.refresh()

    def git_pull(self):
        p = self.selected
        if not p:
            return
        if not is_repo(Path(p["path"])):
            messagebox.showinfo("Not a git repo", f"{p['path']} has no .git folder.")
            return
        self.status.set(f"Pulling {p['name']}...")

        def work():
            try:
                r = subprocess.run(["git", "pull"], cwd=p["path"], capture_output=True,
                                   text=True, timeout=180, env=git_env(), **NO_WINDOW)
                out = (r.stdout + r.stderr).strip() or "Done."
            except Exception as e:  # git missing, timeout, etc.
                out = f"git pull failed: {e}"
            self.call_soon(self._pull_done, p, out)

        threading.Thread(target=work, daemon=True).start()

    def _pull_done(self, p, out):
        self.status.set(f"{p['name']}: {out.splitlines()[-1][:110]}")
        messagebox.showinfo(f"git pull: {p['name']}", out)
        self.refresh_git()

    def pull_all(self):
        if self.pulling:
            return
        if not shutil.which("git"):
            messagebox.showerror("git not found", "git isn't installed or isn't on PATH.")
            return
        repos = [p for p in self.projects if Path(p["path"]).is_dir() and is_repo(Path(p["path"]))]
        if not repos:
            messagebox.showinfo("No repos", "None of your projects are git repos.")
            return
        self.pulling = True

        def work():
            updated, current, problems = [], 0, []
            for n, p in enumerate(repos, 1):
                self.call_soon(self.status.set, f"Pulling {n} of {len(repos)}: {p['name']}...")
                try:
                    # --ff-only: never creates a merge commit behind your back
                    r = subprocess.run(["git", "pull", "--ff-only"], cwd=p["path"], capture_output=True,
                                       text=True, timeout=180, env=git_env(), **NO_WINDOW)
                    out = (r.stdout + r.stderr).strip()
                    if r.returncode:
                        problems.append(f"{p['name']}: {out.splitlines()[-1] if out else 'failed'}")
                    elif "up to date" in out.lower() or "up-to-date" in out.lower():
                        current += 1
                    else:
                        updated.append(p["name"])
                except Exception as e:
                    problems.append(f"{p['name']}: {e}")
            self.call_soon(self._pull_all_done, updated, current, problems)

        threading.Thread(target=work, daemon=True).start()

    def _pull_all_done(self, updated, current, problems):
        self.pulling = False
        msg = f"Updated {len(updated)}, {current} already up to date"
        if problems:
            msg += f", {len(problems)} had problems"
        self.status.set(msg)
        detail = msg + "."
        if updated:
            detail += "\n\nUpdated:\n" + "\n".join("   " + n for n in updated)
        if problems:
            detail += "\n\nProblems (usually uncommitted changes or a diverged branch):\n" + \
                      "\n".join("   " + p for p in problems)
        messagebox.showinfo("Pull all", detail)
        self.refresh_git()

    def refresh_git(self):
        """Look up git status for every repo in the background, then redraw."""
        if not shutil.which("git"):
            return
        if self.git_busy:
            self.git_again = True
            return
        self.git_busy = True
        folders = [(norm_key(p["path"]), Path(p["path"])) for p in self.projects
                   if Path(p["path"]).is_dir() and is_repo(Path(p["path"]))]

        def work():
            self.call_soon(self._git_done, {k: git_info(f) for k, f in folders})

        threading.Thread(target=work, daemon=True).start()

    def _git_done(self, results):
        self.git_busy = False
        self.git_info = {k: v for k, v in results.items() if v}
        self.refresh()
        if self.git_again:
            self.git_again = False
            self.refresh_git()

    def quick_launch(self):
        if self.projects:
            QuickLaunch(self)

    def add_project(self):
        dlg = ProjectDialog(self)
        if dlg.result:
            proj = new_project(**dlg.result)
            self.projects.append(proj)
            self.selected = proj
            self.save()
            self.refresh()
            self.refresh_git()

    def edit_project(self):
        p = self.selected
        if not p:
            return
        dlg = ProjectDialog(self, p)
        if dlg.result:
            p.update(dlg.result)
            self._cover_paths.pop(norm_key(p["path"]), None)
            self._images.clear()
            self.save()
            self.refresh()

    def remove_project(self):
        p = self.selected
        if not p:
            return
        if messagebox.askyesno("Remove from library",
                               f"Remove '{p['name']}' from the library?\nNothing on disk is deleted."):
            self.projects.remove(p)
            self.selected = None
            self.save()
            self.refresh()

    def remove_missing(self, intro=""):
        """Offer to drop entries whose folders are gone. Returns True if it asked."""
        missing = [p for p in self.projects if not Path(p["path"]).is_dir()]
        if not missing:
            if not intro:
                messagebox.showinfo("Nothing missing", "Every project's folder is still there.")
            return False
        n = len(missing)
        names = "\n".join("   " + p["name"] for p in missing[:12])
        if n > 12:
            names += f"\n   ...and {n - 12} more"
        question = (f"{n} entr{'y points' if n == 1 else 'ies point'} to a folder that no longer exists:"
                    f"\n\n{names}\n\nRemove {'it' if n == 1 else 'them'} from the library? "
                    "Nothing on disk is touched, but notes and play counts for these entries are lost.")
        if messagebox.askyesno("Missing folders", (intro + "\n\n" if intro else "") + question):
            for p in missing:
                self.projects.remove(p)
            if any(p is self.selected for p in missing):
                self.selected = None
            self.save()
            self.refresh()
            self.status.set(f"Removed {n} missing entr{'y' if n == 1 else 'ies'}")
        return True

    def pin_to_desktop(self, quiet=False):
        if not IS_WIN:
            messagebox.showinfo("Windows only", "Desktop shortcuts are only set up automatically on Windows.")
            return
        try:
            lnk = make_desktop_shortcut()
        except Exception as e:
            messagebox.showerror("Couldn't make the shortcut", str(e))
            return
        self.status.set("Shortcut added to your desktop")
        if not quiet:
            messagebox.showinfo("Pinned",
                                f"Added {lnk}\n\nDouble-click it to open the launcher with no console window. "
                                "Right-click it and choose Pin to taskbar if you want it there too.")

    def rescan(self):
        self.scan_folder(self.cfg.get("projects_dir") or None)

    def guess_github_user(self) -> str:
        """Most common owner across the GitHub remotes already in the library."""
        owners = {}
        for p in self.projects:
            m = re.match(r"https://github\.com/([^/]+)/", github_url(Path(p["path"])))
            if m:
                owners[m.group(1)] = owners.get(m.group(1), 0) + 1
        return max(owners, key=owners.get) if owners else ""

    def group_for(self, folder: Path) -> str:
        """Group name for a folder, from where it sits under the scanned projects folder."""
        root = self.cfg.get("projects_dir")
        if root:
            try:
                rel = Path(folder).resolve().parent.relative_to(Path(root).resolve())
                return "/".join(rel.parts)
            except ValueError:
                pass
        return ""

    def add_folder(self, folder: Path, commit: bool = True):
        """Add one folder to the library (unless it's there already). Returns the new
        project, or None. Pass commit=False when adding a batch, then save() and
        refresh() once."""
        key = norm_key(folder)
        if any(norm_key(p["path"]) == key for p in self.projects):
            return None
        cmd = detect_command(folder)
        proj = new_project(folder.name, folder, cmd, group=self.group_for(folder),
                           options=detect_options(folder, cmd))
        self.projects.append(proj)
        self.selected = proj
        if commit:
            self.save()
            self.refresh()
        return proj

    def scan_folder(self, root=None):
        if self.scanning:
            return
        if not root:
            root = filedialog.askdirectory(title="Folder that holds all your projects")
            if not root:
                return
        root = Path(root)
        if not root.is_dir():
            messagebox.showerror("Folder not found", str(root))
            return
        self.cfg["projects_dir"] = str(root)
        self.save()

        # snapshot what the worker needs, so it never touches self.projects
        known = {}
        for p in self.projects:
            if p["command"]:
                state = "project"
            elif has_user_data(p):
                state = "kept"
            else:
                state = "placeholder"
            known[norm_key(p["path"])] = state

        self.scanning = True
        for b in self.scan_btns:
            b.config(state="disabled")
        self.status.set(f"Scanning {root}...")
        skip = {norm_key(APP_DIR)}  # don't add the launcher to its own library

        def work():
            try:
                self.call_soon(self._scan_done, root, plan_scan(root, known, skip), None)
            except Exception as e:
                self.call_soon(self._scan_done, root, [], e)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, root, steps, error):
        self.scanning = False
        for b in self.scan_btns:
            b.config(state="normal")
        if error:
            self.status.set("Scan failed")
            messagebox.showerror("Scan failed", f"Couldn't scan {root}:\n{error}")
            return

        by_key = {norm_key(p["path"]): p for p in self.projects}
        added = no_cmd = replaced = kept = 0
        for step, key, folder, cmd, group, opts in steps:
            p = by_key.get(key)  # re-checked: the library may have changed during the scan
            if step == "new":
                if p is None:
                    p = new_project(folder.name, folder, cmd, group=group, options=opts)
                    self.projects.append(p)
                    by_key[key] = p
                    added += 1
                    no_cmd += not cmd
            elif step == "existing":
                if p is not None and group and not p.get("group"):
                    p["group"] = group
            elif step == "replace":
                if p is not None and not p["command"] and not has_user_data(p):
                    self.projects.remove(p)
                    del by_key[key]
                    if p is self.selected:
                        self.selected = None
                    replaced += 1
            elif step == "kept":
                kept += 1
        self._cover_paths.clear()
        self._images.clear()
        self.save()
        self.refresh()
        self.refresh_git()

        msg = f"Added {added} new project{'s' if added != 1 else ''} from {root}."
        if replaced:
            msg += (f"\n\n{replaced} old entr{'y' if replaced == 1 else 'ies'} "
                    "turned out to be a folder of projects and got replaced by what's inside.")
        if kept:
            msg += (f"\n\n{kept} entr{'y looks' if kept == 1 else 'ies look'} like a folder of projects "
                    "but you've added notes, tags, or plays, so "
                    f"{'it was' if kept == 1 else 'they were'} left alone. Remove one and Rescan "
                    "to split it into its projects.")
        if no_cmd:
            msg += (f"\n\n{no_cmd} had no obvious way to run them. "
                    "Select each one and click Edit to set a command.")
        self.status.set(msg.splitlines()[0])
        if not self.remove_missing(intro=msg):
            messagebox.showinfo("Scan finished", msg)


if __name__ == "__main__":
    Launcher().mainloop()