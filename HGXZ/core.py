from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent
import json
import re

ALBUM_DIR_RE = re.compile(r'^(.*?)\s*\[huangguo-(\d+)\]$')


@dataclass
class Album:
    id: int
    category: str
    title: str
    plot: str
    year: int
    tags: list[str]
    episode_count: int
    ended: bool
    cover_url: str


@dataclass
class Episode:
    album_id: int
    number: int
    title: str
    plot: str


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip().rstrip('.')
    # Keep UTF-8 encoded length under the 255-byte limit of common filesystems.
    encoded = value.encode('utf-8')
    if len(encoded) > 200:
        value = encoded[:200].decode('utf-8', errors='ignore').rstrip().rstrip('.')
    return value or 'Untitled'


def build_episode_filename(album_title: str, episode_number: int, suffix: str = '.mp4') -> str:
    return f"{sanitize_filename(album_title)}.S01E{episode_number:02d}{suffix}"


def parse_category_panel(source: str) -> tuple[list[int], int | None]:
    """Extract album IDs and the declared total from the 'latest' channel panel.

    The attribute order inside the panel tag is not guaranteed, so the total is
    looked up within the matched tag rather than relying on a fixed order.
    Returns ``total=None`` when the page does not declare one.
    """
    panel_start = re.search(r'<[^>]+data-channel-panel=["\']latest["\'][^>]*>', source, re.I)
    if not panel_start:
        return [], None
    tag_html = panel_start.group(0)
    tag_match = re.match(r'<\s*([a-zA-Z][\w:-]*)', tag_html)
    if not tag_match:
        return [], None
    tag = tag_match.group(1)
    total_match = re.search(r'data-panel-total=["\'](\d+)["\']', tag_html)
    total = int(total_match.group(1)) if total_match else None

    start = panel_start.end()
    depth = 1
    token_re = re.compile(rf'</?{re.escape(tag)}\b[^>]*>', re.I)
    end = len(source)
    for match in token_re.finditer(source, start):
        depth += -1 if match.group(0).startswith('</') else 1
        if depth == 0:
            end = match.start()
            break
    panel = source[start:end]
    seen: set[int] = set()
    result: list[int] = []
    # The site switched its album detail path from /detail/<id>/ to /video/<id>/
    # (old URLs redirect), so both forms are accepted.
    for raw in re.findall(r'href=["\']/(?:detail|video)/(\d+)/', panel, re.I):
        album_id = int(raw)
        if album_id not in seen:
            seen.add(album_id)
            result.append(album_id)
    return result, total


def _meta(source: str, key: str, attr: str = 'name') -> str:
    pattern = rf'<meta[^>]+{attr}=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)["\']'
    match = re.search(pattern, source, re.I)
    if not match:
        pattern = rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{attr}=["\']{re.escape(key)}["\']'
        match = re.search(pattern, source, re.I)
    return unescape(match.group(1)).strip() if match else ''


def parse_detail_html(album_id: int, category: str, source: str) -> Album:
    # Capture the attribute up to its matching quote: the payload may be
    # HTML-escaped (&quot;) or contain raw quotes of the other kind.
    history_match = re.search(r'<body[^>]+data-history=(["\'])(.*?)\1', source, re.I | re.S)
    history = {}
    if history_match:
        try:
            history = json.loads(unescape(history_match.group(2)))
        except json.JSONDecodeError:
            history = {}
    numbers = [int(x) for x in re.findall(r'data-ep-id=["\'](\d+)', source, re.I)]
    title = str(history.get('title') or '').strip()
    if not title:
        raw_title = re.search(r'<title[^>]*>(.*?)</title>', source, re.I | re.S)
        title = unescape(raw_title.group(1)).split(' - ')[0].strip() if raw_title else ''
    plot = _meta(source, 'description') or str(history.get('desc') or '')
    cover = _meta(source, 'og:image', 'property')
    # A real detail page always carries at least one of these structures. When
    # none is present the response is an anti-bot challenge or an error page,
    # and parsing it would fabricate an album titled "huangguo-<id>".
    if not title and not numbers and not plot and not cover:
        raise ValueError(
            f'album {album_id}: detail page has no recognizable metadata '
            '(blocked by anti-bot or site layout changed)')
    tags = [x.strip() for x in str(history.get('tags') or '').split(',') if x.strip()]
    episode_label = str(history.get('episode') or '')
    if not numbers:
        numbers = [int(x) for x in re.findall(r'(?:更新至|全)\s*(\d+)\s*集', episode_label)]
    episode_count = max(numbers, default=1)
    ended = bool(re.search(r'全\s*\d+\s*集|已完结|完结', episode_label))
    year_match = re.search(r'/upload/(20\d{2})\d{4}/', cover)
    year = int(year_match.group(1)) if year_match else 0
    actual_category = str(history.get('cat') or category).strip()
    return Album(album_id, actual_category, title or f'huangguo-{album_id}', plot,
                 year, tags, episode_count, ended, cover)


def _write_xml(path: Path, root: Element) -> None:
    indent(root, space='  ')
    buffer = BytesIO()
    ElementTree(root).write(buffer, encoding='utf-8', xml_declaration=True)
    data = buffer.getvalue()
    # Skip rewriting when content is unchanged so Emby does not rescan.
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sub(root: Element, name: str, value: object) -> None:
    node = SubElement(root, name)
    node.text = str(value)


def write_tvshow_nfo(path: Path, album: Album) -> None:
    root = Element('tvshow')
    _sub(root, 'title', album.title)
    _sub(root, 'originaltitle', album.title)
    _sub(root, 'sorttitle', album.title)
    _sub(root, 'plot', album.plot)
    if album.year:
        _sub(root, 'year', album.year)
    _sub(root, 'status', 'Ended' if album.ended else 'Continuing')
    _sub(root, 'genre', album.category)
    for tag in album.tags:
        _sub(root, 'genre', tag)
    _sub(root, 'studio', '黄果短剧')
    uid = SubElement(root, 'uniqueid', {'type': 'huangguoai', 'default': 'true'})
    uid.text = str(album.id)
    _write_xml(path, root)


def write_episode_nfo(path: Path, album: Album, episode: Episode) -> None:
    root = Element('episodedetails')
    _sub(root, 'title', episode.title)
    _sub(root, 'showtitle', album.title)
    _sub(root, 'season', 1)
    _sub(root, 'episode', episode.number)
    _sub(root, 'plot', episode.plot or album.plot)
    _sub(root, 'studio', '黄果短剧')
    uid = SubElement(root, 'uniqueid', {'type': 'huangguoai', 'default': 'true'})
    uid.text = f'{album.id}-{episode.number}'
    _write_xml(path, root)
