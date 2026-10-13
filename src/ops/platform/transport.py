"""Bounded GET-only JSON transport. No proxies, redirects, cookies or retries."""
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import stat
import time
import urllib.parse

from .contracts import ReadFailure, require


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReadFailure('INVALID_RESPONSE')))
    except (ValueError, UnicodeError, RecursionError):
        raise ReadFailure('INVALID_RESPONSE') from None


def base_url(value):
    require(isinstance(value, str) and len(value) <= 512, 'INVALID_CONFIG')
    require(not any(ord(c) <= 32 or ord(c) == 127 for c in value), 'INVALID_CONFIG')
    try:
        url = urllib.parse.urlsplit(value)
        require(url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
                and not url.query and not url.fragment and url.path in ('', '/'), 'INVALID_CONFIG')
        _ = url.port
    except ValueError:
        raise ReadFailure('INVALID_CONFIG') from None
    return url


def secret(path):
    try:
        p = Path(path).expanduser()
        info = p.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 4096, 'INVALID_CREDENTIAL_FILE')
        token = p.read_text().strip()
        require(bool(token) and len(token) <= 4096 and all(33 <= ord(c) <= 126 for c in token), 'INVALID_CREDENTIAL_FILE')
        return token
    except (OSError, UnicodeError):
        raise ReadFailure('CREDENTIAL_UNAVAILABLE') from None


class Transport:
    root_type = dict
    def __init__(self, url, token_file=None, timeout=5, max_bytes=4*1024*1024):
        self.url = base_url(url)
        require(type(timeout) in (int, float) and 0.1 <= timeout <= 30, 'INVALID_CONFIG')
        self.timeout, self.max_bytes, self.token_file = timeout, max_bytes, token_file
        if token_file and self.url.scheme == 'http':
            try:
                loopback = ipaddress.ip_address(self.url.hostname).is_loopback
            except ValueError:
                loopback = self.url.hostname == 'localhost'
            require(loopback, 'HTTPS_REQUIRED_FOR_REMOTE_CREDENTIAL')

    def headers(self, path):
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity', 'Connection': 'close'}
        if self.token_file and path.startswith('/api/'):
            headers['Authorization'] = 'Bearer '+secret(self.token_file)
        return headers

    def get(self, path, allow_unready=False):
        require(path.startswith('/') and not path.startswith('//') and '#' not in path
                and all(33 <= ord(c) <= 126 for c in path), 'INVALID_PATH')
        headers = self.headers(path)
        cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        connection = cls(self.url.hostname, self.url.port, timeout=self.timeout)
        deadline = time.monotonic()+self.timeout
        try:
            connection.connect()
            sock = connection.sock
            sock.settimeout(max(0.001, deadline-time.monotonic()))
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            code = response.status
            if code != 200 and not (allow_unready and code == 503):
                mapped = {401:'UNAUTHENTICATED',403:'FORBIDDEN',404:'NOT_FOUND',405:'METHOD_UNAVAILABLE',429:'RATE_LIMITED'}
                raise ReadFailure('REDIRECT_REFUSED' if 300 <= code < 400 else mapped.get(code, 'UPSTREAM_HTTP_ERROR'), code)
            require(response.getheader('Content-Encoding', 'identity') == 'identity', 'ENCODING_UNSUPPORTED')
            require(response.getheader('Content-Type', '').split(';')[0].strip() == 'application/json', 'INVALID_CONTENT_TYPE')
            length = response.getheader('Content-Length')
            if length is not None:
                require(0 < len(length) <= 10 and all('0' <= c <= '9' for c in length), 'INVALID_RESPONSE')
                length = int(length)
                require(length <= self.max_bytes, 'RESPONSE_TOO_LARGE')
            chunks, size = [], 0
            while True:
                remaining = deadline-time.monotonic()
                require(remaining > 0, 'TIMEOUT')
                sock.settimeout(remaining)
                chunk = response.read1(min(65536, self.max_bytes+1-size))
                if not chunk:
                    break
                size += len(chunk)
                require(size <= self.max_bytes, 'RESPONSE_TOO_LARGE')
                chunks.append(chunk)
                # read1 closes a Content-Length response as soon as its last byte arrives.
                if response.isclosed():
                    break
            require(length is None or size == length, 'INCOMPLETE_RESPONSE')
            value = strict_json(b''.join(chunks))
            require(isinstance(value, self.root_type))
            return code, value
        except (TimeoutError, socket.timeout):
            raise ReadFailure('TIMEOUT') from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise ReadFailure('CONNECTION_FAILED') from None
        finally:
            connection.close()
