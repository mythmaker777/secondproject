# InstaSpy — Telegram Bot

Find Instagram non-followers via a Telegram bot. Users upload their own Instagram data export — **no password required**. Results are gated behind a PayNow payment that you approve with a single tap in Telegram.

---

## Project Structure

```
follower_bot/
├── bot.py               # Telegram bot — user flow + admin commands
├── instagram_parser.py  # Parses Instagram JSON/ZIP exports
├── database.py          # SQLite database layer
├── config.py            # Environment variable loader
├── requirements.txt
├── .env.example
├── Procfile             # Railway start command
└── nixpacks.toml        # Railway build config
```

---

## Admin Commands (you only — invisible to other users)

| Command | Description |
|---|---|
| `/pending` | List all payments awaiting verification, each with ✅/❌ buttons |
| `/approve <id>` | Approve a request and deliver results to user |
| `/reject <id> [reason]` | Reject with an optional reason sent to the user |
| `/stats` | Total users, requests, pending, approved, rejected |
| `/broadcast` | Send a message to all users (prompts for message body) |

When a user submits a payment ref, you receive a Telegram notification with **✅ Approve / ❌ Reject** inline buttons — no commands needed for the common case.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

| Variable             | Description                                    |
|----------------------|------------------------------------------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram                    |
| `ADMIN_TELEGRAM_ID`  | Your Telegram user ID (get from @userinfobot)  |
| `PAYNOW_NUMBER`      | Your PayNow phone number or UEN               |
| `PAYMENT_AMOUNT`     | Amount in SGD, e.g. `2.99`                    |
| `DATABASE_PATH`      | SQLite file path (default: `bot_data.db`)      |

### 3. Run locally

```bash
python bot.py
```

---

## Deploying to Railway (cheapest option — ~$5/month)

### 1. Add these two files to your repo root

**`Procfile`**
```
bot: python bot.py
```

**`nixpacks.toml`**
```toml
[phases.setup]
nixPkgs = ["python311"]
```

### 2. Push to GitHub

```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/YOUR_USERNAME/instaspy-bot.git
git push -u origin main
```

### 3. Deploy on Railway

1. Go to [railway.com](https://railway.com) → New Project → Deploy from GitHub
2. Select your repo — Railway auto-detects Python
3. Go to **Settings → Start Command** → set to `python bot.py`
4. Go to **Variables** → add all your `.env` values
5. Add a **Volume** → mount path `/app/data` → set `DATABASE_PATH=/app/data/bot_data.db`
6. Click **Deploy**

That's it. Every `git push` to main auto-redeploys.

---

## User Flow

1. User sends `/start`
2. Bot explains how to download their Instagram data (JSON format)
3. User uploads the ZIP or individual JSON files
4. Bot parses in memory — files are never written to disk
5. Bot shows following/follower counts + generic "results ready" message
6. Bot prompts PayNow payment
7. User sends transaction reference number
8. **You receive a Telegram notification with ✅ Approve / ❌ Reject buttons**
9. Tap Approve → user instantly receives the full list
10. Tap Reject → user is notified with a reason

---

## Privacy

- No Instagram password is ever requested or stored
- Users download their own data directly from Instagram
- Uploaded files are processed in memory and immediately discarded
- The result list is only ever sent after you manually approve payment
