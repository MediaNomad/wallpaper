# wallpaper

`wallpaper` finds high-resolution, public-domain artwork and sets it as your desktop background on macOS or Windows. It searches collections from:

- Cleveland Museum of Art
- Art Institute of Chicago
- Rijksmuseum
- The Metropolitan Museum of Art
- Wikimedia Commons

No API key is required.

## Windows 11 with Python 3.12

Open PowerShell and run:

```powershell
git clone https://github.com/MediaNomad/wallpaper.git
cd wallpaper
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\wallpaper.exe next
```

Keep the cloned folder and its `.venv` if you install automatic rotation, because the scheduled task uses that Python environment.

## macOS

```bash
git clone https://github.com/MediaNomad/wallpaper.git
cd wallpaper
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/wallpaper next
```

## Usage

Set a random painting using the saved defaults:

```text
wallpaper next
```

Search by subject, artist, or title:

```text
wallpaper next --query landscape
wallpaper next --artist "Claude Monet"
wallpaper next --title "Water Lilies"
```

Limit the search to one or more sources:

```text
wallpaper sources
wallpaper next --source chicago --source met --query flowers
```

Save search options as the new defaults:

```text
wallpaper next --artist "Vincent van Gogh" --image-width auto --save
```

Download without changing the desktop:

```text
wallpaper next --query cats --download-only
```

Show the current configuration or last selected artwork:

```text
wallpaper config
wallpaper current
```

## Automatic rotation

Install a six-hour rotation:

```text
wallpaper install-daemon --interval 6h
```

The same commands work on both platforms. On macOS this creates a user LaunchAgent; on Windows it creates a user task named `wallpaper` in Task Scheduler.

```text
wallpaper status
wallpaper stop
wallpaper start
wallpaper restart
wallpaper uninstall-daemon
```

Windows scheduling accepts whole minutes up to 1439, whole hours up to 23, or whole days.

## Data locations

- Windows configuration: `%APPDATA%\wallpaper\config.json`
- Windows cache/state: `%LOCALAPPDATA%\wallpaper\`
- macOS configuration: `~/.config/wallpaper/config.json`
- macOS cache: `~/.cache/wallpaper/`
- downloaded art: `~/Pictures/Wallpapers/`

On macOS, configuration from the former `met-wallpaper` name is migrated automatically the first time `wallpaper` runs.
