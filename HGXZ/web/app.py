from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..state import StateDB
from .. import __version__
from .log_handler import LogBuffer

LOG = logging.getLogger('HGXZ')

STATIC_DIR = Path(__file__).parent / 'static'

_state_db: StateDB | None = None
_log_buffer: LogBuffer | None = None
_archiver_ref: list = []
_sync_status: dict = {'running': False, 'last_sync': None, 'last_error': None}
_last_merge_result: dict = {}
# Shared by the scheduled loop (cli.run_daemon) and every WebUI trigger so two
# syncs can never run at once -- they would download the same episode into the
# same ".part" file.
_sync_lock = Lock()


def try_acquire_sync() -> bool:
    """Claim the single sync slot. False means one is already running."""
    if not _sync_lock.acquire(blocking=False):
        return False
    _sync_status['running'] = True
    _sync_status['last_error'] = None
    return True


def release_sync() -> None:
    _sync_status['running'] = False
    try:
        _sync_lock.release()
    except RuntimeError:
        pass


def mark_sync_finished() -> None:
    from datetime import datetime
    _sync_status['last_sync'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def set_shared_state(state: StateDB, log_buf: LogBuffer, archiver_holder: list) -> None:
    global _state_db, _log_buffer, _archiver_ref
    _state_db = state
    _log_buffer = log_buf
    _archiver_ref = archiver_holder


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app(serve_static: bool = True) -> FastAPI:
    """Build the ASGI app.

    ``serve_static=False`` produces the API-only variant that listens on the
    backend port (7732): same routes, no HTML/static assets, plus permissive
    CORS so the UI port or an external script can call it directly.
    """
    app = FastAPI(title='HGXZ - 黄果下载', lifespan=lifespan)

    if not serve_static:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=['*'],
            allow_methods=['*'],
            allow_headers=['*'],
        )

    # A NAS WebUI is reachable from the whole LAN, so an optional shared token
    # keeps strangers from triggering downloads. Unset means "no auth", which
    # matches the original behaviour and keeps localhost usage frictionless.
    token = os.environ.get('HGXZ_WEB_TOKEN', '').strip()

    @app.middleware('http')
    async def _check_token(request, call_next):
        if token and request.url.path.startswith('/api'):
            supplied = (request.headers.get('x-hgxz-token')
                        or request.query_params.get('token') or '')
            if supplied != token:
                return JSONResponse({'error': 'unauthorized'}, 401)
        return await call_next(request)

    @app.get('/healthz')
    async def healthz():
        return {'status': 'ok', 'version': __version__,
                'ready': _state_db is not None}

    if serve_static:
        app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

        @app.get('/', response_class=HTMLResponse)
        async def index():
            return FileResponse(STATIC_DIR / 'index.html')

        @app.get('/favicon.ico')
        async def favicon():
            icon = STATIC_DIR / 'favicon.ico'
            if icon.is_file():
                return FileResponse(icon)
            return JSONResponse({'error': 'not found'}, 404)

    @app.get('/api/dashboard')
    async def dashboard():
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        stats = _state_db.dashboard_stats()
        stats['sync'] = _sync_status
        return stats

    @app.get('/api/albums')
    async def albums(
        category: str | None = Query(None),
        status: str | None = Query(None),
        search: str | None = Query(None),
    ):
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        return _state_db.all_albums(category=category, status=status, search=search)

    @app.get('/api/albums/{album_id}')
    async def album_detail(album_id: int):
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        result = _state_db.album_with_episodes(album_id)
        if not result:
            return JSONResponse({'error': 'not found'}, 404)
        result.update(_state_db.episode_stats(album_id))
        merge = _state_db.merge_row(album_id)
        result['merge'] = ({
            'path': merge['path'],
            'episodes': merge['merged_episodes'],
            'size_bytes': merge['size_bytes'],
            'exists': Path(merge['path']).is_file(),
        } if merge else None)
        directory = result.get('directory') or ''
        result['poster_available'] = bool(directory) and (
            (Path(directory) / 'poster.jpg').is_file()
            or _merged_poster_exists(album_id, result.get('merge') or {})
        )
        return result

    def _merged_poster_exists(album_id: int, merge_info: dict) -> bool:
        path = (merge_info.get('path') if isinstance(merge_info, dict) else None) or ''
        if not path:
            mrow = _state_db.merge_row(album_id) if _state_db else None
            path = (mrow['path'] if mrow else '') or ''
        if not path:
            return False
        movie = Path(path)
        # Emby-style "<movie>-poster.jpg" next to the merged file.
        sidecar = movie.parent / f'{movie.stem}-poster.jpg'
        return sidecar.is_file() and sidecar.stat().st_size > 0

    @app.get('/api/albums/{album_id}/poster')
    async def album_poster(album_id: int):
        """Serve the scraped poster so the library list can show real covers.

        After a merge the poster lives next to the merged movie file as
        ``<movie>-poster.jpg`` (the old ``poster.jpg`` inside the now-removed
        show folder is gone), so we check both locations.
        """
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        row = _state_db.album_row(album_id)
        if not row or not row['directory']:
            return JSONResponse({'error': 'not found'}, 404)
        candidates = [Path(row['directory']) / 'poster.jpg']
        mrow = _state_db.merge_row(album_id)
        if mrow and mrow['path']:
            movie = Path(mrow['path'])
            candidates.append(movie.parent / f'{movie.stem}-poster.jpg')
        for poster in candidates:
            try:
                if poster.is_file() and poster.stat().st_size > 0:
                    return FileResponse(poster, media_type='image/jpeg',
                                        headers={'Cache-Control': 'max-age=3600'})
            except OSError:
                continue
        return JSONResponse({'error': 'not found'}, 404)

    def _start_background(name: str, work):
        """Run a blocking job once, refusing to start while another is active."""
        if not try_acquire_sync():
            return False

        def _run():
            try:
                work()
                mark_sync_finished()
            except Exception as exc:
                from ..cli import describe_error
                _sync_status['last_error'] = describe_error(exc)
                LOG.exception('%s failed: %s', name, exc)
            finally:
                release_sync()

        import threading
        threading.Thread(target=_run, daemon=True, name=name).start()
        return True

    @app.post('/api/sync')
    async def trigger_sync(album_id: int | None = None):
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]
        if not _start_background('manual-sync',
                                 lambda: archiver.scan(only_id=album_id)):
            return JSONResponse({'error': '同步正在进行中'}, 409)
        return {'status': 'started', 'album_id': album_id}

    @app.post('/api/retry-failed')
    async def retry_failed(album_id: int | None = None):
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        count = _state_db.reset_failed(album_id)
        return {'reset': count}

    @app.post('/api/scan-existing')
    async def scan_existing_api():
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]

        def work():
            from ..scanner import scan_existing
            result = scan_existing(archiver.root, archiver.state,
                                   archiver.minimum_duration)
            LOG.info('scan existing result: %s', result)

        if not _start_background('scan-existing', work):
            return JSONResponse({'error': '同步正在进行中'}, 409)
        return {'status': 'started'}

    @app.post('/api/merge-existing')
    async def merge_existing_api(dry_run: bool = False,
                                 keep_episodes: bool | None = None,
                                 force: bool = False):
        """Merge already-downloaded episodes into single movie files.

        Runs entirely on local files, so it works even when the site is down.
        """
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]
        conf = archiver._merge_config()
        delete_eps = conf['delete_episodes'] if keep_episodes is None else not keep_episodes

        def work():
            from ..merge_existing import merge_library
            result = merge_library(archiver.root, delete_episodes=delete_eps,
                                   dry_run=dry_run, force=force)
            LOG.info('merge existing result: %s', {
                k: v for k, v in result.items() if k != 'details'})
            _last_merge_result['value'] = result

        if not _start_background('merge-existing', work):
            return JSONResponse({'error': '同步正在进行中，请稍后再试'}, 409)
        return {'status': 'started', 'dry_run': dry_run}

    @app.get('/api/merge-existing/preview')
    async def merge_preview():
        """How many albums still have loose episodes waiting to be merged."""
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]
        try:
            from ..merge_existing import preview_library
            return preview_library(archiver.root)
        except Exception as exc:
            return JSONResponse({'error': str(exc)}, 500)

    @app.get('/api/logs')
    async def get_logs(n: int = Query(200, ge=1, le=2000)):
        if not _log_buffer:
            return []
        return _log_buffer.recent(n)

    @app.get('/api/settings')
    async def settings():
        """Read-only runtime settings for the WebUI footer/settings panel."""
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]
        return {
            'base_url': archiver.client.base_url,
            'base_urls': archiver.client.base_urls,
            'media_root': str(archiver.root),
            'categories': [c['name'] for c in archiver.config['site']['categories']],
            'retries': archiver.retries,
            'minimum_duration': archiver.minimum_duration,
            'recheck_days': archiver.recheck_days,
            'failure_cooldown_hours': archiver.failure_cooldown_hours,
            'episode_timeout': archiver.episode_timeout,
            'merge': archiver._merge_config(),
        }

    @app.get('/api/sync-status')
    async def sync_status():
        return _sync_status

    @app.websocket('/ws/logs')
    async def ws_logs(websocket: WebSocket):
        if token and websocket.query_params.get('token', '') != token:
            await websocket.close(code=1008)
            return
        if not _log_buffer:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        loop, q = _log_buffer.subscribe()
        # Push whatever is already buffered so a fresh page is not blank until
        # the next log line happens to arrive.
        for entry in _log_buffer.recent(200):
            await websocket.send_json(entry)
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Keepalive: the client reconnects if this ever fails.
                    await websocket.send_json({'level': 'PING', 'message': '',
                                               'timestamp': 0, 'keepalive': True})
                    continue
                await websocket.send_json(entry)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            LOG.debug('log websocket closed unexpectedly', exc_info=True)
        finally:
            _log_buffer.unsubscribe(loop, q)

    return app
