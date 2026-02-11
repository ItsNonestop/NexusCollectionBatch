#!/usr/bin/env python3
"""Archive installation helpers for auto-que V3."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _safe_stem(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip("._") or "archive"


def _extract_archive(archive_path: Path, target_dir: Path) -> tuple[bool, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.unpack_archive(str(archive_path), str(target_dir))
        return True, "unpack_archive"
    except Exception:
        pass

    seven_zip = shutil.which("7z") or shutil.which("7za")
    if seven_zip:
        cmd = [seven_zip, "x", "-y", f"-o{target_dir}", str(archive_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return True, "7z"
        return False, f"7z_failed:{result.stderr.strip()[:200]}"

    return False, "unsupported_archive_or_missing_7z"


def _merge_tree(source_dir: Path, target_dir: Path) -> int:
    copied = 0
    for src in source_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source_dir)
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def install_downloaded_archives(
    downloaded_paths: list[Path],
    install_dir: Path,
    log_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    install_dir.mkdir(parents=True, exist_ok=True)
    stage_root = log_dir / f"nexus-collection-batch-install-{run_id}"
    stage_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    installed = 0
    failed = 0

    seen: set[str] = set()
    unique_downloads: list[Path] = []
    for p in downloaded_paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        unique_downloads.append(p)

    for archive in unique_downloads:
        item: dict[str, Any] = {"archive": str(archive), "status": "pending", "reason": ""}
        if not archive.exists():
            item["status"] = "failed"
            item["reason"] = "archive_not_found"
            failed += 1
            results.append(item)
            continue

        extract_dir = stage_root / _safe_stem(archive.name)
        ok, method = _extract_archive(archive, extract_dir)
        if not ok:
            item["status"] = "failed"
            item["reason"] = method
            failed += 1
            results.append(item)
            continue

        copied_files = _merge_tree(extract_dir, install_dir)
        item["status"] = "installed"
        item["reason"] = f"method:{method}"
        item["copied_files"] = copied_files
        installed += 1
        results.append(item)

    payload = {
        "installed": installed,
        "failed": failed,
        "results": results,
        "stage_dir": str(stage_root),
        "install_dir": str(install_dir),
    }
    (log_dir / f"nexus-collection-batch-install-{run_id}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    return payload
