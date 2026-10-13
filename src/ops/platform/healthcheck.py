"""Local web-process liveness only; never query the upstream platforms."""
import http.client
import json
import os
import ssl
from pathlib import Path

config=json.loads(Path(os.environ['OPS_CONFIG']).read_text())
if config['tls_cert']:
    # Local liveness uses the configured certificate as trust, preserving hostname validation.
    import urllib.parse
    host=urllib.parse.urlsplit(config['public_url']).hostname
    c=http.client.HTTPSConnection(host,config['port'],timeout=3,context=ssl.create_default_context(cafile=config['tls_cert']))
else:
    c=http.client.HTTPConnection('127.0.0.1',config['port'],timeout=3)
try:
    c.request('GET','/healthz');r=c.getresponse();data=json.loads(r.read(4096))
    raise SystemExit(0 if r.status==200 and data.get('platform')=='ops' else 1)
finally:c.close()
