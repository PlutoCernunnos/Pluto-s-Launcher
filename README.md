# Launcher

A Steam-style library for your own projects. Point it at the folder where your repos live and it turns each one into an entry you can play, open, update, and make notes on — games, tools, scripts, whatever, in any language.

Single Python file, no dependencies. Made for Windows, mostly works elsewhere.

## Running it

```
python launcher.py
```

Needs Python 3.8 or newer with tkinter (included in the standard Windows installer). `git` is optional but needed for the Git pull and Get from GitHub features.

On first run, click **Scan folder…** and pick the folder that holds your projects. Then click **Pin to desktop** in the bottom corner to get a desktop icon that opens the launcher without a console window.

## What it does

**Finds your projects.** Scanning walks the folder up to three levels deep. Anything runnable becomes a project; a folder that only holds other projects becomes a group, shown as a collapsible heading in the list. Run it again any time to pick up new folders — existing entries are left alone.

**Guesses how to run each one**, in this order:

1. `run*.bat`, `start*.bat`, `launch*.bat`, `play*.bat`, then any `.bat` or `.cmd`
2. an `.exe`
3. `main.py`, `app.py`, `game.py`, `run.py`, `start.py`, `__main__.py`
4. a `.ps1` script
5. a `.sh` script
6. `package.json` → `npm start`, `Cargo.toml` → `cargo run`, `go.mod` → `go run .`
7. a `.py` file named after the folder, or a lone `.py` file

If the project has a `venv`, `.venv`, or `env` folder, that Python is used. Anything it can't work out shows `?` and you set the command yourself with **Edit**.

**Launches them.** Play (or double-click, or Enter) runs the command inside the project folder in its own console window. The window closes on success and stays open if the program fails, so you can read the error. Untick *Show a console window* in Edit for GUI-only apps.

**Per project:** open the folder, open it in VS Code, `git pull`, open the repo on GitHub, see the README and your own notes, play count, last played, tags for searching.

**Get from GitHub…** lists any GitHub user's public repos — yours or anyone else's — marks which ones you already have, and clones the ones you pick straight into your projects folder (or a subfolder, which becomes their group). Add a personal access token to the config to see your private repos too.

## Keyboard

| Key | Does |
|---|---|
| Enter / double-click | Play |
| Delete | Remove from library |
| Ctrl+F | Search |
| Ctrl+N | Add project |

## Config

Everything lives in `launcher_config.json` next to the script. It's created on first run and is git-ignored because it contains local paths and possibly a token.

| Key | What it is |
|---|---|
| `projects_dir` | Folder that Rescan and Get from GitHub use by default |
| `editor` | Command for the VS Code button (`code` by default; any editor's name or full path works) |
| `github_user` | Your GitHub username |
| `github_users` | Usernames you've loaded before, for the dropdown |
| `github_token` | Optional. A GitHub personal access token, used only when viewing your own account, to include private repos |
| `projects` | The library itself |

## Not on Windows?

Detection skips `.bat`, `.exe`, and `.ps1` rules, programs run in the background instead of a new console window, and Pin to desktop isn't available. Everything else works.
