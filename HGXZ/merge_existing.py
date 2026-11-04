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
import glob
import logging
import re
import shutil

from .core import build_movie_filename, write_movie_nfo, sanitize_filename
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
                  dry_run: bool = False, force: bool = False,
                  state=None) -> dict:
    """Merge every album folder under ``root`` that still has loose episodes.

    ``force`` re-merges shows that already have a movie file, which is useful
    after changing the naming or when a previous merge looked wrong.
    """
    result = {'albums': 0, 'merged': 0, 'skipped': 0, 'failed': 0, 'details': []}
    if not root.is_dir():
        raise FileNotFoundError(f'media root not found: {root}')

    _migrate_legacy_flat_movies(root)
    if state is not None:
        _backfill_merge_table(root, state)

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
        from .core import movie_target
        _dir, target = movie_target(title, len(episodes), album_dir.parent)

        if target.is_file() and not force:
            result['skipped'] += 1
            result['details'].append({'dir': str(album_dir), 'status': 'already-merged'})
            LOG.info('skip %s: %s already exists', album_dir.name, target.name)
            # The movie exists, but stale episode files may still be on disk
            # (e.g. re-downloaded after an older bug). Point the episode rows at
            # the movie and, if asked, delete the leftovers so the next sync
            # does not treat them as missing and fetch the show again.
            if state is not None and album_id:
                try:
                    state.mark_merge(album_id, str(target), len(episodes),
                                     int(target.stat().st_size))
                    for ep in episodes:
                        try:
                            state.mark_episode(album_id, ep.number, str(target), 'done')
                        except Exception:
                            pass
                except Exception as exc:
                    LOG.debug('cannot repoint skipped merge %s: %s', album_id, exc)
            # Rescue the poster before pruning the old show folder: _prune
            # deletes album_dir/poster.jpg unconditionally, and this skip branch
            # never ran _write_sidecars, so without this the cover would be lost.
            if not dry_run:
                try:
                    poster = album_dir / 'poster.jpg'
                    movie_poster = target.parent / f'{target.stem}-poster.jpg'
                    if poster.is_file() and not movie_poster.is_file():
                        poster.replace(movie_poster)
                except OSError as exc:
                    LOG.warning('cannot move poster for %s: %s', album_dir.name, exc)
            if delete_episodes and not dry_run:
                for episode in episodes:
                    try:
                        episode.path.unlink()
                        episode.path.with_suffix('.nfo').unlink(missing_ok=True)
                    except OSError as exc:
                        LOG.warning('cannot delete %s: %s', episode.path, exc)
                _prune(album_dir)
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
        if state is not None and album_id:
            try:
                state.mark_merge(album_id, str(target), len(episodes),
                                 int((info or {}).get('size', 0)))
                # Repoint episode records to the merged movie so future syncs do
                # not treat the (now-deleted) episodes as missing and re-download.
                for ep in episodes:
                    try:
                        state.mark_episode(album_id, ep.number, str(target), 'done')
                    except Exception:
                        pass
            except Exception as exc:
                LOG.debug('cannot record merge for %s: %s', album_id, exc)
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


def _migrate_legacy_flat_movies(root: Path) -> int:
    """Move already-merged movies that were written flat next to every other
    show into their own folder, matching the current Emby-movie layout.

    Older versions (and early adopters of the merge feature) left
    ``剧名 (全N集).mp4`` directly under the library root. We relocate each such
    file together with its ``.nfo`` and ``-poster.jpg`` into a dedicated
    ``剧名 (全N集)/`` folder. Already-folded movies are skipped, and any file
    still paired with a live ``[huangguo-id]`` source folder is left alone.
    """
    pattern = re.compile(r'^(?P<name>.+?) \(全\d+集\)\.mp4$')
    moved = 0
    try:
        for child in root.rglob('*.mp4'):
            match = pattern.match(child.name)
            if not match:
                continue
            # Relocate only files that sit directly in a library folder (a
            # direct child of root, e.g. /media/AI成人短剧/剧名 (全N集).mp4),
            # and only if they are not already inside a same-named show folder.
            if child.parent.parent != root:
                continue
            if child.parent.name == match.group('name'):
                continue
            dest_dir = child.parent / child.stem
            if dest_dir == child.parent:
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            for src in (child, child.with_suffix('.nfo')):
                if src.is_file():
                    src.replace(dest_dir / src.name)
            poster = child.with_name(f'{child.stem}-poster.jpg')
            if poster.is_file():
                poster.replace(dest_dir / poster.name)
            moved += 1
    except OSError as exc:
        LOG.warning('movie migration interrupted: %s', exc)
    if moved:
        LOG.info('migrated %d legacy flat movie(s) into per-show folders', moved)
    return moved




def _backfill_merge_table(root: Path, state) -> int:
    """Record already-merged movies into the merges table.

    Older manual merges never called mark_merge, so the WebUI's poster endpoint
    (which reads merges.path) could not find their cover. We walk every album in
    the state DB, and if its merged movie file already exists on disk but the
    merges table has no path for it, we upsert the path so the cover resolves.
    """
    pathed = 0
    try:
        for row in state.all_albums():
            album_id = row['id']
            if state.merge_row(album_id):
                continue
            directory = row.get('directory') or ''
            if not directory:
                continue
            lib_root = Path(directory).parent
            if not lib_root.is_dir():
                continue
            title = row['title']
            count = int(row.get('episode_count') or 0)
            movie = None
            # The merged file name uses the actual episode count; try the
            # declared count and a few neighbours in case it differs slightly.
            for n in (count, count - 1, count + 1):
                if n <= 0:
                    continue
                cand = lib_root / f'{sanitize_filename(title)} (全{n}集)' /                     f'{sanitize_filename(title)} (全{n}集).mp4'
                if cand.is_file():
                    movie = cand
                    break
            if movie is None:
                # Fallback: any "全N集" folder whose name starts with the title.
                for folder in lib_root.glob(f'{sanitize_filename(title)} (全*集)'):
                    cand = folder / folder.name + '.mp4'
                    if cand.is_file():
                        movie = cand
                        break
            if movie is None:
                continue
            state.mark_merge(album_id, str(movie), 0, int(movie.stat().st_size))
            pathed += 1
    except Exception as exc:
        LOG.debug('merge table backfill interrupted: %s', exc)
    if pathed:
        LOG.info('backfilled %d merged movie(s) into merges table', pathed)
    return pathed

def cleanup_merged(root: Path, state, *, dry_run: bool = False) -> dict:
    """Tidy up leftovers around already-merged shows.

    A merged show should only exist as ``剧名 (全N集)/``. Older versions of the
    sync recreated the TV layout (``剧名 [huangguo-id]/Season 01/`` plus
    ``tvshow.nfo``) on every run even after the episodes were merged away, so
    libraries ended up with an empty show folder next to the movie. This removes
    those empty leftovers (after rescuing any cover they still hold) and reports
    duplicate movie folders for the same show.

    Nothing is deleted when the show folder still contains videos: those are
    real downloads, not leftovers.
    """
    from .core import sanitize_filename
    result = {'removed': 0, 'rescued_posters': 0, 'duplicates': [],
              'skipped': 0}
    try:
        rows = state.merge_rows_with_titles()
    except Exception as exc:
        LOG.warning('cannot read merges: %s', exc)
        return result

    for row in rows:
        movie = Path(row['path'])
        if not movie.is_file():
            continue
        lib = movie.parent.parent
        title = (row.get('title') or '').strip()
        if not title or not lib.is_dir():
            continue
        name = sanitize_filename(title)
        movie_poster = movie.parent / f'{movie.stem}-poster.jpg'
        # Report sibling movie folders for the same title (e.g. 全5集 and 全6集
        # left behind by an old incremental merge).
        try:
            siblings = [d for d in lib.glob(glob.escape(f'{name}') + ' (全*集)')
                        if d.is_dir() and d != movie.parent]
        except OSError:
            siblings = []
        if siblings:
            result['duplicates'].append(
                {'title': title, 'keep': str(movie.parent),
                 'others': [str(d) for d in siblings]})
        # NOTE: glob treats [huangguo-*] as a character class, so match the
        # show folder names with the ALBUM_PATTERN regex instead.
        try:
            candidates = [d for d in lib.iterdir()
                          if d.is_dir() and d.name.startswith(f'{name} [huangguo-')
                          and ALBUM_PATTERN.match(d.name)]
        except OSError:
            candidates = []
        for show_dir in candidates:
            if not show_dir.is_dir():
                continue
            try:
                has_video = any(show_dir.rglob('*.mp4'))
            except OSError:
                has_video = True
            if has_video:
                result['skipped'] += 1
                continue
            # Rescue a cover that only lives in the show folder.
            poster = show_dir / 'poster.jpg'
            if poster.is_file() and not movie_poster.is_file():
                if dry_run:
                    result['rescued_posters'] += 1
                else:
                    try:
                        poster.replace(movie_poster)
                        result['rescued_posters'] += 1
                    except OSError as exc:
                        LOG.warning('cannot move poster for %s: %s', title, exc)
            if dry_run:
                result['removed'] += 1
                LOG.info('[dry-run] would remove leftover %s', show_dir)
                continue
            try:
                shutil.rmtree(show_dir)
                result['removed'] += 1
                LOG.info('removed leftover show folder %s', show_dir)
            except OSError as exc:
                LOG.warning('cannot remove %s: %s', show_dir, exc)
    if result['duplicates']:
        for item in result['duplicates']:
            LOG.warning('duplicate movie folders for %s: keep %s, also %s',
                        item['title'], item['keep'], item['others'])
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
        from .core import movie_target
        _dir, target = movie_target(title, len(episodes), album_dir.parent)
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
    movie_poster = target.parent / f'{target.stem}-poster.jpg'
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
