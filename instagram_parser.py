"""
instagram_parser.py
Parses Instagram data export files — no credentials required.

Accepts:
  • The full ZIP export from Instagram
  • Individual JSON files: followers_1.json / following.json
    (or any file whose name contains "follower" or "following")

Instagram data export format (as of 2024):
  followers_1.json  → list of {"string_list_data": [{"value": "username", ...}]}
  following.json    → {"relationships_following": [ same structure ]}
"""

import io
import json
import zipfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Public API ────────────────────────────────────────────

def parse_zip(zip_bytes: bytes) -> dict:
    """Extract and parse followers + following from an Instagram data ZIP."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return {"success": False, "error": "The file you sent isn't a valid ZIP archive."}

    names = zf.namelist()

    followers_raw  = _find_and_read(zf, names, "follower")
    following_raw  = _find_and_read(zf, names, "following")

    if followers_raw is None and following_raw is None:
        return {
            "success": False,
            "error": (
                "Couldn't find followers/following files inside the ZIP.\n\n"
                "Make sure you're uploading the Instagram data export ZIP "
                "and that it includes the *Followers and Following* section."
            ),
        }

    return _compute_result(followers_raw, following_raw)


def parse_json_file(file_bytes: bytes, filename: str) -> dict:
    """
    Parse a single JSON file. Caller should call this twice
    (once for followers, once for following) and merge with merge_partial().
    """
    try:
        data = json.loads(file_bytes.decode("utf-8"))
    except Exception:
        return {"success": False, "error": f"Couldn't read `{filename}` as JSON."}

    name_lower = filename.lower()
    if "follower" in name_lower:
        return {"success": True, "type": "followers", "data": data}
    elif "following" in name_lower:
        return {"success": True, "type": "following", "data": data}
    else:
        return {
            "success": False,
            "error": (
                f"Couldn't tell if `{filename}` is a followers or following file.\n"
                "Please make sure the filename contains 'followers' or 'following'."
            ),
        }


def merge_and_compute(followers_result: dict, following_result: dict) -> dict:
    """Merge two parse_json_file results and compute non-followers."""
    if not followers_result["success"]:
        return followers_result
    if not following_result["success"]:
        return following_result

    return _compute_result(followers_result["data"], following_result["data"])


# ── Internal helpers ──────────────────────────────────────

def _find_and_read(zf: zipfile.ZipFile, names: list[str], keyword: str):
    """Find the first file in the ZIP whose path contains `keyword`."""
    for n in names:
        if keyword in Path(n).stem.lower() and n.endswith(".json"):
            try:
                return json.loads(zf.read(n).decode("utf-8"))
            except Exception:
                logger.warning("Failed to parse %s", n)
    return None


def _extract_usernames(raw) -> set[str]:
    """
    Handles both Instagram export structures:
      • List format (followers_1.json):
        [ {"string_list_data": [{"value": "username"}]}, ... ]
      • Dict format (following.json):
        { "relationships_following": [ same items ] }
    """
    usernames = set()

    # Unwrap dict wrapper if present
    if isinstance(raw, dict):
        # Find the first list value
        for v in raw.values():
            if isinstance(v, list):
                raw = v
                break
        else:
            return usernames

    if not isinstance(raw, list):
        return usernames

    for item in raw:
        if not isinstance(item, dict):
            continue
        sld = item.get("string_list_data") or item.get("string_map_data", {})
        if isinstance(sld, list):
            for entry in sld:
                val = entry.get("value") or entry.get("href", "")
                # href is sometimes an instagram.com/username URL
                if val.startswith("http"):
                    val = val.rstrip("/").split("/")[-1]
                if val:
                    usernames.add(val.lower())
        elif isinstance(sld, dict):
            for entry in sld.values():
                val = entry.get("value", "")
                if val:
                    usernames.add(val.lower())

    return usernames


def _compute_result(followers_raw, following_raw) -> dict:
    followers_set = _extract_usernames(followers_raw) if followers_raw is not None else set()
    following_set = _extract_usernames(following_raw) if following_raw is not None else set()

    if not followers_set and not following_set:
        return {
            "success": False,
            "error": (
                "The files were found but no usernames could be extracted.\n\n"
                "Make sure you downloaded the *JSON format* export (not HTML) "
                "when requesting your Instagram data."
            ),
        }

    non_followers = sorted(following_set - followers_set)

    return {
        "success":         True,
        "non_followers":   non_followers,          # hidden behind paywall
        "following_count": len(following_set),
        "followers_count": len(followers_set),
        "count":           len(non_followers),
    }
