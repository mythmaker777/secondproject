"""
instagram_parser.py
Parses Instagram HTML data exports.

Rather than walking the HTML tree, we scan the raw file text with
regex for every instagram.com/<username> URL. This is format-agnostic
and works regardless of how Instagram structures the HTML.
"""

import io
import re
import zipfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Finds every instagram.com/username occurrence in raw text
# Excludes known non-profile paths
_URL_RE = re.compile(
    r'instagram\.com/([A-Za-z0-9][A-Za-z0-9_.]{0,29})(?:[/?"\s<]|$)',
    re.IGNORECASE,
)

_NON_USER_PATHS = {
    "p", "explore", "accounts", "legal", "about", "help",
    "privacy", "safety", "press", "api", "blog", "jobs",
    "reels", "stories", "tv", "directory", "lite", "challenge",
    "oauth", "instagram", "static", "images", "js", "css",
    "_n", "s", "e", "t",
}


def _extract_from_bytes(data: bytes) -> set:
    """Scan raw bytes for all instagram.com/username URLs."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return set()
    usernames = set()
    for m in _URL_RE.finditer(text):
        u = m.group(1).lower()
        if u not in _NON_USER_PATHS:
            usernames.add(u)
    logger.debug("Extracted %d usernames", len(usernames))
    return usernames


# ── ZIP entry finder ──────────────────────────────────────

def _find_and_parse(zf: zipfile.ZipFile, names: list, keyword: str, exclude: str) -> set | None:
    for n in names:
        if not (n.endswith(".html") or n.endswith(".json")):
            continue
        stem = Path(n).stem.lower()
        path = n.lower()
        if keyword not in path and keyword not in stem:
            continue
        if exclude and exclude in stem:
            continue
        try:
            raw       = zf.read(n)
            usernames = _extract_from_bytes(raw)
            logger.info("'%s' file: %s  →  %d usernames", keyword, n, len(usernames))
            return usernames
        except Exception as e:
            logger.warning("Failed to read %s: %s", n, e)
    return None


# ── Public API ────────────────────────────────────────────

def parse_zip(zip_bytes: bytes) -> dict:
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
                "Make sure you included *Followers and following* when creating your export.\n\n"
                "Files found in your ZIP:\n" + sample
            ),
        }

    return _compute_result(followers_set or set(), following_set or set())


def parse_html_file(file_bytes: bytes, filename: str) -> dict:
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
    usernames = _extract_from_bytes(file_bytes)
    return {"success": True, "type": file_type, "usernames": usernames}


def merge_and_compute(followers_result: dict, following_result: dict) -> dict:
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
                "Make sure you are uploading a ZIP from Instagram's data export."
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
