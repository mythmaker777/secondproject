"""
instagram_parser.py
Parses Instagram data export files — no credentials required.

Instagram now exports followers/following as HTML files inside a ZIP.

Known ZIP structures:
  connections/followers_and_following/followers_1.html
  connections/followers_and_following/following.html

Usernames are extracted from anchor tags pointing to instagram.com profiles:
  <a href="https://www.instagram.com/someusername">someusername</a>
"""

import io
import re
import zipfile
import logging
from pathlib import Path
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# Regex to extract username from instagram.com profile URLs
_IG_URL_RE = re.compile(
    r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?',
    re.IGNORECASE,
)


# ── HTML parser ───────────────────────────────────────────

class _IGHtmlParser(HTMLParser):
    """Extracts Instagram usernames from href attributes in an export HTML file."""

    def __init__(self):
        super().__init__()
        self.usernames: set = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for attr, val in attrs:
            if attr == "href" and val:
                m = _IG_URL_RE.match(val.strip())
                if m:
                    username = m.group(1).lower()
                    # Skip Instagram's own nav/help links
                    if username not in ("p", "explore", "accounts", "legal", "about"):
                        self.usernames.add(username)


def _parse_html_bytes(html_bytes: bytes) -> set:
    """Parse raw HTML bytes and return a set of Instagram usernames."""
    try:
        html = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return set()
    parser = _IGHtmlParser()
    parser.feed(html)
    return parser.usernames


# ── Public API ────────────────────────────────────────────

def parse_zip(zip_bytes: bytes) -> dict:
    """Extract and parse followers + following from an Instagram data ZIP."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return {"success": False, "error": "The file you sent isn't a valid ZIP archive."}

    names = zf.namelist()
    logger.info("ZIP contents: %s", names)

    followers_set = _find_and_parse(zf, names, "follower", exclude="following")
    following_set = _find_and_parse(zf, names, "following", exclude=None)

    if followers_set is None and following_set is None:
        html_files = [n for n in names if n.endswith(".html")]
        all_files  = [n for n in names if not n.endswith("/")]
        file_list  = "\n".join(f"• {n}" for n in (html_files or all_files)[:20]) or "No files found."
        return {
            "success": False,
            "error": (
                "Couldn't find followers/following files inside the ZIP.\n\n"
                "Make sure you included *Followers and following* when creating your export.\n\n"
                f"Files found in your ZIP:\n{file_list}"
            ),
        }

    return _compute_result(followers_set or set(), following_set or set())


def parse_html_file(file_bytes: bytes, filename: str) -> dict:
    """Parse a single HTML file uploaded directly."""
    name_lower = filename.lower()
    # "followers" must be checked before "following"
    if "follower" in name_lower and "following" not in name_lower:
        file_type = "followers"
    elif "following" in name_lower:
        file_type = "following"
    else:
        return {
            "success": False,
            "error": (
                f"Couldn't tell if `{filename}` is a followers or following file.\n"
                "Please make sure the filename contains 'followers' or 'following'."
            ),
        }

    usernames = _parse_html_bytes(file_bytes)
    return {"success": True, "type": file_type, "usernames": usernames}


def merge_and_compute(followers_result: dict, following_result: dict) -> dict:
    """Merge two parse_html_file results and compute non-followers."""
    if not followers_result["success"]:
        return followers_result
    if not following_result["success"]:
        return following_result
    return _compute_result(
        followers_result["usernames"],
        following_result["usernames"],
    )


# ── Internal helpers ──────────────────────────────────────

def _find_and_parse(zf: zipfile.ZipFile, names: list, keyword: str, exclude: str) -> set | None:
    """
    Find an HTML file in the ZIP whose path contains `keyword`
    (but not `exclude`) and return the parsed username set.
    """
    for n in names:
        if not n.endswith(".html"):
            continue
        path_lower = n.lower()
        stem_lower = Path(n).stem.lower()
        if keyword not in path_lower and keyword not in stem_lower:
            continue
        if exclude and exclude in stem_lower:
            continue
        try:
            raw = zf.read(n)
            usernames = _parse_html_bytes(raw)
            logger.info("Parsed %s from %s — %d usernames", keyword, n, len(usernames))
            return usernames
        except Exception:
            logger.warning("Failed to read %s", n)

    return None


def _compute_result(followers_set: set, following_set: set) -> dict:
    if not followers_set and not following_set:
        return {
            "success": False,
            "error": (
                "The files were found but no usernames could be extracted.\n\n"
                "Make sure you're uploading a ZIP from Instagram's data export, "
                "not a screenshot or other file."
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
