# Telegram Subtitle Translator Bot

A Telegram bot to translate `.srt` subtitle files into Sinhala and return the translated version.

## Features
- Accepts `.srt` files
- Offers filename options (original/custom)
- Translates using Google Translate API
- Sends back translated file
- Flask keepalive server included for Railway

## Deployment (Railway)
1. Fork this repo
2. Create new project on [Railway](https://railway.app)
3. Set these environment variables:
   - `API_ID`
   - `API_HASH`
   - `BOT_TOKEN`
4. Railway auto-detects and runs `python3 main.py`

## License
MIT
