# 🎙️ Accent Changer

**Live at [myaccentchanger.com](https://myaccentchanger.com)**

A web app for non-native English speakers: record yourself speaking, and hear your own words played back with clear, native-sounding pronunciation — **in your own voice**, thanks to AI voice cloning.

## How it works

1. **Clone your voice** — record 1.5–2 minutes of natural speech (one-time setup)
2. **Record a sentence** — say anything in English
3. **Listen and compare** — the app transcribes your speech (OpenAI Whisper), then re-synthesizes the exact same words with your cloned voice and corrected pronunciation (ElevenLabs)

## Features

- 🎤 In-browser recording — nothing to install, works on phones
- 🧬 Per-user voice cloning with explicit consent
- 🌐 Bilingual UI (English / Russian)
- 💳 Stripe subscriptions with free trial (2 conversions), fair-use cap (300/month)
- 🛡️ Anti-abuse: disposable-email blocking, per-IP trial limits, automatic cleanup of expired trial voice clones
- 📼 Personal history of the last 10 conversions with playback and download
- 👑 Hidden admin panel: user management, trial resets, password resets, gift subscriptions
- 🔒 Privacy-first: recordings are processed in memory; only the user's own results are stored, and voice clones are deleted when trials lapse

## Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI, SQLite |
| Frontend | Vanilla HTML/CSS/JS (single page, no framework) |
| Speech-to-text | OpenAI Whisper API |
| Voice cloning & TTS | ElevenLabs API |
| Payments | Stripe Checkout + webhooks |
| Hosting | Railway (with persistent volume) |

## Running locally

1. `pip install -r requirements.txt`
2. Create a `.env` file:
   ```
   OPENAI_API_KEY=...
   ELEVENLABS_API_KEY=...
   SECRET_KEY=any-long-random-string
   ```
   (Stripe variables are optional — without them the app runs with payments disabled.)
3. `python server.py` and open http://127.0.0.1:8000

---

*Built with [Claude Code](https://claude.com/claude-code).*
