"""
instagram_parser.py
Parses Instagram data export files — JSON and HTML both supported.

Counting logic:
  1. Read followers_1 file -> set of usernames who follow you
  2. Read following file   -> set of usernames you follow
  3. non_followers = following - followers

HTML exports use instagram.com/_u/username as the href format.
The /_u/ segment is a redirect prefix — we skip it to get the real username.
"""

import io
import re
import json
import zipfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches both instagram.com/username and instagram.com/_u/username href formats
_HREF_RE = re.compile(
    r'href="https?://(?:www\.)?instagram\.com/(?:_u/)?([A-Za-z0-9][A-Za-z0-9_.]{0,29})/?(?:\?[^"]*)?(?:#[^"]*)?"',
    re.IGNORECASE,
)

_NON_USER_PATHS = {
    "p", "explore", "accounts", "legal", "about", "help",
    "privacy", "safety", "press", "api", "blog", "jobs",
    "reels", "stories", "tv", "directory", "lite", "challenge",
    "oauth", "instagram", "static", "images", "js", "css", "_u",
}

_FOLLOWING_KEYS = {"relationships_following"}
_FOLLOWER_KEYS  = {"relationships_followers"}


# ── Format detector ───────────────────────────────────────

def _is_json(data: bytes) -> bool:
    try:
        text = data.decode("utf-8").lstrip()
        if text[0] not in ("{", "["):
            return False
        json.loads(text)
        return True
    except Exception:
        return False


# ── Extractors ────────────────────────────────────────────

def _extract_from_json(data: bytes, file_type: str = None) -> set:
    try:
        raw = json.loads(data.decode("utf-8"))
    except Exception:
        return set()

    if isinstance(raw, dict):
        chosen = None
        known = _FOLLOWING_KEYS if file_type == "following" else _FOLLOWER_KEYS
        for key in known:
            if key in raw and isinstance(raw[key], list):
                chosen = raw[key]
                break
        if chosen is None:
            for v in raw.values():
                if isinstance(v, list):
                    chosen = v
                    break
        if chosen is None:
            return set()
        raw = chosen

    if not isinstance(raw, list):
        return set()

    usernames = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries = item.get("string_list_data") or []
        if not entries:
            smd = item.get("string_map_data", {})
            entries = list(smd.values()) if isinstance(smd, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            val = entry.get("value", "").strip()
            if not val:
                href = entry.get("href", "").strip()
                if href:
                    # Handle /_u/ prefix in href values too
                    path = href.rstrip("/").split("/")
                    val = path[-1].split("?")[0]
                    if val == "_u" and len(path) > 1:
                        val = path[-2].split("?")[0] if len(path) >= 2 else ""
            if val:
                usernames.add(val.lower())

    return usernames


def _extract_from_html(data: bytes) -> set:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return set()
    usernames = set()
    for m in _HREF_RE.finditer(text):
        u = m.group(1).lower()
        if u not in _NON_USER_PATHS:
            usernames.add(u)
    return usernames


def _extract(data: bytes, filename: str, file_type: str = None) -> set:
    ext = Path(filename).suffix.lower()
    if ext == ".json" or _is_json(data):
        usernames = _extract_from_json(data, file_type=file_type)
        logger.info("Parsed %s as JSON — %d usernames", filename, len(usernames))
        return usernames
    else:
        usernames = _extract_from_html(data)
        logger.info("Parsed %s as HTML — %d usernames", filename, len(usernames))
        return usernames


# ── ZIP entry finder ──────────────────────────────────────

def _find_and_parse(zf: zipfile.ZipFile, names: list, keyword: str, exclude: str, file_type: str):
    for n in names:
        ext = Path(n).suffix.lower()
        if ext not in (".html", ".json"):
            continue
        # Match on filename stem only — the folder is named
        # "followers_and_following" which contains both keywords,
        # so path-level matching causes both searches to hit the same file.
        stem = Path(n).stem.lower()
        if keyword not in stem:
            continue
        if exclude and exclude in stem:
            continue
        try:
            raw       = zf.read(n)
            usernames = _extract(raw, n, file_type=file_type)
            logger.info("'%s' file: %s -> %d usernames", keyword, n, len(usernames))
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

    followers_set = _find_and_parse(zf, names, keyword="follower",  exclude="following", file_type="followers")
    following_set = _find_and_parse(zf, names, keyword="following", exclude=None,        file_type="following")

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
    usernames = _extract(file_bytes, filename, file_type=file_type)
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
