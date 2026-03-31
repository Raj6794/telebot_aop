# Telegram Bot on GitHub Actions

A Python Telegram bot that runs for **at least 10 minutes** on GitHub Actions (free Linux runners), waits for active tasks before shutting down, and supports file handling up to 2 GB.

---

## Quick Setup

### 1. Create Your Bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. `/newbot` → follow prompts → copy the **token**

### 2. Get Your Telegram User ID
Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric user ID.

### 3. Add GitHub Secrets
Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `ALLOWED_USER_IDS` | Your user ID(s), comma-separated e.g. `123456789` |

### 4. Push This Repo
```bash
git init
git add .
git commit -m "init telegram bot"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 5. Trigger the Bot
Go to **Actions → Telegram Bot Runner → Run workflow** → click **Run workflow**.

The bot will:
- Start on GitHub's Linux runner
- Run for **at least 10 minutes**
- Wait for any active `/task` to finish before exiting
- Automatically stop after the minimum time + no active tasks

---

## Commands

| Command | Description |
|---|---|
| `/start` | Show bot status and available commands |
| `/ping` | Liveness check |
| `/status` | Show elapsed time, tasks, remaining uptime |
| `/task` | Start a 30-second demo background task |
| `/upload_url <url>` | Download file from URL and re-upload to chat |
| `/stop` | Request graceful shutdown |

---

## File Upload Limits

| Method | Max Size |
|---|---|
| Telegram Bot API (default) | **50 MB** send / **20 MB** download |
| Local Bot API Server (self-hosted) | **2 GB** |

### For True 2 GB Support

Run the [Telegram Local Bot API Server](https://github.com/tdlib/telegram-bot-api) alongside your bot.
Add this to your workflow:

```yaml
- name: Start Local Telegram Bot API
  run: |
    docker run -d \
      -e TELEGRAM_API_ID=${{ secrets.TELEGRAM_API_ID }} \
      -e TELEGRAM_API_HASH=${{ secrets.TELEGRAM_API_HASH }} \
      -p 8081:8081 \
      aiogram/telegram-bot-api:latest
```

Then point your bot to `http://localhost:8081/bot`:
```python
app = Application.builder()
    .token(BOT_TOKEN)
    .base_url("http://localhost:8081/bot")
    .local_mode(True)
    .build()
```

You'll also need `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org).

---

## GitHub Actions Free Tier

| Resource | Limit |
|---|---|
| Minutes/month | **2,000 min** (public repos: unlimited) |
| Storage | 500 MB artifacts |
| Max job duration | 6 hours |
| Runner | Ubuntu Latest (2-core, 7 GB RAM, 14 GB SSD) |

**Tip:** Use a **public repository** to get unlimited free minutes.

---

## How the 10-Minute Guarantee Works

```
Bot starts
    │
    ├─ Accepts commands & runs tasks
    │
    └─ Watchdog loop (every 1s):
           elapsed >= 600s ?  ──No──> keep running
                │
               Yes
                │
           active tasks == 0 ?  ──No──> keep running (wait for tasks)
                │
               Yes
                │
           Graceful shutdown ✓
```
