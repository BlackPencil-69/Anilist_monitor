# AniList Monitor Bot

A small Windows tray application that watches [AniList](https://anilist.co) for newly announced and currently airing anime and sends you a Telegram notification for each new title, with the cover art and a link to its AniList page.

It is designed to be **self-hosted on your own PC**: it runs quietly in the system tray, can start together with Windows, and keeps its state in a local `cache.json` file. It is not intended for cloud or server deployment.

> **⚠️ Disclaimer:** this project was written with the help of artificial intelligence (Claude by Anthropic). It has only been tested informally. Review the code before relying on it, and use it at your own risk.

## Features

- Polls the public AniList GraphQL API at a configurable interval
- Sends Telegram messages with cover, format, season, genres and an "Open on AniList" button
- Whitelist: the bot ignores everyone except the Telegram user IDs you allow
- Bot commands: `/start`, `/status`, `/check` (manual check)
- Tray menu: check now, start with Windows, exit
- The first run only records existing titles, so you are not flooded with old announcements

## Requirements

- Windows 10/11
- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (for example from [@userinfobot](https://t.me/userinfobot))

## Quick start

```bash
git clone https://github.com/BlackPencil-69/Anilist_monitor.git
cd Anilist_monitor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `example.env` to `.env` (in the same folder) and fill in your own values:
 
```env
BOT_TOKEN=123456:your-telegram-bot-token
WHITELIST_IDS=123456789
CHECK_INTERVAL=1800
CACHE_FILE=cache.json
```

- `BOT_TOKEN` — token of your Telegram bot, from [@BotFather](https://t.me/BotFather)
- `WHITELIST_IDS` — Telegram user IDs allowed to use the bot (comma-separated, no spaces, for several users)
- `CHECK_INTERVAL` — seconds between automatic checks (default 1800 = 30 minutes; minimum 60)
- `CACHE_FILE` — name of the cache file with already-sent titles; a relative path is resolved next to the app (`.exe` or `main.py`)

Run it:

```bash
python main.py
```

A tray icon appears, and the bot starts answering in Telegram. Optionally put an `icon.ico` next to `main.py` to use your own tray icon; otherwise a built-in fallback icon is used.

## Testing

1. Start the app and send `/start` and `/status` to your bot from a whitelisted account. You should get replies; other accounts get no response at all.
2. Send `/check` or use **Check for updates now** in the tray menu to trigger a check manually.
3. To see a real notification, close the app, remove a few IDs from `sent_ids` in `cache.json`, start the app again and run `/check`. Those titles will be sent again.
4. Toggle **Start with Windows** in the tray menu and check that the autostart entry appears (and disappears) as expected.
5. If something goes wrong, check the console output (when run via `python main.py`) or `bot.log` (in the built `.exe` version).

## Building the .exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name AniListMonitorBot --icon=icon.ico --add-data "icon.ico;." main.py
```

The executable is created in `dist/`. Place your `.env` file (and optionally `icon.ico`) next to `AniListMonitorBot.exe` and run it. Logs are written to `bot.log` because the windowed build has no console.

If you don't have an `icon.ico`, drop the `--icon` and `--add-data` options; the app falls back to a generated icon.

## Note

- Windows only (system tray, autostart, PyInstaller build).

## 📧 Contact

GitHub: [@BlackPencil-69](https://github.com/BlackPencil-69/)  
Telegram: [@MrKap1toshka](https://t.me/MrKap1toshka)  
Discord: [@anonym_pro](https://discord.com/users/1149264703470698529)
