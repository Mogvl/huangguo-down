from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import logging
import random
import time


LOG = logging.getLogger(__name__)


class SiteRequestError(RuntimeError):
    """A site request failed, with the URL and status that caused it.

    urllib's bare ``HTTP Error 404: Not Found`` says nothing about which page
    broke, which makes a WebUI report like that impossible to act on.
    """

    def __init__(self, url: str, status: int | None, detail: str):
        self.url = url
        self.status = status
        super().__init__(f'{status or "ERR"} {detail} <- {url}')


class HuangguoClient:
    def __init__(
        self,
        base_url: str,
        request_interval: float = 2.0,
        timeout: int = 30,
        backup_urls: list[str] | None = None,
        attempts: int = 2,
    ):
        urls = [base_url, *(backup_urls or [])]
        self.base_urls = list(dict.fromkeys(url.rstrip('/') for url in urls if url))
        if not self.base_urls:
            raise ValueError('at least one site base URL is required')
        self.base_url = self.base_urls[0]
        self.request_interval = request_interval
        self.timeout = timeout
        self.last_request = 0.0
        self.user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36'
        self._backup_requests = 0
        # TLS resets and EOFs are usually transient (interference, flaky CDN
        # edges), so retry the same host before failing over to a mirror.
        self.attempts = max(1, int(attempts))

    def _throttle(self) -> None:
        wait = self.request_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.25, self.request_interval / 4)))

    def _candidates(self, is_absolute: bool) -> list[str | None]:
        if is_absolute:
            return [None]
        candidates = [self.base_url, *[url for url in self.base_urls if url != self.base_url]]
        if self.base_url != self.base_urls[0]:
            # After a failover, probe the primary host now and then so a
            # transient outage does not pin us to a backup forever.
            self._backup_requests += 1
            if self._backup_requests >= 20:
                self._backup_requests = 0
                candidates = [self.base_urls[0], self.base_url, *[url for url in self.base_urls if url not in (self.base_urls[0], self.base_url)]]
        return candidates

    def _request(self, method: str, path: str, *, data: bytes | None = None, headers: dict | None = None) -> bytes:
        self._throttle()
        is_absolute = path.startswith('http')
        candidates = self._candidates(is_absolute)
        last_error: Exception | None = None
        try:
            for index, candidate in enumerate(candidates):
                url = path if is_absolute else candidate + path
                referer = self.base_url + '/' if is_absolute else candidate + '/'
                request_headers = {'User-Agent': self.user_agent, 'Referer': referer}
                request_headers.update(headers or {})
                req = Request(url, method=method, data=data, headers=request_headers)
                try:
                    body = self._fetch_with_retry(req, url)
                    if candidate and candidate != self.base_url:
                        LOG.warning('site base URL switched to: %s', candidate)
                        self.base_url = candidate
                        self._backup_requests = 0
                    return body
                except HTTPError as error:
                    # A 404 means the page genuinely does not exist; trying the
                    # mirrors would just repeat it. Fail fast with the URL kept.
                    if error.code == 404:
                        raise SiteRequestError(url, 404, 'Not Found') from error
                    last_error = error
                    if is_absolute or index == len(candidates) - 1:
                        raise SiteRequestError(url, error.code, str(error.reason)) from error
                    LOG.warning('site request failed via %s; trying backup: %s', candidate, error)
                except (URLError, TimeoutError, ConnectionError, OSError) as error:
                    last_error = error
                    if is_absolute or index == len(candidates) - 1:
                        raise SiteRequestError(url, None, str(error)) from error
                    LOG.warning('site request failed via %s; trying backup: %s', candidate, error)
            raise last_error or RuntimeError('site request failed')
        finally:
            self.last_request = time.monotonic()

    def _fetch_with_retry(self, req: Request, url: str) -> bytes:
        """Retry a single URL a few times before giving up on this host.

        A TLS handshake that dies with UNEXPECTED_EOF is common on links that
        get interfered with; the very next attempt usually succeeds, so it is
        worth one or two immediate retries rather than switching mirrors.
        """
        import ssl
        transient = (ssl.SSLError, ssl.SSLEOFError, ConnectionResetError,
                     TimeoutError, ConnectionError, OSError)
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError:
                raise
            except transient as exc:
                last = exc
                if attempt >= self.attempts:
                    raise
                wait = 1.0 * attempt
                LOG.warning('attempt %d/%d failed for %s (%s); retrying in %.0fs',
                            attempt, self.attempts, url, type(exc).__name__, wait)
                time.sleep(wait)
        raise last or RuntimeError('request failed')

    def get_text(self, path: str) -> str:
        return self._request('GET', path).decode('utf-8', errors='replace')

    def get_bytes(self, url: str) -> bytes:
        return self._request('GET', url)

    def play_url(self, album_id: int, episode: int) -> str:
        raw = self._request('GET', f'/api/videos/{int(album_id)}/play?' + urlencode({'ep': int(episode)}))
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'play endpoint failed')
        url = ((payload.get('data') or {}).get('video_url') or '').strip()
        if not url:
            raise RuntimeError('empty video_url')
        return url

    def cover_url(self, album_id: int) -> str:
        body = json.dumps({'items': [{'type': 'video', 'id': int(album_id)}]}, ensure_ascii=False).encode()
        raw = self._request('POST', '/api/media/covers', data=body, headers={'Content-Type': 'application/json'})
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'cover endpoint failed')
        return (((payload.get('data') or {}).get('covers') or {}).get(f'video:{int(album_id)}') or '').strip()
