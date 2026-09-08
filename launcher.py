#!/usr/bin/env python3
"""
launcher.py - a Steam-style library for your own projects.

    python launcher.py

"Scan folder..."  -> point it at the folder where your repos live. Every
                     subfolder becomes an entry with an auto-detected launch
                     command (run*.bat, any .bat, .exe, main.py, .ps1,
                     npm start, cargo run, ...).
"Add project"     -> any folder + any command, Python or not.

Per project: Play, Open folder, Git pull, open on GitHub, README preview,
your own notes, play count, last played. Search and sort at the top.

Stdlib only (tkinter). Your library is saved next to this file in
launcher_config.json, so keep the two together.
"""

import base64
import json
import os
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
MAX_DEPTH = 3  # how many folder levels a scan will descend looking for projects

# (glob patterns tried in order, command template, windows-only?)
LAUNCH_RULES = [
    (["run*.bat", "start*.bat", "launch*.bat", "play*.bat", "*.bat", "*.cmd"],
     "call {f}", True),
    (["*.exe"], "{f}", True),
    (["main.py", "app.py", "game.py", "run.py", "start.py", "__main__.py"],
     "{py} {f}", False),
    (["*.ps1"], "powershell -ExecutionPolicy Bypass -File {f}", True),
    (["*.sh"], "bash {f}", False),
    (["package.json"], "npm start", False),
    (["Cargo.toml"], "cargo run", False),
    (["go.mod"], "go run .", False),
]


# ---- helpers ---------------------------------------------------------------
def _q(name: str) -> str:
    return f'"{name}"' if " " in name else name


def find_python(folder: Path) -> str:
    """Prefer a project-local venv, otherwise whatever 'python' is on PATH."""
    for v in ("venv", ".venv", "env"):
        exe = folder / v / ("Scripts/python.exe" if IS_WIN else "bin/python")
        if exe.exists():
            return f'"{exe}"'
    return "python" if IS_WIN else "python3"


def detect_command(folder: Path) -> str:
    """Best guess at how to run whatever lives in `folder`. '' if no idea."""
    for patterns, template, win_only in LAUNCH_RULES:
        if win_only and not IS_WIN:
            continue
        for pat in patterns:
            hits = sorted(p for p in folder.glob(pat) if p.is_file())
            if hits:
                return template.format(f=_q(hits[0].name), py=find_python(folder))
    same = folder / f"{folder.name}.py"
    if same.is_file():
        return f"{find_python(folder)} {_q(same.name)}"
    pys = [p for p in folder.glob("*.py") if p.is_file()]
    if len(pys) == 1:
        return f"{find_python(folder)} {_q(pys[0].name)}"
    return ""


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
    cfg = folder / ".git" / "config"
    if not cfg.exists():
        return ""
    m = re.search(r"url\s*=\s*(\S+)", cfg.read_text(errors="ignore"))
    if not m:
        return ""
    url = re.sub(r"^git@([^:]+):", r"https://\1/", m.group(1))
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


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"projects_dir": "", "editor": "code", "github_user": "", "github_token": "",
            "projects": []}


def fetch_github_repos(user: str, token: str = "") -> list:
    """Repos for `user` from the GitHub API. Public only, unless a token is given
    (then it lists everything the token's owner has, private included)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "launcher.py"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        base = "https://api.github.com/user/repos?affiliation=owner&sort=pushed&per_page=100"
    else:
        base = f"https://api.github.com/users/{user}/repos?sort=pushed&per_page=100"
    repos = []
    for page in range(1, 6):  # up to 500 repos
        req = urllib.request.Request(f"{base}&page={page}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.load(r)
        repos.extend(batch)
        if len(batch) < 100:
            break
    return repos


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def new_project(name, path, command="", **extra) -> dict:
    p = {"name": name, "path": str(path), "command": command, "group": "",
         "tags": "", "notes": "", "console": True, "runs": 0, "last_run": ""}
    p.update(extra)
    return p


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
        # a token lists *its owner's* repos, so only use it when looking at your own account
        own = self.app.cfg.get("github_user", "")
        if user and own and user.lower() != own.lower():
            token = ""
        elif user:
            self.app.cfg["github_user"] = user
        users = self.app.cfg.setdefault("github_users", [])
        if user and user not in users:
            users.append(user)
            self.user_box.config(values=users)
        self.app.save()
        self.set_msg(f"Loading repos for {user or 'your token'}...")
        self.btn_get.config(state="disabled")

        def work():
            try:
                repos = fetch_github_repos(user, token)
                self.after(0, self.show_repos, repos)
            except urllib.error.HTTPError as e:
                msg = {404: f"No GitHub user called '{user}'.",
                       403: "GitHub is rate-limiting this computer (60 lookups an hour without a token). Try again later.",
                       401: f"GitHub rejected the token in {CONFIG_FILE.name}."}.get(
                    e.code, f"GitHub answered {e.code} {e.reason}.")
                self.after(0, self.fail, msg)
            except Exception as e:
                msg = f"Couldn't reach GitHub: {e}"
                self.after(0, self.fail, msg)

        threading.Thread(target=work, daemon=True).start()

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
            pushed = (r.get("pushed_at") or "").rstrip("Z")
            self.tree.insert("", "end", iid=str(i), text=r["name"],
                             values=(r.get("language") or "", fmt_date(pushed),
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
            for n, r in enumerate(picked, 1):
                target = dest / r["name"]
                self.after(0, self.set_msg, f"Downloading {r['name']} ({n} of {len(picked)})...")
                if target.exists():
                    results.append((r, target, "ok"))  # already on disk; just add it
                    continue
                try:
                    res = subprocess.run(["git", "clone", r["clone_url"], str(target)],
                                         capture_output=True, text=True, timeout=900, **kw)
                    err = res.stderr.strip().splitlines()
                    results.append((r, target, "ok" if res.returncode == 0
                                    else (err[-1] if err else "git clone failed")))
                except Exception as e:
                    results.append((r, target, f"failed: {e}"))
            self.after(0, self.finish, results)

        threading.Thread(target=work, daemon=True).start()

    def finish(self, results):
        self.busy = False
        self.btn_get.config(state="normal")
        added, problems = 0, []
        for r, target, result in results:
            if result == "ok":
                added += self.app.add_folder(target)
            else:
                problems.append(f"{r['name']}: {result}")
        msg = f"Added {added} project{'s' if added != 1 else ''} to the library."
        if problems:
            msg += "\n\nProblems:\n" + "\n".join(problems)
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
        self.cfg = load_config()
        self.projects = self.cfg.setdefault("projects", [])
        self.filtered = []
        self.selected = None
        self._style()
        self._build()
        self.refresh()

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
        ttk.Button(top, text="Scan folder...", command=self.scan_folder).pack(side="right", padx=6)
        ttk.Button(top, text="Rescan", command=self.rescan).pack(side="right")
        ttk.Button(top, text="Get from GitHub...",
                   command=lambda: GitHubDialog(self)).pack(side="right", padx=(0, 6))

        self.status = tk.StringVar(value="")
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status, style="Status.TLabel",
                  padding=(14, 4)).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Pin to desktop", command=self.pin_to_desktop).pack(side="right", padx=14, pady=4)

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
        if not (Path(p["path"]) / ".git").exists():
            messagebox.showinfo("Not a git repo", f"{p['path']} has no .git folder.")
            return
        self.status.set(f"Pulling {p['name']}...")

        def work():
            try:
                kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
                r = subprocess.run(["git", "pull"], cwd=p["path"], capture_output=True,
                                   text=True, timeout=180, **kw)
                out = (r.stdout + r.stderr).strip() or "Done."
            except Exception as e:  # git missing, timeout, etc.
                out = f"git pull failed: {e}"
            self.after(0, lambda: self._pull_done(p, out))

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

    def add_folder(self, folder: Path) -> bool:
        """Add one folder to the library (unless it's there already). True if added."""
        key = os.path.normcase(os.path.normpath(str(folder)))
        if any(os.path.normcase(os.path.normpath(p["path"])) == key for p in self.projects):
            return False
        proj = new_project(folder.name, folder, detect_command(folder), group=self.group_for(folder))
        self.projects.append(proj)
        self.selected = proj
        self.save()
        self.refresh()
        return True

    def scan_folder(self, root=None):
        if not root:
            root = filedialog.askdirectory(title="Folder that holds all your projects")
            if not root:
                return
        root = Path(root)
        if not root.is_dir():
            messagebox.showerror("Folder not found", str(root))
            return
        self.cfg["projects_dir"] = str(root)
        known = {os.path.normcase(os.path.normpath(p["path"])): p for p in self.projects}
        stats = {"added": 0, "no_cmd": 0, "replaced": 0}
        self._scan(root, "", 0, known, stats)
        self.save()
        self.refresh()
        added = stats["added"]
        msg = f"Added {added} new project{'s' if added != 1 else ''} from {root}."
        if stats["replaced"]:
            msg += (f"\n\n{stats['replaced']} old entr{'y' if stats['replaced'] == 1 else 'ies'} "
                    "turned out to be a folder of projects and got replaced by what's inside.")
        if stats["no_cmd"]:
            msg += (f"\n\n{stats['no_cmd']} had no obvious way to run them. "
                    "Select each one and click Edit to set a command.")
        self.status.set(msg.splitlines()[0])
        messagebox.showinfo("Scan finished", msg)

    @staticmethod
    def _subdirs(folder):
        try:
            return sorted((s for s in folder.iterdir() if s.is_dir()
                           and not s.name.startswith(".") and s.name not in SKIP_DIRS),
                          key=lambda s: s.name.lower())
        except OSError:
            return []

    def _scan(self, folder, group, depth, known, stats):
        """Walk `folder`. Runnable folders become projects; folders that just hold
        other projects become a group and get walked in turn."""
        for d in self._subdirs(folder):
            key = os.path.normcase(os.path.normpath(str(d)))
            cmd = detect_command(d)
            kids = self._subdirs(d)
            # A folder is a *group* (not a project) if nothing runs at its top level
            # and it is either a direct child of the scan root or holds something
            # that looks like a project (runnable, or its own git repo).
            is_group = (not cmd and kids and depth < MAX_DEPTH and
                        (depth == 0 or any(detect_command(k) or (k / ".git").is_dir() for k in kids)))

            existing = known.get(key)
            if existing is not None:
                if is_group and not existing["command"]:
                    # was added as a '?' placeholder by an older scan; its contents are the real projects
                    self.projects.remove(existing)
                    del known[key]
                    stats["replaced"] += 1
                else:
                    if group and not existing.get("group"):
                        existing["group"] = group
                    continue

            if is_group:
                self._scan(d, f"{group}/{d.name}" if group else d.name, depth + 1, known, stats)
                continue

            self.projects.append(new_project(d.name, d, cmd, group=group))
            stats["added"] += 1
            stats["no_cmd"] += not cmd


if __name__ == "__main__":
    Launcher().mainloop()
