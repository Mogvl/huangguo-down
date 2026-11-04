from __future__ import annotations

from pathlib import Path
import argparse
import json
import logging
import re

from .cover import valid_cover, write_cover
from .site import HuangguoClient


LOG = logging.getLogger('HGXZ')
PATTERN = re.compile(r'\[huangguo-(\d+)\]$')


def _fetch_cover(client: HuangguoClient, album_id: int, poster: Path) -> None:
    url = client.cover_url(album_id)
    if not url:
        raise RuntimeError('cover endpoint returned empty URL')
    write_cover(client.get_bytes(url), poster)


def rebuild(root: Path, client: HuangguoClient, state=None) -> dict:
    result = {'checked': 0, 'rebuilt': 0, 'valid': 0, 'failed': 0}

    # 1) Show folders that still use the <title> [huangguo-id] layout.
    for nfo in sorted(root.rglob('tvshow.nfo')):
        album_dir = nfo.parent
        match = PATTERN.search(album_dir.name)
        if not match:
            continue
        result['checked'] += 1
        poster = album_dir / 'poster.jpg'
        if valid_cover(poster):
            result['valid'] += 1
            continue
        album_id = int(match.group(1))
        try:
            _fetch_cover(client, album_id, poster)
            result['rebuilt'] += 1
            LOG.info('rebuilt album=%s dir=%s', album_id, album_dir.name)
        except Exception as exc:
            result['failed'] += 1
            LOG.error('failed album=%s dir=%s error=%s', album_id, album_dir.name, exc)

    # 2) Merged movies live in <title> (全N集)/<title> (全N集).mp4 with the
    #    cover next to them as <movie>-poster.jpg. Recover missing ones using
    #    the album id recorded in the merges table.
    if state is not None:
        try:
            rows = state.merge_rows_with_titles()
        except AttributeError:
            rows = []
        for row in rows:
            movie = Path(row['path'])
            if not movie.is_file():
                continue
            poster = movie.parent / f'{movie.stem}-poster.jpg'
            result['checked'] += 1
            if valid_cover(poster):
                result['valid'] += 1
                continue
            album_id = int(row['album_id'])
            try:
                _fetch_cover(client, album_id, poster)
                result['rebuilt'] += 1
                LOG.info('rebuilt merged album=%s poster=%s', album_id, poster)
            except Exception as exc:
                result['failed'] += 1
                LOG.error('failed merged album=%s error=%s', album_id, exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/config/config.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    site = config['site']
    client = HuangguoClient(
        site['base_url'],
        float(site.get('request_interval', 2)),
        int(site.get('timeout', 30)),
        backup_urls=site.get('backup_urls', []),
    )
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    state = None
    try:
        from .state import StateDB
        state = StateDB(Path(config['state']['database']))
    except Exception as exc:
        logging.getLogger('HGXZ').warning('state DB unavailable, merged covers skipped: %s', exc)
    try:
        result = rebuild(Path(config['download']['root']), client, state=state)
    finally:
        if state is not None:
            state.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
