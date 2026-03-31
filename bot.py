"""
bot.py — Telegram Bot with large file support
- Downloads via downloadAT.py
- Videos > 1.9 GB: split by duration using ffmpeg (Part 1, 2, 3…)
- Others  > 1.9 GB: split into .7z segments (with optional password)
- All parts uploaded serially
- Runs at least 10 min, waits for active tasks before shutdown
"""

import asyncio
import logging
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from downloadAT import download_file, cleanup_file, cleanup_dir, DOWNLOAD_DIR

# ─── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Config — only chat IDs matter now
# Add to config section
ALLOWED_CHAT_IDS_RAW = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = (
    {int(cid.strip()) for cid in ALLOWED_CHAT_IDS_RAW.split(",") if cid.strip()}
    if ALLOWED_CHAT_IDS_RAW else set()
)

# Replace your is_allowed() with this combined check
def is_allowed(update: Update) -> bool:
    user_ok = (
        not ALLOWED_USER_IDS
        or update.effective_user.id in ALLOWED_USER_IDS
    )
    chat_ok = (
        not ALLOWED_CHAT_IDS
        or update.effective_chat.id in ALLOWED_CHAT_IDS
    )
    return user_ok and chat_ok

MIN_RUNTIME_SECONDS = 10 * 60      # 10 minutes
SEGMENT_SIZE = 1_900 * 1024**2     # 1.9 GB per segment
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".wmv"}
SPLIT_DIR = Path("/tmp/tgbot_splits")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── State ───────────────────────────────────────────────────────────────────

start_time = time.monotonic()
active_tasks: set[str] = set()
shutdown_event = asyncio.Event()
pending_jobs: dict[int, dict] = {}   # chat_id → job waiting for user input


def is_allowed(uid: int) -> bool:
    return not ALLOWED_USER_IDS or uid in ALLOWED_USER_IDS

def register_task(tid: str):
    active_tasks.add(tid)

def finish_task(tid: str):
    active_tasks.discard(tid)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def split_video_by_size(src: Path, segment_bytes: int, out_dir: Path) -> list[Path]:
    """Split video into parts proportional to segment_bytes using ffmpeg stream copy."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total_size = src.stat().st_size
    duration = get_video_duration(src)
    if duration <= 0:
        raise ValueError("Could not determine video duration via ffprobe")

    secs_per_byte = duration / total_size
    seg_duration = secs_per_byte * segment_bytes
    n_parts = math.ceil(duration / seg_duration)

    parts: list[Path] = []
    for i in range(n_parts):
        start = i * seg_duration
        out_path = out_dir / f"{src.stem}_part{i+1}{src.suffix}"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(src),
            "-t", str(seg_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(out_path)
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        if out_path.exists() and out_path.stat().st_size > 0:
            parts.append(out_path)
    return parts


def split_7z(src: Path, segment_bytes: int, out_dir: Path,
             password: Optional[str] = None) -> list[Path]:
    """Compress + split into .7z.001, .7z.002, … segments."""
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_base = out_dir / src.name

    cmd = ["7z", "a", "-t7z", f"-v{segment_bytes}b"]
    if password:
        cmd += [f"-p{password}", "-mhe=on"]
    cmd += [str(archive_base), str(src)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"7z failed:\n{result.stderr}")

    parts = sorted(out_dir.glob(f"{src.name}.7z.*"))
    return parts


async def send_progress_updater(msg, prefix: str):
    last_update = [0.0]

    async def callback(downloaded: int, total: int, speed: float):
        now = time.monotonic()
        if now - last_update[0] < 3:
            return
        last_update[0] = now
        pct = downloaded / total * 100 if total else 0
        bar_filled = int(pct / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        text = (
            f"{prefix}\n"
            f"`{bar}` {pct:.1f}%\n"
            f"{downloaded/1024**2:.1f} MB / {total/1024**2:.1f} MB\n"
            f"⚡ {speed:.1f} MB/s"
        )
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass

    return callback


# ─── Upload Logic ─────────────────────────────────────────────────────────────

async def upload_parts(update: Update, parts: list[Path], label: str, is_video: bool):
    total = len(parts)
    chat = update.effective_chat

    for i, part in enumerate(parts, 1):
        caption = f"📦 {label} — Part {i}/{total}" if total > 1 else f"📦 {label}"
        size_mb = part.stat().st_size / 1024**2
        status_msg = await chat.send_message(
            f"⬆️ Uploading part {i}/{total}: `{part.name}` ({size_mb:.1f} MB)…",
            parse_mode="Markdown"
        )
        try:
            with open(part, "rb") as f:
                if is_video and part.suffix.lower() in VIDEO_EXTS:
                    await chat.send_video(
                        video=f, caption=caption,
                        supports_streaming=True,
                        read_timeout=600, write_timeout=600,
                    )
                else:
                    await chat.send_document(
                        document=f, caption=caption,
                        read_timeout=600, write_timeout=600,
                    )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed uploading part {i}: {e}")
            raise


async def process_and_upload(
    update: Update,
    file_path: Path,
    do_zip: bool,
    password: Optional[str],
    task_id: str,
):
    chat = update.effective_chat
    file_size = file_path.stat().st_size
    is_video = file_path.suffix.lower() in VIDEO_EXTS
    split_dir = SPLIT_DIR / task_id
    label = file_path.stem

    try:
        if file_size <= SEGMENT_SIZE and not do_zip:
            # ── Direct upload ──
            await chat.send_message(f"⬆️ Uploading `{file_path.name}`…", parse_mode="Markdown")
            with open(file_path, "rb") as f:
                if is_video:
                    await chat.send_video(
                        video=f, caption=f"🎬 {file_path.name}",
                        supports_streaming=True,
                        read_timeout=600, write_timeout=600,
                    )
                else:
                    await chat.send_document(
                        document=f, caption=f"📄 {file_path.name}",
                        read_timeout=600, write_timeout=600,
                    )

        elif is_video and not do_zip:
            # ── Video split with ffmpeg ──
            msg = await chat.send_message(
                f"✂️ Splitting video into ~1.9 GB parts (stream copy, no re-encode)…"
            )
            parts = await asyncio.get_event_loop().run_in_executor(
                None, lambda: split_video_by_size(file_path, SEGMENT_SIZE, split_dir)
            )
            await msg.edit_text(f"✂️ Split into {len(parts)} parts. Uploading…")
            await upload_parts(update, parts, label, is_video=True)

        else:
            # ── 7z split ──
            pwd_note = f" 🔒 Password: `{password}`" if password else ""
            msg = await chat.send_message(
                f"🗜 Compressing into 1.9 GB .7z segments…" + pwd_note,
                parse_mode="Markdown"
            )
            parts = await asyncio.get_event_loop().run_in_executor(
                None, lambda: split_7z(file_path, SEGMENT_SIZE, split_dir, password)
            )
            await msg.edit_text(
                f"🗜 {len(parts)} segment(s) ready. Uploading…" + pwd_note,
                parse_mode="Markdown"
            )
            await upload_parts(update, parts, label, is_video=False)

        await chat.send_message(f"✅ All parts of `{label}` uploaded!", parse_mode="Markdown")

    except Exception as e:
        logger.exception("process_and_upload failed")
        await chat.send_message(f"❌ Error: {e}")
    finally:
        cleanup_file(file_path)
        cleanup_dir(split_dir)
        finish_task(task_id)


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    elapsed = time.monotonic() - start_time
    remaining = max(0, MIN_RUNTIME_SECONDS - elapsed)
    await update.message.reply_html(
        f"👋 <b>Bot is running!</b>\n\n"
        f"⏱ Min uptime remaining: <code>{int(remaining)}s</code>\n"
        f"📦 Active tasks: <code>{len(active_tasks)}</code>\n\n"
        f"<b>Commands:</b>\n"
        f"  /dl &lt;url&gt; — download &amp; upload\n"
        f"  /dl &lt;url&gt; zip — force 7z compression\n"
        f"  /dl &lt;url&gt; nozip — skip compression (video split only)\n"
        f"  /dl &lt;url&gt; zip pass=mySecret — zip with password\n"
        f"  /status — show runtime info\n"
        f"  /stop — graceful shutdown\n\n"
        f"<b>Auto behavior:</b>\n"
        f"  • Video &gt; 1.9 GB → split with ffmpeg (no re-encode)\n"
        f"  • Other &gt; 1.9 GB → 7z split (asks for password)\n"
        f"  • ≤ 1.9 GB → direct upload"
    )


async def cmd_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dl <url> [zip|nozip] [pass=PASSWORD]
    """
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /dl <url> [zip|nozip] [pass=PASSWORD]")
        return

    url = context.args[0]
    args_lower = [a.lower() for a in context.args[1:]]
    force_zip   = "zip"   in args_lower
    force_nozip = "nozip" in args_lower
    password = None
    for arg in context.args[1:]:
        if arg.lower().startswith("pass="):
            password = arg[5:]

    task_id = f"dl-{int(time.time())}"
    register_task(task_id)
    status_msg = await update.message.reply_text(
        f"⬇️ Starting download…\n`{url}`", parse_mode="Markdown"
    )

    async def _run():
        try:
            # 1. Download
            cb = await send_progress_updater(status_msg, "⬇️ Downloading…")
            file_path = await download_file(url, progress_callback=cb)

            size_mb = file_path.stat().st_size / 1024**2
            await status_msg.edit_text(
                f"✅ Downloaded: `{file_path.name}` ({size_mb:.1f} MB)",
                parse_mode="Markdown"
            )

            # 2. Decide strategy
            file_size = file_path.stat().st_size
            is_video  = file_path.suffix.lower() in VIDEO_EXTS
            needs_split = file_size > SEGMENT_SIZE

            if force_nozip:
                do_zip = False
            elif force_zip:
                do_zip = True
            elif needs_split and not is_video:
                do_zip = True
            else:
                do_zip = False

            # 3. Ask about password if zipping and no password given
            if do_zip and password is None and needs_split:
                pending_jobs[update.effective_chat.id] = {
                    "file_path": file_path,
                    "do_zip": True,
                    "task_id": task_id,
                    "update": update,
                    "awaiting_password": False,
                }
                keyboard = [[
                    InlineKeyboardButton("🔓 No password", callback_data=f"pwd:none:{task_id}"),
                    InlineKeyboardButton("🔒 Set password", callback_data=f"pwd:ask:{task_id}"),
                ]]
                await update.effective_chat.send_message(
                    "Do you want to password-protect the .7z archive?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return  # wait for inline callback

            # 4. Process immediately
            await process_and_upload(update, file_path, do_zip, password, task_id)

        except Exception as e:
            logger.exception("cmd_dl failed")
            await update.effective_chat.send_message(f"❌ Error: {e}")
            finish_task(task_id)

    asyncio.create_task(_run())


async def callback_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data   # "pwd:none:<task_id>" | "pwd:ask:<task_id>"
    chat_id = update.effective_chat.id
    job = pending_jobs.pop(chat_id, None)

    if not job:
        await query.edit_message_text("⚠️ Session expired. Please retry /dl.")
        return

    _, choice, task_id = data.split(":", 2)

    if choice == "none":
        await query.edit_message_text("🔓 No password. Starting upload…")
        asyncio.create_task(
            process_and_upload(job["update"], job["file_path"], True, None, task_id)
        )
    elif choice == "ask":
        await query.edit_message_text(
            "🔒 Send your password with:\n`/setpass YOUR_PASSWORD`",
            parse_mode="Markdown"
        )
        pending_jobs[chat_id] = {**job, "awaiting_password": True}


async def cmd_setpass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    job = pending_jobs.get(chat_id)

    if not job or not job.get("awaiting_password"):
        await update.message.reply_text("No upload is waiting for a password.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setpass <password>")
        return

    password = context.args[0]
    pending_jobs.pop(chat_id)
    await update.message.reply_text(f"🔒 Password set. Starting upload…")
    asyncio.create_task(
        process_and_upload(job["update"], job["file_path"], True, password, job["task_id"])
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    elapsed = int(time.monotonic() - start_time)
    remaining = max(0, MIN_RUNTIME_SECONDS - elapsed)
    await update.message.reply_html(
        f"📊 <b>Status</b>\n"
        f"Elapsed: <code>{elapsed}s</code>\n"
        f"Min uptime left: <code>{int(remaining)}s</code>\n"
        f"Active tasks: <code>{len(active_tasks)}</code>"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("🛑 Shutdown requested. Waiting for tasks…")
    shutdown_event.set()


# ─── Watchdog ────────────────────────────────────────────────────────────────

async def shutdown_watchdog(app: Application):
    logger.info(f"Watchdog: min runtime = {MIN_RUNTIME_SECONDS}s")
    while True:
        await asyncio.sleep(1)
        elapsed = time.monotonic() - start_time
        if elapsed >= MIN_RUNTIME_SECONDS and len(active_tasks) == 0:
            logger.info(f"Watchdog: shutting down cleanly at {int(elapsed)}s")
            app.stop_running()
            break
        if int(elapsed) % 60 == 0:
            logger.info(f"Watchdog: {int(elapsed)}s elapsed | tasks={len(active_tasks)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    for tool in ["ffmpeg", "ffprobe", "7z"]:
        if not shutil.which(tool):
            logger.warning(f"Missing tool: {tool} — add install step to workflow")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url("http://127.0.0.1:8081/bot")
        .base_file_url("http://127.0.0.1:8081/file/bot")
        .local_mode(True)
        .read_timeout(600)
        .write_timeout(600)
        .connect_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("dl",      cmd_dl))
    app.add_handler(CommandHandler("setpass", cmd_setpass))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CallbackQueryHandler(callback_password, pattern=r"^pwd:"))

    async def post_init(application):
        asyncio.create_task(shutdown_watchdog(application))
        logger.info("Bot ready.")

    app.post_init = post_init
    logger.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
