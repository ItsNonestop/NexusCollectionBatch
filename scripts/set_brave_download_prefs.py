#!/usr/bin/env python3
"""Safely set Brave download preferences for unattended automation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    local_app_data = os.environ.get("LOCALAPPDATA")
    user_profile = os.environ.get("USERPROFILE")
    if not local_app_data or not user_profile:
        print("[!] LOCALAPPDATA or USERPROFILE is not set.")
        return 2

    pref_path = Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default" / "Preferences"
    if not pref_path.exists():
        print(f"[!] Preferences file not found: {pref_path}")
        return 1

    try:
        raw = pref_path.read_text(encoding="utf-8")
        prefs = json.loads(raw)
    except Exception as exc:
        print(f"[!] Could not parse preferences JSON: {exc}")
        return 1

    downloads_dir = str(Path(user_profile) / "Downloads")
    prefs.setdefault("download", {})
    prefs["download"]["prompt_for_download"] = False
    prefs.setdefault("savefile", {})
    prefs["savefile"]["type"] = 0
    prefs["savefile"]["default_directory"] = downloads_dir

    temp_path = pref_path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(prefs, separators=(",", ":")), encoding="utf-8")
        temp_path.replace(pref_path)
    except Exception as exc:
        print(f"[!] Could not write updated preferences: {exc}")
        return 1

    print(f"[+] Updated Brave preferences: {pref_path}")
    print("[+] Set download.prompt_for_download=false")
    print("[+] Set savefile.type=0")
    print(f"[+] Set savefile.default_directory={downloads_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
