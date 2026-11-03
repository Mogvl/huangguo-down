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


def rebuild(root: Path, client: HuangguoClient) -> dict:
    result = {'checked': 0, 'rebuilt': 0, 'valid': 0, 'failed': 0}
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
            url = client.cover_url(album_id)
            if not url:
                raise RuntimeError('cover endpoint returned empty URL')
            write_cover(client.get_bytes(url), poster)
            result['rebuilt'] += 1
            LOG.info('rebuilt album=%s dir=%s', album_id, album_dir.name)
        except Exception as exc:
            result['failed'] += 1
            LOG.error('failed album=%s dir=%s error=%s', album_id, album_dir.name, exc)
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
    result = rebuild(Path(config['download']['root']), client)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
