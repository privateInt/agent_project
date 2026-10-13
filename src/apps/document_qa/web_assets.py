"""Fixed public screen assets; private state still requires the existing bearer key."""
import ipaddress
import os
import stat
from pathlib import Path

from .intake import check

WEB = Path(__file__).with_name('web')
ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/console': ('index.html', 'text/html; charset=utf-8'),
    '/console/': ('index.html', 'text/html; charset=utf-8'),
    '/console/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/console/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/console/upload.js': ('upload.js', 'text/javascript; charset=utf-8'),
    '/console/recovery.js': ('recovery.js', 'text/javascript; charset=utf-8'),
    '/console/questions.js': ('questions.js', 'text/javascript; charset=utf-8'),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def browser_origin(handler, require_host=False):
    """Do not serve a login page or accept browser calls through a rebound Host."""
    address, port = handler.server.server_address
    names = {address}
    if ipaddress.ip_address(address).is_loopback:
        names.add('localhost')
    hosts = {name + ':' + str(port) for name in names}
    if port == 80:
        hosts.update(names)
    values = handler.headers.get_all('Host', [])
    # Existing non-browser HTTP/1.0 callbacks may omit Host; the public page may not.
    check((not values and not require_host) or len(values) == 1 and values[0] in hosts,
          'HOST_NOT_ALLOWED', 403)
    origins = handler.headers.get_all('Origin', [])
    check(not origins or len(origins) == 1 and origins[0] in {'http://' + host for host in hosts},
          'ORIGIN_NOT_ALLOWED', 403)


def asset(path):
    filename, content_type = ASSETS[path]
    directory = os.open(WEB, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 256 << 10,
                  'SCREEN_ASSET_UNAVAILABLE', 503)
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                raw = stream.read((256 << 10) + 1)
            after = os.fstat(descriptor)
            current = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            signature = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            check(len(raw) == info.st_size and signature(info) == signature(after) == signature(current),
                  'SCREEN_ASSET_UNAVAILABLE', 503)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    return content_type, raw
