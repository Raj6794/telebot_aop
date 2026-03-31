"""
Telegram Bot — GitHub Actions hosted
- Runs for at least MIN_RUNTIME_SECONDS (10 min default)
- Waits for any active task to complete before shutting down
- Supports large file uploads up to 2GB via URL
"""

import asyncio
import logging
import os
import signal
import time
from datetime import datetime

from telegram import Update, Document
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ─── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS_RAW = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in ALLOWED_IDS_RAW.split(",") if uid.strip()}
    if ALLOWED_IDS_RAW
    else set()  # empty = allow everyone (not recommended for production)
)

MIN_RUNTIME_SECONDS = 10 * 60   # 10 minutes
POLL_INTERVAL = 1               # seconds between shutdown checks
MAX_FILE_SIZE_BYTES = 2 * 1024 ** 3  # 2 GB

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── State ───────────────────────────────────────────────────────────────────

start_time = time.monotonic()
active_tasks: set[str] = set()   # task IDs currently running
shutdown_event = asyncio.Event()


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def register_task(task_id: str):
    active_tasks.add(task_id)
    logger.info(f"Task registered: {task_id}  |  active={len(active_tasks)}")


def finish_task(task_id: str):
    active_tasks.discard(task_id)
    logger.info(f"Task finished:  {task_id}  |  active={len(active_tasks)}")


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ You are not authorised to use this bot.")
        return

    elapsed = time.monotonic() - start_time
    remaining = max(0, MIN_RUNTIME_SECONDS - elapsed)

    await update.message.reply_html(
        f"👋 Hello <b>{user.first_name}</b>!\n\n"
        f"🤖 Bot is <b>running</b> on GitHub Actions.\n"
        f"⏱ Started: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
        f"⏳ Min uptime remaining: <code>{int(remaining)}s</code>\n"
        f"📦 Active tasks: <code>{len(active_tasks)}</code>\n\n"
        f"Commands:\n"
        f"  /start — show status\n"
        f"  /ping — liveness check\n"
        f"  /upload_url &lt;url&gt; — download + re-upload a file (≤2 GB)\n"
        f"  /task — run a sample 30-second background task\n"
        f"  /status — show runtime info\n"
        f"  /stop — request graceful shutdown"
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("🏓 Pong!")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    elapsed = int(time.monotonic() - start_time)
    remaining = max(0, MIN_RUNTIME_SECONDS - elapsed)
    await update.message.reply_html(
        f"📊 <b>Bot Status</b>\n"
        f"Elapsed: <code>{elapsed}s</code>\n"
        f"Min uptime remaining: <code>{int(remaining)}s</code>\n"
        f"Active tasks: <code>{len(active_tasks)}</code>\n"
        f"Will shutdown when: elapsed ≥ {MIN_RUNTIME_SECONDS}s AND tasks = 0"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "🛑 Shutdown requested. Will stop after min runtime + all tasks finish."
    )
    shutdown_event.set()


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demo: runs a 30-second background task so you can test graceful shutdown."""
    if not is_allowed(update.effective_user.id):
        return

    task_id = f"demo-task-{int(time.time())}"
    register_task(task_id)
    msg = await update.message.reply_text(f"🔄 Task `{task_id}` started (30s demo)…")

    async def _work():
        try:
            await asyncio.sleep(30)
            await msg.edit_text(f"✅ Task `{task_id}` completed!")
        finally:
            finish_task(task_id)

    asyncio.create_task(_work())


async def cmd_upload_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Download a file from a URL and send it back to the chat.
    Telegram's sendDocument supports up to 50 MB via Bot API.
    For larger files you need a local Bot API server — see README.
    Usage: /upload_url https://example.com/file.zip
    """
    if not is_allowed(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /upload_url <url>")
        return

    url = context.args[0]
    task_id = f"upload-{int(time.time())}"
    register_task(task_id)
    msg = await update.message.reply_text(f"⬇️ Downloading from:\n{url}")

    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_length = int(resp.headers.get("content-length", 0))
                if content_length > MAX_FILE_SIZE_BYTES:
                    await msg.edit_text(
                        f"❌ File too large: {content_length / 1024**3:.2f} GB > 2 GB limit"
                    )
                    return

                data = await resp.aread()

        filename = url.split("/")[-1].split("?")[0] or "file"
        size_mb = len(data) / 1024**2

        await msg.edit_text(f"⬆️ Uploading {filename} ({size_mb:.1f} MB)…")

        from io import BytesIO
        await update.message.reply_document(
            document=BytesIO(data),
            filename=filename,
            caption=f"📎 {filename} ({size_mb:.1f} MB)",
        )
        await msg.delete()

    except Exception as e:
        logger.exception("Upload failed")
        await msg.edit_text(f"❌ Failed: {e}")
    finally:
        finish_task(task_id)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Echo back info about any document the user sends."""
    if not is_allowed(update.effective_user.id):
        return
    doc: Document = update.message.document
    size_mb = doc.file_size / 1024**2 if doc.file_size else 0
    await update.message.reply_html(
        f"📄 Received: <b>{doc.file_name}</b>\n"
        f"Size: <code>{size_mb:.2f} MB</code>\n"
        f"MIME: <code>{doc.mime_type}</code>\n\n"
        f"ℹ️ Telegram Bot API supports direct download up to 20 MB.\n"
        f"For larger files use /upload_url or a local Bot API server."
    )


# ─── Graceful-shutdown watchdog ───────────────────────────────────────────────

async def shutdown_watchdog(app: Application):
    """
    Waits until:
      1. Minimum runtime has elapsed, AND
      2. No active tasks are running (or manual /stop was issued)
    Then stops the bot cleanly.
    """
    logger.info(f"Watchdog started. Min runtime: {MIN_RUNTIME_SECONDS}s")

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed = time.monotonic() - start_time
        min_elapsed = elapsed >= MIN_RUNTIME_SECONDS
        no_tasks = len(active_tasks) == 0
        stop_requested = shutdown_event.is_set()

        if min_elapsed and no_tasks:
            reason = "stop requested" if stop_requested else "min runtime reached, no active tasks"
            logger.info(f"Watchdog: shutting down ({reason}). Elapsed={int(elapsed)}s")
            app.stop_running()
            break

        if elapsed % 60 < POLL_INTERVAL:  # log every ~60s
            logger.info(
                f"Watchdog: elapsed={int(elapsed)}s  tasks={len(active_tasks)}  "
                f"min_ok={min_elapsed}"
            )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = (
    Application.builder()
    .token(BOT_TOKEN)
    .base_url("http://127.0.0.1:8081/bot")   # ← point to local server
    .base_file_url("http://127.0.0.1:8081/file/bot")  # ← for file downloads
    .local_mode(True)                          # ← disables cloud size checks
    .build()
)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("upload_url", cmd_upload_url))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Register the watchdog as a post-init coroutine
    async def post_init(application: Application):
        asyncio.create_task(shutdown_watchdog(application))
        logger.info("Bot started. Polling for updates…")

    app.post_init = post_init

    logger.info("Running bot…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
