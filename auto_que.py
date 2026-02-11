#!/usr/bin/env python3
"""User-facing V3 runner for auto-que."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright

import nexus_browser_first as baseline
from user_config import AppConfig, load_config, save_config
from v3_install import install_downloaded_archives

APP_NAME = "NexusCollectionBatch"


@dataclass
class StageSettings:
    collection_url: str
    downloads_dir: Path
    install_dir: Path
    cdp_url: str
    log_dir: Path
    dry_run: bool
    verify_downloads: bool
    max_mods: int
    click_timeout_sec: float
    delay_sec: float
    download_timeout_sec: int
    skip_install: bool


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def prompt_with_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def prompt_yes_no(label: str, default_yes: bool = True) -> bool:
    default = "Y/n" if default_yes else "y/N"
    raw = input(f"{label} ({default}): ").strip().lower()
    if not raw:
        return default_yes
    return raw in {"y", "yes"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} user runner")
    parser.add_argument("--collection-url", help="Nexus collection URL")
    parser.add_argument("--downloads-dir", type=Path, help="Folder where downloads are saved")
    parser.add_argument("--install-dir", type=Path, help="Folder where mods are installed")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="Browser CDP URL")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="Log output folder")
    parser.add_argument("--max-mods", type=int, default=0, help="Limit mods processed (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Run navigation only")
    parser.add_argument("--no-verify-downloads", action="store_true", help="Disable download file verification")
    parser.add_argument("--skip-install", action="store_true", help="Skip archive installation stage")
    parser.add_argument("--no-prompt", action="store_true", help="Non-interactive mode (requires required args)")
    parser.add_argument("--click-timeout-sec", type=float, default=12.0)
    parser.add_argument("--delay-sec", type=float, default=1.5)
    parser.add_argument("--download-timeout-sec", type=int, default=45)
    return parser.parse_args()


def resolve_settings(args: argparse.Namespace, config: AppConfig) -> StageSettings:
    default_collection = (
        config.collection_url
        or "https://www.nexusmods.com/games/stardewvalley/collections/w0mnwh/mods"
    )
    default_downloads = str(config.downloads_dir or (Path.home() / "Downloads"))
    default_install = str(config.install_dir or (Path.cwd() / "mods"))

    if args.no_prompt:
        if not args.collection_url and not config.collection_url:
            raise ValueError("--collection-url is required when --no-prompt is used.")
        if not args.install_dir and not config.install_dir:
            raise ValueError("--install-dir is required when --no-prompt is used.")
        collection_url = args.collection_url or config.collection_url
        downloads_dir = args.downloads_dir or config.downloads_dir or (Path.home() / "Downloads")
        install_dir = args.install_dir or config.install_dir
        if install_dir is None:
            raise ValueError("Install directory must be set.")
    else:
        print(f"{APP_NAME} setup")
        print("- Enter values or press Enter to use defaults.")
        collection_url = prompt_with_default("Collection URL", default_collection)
        downloads_dir = Path(prompt_with_default("Downloads folder", default_downloads))
        install_dir = Path(prompt_with_default("Install folder", default_install))

        print("\nRun summary")
        print(f"- Collection URL: {collection_url}")
        print(f"- Downloads: {downloads_dir}")
        print(f"- Install target: {install_dir}")
        print(f"- Browser CDP: {args.cdp_url}")
        if not prompt_yes_no("Start now?", default_yes=True):
            raise KeyboardInterrupt("User cancelled before run.")

    assert collection_url is not None
    if not baseline.COLLECTION_URL_RE.match(collection_url.strip()):
        raise ValueError("Collection URL format is invalid.")

    settings = StageSettings(
        collection_url=baseline.clean_collection_url(collection_url),
        downloads_dir=Path(downloads_dir),
        install_dir=Path(install_dir),
        cdp_url=args.cdp_url,
        log_dir=args.log_dir,
        dry_run=bool(args.dry_run),
        verify_downloads=not args.no_verify_downloads,
        max_mods=max(0, int(args.max_mods)),
        click_timeout_sec=max(1.0, float(args.click_timeout_sec)),
        delay_sec=max(0.0, float(args.delay_sec)),
        download_timeout_sec=max(5, int(args.download_timeout_sec)),
        skip_install=bool(args.skip_install),
    )
    return settings


def stage_header(index: int, total: int, text: str) -> None:
    print(f"\nStage {index}/{total}: {text}")


def ensure_cdp_reachable(cdp_url: str, timeout_sec: float = 4.0) -> tuple[bool, str]:
    probe = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(probe, timeout=timeout_sec) as response:
            if response.status == 200:
                return True, probe
            return False, f"CDP endpoint returned HTTP {response.status}: {probe}"
    except urllib.error.URLError:
        return (
            False,
            "CDP endpoint is not reachable.",
        )
    except Exception as exc:
        return False, f"CDP endpoint check failed: {exc}"


def candidate_browser_paths() -> list[Path]:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    paths = [
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    if local_app_data:
        base = Path(local_app_data)
        paths.extend(
            [
                base / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
                base / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
        )
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def try_launch_browser_for_cdp(cdp_url: str) -> tuple[bool, str]:
    parsed = baseline.urlparse(cdp_url)
    port = parsed.port or 9222
    for browser_path in candidate_browser_paths():
        if not browser_path.exists():
            continue
        cmd = [str(browser_path), f"--remote-debugging-port={port}", "--profile-directory=Default", "--new-window"]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue

        deadline_ms = 20000
        start = datetime.now()
        while int((datetime.now() - start).total_seconds() * 1000) < deadline_ms:
            ok, detail = ensure_cdp_reachable(cdp_url, timeout_sec=1.5)
            if ok:
                return True, f"Launched browser for CDP: {browser_path}"
            time.sleep(0.75)
        continue
    return False, "Could not find Brave/Chrome executable to auto-launch."


def cdp_help_text(cdp_url: str) -> str:
    parsed = baseline.urlparse(cdp_url)
    port = parsed.port or 9222
    return (
        "Start Brave or Chrome with remote debugging, then run again:\n"
        f'  "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe" --remote-debugging-port={port}'
    )


def find_download_path(reason: str) -> Optional[Path]:
    for prefix in ("download_saved:", "direct_download:", "direct_download_insecure_ssl:"):
        if reason.startswith(prefix):
            return Path(reason[len(prefix) :].strip())
    return None


def format_reason_for_console(reason: str) -> str:
    saved = find_download_path(reason)
    if saved is not None:
        return f"saved: {saved.name}"
    text = reason.strip()
    if len(text) > 90:
        return text[:87] + "..."
    return text


def parse_mod_id(url: str) -> str:
    domain, mod_id, file_id = baseline.parse_mod_target(url)
    if mod_id is None:
        return "unknown"
    if file_id:
        return f"{mod_id} (file {file_id})"
    return str(mod_id)


def run_download_stage(settings: StageSettings, run_data: dict[str, Any]) -> list[Path]:
    downloaded_files: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(settings.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        try:
            cookies = context.cookies(["https://www.nexusmods.com"])
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            cookie_header = ""

        print("- Loading collection page...")
        extraction = baseline.collect_links_via_network(page, settings.collection_url)
        links = extraction.links
        run_data["extraction"]["network_graphql"] = extraction.details
        game_id = baseline.extract_game_id(page.content())
        run_data["game_id"] = game_id

        if not links:
            dom_links = baseline.extract_mod_links(page)
            links = dom_links
            run_data["extraction"]["dom_fallback"] = {"links_found": len(dom_links)}

        if settings.max_mods > 0:
            links = links[: settings.max_mods]

        run_data["queue_count"] = len(links)
        run_data["queue_first_5"] = links[:5]

        print(f"- Queue count: {len(links)}")
        if settings.verify_downloads:
            print(f"- Download verification: {settings.downloads_dir}")

        if not links:
            print("[!] Queue extraction returned 0. Writing diagnostics artifacts.")
            artifacts = baseline.write_zero_queue_artifacts(page, settings.log_dir, run_data["run_id"])
            run_data["extraction"]["zero_queue_artifacts"] = artifacts
            return downloaded_files

        print("\nStage 3/4: Downloading mods")
        for idx, mod_url in enumerate(links, start=1):
            mod_id_text = parse_mod_id(mod_url)
            print(f"[{idx}/{len(links)}] mod {mod_id_text} ...", end="")
            with contextlib.redirect_stdout(io.StringIO()):
                item = baseline.process_mod(
                    page=page,
                    mod_url=mod_url,
                    click_timeout_sec=settings.click_timeout_sec,
                    dry_run=settings.dry_run,
                    verify_downloads=settings.verify_downloads,
                    downloads_dir=settings.downloads_dir,
                    download_timeout_sec=settings.download_timeout_sec,
                    cookie_header=cookie_header,
                    game_id=game_id,
                )
            item.index = idx
            item_data = asdict(item)
            run_data["results"].append(item_data)
            print(f" {item.status.upper()} - {format_reason_for_console(item.reason)}")
            saved = find_download_path(item.reason)
            if saved is not None:
                downloaded_files.append(saved)
            if idx < len(links):
                page.wait_for_timeout(int(settings.delay_sec * 1000))
        page.wait_for_timeout(400)
    return downloaded_files


def write_run_logs(log_dir: Path, run_id: str, run_data: dict[str, Any]) -> tuple[Path, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    json_log = log_dir / f"nexus-collection-batch-{run_id}.json"
    txt_log = log_dir / f"nexus-collection-batch-{run_id}.txt"

    ok_count = sum(1 for r in run_data["results"] if r["status"] == "ok")
    partial_count = sum(1 for r in run_data["results"] if r["status"] == "partial")
    fail_count = sum(1 for r in run_data["results"] if r["status"] == "fail")
    fallback_count = sum(1 for r in run_data["results"] if r["status"] == "fallback_needed")
    dry_count = sum(1 for r in run_data["results"] if r["status"] == "dry_run")
    installed = run_data.get("install_summary", {}).get("installed", 0)
    install_failed = run_data.get("install_summary", {}).get("failed", 0)

    summary = [
        f"run_id: {run_id}",
        f"collection_url: {run_data['collection_url']}",
        f"queue_count: {run_data['queue_count']}",
        f"ok: {ok_count}",
        f"partial: {partial_count}",
        f"fallback_needed: {fallback_count}",
        f"fail: {fail_count}",
        f"dry_run: {dry_count}",
        f"install_ok: {installed}",
        f"install_fail: {install_failed}",
        f"json_log: {json_log}",
    ]

    json_log.write_text(json.dumps(run_data, indent=2), encoding="utf-8")
    txt_log.write_text("\n".join(summary) + "\n", encoding="utf-8")
    return json_log, txt_log


def print_final_summary(run_data: dict[str, Any], json_log: Path, txt_log: Path) -> None:
    downloaded = sum(1 for r in run_data["results"] if find_download_path(r["reason"]) is not None)
    failed = sum(1 for r in run_data["results"] if r["status"] in {"fail", "partial", "fallback_needed"})
    install_summary = run_data.get("install_summary", {})
    print("\nFinal summary")
    print(f"- Queue: {run_data['queue_count']}")
    print(f"- Downloaded files: {downloaded}")
    print(f"- Items needing attention: {failed}")
    print(f"- Installed archives: {install_summary.get('installed', 0)}")
    print(f"- Install failures: {install_summary.get('failed', 0)}")
    print(f"- JSON log: {json_log}")
    print(f"- Text summary: {txt_log}")


def main() -> int:
    args = parse_args()
    config_path = Path("nexus_collection_batch_config.json")
    legacy_config_path = Path("auto_que_config.json")
    config = load_config(config_path)
    if (
        config.collection_url is None
        and config.downloads_dir is None
        and config.install_dir is None
        and legacy_config_path.exists()
    ):
        config = load_config(legacy_config_path)

    try:
        settings = resolve_settings(args, config)
    except KeyboardInterrupt:
        print("[i] Cancelled.")
        return 130
    except Exception as exc:
        print(f"[!] Input error: {exc}")
        return 2

    config.collection_url = settings.collection_url
    config.downloads_dir = settings.downloads_dir
    config.install_dir = settings.install_dir
    config.cdp_url = settings.cdp_url
    save_config(config_path, config)

    run_id = now_stamp()
    run_data: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "collection_url": settings.collection_url,
        "downloads_dir": str(settings.downloads_dir),
        "install_dir": str(settings.install_dir),
        "cdp_url": settings.cdp_url,
        "dry_run": settings.dry_run,
        "verify_downloads": settings.verify_downloads,
        "queue_count": 0,
        "queue_first_5": [],
        "results": [],
        "extraction": {},
        "install_summary": {},
    }

    try:
        stage_header(1, 4, "Browser/session check")
        ok, cdp_detail = ensure_cdp_reachable(settings.cdp_url)
        if ok:
            print(f"- Connected endpoint: {cdp_detail}")
        else:
            print(f"- {cdp_detail}")
            print("- CDP endpoint is down. Attempting to launch browser automatically...")
            launched, launch_detail = try_launch_browser_for_cdp(settings.cdp_url)
            if not launched:
                raise RuntimeError(f"{launch_detail}\n{cdp_help_text(settings.cdp_url)}")
            print(f"- {launch_detail}")

        stage_header(2, 4, "Collection scan")
        os.environ.setdefault("NODE_NO_WARNINGS", "1")
        downloaded_paths = run_download_stage(settings, run_data)

        stage_header(4, 4, "Installing mods")
        if settings.skip_install or settings.dry_run:
            print("- Install stage skipped.")
        else:
            install_summary = install_downloaded_archives(
                downloaded_paths=downloaded_paths,
                install_dir=settings.install_dir,
                log_dir=settings.log_dir,
                run_id=run_id,
            )
            run_data["install_summary"] = install_summary
            print(
                f"- Installed: {install_summary.get('installed', 0)}, "
                f"Failed: {install_summary.get('failed', 0)}"
            )
    except KeyboardInterrupt:
        run_data["fatal_error"] = "Interrupted by user (Ctrl+C)"
        print("[!] Interrupted by user.")
    except Exception as exc:
        run_data["fatal_error"] = str(exc)
        print(f"[!] Run failed: {exc}")

    json_log, txt_log = write_run_logs(settings.log_dir, run_id, run_data)
    print_final_summary(run_data, json_log, txt_log)
    if run_data.get("fatal_error"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
