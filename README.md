# Pluto's Launcher

A Steam-style library for your own projects. Point it at the folder where your repos live and it turns each one into an entry you can play, stop, set up, update, screenshot and make notes on: games, tools, scripts, whatever, in any language.

Single Python file, no dependencies. Made for Windows, mostly works elsewhere.

## Getting started

1. Install [Python 3.8 or newer](https://www.python.org/downloads/). Tick **Add python.exe to PATH** in the installer. tkinter comes with it.
2. Download this repo (green **Code** button → **Download ZIP**, or `git clone`).
3. Double-click **`Start Launcher.bat`**.

That's it, no terminal or code editor needed. If Python is missing, the .bat says so and offers to install it for you with winget.

The first time it opens, a welcome screen asks three things:

- **Where your projects live.** It guesses the folder the launcher sits in if there are other repos next to it.
- **Your GitHub username.** This is optional and used by Get from GitHub.
- **Whether to add a desktop shortcut.** After that, opening the launcher is one double-click.

`git` is optional, but you need it for Get from GitHub, Git pull, Pull all, git status and updates.

## What it does

### Finds your projects

**Scan folder…** walks the folder up to three levels deep. Anything runnable becomes a project. A folder that only holds other projects becomes a group, shown as a heading. Run **Rescan** any time to pick up new folders. Existing entries, along with their notes and playtime, are left alone.

At the end of a scan, it offers to remove entries whose folders no longer exist. **Remove missing** in the bottom bar does the same thing whenever you like.

### Guesses how to run each one

It checks these in order and uses the first match:

1. `run*`, `start*`, `launch*` or `play*` `.bat`/`.cmd`
2. an `.exe` (installers and uninstallers are skipped)
3. `main.py`, `app.py`, `game.py`, `run.py`, `start.py`, `__main__.py`
4. a `.ps1` script
5. a `.sh` script
6. `package.json` → `npm start` (or `npm run dev` if there's no start script), `Cargo.toml` → `cargo run`, `go.mod` → `go run .`
7. a `.py` file named after the folder, or a lone `.py` file
8. any other `.bat` or `.cmd`

If the project has a `venv`, `.venv` or `env` folder, that Python is used. Anything it can't work out shows `?`, and you set the command yourself with **Edit**.

It also finds **extra commands**, such as running tests (pytest or unittest, `npm test`, `cargo test`, `go test`), `npm run dev`, `npm run build`, `cargo build --release` and `make`. They're under the **▾** next to Play. You can add your own in Edit, one per line, written as `Name: command`.

### Launches them

**Play** (or double-click, or Enter) runs the command inside the project folder in its own console window. The window closes when the program finishes and stays open if it fails, so you can read the error.

While it runs, Play turns into **Stop**, which closes the program and anything it started. The list marks it with ▶, and you can't start the same project twice by accident.

Untick *Show a console window* in Edit for GUI apps. Their output goes to a log instead.

### Sets up fresh downloads

If a project has a `requirements.txt` but no venv, or a `package.json` but no `node_modules`, a **Set up** button appears. It runs the install for you:

- For Python, it makes a `.venv` and pip-installs the requirements, then points the project's commands at it.
- For Node, it runs `npm install`.

After **Get from GitHub** downloads anything that needs this, it offers to set it all up straight away.

### Keeps track

- **Playtime** shows total and per project. Sort by it, or see it in Stats.
- **Log** shows the output and exit code of the last run, with the run before it kept as `.prev.log`. Console-window runs only log the command, time and exit code, since their output went to the window.
- **Screenshot ▾** captures a running project. Click into its window and the shot is taken after 3 seconds. Screenshots are saved per project, and the latest one becomes the cover if the project has no other.
- **Git column** shows ✎ for uncommitted changes and ↓/↑ for commits behind or ahead. The ↓ count is as fresh as your last fetch or pull. **Pull all** updates every repo, fast-forward only, so it never makes merge commits.
- **Changed column** shows the latest commit or file edit, for git and non-git projects alike.

### Finds things fast

- **Search** matches name, group, tags and path. Ctrl+F jumps to it.
- **Sort** by name, recently played, recently changed, most played, playtime or kind.
- **Show** filters to All, Favorites, Recently changed (the last 7 days), Running or Hidden.
- **Favorites** (☆ in the project panel) always sit at the top.
- **Hidden** projects (set in Edit) stay out of the list but are still in Ctrl+K.
- **Ctrl+K** opens quick launch. Type a few letters of a name and press Enter to play it.
- **Grid view** shows cover art as tiles. Covers come from `icon.png`, `logo.png`, `cover.png` and similar files, from the README's first image, or from your screenshots. You can also pick one in Edit. Only PNG and GIF images work, because that's what tkinter can show.

### Per project

Each project panel has buttons to open the folder, open it in VS Code, run `git pull` and open the repo on GitHub. It also shows the README with your own notes, tags, environment variables (`KEY=value` per line, set in Edit), play count, last played and the last exit code.

### Menu

**☰ Menu** at the top right has the following:

- **New project from template.** The templates are a Python game (a small arrow-keys game to build on), Python script, Python app with a venv, Node app, Batch script, and an empty folder. It can also start a git repo and open the new project in VS Code.
- **Stats.** Total playtime, launches, playtime per week for the last 12 weeks, most played, and a "gathering dust" list of projects not played or changed in 90 days.
- **Export library and Import library.** These move your library, with notes, playtime and favorites, to another PC. Your GitHub token is never exported. Importing offers to fix paths if your projects folder is somewhere else, then merges or replaces. Your current library is backed up first either way.
- **Open backups folder.** The launcher saves a copy of your library there once a day and keeps the last 10.
- **Theme.** Choose from Steam, Midnight, Forest, Ember, Light and High contrast.
- **Check for updates.** The launcher also checks by itself twice a day and shows **⬆ Update available** when there's something new.
  - **If you cloned the repo with git,** updating runs `git pull`.
  - **If you downloaded the zip,** it compares its files with this repo, keeps the old ones in `backups/`, swaps in the new ones, and restarts.

### Get from GitHub…

This lists any GitHub user's public repos, yours or anyone else's. It marks the ones you already have and clones the ones you pick into your projects folder, or into a subfolder that becomes their group.

To see your private repos too, add a personal access token to the config. It's only ever used for your own account.

## Keyboard

| Key | Does |
| --- | --- |
| Enter / double-click | Play |
| Delete | Remove from library (nothing on disk is deleted) |
| Ctrl+K | Quick launch |
| Ctrl+F | Search |
| Ctrl+N | Add project |
| F5 | Refresh git status and recent changes |
| Esc | Close a dialog |

## Config

Everything lives in `launcher_config.json` next to the script. It's created on first run and is git-ignored, because it contains local paths and possibly a token. Saves go through a temp file, so a crash can't corrupt it. If the file ever can't be read, it's copied to `launcher_config.broken-<date>.json` instead of being overwritten.

| Key | What it is |
| --- | --- |
| `projects_dir` | Folder that Rescan, Get from GitHub and New project use by default |
| `editor` | Command for the VS Code button (`code` by default; any editor's name or full path works) |
| `github_user` | Your GitHub username |
| `github_users` | Usernames you've loaded before, for the dropdown |
| `github_token` | Optional. A GitHub personal access token, used only for your own account, to include private repos |
| `theme`, `view`, `show` | Your theme, list or grid view, and Show filter |
| `projects` | The library itself |

## Files it makes

These all sit next to `launcher.py`, and none of them end up in git:

- `launcher_config.json` is your library.
- `logs/` holds the last two runs of each project.
- `backups/` holds daily library backups and old versions kept by updates.
- `screenshots/` holds your screenshots, one folder per project.
- `launcher.ico` is the desktop shortcut's icon.

The three folders each contain their own `.gitignore`, so git skips them without any setup.

## Not on Windows?

Start it with `python3 launcher.py`.

- **Detection** skips the `.bat`, `.exe` and `.ps1` rules.
- **Programs** run in the background, with their output in the log, instead of in a new console window.
- **Pin to desktop** isn't available.
- **Screenshots** capture the whole screen on macOS and need `gnome-screenshot` or `scrot` on Linux.

Everything else works.