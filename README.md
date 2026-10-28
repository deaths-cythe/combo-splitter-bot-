# Combo Splitter Bot

A Telegram bot that receives a text/combo file, splits it by lines, by file
size, or into N equal parts, and sends the pieces back — optionally zipped.

Built on `python-telegram-bot` v21 (async API). All splitting is done by
streaming, so file size is bounded by your staging disk, not by RAM.

## Features

- Three split modes: **lines per part**, **max bytes per part**, **N equal parts**
- Streaming input and output — nothing is loaded into memory whole
- Preserves original line endings (`\n`, `\r\n`, mixed)
- Optional header preservation: first line copied to the top of every part
- Optional ZIP packaging of all outputs
- Optional pre-flight preview
- Progress updates for large files
- Configurable Telegram API base URL (works with a self-hosted local Bot API server)
- Configurable download / upload limits enforced before download and before send
- `MAX_PARTS` guard so a 1-line-per-part request on a 10M-line file fails fast
- Temp files are staged under a per-job directory and deleted after send
- Filenames are sanitised; file contents are never executed or logged

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
git clone <this-repo>
cd combo-splitter-bot
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set BOT_TOKEN