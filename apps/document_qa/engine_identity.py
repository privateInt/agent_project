"""n8n version evidence; the local Docker reader is a deployment adapter only."""
import json
import os
import re
import subprocess
import time
from urllib.parse import urlsplit

from .intake import check, UploadError

ENGINE_VERSION = '2.40.5'
IMAGE_DIGEST = 'sha256:9f693fd5565539efd5e75ad168526c8041a6af516d9e50bc4d9cb1c9c5031523'
IMAGE_REF = 'n8nio/n8n:' + ENGINE_VERSION + '@' + IMAGE_DIGEST
REPO_DIGEST = 'n8nio/n8n@' + IMAGE_DIGEST
IMAGE_REFERENCES = (IMAGE_REF, 'n8nio/n8n:' + ENGINE_VERSION,
                    'docker.io/' + IMAGE_REF, 'docker.io/n8nio/n8n:' + ENGINE_VERSION)
REPO_DIGESTS = (REPO_DIGEST, 'docker.io/' + REPO_DIGEST)
CONTAINER_FORMAT = ('{"running":{{json .State.Running}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"image_ref":{{json .Config.Image}},"image_id":{{json .Image}},'
    '"ports":{{json .NetworkSettings.Ports}}}')
IMAGE_FORMAT = '{"id":{{json .Id}},"repo_digests":{{json .RepoDigests}}}'


class MetadataCommands:
    def __init__(self):
        self.deadline = time.monotonic() + 20
        self.calls = 0

    def __call__(self, args):
        remaining = self.deadline - time.monotonic()
        check(remaining > 0 and self.calls < 4, 'ENGINE_VERSION_UNVERIFIED', 409)
        self.calls += 1
        result = subprocess.run(args, capture_output=True, timeout=min(5, remaining))
        check(result.returncode == 0 and len(result.stdout) <= 65536,
              'ENGINE_VERSION_UNVERIFIED', 409)
        return result.stdout


def local_image_identity(origin, run=None, env=None):
    """Read only the owned n8n container/image on the API's local published port."""
    try:
        url = urlsplit(origin)
        check(url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')
              and not url.username and not url.password and url.path in ('', '/')
              and not url.query and not url.fragment, 'ENGINE_VERSION_UNVERIFIED', 409)
        port = str(url.port or 80)
        run = run or MetadataCommands()
        env = os.environ if env is None else env
        override, context = env.get('DOCKER_HOST'), env.get('DOCKER_CONTEXT')
        check(not (override and context), 'ENGINE_VERSION_UNVERIFIED', 409)
        endpoint = override or json.loads(run(['docker', 'context', 'inspect', '--format',
                                               '{{json .Endpoints.docker.Host}}']))
        check(isinstance(endpoint, str) and endpoint.startswith('unix:///'),
              'ENGINE_VERSION_UNVERIFIED', 409)
        raw = run(['docker', 'ps', '-q', '--no-trunc', '--filter',
                   'label=com.docker.compose.project=infrastructure', '--filter',
                   'label=com.docker.compose.service=n8n'])
        ids = (raw.decode() if isinstance(raw, bytes) else raw).split()
        check(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{64}', ids[0]),
              'ENGINE_VERSION_UNVERIFIED', 409)
        container = json.loads(run(['docker', 'inspect', '--type=container', '--format',
                                    CONTAINER_FORMAT, ids[0]]))
        check(container.get('running') is True and container.get('project') == 'infrastructure'
              and container.get('service') == 'n8n' and container.get('image_ref') in IMAGE_REFERENCES,
              'ENGINE_VERSION_UNVERIFIED', 409)
        image_id = container.get('image_id')
        check(isinstance(image_id, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', image_id),
              'ENGINE_VERSION_UNVERIFIED', 409)
        ports = container.get('ports')
        check(isinstance(ports, dict) and isinstance(ports.get('5678/tcp'), list),
              'ENGINE_VERSION_UNVERIFIED', 409)
        bindings = ports['5678/tcp']
        allowed = ('::', '::1') if url.hostname == '::1' else ('0.0.0.0', '127.0.0.1')
        check(any(isinstance(row, dict) and row.get('HostPort') == port
                  and row.get('HostIp') in allowed for row in bindings),
              'ENGINE_VERSION_UNVERIFIED', 409)
        image = json.loads(run(['docker', 'image', 'inspect', '--format', IMAGE_FORMAT, image_id]))
        check(image.get('id') == image_id and isinstance(image.get('repo_digests'), list)
              and any(value in image['repo_digests'] for value in REPO_DIGESTS),
              'ENGINE_VERSION_UNVERIFIED', 409)
        return {'version': ENGINE_VERSION, 'source': 'owned_local_pinned_image',
                'origin': origin, 'image_digest': IMAGE_DIGEST}
    except UploadError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError):
        # Docker stderr and HTTP response bodies never become public diagnostics.
        raise UploadError('ENGINE_VERSION_UNVERIFIED', 409) from None


def verify_engine_version(api, identity_reader=None):
    settings = api.request('GET', '/rest/settings')
    check(isinstance(settings, dict) and isinstance(settings.get('data'), dict),
          'ENGINE_VERSION_UNVERIFIED', 409)
    data = settings['data']
    if 'versionCli' in data:
        # A reported incompatible/invalid version cannot use the fallback.
        check(data['versionCli'] == ENGINE_VERSION, 'ENGINE_VERSION_UNVERIFIED', 409)
        return {'version': ENGINE_VERSION, 'source': 'settings_versionCli', 'origin': api.origin}
    reader = identity_reader or getattr(api, 'identity_reader', None) or local_image_identity
    value = reader(api.origin)
    check(isinstance(value, dict) and value.get('version') == ENGINE_VERSION
          and value.get('origin') == api.origin and value.get('image_digest') == IMAGE_DIGEST
          and value.get('source') == 'owned_local_pinned_image', 'ENGINE_VERSION_UNVERIFIED', 409)
    return value
