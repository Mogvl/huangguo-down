from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time


LOG = logging.getLogger(__name__)

_HEADERS_UA = 'Mozilla/5.0'
_SEGMENT_ATTEMPTS = 3


def probe_media(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries',
        'format=duration,size:stream=codec_type', '-of', 'json', str(path)
    ], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get('streams') or []
    return {
        'duration': float((payload.get('format') or {}).get('duration') or 0),
        'size': int((payload.get('format') or {}).get('size') or path.stat().st_size),
        'video': sum(1 for x in streams if x.get('codec_type') == 'video'),
        'audio': sum(1 for x in streams if x.get('codec_type') == 'audio'),
    }


def validate_media(info: dict, minimum_duration: float = 3.0) -> bool:
    return bool(info.get('duration', 0) >= minimum_duration and info.get('video', 0) >= 1)


def _fetch(url: str, referer: str, timeout: float) -> bytes:
    req = Request(url, headers={'User-Agent': _HEADERS_UA, 'Referer': referer})
    with urlopen(req, timeout=timeout) as response:
        return response.read()


def _attrs(line: str) -> dict:
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("([^"]*)"|[^,]*)', line):
        attrs[match.group(1)] = match.group(3) if match.group(3) is not None else match.group(2)
    return attrs


def _parse_m3u8(text: str, playlist_url: str) -> dict:
    """Parse a media playlist; keys may change mid-stream so each segment
    remembers the key that was active when it was listed."""
    key_uri = None
    iv = None
    sequence = 0
    segments: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-KEY:'):
            attrs = _attrs(line)
            method = attrs.get('METHOD', 'NONE')
            if method == 'NONE':
                key_uri, iv = None, None
            elif method == 'AES-128':
                uri = attrs.get('URI', '')
                if not uri:
                    raise RuntimeError('AES-128 key without URI')
                key_uri = urljoin(playlist_url, uri)
                raw_iv = attrs.get('IV')
                iv = bytes.fromhex(raw_iv[2:]) if raw_iv else None
            else:
                raise RuntimeError(f'unsupported key method: {method}')
        elif not line.startswith('#'):
            segments.append({
                'url': urljoin(playlist_url, line),
                'key_uri': key_uri,
                'iv': iv if iv is not None else sequence.to_bytes(16, 'big'),
            })
            sequence += 1
    return {'segments': segments}


def _resolve_media_playlist(url: str, referer: str, timeout: float) -> tuple[str, str]:
    """Follow master playlists (multiple variants) down to a media playlist."""
    playlist_url = url
    for _ in range(3):
        text = _fetch(playlist_url, referer, timeout).decode('utf-8', errors='replace')
        if '#EXT-X-STREAM-INF' not in text:
            return playlist_url, text
        lines = text.splitlines()
        variant, best_bw = None, -1
        for index, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF'):
                bandwidth = int(_attrs(line).get('BANDWIDTH', '0') or 0)
                if bandwidth >= best_bw and index + 1 < len(lines):
                    variant, best_bw = lines[index + 1].strip(), bandwidth
        if not variant:
            raise RuntimeError('master playlist has no variant')
        playlist_url = urljoin(playlist_url, variant)
    raise RuntimeError('too many playlist levels')


def _decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        raise RuntimeError(f'segment size {len(data)} not AES block aligned')
    # Decrypt via openssl (like cover.py) and strip PKCS#7 padding manually:
    # the last byte tells how many padding bytes to drop.
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / 'seg.bin'
        dst = Path(td) / 'seg.ts'
        src.write_bytes(data)
        proc = subprocess.run([
            'openssl', 'enc', '-d', '-aes-128-cbc', '-nopad',
            '-K', key.hex(), '-iv', iv.hex(),
            '-in', str(src), '-out', str(dst),
        ], capture_output=True)
        if proc.returncode:
            raise RuntimeError('segment AES decryption failed')
        plain = dst.read_bytes()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain


def download_hls(url: str, destination: Path, referer: str, minimum_duration: float = 3.0, timeout: float | None = 1800.0) -> dict:
    # Fetch every HLS segment as its own short-lived request with retries
    # instead of one long ffmpeg stream: the CDN resets long connections
    # mid-transfer, but a few megabytes per request survive that fine.
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + '.part')
    partial.unlink(missing_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    fetch_timeout = 30.0
    try:
        playlist_url, playlist_text = _resolve_media_playlist(url, referer, fetch_timeout)
        parsed = _parse_m3u8(playlist_text, playlist_url)
        segments = parsed['segments']
        if not segments:
            raise RuntimeError('playlist has no segments')
        LOG.info('HLS playlist parsed: %d segments', len(segments))
        playlist_referer = f'{urlsplit(playlist_url).scheme}://{urlsplit(playlist_url).netloc}/'
        keys: dict[str, bytes] = {}
        workdir = Path(tempfile.mkdtemp(prefix='hls-'))
        try:
            listing = workdir / 'concat.txt'
            with listing.open('w') as fh:
                for index, segment in enumerate(segments):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(f'HLS download exceeded {timeout}s')
                    key_uri = segment['key_uri']
                    if key_uri and key_uri not in keys:
                        keys[key_uri] = _fetch(key_uri, playlist_referer, fetch_timeout)
                    data = None
                    last_error: Exception | None = None
                    for attempt in range(1, _SEGMENT_ATTEMPTS + 1):
                        remaining = None if deadline is None else deadline - time.monotonic()
                        if remaining is not None and remaining <= 0:
                            raise TimeoutError(f'HLS download exceeded {timeout}s')
                        try:
                            data = _fetch(segment['url'], playlist_referer,
                                          fetch_timeout if remaining is None else min(fetch_timeout, remaining))
                            if key_uri:
                                data = _decrypt_segment(data, keys[key_uri], segment['iv'])
                            break
                        except Exception as exc:
                            last_error = exc
                            if attempt < _SEGMENT_ATTEMPTS:
                                LOG.warning('segment %d/%d attempt %d failed: %s', index + 1, len(segments), attempt, exc)
                                time.sleep(2 * attempt)
                    else:
                        raise RuntimeError(f'segment {index + 1}/{len(segments)} failed after {_SEGMENT_ATTEMPTS} attempts: {last_error}')
                    seg_path = workdir / f'{index:05d}.ts'
                    seg_path.write_bytes(data)
                    fh.write(f"file '{seg_path}'\n")
                    if (index + 1) % 10 == 0 or (index + 1) == len(segments):
                        LOG.info('downloaded segments %d/%d', index + 1, len(segments))
            LOG.info('merging %d segments with ffmpeg...', len(segments))
            base_cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
                '-analyzeduration', '100M', '-probesize', '100M',
                '-fflags', '+igndts+genpts',
                '-f', 'concat', '-safe', '0', '-i', str(listing),
                '-avoid_negative_ts', 'make_zero',
                '-map', '0',
            ]
            cmd = base_cmd + ['-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(partial)]
            try:
                result = subprocess.run(cmd, check=True, capture_output=True, text=True,
                                        timeout=None if deadline is None else max(1.0, deadline - time.monotonic()))
                stderr = result.stderr or ''
                if 'failed too many times, skipping' in stderr.lower():
                    raise RuntimeError('concat reported skipped segments')
            except subprocess.CalledProcessError as exc:
                LOG.warning('ffmpeg copy mode failed, retrying with audio transcode: %s', (exc.stderr or '').strip()[-200:])
                cmd_fallback = base_cmd + ['-c:v', 'copy', '-c:a', 'aac', '-movflags', '+faststart', '-f', 'mp4', str(partial)]
                try:
                    result = subprocess.run(cmd_fallback, check=True, capture_output=True, text=True,
                                            timeout=None if deadline is None else max(1.0, deadline - time.monotonic()))
                except subprocess.CalledProcessError as exc2:
                    stderr = (exc2.stderr or '').strip()
                    raise RuntimeError(f'ffmpeg exited with {exc2.returncode}: {stderr[-800:]}') from exc2
        finally:
            for child in workdir.iterdir():
                child.unlink(missing_ok=True)
            workdir.rmdir()
        info = probe_media(partial)
        if not validate_media(info, minimum_duration):
            raise RuntimeError(f'invalid media: {info}')
        os.replace(partial, destination)
        return info
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def merge_videos(sources: list[Path], destination: Path,
                 timeout: float | None = None) -> dict:
    """Concatenate several videos into one file, stream-copying when possible.

    All episodes of a show come from the same encoder, so the concat demuxer
    with ``-c copy`` is lossless and fast. If the audio codec changes midway
    (some CDNs switch profiles) the copy fails and we fall back to re-encoding
    only the audio, which is cheap compared to a full transcode.

    The output is written to a temporary sibling and moved into place, so an
    interrupted merge never leaves a half file where Emby can index it.
    """
    if not sources:
        raise ValueError('merge needs at least one source')
    for src in sources:
        if not src.is_file():
            raise FileNotFoundError(src)

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write the partial output through tempfile so the name never depends on
    # Write the partial output next to the destination, on the same filesystem,
    # so the final os.replace is a cheap rename. Putting it under /tmp (tmpfs)
    # makes os.replace fail with EXDEV (Invalid cross-device link) when the
    # media library is a separate mount, which is the normal NAS setup.
    workdir = destination.parent / f'.merge-{os.getpid()}'
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to a temp dir only if the destination's filesystem rejects
        # the sidecar directory (e.g. a read-only mount); in that case the
        # caller's own volume is still where the file ends up via copy.
        workdir = Path(tempfile.mkdtemp(prefix='merge-'))
    partial = workdir / 'partial.mp4'
    try:
        listing = workdir / 'concat.txt'
        with listing.open('w', encoding='utf-8') as fh:
            for src in sources:
                # The concat demuxer treats ' as a quote delimiter.
                escaped = str(src.resolve()).replace("'", "'\\''")
                fh.write(f"file '{escaped}'\n")

        base = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
            '-analyzeduration', '100M', '-probesize', '100M',
            '-fflags', '+igndts+genpts',
            '-f', 'concat', '-safe', '0', '-i', str(listing),
            '-avoid_negative_ts', 'make_zero',
            '-map', '0',
        ]
        cmd = base + ['-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(partial)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            LOG.warning('merge copy failed, retrying with audio transcode: %s',
                        (exc.stderr or '').strip()[-300:])
            cmd2 = base + ['-c:v', 'copy', '-c:a', 'aac',
                           '-movflags', '+faststart', '-f', 'mp4', str(partial)]
            try:
                subprocess.run(cmd2, check=True, capture_output=True, text=True, timeout=timeout)
            except subprocess.CalledProcessError as exc2:
                raise RuntimeError(
                    f'merge failed: {(exc2.stderr or "").strip()[-800:]}') from exc2

        if not partial.is_file():
            raise RuntimeError('ffmpeg produced no output file')
        info = probe_media(partial)
        if not info.get('video'):
            raise RuntimeError(f'merged file has no video stream: {info}')
        # os.replace is a rename and fails across filesystems (EXDEV); fall back
        # to a copy+delete so merges onto a separate media mount still work.
        try:
            os.replace(partial, destination)
        except OSError as exc:
            if getattr(exc, 'errno', None) == 18:  # EXDEV: cross-device
                shutil.move(str(partial), str(destination))
            else:
                raise
        return info
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        # Only remove the sidecar directory we created next to the destination;
        # never recurse into a system temp dir we did not fully own.
        if str(workdir).startswith(str(destination.parent)):
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
