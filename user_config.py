#!/usr/bin/env python3
"""Configuration helpers for auto-que V3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class AppConfig:
    collection_url: Optional[str] = None
    downloads_dir: Optional[Path] = None
    install_dir: Optional[Path] = None
    cdp_url: str = "http://127.0.0.1:9222"


def _path_or_none(value: Any) -> Optional[Path]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return Path(text)


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return AppConfig()
    return AppConfig(
        collection_url=raw.get("collection_url") if isinstance(raw.get("collection_url"), str) else None,
        downloads_dir=_path_or_none(raw.get("downloads_dir")),
        install_dir=_path_or_none(raw.get("install_dir")),
        cdp_url=raw.get("cdp_url") if isinstance(raw.get("cdp_url"), str) else "http://127.0.0.1:9222",
    )


def save_config(path: Path, config: AppConfig) -> None:
    payload = {
        "collection_url": config.collection_url or "",
        "downloads_dir": str(config.downloads_dir) if config.downloads_dir else "",
        "install_dir": str(config.install_dir) if config.install_dir else "",
        "cdp_url": config.cdp_url,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
