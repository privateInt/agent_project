"""Offline Docker-save/OCI image verification; never load, extract or run layers.

Image IDs bind the config bytes. Each config diff_id binds an uncompressed
layer tar. OCI descriptor digests additionally bind the exported blobs.
Unsupported compression/media/layout is refused rather than skipped.
"""
import hashlib
import gzip
import json
import re
import tarfile

from ops_candidates.backup_capture_20260930 import capture as c, age_tool as a

require = a.require
DIGEST = re.compile('sha256:[0-9a-f]{64}')
CONFIG = {'application/vnd.oci.image.config.v1+json', 'application/vnd.docker.container.image.v1+json'}
MANIFEST = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
LAYER = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.docker.image.rootfs.diff.tar',
         'application/vnd.oci.image.layer.v1.tar+gzip', 'application/vnd.docker.image.rootfs.diff.tar.gzip'}


class BoundedInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # Reject oversized extension payloads before tarfile reads them into
        # memory. This bound applies to PAX and GNU long-name records too.
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                         tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            require(0 <= self.size <= 65536, 'RESTORE_TAR_EXTENSION_LIMIT')
        require(self.type != tarfile.GNUTYPE_SPARSE, 'RESTORE_TAR_SPARSE_REFUSED')
        return super()._proc_member(archive)


def json_value(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'RESTORE_JSON_DUPLICATE_KEY')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def verify(path, image_id, *, max_bytes, max_entries, guard=lambda: None):
    require(isinstance(image_id, str) and DIGEST.fullmatch(image_id), 'RESTORE_IMAGE_ID_INVALID')
    require(path.stat().st_size <= max_bytes, 'RESTORE_IMAGE_BUDGET')
    rows = {}; small = {}; total = 0
    # Read sequentially with bounded member count and plaintext bytes. Never
    # follow links or extract an image's rootfs onto the host.
    with tarfile.open(path, mode='r:', tarinfo=BoundedInfo) as archive:
        for member in archive:
            guard(); name = c.safe_name(member.name.rstrip('/') if member.isdir() else member.name)
            require(name not in rows and len(rows) < max_entries, 'RESTORE_IMAGE_DUPLICATE_OR_LIMIT')
            require((member.isfile() or member.isdir()) and not member.linkname and not member.issparse()
                    and not member.mode & 0o7000 and (not member.pax_headers or
                    set(member.pax_headers) <= {'path', 'mtime', 'size', 'uid', 'gid'}), 'RESTORE_IMAGE_MEMBER_REFUSED')
            require(type(member.size) is int and 0 <= member.size <= max_bytes-total,
                    'RESTORE_IMAGE_BUDGET')
            if member.isdir():
                require(member.size == 0, 'RESTORE_IMAGE_DIRECTORY_SIZE')
                rows[name] = {'directory': True}; continue
            total += member.size; digest = hashlib.sha256(); chunks = []
            stream = archive.extractfile(member); remaining = member.size
            while remaining:
                guard(); data = stream.read(min(65536, remaining))
                require(data, 'RESTORE_IMAGE_TRUNCATED'); remaining -= len(data); digest.update(data)
                if member.size <= 2**20: chunks.append(data)
            rows[name] = {'size': member.size, 'digest': 'sha256:'+digest.hexdigest()}
            if member.size <= 2**20: small[name] = b''.join(chunks)
            if name.startswith('blobs/'):
                parts = name.split('/')
                require(len(parts) == 3 and parts[1] == 'sha256' and re.fullmatch('[0-9a-f]{64}', parts[2])
                        and rows[name]['digest'] == 'sha256:'+parts[2], 'RESTORE_IMAGE_BLOB_DIGEST')
        # A tar terminator is insufficient if another archive/nonzero payload
        # follows it. Inspect the remaining padding without ignoring content.
        archive.fileobj.seek(archive.offset)
        while True:
            guard(); data = archive.fileobj.read(65536)
            if not data: break
            require(not any(data), 'RESTORE_IMAGE_TRAILING_DATA')

    def document(name):
        c.safe_name(name)
        require(name in small and name in rows and 'digest' in rows[name], 'RESTORE_IMAGE_JSON_MISSING_OR_LIMIT')
        return json_value(small[name])

    def descriptor(value, types):
        require(isinstance(value, dict) and value.get('mediaType') in types and DIGEST.fullmatch(value.get('digest', ''))
                and type(value.get('size')) is int and value['size'] >= 0, 'RESTORE_IMAGE_DESCRIPTOR')
        name = 'blobs/sha256/'+value['digest'][7:]
        require(name in rows and rows[name] == {'size': value['size'], 'digest': value['digest']},
                'RESTORE_IMAGE_DESCRIPTOR_MISMATCH')
        return name

    classic = None; oci = None; encodings = {}
    if 'manifest.json' in rows:
        value = document('manifest.json')
        require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict), 'RESTORE_IMAGE_SINGLE_CONFIG_REQUIRED')
        item = value[0]; config = item.get('Config'); layers = item.get('Layers')
        require(isinstance(config, str) and isinstance(layers, list) and len(layers) <= max_entries
                and all(isinstance(x, str) for x in layers), 'RESTORE_IMAGE_MANIFEST')
        c.safe_name(config)
        require(config in rows and rows[config].get('digest') == image_id, 'RESTORE_IMAGE_CONFIG_ID')
        classic = (config, layers)
    if 'index.json' in rows or 'oci-layout' in rows:
        layout = document('oci-layout'); index = document('index.json')
        require(layout == {'imageLayoutVersion': '1.0.0'} and isinstance(index, dict) and index.get('schemaVersion') == 2
                and isinstance(index.get('manifests'), list) and 1 <= len(index['manifests']) <= max_entries,
                'RESTORE_IMAGE_OCI_LAYOUT')
        found = []
        for value in index['manifests']:
            manifest = document(descriptor(value, MANIFEST))
            require(isinstance(manifest, dict) and manifest.get('schemaVersion') == 2
                    and isinstance(manifest.get('layers'), list) and len(manifest['layers']) <= max_entries,
                    'RESTORE_IMAGE_OCI_MANIFEST')
            config = descriptor(manifest.get('config'), CONFIG)
            require(rows[config]['digest'] == image_id, 'RESTORE_IMAGE_CONFIG_ID')
            layers = [descriptor(layer, LAYER) for layer in manifest['layers']]
            for name, layer in zip(layers, manifest['layers']):
                encoding = 'gzip' if layer['mediaType'].endswith(('+gzip', '.gzip')) else 'tar'
                require(name not in encodings or encodings[name] == encoding, 'RESTORE_IMAGE_ENCODING_DISAGREES')
                encodings[name] = encoding
            found.append((config, layers))
        require(all(x == found[0] for x in found), 'RESTORE_IMAGE_MULTIPLE_VARIANTS_REFUSED')
        oci = found[0]
    require(classic is not None or oci is not None, 'RESTORE_IMAGE_LAYOUT_UNSUPPORTED')
    if classic is not None and oci is not None:
        require(classic == oci, 'RESTORE_IMAGE_LAYOUT_DISAGREES')
    config, layers = classic or oci
    value = document(config)
    require(isinstance(value, dict) and isinstance(value.get('rootfs'), dict) and value['rootfs'].get('type') == 'layers'
            and isinstance(value['rootfs'].get('diff_ids'), list) and isinstance(value.get('architecture'), str)
            and value['architecture'] and isinstance(value.get('os'), str) and value['os'], 'RESTORE_IMAGE_CONFIG_INVALID')
    diffs = value['rootfs']['diff_ids']
    require(len(diffs) == len(layers) and all(isinstance(x, str) and DIGEST.fullmatch(x) for x in diffs), 'RESTORE_IMAGE_DIFF_IDS')
    expanded = 0
    class LayerReader:
        def __init__(self, stream): self.stream = stream; self.digest = hashlib.sha256()
        def read(self, size):
            nonlocal expanded
            guard(); require(0 <= size <= 2**20, 'RESTORE_IMAGE_READ_LIMIT')
            data = self.stream.read(size); expanded += len(data)
            require(expanded <= max_bytes, 'RESTORE_IMAGE_EXPANSION_BUDGET')
            self.digest.update(data); return data
    for layer, expected in zip(layers, diffs):
        c.safe_name(layer)
        require(layer in rows and 'digest' in rows[layer], 'RESTORE_IMAGE_LAYER_MISSING')
        # Verify diff_id over uncompressed bytes, with bounded gzip expansion.
        # OCI descriptor hashes were verified over compressed blob bytes above.
        with tarfile.open(path, mode='r:', tarinfo=BoundedInfo) as outer:
            member = outer.getmember(layer); stream = outer.extractfile(member)
            encoding = encodings.get(layer)
            if encoding is None:
                encoding = 'gzip' if stream.read(2) == b'\x1f\x8b' else 'tar'; stream.seek(0)
            decoded = gzip.GzipFile(fileobj=stream, mode='rb') if encoding == 'gzip' else stream
            reader = LayerReader(decoded)
            try:
                with tarfile.open(fileobj=reader, mode='r|', tarinfo=BoundedInfo) as inner:
                    count = 0
                    for entry in inner:
                        guard(); count += 1
                        require(count <= max_entries, 'RESTORE_IMAGE_LAYER_ENTRY_LIMIT')
                        # Links/whiteouts remain archive bytes, never host paths.
                    while True:
                        data = inner.fileobj.read(65536)
                        if not data: break
                        require(not any(data), 'RESTORE_IMAGE_LAYER_TRAILING_DATA')
                require(not reader.read(1), 'RESTORE_IMAGE_LAYER_EOF')
                require(reader.digest.hexdigest() == expected[7:], 'RESTORE_IMAGE_LAYER_DIGEST')
            except (gzip.BadGzipFile, EOFError):
                raise a.Failure('RESTORE_IMAGE_LAYER_ENCODING') from None
            finally: decoded.close()
    return {'image_id': image_id, 'layout': 'docker_save_and_oci' if classic and oci else 'docker_save' if classic else 'oci',
            'architecture': value['architecture'], 'os': value['os'],
            'config_digest_verified': True, 'uncompressed_layer_digests_verified': True,
            'layers': len(layers), 'image_imported': False, 'application_started': False}
