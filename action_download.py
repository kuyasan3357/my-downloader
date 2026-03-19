"""
GitHub Actions download wrapper.
Calls downloader_core.py logic directly, outputs results as JSON.
Supports upload to Telegram Bot and Google Drive.
"""

import os
import sys
import json
import time
import argparse
import mimetypes

import requests as http_requests
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


###############################################################################
# Upload helpers
###############################################################################

def upload_to_telegram(file_path, task_id):
    """Upload file to Telegram via Bot API. Returns message info or None."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        log("Telegram: skipped (no BOT_TOKEN or CHAT_ID)")
        return None

    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    size_mb = file_size / (1024 * 1024)
    log(f"Telegram: uploading {filename} ({size_mb:.1f} MB)")

    # Telegram Bot API limit: 50MB for sendDocument, 2GB for local bot API
    if file_size > 50 * 1024 * 1024:
        log(f"Telegram: file too large ({size_mb:.1f} MB > 50MB limit), sending as link")
        return None

    try:
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        is_video = mime.startswith("video/")

        if is_video:
            url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
            with open(file_path, "rb") as f:
                resp = http_requests.post(url, data={
                    "chat_id": chat_id,
                    "caption": f"📥 {filename}\n🆔 Task: {task_id}",
                    "supports_streaming": "true",
                }, files={"video": (filename, f, mime)}, timeout=600)
        else:
            url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
            with open(file_path, "rb") as f:
                resp = http_requests.post(url, data={
                    "chat_id": chat_id,
                    "caption": f"📥 {filename}\n🆔 Task: {task_id}",
                }, files={"document": (filename, f, mime)}, timeout=600)

        data = resp.json()
        if data.get("ok"):
            log(f"Telegram: uploaded {filename} successfully")
            return data.get("result")
        else:
            log(f"Telegram: failed - {data.get('description', 'unknown error')}")
            return None
    except Exception as e:
        log(f"Telegram: error - {e}")
        return None


def upload_to_gdrive(file_path, task_id):
    """Upload file to Google Drive via service account. Returns file link or None."""
    creds_json = os.environ.get("GDRIVE_CREDENTIALS", "").strip()
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    if not creds_json or not folder_id:
        log("Google Drive: skipped (no GDRIVE_CREDENTIALS or GDRIVE_FOLDER_ID)")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        # Parse service account credentials
        creds_data = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_data,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        service = build("drive", "v3", credentials=creds)

        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        size_mb = file_size / (1024 * 1024)
        log(f"Google Drive: uploading {filename} ({size_mb:.1f} MB)")

        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        file_metadata = {
            "name": filename,
            "parents": [folder_id],
        }
        media = MediaFileUpload(file_path, mimetype=mime, resumable=True)
        gfile = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
        ).execute()

        file_id = gfile.get("id")
        # Make file viewable by anyone with link
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

        link = gfile.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
        log(f"Google Drive: uploaded {filename} -> {link}")
        return link

    except ImportError:
        log("Google Drive: skipped (google-api-python-client not installed)")
        return None
    except Exception as e:
        log(f"Google Drive: error - {e}")
        return None


def upload_files(output_dir, task_id):
    """Upload all media files in output_dir to Telegram and Google Drive.
    Returns dict with upload results for callback."""
    upload_results = {"telegram": [], "gdrive": []}

    media_exts = {".mp4", ".mp3", ".webm", ".m4a", ".mkv", ".avi",
                  ".jpg", ".jpeg", ".png", ".webp", ".gif"}

    for fname in sorted(os.listdir(output_dir)):
        fpath = os.path.join(output_dir, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in media_exts and fname != "results.json":
            # Also try files without known ext (might be video)
            if os.path.getsize(fpath) < 1024:
                continue
        if fname == "results.json":
            continue

        # Telegram
        tg_result = upload_to_telegram(fpath, task_id)
        if tg_result:
            upload_results["telegram"].append({
                "filename": fname,
                "message_id": tg_result.get("message_id"),
            })

        # Google Drive
        gd_link = upload_to_gdrive(fpath, task_id)
        if gd_link:
            upload_results["gdrive"].append({
                "filename": fname,
                "link": gd_link,
            })

    return upload_results


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

    # Upload files to Telegram + Google Drive
    upload_results = {"telegram": [], "gdrive": []}
    if summary["success"] > 0:
        log("=== Starting uploads (Telegram + Google Drive) ===")
        upload_results = upload_files(output_dir, task_id)
        summary["uploads"] = upload_results
        log(f"Uploads done: Telegram={len(upload_results['telegram'])}, GDrive={len(upload_results['gdrive'])}")

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
            # Export upload links for callback
            if upload_results["gdrive"]:
                gdrive_links = ",".join(r["link"] for r in upload_results["gdrive"])
                f.write(f"gdrive_links={gdrive_links}\n")
            if upload_results["telegram"]:
                f.write(f"telegram_uploaded=true\n")

    if summary["success"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
