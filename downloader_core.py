"""
Core download logic extracted from video_downloader.py
No GUI dependencies (no customtkinter, no tkinter).
Used by web_app.py for the web interface.
"""

import os
import sys
import re
import json
import tempfile
import time
import http.cookiejar
import requests
import logging
from urllib.parse import urlparse, parse_qs

import yt_dlp

# Sites that need Selenium-based cookie/download handling
SELENIUM_SITES = ["douyin.com", "iesdouyin.com"]


def extract_urls(text):
    """Extract all valid URLs from pasted text (Douyin share text, etc)."""
    url_pattern = r'https?://[^\s<>"\'，。！？）》\]\)]*'
    urls = re.findall(url_pattern, text)
    cleaned = []
    for url in urls:
        url = url.rstrip('.,;:!?)')
        if url:
            cleaned.append(url)
    return cleaned


def is_channel_url(url):
    """Check if URL points to a channel/user profile rather than a single video."""
    channel_patterns = [
        r'youtube\.com/@',
        r'youtube\.com/c/',
        r'youtube\.com/channel/',
        r'youtube\.com/user/',
        r'douyin\.com/user/',
    ]
    if any(re.search(p, url) for p in channel_patterns):
        return True
    if re.search(r'facebook\.com/[^/]+/?(\?.*)?$', url):
        return True
    return False


def needs_selenium(url):
    """Check if URL requires Selenium-based download."""
    return any(site in url for site in SELENIUM_SITES)


def resolve_short_url(url, proxy=None):
    """Resolve short URLs (like v.douyin.com) to full URLs."""
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = requests.head(url, allow_redirects=True, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"}, proxies=proxies)
        return resp.url
    except Exception:
        return url


def normalize_douyin_url(url):
    """Convert Douyin search/note/share URLs to standard /video/ URLs."""
    parsed = urlparse(url)
    if not any(d in parsed.netloc for d in ["douyin.com", "iesdouyin.com"]):
        return url
    if "/video/" in parsed.path:
        return url
    params = parse_qs(parsed.query)
    modal_id = params.get("modal_id", [None])[0]
    if modal_id and modal_id.isdigit():
        return f"https://www.douyin.com/video/{modal_id}"
    note_match = re.search(r'/note/(\d+)', url)
    if note_match:
        return f"https://www.douyin.com/video/{note_match.group(1)}"
    return url


def export_cookies_netscape(cookies, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expires = str(c.get("expires", 0))
            name = c.get("name", "")
            value = c.get("value", "")
            f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")


def is_xhs_url(url):
    """Check if URL is a Xiaohongshu link (any format)."""
    return any(d in url for d in [
        "xiaohongshu.com", "xhslink.com", "xhs.link",
    ])


def normalize_xhs_url(url, proxy=None):
    """Normalize any XHS URL to standard explore format, keeping xsec_token."""
    # Resolve short links first
    if any(d in url for d in ["xhslink.com", "xhs.link"]):
        url = resolve_short_url(url, proxy=proxy)

    # Extract note_id from various patterns
    id_match = re.search(r'/(?:explore|discovery/item)/([\da-f]+)', url)
    if id_match:
        note_id = id_match.group(1)
        # Preserve xsec_token if present
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        xsec = params.get("xsec_token", [None])[0]
        full_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec:
            full_url += f"?xsec_token={xsec}"
        return full_url

    return url


def _parse_xhs_page(url, logger=None, proxy=None):
    """
    Fetch and parse XHS page __INITIAL_STATE__.
    Returns (note_detail_dict, page_html) or (None, None) on failure.
    """
    log = logger or print

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # Extract note_id
    id_match = re.search(r'/(?:explore|discovery/item)/([\da-f]+)', url)
    note_id = id_match.group(1) if id_match else None

    try:
        resp = requests.get(url, headers=headers, timeout=20, proxies=proxies)
        if resp.status_code != 200:
            log(f"  [XHS] Page HTTP {resp.status_code}")
            return None, None
        page_html = resp.text
    except Exception as e:
        log(f"  [XHS] Could not fetch page: {str(e)[:80]}")
        return None, None

    # Parse __INITIAL_STATE__
    state_match = re.search(
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?})\s*</script>',
        page_html, re.DOTALL,
    )
    if not state_match:
        log("  [XHS] Could not find __INITIAL_STATE__ (may need cookies/proxy)")
        return None, page_html

    try:
        json_str = re.sub(r'(?<=[:,\[{])\s*undefined\b', 'null', state_match.group(1))
        state = json.loads(json_str)
    except json.JSONDecodeError:
        log("  [XHS] Could not parse __INITIAL_STATE__ JSON")
        return None, page_html

    # Navigate to note detail
    note_map = state.get("note", {}).get("noteDetailMap", {})
    note_detail = None
    if note_id and note_id in note_map:
        note_detail = note_map[note_id].get("note", {})
    elif note_map:
        first_key = next(iter(note_map))
        note_detail = note_map[first_key].get("note", {})

    if not note_detail:
        log("  [XHS] No note detail found in page data")
        return None, page_html

    return note_detail, page_html


def download_xhs_content(url, output_dir, logger=None, proxy=None, progress_cb=None):
    """
    Download content from Xiaohongshu — video OR images.
    Handles all XHS URL types (explore, discovery, xhslink, with xsec_token).

    Returns (success_count, title, content_type)
        content_type: "video" | "images" | None
    """
    log = logger or print

    # Normalize URL first
    url = normalize_xhs_url(url, proxy=proxy)
    log(f"  [XHS] URL: {url}")

    note_detail, page_html = _parse_xhs_page(url, logger=log, proxy=proxy)
    if not note_detail:
        return 0, None, None

    # Extract title
    title = note_detail.get("title") or note_detail.get("desc") or "xhs_video"
    # Clean title for display
    try:
        log(f"  [XHS] Title: {title}")
    except UnicodeEncodeError:
        log(f"  [XHS] Title: {title.encode('ascii', errors='replace').decode('ascii')}")

    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:50]
    id_match = re.search(r'/(?:explore|discovery/item)/([\da-f]+)', url)
    note_id = id_match.group(1) if id_match else "unknown"
    if not safe_title:
        safe_title = f"xhs_{note_id}"

    post_type = note_detail.get("type", "normal")
    log(f"  [XHS] Post type: {post_type}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.xiaohongshu.com/",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # ────────────────────────────────────────────────────────
    # VIDEO POST
    # ────────────────────────────────────────────────────────
    video_data = note_detail.get("video", {})
    if post_type == "video" or video_data.get("media") or video_data.get("consumer"):
        log("  [XHS] Detected VIDEO post, extracting video URL...")

        video_url = None

        # Strategy 1: originVideoKey (highest quality, no watermark)
        origin_key = video_data.get("consumer", {}).get("originVideoKey")
        if origin_key:
            video_url = f"https://sns-video-bd.xhscdn.com/{origin_key}"
            log(f"  [XHS] Found origin video key: {origin_key[:40]}...")

        # Strategy 2: stream URLs (h264 > h265 > av1)
        if not video_url:
            streams = video_data.get("media", {}).get("stream", {})
            for codec in ["h264", "h265", "av1"]:
                codec_streams = streams.get(codec, [])
                if codec_streams:
                    # Pick highest quality (last or sorted by avgBitrate)
                    best = max(codec_streams, key=lambda s: s.get("avgBitrate", 0) or 0)
                    video_url = best.get("masterUrl")
                    if not video_url:
                        # Try backup URLs
                        for backup in best.get("backupUrls", []):
                            if backup:
                                video_url = backup
                                break
                    if video_url:
                        quality = best.get("qualityType", "unknown")
                        log(f"  [XHS] Found {codec} stream ({quality})")
                        break

        # Strategy 3: og:video meta tag from HTML
        if not video_url and page_html:
            og_match = re.search(
                r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)',
                page_html,
            )
            if og_match:
                video_url = og_match.group(1)
                log("  [XHS] Found og:video meta URL")

        # Strategy 4: any video URL in page HTML
        if not video_url and page_html:
            vid_match = re.search(
                r'(https?://sns-video[^"\'\\<>\s]+\.mp4[^"\'\\<>\s]*)',
                page_html,
            )
            if vid_match:
                video_url = vid_match.group(1)
                log("  [XHS] Found video URL in page source")

        if video_url:
            # Ensure https
            if video_url.startswith("//"):
                video_url = "https:" + video_url

            filepath = os.path.join(output_dir, f"{safe_title}.mp4")
            log(f"  [XHS] Downloading video: {safe_title}.mp4")

            try:
                vid_resp = requests.get(
                    video_url, headers=headers, timeout=120,
                    proxies=proxies, stream=True,
                )
                if vid_resp.status_code != 200:
                    log(f"  [XHS] Video download HTTP {vid_resp.status_code}")
                    # Try backup CDN
                    alt_url = video_url.replace("sns-video-bd", "sns-video-al")
                    if alt_url != video_url:
                        log("  [XHS] Trying backup CDN...")
                        vid_resp = requests.get(
                            alt_url, headers=headers, timeout=120,
                            proxies=proxies, stream=True,
                        )
                    if vid_resp.status_code != 200:
                        log(f"  [XHS] Video download failed: HTTP {vid_resp.status_code}")
                        return 0, title, None

                total = int(vid_resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(filepath, "wb") as f:
                    for chunk in vid_resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total > 0:
                            progress_cb(downloaded / total, downloaded, total)

                size_mb = os.path.getsize(filepath) / 1024 / 1024
                log(f"  [XHS] Video saved: {filepath} ({size_mb:.1f} MB)")
                return 1, title, "video"

            except Exception as e:
                log(f"  [XHS] Video download error: {str(e)[:80]}")
                return 0, title, None
        else:
            log("  [XHS] Video post but no video URL found (may need cookies)")

    # ────────────────────────────────────────────────────────
    # IMAGE POST (or video fallback to thumbnail images)
    # ────────────────────────────────────────────────────────
    image_list = note_detail.get("imageList", [])
    if not image_list:
        log("  [XHS] No images found in this post")
        return 0, title, None

    log(f"  [XHS] Found {len(image_list)} images, downloading...")

    success_count = 0
    for i, img_info in enumerate(image_list, 1):
        img_url = None
        # Try urlDefault (highest quality), then urlPre
        for key in ["urlDefault", "urlPre"]:
            u = img_info.get(key)
            if u:
                img_url = u
                break
        # Fallback: infoList with WB_DFT (default) first, then WB_PRV (preview)
        if not img_url:
            info_list = img_info.get("infoList", [])
            # Prefer WB_DFT (default/full quality) over WB_PRV (preview)
            for scene in ["WB_DFT", "WB_PRV"]:
                for info in info_list:
                    if info.get("imageScene") == scene and info.get("url"):
                        img_url = info["url"]
                        break
                if img_url:
                    break
            # Last resort: any URL in infoList
            if not img_url:
                for info in info_list:
                    u = info.get("url")
                    if u:
                        img_url = u
                        break

        if not img_url:
            log(f"  [XHS] Image {i}: no URL found, skipping")
            continue
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        # Upgrade http to https
        if img_url.startswith("http://"):
            img_url = "https://" + img_url[7:]

        try:
            img_resp = requests.get(img_url, headers=headers, timeout=30, proxies=proxies)
            if img_resp.status_code != 200:
                log(f"  [XHS] Image {i}: HTTP {img_resp.status_code}")
                continue
            ct = img_resp.headers.get("Content-Type", "")
            if "png" in ct:
                ext = "png"
            elif "webp" in ct:
                ext = "webp"
            else:
                ext = "jpg"
            filepath = os.path.join(output_dir, f"{safe_title}_{i}.{ext}")
            with open(filepath, "wb") as f:
                f.write(img_resp.content)
            success_count += 1
            if progress_cb:
                progress_cb(i / len(image_list), 0, 0)
        except Exception as e:
            log(f"  [XHS] Image {i}: {str(e)[:60]}")

    log(f"  [XHS] Downloaded {success_count}/{len(image_list)} images")
    return success_count, title, "images"


# Keep backward compatibility
def download_xhs_images(url, output_dir, logger=None, proxy=None, progress_cb=None):
    """Legacy wrapper — now calls download_xhs_content()."""
    count, title, _ = download_xhs_content(url, output_dir, logger=logger,
                                            proxy=proxy, progress_cb=progress_cb)
    return count, title


class SeleniumDownloader:
    """Download videos from sites that need browser JS execution (Douyin, etc)."""

    SITE_CONFIG = {
        "douyin.com": {
            "api_path": "/aweme/v1/web/aweme/detail/",
            "api_params": {"aid": "6383", "channel": "channel_pc_web", "device_platform": "webapp"},
            "id_pattern": r"/video/(\d+)",
            "id_param": "aweme_id",
            "data_path": ["aweme_detail"],
            "video_path": ["video", "bit_rate", 0, "play_addr", "url_list", 0],
            "video_fallback": ["video", "play_addr", "url_list", 0],
            "title_path": ["desc"],
        },
    }

    def __init__(self, logger=None, proxy=None):
        self.logger = logger or print
        self.proxy = proxy

    def _get_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--lang=zh-CN")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        if self.proxy:
            options.add_argument(f"--proxy-server={self.proxy}")
        return webdriver.Chrome(options=options)

    def _traverse(self, data, path):
        current = data
        for key in path:
            if current is None:
                return None
            if isinstance(key, int):
                if isinstance(current, list) and len(current) > key:
                    current = current[key]
                else:
                    return None
            else:
                current = current.get(key) if isinstance(current, dict) else None
        return current

    def download(self, url, output_dir, format_choice="best_video", progress_cb=None):
        """
        Download video using Selenium + in-browser API call.
        Returns (success, title, filepath) tuple.
        """
        full_url = resolve_short_url(url, proxy=self.proxy)
        full_url = normalize_douyin_url(full_url)
        self.logger(f"  Resolved: {full_url}")

        config = None
        for domain, cfg in self.SITE_CONFIG.items():
            if domain in full_url:
                config = cfg
                break

        if not config:
            return False, None, "Unsupported site for Selenium download"

        match = re.search(config["id_pattern"], full_url)
        if not match:
            return False, None, "Could not extract video ID from URL"
        video_id = match.group(1)
        self.logger(f"  Video ID: {video_id}")

        driver = None
        try:
            self.logger("  Launching browser...")
            driver = self._get_driver()

            page_url = full_url.split("?")[0]
            self.logger("  Loading page...")
            driver.get(page_url)
            time.sleep(10)

            api_path = config["api_path"]
            params = dict(config["api_params"])
            params[config["id_param"]] = video_id
            query_str = "&".join(f"{k}={v}" for k, v in params.items())

            self.logger("  Fetching video info via API...")
            js_code = f"""
                return new Promise((resolve, reject) => {{
                    fetch('{api_path}?{query_str}', {{
                        credentials: 'include',
                        headers: {{'Accept': 'application/json', 'Referer': window.location.href}}
                    }})
                    .then(r => r.text())
                    .then(text => resolve(text))
                    .catch(e => resolve('FETCH_ERROR: ' + e.message));
                }});
            """
            result = driver.execute_script(js_code)

            if not result or result.startswith("FETCH_ERROR"):
                return False, None, f"API call failed: {result}"

            data = json.loads(result)
            detail = self._traverse(data, config["data_path"])
            if not detail:
                return False, None, "No video detail in API response"

            title = self._traverse(detail, config["title_path"]) or f"video_{video_id}"
            try:
                self.logger(f"  Title: {title}")
            except UnicodeEncodeError:
                safe_display = title.encode("ascii", errors="replace").decode("ascii")
                self.logger(f"  Title: {safe_display}")

            video_url = self._traverse(detail, config["video_path"])
            if not video_url:
                video_url = self._traverse(detail, config["video_fallback"])
            if not video_url:
                return False, None, "Could not find video URL in API response"

            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Referer": full_url,
            }

            self.logger("  Downloading video file...")
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            resp = requests.get(video_url, headers=headers, cookies=cookies,
                                stream=True, timeout=60, proxies=proxies)

            if resp.status_code != 200:
                return False, title, f"Download HTTP {resp.status_code}"

            safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:60]
            if not safe_title:
                safe_title = f"video_{video_id}"
            filepath = os.path.join(output_dir, f"{safe_title}.mp4")

            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total_size > 0:
                        progress_cb(downloaded / total_size, downloaded, total_size)

            actual_size = os.path.getsize(filepath)
            self.logger(f"  Saved: {filepath} ({actual_size / 1024 / 1024:.1f} MB)")
            return True, title, filepath

        except Exception as e:
            return False, None, str(e)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass


class CookieManager:
    BROWSERS = ["chrome", "edge", "firefox", "brave", "opera", "chromium", "vivaldi"]

    def __init__(self, logger=None):
        self.logger = logger or print
        self.cookie_file = None
        self._extraction_attempted = False
        self._temp_dir = tempfile.mkdtemp(prefix="vidl_cookies_")

    def cleanup(self):
        if self.cookie_file and os.path.exists(self.cookie_file):
            try:
                os.unlink(self.cookie_file)
            except Exception:
                pass

    def try_rookiepy(self, browser_name):
        try:
            import rookiepy
        except ImportError:
            return None
        extract_fn = getattr(rookiepy, browser_name, None)
        if not extract_fn:
            return None
        try:
            cookies = extract_fn()
            if cookies:
                cookie_path = os.path.join(self._temp_dir, f"{browser_name}_cookies.txt")
                export_cookies_netscape(cookies, cookie_path)
                self.logger(f"[Cookie] rookiepy: {len(cookies)} cookies from {browser_name}")
                return cookie_path
        except Exception as e:
            self.logger(f"[Cookie] rookiepy {browser_name}: {str(e)[:60]}")
        return None

    def try_yt_dlp_browser(self, browser_name):
        try:
            from yt_dlp.cookies import extract_cookies_from_browser

            class QuietLogger:
                def debug(self, msg): pass
                def info(self, msg): pass
                def warning(self, msg): pass
                def error(self, msg): pass

            jar = extract_cookies_from_browser(browser_name, None, QuietLogger())
            if jar and len(jar):
                cookie_path = os.path.join(self._temp_dir, f"{browser_name}_ytdlp_cookies.txt")
                jar.save(cookie_path, ignore_discard=True, ignore_expires=True)
                self.logger(f"[Cookie] yt-dlp: {len(list(jar))} cookies from {browser_name}")
                return cookie_path
        except Exception as e:
            self.logger(f"[Cookie] yt-dlp {browser_name}: {str(e)[:60]}")
        return None

    def auto_extract(self, preferred_browser=None):
        if self._extraction_attempted:
            return self.cookie_file
        self._extraction_attempted = True

        browsers = list(self.BROWSERS)
        if preferred_browser and preferred_browser in browsers:
            browsers.remove(preferred_browser)
            browsers.insert(0, preferred_browser)

        self.logger("[Cookie] Trying rookiepy...")
        for browser in browsers:
            result = self.try_rookiepy(browser)
            if result:
                self.cookie_file = result
                return result

        self.logger("[Cookie] Trying yt-dlp browser cookies...")
        for browser in browsers:
            result = self.try_yt_dlp_browser(browser)
            if result:
                self.cookie_file = result
                return result

        self.logger("[Cookie] Auto-extraction failed. Proceeding without cookies.")
        return None

    def get_ydl_cookie_opts(self, preferred_browser=None, manual_cookie_file=None):
        if manual_cookie_file and os.path.exists(manual_cookie_file):
            return {"cookiefile": manual_cookie_file}
        cookie_path = self.auto_extract(preferred_browser)
        if cookie_path:
            return {"cookiefile": cookie_path}
        return {}
