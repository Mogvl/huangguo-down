"""Merge already-downloaded episodes into one movie per show.

Older versions of this tool kept every episode as a separate file. Turning the
merge feature on afterwards would only take effect for shows that get synced
again, which is slow (a sync re-walks the whole site) and pointless when all the
bytes are already on disk.

This module works purely from the local library: it walks every album folder,
collects the episode files that are already there, merges them, and deletes the
per-episode files the same way a normal sync would. No network access at all, so
it is safe to run against a library whose site has moved or gone away.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import logging
import re

from .core import build_movie_filename, write_movie_nfo
from .download import merge_videos, probe_media


LOG = logging.getLogger('HGXZ')
ALBUM_PATTERN = re.compile(r'^(?P<title>.+?) \[huangguo-(?P<id>\d+)\]$')
EPISODE_PATTERN = re.compile(r'\.S(\d+)E(\d+)\.(?:mp4|mkv|ts)$', re.I)


@dataclass
class LocalEpisode:
    number: int
    path: Path


def _collect_episodes(album_dir: Path) -> list[LocalEpisode]:
    """Episode files under an album folder, ordered by episode number.

    Handles both ``Season 01/`` and a flat folder, because older versions of
    the tool (and manual edits) left files in either layout.
    """
    found: dict[int, Path] = {}
    for path in album_dir.rglob('*'):
        if not path.is_file():
            continue
        match = EPISODE_PATTERN.search(path.name)
        if not match:
            continue
        number = int(match.group(2))
        # Prefer the largest file if duplicates exist in both layouts.
        current = found.get(number)
        if current is None or path.stat().st_size > current.stat().st_size:
            found[number] = path
    return [LocalEpisode(n, found[n]) for n in sorted(found)]


def _read_title(album_dir: Path) -> tuple[str, int]:
    name = album_dir.name
    match = ALBUM_PATTERN.match(name)
    if match:
        return match.group('title'), int(match.group('id'))
    return name, 0


def merge_library(root: Path, *, delete_episodes: bool = True,
                  dry_run: bool = False, force: bool = False) -> dict:
    """Merge every album folder under ``root`` that still has loose episodes.

    ``force`` re-merges shows that already have a movie file, which is useful
    after changing the naming or when a previous merge looked wrong.
    """
    result = {'albums': 0, 'merged': 0, 'skipped': 0, 'failed': 0, 'details': []}
    if not root.is_dir():
        raise FileNotFoundError(f'media root not found: {root}')

    album_dirs = sorted(p.parent for p in root.rglob('tvshow.nfo'))
    # A folder that only has episodes (no tvshow.nfo) still deserves merging.
    for path in root.rglob('*'):
        if path.is_dir() and ALBUM_PATTERN.match(path.name) and path not in album_dirs:
            album_dirs.append(path)

    for album_dir in album_dirs:
        episodes = _collect_episodes(album_dir)
        if not episodes:
            continue
        result['albums'] += 1
        title, album_id = _read_title(album_dir)
        target = album_dir.parent / build_movie_filename(title, len(episodes))

        if target.is_file() and not force:
            result['skipped'] += 1
            result['details'].append({'dir': str(album_dir), 'status': 'already-merged'})
            LOG.info('skip %s: %s already exists', album_dir.name, target.name)
            continue

        if dry_run:
            result['details'].append({
                'dir': str(album_dir), 'status': 'would-merge',
                'episodes': len(episodes), 'target': str(target)})
            LOG.info('[dry-run] would merge %d episode(s) of %s -> %s',
                     len(episodes), album_dir.name, target.name)
            continue

        LOG.info('merging %d episode(s) of %s -> %s',
                 len(episodes), album_dir.name, target.name)
        try:
            info = merge_videos([e.path for e in episodes], target)
        except Exception as exc:
            result['failed'] += 1
            result['details'].append({'dir': str(album_dir), 'status': 'failed',
                                      'error': str(exc)[:300]})
            LOG.error('merge failed for %s: %s', album_dir.name, exc)
            continue

        _write_sidecars(album_dir, target, title, album_id, len(episodes), info)
        result['merged'] += 1

        if delete_episodes:
            for episode in episodes:
                try:
                    episode.path.unlink()
                    episode.path.with_suffix('.nfo').unlink(missing_ok=True)
                except OSError as exc:
                    LOG.warning('cannot delete %s: %s', episode.path, exc)
            _prune(album_dir)

        result['details'].append({'dir': str(album_dir), 'status': 'merged',
                                  'episodes': len(episodes), 'target': str(target)})
        LOG.info('merged %s (%d 集, %.1f MB)', target.name, len(episodes),
                 (info.get('size') or 0) / 1024 / 1024)
    return result


def preview_library(root: Path) -> dict:
    """Cheap count of albums that still have loose episodes.

    Only stats the filesystem, so the WebUI can show "N 部待合并" on the button
    without kicking off a merge.
    """
    if not root.is_dir():
        return {'albums': 0, 'episodes': 0, 'pending': 0, 'merged': 0}
    album_dirs = sorted(p.parent for p in root.rglob('tvshow.nfo'))
    for path in root.rglob('*'):
        if path.is_dir() and ALBUM_PATTERN.match(path.name) and path not in album_dirs:
            album_dirs.append(path)

    total_episodes = 0
    pending = 0
    already = 0
    for album_dir in album_dirs:
        episodes = _collect_episodes(album_dir)
        if not episodes:
            continue
        title, _ = _read_title(album_dir)
        target = album_dir.parent / build_movie_filename(title, len(episodes))
        if target.is_file():
            already += 1
            continue
        pending += 1
        total_episodes += len(episodes)
    return {'albums': len(album_dirs), 'episodes': total_episodes,
            'pending': pending, 'merged': already}


def _write_sidecars(album_dir: Path, target: Path, title: str, album_id: int,
                    episode_count: int, info: dict) -> None:
    """Reuse whatever metadata the old episode layout already had."""
    plot = ''
    year = None
    genres: list[str] = []
    tvshow = album_dir / 'tvshow.nfo'
    if tvshow.is_file():
        try:
            text = tvshow.read_text(encoding='utf-8', errors='replace')
            import xml.etree.ElementTree as ET
            root = ET.fromstring(text)
            plot = (root.findtext('plot') or '').strip()
            year_text = (root.findtext('year') or '').strip()
            year = int(year_text) if year_text.isdigit() else None
            genres = [(g.text or '').strip() for g in root.findall('genre') if g.text]
        except Exception as exc:
            LOG.debug('cannot read %s: %s', tvshow, exc)

    class _Meta:
        pass

    meta = _Meta()
    meta.title = title
    meta.plot = plot
    meta.year = year
    # Empty strings would be written as <genre /> / <plot />, which Emby shows
    # as blank entries; leave them out entirely instead.
    meta.category = genres[0] if genres else None
    meta.tags = genres[1:]
    meta.id = album_id
    write_movie_nfo(target.with_suffix('.nfo'), meta, episode_count,
                    info.get('duration', 0))

    poster = album_dir / 'poster.jpg'
    movie_poster = target.with_name(f'{target.stem}-poster.jpg')
    if poster.is_file() and not movie_poster.is_file():
        try:
            poster.replace(movie_poster)
        except OSError as exc:
            LOG.warning('cannot move poster: %s', exc)


def _prune(album_dir: Path) -> None:
    try:
        season = album_dir / 'Season 01'
        if season.is_dir() and not any(season.iterdir()):
            season.rmdir()
        for name in ('tvshow.nfo', 'poster.jpg'):
            (album_dir / name).unlink(missing_ok=True)
        if album_dir.is_dir() and not any(album_dir.iterdir()):
            album_dir.rmdir()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description='把已经下载好的分集合并成单文件电影（不联网）')
    parser.add_argument('root', help='媒体根目录，例如 /media/黄果')
    parser.add_argument('--keep-episodes', action='store_true',
                        help='保留分集，只生成合并文件')
    parser.add_argument('--dry-run', action='store_true', help='只列出会做什么')
    parser.add_argument('--force', action='store_true', help='已有合并文件也重新合并')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        result = merge_library(Path(args.root),
                               delete_episodes=not args.keep_episodes,
                               dry_run=args.dry_run, force=args.force)
    except FileNotFoundError as exc:
        print(f'错误：{exc}')
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描专辑 {result['albums']} 个，"
              f"合并 {result['merged']} 个，跳过 {result['skipped']} 个，"
              f"失败 {result['failed']} 个")
    return 0 if result['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
