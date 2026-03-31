"""
downloadAT.py — Handles all downloading logic
Called by bot.py with a URL, saves to a temp directory, reports progress via callback.
"""

import asyncio
import logging
import os
import time
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
    """
    Download a file from URL to DOWNLOAD_DIR.
    
    Args:
        url: Direct download URL
        progress_callback: Called with (downloaded_bytes, total_bytes, speed_mbps)
        custom_filename: Override the filename detected from URL/headers
    
    Returns:
        Path to the downloaded file
    
    Raises:
        ValueError: If file too large or URL invalid
        httpx.HTTPError: On network errors
    """
    MAX_SIZE = 50 * 1024 ** 3  # 50 GB hard cap

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=300, write=300, pool=30),
    ) as client:

        # HEAD request first to get filename + size
        try:
            head = await client.head(url)
            head.raise_for_status()
        except Exception:
            # Some servers don't support HEAD — fall through
            head = None

        # Determine filename
        filename = custom_filename
        if not filename and head:
            cd = head.headers.get("content-disposition", "")
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip().strip('"').strip("'")
        if not filename:
            filename = url.split("?")[0].rstrip("/").split("/")[-1]
        if not filename:
            filename = f"download_{int(time.time())}"

        # Sanitize filename
        filename = "".join(c for c in filename if c not in r'\/:*?"<>|').strip()

        dest = DOWNLOAD_DIR / filename
        # Avoid overwriting
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = DOWNLOAD_DIR / f"{stem}_{int(time.time())}{suffix}"

        # Stream download
        downloaded = 0
        total = 0
        start_time = time.monotonic()

        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))

            if total > MAX_SIZE:
                raise ValueError(
                    f"File too large: {total / 1024**3:.2f} GB exceeds 50 GB limit"
                )

            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):  # 1 MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback and total:
                        elapsed = time.monotonic() - start_time
                        speed = (downloaded / elapsed) / 1024**2 if elapsed > 0 else 0
                        await asyncio.get_event_loop().run_in_executor(
                            None, lambda: None  # yield control
                        )
                        try:
                            await progress_callback(downloaded, total, speed)
                        except Exception:
                            pass  # never crash on callback error

        logger.info(f"Downloaded: {dest} ({downloaded / 1024**2:.1f} MB)")
        return dest


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
