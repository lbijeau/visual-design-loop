"""Standalone export: turn the loop's final HTML into a self-contained file.

export_extract.js harvests the Play CDN's compiled CSS in headless chromium;
this module fetches/embeds external stylesheet assets (SSRF-guarded) and
performs the HTML surgery. build_standalone() raises ExportError on failure —
the caller (loop.py) owns the CDN-dependent fallback and never lets an export
failure block finishing.
"""

import base64
import ipaddress
import json
import os
import re
import subprocess
import sys
from urllib.parse import urljoin, urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


class ExportError(Exception):
    """The standalone export could not be produced; caller should fall back."""


DATA_URI_MIME = {
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".css": "text/css",
}

_URL_REF = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
_IMPORT = re.compile(r"@import\s+(?:url\()?['\"]?([^'\")\s;]+)['\"]?\)?\s*;")
_CDN_SCRIPT = re.compile(r"<script[^>]*src=[\"']https://cdn\.tailwindcss\.com[^\"']*[\"'][^>]*>\s*</script>", re.IGNORECASE)


# Google Fonts serves woff2 CSS only to UAs it recognizes; python-requests'
# default UA would get the larger, less faithful TTF-fallback variant.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _default_fetch(url, timeout=10):
    return requests.get(url, timeout=timeout, headers={"User-Agent": BROWSER_UA})


def _inject_config(code: str, theme: dict) -> str:
    """Inject tailwind.config exactly like loop._inject_theme (which cannot be
    imported here: loop imports export, so the dependency must not point back)."""
    tailwind_config = theme.get("tailwind_config", {})
    config_script = f"""
        <script>
            tailwind.config = {json.dumps(tailwind_config)};
        </script>
        """
    if "</head>" in code:
        return code.replace("</head>", f"{config_script}</head>")
    if "<body>" in code:
        return code.replace("<body>", f"{config_script}<body>")
    return config_script + code


def _is_fetch_allowed(url: str) -> bool:
    """Only https to public hosts — hrefs come from LLM-generated HTML."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        return True  # hostname, not an IP literal


def _fetch_stylesheet(href: str, fetch=None):
    """Fetch a stylesheet's text. Returns None (with a warning) on any failure."""
    fetch = fetch or _default_fetch
    if not _is_fetch_allowed(href):
        print(f"⚠️ Export: refusing to fetch {href} (blocked by fetch guard)")
        return None
    try:
        resp = fetch(href, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ Export: stylesheet fetch failed ({resp.status_code}): {href}")
            return None
        return resp.text
    except Exception as e:
        print(f"⚠️ Export: stylesheet fetch failed ({e}): {href}")
        return None


def _embed_url_refs(css: str, base_url: str, fetch) -> str:
    """Replace url(...) refs with data URIs; leave failures/data URIs as-is."""

    def replace(match):
        ref = match.group(1).strip()
        if ref.startswith("data:"):
            return match.group(0)
        absolute = urljoin(base_url, ref)
        if not _is_fetch_allowed(absolute):
            print(f"⚠️ Export: refusing to fetch {absolute} (blocked by fetch guard)")
            return match.group(0)
        try:
            resp = fetch(absolute, timeout=10)
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}")
        except Exception as e:
            print(f"⚠️ Export: asset fetch failed ({e}): {absolute}")
            return match.group(0)
        ext = os.path.splitext(urlparse(absolute).path)[1].lower()
        mime = DATA_URI_MIME.get(ext)
        if mime is None:
            ctype = getattr(resp, "headers", {}).get("Content-Type", "")
            mime = ctype.split(";")[0].strip() or "application/octet-stream"
        b64 = base64.b64encode(resp.content).decode("ascii")
        return f"url(data:{mime};base64,{b64})"

    return _URL_REF.sub(replace, css)


def _inline_stylesheet_assets(css: str, base_url: str, fetch=None) -> str:
    """Embed url(...) refs as data URIs; inline @import one level deep."""
    fetch = fetch or _default_fetch

    def replace_import(match):
        href = urljoin(base_url, match.group(1).strip())
        text = _fetch_stylesheet(href, fetch=fetch)
        if text is None:
            return match.group(0)  # unresolvable @import stays
        return _embed_url_refs(text, href, fetch)

    css = _IMPORT.sub(replace_import, css)
    return _embed_url_refs(css, base_url, fetch)


def _remove_cdn_script(code: str) -> str:
    return _CDN_SCRIPT.sub("", code)


def _remove_links(code: str, attrs) -> str:
    """Remove <link ...> tags whose href attribute equals any of `attrs`
    (raw or &amp;-escaped, both quote styles)."""
    for attr in attrs:
        for variant in {attr, attr.replace("&", "&amp;")}:
            pattern = re.compile(r"<link[^>]*href=[\"']" + re.escape(variant) + r"[\"'][^>]*/?>", re.IGNORECASE)
            code = pattern.sub("", code)
    return code


def _insert_style(code: str, css: str) -> str:
    block = f"<style data-stylesentry-export>\n{css}\n</style>"
    if "</head>" in code:
        return code.replace("</head>", f"{block}</head>", 1)
    if "<body>" in code:
        return code.replace("<body>", f"{block}<body>", 1)
    return block + code


def build_standalone(code: str, theme: dict) -> str:
    """Produce fully self-contained HTML for `code` styled by `theme`.
    Raises ExportError on any failure; never writes final_result.html itself."""
    temp_path = config.RUN_STATE_DIR / "export_input.html"
    try:
        os.makedirs(config.RUN_STATE_DIR, exist_ok=True)
        with open(temp_path, "w") as f:
            f.write(_inject_config(code, theme))
        try:
            result = subprocess.run(
                ["node", str(config.PROJECT_DIR / "export_extract.js"), str(temp_path)],
                capture_output=True,
                text=True,
                cwd=str(config.PROJECT_DIR),
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise ExportError("harvest timed out (60s)")
        if result.returncode != 0:
            raise ExportError(f"harvest failed: {result.stderr.strip()[:200]}")
        try:
            harvest = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            raise ExportError(f"unparseable harvest output: {e}")
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    tailwind_css = harvest.get("tailwindCss", "")
    if not tailwind_css.strip():
        raise ExportError("no compiled Tailwind CSS in harvest")

    inlined_parts, inlined_attrs = [], []
    for entry in harvest.get("stylesheetHrefs", []):
        url, attr = entry.get("url", ""), entry.get("attr", "")
        css = _fetch_stylesheet(url)
        if css is None:
            continue  # link stays in the document; page degrades per-asset
        inlined_parts.append(_inline_stylesheet_assets(css, url))
        inlined_attrs.append(attr)

    out = _remove_cdn_script(code)
    out = _remove_links(out, inlined_attrs)
    combined = "\n".join(inlined_parts + [tailwind_css])
    return _insert_style(out, combined)
