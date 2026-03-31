"""
downloadAT.py — Handles all downloading logic
Called by bot.py with a URL, saves to a temp directory, reports progress via callback.
"""


import asyncio
import logging
import os
import time
import re
from pathlib import Path
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path("/tmp/tgbot_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


async def download_file(
    url: str,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
    custom_filename: Optional[str] = None,
) -> Path:

    MAX_SIZE = 50 * 1024 ** 3

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:

        # ===== HEAD request =====
        try:
            head = await client.head(url)
            head.raise_for_status()
        except Exception:
            head = None

        # ===== Filename logic (UNCHANGED) =====
        filename = custom_filename
        if not filename and head:
            cd = head.headers.get("content-disposition", "")
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip().strip('"').strip("'")

        if not filename:
            filename = url.split("?")[0].rstrip("/").split("/")[-1]

        if not filename:
            filename = f"download_{int(time.time())}"

        filename = "".join(c for c in filename if c not in r'\/:*?"<>|').strip()

        dest = DOWNLOAD_DIR / filename

        if dest.exists():
            dest = DOWNLOAD_DIR / f"{dest.stem}_{int(time.time())}{dest.suffix}"

        # ===== ROUTING =====
        if ".m3u8" in url:
            return await _download_m3u8(url, dest, progress_callback)

        if url.startswith("magnet:") or url.endswith(".torrent"):
            return await _download_torrent(url, progress_callback)

        return await _download_aria2(url, dest, progress_callback)


# ==========================================
# ⚡ ARIA2 WITH REAL-TIME PROGRESS
# ==========================================
async def _download_aria2(url: str, dest: Path, cb):

    cmd = [
        "aria2c",
        "-x", "16",
        "-s", "16",
        "-k", "1M",
        "--file-allocation=none",
        "--summary-interval=1",
        "--dir", str(dest.parent),
        "--out", dest.name,
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    pattern = re.compile(
        r"\[#\w+\s+([\d.]+)%.*?DL:([\d.]+)([KMG])iB/s.*?ETA:(\d+s)?"
    )

    while True:
        line = await proc.stdout.readline()
        if not line:
            break

        text = line.decode().strip()

        if cb:
            match = pattern.search(text)
            if match:
                percent = float(match.group(1))
                speed_val = float(match.group(2))
                unit = match.group(3)

                # convert speed to MB/s
                mult = {"K": 1/1024, "M": 1, "G": 1024}
                speed = speed_val * mult.get(unit, 1)

                # fake total for percentage
                total = 100
                downloaded = percent

                try:
                    await cb(downloaded, total, speed)
                except:
                    pass

    await proc.wait()
    return dest


# ==========================================
# 🎬 YT-DLP (M3U8) PROGRESS
# ==========================================
async def _download_m3u8(url: str, dest: Path, cb):

    cmd = [
        "yt-dlp",
        "-o", str(dest),
        "--concurrent-fragments", "16",
        "--newline",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    pattern = re.compile(r"\[download\]\s+([\d.]+)%.*?at\s+([\d.]+)([KMG])iB/s")

    while True:
        line = await proc.stdout.readline()
        if not line:
            break

        text = line.decode()

        if cb:
            match = pattern.search(text)
            if match:
                percent = float(match.group(1))
                speed_val = float(match.group(2))
                unit = match.group(3)

                mult = {"K": 1/1024, "M": 1, "G": 1024}
                speed = speed_val * mult.get(unit, 1)

                try:
                    await cb(percent, 100, speed)
                except:
                    pass

    await proc.wait()
    return dest


# ==========================================
# 🧲 TORRENT PROGRESS (ARIA2)
# ==========================================
async def _download_torrent(url: str, cb):

    cmd = [
        "aria2c",
        "--enable-dht=true",
        "--enable-peer-exchange=true",
        "--summary-interval=1",
        "--seed-time=0",
        "--dir", str(DOWNLOAD_DIR),
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    pattern = re.compile(r"([\d.]+)%.*DL:([\d.]+)([KMG])iB/s")

    while True:
        line = await proc.stdout.readline()
        if not line:
            break

        text = line.decode()

        if cb:
            match = pattern.search(text)
            if match:
                percent = float(match.group(1))
                speed_val = float(match.group(2))
                unit = match.group(3)

                mult = {"K": 1/1024, "M": 1, "G": 1024}
                speed = speed_val * mult.get(unit, 1)

                try:
                    await cb(percent, 100, speed)
                except:
                    pass

    await proc.wait()
    return DOWNLOAD_DIR

def cleanup_file(path: Path):
    """Delete a downloaded/processed file."""
    try:
        if path.exists():
            path.unlink()
            logger.info(f"Cleaned up: {path}")
    except Exception as e:
        logger.warning(f"Cleanup failed for {path}: {e}")


def cleanup_dir(directory: Path):
    """Delete a directory and all its contents."""
    import shutil
    try:
        if directory.exists():
            shutil.rmtree(directory)
            logger.info(f"Cleaned up dir: {directory}")
    except Exception as e:
        logger.warning(f"Dir cleanup failed for {directory}: {e}")
