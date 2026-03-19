"""
GitHub Actions download wrapper.
Calls downloader_core.py logic directly, outputs results as JSON.
"""

import os
import sys
import json
import time
import argparse

import yt_dlp

from downloader_core import (
    extract_urls, is_channel_url, needs_selenium, is_xhs_url,
    resolve_short_url, normalize_douyin_url, normalize_xhs_url,
    download_xhs_content, SeleniumDownloader, CookieManager,
)

FORMAT_MAP = {
    "best_video": {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "merge_output_format": "mp4",
    },
    "mp4_720": {
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720][ext=mp4]/best[height<=720]/best",
        "merge_output_format": "mp4",
    },
    "mp4_1080": {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080][ext=mp4]/best[height<=1080]/best",
        "merge_output_format": "mp4",
    },
    "mp3_audio": {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    },
}

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")


def log(msg):
    print(f"[ACTION] {msg}", flush=True)


def setup_cookies():
    """Write YouTube cookies from env var to cookies.txt file."""
    cookies_data = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_data:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(cookies_data)
        log(f"Cookies file created: {COOKIES_FILE}")
        return COOKIES_FILE
    return None


def download_single(url, fmt, output_dir):
    """Download a single URL. Returns dict with result info."""
    result = {
        "url": url,
        "status": "failed",
        "title": None,
        "files": [],
        "error": None,
    }

    try:
        # Normalize URLs
        if any(d in url for d in ["douyin.com", "iesdouyin.com"]):
            url = normalize_douyin_url(url)

        # XHS pathway
        if is_xhs_url(url):
            log(f"XHS detected: {url}")
            url = normalize_xhs_url(url)
            count, title, content_type = download_xhs_content(
                url, output_dir, logger=log,
            )
            if count > 0:
                result["status"] = "completed"
                result["title"] = title
                for f in os.listdir(output_dir):
                    fpath = os.path.join(output_dir, f)
                    if os.path.isfile(fpath):
                        result["files"].append({
                            "filename": f,
                            "size": os.path.getsize(fpath),
                        })
                return result
            log("XHS custom handler failed, trying yt-dlp...")

        # Selenium pathway (Douyin)
        if needs_selenium(url):
            log(f"Selenium download: {url}")
            selenium_dl = SeleniumDownloader(logger=log)
            ok, title, filepath_or_err = selenium_dl.download(url, output_dir, format_choice=fmt)
            if ok:
                result["status"] = "completed"
                result["title"] = title
                for f in os.listdir(output_dir):
                    fpath = os.path.join(output_dir, f)
                    if os.path.isfile(fpath):
                        result["files"].append({
                            "filename": f,
                            "size": os.path.getsize(fpath),
                        })
            else:
                result["error"] = filepath_or_err
            return result

        # yt-dlp pathway
        log(f"yt-dlp download: {url}")

        # Verify JS runtime for n-parameter challenge
        import shutil
        for rt in ("node", "deno", "phantomjs"):
            path = shutil.which(rt)
            if path:
                log(f"JS runtime found: {rt} -> {path}")
                break
        else:
            log("WARNING: No JS runtime found! n-parameter solving may fail")

        outtmpl = os.path.join(output_dir, "%(title)s.%(ext)s")
        opts = {
            "outtmpl": outtmpl,
            "windowsfilenames": False,
            "trim_file_name": 80,
            "encoding": "utf-8",
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "remote_components": {"ejs:github"},
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_creator", "web"],
                },
            },
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            **FORMAT_MAP.get(fmt, FORMAT_MAP["best_video"]),
        }

        # Add cookies if available (fallback auth for YouTube)
        cookies_path = setup_cookies()
        if cookies_path and os.path.isfile(cookies_path):
            opts["cookiefile"] = cookies_path
            log("Using cookies for authentication")

        # PO Token provider is auto-detected by yt-dlp if
        # bgutil-ytdlp-pot-provider plugin is installed and
        # the bgutil-pot server is running on port 4416
        log("PO Token provider should be active (bgutil-pot server)")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            result["title"] = info.get("title", "video") if info else "video"

        result["status"] = "completed"
        for f in os.listdir(output_dir):
            fpath = os.path.join(output_dir, f)
            if os.path.isfile(fpath):
                result["files"].append({
                    "filename": f,
                    "size": os.path.getsize(fpath),
                })

    except Exception as e:
        result["error"] = str(e)[:300]
        log(f"Error: {result['error']}")

        # XHS fallback
        if is_xhs_url(url):
            log("Trying XHS fallback...")
            try:
                count, title, _ = download_xhs_content(url, output_dir, logger=log)
                if count > 0:
                    result["status"] = "completed"
                    result["title"] = title
                    result["error"] = None
                    for f in os.listdir(output_dir):
                        fpath = os.path.join(output_dir, f)
                        if os.path.isfile(fpath):
                            result["files"].append({
                                "filename": f,
                                "size": os.path.getsize(fpath),
                            })
            except Exception:
                pass

    return result


def main():
    parser = argparse.ArgumentParser(description="Download video via GitHub Actions")
    parser.add_argument("--url", required=True, help="Video URL to download")
    parser.add_argument("--format", default="best_video", choices=FORMAT_MAP.keys())
    parser.add_argument("--task-id", default=None, help="Task ID for tracking")
    args = parser.parse_args()

    task_id = args.task_id or f"action_{int(time.time())}"
    output_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(output_dir, exist_ok=True)

    urls = extract_urls(args.url)
    if not urls:
        urls = [args.url]

    log(f"Task: {task_id}")
    log(f"URLs: {urls}")
    log(f"Format: {args.format}")

    results = []
    for url in urls:
        if is_channel_url(url):
            log(f"Skipping channel URL: {url}")
            results.append({"url": url, "status": "skipped", "error": "Channel/playlist not supported"})
            continue
        r = download_single(url, args.format, output_dir)
        results.append(r)
        log(f"Result: {r['status']} - {r.get('title', 'N/A')}")

    summary = {
        "task_id": task_id,
        "results": results,
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    }

    results_file = os.path.join(output_dir, "results.json")
    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log(f"Done: {summary['success']}/{summary['total']} succeeded")

    # Set GitHub Actions outputs
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"task_id={task_id}\n")
            f.write(f"success_count={summary['success']}\n")
            f.write(f"total_count={summary['total']}\n")

    if summary["success"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
