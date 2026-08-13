#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import os
import plistlib
import random
import re
import shutil
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi
from PIL import Image, UnidentifiedImageError

APP_NAME = "wallpaper"
APP_VERSION = "3.0.0"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (cross-platform museum wallpaper client)"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
CLEVELAND_API_BASE = "https://openaccess-api.clevelandart.org/api"
CHICAGO_API_BASE = "https://api.artic.edu/api/v1"
RIJKSMUSEUM_API_BASE = "https://data.rijksmuseum.nl"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
LABEL = "io.github.medianomad.wallpaper"
WINDOWS_TASK_NAME = "wallpaper"
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

SOURCE_LABELS = {
    "cleveland": "Cleveland Museum of Art",
    "chicago": "Art Institute of Chicago",
    "rijksmuseum": "Rijksmuseum",
    "met": "The Metropolitan Museum of Art",
    "commons": "Wikimedia Commons",
}
DEFAULT_SOURCES = ["cleveland", "chicago", "rijksmuseum", "met", "commons"]
SOURCE_ALIASES = {
    "aic": "chicago",
    "artic": "chicago",
    "cma": "cleveland",
    "rijks": "rijksmuseum",
    "wikimedia": "commons",
}

HOME = Path.home()
if IS_WINDOWS:
    CONFIG_DIR = (
        Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming")) / APP_NAME
    )
    local_app_data = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    STATE_DIR = local_app_data / APP_NAME / "state"
    CACHE_DIR = local_app_data / APP_NAME / "cache"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP_NAME
    STATE_DIR = (
        Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / APP_NAME
    )
    CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / APP_NAME
IMAGE_DIR = HOME / "Pictures" / "Wallpapers"
LAUNCH_AGENT = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
CONFIG_PATH = CONFIG_DIR / "config.json"
LAST_PATH = STATE_DIR / "last.json"
LEGACY_CONFIG_PATH = HOME / ".config" / "met-wallpaper" / "config.json"
LEGACY_LAST_PATH = HOME / ".local" / "state" / "met-wallpaper" / "last.json"

DEFAULT_CONFIG = {
    "query": "painting",
    "artist": None,
    "title": None,
    "sources": DEFAULT_SOURCES,
    "department_id": 11,
    "department_name": "European Paintings",
    "interval_seconds": 21600,
    "download_dir": str(IMAGE_DIR),
    "prefer_original_image": True,
    "image_width": "auto",
    "minimum_image_width": 3840,
    "max_object_attempts": 50,
    "cache_ttl_seconds": 86400,
}


class WallpaperError(Exception):
    pass


_DISPLAY_WIDTH = None


def ensure_dirs():
    paths = [CONFIG_DIR, STATE_DIR, CACHE_DIR, IMAGE_DIR]
    if IS_MACOS:
        paths.append(LAUNCH_AGENT.parent)
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def migrate_legacy_files():
    """Carry the old macOS configuration forward after the app rename."""
    if IS_WINDOWS:
        return
    ensure_dirs()
    if not CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.exists():
        shutil.copy2(LEGACY_CONFIG_PATH, CONFIG_PATH)
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
            legacy_download_dir = str(HOME / "Pictures" / "MetWallpapers")
            if config.get("download_dir") == legacy_download_dir:
                config["download_dir"] = str(IMAGE_DIR)
            save_config(config)
        except (OSError, json.JSONDecodeError):
            CONFIG_PATH.unlink(missing_ok=True)
    if not LAST_PATH.exists() and LEGACY_LAST_PATH.exists():
        shutil.copy2(LEGACY_LAST_PATH, LAST_PATH)


def load_config():
    migrate_legacy_files()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    config = DEFAULT_CONFIG.copy()
    config.update(loaded)
    return config


def save_config(config):
    ensure_dirs()
    tmp = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(CONFIG_PATH)


def run_checked(command, *, input_text=None, capture=True):
    kwargs = {
        "text": True,
        "input": input_text,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    result = subprocess.run(command, check=False, **kwargs)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise WallpaperError(f"{command[0]} failed: {stderr or result.returncode}")
    return result.stdout if capture else ""


def connected_display_width():
    global _DISPLAY_WIDTH
    if _DISPLAY_WIDTH is not None:
        return _DISPLAY_WIDTH
    if IS_WINDOWS:
        try:
            import ctypes

            _DISPLAY_WIDTH = int(ctypes.windll.user32.GetSystemMetrics(0))
        except (AttributeError, OSError, ValueError):
            _DISPLAY_WIDTH = 0
        return _DISPLAY_WIDTH
    if not IS_MACOS:
        _DISPLAY_WIDTH = 0
        return _DISPLAY_WIDTH

    try:
        result = subprocess.run(
            ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        payload = {}

    widths = []
    for gpu in payload.get("SPDisplaysDataType") or []:
        for display in gpu.get("spdisplays_ndrvs") or []:
            if display.get("spdisplays_online") == "spdisplays_no":
                continue
            for key in (
                "_spdisplays_pixels",
                "_spdisplays_resolution",
                "spdisplays_pixelresolution",
            ):
                match = re.search(r"(\d+)\s*[xX]\s*(\d+)", str(display.get(key) or ""))
                if match:
                    widths.append(int(match.group(1)))
    _DISPLAY_WIDTH = max(widths, default=0)
    return _DISPLAY_WIDTH


def requested_image_width(config):
    configured = config.get("image_width", "auto")
    if str(configured).strip().lower() != "auto":
        try:
            width = int(configured)
        except (TypeError, ValueError) as exc:
            raise WallpaperError(
                "Image width must be 'auto' or a pixel count such as 5120."
            ) from exc
        if width < 800:
            raise WallpaperError("Image width must be at least 800 pixels.")
        return width
    minimum = max(800, int(config.get("minimum_image_width", 3840)))
    return max(minimum, connected_display_width())


def image_dimensions(path):
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, UnidentifiedImageError):
        return None, None


def http_bytes(url, *, timeout=45, headers=None):
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=SSL_CONTEXT
            ) as response:
                return response.read()
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    reason = getattr(last_error, "reason", None) or last_error
    raise WallpaperError(f"Could not fetch {url}: {reason}") from last_error


def http_json_url(url, params=None, source_name="API"):
    query = urllib.parse.urlencode(params or {})
    if query:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"
    payload = http_bytes(url)
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise WallpaperError(f"Invalid JSON from {source_name}: {exc}") from exc


def met_json(endpoint, params=None):
    return http_json_url(f"{MET_API_BASE}/{endpoint}", params, "The Met API")


def download_file(url, destination, max_width=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw_destination = destination.with_name(
        f".{destination.name}.{os.getpid()}.download"
    )
    headers = {}
    if str(url).startswith("https://www.artic.edu/iiif/"):
        headers["Referer"] = "https://www.artic.edu/"
    try:
        raw_destination.write_bytes(http_bytes(url, timeout=120, headers=headers))
    except (OSError, WallpaperError):
        raw_destination.unlink(missing_ok=True)
        raise
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    raw_width, _ = image_dimensions(raw_destination)
    convert_tiff = suffix in {".tif", ".tiff"}
    resize = bool(max_width and raw_width and raw_width > int(max_width))
    if convert_tiff or resize:
        converted = destination.with_name(
            f".{destination.stem}.{os.getpid()}.converted{destination.suffix}"
        )
        try:
            with Image.open(raw_destination) as image:
                if resize:
                    target_width = int(max_width)
                    target_height = max(
                        1, round(image.height * target_width / image.width)
                    )
                    image = image.resize(
                        (target_width, target_height), Image.Resampling.LANCZOS
                    )
                if convert_tiff or image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(converted, format="JPEG", quality=92, optimize=True)
            converted.replace(destination)
        except (OSError, UnidentifiedImageError) as exc:
            converted.unlink(missing_ok=True)
            raise WallpaperError(f"Could not prepare artwork image: {exc}") from exc
        finally:
            raw_destination.unlink(missing_ok=True)
    else:
        raw_destination.replace(destination)


def cache_key(parts):
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def cached_json(name, ttl_seconds, fetcher):
    path = CACHE_DIR / f"{name}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl_seconds:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    data = fetcher()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    tmp.replace(path)
    return data


def departments(config=None):
    ttl = int((config or DEFAULT_CONFIG).get("cache_ttl_seconds", 86400))
    return cached_json("departments", ttl, lambda: met_json("departments"))[
        "departments"
    ]


def resolve_department(value, config=None):
    if value is None:
        return None, None
    needle = str(value).strip()
    if not needle or needle.lower() in {"none", "all", "any", "-"}:
        return None, None

    all_departments = departments(config)
    if needle.isdigit():
        department_id = int(needle)
        for item in all_departments:
            if item["departmentId"] == department_id:
                return department_id, item["displayName"]
        raise WallpaperError(f"Unknown department id: {department_id}")

    lowered = needle.lower()
    exact = [item for item in all_departments if item["displayName"].lower() == lowered]
    if exact:
        item = exact[0]
        return item["departmentId"], item["displayName"]

    matches = [
        item for item in all_departments if lowered in item["displayName"].lower()
    ]
    if len(matches) == 1:
        item = matches[0]
        return item["departmentId"], item["displayName"]
    if matches:
        names = ", ".join(
            f"{item['departmentId']}: {item['displayName']}" for item in matches
        )
        raise WallpaperError(f"Department is ambiguous: {names}")
    raise WallpaperError(f"Unknown department: {needle}")


def search_objects(config):
    query = search_text(config)
    # The Met API currently returns much broader, incorrect results when
    # hasImages precedes q in the query string, so keep q first.
    params = {"q": query, "hasImages": "true"}
    if config.get("department_id") is not None:
        params["departmentId"] = str(config["department_id"])
    key = cache_key(params)
    ttl = int(config.get("cache_ttl_seconds", 86400))
    data = cached_json(f"search-{key}", ttl, lambda: met_json("search", params))
    object_ids = data.get("objectIDs") or []
    if not object_ids:
        department = (
            config.get("department_name")
            or config.get("department_id")
            or "all departments"
        )
        raise WallpaperError(
            f"No Met objects found for query '{query}' in {department}."
        )
    return object_ids


def get_object(object_id, config):
    ttl = int(config.get("cache_ttl_seconds", 86400))
    return cached_json(
        f"object-{object_id}", ttl, lambda: met_json(f"objects/{object_id}")
    )


def clean_filename(text):
    text = re.sub(r"[^A-Za-z0-9._ -]+", "", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:90].strip(" ._-") or "met-artwork"


def image_extension(url):
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        return ".jpg" if suffix in {".tif", ".tiff"} else suffix
    return ".jpg"


def select_image_url(obj, config):
    if not obj.get("isPublicDomain"):
        return None
    primary = obj.get("primaryImage") or ""
    small = obj.get("primaryImageSmall") or ""
    if config.get("prefer_original_image", True):
        return primary or small or None
    return small or primary or None


def normalized_terms(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return set(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def clean_text(value):
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def search_text(config):
    parts = [config.get("artist"), config.get("title"), config.get("query")]
    unique = []
    seen = set()
    for part in parts:
        value = str(part or "").strip()
        if value and value.lower() not in seen:
            unique.append(value)
            seen.add(value.lower())
    if not unique:
        raise WallpaperError("Search is empty. Use --query, --artist, or --title.")
    return " ".join(unique)


def normalize_sources(values):
    if not values:
        return DEFAULT_SOURCES.copy()
    if isinstance(values, str):
        values = [values]
    normalized = []
    for raw in values:
        for item in str(raw).split(","):
            source = SOURCE_ALIASES.get(item.strip().lower(), item.strip().lower())
            if source == "all":
                return DEFAULT_SOURCES.copy()
            if source not in SOURCE_LABELS:
                choices = ", ".join(["all", *SOURCE_LABELS])
                raise WallpaperError(f"Unknown source '{item}'. Choose: {choices}")
            if source not in normalized:
                normalized.append(source)
    return normalized


def make_candidate(
    source,
    object_id,
    title,
    artist,
    date,
    object_url,
    image_url,
    *,
    department=None,
    search_text_value=None,
    image_width=None,
    image_height=None,
):
    return {
        "objectID": f"{source}-{object_id}",
        "sourceObjectID": str(object_id),
        "title": clean_text(title) or "Untitled",
        "artistDisplayName": clean_text(artist) or "Unknown artist",
        "objectDate": clean_text(date),
        "department": clean_text(department),
        "objectURL": object_url,
        "isPublicDomain": True,
        "primaryImage": image_url,
        "_source": source,
        "_sourceLabel": SOURCE_LABELS[source],
        "_searchText": clean_text(search_text_value),
        "_imageWidth": int(image_width) if image_width else None,
        "_imageHeight": int(image_height) if image_height else None,
    }


def fields_contain_query(fields, query_terms):
    object_terms = set()
    for field in fields:
        object_terms.update(normalized_terms(field))
    return bool(query_terms) and query_terms.issubset(object_terms)


def object_match_rank(obj, query):
    query_terms = normalized_terms(query)
    constituents = [
        constituent
        for constituent in (obj.get("constituents") or [])
        if isinstance(constituent, dict)
    ]
    artist_fields = [
        obj.get("artistDisplayName"),
        obj.get("artistAlphaSort"),
        *(constituent.get("name") for constituent in constituents),
    ]
    if fields_contain_query(artist_fields, query_terms):
        return 2

    fields = [
        obj.get("title"),
        obj.get("artistDisplayBio"),
        obj.get("artistNationality"),
        obj.get("culture"),
        obj.get("period"),
        obj.get("dynasty"),
        obj.get("department"),
        obj.get("objectName"),
        obj.get("classification"),
        obj.get("medium"),
        obj.get("portfolio"),
        obj.get("country"),
        obj.get("region"),
        obj.get("_searchText"),
    ]
    fields.extend(
        tag.get("term") for tag in (obj.get("tags") or []) if isinstance(tag, dict)
    )
    fields.extend(constituent.get("name") for constituent in constituents)
    return 1 if fields_contain_query(fields, query_terms) else 0


def object_matches_query(obj, query):
    return object_match_rank(obj, query) > 0


def candidate_match_rank(obj, config):
    rank = 0
    artist = str(config.get("artist") or "").strip()
    title = str(config.get("title") or "").strip()
    query = str(config.get("query") or "").strip()

    if artist:
        artist_terms = normalized_terms(artist)
        if not fields_contain_query([obj.get("artistDisplayName")], artist_terms):
            return 0
        rank += 8
    if title:
        title_terms = normalized_terms(title)
        if not fields_contain_query([obj.get("title")], title_terms):
            return 0
        rank += 4
    if query:
        query_rank = object_match_rank(obj, query)
        if not query_rank:
            return 0
        rank += query_rank
    return rank


def cached_api_json(source, kind, key_parts, config, fetcher):
    key = cache_key(key_parts)
    ttl = int(config.get("cache_ttl_seconds", 86400))
    return cached_json(f"{source}-{kind}-{key}", ttl, fetcher)


def search_cleveland_candidates(config):
    target_width = requested_image_width(config)
    params = [("has_image", "1"), ("limit", "100")]
    if config.get("artist"):
        params.append(("artists", str(config["artist"])))
    if config.get("title"):
        params.append(("title", str(config["title"])))
    if config.get("query"):
        params.append(("q", str(config["query"])))
    url = f"{CLEVELAND_API_BASE}/artworks/?cc0&{urllib.parse.urlencode(params)}"
    data = cached_api_json(
        "cleveland",
        "search",
        params,
        config,
        lambda: http_json_url(url, source_name="Cleveland API"),
    )
    candidates = []
    for item in data.get("data") or []:
        if str(item.get("share_license_status") or "").upper() != "CC0":
            continue
        images = item.get("images") or {}
        renditions = []
        for image in images.values():
            if not isinstance(image, dict) or not image.get("url"):
                continue
            try:
                width = int(image.get("width") or 0)
                height = int(image.get("height") or 0)
            except (TypeError, ValueError):
                width, height = 0, 0
            renditions.append((width, height, image.get("url")))
        if not renditions:
            continue
        large_enough = [image for image in renditions if image[0] >= target_width]
        image_width, image_height, image_url = min(
            large_enough or renditions,
            key=lambda image: image[0] if large_enough else -image[0],
        )
        creators = item.get("creators") or []
        artist = "; ".join(
            clean_text(creator.get("description") or creator.get("name"))
            for creator in creators
            if isinstance(creator, dict)
        )
        candidate = make_candidate(
            "cleveland",
            item.get("id") or item.get("accession_number"),
            item.get("title"),
            artist,
            item.get("creation_date") or item.get("date_text"),
            item.get("url")
            or f"https://www.clevelandart.org/art/{item.get('accession_number', '')}",
            image_url,
            department=item.get("department"),
            search_text_value=" ".join(
                clean_text(item.get(field))
                for field in (
                    "tombstone",
                    "description",
                    "type",
                    "technique",
                    "culture",
                )
            ),
            image_width=min(image_width, target_width) if image_width else None,
            image_height=image_height,
        )
        if candidate_match_rank(candidate, config):
            candidates.append(candidate)
    return candidates


def search_chicago_candidates(config):
    target_width = requested_image_width(config)
    fields = (
        "id,title,artist_title,artist_display,date_display,image_id,is_public_domain,"
        "medium_display,place_of_origin,classification_titles,category_titles,"
        "style_titles,subject_titles,material_titles"
    )
    params = [
        ("q", search_text(config)),
        ("query[term][is_public_domain]", "true"),
        ("limit", "100"),
        ("fields", fields),
    ]
    data = cached_api_json(
        "chicago",
        "search",
        params,
        config,
        lambda: http_json_url(
            f"{CHICAGO_API_BASE}/artworks/search",
            params,
            "Art Institute of Chicago API",
        ),
    )
    iiif_base = (data.get("config") or {}).get(
        "iiif_url"
    ) or "https://www.artic.edu/iiif/2"
    candidates = []
    for item in data.get("data") or []:
        image_id = item.get("image_id")
        if not item.get("is_public_domain") or not image_id:
            continue
        artist = item.get("artist_title") or item.get("artist_display")
        search_fields = []
        for field in (
            "medium_display",
            "place_of_origin",
            "classification_titles",
            "category_titles",
            "style_titles",
            "subject_titles",
            "material_titles",
        ):
            value = item.get(field)
            search_fields.extend(value if isinstance(value, list) else [value])
        candidate = make_candidate(
            "chicago",
            item.get("id"),
            item.get("title"),
            artist,
            item.get("date_display"),
            f"https://www.artic.edu/artworks/{item.get('id')}",
            f"{iiif_base}/{image_id}/full/{target_width},/0/default.jpg",
            department="Art Institute of Chicago",
            search_text_value=" ".join(clean_text(value) for value in search_fields),
            image_width=target_width,
        )
        if candidate_match_rank(candidate, config):
            candidates.append(candidate)
    return candidates


def rijks_data_url(identifier):
    return f"{RIJKSMUSEUM_API_BASE}/{str(identifier).rstrip('/').rsplit('/', 1)[-1]}"


def rijks_entity(identifier, config):
    entity_id = str(identifier).rstrip("/").rsplit("/", 1)[-1]
    return cached_api_json(
        "rijksmuseum",
        "entity",
        entity_id,
        config,
        lambda: http_json_url(
            rijks_data_url(identifier), source_name="Rijksmuseum API"
        ),
    )


def linked_art_content(items, *, english=True):
    values = [
        item for item in (items or []) if isinstance(item, dict) and item.get("content")
    ]
    if english:
        for item in values:
            languages = item.get("language") or []
            if any(
                str(language.get("id") or "").endswith("300388277")
                for language in languages
            ):
                return clean_text(item.get("content"))
    return clean_text(values[0].get("content")) if values else ""


def rijks_object_url(obj):
    for subject in obj.get("subject_of") or []:
        for carrier in subject.get("digitally_carried_by") or []:
            if carrier.get("format") != "text/html":
                continue
            access_points = carrier.get("access_point") or []
            if access_points:
                return access_points[0].get("id")
    return obj.get("id")


def rijks_candidate(identifier, config):
    obj = rijks_entity(identifier, config)
    visual_ids = [item.get("id") for item in (obj.get("shows") or []) if item.get("id")]
    if not visual_ids:
        return None
    visual = rijks_entity(visual_ids[0], config)
    rights = [
        str(kind.get("id") or "").lower()
        for right in (visual.get("subject_to") or [])
        for kind in (right.get("classified_as") or [])
    ]
    if not any("publicdomain" in right for right in rights):
        return None
    digital_ids = [
        item.get("id")
        for item in (visual.get("digitally_shown_by") or [])
        if item.get("id")
    ]
    if not digital_ids:
        return None
    digital = rijks_entity(digital_ids[0], config)
    image_points = digital.get("access_point") or []
    image_url = image_points[0].get("id") if image_points else None
    if not image_url:
        return None
    target_width = requested_image_width(config)
    image_service_url = image_url.split("/full/", 1)[0]
    image_info = cached_api_json(
        "rijksmuseum",
        "iiif-info",
        image_service_url,
        config,
        lambda: http_json_url(
            f"{image_service_url}/info.json", source_name="Rijksmuseum IIIF API"
        ),
    )
    native_width = int(image_info.get("width") or target_width)
    requested_width = min(target_width, native_width)
    image_url = f"{image_service_url}/full/{requested_width},/0/default.jpg"

    production = obj.get("produced_by") or {}
    artist = linked_art_content(production.get("referred_to_by"))
    if not artist:
        for part in production.get("part") or []:
            artist = linked_art_content(part.get("referred_to_by"))
            if artist:
                break
    timespan = production.get("timespan") or {}
    date = linked_art_content(timespan.get("identified_by"))
    title = linked_art_content(obj.get("identified_by"))
    object_id = str(obj.get("id") or identifier).rstrip("/").rsplit("/", 1)[-1]
    search_values = [
        item.get("content")
        for item in (obj.get("referred_to_by") or [])
        if isinstance(item, dict)
    ]
    return make_candidate(
        "rijksmuseum",
        object_id,
        title,
        artist,
        date,
        rijks_object_url(obj),
        image_url,
        department="Rijksmuseum",
        search_text_value=" ".join(clean_text(value) for value in search_values),
        image_width=requested_width,
    )


def search_rijksmuseum_candidates(config):
    params = [("imageAvailable", "true")]
    if config.get("artist"):
        params.append(("creator", str(config["artist"])))
    if config.get("title"):
        params.append(("title", str(config["title"])))
    if config.get("query"):
        params.append(("description", str(config["query"])))
    if len(params) == 1:
        params.append(("description", search_text(config)))
    data = cached_api_json(
        "rijksmuseum",
        "search",
        params,
        config,
        lambda: http_json_url(
            f"{RIJKSMUSEUM_API_BASE}/search/collection",
            params,
            "Rijksmuseum Search API",
        ),
    )
    identifiers = [
        item.get("id") for item in (data.get("orderedItems") or []) if item.get("id")
    ]
    attempts = min(len(identifiers), int(config.get("max_object_attempts", 50)), 25)
    candidates = []
    for identifier in identifiers[:attempts]:
        try:
            candidate = rijks_candidate(identifier, config)
        except WallpaperError as exc:
            if "404" in str(exc):
                continue
            raise
        if candidate and candidate_match_rank(candidate, config):
            candidates.append(candidate)
    return candidates


def commons_metadata_value(metadata, key):
    value = (metadata.get(key) or {}).get("value")
    return clean_text(value)


def clean_commons_title(value, fallback):
    title = clean_text(value)
    title = re.sub(r"\s+(?:title|label)\s+QS:.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^[A-Za-z]+:\s*", "", title)
    if not title or len(title) > 180:
        title = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", fallback)
    return title


def search_commons_candidates(config):
    target_width = requested_image_width(config)
    params = [
        ("action", "query"),
        ("format", "json"),
        ("formatversion", "2"),
        ("generator", "search"),
        ("gsrsearch", f"{search_text(config)} filetype:bitmap"),
        ("gsrnamespace", "6"),
        ("gsrlimit", "50"),
        ("prop", "imageinfo"),
        ("iiprop", "url|extmetadata|mime|size"),
        ("iiurlwidth", str(target_width)),
    ]
    data = cached_api_json(
        "commons",
        "search",
        params,
        config,
        lambda: http_json_url(COMMONS_API, params, "Wikimedia Commons API"),
    )
    candidates = []
    for page in (data.get("query") or {}).get("pages") or []:
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        if not str(info.get("mime") or "").startswith("image/"):
            continue
        metadata = info.get("extmetadata") or {}
        license_name = commons_metadata_value(metadata, "LicenseShortName").lower()
        if not ("public domain" in license_name or license_name in {"cc0", "pdm"}):
            continue
        image_url = info.get("thumburl") or info.get("url")
        if not image_url:
            continue
        page_title = re.sub(r"^File:", "", page.get("title") or "", flags=re.IGNORECASE)
        title = clean_commons_title(
            commons_metadata_value(metadata, "ObjectName"), page_title
        )
        artist = commons_metadata_value(metadata, "Artist") or commons_metadata_value(
            metadata, "Credit"
        )
        date = commons_metadata_value(
            metadata, "DateTimeOriginal"
        ) or commons_metadata_value(metadata, "DateTime")
        search_fields = [
            commons_metadata_value(metadata, key)
            for key in ("ImageDescription", "Categories", "Credit", "Attribution")
        ]
        candidate = make_candidate(
            "commons",
            page.get("pageid"),
            title,
            artist,
            date,
            info.get("descriptionurl"),
            image_url,
            department="Wikimedia Commons",
            search_text_value=" ".join(search_fields),
            image_width=min(target_width, int(info.get("width") or target_width)),
            image_height=info.get("height"),
        )
        if candidate_match_rank(candidate, config):
            candidates.append(candidate)
    return candidates


def pick_met_object(config):
    object_ids = search_objects(config)
    attempts = min(int(config.get("max_object_attempts", 50)), len(object_ids))
    matches = []
    missing_objects = 0
    highest_rank = 0
    for object_id in object_ids[:attempts]:
        try:
            obj = get_object(object_id, config)
        except WallpaperError as exc:
            if "404" in str(exc):
                missing_objects += 1
                continue
            raise
        rank = candidate_match_rank(obj, config)
        if not rank:
            continue
        highest_rank = max(highest_rank, rank)
        image_url = select_image_url(obj, config)
        if image_url:
            matches.append((rank, obj, image_url))

    if matches:
        best_matches = [item for item in matches if item[0] == highest_rank]
        if best_matches:
            _, obj, image_url = random.choice(best_matches)
            obj = dict(obj)
            source_object_id = obj.get("objectID")
            obj["sourceObjectID"] = str(source_object_id)
            obj["objectID"] = f"met-{source_object_id}"
            obj["_source"] = "met"
            obj["_sourceLabel"] = SOURCE_LABELS["met"]
            return obj, image_url

    missing_note = (
        f" ({missing_objects} stale API objects skipped)" if missing_objects else ""
    )
    raise WallpaperError(
        f"Checked {attempts} relevant API results{missing_note}, but The Met provided no "
        "matching public-domain image URL. Try a broader query or another artist."
    )


PROVIDER_SEARCHERS = {
    "cleveland": search_cleveland_candidates,
    "chicago": search_chicago_candidates,
    "rijksmuseum": search_rijksmuseum_candidates,
    "commons": search_commons_candidates,
}


def pick_object(config):
    sources = normalize_sources(config.get("sources"))
    source_order = random.sample(sources, len(sources)) if len(sources) > 1 else sources
    failures = []
    for source in source_order:
        try:
            if source == "met":
                return pick_met_object(config)
            candidates = PROVIDER_SEARCHERS[source](config)
            if not candidates:
                failures.append(
                    f"{SOURCE_LABELS[source]}: no matching public-domain image"
                )
                continue
            ranks = [
                (candidate_match_rank(candidate, config), candidate)
                for candidate in candidates
            ]
            best_rank = max(rank for rank, _ in ranks)
            best_candidates = [
                candidate for rank, candidate in ranks if rank == best_rank
            ]
            candidate = random.choice(best_candidates)
            return candidate, candidate.get("primaryImage")
        except WallpaperError as exc:
            failures.append(f"{SOURCE_LABELS[source]}: {exc}")

    details = "; ".join(failures)
    raise WallpaperError(f"No matching public-domain image found. {details}")


def download_artwork(obj, image_url, config):
    download_dir = Path(config.get("download_dir") or IMAGE_DIR).expanduser()
    title = clean_filename(obj.get("title") or f"object-{obj.get('objectID')}")
    object_id = obj.get("objectID", "unknown")
    destination = download_dir / f"{object_id}-{title}{image_extension(image_url)}"
    needs_download = not destination.exists() or destination.stat().st_size == 0
    expected_width = obj.get("_imageWidth")
    if not needs_download and expected_width:
        existing_width, _ = image_dimensions(destination)
        needs_download = not existing_width or existing_width < int(expected_width)
    if needs_download:
        download_file(image_url, destination, requested_image_width(config))
    return destination


def set_wallpaper(path):
    path = Path(path).resolve()
    if IS_WINDOWS:
        try:
            import ctypes

            # SPI_SETDESKWALLPAPER, plus persist and broadcast the change.
            changed = ctypes.windll.user32.SystemParametersInfoW(
                20, 0, str(path), 0x01 | 0x02
            )
        except (AttributeError, OSError) as exc:
            raise WallpaperError(f"Windows could not set the wallpaper: {exc}") from exc
        if not changed:
            error_code = ctypes.get_last_error()
            raise WallpaperError(
                f"Windows could not set the wallpaper (error {error_code})."
            )
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Setting the wallpaper is currently supported on macOS and Windows."
        )

    script = """
on run argv
  set imagePath to item 1 of argv
  try
    tell application "System Events"
      set picture of every desktop to imagePath
    end tell
  on error
    tell application "Finder"
      set desktop picture to POSIX file imagePath
    end tell
  end try
end run
""".strip()
    run_checked(["/usr/bin/osascript", "-", str(path)], input_text=script)


def artwork_summary(obj, image_path, config):
    width, height = image_dimensions(image_path)
    return {
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "image_path": str(image_path),
        "object_id": obj.get("objectID"),
        "title": obj.get("title"),
        "artist": obj.get("artistDisplayName"),
        "date": obj.get("objectDate"),
        "department": obj.get("department"),
        "object_url": obj.get("objectURL"),
        "source": obj.get("_source"),
        "source_label": obj.get("_sourceLabel"),
        "image_width": width,
        "image_height": height,
        "requested_image_width": requested_image_width(config),
        "query": config.get("query"),
        "artist_query": config.get("artist"),
        "title_query": config.get("title"),
        "department_id": config.get("department_id"),
    }


def write_last(obj, image_path, config):
    last = artwork_summary(obj, image_path, config)
    tmp = LAST_PATH.with_name(f"{LAST_PATH.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(last, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(LAST_PATH)
    return last


def apply_search_args(config, args):
    changed = False
    query = getattr(args, "query", None)
    artist = getattr(args, "artist", None)
    title = getattr(args, "title", None)
    if any(value is not None for value in (query, artist, title)):
        config["query"] = str(query).strip() if query else None
        config["artist"] = str(artist).strip() if artist else None
        config["title"] = str(title).strip() if title else None
        changed = True
    sources = getattr(args, "source", None)
    if sources:
        config["sources"] = normalize_sources(sources)
        changed = True
    image_width = getattr(args, "image_width", None)
    if image_width is not None:
        value = str(image_width).strip().lower()
        if value == "auto":
            config["image_width"] = "auto"
        else:
            try:
                parsed_width = int(value)
            except ValueError as exc:
                raise WallpaperError(
                    "--image-width must be 'auto' or pixels such as 5120."
                ) from exc
            if parsed_width < 800:
                raise WallpaperError("--image-width must be at least 800 pixels.")
            config["image_width"] = parsed_width
        changed = True
    return changed


def next_wallpaper(args):
    config = load_config()
    changed = apply_search_args(config, args)
    department = getattr(args, "department", None)
    if department:
        department_id, department_name = resolve_department(department, config)
        config["department_id"] = department_id
        config["department_name"] = department_name
        changed = True
    if getattr(args, "save", False) and changed:
        save_config(config)

    obj, image_url = pick_object(config)
    image_path = download_artwork(obj, image_url, config)
    if args.download_only:
        print(
            format_last(artwork_summary(obj, image_path, config), prefix="Downloaded")
        )
        return
    set_wallpaper(image_path)
    last = write_last(obj, image_path, config)
    print(format_last(last, prefix="Set"))


def format_last(last, prefix="Current"):
    title = last.get("title") or f"Object {last.get('object_id')}"
    artist = last.get("artist") or "Unknown artist"
    date = last.get("date") or "undated"
    path = last.get("image_path") or ""
    url = last.get("object_url") or ""
    source = last.get("source_label") or SOURCE_LABELS.get(last.get("source"), "")
    source_line = f"\nSource: {source}" if source else ""
    width, height = last.get("image_width"), last.get("image_height")
    resolution_line = f"\nResolution: {width} × {height}" if width and height else ""
    return f"{prefix}: {title} - {artist}, {date}{source_line}{resolution_line}\nObject: {url}\nFile: {path}"


def print_sources(args):
    for source in DEFAULT_SOURCES:
        print(f"{source:<12} {SOURCE_LABELS[source]}")


def print_departments(args):
    for item in departments(load_config()):
        print(f"{item['departmentId']:>2}  {item['displayName']}")


def prompt(text):
    try:
        return input(text)
    except EOFError as exc:
        raise WallpaperError("No interactive input available.") from exc


def choose_category(args):
    config = load_config()
    all_departments = departments(config)
    current_department = (
        config.get("department_name")
        or config.get("department_id")
        or "all departments"
    )
    current_query = config.get("query") or ""

    print(f"Current department: {current_department}")
    print(f"Current query: {current_query}")
    print()
    print(" 0  All departments")
    for item in all_departments:
        marker = ""
        if config.get("department_id") == item["departmentId"]:
            marker = " *"
        print(f"{item['departmentId']:>2}  {item['displayName']}{marker}")
    print()

    while True:
        value = prompt(
            "Choose department id/name, 0 for all, or blank to keep current: "
        ).strip()
        if not value:
            department_id = config.get("department_id")
            department_name = config.get("department_name")
            break
        if value == "0":
            department_id, department_name = None, None
            break
        try:
            department_id, department_name = resolve_department(value, config)
            break
        except WallpaperError as exc:
            print(f"{exc}")

    if args.query is not None:
        query = args.query.strip()
    elif args.keep_query:
        query = current_query
    else:
        value = prompt(f"Search query [{current_query}]: ").strip()
        query = value or current_query
    if not query:
        raise WallpaperError("Search query cannot be empty.")

    config["department_id"] = department_id
    config["department_name"] = department_name
    config["query"] = query
    config["artist"] = None
    config["title"] = None
    save_config(config)
    print()
    print_config(argparse.Namespace(json=False))

    if args.next:
        print()
        next_wallpaper(
            argparse.Namespace(
                query=None,
                department=None,
                save=False,
                download_only=args.download_only,
            )
        )


def category(args):
    config = load_config()
    department_text = " ".join(args.department).strip() if args.department else None
    if department_text:
        department_id, department_name = resolve_department(department_text, config)
        config["department_id"] = department_id
        config["department_name"] = department_name
    if args.query:
        config["query"] = args.query
        config["artist"] = None
        config["title"] = None
    save_config(config)
    print_config(argparse.Namespace(json=False))


def interval(args):
    config = load_config()
    seconds = parse_interval(args.interval)
    config["interval_seconds"] = seconds
    save_config(config)
    print(f"Interval set to {seconds} seconds.")


def parse_interval(value):
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)([smhd]?)", text)
    if not match:
        raise WallpaperError("Use an interval like 30m, 6h, 1d, or raw seconds.")
    number = int(match.group(1))
    unit = match.group(2)
    factor = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = number * factor
    if seconds < 60:
        raise WallpaperError("Scheduler interval must be at least 60 seconds.")
    return seconds


def print_config(args):
    config = load_config()
    if args.json:
        print(json.dumps(config, indent=2, sort_keys=True))
        return
    department = config.get("department_name") or config.get("department_id") or "all"
    print(f"Query: {config.get('query')}")
    print(f"Artist: {config.get('artist') or '-'}")
    print(f"Title: {config.get('title') or '-'}")
    print(f"Sources: {', '.join(normalize_sources(config.get('sources')))}")
    print(
        f"Image width: {config.get('image_width', 'auto')} (auto floor: {config.get('minimum_image_width', 3840)} px)"
    )
    print(f"Department: {department}")
    print(f"Interval: {config.get('interval_seconds')} seconds")
    print(f"Download dir: {config.get('download_dir')}")
    print(f"Config: {CONFIG_PATH}")
    if IS_WINDOWS:
        print(f"Scheduled task: {WINDOWS_TASK_NAME}")
    elif IS_MACOS:
        print(f"LaunchAgent: {LAUNCH_AGENT}")


def current(args):
    if not LAST_PATH.exists():
        raise WallpaperError("No wallpaper has been set by wallpaper yet.")
    with LAST_PATH.open("r", encoding="utf-8") as handle:
        print(format_last(json.load(handle)))


def plist_payload(config):
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            str(Path(__file__).resolve()),
            "daemon-run",
        ],
        "StartInterval": int(config["interval_seconds"]),
        "RunAtLoad": True,
        "StandardOutPath": str(STATE_DIR / "launchd.out.log"),
        "StandardErrorPath": str(STATE_DIR / "launchd.err.log"),
        "WorkingDirectory": str(HOME),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        },
    }


def launchctl_domain():
    return f"gui/{os.getuid()}"


def launchctl_target():
    return f"{launchctl_domain()}/{LABEL}"


def write_plist(config):
    ensure_dirs()
    with LAUNCH_AGENT.open("wb") as handle:
        plistlib.dump(plist_payload(config), handle)


def windows_schedule(seconds):
    if seconds % 86400 == 0:
        return "DAILY", seconds // 86400
    if seconds % 3600 == 0 and seconds // 3600 <= 23:
        return "HOURLY", seconds // 3600
    if seconds % 60 == 0 and seconds // 60 <= 1439:
        return "MINUTE", seconds // 60
    raise WallpaperError(
        "Windows scheduling requires whole minutes (up to 1439), whole hours "
        "(up to 23), or whole days."
    )


def install_windows_task(config, *, kickstart=True):
    schedule, modifier = windows_schedule(int(config["interval_seconds"]))
    action = subprocess.list2cmdline(
        [sys.executable, str(Path(__file__).resolve()), "daemon-run"]
    )
    run_checked(
        [
            "schtasks",
            "/Create",
            "/TN",
            WINDOWS_TASK_NAME,
            "/TR",
            action,
            "/SC",
            schedule,
            "/MO",
            str(modifier),
            "/F",
        ]
    )
    if kickstart:
        run_checked(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])


def install_daemon(args):
    config = load_config()
    if args.interval:
        config["interval_seconds"] = parse_interval(args.interval)
        save_config(config)
    if IS_WINDOWS:
        install_windows_task(config, kickstart=not args.no_kickstart)
        print(
            f"Installed scheduled task {WINDOWS_TASK_NAME} with interval "
            f"{config['interval_seconds']} seconds."
        )
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Automatic scheduling is currently supported on macOS and Windows."
        )
    write_plist(config)
    try:
        run_checked(
            ["/bin/launchctl", "bootout", launchctl_domain(), str(LAUNCH_AGENT)]
        )
    except WallpaperError:
        pass
    run_checked(["/bin/launchctl", "bootstrap", launchctl_domain(), str(LAUNCH_AGENT)])
    if not args.no_kickstart:
        run_checked(["/bin/launchctl", "kickstart", "-k", launchctl_target()])
    print(f"Installed {LABEL} with interval {config['interval_seconds']} seconds.")


def uninstall_daemon(args):
    if IS_WINDOWS:
        run_checked(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"])
        print(f"Uninstalled scheduled task {WINDOWS_TASK_NAME}.")
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Automatic scheduling is currently supported on macOS and Windows."
        )
    try:
        run_checked(
            ["/bin/launchctl", "bootout", launchctl_domain(), str(LAUNCH_AGENT)]
        )
    except WallpaperError as exc:
        print(
            f"LaunchAgent was not loaded or could not be unloaded: {exc}",
            file=sys.stderr,
        )
    if not args.keep_plist and LAUNCH_AGENT.exists():
        LAUNCH_AGENT.unlink()
    print(f"Uninstalled {LABEL}.")


def start_daemon(args):
    if IS_WINDOWS:
        run_checked(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
        print(f"Started scheduled task {WINDOWS_TASK_NAME}.")
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Automatic scheduling is currently supported on macOS and Windows."
        )
    if not LAUNCH_AGENT.exists():
        write_plist(load_config())
    try:
        run_checked(
            ["/bin/launchctl", "bootstrap", launchctl_domain(), str(LAUNCH_AGENT)]
        )
    except WallpaperError:
        pass
    run_checked(["/bin/launchctl", "kickstart", "-k", launchctl_target()])
    print(f"Started {LABEL}.")


def stop_daemon(args):
    if IS_WINDOWS:
        run_checked(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
        print(f"Stopped scheduled task {WINDOWS_TASK_NAME}.")
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Automatic scheduling is currently supported on macOS and Windows."
        )
    run_checked(["/bin/launchctl", "bootout", launchctl_domain(), str(LAUNCH_AGENT)])
    print(f"Stopped {LABEL}.")


def restart_daemon(args):
    if IS_WINDOWS:
        try:
            run_checked(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
        except WallpaperError:
            pass
        run_checked(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
        print(f"Restarted scheduled task {WINDOWS_TASK_NAME}.")
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Automatic scheduling is currently supported on macOS and Windows."
        )
    try:
        run_checked(
            ["/bin/launchctl", "bootout", launchctl_domain(), str(LAUNCH_AGENT)]
        )
    except WallpaperError:
        pass
    write_plist(load_config())
    run_checked(["/bin/launchctl", "bootstrap", launchctl_domain(), str(LAUNCH_AGENT)])
    run_checked(["/bin/launchctl", "kickstart", "-k", launchctl_target()])
    print(f"Restarted {LABEL}.")


def status_daemon(args):
    if IS_WINDOWS:
        output = run_checked(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME, "/FO", "LIST", "/V"]
        )
        print(output.rstrip())
        return
    if not IS_MACOS:
        raise WallpaperError(
            "Automatic scheduling is currently supported on macOS and Windows."
        )
    if not LAUNCH_AGENT.exists():
        print(f"Not installed: {LAUNCH_AGENT}")
        return
    try:
        output = run_checked(["/bin/launchctl", "print", launchctl_target()])
        lines = []
        seen = set()
        seen_fields = set()
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith(
                (
                    "state =",
                    "last exit code =",
                    "path =",
                    "program =",
                    "last spawn time =",
                    "pid =",
                )
            ):
                field = stripped.split("=", 1)[0].strip()
                if field == "state" and field in seen_fields:
                    continue
                seen_fields.add(field)
                if stripped in seen:
                    continue
                seen.add(stripped)
                lines.append(stripped)
        print(f"Installed: {LAUNCH_AGENT}")
        print("\n".join(lines) if lines else "Loaded.")
    except WallpaperError as exc:
        print(f"Installed but not loaded: {exc}")


def daemon_run(args):
    args = argparse.Namespace(
        query=None, department=None, save=False, download_only=False
    )
    next_wallpaper(args)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="wallpaper",
        description=(
            "Search public-domain museum collections and set artwork as the macOS or "
            "Windows wallpaper."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("next", help="download and set a new random artwork wallpaper")
    p.add_argument(
        "-q", "--query", help="broad subject search, e.g. landscape, cats, flowers"
    )
    p.add_argument(
        "-a", "--artist", help="search explicitly by artist, e.g. Claude Monet"
    )
    p.add_argument(
        "-t", "--title", help="search explicitly by artwork title, e.g. Water Lilies"
    )
    p.add_argument(
        "-s",
        "--source",
        action="append",
        metavar="SOURCE",
        help="museum source (repeatable or comma-separated); use 'all' for every source",
    )
    p.add_argument("-d", "--department", help="temporary Met department id or name")
    p.add_argument(
        "--image-width",
        metavar="PIXELS|auto",
        help="requested wallpaper width; auto uses the largest display with a 3840 px floor",
    )
    p.add_argument(
        "--save",
        action="store_true",
        help="save search criteria, sources, and image width as the default",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="download without changing the wallpaper",
    )
    p.set_defaults(func=next_wallpaper)

    p = sub.add_parser(
        "category",
        aliases=["set-category"],
        help="set the default department/category and query",
    )
    p.add_argument("department", nargs="*", help='department id/name, or "all"')
    p.add_argument("-q", "--query", help="default search query")
    p.set_defaults(func=category)

    p = sub.add_parser(
        "departments", aliases=["categories"], help="list Met departments"
    )
    p.set_defaults(func=print_departments)

    p = sub.add_parser("sources", help="list available no-key museum sources")
    p.set_defaults(func=print_sources)

    p = sub.add_parser(
        "+list-categories",
        aliases=["list-categories", "choose-category"],
        help="interactively choose a Met department/category",
    )
    p.add_argument("-q", "--query", help="set the search query without prompting")
    p.add_argument(
        "--keep-query", action="store_true", help="do not prompt for the search query"
    )
    p.add_argument(
        "--next",
        action="store_true",
        help="set a new wallpaper after saving the category",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="with --next, download without changing wallpaper",
    )
    p.set_defaults(func=choose_category)

    p = sub.add_parser("interval", help="set daemon interval, e.g. 30m, 6h, 1d")
    p.add_argument("interval")
    p.set_defaults(func=interval)

    p = sub.add_parser("config", help="show current config")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=print_config)

    p = sub.add_parser("current", help="show the last artwork set by this tool")
    p.set_defaults(func=current)

    p = sub.add_parser("install-daemon", help="install automatic wallpaper scheduling")
    p.add_argument("--interval", help="interval such as 30m, 6h, 1d")
    p.add_argument(
        "--no-kickstart",
        action="store_true",
        help="install without immediately running once",
    )
    p.set_defaults(func=install_daemon)

    p = sub.add_parser("uninstall-daemon", help="remove automatic wallpaper scheduling")
    p.add_argument("--keep-plist", action="store_true")
    p.set_defaults(func=uninstall_daemon)

    p = sub.add_parser("start", help="start the scheduled wallpaper task")
    p.set_defaults(func=start_daemon)

    p = sub.add_parser("stop", help="stop the scheduled wallpaper task")
    p.set_defaults(func=stop_daemon)

    p = sub.add_parser("restart", help="restart the scheduled wallpaper task")
    p.set_defaults(func=restart_daemon)

    p = sub.add_parser("status", help="show scheduler status")
    p.set_defaults(func=status_daemon)

    p = sub.add_parser("daemon-run", help="internal scheduler entrypoint")
    p.set_defaults(func=daemon_run)

    return parser


def main(argv=None):
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except WallpaperError as exc:
        print(f"wallpaper: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
