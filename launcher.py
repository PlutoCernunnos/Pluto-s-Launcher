#!/usr/bin/env python3
"""
launcher.py - a Steam-style library for your own projects.

    python launcher.py

"Scan folder..."  -> point it at the folder where your repos live. Every
                     subfolder becomes an entry with an auto-detected launch
                     command (run*.bat, .exe, main.py, .ps1, npm start,
                     cargo run, ...).
"Add project"     -> any folder + any command, Python or not.

Per project: Play, Open folder, Git pull, open on GitHub, README preview,
your own notes, play count, last played. Search and sort at the top.

Stdlib only (tkinter). Your library is saved next to this file in
launcher_config.json, so keep the two together.
"""

import base64
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "launcher_config.json"
IS_WIN = sys.platform.startswith("win")

# ---- theme (Steam-ish blues) ---------------------------------------------
BG, PANEL, PANEL2 = "#171d25", "#1f2a37", "#2f4a68"
FG, DIM, ACCENT = "#c7d5e0", "#8f98a0", "#66c0f4"
PLAY, PLAY_HOT = "#4c8b2f", "#6ab04c"
FONT = ("Segoe UI", 10)
MONO = ("Consolas", 9)

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
    return bool(p.get("notes") or p.get("tags") or p.get("runs"))


def find_python(folder: Path) -> str:
    """Prefer a project-local venv, otherwise whatever 'python' is on PATH."""
    for v in ("venv", ".venv", "env"):
        exe = folder / v / ("Scripts/python.exe" if IS_WIN else "bin/python")
        if exe.exists():
            return f'"{exe}"'
    return "python" if IS_WIN else "python3"


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
    if cmd:
        return cmd
    same = folder / f"{folder.name}.py"
    if same.is_file():
        return f"{find_python(folder)} {_q(same.name)}"
    pys = [p for p in folder.glob("*.py") if p.is_file()]
    if len(pys) == 1:
        return f"{find_python(folder)} {_q(pys[0].name)}"
    return _first_match(folder, LAST_RESORT_RULES)


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


def readme_text(folder: Path, limit: int = 3000) -> str:
    try:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.stem.lower() == "readme":
                txt = p.read_text(errors="ignore").strip()
                return txt[:limit] + ("\n\n[...]" if len(txt) > limit else "")
    except OSError:
        pass
    return ""


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


def utc_to_local(stamp: str) -> str:
    """GitHub's '2026-10-06T18:13:00Z' -> naive local-time ISO string for fmt_date."""
    if not stamp:
        return ""
    try:
        d = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return d.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except ValueError:
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
         "tags": "", "notes": "", "console": True, "runs": 0, "last_run": ""}
    p.update(extra)
    return p


def subdirs(folder: Path):
    try:
        return sorted((s for s in folder.iterdir() if s.is_dir()
                       and not s.name.startswith(".") and s.name not in SKIP_DIRS),
                      key=lambda s: s.name.lower())
    except OSError:
        return []


def plan_scan(root: Path, known: dict) -> list:
    """The slow half of a scan; runs on a worker thread and only reads the disk.

    `known` maps norm_key(path) of each library entry to 'project' (has a command),
    'placeholder' (no command, no notes/tags/plays) or 'kept' (no command, but has
    your data). Returns (step, key, folder, command, group) tuples for the main
    thread to apply."""
    steps = []

    def walk(folder, group, depth):
        for d in subdirs(folder):
            key = norm_key(d)
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
                    steps.append(("replace", key, d, "", group))
                elif is_group and state == "kept":
                    steps.append(("kept", key, d, "", group))
                    continue
                else:
                    steps.append(("existing", key, d, "", group))
                    continue

            if is_group:
                walk(d, f"{group}/{d.name}" if group else d.name, depth + 1)
            else:
                steps.append(("new", key, d, cmd, group))

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
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "PowerShell couldn't create the shortcut")
    return r.stdout.strip()


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
            "console": tk.BooleanVar(value=p.get("console", True)),
        }

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        rows = [("Name", "name"), ("Folder", "path"), ("Group", "group"),
                ("Command", "command"), ("Tags", "tags")]
        for row, (label, key) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            ttk.Entry(body, textvariable=self.v[key], width=54).grid(row=row, column=1, sticky="ew", pady=4)
            if key == "path":
                ttk.Button(body, text="Browse", command=self.browse).grid(row=row, column=2, padx=(6, 0))
            if key == "command":
                ttk.Button(body, text="Detect", command=self.detect).grid(row=row, column=2, padx=(6, 0))

        r = len(rows)
        ttk.Checkbutton(body, text="Show a console window (uncheck for GUI-only apps)",
                        variable=self.v["console"]).grid(row=r, column=1, columnspan=2, sticky="w", pady=4)

        ttk.Label(body, text="Notes").grid(row=r + 1, column=0, sticky="nw", pady=4)
        self.notes = tk.Text(body, height=5, width=54, bg=PANEL, fg=FG, insertbackground=FG,
                             relief="flat", wrap="word", font=FONT, padx=6, pady=4)
        self.notes.grid(row=r + 1, column=1, columnspan=2, sticky="ew", pady=4)
        self.notes.insert("1.0", p.get("notes", ""))

        ttk.Label(body, foreground=DIM, wraplength=440, justify="left",
                  text="The command runs inside the project folder, e.g. "
                       "call run.bat, python main.py, npm start, game.exe. "
                       "Group is the heading it sits under in the list (leave blank for none). "
                       "Tags are free text for searching (game, tool, wip).").grid(
            row=r + 2, column=1, columnspan=2, sticky="w", pady=(4, 0))

        btns = ttk.Frame(body)
        btns.grid(row=r + 3, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self.ok).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(master)
        self.grab_set()
        self.wait_window()

    def browse(self):
        d = filedialog.askdirectory(title="Pick the project folder")
        if d:
            self.v["path"].set(d)
            if not self.v["name"].get():
                self.v["name"].set(Path(d).name)
            if not self.v["command"].get():
                self.detect(quiet=True)

    def detect(self, quiet=False):
        folder = Path(self.v["path"].get())
        if not folder.is_dir():
            messagebox.showwarning("No folder", "Pick a folder first.", parent=self)
            return
        cmd = detect_command(folder)
        if cmd:
            self.v["command"].set(cmd)
        elif not quiet:
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
            "console": self.v["console"].get(),
            "notes": self.notes.get("1.0", "end").strip(),
        }
        self.destroy()


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
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
            # fail fast instead of waiting on a password prompt nobody can see
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            for n, r in enumerate(picked, 1):
                target = dest / r["name"]
                self.app.call_soon(self.set_msg, f"Downloading {r['name']} ({n} of {len(picked)})...")
                if target.exists():
                    results.append((r, target, "ok"))  # already on disk; just add it
                    continue
                try:
                    res = subprocess.run(["git", "clone", r["clone_url"], str(target)],
                                         capture_output=True, text=True, timeout=900,
                                         env=env, **kw)
                    err = res.stderr.strip().splitlines()
                    results.append((r, target, "ok" if res.returncode == 0
                                    else (err[-1] if err else "git clone failed")))
                except Exception as e:
                    results.append((r, target, f"failed: {e}"))
            self.app.call_soon(self.finish, results)

        threading.Thread(target=work, daemon=True).start()

    def finish(self, results):
        # library work first, so it still happens if this dialog was closed mid-download
        added, problems = 0, []
        for r, target, result in results:
            if result == "ok":
                added += self.app.add_folder(target, commit=False)
            else:
                problems.append(f"{r['name']}: {result}")
        if added:
            self.app.save()
            self.app.refresh()
        self.busy = False
        msg = f"Added {added} project{'s' if added != 1 else ''} to the library."
        if problems:
            msg += "\n\nProblems:\n" + "\n".join(problems)
        if not self.winfo_exists():
            self.app.status.set(msg.splitlines()[0])
            return
        self.btn_get.config(state="normal")
        self.set_msg(msg.splitlines()[0])
        self.show_repos(self.repos)
        messagebox.showinfo("Download finished", msg, parent=self)


# ---- main window -----------------------------------------------------------
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Launcher")
        self.geometry("1060x660")
        self.minsize(840, 520)
        self.configure(bg=BG)

        self.cfg, config_warning, self.can_save = load_config()
        self.projects = self.cfg.setdefault("projects", [])
        self.filtered = []
        self.selected = None
        self.scanning = False

        # worker threads hand results back through this; only the main thread touches tkinter
        self._calls = queue.Queue()
        self.after(50, self._pump)

        self._style()
        self._build()
        self.refresh()
        if config_warning:
            self.after(200, lambda: messagebox.showwarning("Library file problem", config_warning))

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
        s.configure("Play.TButton", background=PLAY, foreground="#ffffff",
                    font=("Segoe UI", 11, "bold"), padding=(20, 8))
        s.map("Play.TButton", background=[("active", PLAY_HOT)], foreground=[("active", "#ffffff")])
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
        self.search_entry = ttk.Entry(top, textvariable=self.search, width=26)
        self.search_entry.pack(side="left")

        ttk.Label(top, text="Sort").pack(side="left", padx=(16, 6))
        self.sort = tk.StringVar(value="Name")
        cb = ttk.Combobox(top, textvariable=self.sort, state="readonly", width=16,
                          values=["Name", "Recently played", "Most played", "Kind"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())

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

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        left = ttk.Frame(pane)
        self.tree = ttk.Treeview(left, columns=("kind", "last"), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="Project", anchor="w")
        self.tree.heading("kind", text="Kind")
        self.tree.heading("last", text="Last played")
        self.tree.column("#0", width=240, anchor="w")
        self.tree.column("kind", width=90, anchor="w", stretch=False)
        self.tree.column("last", width=110, anchor="w", stretch=False)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", lambda e: self.launch())
        self.tree.bind("<Delete>", lambda e: self.remove_project())

        right = ttk.Frame(pane, style="Panel.TFrame", padding=20)
        self.lbl_name = ttk.Label(right, style="Title.TLabel", wraplength=560)
        self.lbl_name.pack(anchor="w")
        self.lbl_meta = ttk.Label(right, style="Dim.TLabel", wraplength=560, justify="left")
        self.lbl_meta.pack(anchor="w", pady=(4, 12))

        btns = ttk.Frame(right, style="Panel.TFrame")
        btns.pack(fill="x", pady=(0, 10))
        self.btn_play = ttk.Button(btns, text="Play", style="Play.TButton", command=self.launch)
        self.btn_play.pack(side="left")
        self.action_btns = []
        for text, cmd in (("Open folder", self.open_folder), ("VS Code", self.open_editor),
                          ("Git pull", self.git_pull), ("GitHub", self.open_github),
                          ("Edit", self.edit_project), ("Remove", self.remove_project)):
            b = ttk.Button(btns, text=text, command=cmd)
            b.pack(side="left", padx=(6, 0))
            self.action_btns.append(b)

        self.lbl_cmd = ttk.Label(right, style="Cmd.TLabel", wraplength=560, justify="left")
        self.lbl_cmd.pack(anchor="w", pady=(0, 10))

        self.txt = tk.Text(right, bg=BG, fg=FG, relief="flat", wrap="word", padx=12, pady=10,
                           state="disabled", font=FONT, highlightthickness=0)
        self.txt.pack(fill="both", expand=True)

        pane.add(left, weight=2)
        pane.add(right, weight=3)

        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Control-n>", lambda e: self.add_project())

    # -- list + details ------------------------------------------------------
    def refresh(self):
        q = self.search.get().lower().strip()
        items = [p for p in self.projects if not q or q in
                 f"{p['name']} {p.get('group', '')} {p.get('tags', '')} {p['path']}".lower()]
        key = self.sort.get()
        if key == "Recently played":
            items.sort(key=lambda p: p.get("last_run", ""), reverse=True)
        elif key == "Most played":
            items.sort(key=lambda p: p.get("runs", 0), reverse=True)
        elif key == "Kind":
            items.sort(key=lambda p: (kind_of(p["command"]), p["name"].lower()))
        else:
            items.sort(key=lambda p: p["name"].lower())
        self.filtered = items

        self.tree.delete(*self.tree.get_children())
        self.by_iid = {}  # tree row id -> project (group headers are not in here)

        # group headers first (alphabetical), loose projects after them
        counts = {}
        for p in items:
            g = p.get("group", "")
            if g:
                counts[g] = counts.get(g, 0) + 1
        headers = {}
        for n, g in enumerate(sorted(counts, key=str.lower)):
            headers[g] = self.tree.insert("", "end", iid=f"g{n}", open=True,
                                          text=f"{g}  ({counts[g]})", values=("", ""))

        for i, p in enumerate(items):
            iid = f"p{i}"
            name = p["name"] + ("" if Path(p["path"]).is_dir() else "  (missing)")
            self.tree.insert(headers.get(p.get("group", ""), ""), "end", iid=iid, text=name,
                             values=(kind_of(p["command"]), fmt_date(p.get("last_run", ""))))
            self.by_iid[iid] = p

        sel = next((k for k, v in self.by_iid.items() if v is self.selected), None)
        if sel:
            self.tree.selection_set(sel)
            self.tree.see(sel)
        else:
            self.selected = None
            self.show_details(None)
        n, total = len(items), len(self.projects)
        self.status.set(f"{n} of {total} projects" if q else f"{total} projects")

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

    def _set_text(self, s):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", s)
        self.txt.config(state="disabled")

    def show_details(self, p, group=""):
        state = "normal" if p else "disabled"
        self.btn_play.config(state=state)
        for b in self.action_btns:
            b.config(state=state)

        if not p:
            if group:
                self.lbl_name.config(text=group)
                self.lbl_meta.config(text="A folder of projects. Pick one inside it to play.")
            else:
                self.lbl_name.config(text="Nothing selected" if self.projects else "Your library is empty")
                self.lbl_meta.config(text="" if self.projects else
                                     "Click Scan folder and point it at the folder that holds your repos. "
                                     "Each one becomes an entry here with a launch command guessed for it.")
            self.lbl_cmd.config(text="")
            self._set_text("")
            return

        folder = Path(p["path"])
        missing = not folder.is_dir()
        runs = p.get("runs", 0)
        self.lbl_name.config(text=p["name"])
        lines = [f"Played {runs} time{'s' if runs != 1 else ''}, last {fmt_date(p.get('last_run', ''))}"
                 if runs else "Never played"]
        if p.get("group"):
            lines.append("In " + p["group"])
        if p.get("tags"):
            lines.append("Tags: " + p["tags"])
        remote = github_url(folder) if not missing else ""
        if remote:
            lines.append("From " + remote.replace("https://", ""))
        lines.append(str(folder) + ("   (folder not found)" if missing else ""))
        self.lbl_meta.config(text="\n".join(lines))
        self.lbl_cmd.config(text="> " + p["command"] if p["command"]
                            else "> no launch command yet. Click Edit and set one.")

        body = ""
        if p.get("notes"):
            body += p["notes"] + "\n\n" + "-" * 48 + "\n\n"
        if not missing:
            body += readme_text(folder)
        self._set_text(body.strip() or "No README or notes yet. Add notes with Edit.")

    # -- actions -------------------------------------------------------------
    def save(self):
        if not self.can_save:
            self.status.set(f"Not saved: {CONFIG_FILE.name} couldn't be read at startup")
            return
        try:
            save_config(self.cfg)
        except OSError as e:
            messagebox.showerror("Couldn't save library", str(e))

    def launch(self):
        p = self.selected
        if not p:
            return
        cmd = p["command"].strip()
        if not cmd:
            if messagebox.askyesno("No command", "No launch command set. Edit it now?"):
                self.edit_project()
            return
        if not Path(p["path"]).is_dir():
            messagebox.showerror("Folder not found", p["path"])
            return
        try:
            if IS_WIN:
                if p.get("console", True):
                    # own console window; stays open only if the program fails
                    subprocess.Popen(f"{cmd} || pause", cwd=p["path"], shell=True,
                                     creationflags=subprocess.CREATE_NEW_CONSOLE)
                else:
                    subprocess.Popen(cmd, cwd=p["path"], shell=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.Popen(cmd, cwd=p["path"], shell=True)
        except OSError as e:
            messagebox.showerror("Launch failed", str(e))
            return
        p["runs"] = p.get("runs", 0) + 1
        p["last_run"] = datetime.now().isoformat(timespec="seconds")
        self.save()
        self.refresh()
        self.status.set(f"Launched {p['name']}")

    def open_folder(self):
        p = self.selected
        if not p:
            return
        path = p["path"]
        if not Path(path).is_dir():
            messagebox.showerror("Folder not found", path)
            return
        if IS_WIN:
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

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
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
        try:
            subprocess.Popen(f'{_q(editor)} "{p["path"]}"', shell=True, **kw)
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
                kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
                r = subprocess.run(["git", "pull"], cwd=p["path"], capture_output=True,
                                   text=True, timeout=180,
                                   env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), **kw)
                out = (r.stdout + r.stderr).strip() or "Done."
            except Exception as e:  # git missing, timeout, etc.
                out = f"git pull failed: {e}"
            self.call_soon(self._pull_done, p, out)

        threading.Thread(target=work, daemon=True).start()

    def _pull_done(self, p, out):
        self.status.set(f"{p['name']}: {out.splitlines()[-1][:110]}")
        messagebox.showinfo(f"git pull: {p['name']}", out)
        if self.selected is p:
            self.show_details(p)

    def add_project(self):
        dlg = ProjectDialog(self)
        if dlg.result:
            proj = new_project(**dlg.result)
            self.projects.append(proj)
            self.selected = proj
            self.save()
            self.refresh()

    def edit_project(self):
        p = self.selected
        if not p:
            return
        dlg = ProjectDialog(self, p)
        if dlg.result:
            p.update(dlg.result)
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

    def pin_to_desktop(self):
        if not IS_WIN:
            messagebox.showinfo("Windows only", "Desktop shortcuts are only set up automatically on Windows.")
            return
        try:
            lnk = make_desktop_shortcut()
        except Exception as e:
            messagebox.showerror("Couldn't make the shortcut", str(e))
            return
        self.status.set("Shortcut added to your desktop")
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

    def add_folder(self, folder: Path, commit: bool = True) -> bool:
        """Add one folder to the library (unless it's there already). True if added.
        Pass commit=False when adding a batch, then save() and refresh() once."""
        key = norm_key(folder)
        if any(norm_key(p["path"]) == key for p in self.projects):
            return False
        proj = new_project(folder.name, folder, detect_command(folder), group=self.group_for(folder))
        self.projects.append(proj)
        self.selected = proj
        if commit:
            self.save()
            self.refresh()
        return True

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

        def work():
            try:
                self.call_soon(self._scan_done, root, plan_scan(root, known), None)
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
        for step, key, folder, cmd, group in steps:
            p = by_key.get(key)  # re-checked: the library may have changed during the scan
            if step == "new":
                if p is None:
                    p = new_project(folder.name, folder, cmd, group=group)
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
        self.save()
        self.refresh()

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