"""
bot.py — Telegram Bot with full feature set:
- Allowed chat IDs only (groups/channels), any member can use
- Topic-aware replies (Forum groups)
- Downloads via downloadAT.py
- Videos > 1.9 GB: split with ffmpeg (no re-encode)
- Others > 1.9 GB: split into .7z segments
- .m3u8 treated as video (ffmpeg split/remux, never zipped)
- Optional zip + password for any file
- Serial uploads with progress
- Batch progress counter shown in download status (e.g. 📦 3/10)
- /fp: batch download+upload from a text/csv file of URLs
- /opv: fetch links via poneDownload then download+upload serially
- photo=<url|file_url>: send a photo before each upload
- name=<caption>: send a name/caption message before each upload
- -nv: no-verbose mode (status messages deleted when done, no final "done")
- Runs at least 20 min, waits for active tasks before shutdown
"""

import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from downloadAT import download_file, cleanup_file, cleanup_dir
from poneDownload import poneDownload  # your downloader module

# ─── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ALLOWED_CHAT_IDS_RAW = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = (
    {int(cid.strip()) for cid in ALLOWED_CHAT_IDS_RAW.split(",") if cid.strip()}
    if ALLOWED_CHAT_IDS_RAW else set()
)

MIN_RUNTIME_SECONDS = 20 * 60        # 20 minutes
SEGMENT_SIZE        = 1_900 * 1024**2  # 1.9 GB

# .m3u8 is treated as video — ffmpeg remux/split, never 7z
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".ts",  ".m4v", ".wmv", ".m3u8",
}

SPLIT_DIR = Path("/tmp/tgbot_splits")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Runtime State ────────────────────────────────────────────────────────────

start_time             = time.monotonic()
active_tasks: set[str] = set()
shutdown_event         = asyncio.Event()
pending_jobs: dict[int, dict] = {}   # chat_id -> job awaiting user input
pending_fp:   dict[int, dict] = {}   # chat_id -> /fp session waiting for file


# ─── Guards ──────────────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return update.effective_chat.id in ALLOWED_CHAT_IDS


def get_thread_id(update: Update) -> Optional[int]:
    msg: Optional[Message] = (
        update.message
        or (update.callback_query.message if update.callback_query else None)
    )
    if msg and getattr(msg, "is_topic_message", False):
        return msg.message_thread_id
    return None


def register_task(tid: str):
    active_tasks.add(tid)
    logger.info(f"Task registered: {tid} | active={len(active_tasks)}")


def finish_task(tid: str):
    active_tasks.discard(tid)
    logger.info(f"Task finished:   {tid} | active={len(active_tasks)}")


# ─── Argument Parsing Helpers ─────────────────────────────────────────────────

def parse_common_args(args: list[str]) -> dict:
    """
    Parse shared flags. Returned keys:
      force_zip, force_nozip, password, photo_url, name_label, no_verbose
    """
    args_lower  = [a.lower() for a in args]
    force_zip   = "zip"   in args_lower
    force_nozip = "nozip" in args_lower
    no_verbose  = "-nv"   in args_lower
    password    = None
    photo_url   = None
    name_label  = None

    for arg in args:
        al = arg.lower()
        if al.startswith("pass="):
            password = arg[5:]
        elif al.startswith("photo="):
            val = arg[6:]
            if val.lower() not in ("false", "no", "0", ""):
                photo_url = val
        elif al.startswith("name="):
            name_label = arg[5:]

    return dict(
        force_zip=force_zip, force_nozip=force_nozip, password=password,
        photo_url=photo_url, name_label=name_label, no_verbose=no_verbose,
    )


def parse_urls_from_text(text: str) -> list[str]:
    """Split by newlines / commas / semicolons; keep only http(s) entries."""
    raw = re.split(r"[\n,;]+", text)
    return [u.strip() for u in raw if u.strip().lower().startswith("http")]


# ─── Video / Archive Helpers ──────────────────────────────────────────────────

def get_video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def split_video_by_size(src: Path, segment_bytes: int, out_dir: Path) -> list[Path]:
    """
    Split a video into ~segment_bytes parts (stream copy, no re-encode).
    .m3u8 files are first remuxed to a single .mp4 via ffmpeg before splitting.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Remux .m3u8 -> .mp4 so ffprobe can measure duration and size properly
    work = src
    if src.suffix.lower() == ".m3u8":
        remuxed = out_dir / (src.stem + "_remuxed.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy", str(remuxed)],
            capture_output=True, check=True,
        )
        work = remuxed

    total_size = work.stat().st_size
    duration   = get_video_duration(work)

    if duration <= 0:
        raise ValueError("ffprobe could not determine video duration.")

    secs_per_byte = duration / total_size
    seg_duration  = secs_per_byte * segment_bytes
    n_parts       = math.ceil(duration / seg_duration)

    parts: list[Path] = []
    for i in range(n_parts):
        start    = i * seg_duration
        out_path = out_dir / f"{src.stem}_part{i + 1}.mp4"
        subprocess.run(
            ["ffmpeg", "-y",
             "-ss", str(start), "-i", str(work),
             "-t", str(seg_duration),
             "-c", "copy", "-avoid_negative_ts", "make_zero",
             str(out_path)],
            capture_output=True, check=True,
        )
        if out_path.exists() and out_path.stat().st_size > 0:
            parts.append(out_path)

    return parts


def split_7z(
    src: Path,
    segment_bytes: int,
    out_dir: Path,
    password: Optional[str] = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_base = out_dir / src.name
    cmd = ["7z", "a", "-t7z", f"-v{segment_bytes}b"]
    if password:
        cmd += [f"-p{password}", "-mhe=on"]
    cmd += [str(archive_base), str(src)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"7z failed:\n{result.stderr}")
    return sorted(out_dir.glob(f"{src.name}.7z.*"))


# ─── Progress Callback ────────────────────────────────────────────────────────

async def make_progress_callback(
    msg: Message,
    prefix: str,
    batch_index: Optional[int] = None,
    batch_total: Optional[int] = None,
):
    """
    Returns an async progress callback.

    When batch_index / batch_total are supplied the message looks like:

        ⬇️ Downloading…
        📦 File 3/10
        ████████░░░░░░░░░░░░ 40.0%
        36.2 MB / 90.5 MB
        ⚡ 3.1 MB/s
    """
    last_update = [0.0]
    batch_line  = (
        f"📦 File {batch_index}/{batch_total}\n"
        if batch_index is not None and batch_total is not None
        else ""
    )

    async def callback(downloaded: int, total: int, speed: float):
        now = time.monotonic()
        if now - last_update[0] < 3:
            return
        last_update[0] = now

        pct        = downloaded / total * 100 if total else 0
        bar_filled = int(pct / 5)
        bar        = "█" * bar_filled + "░" * (20 - bar_filled)

        text = (
            f"{prefix}\n"
            f"{batch_line}"
            f"`{bar}` {pct:.1f}%\n"
            f"{downloaded / 1024**2:.1f} MB / {total / 1024**2:.1f} MB\n"
            f"⚡ {speed:.1f} MB/s"
        )
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass

    return callback


# ─── Upload Logic ─────────────────────────────────────────────────────────────

async def upload_parts(
    update: Update,
    parts: list[Path],
    label: str,
    is_video: bool,
    no_verbose: bool = False,
):
    """Upload all parts serially, deleting the status message when each finishes."""
    total     = len(parts)
    chat      = update.effective_chat
    thread_id = get_thread_id(update)

    for i, part in enumerate(parts, 1):
        caption    = f"📦 {label} — Part {i}/{total}" if total > 1 else f"📦 {label}"
        size_mb    = part.stat().st_size / 1024**2
        status_msg = await chat.send_message(
            f"⬆️ Uploading part {i}/{total}: `{part.name}` ({size_mb:.1f} MB)…",
            parse_mode="Markdown",
            message_thread_id=thread_id,
        )
        try:
            with open(part, "rb") as f:
                if is_video and part.suffix.lower() in VIDEO_EXTS:
                    await chat.send_video(
                        video=f, caption=caption,
                        supports_streaming=True,
                        read_timeout=600, write_timeout=600,
                        message_thread_id=thread_id,
                    )
                else:
                    await chat.send_document(
                        document=f, caption=caption,
                        read_timeout=600, write_timeout=600,
                        message_thread_id=thread_id,
                    )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed uploading part {i}: {e}")
            raise


async def send_photo_if_needed(
    chat,
    thread_id: Optional[int],
    photo_url: Optional[str],
    name_label: Optional[str],
):
    if name_label:
        try:
            await chat.send_message(name_label, message_thread_id=thread_id)
        except Exception as e:
            logger.warning(f"send name_label failed: {e}")

    if photo_url:
        try:
            photo_path = Path(photo_url)
            if photo_path.exists():
                with open(photo_path, "rb") as pf:
                    await chat.send_photo(photo=pf, message_thread_id=thread_id)
            else:
                await chat.send_photo(photo=photo_url, message_thread_id=thread_id)
        except Exception as e:
            logger.warning(f"send photo failed: {e}")


async def process_and_upload(
    update: Update,
    file_path: Path,
    do_zip: bool,
    password: Optional[str],
    task_id: str,
    no_verbose: bool = False,
    photo_url: Optional[str] = None,
    name_label: Optional[str] = None,
):
    """Decide split strategy, execute, upload, clean up."""
    chat      = update.effective_chat
    thread_id = get_thread_id(update)
    file_size = file_path.stat().st_size
    is_video  = file_path.suffix.lower() in VIDEO_EXTS
    split_dir = SPLIT_DIR / task_id
    label     = file_path.stem

    try:
        await send_photo_if_needed(chat, thread_id, photo_url, name_label)

        # ── 1. Small file, no zip ─────────────────────────────────────────────
        if file_size <= SEGMENT_SIZE and not do_zip:
            status_msg = await chat.send_message(
                f"⬆️ Uploading `{file_path.name}`…",
                parse_mode="Markdown",
                message_thread_id=thread_id,
            )
            with open(file_path, "rb") as f:
                if is_video:
                    await chat.send_video(
                        video=f, caption=f"🎬 {file_path.name}",
                        supports_streaming=True,
                        read_timeout=600, write_timeout=600,
                        message_thread_id=thread_id,
                    )
                else:
                    await chat.send_document(
                        document=f, caption=f"📄 {file_path.name}",
                        read_timeout=600, write_timeout=600,
                        message_thread_id=thread_id,
                    )
            await status_msg.delete()

        # ── 2. Large video (incl. remuxed .m3u8) — ffmpeg split ───────────────
        elif is_video and not do_zip:
            msg = await chat.send_message(
                "✂️ Splitting video into ~1.9 GB parts (stream copy, no re-encode)…",
                message_thread_id=thread_id,
            )
            parts = await asyncio.get_event_loop().run_in_executor(
                None, lambda: split_video_by_size(file_path, SEGMENT_SIZE, split_dir)
            )
            await msg.edit_text(f"✂️ Split into {len(parts)} part(s). Uploading…")
            await upload_parts(update, parts, label, is_video=True, no_verbose=no_verbose)
            await msg.delete()

        # ── 3. Non-video large file or forced zip — 7z ────────────────────────
        else:
            pwd_note = f" 🔒 Password: `{password}`" if password else ""
            msg = await chat.send_message(
                "🗜 Compressing into 1.9 GB .7z segments…" + pwd_note,
                parse_mode="Markdown",
                message_thread_id=thread_id,
            )
            parts = await asyncio.get_event_loop().run_in_executor(
                None, lambda: split_7z(file_path, SEGMENT_SIZE, split_dir, password)
            )
            await msg.edit_text(
                f"🗜 {len(parts)} segment(s) ready. Uploading…" + pwd_note,
                parse_mode="Markdown",
            )
            await upload_parts(update, parts, label, is_video=False, no_verbose=no_verbose)
            await msg.delete()

        if not no_verbose:
            await chat.send_message(
                f"✅ All done! `{label}` uploaded successfully.",
                parse_mode="Markdown",
                message_thread_id=thread_id,
            )

    except Exception as e:
        logger.exception("process_and_upload error")
        await chat.send_message(f"❌ Error: {e}", message_thread_id=thread_id)
    finally:
        cleanup_file(file_path)
        cleanup_dir(split_dir)
        finish_task(task_id)


# ─── Shared serial-URL worker (used by /fp and /opv) ─────────────────────────

async def process_url_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    urls: list[str],
    opts: dict,
    task_id: str,
):
    """
    Download + upload each URL serially.
    Every download status message shows the batch counter (e.g. 📦 File 3/10).
    """
    chat       = update.effective_chat
    thread_id  = get_thread_id(update)
    total_urls = len(urls)

    for idx, url in enumerate(urls, 1):
        url_task_id = f"{task_id}-{idx}"
        register_task(url_task_id)

        dl_msg = await chat.send_message(
            f"📦 [{idx}/{total_urls}] ⬇️ Starting…\n`{url}`",
            parse_mode="Markdown",
            message_thread_id=thread_id,
        )

        try:
            cb = await make_progress_callback(
                dl_msg,
                f"📦 [{idx}/{total_urls}] ⬇️ Downloading…",
                batch_index=idx,
                batch_total=total_urls,
            )
            file_path = await download_file(url, progress_callback=cb)
            size_mb   = file_path.stat().st_size / 1024**2

            if opts["no_verbose"]:
                await dl_msg.delete()
            else:
                await dl_msg.edit_text(
                    f"📦 [{idx}/{total_urls}] ✅ `{file_path.name}` ({size_mb:.1f} MB)",
                    parse_mode="Markdown",
                )

            file_size   = file_path.stat().st_size
            is_video    = file_path.suffix.lower() in VIDEO_EXTS
            needs_split = file_size > SEGMENT_SIZE

            if opts["force_nozip"]:
                do_zip = False
            elif opts["force_zip"]:
                do_zip = True
            elif needs_split and not is_video:
                do_zip = True
            else:
                do_zip = False

            await process_and_upload(
                update, file_path, do_zip, opts["password"], url_task_id,
                no_verbose=opts["no_verbose"],
                photo_url=opts["photo_url"],
                name_label=opts["name_label"],
            )

        except Exception as e:
            logger.exception(f"process_url_list item {idx} error")
            await chat.send_message(
                f"❌ [{idx}/{total_urls}] Error for\n`{url}`\n{e}",
                parse_mode="Markdown",
                message_thread_id=thread_id,
            )
            finish_task(url_task_id)


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    thread_id = get_thread_id(update)
    elapsed   = time.monotonic() - start_time
    remaining = max(0, MIN_RUNTIME_SECONDS - elapsed)

    await update.effective_chat.send_message(
        f"👋 <b>Bot is running!</b>\n\n"
        f"⏱ Min uptime remaining: <code>{int(remaining)}s</code>\n"
        f"📦 Active tasks: <code>{len(active_tasks)}</code>\n\n"
        f"<b>Commands:</b>\n"
        f"  /dl &lt;url&gt; [zip|nozip] [pass=X] [photo=URL] [name=X] [-nv]\n"
        f"    Download a single URL and upload it.\n\n"
        f"  /fp [zip|nozip] [pass=X] [photo=URL] [name=X] [-nv]\n"
        f"    Then send a .txt/.csv of URLs (newline / comma / semicolon).\n\n"
        f"  /opv &lt;query&gt; [start=N] [end=N] [maxp=N] [maxv=N]\n"
        f"       [quality=Q] [zip|nozip] [pass=X] [photo=URL] [name=X] [-nv]\n"
        f"    Fetch links via poneDownload then download+upload all serially.\n"
        f"    quality: high | medium | low | 2160p | 1080p | 720p | 480p …\n\n"
        f"  /status — runtime info\n"
        f"  /stop — graceful shutdown\n\n"
        f"<b>Auto behavior:</b>\n"
        f"  • Video / .m3u8 &gt; 1.9 GB → ffmpeg split (stream copy)\n"
        f"  • Other &gt; 1.9 GB → 7z split, asks for password\n"
        f"  • ≤ 1.9 GB → direct upload\n"
        f"  • -nv: all status msgs deleted silently, no done message",
        parse_mode="HTML",
        message_thread_id=thread_id,
    )


async def cmd_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dl <url> [zip|nozip] [pass=PASSWORD] [photo=URL|false] [name=LABEL] [-nv]
    """
    if not is_allowed(update):
        return

    if not context.args:
        await update.effective_chat.send_message(
            "Usage: /dl <url> [zip|nozip] [pass=X] [photo=URL|false] [name=LABEL] [-nv]",
            message_thread_id=get_thread_id(update),
        )
        return

    url       = context.args[0]
    opts      = parse_common_args(context.args[1:])
    task_id   = f"dl-{int(time.time())}"
    thread_id = get_thread_id(update)
    register_task(task_id)

    status_msg = await update.effective_chat.send_message(
        f"⬇️ Starting download…\n`{url}`",
        parse_mode="Markdown",
        message_thread_id=thread_id,
    )

    async def _run():
        try:
            cb        = await make_progress_callback(status_msg, "⬇️ Downloading…")
            file_path = await download_file(url, progress_callback=cb)
            size_mb   = file_path.stat().st_size / 1024**2

            if opts["no_verbose"]:
                await status_msg.delete()
            else:
                await status_msg.edit_text(
                    f"✅ Downloaded: `{file_path.name}` ({size_mb:.1f} MB)",
                    parse_mode="Markdown",
                )

            file_size   = file_path.stat().st_size
            is_video    = file_path.suffix.lower() in VIDEO_EXTS
            needs_split = file_size > SEGMENT_SIZE

            if opts["force_nozip"]:
                do_zip = False
            elif opts["force_zip"]:
                do_zip = True
            elif needs_split and not is_video:
                do_zip = True
            else:
                do_zip = False

            if do_zip and opts["password"] is None and needs_split:
                pending_jobs[update.effective_chat.id] = {
                    "file_path":         file_path,
                    "do_zip":            True,
                    "task_id":           task_id,
                    "update":            update,
                    "thread_id":         thread_id,
                    "awaiting_password": False,
                    "no_verbose":        opts["no_verbose"],
                    "photo_url":         opts["photo_url"],
                    "name_label":        opts["name_label"],
                }
                keyboard = [[
                    InlineKeyboardButton("🔓 No password", callback_data=f"pwd:none:{task_id}"),
                    InlineKeyboardButton("🔒 Set password", callback_data=f"pwd:ask:{task_id}"),
                ]]
                await update.effective_chat.send_message(
                    "Do you want to password-protect the .7z archive?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    message_thread_id=thread_id,
                )
                return

            await process_and_upload(
                update, file_path, do_zip, opts["password"], task_id,
                no_verbose=opts["no_verbose"],
                photo_url=opts["photo_url"],
                name_label=opts["name_label"],
            )

        except Exception as e:
            logger.exception("cmd_dl error")
            await update.effective_chat.send_message(
                f"❌ Error: {e}", message_thread_id=thread_id,
            )
            finish_task(task_id)

    asyncio.create_task(_run())


# ─── /fp — Batch from uploaded URL file ──────────────────────────────────────

async def cmd_fp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /fp [zip|nozip] [pass=X] [photo=URL|false] [name=LABEL] [-nv]
    Then attach / send a .txt or .csv file of URLs.
    """
    if not is_allowed(update):
        return

    thread_id = get_thread_id(update)
    opts      = parse_common_args(context.args or [])

    msg = update.message
    if msg and msg.document:
        await _handle_fp_document(update, context, msg.document, opts)
        return

    pending_fp[update.effective_chat.id] = {"opts": opts, "thread_id": thread_id}
    await update.effective_chat.send_message(
        "📂 Send or attach your URL list file (.txt or .csv) now.\n"
        "URLs may be separated by newlines, commas, or semicolons.",
        message_thread_id=thread_id,
    )


async def _handle_fp_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    document,
    opts: dict,
):
    chat      = update.effective_chat
    thread_id = get_thread_id(update)
    task_id   = f"fp-{int(time.time())}"
    register_task(task_id)

    status_msg = await chat.send_message(
        "📥 Reading URL list file…", message_thread_id=thread_id,
    )

    async def _run():
        tmp_file = None
        try:
            tg_file = await context.bot.get_file(document.file_id)
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=Path(document.file_name or "urls.txt").suffix or ".txt",
            ) as tf:
                tmp_file = Path(tf.name)

            await tg_file.download_to_drive(str(tmp_file))
            raw_text = tmp_file.read_text(encoding="utf-8", errors="replace")
            urls     = parse_urls_from_text(raw_text)

            if not urls:
                await status_msg.edit_text("⚠️ No valid URLs found in the file.")
                finish_task(task_id)
                return

            total_urls = len(urls)
            if opts["no_verbose"]:
                await status_msg.delete()
            else:
                await status_msg.edit_text(
                    f"📋 Found {total_urls} URL(s). Processing serially…"
                )

            await process_url_list(update, context, urls, opts, task_id)

            if not opts["no_verbose"]:
                await chat.send_message(
                    f"✅ Batch done! {total_urls} URL(s) processed.",
                    message_thread_id=thread_id,
                )

        except Exception as e:
            logger.exception("_handle_fp_document error")
            await chat.send_message(f"❌ /fp error: {e}", message_thread_id=thread_id)
        finally:
            if tmp_file and tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            finish_task(task_id)

    asyncio.create_task(_run())


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch documents sent after /fp command."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    job     = pending_fp.pop(chat_id, None)
    if job is None:
        return
    doc = update.message.document if update.message else None
    if doc:
        await _handle_fp_document(update, context, doc, job["opts"])


# ─── /opv — poneDownload-powered batch ───────────────────────────────────────

async def cmd_opv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /opv <query_or_url_or_category>
         [start=N]    starting page  (default 1)
         [end=N]      ending page
         [maxp=N]     maximum pages
         [maxv=N]     maximum videos
         [quality=Q]  high | medium | low | 2160p | 1080p | 720p | 480p | 360p …
         [zip|nozip] [pass=X] [photo=URL] [name=X] [-nv]

    Calls poneDownload(query, starting_page, ending_page, maximum_page,
                       maximum_video, quality) → list[str] of direct URLs,
    then downloads + uploads them all serially with batch progress.
    """
    if not is_allowed(update):
        return

    thread_id = get_thread_id(update)

    if not context.args:
        await update.effective_chat.send_message(
            "Usage: /opv <query> [start=N] [end=N] [maxp=N] [maxv=N] "
            "[quality=Q] [zip|nozip] [pass=X] [photo=URL] [name=X] [-nv]",
            message_thread_id=thread_id,
        )
        return

    # ── Parse /opv-specific args ───────────────────────────────────────────────
    query_parts:   list[str]     = []
    starting_page: int           = 1
    ending_page:   Optional[int] = None
    maximum_page:  Optional[int] = None
    maximum_video: Optional[int] = None
    quality:       Optional[str] = None
    passthrough:   list[str]     = []   # forwarded to parse_common_args

    COMMON_PREFIXES = ("pass=", "photo=", "name=")
    COMMON_FLAGS    = {"zip", "nozip", "-nv"}

    for arg in context.args:
        al = arg.lower()
        if al.startswith("start="):
            try:
                starting_page = int(arg[6:])
            except ValueError:
                pass
        elif al.startswith("end="):
            try:
                ending_page = int(arg[4:])
            except ValueError:
                pass
        elif al.startswith("maxp="):
            try:
                maximum_page = int(arg[5:])
            except ValueError:
                pass
        elif al.startswith("maxv="):
            try:
                maximum_video = int(arg[5:])
            except ValueError:
                pass
        elif al.startswith("quality="):
            quality = arg[8:]
        elif al in COMMON_FLAGS or any(al.startswith(p) for p in COMMON_PREFIXES):
            passthrough.append(arg)
        else:
            query_parts.append(arg)

    if not query_parts:
        await update.effective_chat.send_message(
            "❌ Please provide a search query, category, or URL.",
            message_thread_id=thread_id,
        )
        return

    query   = " ".join(query_parts)
    opts    = parse_common_args(passthrough)
    task_id = f"opv-{int(time.time())}"
    register_task(task_id)

    page_range = f"pages {starting_page}→{ending_page}" if ending_page else f"from page {starting_page}"
    status_msg = await update.effective_chat.send_message(
        f"🔍 Fetching links for <code>{query}</code> ({page_range})"
        + (f", quality={quality}" if quality else "") + "…",
        parse_mode="HTML",
        message_thread_id=thread_id,
    )

    async def _run():
        try:
            # Build optional kwargs — only pass what the caller specified
            kwargs: dict = {}
            if ending_page   is not None: kwargs["ending_page"]   = ending_page
            if maximum_page  is not None: kwargs["maximum_page"]  = maximum_page
            if maximum_video is not None: kwargs["maximum_video"] = maximum_video
            if quality       is not None: kwargs["quality"]       = quality

            # poneDownload is synchronous — run off the event loop
            urls: list[str] = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: poneDownload(query, starting_page=starting_page, **kwargs),
            )

            if not urls:
                await status_msg.edit_text("⚠️ poneDownload returned no links.")
                finish_task(task_id)
                return

            total = len(urls)
            if opts["no_verbose"]:
                await status_msg.delete()
            else:
                await status_msg.edit_text(
                    f"✅ Got {total} link(s). Downloading + uploading serially…"
                )

            await process_url_list(update, context, urls, opts, task_id)

            if not opts["no_verbose"]:
                await update.effective_chat.send_message(
                    f"✅ /opv done! {total} video(s) processed.",
                    message_thread_id=thread_id,
                )

        except Exception as e:
            logger.exception("cmd_opv error")
            await update.effective_chat.send_message(
                f"❌ /opv error: {e}", message_thread_id=thread_id,
            )
        finally:
            finish_task(task_id)

    asyncio.create_task(_run())


# ─── Password Callback ────────────────────────────────────────────────────────

async def callback_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data    = query.data
    chat_id = update.effective_chat.id
    job     = pending_jobs.pop(chat_id, None)

    if not job:
        await query.edit_message_text("⚠️ Session expired. Please retry /dl.")
        return

    _, choice, task_id = data.split(":", 2)

    if choice == "none":
        await query.edit_message_text("🔓 No password. Starting upload…")
        asyncio.create_task(
            process_and_upload(
                job["update"], job["file_path"], True, None, task_id,
                no_verbose=job.get("no_verbose", False),
                photo_url=job.get("photo_url"),
                name_label=job.get("name_label"),
            )
        )
    elif choice == "ask":
        await query.edit_message_text(
            "🔒 Send your password with:\n`/setpass YOUR_PASSWORD`",
            parse_mode="Markdown",
        )
        pending_jobs[chat_id] = {**job, "awaiting_password": True}


async def cmd_setpass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    job     = pending_jobs.get(chat_id)

    if not job or not job.get("awaiting_password"):
        await update.effective_chat.send_message(
            "No upload is currently waiting for a password.",
            message_thread_id=get_thread_id(update),
        )
        return

    if not context.args:
        await update.effective_chat.send_message(
            "Usage: /setpass <password>",
            message_thread_id=get_thread_id(update),
        )
        return

    password = context.args[0]
    pending_jobs.pop(chat_id)
    await update.effective_chat.send_message(
        "🔒 Password set. Starting upload…",
        message_thread_id=job["thread_id"],
    )
    asyncio.create_task(
        process_and_upload(
            job["update"], job["file_path"], True, password, job["task_id"],
            no_verbose=job.get("no_verbose", False),
            photo_url=job.get("photo_url"),
            name_label=job.get("name_label"),
        )
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    elapsed   = int(time.monotonic() - start_time)
    remaining = max(0, MIN_RUNTIME_SECONDS - elapsed)
    await update.effective_chat.send_message(
        f"📊 <b>Status</b>\n"
        f"Elapsed:          <code>{elapsed}s</code>\n"
        f"Min uptime left:  <code>{int(remaining)}s</code>\n"
        f"Active tasks:     <code>{len(active_tasks)}</code>",
        parse_mode="HTML",
        message_thread_id=get_thread_id(update),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.effective_chat.send_message(
        "🛑 Shutdown requested. Waiting for all tasks to finish…",
        message_thread_id=get_thread_id(update),
    )
    shutdown_event.set()


# ─── Shutdown Watchdog ────────────────────────────────────────────────────────

async def shutdown_watchdog(app: Application):
    logger.info(f"Watchdog started. Min runtime: {MIN_RUNTIME_SECONDS}s")
    while True:
        await asyncio.sleep(1)
        elapsed = time.monotonic() - start_time
        if elapsed >= MIN_RUNTIME_SECONDS and len(active_tasks) == 0:
            logger.info(f"Watchdog: clean shutdown at {int(elapsed)}s")
            app.stop_running()
            break
        if int(elapsed) % 60 == 0:
            logger.info(f"Watchdog: {int(elapsed)}s elapsed | tasks={len(active_tasks)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    for tool in ["ffmpeg", "ffprobe", "7z"]:
        if not shutil.which(tool):
            logger.warning(f"Missing system tool: {tool} — add to workflow apt-get")

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
    app.add_handler(CommandHandler("fp",      cmd_fp))
    app.add_handler(CommandHandler("opv",     cmd_opv))
    app.add_handler(CommandHandler("setpass", cmd_setpass))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CallbackQueryHandler(callback_password, pattern=r"^pwd:"))
    app.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document)
    )

    async def post_init(application: Application):
        asyncio.create_task(shutdown_watchdog(application))
        logger.info("Bot ready. Polling…")

    app.post_init = post_init

    logger.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
