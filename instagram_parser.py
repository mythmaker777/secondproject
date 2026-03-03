"""
instagram_parser.py
Parses Instagram data export files — JSON and HTML both supported.

Instagram ZIP structure (JSON format):
  connections/followers_and_following/followers_1.json
  connections/followers_and_following/following.json

Instagram ZIP structure (HTML format):
  connections/followers_and_following/followers_1.html
  connections/followers_and_following/following.html

JSON schema:
  followers_1.json -> [ {"string_list_data": [{"value": "username", ...}]} ]
  following.json   -> { "relationships_following": [ same structure ] }

HTML: we regex-scan for instagram.com/<username> URLs.
"""

import io
import re
import json
import zipfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Regex to find instagram.com/username in raw text (HTML or JSON)
_URL_RE = re.compile(
    r'instagram\.com/([A-Za-z0-9][A-Za-z0-9_.]{0,29})(?:[/?"\s<\\]|$)',
    re.IGNORECASE,
)

_NON_USER_PATHS = {
    "p", "explore", "accounts", "legal", "about", "help",
    "privacy", "safety", "press", "api", "blog", "jobs",
    "reels", "stories", "tv", "directory", "lite", "challenge",
    "oauth", "instagram", "static", "images", "js", "css",
    "_n", "s", "e", "t",
}


# ── Format detectors ──────────────────────────────────────

def _is_json(data: bytes) -> bool:
    try:
        data.decode("utf-8").lstrip()[0] in ("{", "[")
        json.loads(data.decode("utf-8"))
        return True
    except Exception:
        return False


# ── Extractors ────────────────────────────────────────────

def _extract_from_json(data: bytes) -> set:
    """
    Parse Instagram JSON export format.
    Handles both list format (followers) and dict-wrapped format (following).
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except Exception:
        return set()

    usernames = set()

    # Unwrap dict wrapper: {"relationships_following": [...]}
    if isinstance(raw, dict):
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
        entries = item.get("string_list_data") or []
        # Some exports use string_map_data
        if not entries:
            smd = item.get("string_map_data", {})
            entries = list(smd.values()) if isinstance(smd, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            val = entry.get("value", "").strip()
            if not val:
                # Fall back to extracting from href URL
                href = entry.get("href", "").strip()
                if href:
                    val = href.rstrip("/").split("/")[-1].split("?")[0]
            if val:
                usernames.add(val.lower())

    return usernames


def _extract_from_html(data: bytes) -> set:
    """Regex-scan HTML for instagram.com/username URLs."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return set()
    usernames = set()
    for m in _URL_RE.finditer(text):
        u = m.group(1).lower()
        if u not in _NON_USER_PATHS:
            usernames.add(u)
    return usernames


def _extract(data: bytes, filename: str) -> set:
    """Auto-detect format and extract usernames."""
    ext = Path(filename).suffix.lower()
    if ext == ".json" or _is_json(data):
        usernames = _extract_from_json(data)
        logger.info("Parsed %s as JSON — %d usernames", filename, len(usernames))
        return usernames
    else:
        usernames = _extract_from_html(data)
        logger.info("Parsed %s as HTML — %d usernames", filename, len(usernames))
        return usernames


# ── ZIP entry finder ──────────────────────────────────────

def _find_and_parse(zf: zipfile.ZipFile, names: list, keyword: str, exclude: str):
    """Find a file in the ZIP whose path contains keyword (not exclude)."""
    for n in names:
        ext = Path(n).suffix.lower()
        if ext not in (".html", ".json"):
            continue
        stem = Path(n).stem.lower()
        path = n.lower()
        if keyword not in path and keyword not in stem:
            continue
        if exclude and exclude in stem:
            continue
        try:
            raw       = zf.read(n)
            usernames = _extract(raw, n)
            logger.info("'%s' file: %s -> %d usernames", keyword, n, len(usernames))
            return usernames
        except Exception as e:
            logger.warning("Failed to read %s: %s", n, e)
    return None


# ── Public API ────────────────────────────────────────────

def parse_zip(zip_bytes: bytes) -> dict:
    """Extract and parse followers + following from an Instagram data ZIP."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return {"success": False, "error": "The file you sent isn't a valid ZIP archive."}

    names = zf.namelist()
    logger.info("ZIP contents: %s", names)

    followers_set = _find_and_parse(zf, names, keyword="follower", exclude="following")
    following_set = _find_and_parse(zf, names, keyword="following", exclude=None)

    if followers_set is None and following_set is None:
        all_files = [n for n in names if not n.endswith("/")]
        sample    = "\n".join("• " + n for n in all_files[:20]) or "No files found."
        return {
            "success": False,
            "error": (
                "Couldn't find followers/following files inside the ZIP.\n\n"
                "Make sure you included *Followers and following* "
                "when creating your export.\n\n"
                "Files found in your ZIP:\n" + sample
            ),
        }

    return _compute_result(followers_set or set(), following_set or set())


def parse_upload(file_bytes: bytes, filename: str) -> dict:
    """Parse a single file (JSON or HTML) uploaded directly."""
    name_lower = filename.lower()
    if "follower" in name_lower and "following" not in name_lower:
        file_type = "followers"
    elif "following" in name_lower:
        file_type = "following"
    else:
        return {
            "success": False,
            "error": (
                "Couldn't tell if `" + filename + "` is a followers or following file.\n"
                "Please make sure the filename contains 'followers' or 'following'."
            ),
        }
    usernames = _extract(file_bytes, filename)
    return {"success": True, "type": file_type, "usernames": usernames}


def merge_and_compute(followers_result: dict, following_result: dict) -> dict:
    """Merge two parse_upload results and compute non-followers."""
    if not followers_result["success"]:
        return followers_result
    if not following_result["success"]:
        return following_result
    return _compute_result(
        followers_result["usernames"],
        following_result["usernames"],
    )


# ── Compute ───────────────────────────────────────────────

def _compute_result(followers_set: set, following_set: set) -> dict:
    if not followers_set and not following_set:
        return {
            "success": False,
            "error": (
                "The files were found but no usernames could be extracted.\n\n"
                "Make sure you selected JSON or HTML format when requesting "
                "your Instagram data export."
            ),
        }

    non_followers = sorted(following_set - followers_set)

    return {
        "success":         True,
        "non_followers":   non_followers,
        "following_count": len(following_set),
        "followers_count": len(followers_set),
        "count":           len(non_followers),
    }
