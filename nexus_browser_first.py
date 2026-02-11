#!/usr/bin/env python3
"""
Browser-first Nexus collection helper (V2 minimal prototype).

This script connects to an already running Chromium-based browser via CDP so it
can use the user's normal logged-in profile/session. It then:
1) loads a collection mods page,
2) extracts mod links,
3) attempts page-driven download clicks (Manual -> Slow).

It writes per-run diagnostics to logs/ for pass/fail analysis.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import re
import shutil
import ssl
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

COLLECTION_URL_RE = re.compile(
    r"^https?://(?:www\.)?nexusmods\.com/games/[^/]+/collections/[^/?#]+(?:/mods)?/?$",
    re.IGNORECASE,
)

MOD_LINK_RE = re.compile(r"^https?://(?:www\.)?nexusmods\.com/[^/]+/mods/\d+/?(?:\?.*)?$", re.IGNORECASE)
COLLECTION_DOMAIN_RE = re.compile(r"/games/([^/]+)/collections/", re.IGNORECASE)
MOD_PATH_RE = re.compile(r"^/[^/]+/mods/\d+/?$", re.IGNORECASE)
GAME_ID_RE = re.compile(r"/images/games/v2/(\d+)/")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

COOKIE_SELECTORS = [
    "button:has-text('Accept')",
    "button:has-text('Accept all')",
    "button:has-text('I agree')",
]

MANUAL_SELECTORS = [
    "button:has-text('Manual'):visible",
    "a:has-text('Manual'):visible",
    "button:has-text('Manual download'):visible",
    "a:has-text('Manual download'):visible",
]

SLOW_SELECTORS = [
    "button:has-text('Slow download'):visible",
    "a:has-text('Slow download'):visible",
    "text=Slow download",
    "button:has-text('Free download'):visible",
    "a:has-text('Free download'):visible",
]

TEMP_DOWNLOAD_EXTENSIONS = {".crdownload", ".part", ".tmp"}
SUSPICIOUS_FILENAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class ItemResult:
    index: int
    mod_url: str
    status: str
    reason: str


@dataclass
class ExtractionResult:
    links: list[str]
    strategy: str
    details: dict[str, Any]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def clean_collection_url(url: str) -> str:
    parsed = urlparse(url.strip())
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    if not base.endswith("/mods"):
        base = base + "/mods"
    return base


def click_first_visible(page: Any, selectors: list[str], timeout_sec: float) -> Optional[str]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = locator.count()
                if count <= 0:
                    continue
                limit = min(count, 8)
                for i in range(limit):
                    target = locator.nth(i)
                    if target.is_visible():
                        target.click(timeout=1500)
                        return selector
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        page.wait_for_timeout(250)
    return None


def list_candidate_files(download_dir: Path) -> list[Path]:
    if not download_dir.exists():
        return []
    return [p for p in download_dir.iterdir() if p.is_file()]


def is_temp_download(path: Path) -> bool:
    return path.suffix.lower() in TEMP_DOWNLOAD_EXTENSIONS


def wait_until_file_is_stable(path: Path, checks: int = 3, interval_sec: float = 1.0) -> bool:
    if not path.exists() or is_temp_download(path):
        return False
    sizes = []
    for _ in range(checks):
        if not path.exists():
            return False
        sizes.append(path.stat().st_size)
        time.sleep(interval_sec)
    return len(set(sizes)) == 1


def wait_for_new_completed_download(download_dir: Path, baseline: set[Path], timeout_sec: int) -> Optional[Path]:
    start = time.time()
    while True:
        current = set(list_candidate_files(download_dir))
        new_files = [p for p in current - baseline if not is_temp_download(p)]
        for path in sorted(new_files, key=lambda p: p.stat().st_mtime, reverse=True):
            if wait_until_file_is_stable(path):
                return path
        if time.time() - start > timeout_sec:
            return None
        time.sleep(1.0)


def is_good_archive_name(name: str) -> bool:
    lower = name.lower()
    if lower.endswith((".zip", ".7z", ".rar")):
        return True
    stem = Path(name).name
    if SUSPICIOUS_FILENAME_RE.match(stem):
        return False
    return False


def extract_game_id(html: str) -> Optional[int]:
    m = GAME_ID_RE.search(html)
    if m:
        return int(m.group(1))
    return None


def extract_mod_links(page: Any) -> list[str]:
    links = page.locator("a[href*='/mods/']").evaluate_all("els => els.map(e => e.href)")
    normalized: list[str] = []
    for href in links:
        if not isinstance(href, str):
            continue
        target = normalize_mod_target_url(href)
        if target is not None:
            normalized.append(target)
    return dedupe_links(normalized)


def extract_collection_domain(collection_url: str) -> Optional[str]:
    m = COLLECTION_DOMAIN_RE.search(collection_url)
    if not m:
        return None
    return m.group(1).lower()


def dedupe_links(links: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for link in links:
        canonical = normalize_mod_target_url(link)
        if canonical is None:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def normalize_mod_target_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in ("www.nexusmods.com", "nexusmods.com"):
        return None
    path = parsed.path.rstrip("/")
    if not MOD_PATH_RE.match(path):
        return None
    query = parse_qs(parsed.query)
    file_id_values = query.get("file_id", [])
    file_id: Optional[int] = None
    if file_id_values:
        try:
            file_id = int(file_id_values[0])
        except (TypeError, ValueError):
            file_id = None
    base = f"https://www.nexusmods.com{path}"
    if file_id is not None and file_id > 0:
        return f"{base}?{urlencode({'tab': 'files', 'file_id': file_id})}"
    return base


def parse_mod_target(url: str) -> tuple[Optional[str], Optional[int], Optional[int]]:
    normalized = normalize_mod_target_url(url)
    if not normalized:
        return None, None, None
    parsed = urlparse(normalized)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 3 or parts[1] != "mods":
        return None, None, None
    domain = parts[0].lower()
    try:
        mod_id = int(parts[2])
    except ValueError:
        return None, None, None
    q = parse_qs(parsed.query)
    try:
        file_id = int(q["file_id"][0]) if "file_id" in q and q["file_id"] else None
    except (TypeError, ValueError):
        file_id = None
    return domain, mod_id, file_id


def resolve_download_url_via_web(cookie_header: str, mod_url: str, game_id: int, file_id: int) -> str:
    req = urllib.request.Request(
        "https://www.nexusmods.com/Core/Libs/Common/Managers/Downloads?GenerateDownloadUrl",
        data=urllib.parse.urlencode({"fid": str(file_id), "game_id": str(game_id)}).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.nexusmods.com",
            "Referer": mod_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        for key in ("url", "URI", "uri"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_download_url(value)
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict):
                for key in ("url", "URI", "uri"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        return normalize_download_url(value)
    raise RuntimeError(f"Unexpected download URL payload: {raw[:200]}")


def normalize_download_url(raw_url: str) -> str:
    """Convert Nexus returned URL/URI into a safe absolute URL."""
    value = raw_url.strip()
    if not value:
        raise RuntimeError("Empty download URL")

    parsed = urlparse(value)
    # Some payloads return relative URI like:
    # /1303/2113/Filename With Spaces.zip?md5=...&expires=...
    if not parsed.scheme:
        value = urllib.parse.urljoin("https://www.nexusmods.com", value)
        parsed = urlparse(value)

    path = urllib.parse.quote(parsed.path, safe="/._-~")
    query = urllib.parse.quote_plus(parsed.query, safe="=&:_-~")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def filename_from_response_headers(url: str, headers: Any, fallback_name: str) -> str:
    content_disposition = headers.get("Content-Disposition", "") if headers else ""
    m_utf = re.search(r"filename\\*=UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE)
    if m_utf:
        return urllib.parse.unquote(m_utf.group(1))
    m_std = re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.IGNORECASE)
    if m_std:
        return m_std.group(1).strip()
    path_name = Path(urllib.parse.urlparse(url).path).name
    if path_name:
        return path_name
    return fallback_name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(1, 10000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique path for {path}")


def direct_download_to_folder(url: str, download_dir: Path, fallback_name: str) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as response:
        file_name = filename_from_response_headers(url, response.headers, fallback_name)
        file_name = file_name.strip() or fallback_name
        if not Path(file_name).suffix:
            file_name = file_name + ".zip"
        target = unique_path(download_dir / file_name)
        temp = target.with_suffix(target.suffix + ".part")
        with temp.open("wb") as fh:
            shutil.copyfileobj(response, fh, length=1024 * 1024)
    temp.replace(target)
    return target


def resolve_download_url_via_web_with_context(
    cookie_header: str,
    mod_url: str,
    game_id: int,
    file_id: int,
    ssl_context: Optional[ssl.SSLContext],
) -> str:
    req = urllib.request.Request(
        "https://www.nexusmods.com/Core/Libs/Common/Managers/Downloads?GenerateDownloadUrl",
        data=urllib.parse.urlencode({"fid": str(file_id), "game_id": str(game_id)}).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.nexusmods.com",
            "Referer": mod_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60, context=ssl_context) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        for key in ("url", "URI", "uri"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_download_url(value)
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict):
                for key in ("url", "URI", "uri"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        return normalize_download_url(value)
    raise RuntimeError(f"Unexpected download URL payload: {raw[:200]}")


def direct_download_to_folder_with_context(
    url: str,
    download_dir: Path,
    fallback_name: str,
    ssl_context: Optional[ssl.SSLContext],
) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180, context=ssl_context) as response:
        file_name = filename_from_response_headers(url, response.headers, fallback_name)
        file_name = file_name.strip() or fallback_name
        if not Path(file_name).suffix:
            file_name = file_name + ".zip"
        target = unique_path(download_dir / file_name)
        temp = target.with_suffix(target.suffix + ".part")
        with temp.open("wb") as fh:
            shutil.copyfileobj(response, fh, length=1024 * 1024)
    temp.replace(target)
    return target


def is_ssl_verify_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "certificate verify failed" in text or "self-signed certificate" in text


def links_from_collection_payload(payload: Any, domain: Optional[str]) -> list[str]:
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    revision = data.get("collectionRevision")
    if not isinstance(revision, dict):
        return []

    links: list[str] = []
    mod_files = revision.get("modFiles")
    if isinstance(mod_files, list):
        for entry in mod_files:
            if not isinstance(entry, dict):
                continue
            mod_id = entry.get("modId")
            file_id = entry.get("fileId")
            if mod_id is None and isinstance(entry.get("file"), dict):
                mod_obj = entry["file"].get("mod")
                if isinstance(mod_obj, dict):
                    mod_id = mod_obj.get("modId") or mod_obj.get("id")
                file_obj = entry.get("file")
                if isinstance(file_obj, dict):
                    file_id = file_id or file_obj.get("fileId") or file_obj.get("id")
            try:
                mod_id_int = int(mod_id)
            except (TypeError, ValueError):
                continue
            try:
                file_id_int = int(file_id) if file_id is not None else None
            except (TypeError, ValueError):
                file_id_int = None
            if domain:
                if file_id_int is not None and file_id_int > 0:
                    links.append(
                        f"https://www.nexusmods.com/{domain}/mods/{mod_id_int}?{urlencode({'tab': 'files', 'file_id': file_id_int})}"
                    )
                else:
                    links.append(f"https://www.nexusmods.com/{domain}/mods/{mod_id_int}")
    return dedupe_links(links)


def collect_links_via_network(page: Any, collection_url: str, wait_ms: int = 12000) -> ExtractionResult:
    domain = extract_collection_domain(collection_url)
    payload_candidates: list[dict[str, Any]] = []

    def on_response(response: Any) -> None:
        try:
            request = response.request
            op_header = request.headers.get("x-graphql-operationname", "")
            post_data = request.post_data or ""
            looks_relevant = (
                "CollectionRevisionMods" in op_header
                or "CollectionRevisionMods" in post_data
                or ("graphql" in response.url.lower() and "collectionrevision" in post_data.lower())
            )
            if not looks_relevant:
                return
            if response.status != 200:
                payload_candidates.append(
                    {
                        "source": "graphql_non_200",
                        "status": response.status,
                        "url": response.url,
                        "operation": op_header,
                    }
                )
                return
            body = response.json()
            payload_candidates.append(
                {
                    "source": "graphql_200",
                    "status": response.status,
                    "url": response.url,
                    "operation": op_header,
                    "body": body,
                }
            )
        except Exception:
            return

    page.on("response", on_response)
    try:
        page.goto(collection_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(wait_ms)
    finally:
        page.remove_listener("response", on_response)

    links: list[str] = []
    matched_payloads = 0
    for candidate in payload_candidates:
        body = candidate.get("body")
        if body is None:
            continue
        matched_payloads += 1
        links.extend(links_from_collection_payload(body, domain))

    links = dedupe_links(links)
    details = {
        "payload_candidates_seen": len(payload_candidates),
        "payloads_parsed": matched_payloads,
        "domain": domain,
    }
    return ExtractionResult(links=links, strategy="network_graphql", details=details)


def write_zero_queue_artifacts(page: Any, log_dir: Path, run_id: str) -> dict[str, str]:
    screenshot_path = log_dir / f"browser-first-{run_id}-zero-queue.png"
    html_path = log_dir / f"browser-first-{run_id}-zero-queue.html"
    meta_path = log_dir / f"browser-first-{run_id}-zero-queue-meta.json"
    out: dict[str, str] = {}
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        out["screenshot"] = str(screenshot_path)
    except Exception as e:
        out["screenshot_error"] = str(e)
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        out["html"] = str(html_path)
    except Exception as e:
        out["html_error"] = str(e)
    try:
        meta = {
            "url": page.url,
            "title": page.title(),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        out["meta"] = str(meta_path)
    except Exception as e:
        out["meta_error"] = str(e)
    return out


def process_mod(
    page: Any,
    mod_url: str,
    click_timeout_sec: float,
    dry_run: bool,
    verify_downloads: bool,
    downloads_dir: Path,
    download_timeout_sec: int,
    cookie_header: Optional[str],
    game_id: Optional[int],
) -> ItemResult:
    files_url = mod_url if "file_id=" in mod_url else f"{mod_url}?tab=files"
    domain_name, mod_id, file_id = parse_mod_target(mod_url)

    if verify_downloads and cookie_header and game_id and file_id and mod_id and domain_name:
        print("[i] Trying direct session download URL...")
        try:
            direct_url = resolve_download_url_via_web_with_context(
                cookie_header,
                files_url,
                game_id,
                file_id,
                ssl_context=None,
            )
            downloaded = direct_download_to_folder_with_context(
                direct_url,
                downloads_dir,
                fallback_name=f"{domain_name}-{mod_id}-{file_id}.zip",
                ssl_context=None,
            )
            if is_good_archive_name(downloaded.name):
                return ItemResult(0, mod_url, "ok", f"direct_download:{downloaded}")
            return ItemResult(0, mod_url, "partial", f"direct_download_suspicious:{downloaded.name}")
        except Exception as e:
            if is_ssl_verify_error(e):
                print("[i] Direct download hit SSL verify issue. Retrying with insecure SSL context...")
                try:
                    insecure_ctx = ssl._create_unverified_context()
                    direct_url = resolve_download_url_via_web_with_context(
                        cookie_header,
                        files_url,
                        game_id,
                        file_id,
                        ssl_context=insecure_ctx,
                    )
                    downloaded = direct_download_to_folder_with_context(
                        direct_url,
                        downloads_dir,
                        fallback_name=f"{domain_name}-{mod_id}-{file_id}.zip",
                        ssl_context=insecure_ctx,
                    )
                    if is_good_archive_name(downloaded.name):
                        return ItemResult(0, mod_url, "ok", f"direct_download_insecure_ssl:{downloaded}")
                    return ItemResult(0, mod_url, "partial", f"direct_download_suspicious:{downloaded.name}")
                except Exception as e2:
                    print(f"[i] Insecure SSL retry failed: {e2}. Falling back to click flow...")
            else:
                print(f"[i] Direct session download failed: {e}. Falling back to click flow...")

    nav_error: Optional[str] = None
    for _ in range(2):
        try:
            page.goto(files_url, wait_until="domcontentloaded", timeout=60000)
            nav_error = None
            break
        except Exception as e:
            nav_error = str(e)
            page.wait_for_timeout(1200)
    if nav_error is not None:
        return ItemResult(0, mod_url, "fail", f"navigation_error: {nav_error}")

    click_first_visible(page, COOKIE_SELECTORS, timeout_sec=2.0)

    if dry_run:
        return ItemResult(0, mod_url, "dry_run", "navigation_only")

    baseline: set[Path] = set()
    if verify_downloads:
        downloads_dir.mkdir(parents=True, exist_ok=True)
        baseline = set(list_candidate_files(downloads_dir))

    seen_downloads: list[str] = []
    saved_downloads: list[str] = []
    download_errors: list[str] = []

    def on_download(download: Any) -> None:
        try:
            name = download.suggested_filename
        except Exception:
            name = "unknown"
        seen_downloads.append(str(name))
        try:
            downloads_dir.mkdir(parents=True, exist_ok=True)
            target = unique_path(downloads_dir / str(name))
            download.save_as(str(target))
            saved_downloads.append(str(target))
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                download_errors.append(f"{name}: cancelled")
                return
            download_errors.append(f"{name}: {exc}")

    page.on("download", on_download)
    print("[i] Trying direct slow/free download click...")
    slow_selector = click_first_visible(page, SLOW_SELECTORS, timeout_sec=click_timeout_sec)
    if not slow_selector:
        print("[i] Direct slow click not found. Trying manual-then-slow path...")
        manual_selector = click_first_visible(page, MANUAL_SELECTORS, timeout_sec=click_timeout_sec)
        if not manual_selector:
            try:
                page.remove_listener("download", on_download)
            except Exception:
                pass
            return ItemResult(0, mod_url, "fallback_needed", "manual_button_not_found")

        slow_selector = click_first_visible(page, SLOW_SELECTORS, timeout_sec=click_timeout_sec)
        if not slow_selector:
            try:
                page.remove_listener("download", on_download)
            except Exception:
                pass
            return ItemResult(0, mod_url, "partial", "download_confirmation_button_not_found")

    print(f"[i] Clicked selector: {slow_selector}. Waiting for download signal...")

    if verify_downloads:
        deadline = time.time() + max(3, download_timeout_sec)
        manual_retry_used = False
        while time.time() < deadline:
            if saved_downloads:
                try:
                    page.remove_listener("download", on_download)
                except Exception:
                    pass
                return ItemResult(0, mod_url, "ok", f"download_saved:{saved_downloads[-1]}")

            if seen_downloads:
                last_name = seen_downloads[-1]
                if is_good_archive_name(last_name):
                    # Event fired but save failed so far; keep waiting briefly for save/file detection.
                    if download_errors:
                        print(f"[i] Download event save issue: {download_errors[-1]}")
                else:
                    if not manual_retry_used:
                        manual_retry_used = True
                        print(f"[i] Suspicious filename '{last_name}'. Trying manual retry link...")
                        click_first_visible(
                            page,
                            [
                                "a:has-text('click here to download manually'):visible",
                                "a:has-text('download manually'):visible",
                            ],
                            timeout_sec=5.0,
                        )
                        page.wait_for_timeout(1000)
                        continue
                    try:
                        page.remove_listener("download", on_download)
                    except Exception:
                        pass
                    return ItemResult(0, mod_url, "partial", f"suspicious_download_filename:{last_name}")

            try:
                started_text = page.locator("text=Your download has started").first
                if started_text.count() > 0 and started_text.is_visible():
                    if not manual_retry_used:
                        manual_retry_used = True
                        print("[i] Download start page detected. Triggering manual link to get file event...")
                        click_first_visible(
                            page,
                            [
                                "a:has-text('click here to download manually'):visible",
                                "a:has-text('download manually'):visible",
                            ],
                            timeout_sec=5.0,
                        )
                        page.wait_for_timeout(1000)
                        continue
            except Exception:
                pass

            downloaded = wait_for_new_completed_download(downloads_dir, baseline, timeout_sec=1)
            if downloaded:
                try:
                    page.remove_listener("download", on_download)
                except Exception:
                    pass
                return ItemResult(0, mod_url, "ok", f"download_file_detected:{downloaded.name}")

            page.wait_for_timeout(500)

        try:
            page.remove_listener("download", on_download)
        except Exception:
            pass
        if download_errors:
            return ItemResult(0, mod_url, "partial", f"download_save_error:{download_errors[-1]}")
        return ItemResult(0, mod_url, "partial", "download_signal_not_detected_in_time")

    try:
        page.remove_listener("download", on_download)
    except Exception:
        pass
    return ItemResult(0, mod_url, "ok", "manual_and_slow_clicked")


def build_summary_lines(run_id: str, run_data: dict[str, Any], json_log: Path) -> list[str]:
    ok_count = sum(1 for r in run_data["results"] if r["status"] == "ok")
    fallback_count = sum(1 for r in run_data["results"] if r["status"] == "fallback_needed")
    partial_count = sum(1 for r in run_data["results"] if r["status"] == "partial")
    fail_count = sum(1 for r in run_data["results"] if r["status"] == "fail")
    dry_count = sum(1 for r in run_data["results"] if r["status"] == "dry_run")
    return [
        f"run_id: {run_id}",
        f"collection_url: {run_data['collection_url']}",
        f"queue_count: {run_data['queue_count']}",
        f"ok: {ok_count}",
        f"fallback_needed: {fallback_count}",
        f"partial: {partial_count}",
        f"fail: {fail_count}",
        f"dry_run: {dry_count}",
        f"json_log: {json_log}",
    ]


def write_run_logs(run_id: str, run_data: dict[str, Any], json_log: Path, txt_log: Path) -> None:
    summary_lines = build_summary_lines(run_id, run_data, json_log)
    json_log.write_text(json.dumps(run_data, indent=2), encoding="utf-8")
    txt_log.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browser-first Nexus collection helper using existing logged-in browser via CDP."
    )
    parser.add_argument("--collection-url", required=True, help="Nexus collection URL.")
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="CDP endpoint for running browser, e.g. http://127.0.0.1:9222",
    )
    parser.add_argument("--max-mods", type=int, default=0, help="Limit mods processed (0 = all).")
    parser.add_argument("--click-timeout-sec", type=float, default=12.0, help="Click wait per step.")
    parser.add_argument("--delay-sec", type=float, default=1.5, help="Delay between mod attempts.")
    parser.add_argument("--dry-run", action="store_true", help="Do not click download buttons.")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="Directory for run logs.")
    parser.add_argument(
        "--verify-downloads",
        action="store_true",
        help="After clicking download, confirm a new file appears in downloads folder.",
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=Path.home() / "Downloads",
        help="Downloads folder used when --verify-downloads is enabled.",
    )
    parser.add_argument(
        "--download-timeout-sec",
        type=int,
        default=45,
        help="Seconds to wait for a new completed file when --verify-downloads is enabled.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not COLLECTION_URL_RE.match(args.collection_url.strip()):
        print("[!] Invalid collection URL format.")
        return 2

    run_id = now_stamp()
    log_dir = args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    json_log = log_dir / f"browser-first-{run_id}.json"
    txt_log = log_dir / f"browser-first-{run_id}.txt"

    run_data: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "collection_url": clean_collection_url(args.collection_url),
        "cdp_url": args.cdp_url,
        "dry_run": bool(args.dry_run),
        "max_mods": int(args.max_mods),
        "queue_count": 0,
        "queue_first_5": [],
        "results": [],
        "extraction": {},
        "verify_downloads": bool(args.verify_downloads),
        "downloads_dir": str(args.downloads_dir),
    }

    interrupted = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(args.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                cookies = context.cookies(["https://www.nexusmods.com"])
                cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            except Exception:
                cookie_header = ""

            print(f"[+] Loading collection mods page: {run_data['collection_url']}")
            extraction = collect_links_via_network(page, run_data["collection_url"])
            links = extraction.links
            run_data["extraction"]["network_graphql"] = extraction.details
            game_id = extract_game_id(page.content())
            run_data["game_id"] = game_id

            if not links:
                dom_links = extract_mod_links(page)
                links = dom_links
                run_data["extraction"]["dom_fallback"] = {
                    "links_found": len(dom_links),
                }

            if args.max_mods and args.max_mods > 0:
                links = links[: args.max_mods]

            run_data["queue_count"] = len(links)
            run_data["queue_first_5"] = links[:5]
            print(f"[+] Queue count: {len(links)}")
            if args.verify_downloads:
                print(f"[i] Download verification enabled. Directory: {args.downloads_dir}")
            if links:
                print("[i] First links:")
                for link in links[:5]:
                    print(f"    - {link}")
            else:
                print("[!] Queue extraction returned 0. Writing diagnostics artifacts.")
                artifacts = write_zero_queue_artifacts(page, log_dir, run_id)
                run_data["extraction"]["zero_queue_artifacts"] = artifacts

            for idx, mod_url in enumerate(links, start=1):
                print(f"\n[{idx}/{len(links)}] {mod_url}")
                item = process_mod(
                    page,
                    mod_url,
                    args.click_timeout_sec,
                    args.dry_run,
                    args.verify_downloads,
                    args.downloads_dir,
                    args.download_timeout_sec,
                    cookie_header,
                    game_id,
                )
                item.index = idx
                run_data["results"].append(asdict(item))
                print(f"[{item.status}] {item.reason}")
                write_run_logs(run_id, run_data, json_log, txt_log)
                if idx < len(links):
                    page.wait_for_timeout(int(args.delay_sec * 1000))

            try:
                browser.close()
            except Exception:
                pass
    except KeyboardInterrupt:
        interrupted = True
        run_data["interrupted"] = True
        run_data["fatal_error"] = "Interrupted by user (Ctrl+C)"
        print("\n[!] Interrupted by user. Writing partial logs.")
    except Exception as e:
        run_data["fatal_error"] = str(e)
        print(f"[!] Fatal error: {e}")

    write_run_logs(run_id, run_data, json_log, txt_log)
    summary_lines = build_summary_lines(run_id, run_data, json_log)

    print("\n" + "=" * 64)
    for line in summary_lines:
        print(line)
    print(f"txt_log: {txt_log}")

    if interrupted:
        return 130
    if run_data.get("fatal_error"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
