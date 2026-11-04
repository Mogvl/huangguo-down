from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import threading
import time
import re

from .core import (Episode, build_episode_filename, build_movie_filename,
                   parse_category_panel, parse_detail_html, sanitize_filename,
                   write_episode_nfo, write_movie_nfo, write_tvshow_nfo)
from .download import download_hls, merge_videos, probe_media, validate_media
from .cover import valid_cover, write_cover
from .site import HuangguoClient
from .state import StateDB
from . import __version__

LOG = logging.getLogger('HGXZ')

# ---------------------------------------------------------------------------
# Environment overrides
#
# NAS users (UGREEN / Unraid / Synology) configure containers through a GUI that
# only exposes environment variables, so every tunable in config.json also has
# an env equivalent. Env wins over the file, which keeps the JSON as a set of
# defaults rather than a second source of truth.
# ---------------------------------------------------------------------------


def _env(*names: str, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != '':
            return value.strip()
    return default


def _env_int(*names: str, default=None):
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning('ignoring non-numeric env %s=%r', names[0], raw)
        return default


def _env_float(*names: str, default=None):
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        LOG.warning('ignoring non-numeric env %s=%r', names[0], raw)
        return default


def _env_list(*names: str, default=None):
    raw = _env(*names)
    if raw is None:
        return default
    return [item.strip() for item in raw.replace('\n', ',').split(',') if item.strip()]


def apply_env_overrides(config: dict) -> dict:
    """Layer environment variables over the JSON config in place."""
    site = config.setdefault('site', {})
    download = config.setdefault('download', {})
    state = config.setdefault('state', {})
    log_conf = config.setdefault('log', {})

    site['base_url'] = _env('HGXZ_BASE_URL', default=site.get('base_url'))
    backups = _env_list('HGXZ_BACKUP_URLS')
    if backups is not None:
        site['backup_urls'] = backups
    interval = _env_float('HGXZ_REQUEST_INTERVAL')
    if interval is not None:
        site['request_interval'] = interval
    timeout = _env_int('HGXZ_TIMEOUT')
    if timeout is not None:
        site['timeout'] = timeout
    attempts = _env_int('HGXZ_ATTEMPTS')
    if attempts is not None:
        site['attempts'] = attempts

    # MEDIA_ROOT / DOWNLOAD_ROOT are the names NAS templates tend to use.
    root = _env('HGXZ_MEDIA_ROOT', 'MEDIA_ROOT', 'DOWNLOAD_ROOT')
    if root:
        download['root'] = root
    for key, names in (
        ('retries', ('HGXZ_RETRIES',)),
        ('minimum_duration', ('HGXZ_MINIMUM_DURATION',)),
        ('recheck_days', ('HGXZ_RECHECK_DAYS',)),
        ('failure_cooldown_hours', ('HGXZ_FAILURE_COOLDOWN_HOURS',)),
        ('episode_timeout', ('HGXZ_EPISODE_TIMEOUT',)),
    ):
        value = _env_float(*names)
        if value is not None:
            download[key] = value

    merge = config.setdefault('merge', {})
    for key, names in (
        ('enabled', ('HGXZ_MERGE', 'MERGE_EPISODES')),
        ('delete_episodes', ('HGXZ_MERGE_DELETE',)),
        ('only_when_complete', ('HGXZ_MERGE_ONLY_WHEN_COMPLETE',)),
    ):
        raw = _env(*names)
        if raw is not None:
            merge[key] = raw.lower() in ('1', 'true', 'yes', 'on')

    state['database'] = _env('HGXZ_STATE_DB', default=state.get('database'))
    log_conf['directory'] = _env('HGXZ_LOG_DIR', default=log_conf.get('directory'))
    return config


DEFAULT_CONFIG_TEMPLATE = {
    'site': {
        'base_url': 'https://huangguoai.com',
        'backup_urls': [],
        'request_interval': 2,
        'timeout': 30,
        'categories': [
            {'name': 'AI成人短剧', 'path': '/ai-duanju/', 'library': 'AI成人短剧'},
            {'name': 'AI成人漫剧', 'path': '/ai-manju/', 'library': 'AI成人漫剧'},
        ],
    },
    'download': {
        'root': '/media',
        'retries': 3,
        'minimum_duration': 3,
        'recheck_days': 7,
        'failure_cooldown_hours': 24,
        'episode_timeout': 1800,
    },
    'merge': {'enabled': False, 'delete_episodes': True, 'only_when_complete': False},
    'state': {'database': '/data/state.sqlite3'},
    'log': {'directory': '/logs'},
}


def ensure_config(path: Path) -> dict:
    """Load config.json, seeding it from the bundled template when absent.

    A first-run container should come up working instead of crash-looping on a
    missing file, so the template is written to the config volume and then
    overlaid with environment variables.
    """
    if path.is_file():
        try:
            return apply_env_overrides(load_config(path))
        except json.JSONDecodeError as exc:
            LOG.error('config %s is not valid JSON: %s', path, exc)
            raise

    template = json.loads(json.dumps(DEFAULT_CONFIG_TEMPLATE))
    bundled = Path('/app/config.example.json')
    if bundled.is_file():
        try:
            template = json.loads(bundled.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning('bundled template unreadable (%s), using built-in defaults', exc)
    config = apply_env_overrides(template)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
        LOG.warning('config %s not found; wrote a default from the template', path)
    except OSError as exc:
        LOG.warning('config %s not found and could not be created (%s); '
                    'running from environment only', path, exc)
    return config


def configure_logging() -> None:
    level_name = _env('HGXZ_LOG_LEVEL', default='INFO').upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level,
                        format='%(asctime)s %(levelname)s %(message)s')


def drop_privileges() -> None:
    """Drop from root to PUID/PGID when the container was started as root.

    LinuxServer-style images run the entrypoint as root and then re-exec as an
    unprivileged user so files land on the NAS share with the owner the user
    actually wants. Only attempted when explicitly asked for via PUID/PGID.
    """
    puid, pgid = _env_int('PUID'), _env_int('PGID')
    if puid is None and pgid is None:
        return
    if not hasattr(os, 'setuid') or os.geteuid() != 0:
        return
    try:
        import grp
        import pwd
        target_uid = puid if puid is not None else os.geteuid()
        target_gid = pgid if pgid is not None else os.getegid()
        try:
            os.setgroups([target_gid])
        except OSError:
            pass
        os.setgid(target_gid)
        os.setuid(target_uid)
        try:
            user = pwd.getpwuid(target_uid).pw_name
        except KeyError:
            user = str(target_uid)
        LOG.info('dropped privileges to %s:%s', user, target_gid)
    except (OSError, ImportError) as exc:
        LOG.warning('could not drop privileges to PUID=%s PGID=%s: %s',
                    puid, pgid, exc)


def describe_error(exc: Exception) -> str:
    """Turn an exception into one line the WebUI can actually act on."""
    from .site import SiteRequestError
    if isinstance(exc, SiteRequestError):
        if exc.status == 404:
            return f'页面不存在(404)：{exc.url}'
        if exc.status:
            return f'站点返回 {exc.status}：{exc.url}'
        return f'无法连接站点：{exc.url}（{exc.args[0].split("<-")[0].strip()}）'
    return f'{type(exc).__name__}: {exc}'


def _is_not_found(exc: Exception) -> bool:
    """True when the failure is a plain 404, meaning "no such page"."""
    from urllib.error import HTTPError
    if isinstance(exc, HTTPError):
        return exc.code == 404
    return '404' in str(exc) or 'not found' in str(exc).lower()


class Archiver:
    MAX_CATEGORY_PAGES = 100

    def __init__(self, config: dict):
        self.config = config
        site = config['site']
        self.client = HuangguoClient(
            site['base_url'],
            float(site.get('request_interval', 2)),
            int(site.get('timeout', 30)),
            backup_urls=site.get('backup_urls', []),
            attempts=int(site.get('attempts', 2)),
        )
        self.root = Path(config['download']['root'])
        self.state = StateDB(Path(config['state']['database']))
        self.retries = int(config['download'].get('retries', 3))
        self.minimum_duration = float(config['download'].get('minimum_duration', 3))
        self.recheck_days = float(config['download'].get('recheck_days', 7))
        self.failure_cooldown_hours = float(config['download'].get('failure_cooldown_hours', 24))
        self.episode_timeout = float(config['download'].get('episode_timeout', 1800))

    def close(self):
        self.state.close()

    def discover(self) -> list[tuple[int, dict]]:
        seen: set[int] = set()
        out: list[tuple[int, dict]] = []
        for category in self.config['site']['categories']:
            page = 1
            category_ids: list[int] = []
            known_total: int | None = None
            while page <= self.MAX_CATEGORY_PAGES:
                path = category['path'] if page == 1 else category['path'].rstrip('/') + f'/{page}/'
                try:
                    html = self.client.get_text(path)
                except Exception as exc:
                    # One flaky page must not throw away everything discovered
                    # so far: the site throttles and drops TLS handshakes under
                    # load. Retry the page once via a mirror, then keep what we
                    # have and move on to syncing it.
                    LOG.warning('category=%s page %d failed (%s); '
                                'continuing with %d album(s) already found',
                                category['name'], page, describe_error(exc),
                                len(category_ids))
                    if category_ids:
                        break
                    # The declared total is not trustworthy: the site advertises
                    # more albums than it can actually paginate (1416 vs 1398),
                    # so walking past the real last page answers 404. Treating
                    # that as "no more pages" is correct; letting it bubble up
                    # would abort the whole sync and lose every earlier page.
                    if _is_not_found(exc):
                        LOG.info('category=%s stopped at page %d (site returned 404)',
                                 category['name'], page)
                        break
                    raise
                ids, page_total = parse_category_panel(html)
                if page_total is not None:
                    known_total = page_total
                fresh = [i for i in ids if i not in category_ids]
                category_ids.extend(fresh)
                if not fresh:
                    # Last page reached, or the site ignored our page number
                    # and re-served the same content; stop either way.
                    break
                if known_total is not None and len(category_ids) >= known_total:
                    break
                page += 1
            else:
                LOG.warning('category=%s hit the %d-page cap, results may be incomplete',
                            category['name'], self.MAX_CATEGORY_PAGES)
            LOG.info('category=%s discovered=%d', category['name'], len(category_ids))
            for album_id in category_ids:
                if album_id not in seen:
                    seen.add(album_id)
                    out.append((album_id, category))
        return out

    # ---- merging episodes into a single movie file ----

    def _merge_config(self) -> dict:
        conf = self.config.get('merge') or {}
        return {
            'enabled': bool(conf.get('enabled', False)),
            'delete_episodes': bool(conf.get('delete_episodes', True)),
            'only_when_complete': bool(conf.get('only_when_complete', False)),
        }

    def _merged_path(self, album, album_dir: Path, episode_count: int) -> Path:
        # Movies live next to the show folder, in a library Emby indexes as
        # "movies", so one file per show does not collide with the TV layout.
        library = album_dir.parent
        return library / build_movie_filename(album.title, episode_count)

    def _episode_sources(self, album_id: int, album_dir: Path,
                         upto: int, start: int = 1) -> list[Path]:
        """Episode files start..upto, in order.

        A gap inside the requested range would silently drop an episode from
        the merged movie, so that is treated as "not ready" rather than
        skipped. Episodes below ``start`` are already inside the existing
        merged file and are not expected on disk.
        """
        season_dir = album_dir / 'Season 01'
        sources: list[Path] = []
        missing: list[int] = []
        for number in range(start, upto + 1):
            row = self.state.episode_row(album_id, number)
            candidate = None
            if row and row['path']:
                p = Path(row['path'])
                if p.is_file():
                    candidate = p
            if candidate is None:
                candidate = self._find_episode_video(season_dir, number)
            if candidate is None:
                missing.append(number)
            else:
                sources.append(candidate)
        if missing:
            LOG.warning('album=%s merge skipped: episodes missing %s',
                        album_id, missing[:10])
            return []
        return sources

    def merge_album(self, album, album_dir: Path, limit: int, *,
                    force: bool = False) -> dict | None:
        """Merge episodes 1..limit into one movie file, incrementally.

        Deleting the episodes after a merge would otherwise make a later
        "new episodes appeared" sync impossible to satisfy, so the merge is
        incremental: the existing merged file is treated as the prefix and only
        the newly arrived episodes are appended.
        """
        conf = self._merge_config()
        if not conf['enabled']:
            return None
        if limit < 1:
            return None

        row = self.state.merge_row(album.id)
        merged_upto = int(row['merged_episodes']) if row else 0
        merged_file = Path(row['path']) if row and row['path'] else None

        # Already up to date and the file still exists.
        if (not force and merged_file and merged_file.is_file()
                and merged_upto >= limit):
            return None

        # Incremental append: the old merged file becomes the first source.
        prefix: Path | None = None
        start = 1
        if (not force and merged_file and merged_file.is_file()
                and 0 < merged_upto < limit):
            prefix = merged_file
            start = merged_upto + 1
            LOG.info('album=%s incremental merge: have %d, adding %d..%d',
                     album.id, merged_upto, start, limit)

        # With a prefix in place, episodes 1..merged_upto are already inside
        # the merged file and were deliberately deleted, so only look for the
        # new range.
        sources = self._episode_sources(album.id, album_dir, limit, start=start)
        if not sources:
            LOG.info('album=%s merge: no new episode files found', album.id)
            return None

        target = self._merged_path(album, album_dir, limit)
        inputs = ([prefix] if prefix is not None else []) + sources
        LOG.info('album=%s merging %d file(s) -> %s', album.id, len(inputs), target.name)
        try:
            info = merge_videos(inputs, target, timeout=None)
        except Exception as exc:
            LOG.error('album=%s merge failed: %s', album.id, exc)
            return {'album_id': album.id, 'merged': False, 'error': str(exc)}

        write_movie_nfo(target.with_suffix('.nfo'), album, limit,
                        info.get('duration', 0))
        # Emby looks for "<movie>-poster.jpg" next to the movie file.
        poster = album_dir / 'poster.jpg'
        movie_poster = target.with_name(f'{target.stem}-poster.jpg')
        if poster.is_file() and not movie_poster.is_file():
            try:
                shutil.move(str(poster), str(movie_poster))
            except OSError as exc:
                LOG.warning('album=%s cannot move poster: %s', album.id, exc)
        self.state.mark_merge(album.id, str(target), limit, info.get('size', 0))

        # Old file replaced by a new name (episode count changed) -> drop stale
        # movie + sidecar + poster, otherwise Emby keeps showing the old entry.
        if prefix is not None and merged_file and merged_file != target:
            merged_file.unlink(missing_ok=True)
            merged_file.with_suffix('.nfo').unlink(missing_ok=True)
            stale_poster = merged_file.with_name(f'{merged_file.stem}-poster.jpg')
            stale_poster.unlink(missing_ok=True)

        deleted = 0
        if conf['delete_episodes']:
            for src in inputs:
                if src == prefix:
                    continue
                try:
                    src.unlink()
                    src.with_suffix('.nfo').unlink(missing_ok=True)
                    deleted += 1
                except OSError as exc:
                    LOG.warning('album=%s cannot delete %s: %s', album.id, src, exc)
            if deleted:
                LOG.info('album=%s deleted %d episode file(s) after merge',
                         album.id, deleted)
                # The TV folder is now empty; Emby would show a phantom show.
                self._prune_empty_season(album_dir)

        LOG.info('album=%s merged -> %s (%.1f MB, 全%d集)',
                 album.id, target.name, (info.get('size') or 0) / 1024 / 1024, limit)
        return {'album_id': album.id, 'merged': True, 'path': str(target),
                'episodes': limit, 'deleted': deleted}

    @staticmethod
    @staticmethod
    def _prune_empty_season(album_dir: Path) -> None:
        """Remove the TV layout once its episodes were merged away.

        Leaving an empty folder with a ``tvshow.nfo`` behind makes Emby index a
        phantom show with zero episodes, so the sidecars that only make sense
        for a TV entry are removed along with the folder.
        """
        season_dir = album_dir / 'Season 01'
        try:
            if season_dir.is_dir():
                for leftover in season_dir.iterdir():
                    leftover.unlink(missing_ok=True)
                season_dir.rmdir()
            if album_dir.is_dir():
                for name in ('tvshow.nfo', 'poster.jpg'):
                    (album_dir / name).unlink(missing_ok=True)
                for leftover in album_dir.iterdir():
                    leftover.unlink(missing_ok=True)
                album_dir.rmdir()
        except OSError as exc:
            LOG.debug('cannot prune %s: %s', album_dir, exc)

    def _album_dir(self, album, category: dict) -> Path:
        library = sanitize_filename(category['library'])
        row = self.state.album_row(album.id)
        stored = (row['directory'] if row else '') or ''
        if stored:
            stored_path = Path(stored)
            # Prefer the directory recorded in the state DB so a title change
            # on the site does not strand the old folder (and duplicate the
            # album in Emby). Only trust paths that still exist inside the
            # current media root and library.
            try:
                if (stored_path.is_dir() and stored_path.is_relative_to(self.root)
                        and stored_path.parent.name == library):
                    return stored_path
            except OSError:
                pass
        return self.root / library / f'{sanitize_filename(album.title)} [huangguo-{album.id}]'

    def _complete_and_fresh(self, album_id: int) -> bool:
        row = self.state.album_row(album_id)
        if row is None or not row['ended'] or row['episode_count'] < 1:
            return False
        if not self.state.album_is_complete(album_id, row['episode_count']):
            return False
        if not row['last_checked_at']:
            return False
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row['last_checked_at'])
        except ValueError:
            return False
        return age.total_seconds() < self.recheck_days * 86400

    def _sync_cover(self, album, poster: Path) -> None:
        # Two independent sources: the API returns an AES-encrypted image,
        # the og:image fallback is usually plain. A failure in one must not
        # prevent trying the other.
        for source, url in (('api', None), ('og:image', album.cover_url)):
            try:
                if source == 'api':
                    url = self.client.cover_url(album.id)
                if not url:
                    continue
                write_cover(self.client.get_bytes(url), poster)
                return
            except Exception as exc:
                LOG.warning('album=%s cover via %s failed: %s',
                            album.id, source, exc)

    def _recorded_video_path(self, album_id: int, number: int) -> Path | None:
        """Path the episode was actually downloaded to, if it still exists."""
        row = self.state.episode_row(album_id, number)
        if not row or not row['path']:
            return None
        path = Path(row['path'])
        return path if path.exists() else None

    @staticmethod
    def _find_episode_video(season_dir: Path, number: int) -> Path | None:
        """Locate an already-downloaded episode regardless of its title prefix.

        The site occasionally renames a show; the existing files keep the old
        name, so matching on the episode number alone avoids downloading a
        second copy into the same folder.
        """
        pattern = re.compile(rf'\.S01E{number:02d}\.(mp4|mkv|ts)$', re.I)
        try:
            for candidate in season_dir.iterdir():
                if candidate.is_file() and pattern.search(candidate.name):
                    return candidate
        except OSError:
            return None
        return None

    def sync_album(self, album_id: int, category: dict, *,
                   metadata_only: bool = False,
                   max_episodes: int | None = None,
                   force: bool = False) -> dict:
        html = self.client.get_text(f'/video/{album_id}/')
        album = parse_detail_html(album_id, category['name'], html)
        cat_match = next((c for c in self.config['site']['categories'] if c['name'] == album.category), None)
        if cat_match:
            category = cat_match
        elif album.category != category['name']:
            LOG.warning('skip album=%s expected=%s actual=%s',
                        album.id, category['name'], album.category)
            return {'album_id': album.id, 'skipped': True,
                    'expected_category': category['name'],
                    'actual_category': album.category}
        album_dir = self._album_dir(album, category)
        self.state.upsert_album(album.id, album.category, album.title,
                                album.episode_count, album.ended,
                                str(album_dir))
        season_dir = album_dir / 'Season 01'
        season_dir.mkdir(parents=True, exist_ok=True)
        write_tvshow_nfo(album_dir / 'tvshow.nfo', album)
        poster = album_dir / 'poster.jpg'
        if not valid_cover(poster):
            self._sync_cover(album, poster)
        limit = album.episode_count if max_episodes is None else min(album.episode_count, max_episodes)
        done = 0
        failed_episodes: list[int] = []
        pending = [n for n in range(1, limit + 1) if not self.state.episode_done(album.id, n)]
        if pending and not metadata_only:
            LOG.info('album=%s [%s] 开始同步: 待下载 %d/%d 集', album.id, album.title, len(pending), limit)
        for number in range(1, limit + 1):
            video = season_dir / build_episode_filename(album.title, number)
            ep = Episode(album.id, number, f'S01E{number:02d}', album.plot)
            if metadata_only:
                write_episode_nfo(video.with_suffix('.nfo'), album, ep)
                continue
            if self.state.episode_done(album.id, number):
                # The file was downloaded under an older title, so its name no
                # longer matches the one derived from the current title; keep
                # the sidecar next to the video it actually describes.
                recorded = self._recorded_video_path(album.id, number) or video
                if not recorded.with_suffix('.nfo').exists():
                    write_episode_nfo(recorded.with_suffix('.nfo'), album, ep)
                done += 1
                continue
            # A rename on the site must not trigger a re-download of episodes
            # that are already sitting in this folder under the old name.
            existing = self._find_episode_video(season_dir, number)
            if existing is None:
                write_episode_nfo(video.with_suffix('.nfo'), album, ep)
                if video.exists():
                    existing = video
            if existing is not None:
                try:
                    info = probe_media(existing)
                    if validate_media(info, self.minimum_duration):
                        self.state.mark_episode(album.id, number, str(existing), 'done',
                                                info.get('size', 0))
                        if not existing.with_suffix('.nfo').exists():
                            write_episode_nfo(existing.with_suffix('.nfo'), album, ep)
                        done += 1
                        continue
                except Exception:
                    pass
            if not force:
                failed_at = self.state.episode_failed_at(album.id, number)
                if failed_at:
                    try:
                        age = datetime.now(timezone.utc) - datetime.fromisoformat(failed_at)
                    except ValueError:
                        age = None
                    if age is not None and age.total_seconds() < self.failure_cooldown_hours * 3600:
                        LOG.info('album=%s ep=%s in cooldown, skipping', album.id, number)
                        continue
            LOG.info('album=%s [%s] 第 %d/%d 集 开始下载...', album.id, album.title, number, limit)
            last = None
            succeeded = False
            for attempt in range(1, self.retries + 1):
                try:
                    url = self.client.play_url(album.id, number)
                    info = download_hls(url, video, self.client.base_url + '/',
                                        self.minimum_duration, timeout=self.episode_timeout)
                    self.state.mark_episode(album.id, number, str(video), 'done',
                                            info.get('size', 0))
                    done += 1
                    succeeded = True
                    size_mb = (info.get('size', 0) or 0) / (1024 * 1024)
                    LOG.info('album=%s [%s] 第 %d/%d 集 下载完成 (时长 %.0f秒, 大小 %.1fMB)',
                             album.id, album.title, number, limit, info.get('duration', 0), size_mb)
                    break
                except Exception as exc:
                    last = exc
                    err_msg = str(exc).lower()
                    is_permanent = any(k in err_msg for k in (
                        'empty video_url', 'play endpoint failed', 'not found', '404', '403',
                        '不存在', '已下架', '下架'
                    ))
                    LOG.warning('album=%s ep=%s attempt=%s failed=%s',
                                album.id, number, attempt, exc)
                    if is_permanent:
                        LOG.error('album=%s ep=%s permanent failure, skipping retries: %s',
                                  album.id, number, exc)
                        break
                    if attempt < self.retries:
                        time.sleep([5, 15, 30][min(attempt - 1, 2)])
            if not succeeded:
                # Recorded even for permanent failures so the cooldown applies,
                # the WebUI shows the episode as failed, and later syncs do not
                # keep hammering a withdrawn episode.
                self.state.mark_episode(album.id, number, str(video), 'failed')
                failed_episodes.append(number)
                LOG.error('album=%s ep=%s giving up after %s attempt(s): %s',
                          album.id, number, attempt, last)
        if pending and done == limit and not metadata_only:
            LOG.info('album=%s [%s] 全部 %d 集已下载完毕', album.id, album.title, limit)

        # Merge only when every episode up to `limit` is present: a partial
        # merge would produce a movie with a hole in the middle.
        merge_result = None
        if not metadata_only:
            conf = self._merge_config()
            ready = (conf['enabled'] and limit > 0 and done >= limit
                     and not failed_episodes)
            if ready and conf['only_when_complete'] and not album.ended:
                LOG.info('album=%s merge deferred: still airing', album.id)
            elif ready:
                merge_result = self.merge_album(album, album_dir, limit, force=force)

        result = {'album': asdict(album), 'directory': str(album_dir),
                  'episodes_done': done, 'metadata_only': metadata_only}
        if merge_result:
            result['merge'] = merge_result
        if failed_episodes:
            result['failed_episodes'] = failed_episodes
            LOG.error('album=%s finished with %d failed episodes: %s',
                      album.id, len(failed_episodes), failed_episodes)
        return result

    def scan(self, *, metadata_only=False, only_id=None,
             max_albums=None, max_episodes=None):
        force = bool(only_id)
        if only_id:
            found = [(only_id, self.config['site']['categories'][0])]
        else:
            found = self.discover()
        if max_albums:
            found = found[:max_albums]
        results = []
        failures = 0
        skipped_complete = 0
        for album_id, category in found:
            if not force and self._complete_and_fresh(album_id):
                skipped_complete += 1
                continue
            try:
                result = self.sync_album(
                    album_id, category, metadata_only=metadata_only,
                    max_episodes=max_episodes, force=force)
                results.append(result)
                if result.get('failed_episodes'):
                    failures += 1
            except Exception as exc:
                failures += 1
                LOG.exception('sync album %s failed: %s', album_id, exc)
        episodes_done = sum(r.get('episodes_done', 0) for r in results)
        LOG.info('sync finished synced=%d failures=%d skipped=%d episodes=%d',
                 len(results), failures, skipped_complete, episodes_done)
        return results, failures


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def run_daemon(config: dict, schedule: str, web_port: int = 7731) -> int:
    from .web.app import (create_app, mark_sync_finished, release_sync,
                          set_shared_state, try_acquire_sync)
    from .web.log_handler import LogBuffer
    import uvicorn

    log_buf = LogBuffer()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    log_buf.setFormatter(formatter)
    root_logger = logging.getLogger('HGXZ')
    root_logger.addHandler(log_buf)

    log_dir_str = config.get('log', {}).get('directory', '')
    if log_dir_str:
        log_dir = Path(log_dir_str)
        # A read-only or missing log volume must not take the whole service
        # down; the in-memory buffer still feeds the WebUI.
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.TimedRotatingFileHandler(
                log_dir / 'hgxz.log', when='midnight', backupCount=30,
                encoding='utf-8')
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except OSError as exc:
            LOG.warning('cannot write logs to %s (%s); continuing without a log file',
                        log_dir, exc)

    archiver = Archiver(config)
    archiver_ref = [archiver]
    set_shared_state(archiver.state, log_buf, archiver_ref)

    from .web.app import _sync_status

    try:
        hour, minute = (int(part) for part in schedule.split(':'))
    except ValueError:
        LOG.error('invalid --schedule %r, expected HH:MM', schedule)
        return 1

    run_on_start = _env('HGXZ_SYNC_ON_START', 'SYNC_ON_START',
                        default='1').lower() in ('1', 'true', 'yes', 'on')

    def sync_loop():
        if not run_on_start:
            LOG.info('HGXZ_SYNC_ON_START=0, waiting for the first scheduled run')
        while True:
            if not run_on_start and _first_run[0]:
                _first_run[0] = False
            else:
                # Manual syncs (WebUI) and this loop must never run
                # concurrently: they would download the same episode into the
                # same .part file.
                if not try_acquire_sync():
                    LOG.warning('scheduled sync skipped: another sync is already running')
                else:
                    try:
                        archiver.scan()
                        mark_sync_finished()
                    except Exception as exc:
                        _sync_status['last_error'] = describe_error(exc)
                        LOG.exception('sync failed')
                    finally:
                        release_sync()

            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            LOG.info('next sync at %s (%.0f seconds from now)',
                     target.strftime('%Y-%m-%d %H:%M:%S'), wait_seconds)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                time.sleep(30)

    _first_run = [True]
    sync_thread = threading.Thread(target=sync_loop, daemon=True, name='sync-daemon')
    sync_thread.start()

    host = _env('HGXZ_HOST', 'HOST', default='0.0.0.0')
    api_port = _env_int('HGXZ_API_PORT', 'API_PORT', default=7732)

    # Two listeners, one process: the UI owns the front-end port and the API
    # owns its own so a reverse proxy or a separate front end can talk to the
    # backend directly. Both share the same state, log buffer and sync lock.
    servers = [('web', create_app(serve_static=True), web_port)]
    if api_port and api_port != web_port:
        servers.append(('api', create_app(serve_static=False), api_port))

    for name, _, port in servers:
        LOG.info('%s listening on http://%s:%s', name, host, port)

    async def serve_all():
        import uvicorn
        configs = [uvicorn.Config(app, host=host, port=port, log_level='warning')
                   for _, app, port in servers]
        instances = [uvicorn.Server(cfg) for cfg in configs]
        await asyncio.gather(*(server.serve() for server in instances))

    try:
        asyncio.run(serve_all())
    except KeyboardInterrupt:
        LOG.info('shutting down')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog='HGXZ', description='黄果下载')
    parser.add_argument('--config', default=_env('HGXZ_CONFIG', default='/config/config.json'))
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--only-id', type=int)
    parser.add_argument('--max-albums', type=int)
    parser.add_argument('--max-episodes', type=int)
    parser.add_argument('--rebuild-covers', action='store_true')
    parser.add_argument('--merge-existing', nargs='?', const='', default=None,
                        metavar='ROOT',
                        help='把已下载的分集合并成单文件（不联网）；'
                             '不填路径则用配置里的媒体根目录')
    parser.add_argument('--scan-existing', action='store_true',
                        help='扫描媒体目录，将已有视频注册到状态库')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--force-merge', action='store_true',
                        help='配合 --merge-existing：已有合并文件也重新合并')
    parser.add_argument('--merge-dry-run', action='store_true',
                        help='配合 --merge-existing：只列出会做什么，不改动文件')
    parser.add_argument('--daemon', action='store_true',
                        default=_env('DAEMON', default='').lower() in ('1', 'true', 'yes', 'on'),
                        help='常驻运行：启动后同步一次，之后每天定时同步，同时开启 WebUI')
    parser.add_argument('--schedule', default=_env('SCHEDULE', 'HGXZ_SCHEDULE', default='03:30'),
                        help='--daemon 模式下的每日同步时间 HH:MM')
    parser.add_argument('--port', type=int,
                        default=_env_int('HGXZ_PORT', 'PORT', 'WEBUI_PORT', default=7731),
                        help='前端 WebUI 端口（默认 7731）')
    parser.add_argument('--api-port', type=int,
                        default=_env_int('HGXZ_API_PORT', 'API_PORT', default=7732),
                        help='后端 API 端口（默认 7732，设为 0 或与前端相同则只监听一个端口）')
    parser.add_argument('--version', action='version', version=f'HGXZ {__version__}')
    args = parser.parse_args()

    configure_logging()
    # NAS shares usually need permissive masks or the media server cannot read
    # the files; PUID/PGID are honoured for the same reason.
    drop_privileges()
    umask_val = _env('UMASK')
    if umask_val:
        try:
            os.umask(int(umask_val, 8))
        except Exception as exc:
            logging.getLogger('HGXZ').warning('failed to set umask=%s: %s', umask_val, exc)
    config = ensure_config(Path(args.config))

    if args.daemon:
        return run_daemon(config, args.schedule, args.port)

    if args.scan_existing:
        from .scanner import scan_existing
        state = StateDB(Path(config['state']['database']))
        try:
            result = scan_existing(
                Path(config['download']['root']), state,
                float(config['download'].get('minimum_duration', 3)))
        finally:
            state.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.merge_existing is not None:
        from .merge_existing import merge_library
        merge_conf = config.get('merge') or {}
        root = Path(args.merge_existing) if args.merge_existing else Path(config['download']['root'])
        result = merge_library(
            root,
            delete_episodes=merge_conf.get('delete_episodes', True),
            force=bool(args.force_merge),
            dry_run=bool(args.merge_dry_run))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else f"扫描 {result['albums']} 个专辑，合并 {result['merged']} 个，"
                   f"跳过 {result['skipped']} 个，失败 {result['failed']} 个")
        return 0 if result['failed'] == 0 else 1

    if args.rebuild_covers:
        from .rebuild_covers import rebuild
        site = config['site']
        client = HuangguoClient(
            site['base_url'],
            float(site.get('request_interval', 2)),
            int(site.get('timeout', 30)),
            backup_urls=site.get('backup_urls', []),
        )
        result = rebuild(Path(config['download']['root']), client)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['failed'] == 0 else 1

    archiver = Archiver(config)
    try:
        results, failures = archiver.scan(
            metadata_only=args.metadata_only, only_id=args.only_id,
            max_albums=args.max_albums, max_episodes=args.max_episodes)
    finally:
        archiver.close()
    if args.json:
        print(json.dumps({'results': results, 'failures': failures},
                         ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
