"""Real native age, generation pipes and isolated filesystem; no daemon/API."""
from copy import deepcopy
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import unittest
from unittest.mock import patch

from src.ops.candidates.backup_generation_20261001.tests.test_generation import NativeAgeTests
from src.ops.candidates.backup_generation_20261001 import generation as g
from src.ops.candidates.backup_restore_20261001 import restore as r, image_archive as im
from src.ops.candidates.backup_capture_20260930 import capture as c, age_tool as a


def packed(items):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, raw in items:
            item = tarfile.TarInfo(name); item.size = len(raw); item.mode = 0o600
            archive.addfile(item, io.BytesIO(raw))
    return out.getvalue()


def image_fixture(oci=False, compressed=False):
    layer = packed([('synthetic-record', b'fixture image file')]); diff = 'sha256:'+hashlib.sha256(layer).hexdigest()
    config = json.dumps({'architecture':'amd64','os':'linux','rootfs':{'type':'layers','diff_ids':[diff]}}).encode()
    ident = 'sha256:'+hashlib.sha256(config).hexdigest()
    if not oci:
        return ident, packed([(ident[7:]+'.json', config), ('layer/layer.tar', layer),
            ('manifest.json', json.dumps([{'Config':ident[7:]+'.json','RepoTags':None,'Layers':['layer/layer.tar']}]).encode())])
    blob=gzip.compress(layer,mtime=0) if compressed else layer
    blob_digest='sha256:'+hashlib.sha256(blob).hexdigest()
    manifest = json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json',
        'config':{'mediaType':'application/vnd.oci.image.config.v1+json','size':len(config),'digest':ident},
        'layers':[{'mediaType':'application/vnd.oci.image.layer.v1.tar'+('+gzip' if compressed else ''),'size':len(blob),'digest':blob_digest}]}).encode()
    digest = 'sha256:'+hashlib.sha256(manifest).hexdigest()
    index = json.dumps({'schemaVersion':2,'manifests':[{'mediaType':'application/vnd.oci.image.manifest.v1+json',
        'size':len(manifest),'digest':digest}]}).encode()
    return ident, packed([('oci-layout',b'{"imageLayoutVersion":"1.0.0"}'),('index.json',index),
        ('blobs/sha256/'+ident[7:],config),('blobs/sha256/'+blob_digest[7:],blob),('blobs/sha256/'+digest[7:],manifest)])


class Exporter:
    def __init__(self, ident, raw): self.ident, self.raw, self.children = ident, raw, []
    def inspect(self, ident):
        assert ident == self.ident
        return [ident,'amd64',len(self.raw)]
    def start(self, ident, scope, guard):
        guard(); assert ident == self.ident
        process = subprocess.Popen([sys.executable,'-I','-B','-c',
            'import base64,sys;sys.stdout.buffer.write(base64.b64decode(sys.argv[1]))',base64.b64encode(self.raw).decode()],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,start_new_session=True,
            pass_fds=(scope.descriptor,))
        self.children.append(process); return process


class RestoreTests(NativeAgeTests):
    fail = unittest.TestCase.fail
    def setUp(self):
        super().setUp()
        self.ident, self.archive = image_fixture()
        for row in self.rows.values(): row['image_id'] = self.ident
        self.helper['image_id'] = self.ident; self.images = [self.ident]
        self.proposal = self.plan(); self.backend.rows = deepcopy(self.rows)
        self.exporter = Exporter(self.ident, self.archive)
        stamp = 1760000000123456789
        os.utime(self.config/'settings', ns=(stamp,stamp)); os.utime(self.src/'record',ns=(stamp,stamp))
        self.gen = self.make(); self.gen.test_codec = False; self.gen(self.scope)
        self.trusted = self.receipt(self.gen)

    def restore_plan(self, **kwargs):
        return r.plan(self.gen.folder,self.trusted,self.target,self.identity,**kwargs)

    def restore(self, **kwargs):
        self.restore_op = r.Restore(self.restore_plan(**kwargs),self.codec)
        return self.restore_op.run()

    def fail_restore(self, code=None, **kwargs):
        with self.assertRaises((a.Failure,tarfile.TarError,ValueError,OSError)) as caught: self.restore(**kwargs)
        if code: self.assertEqual(getattr(caught.exception,'code',None),code)
        self.assertFalse((self.target/('restored-'+self.gen.p['id'])).exists())
        return caught.exception

    def decrypt(self, role):
        return subprocess.check_output([str(self.codec.path),'-d','-i',str(self.identity),
            str(self.gen.folder/self.trusted['artifacts'][role]['path'])],stderr=subprocess.DEVNULL,timeout=10)

    def encrypt(self, path, raw):
        result = subprocess.run([str(self.codec.path),'-e','-r',self.recipient],input=raw,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=10)
        path.write_bytes(result.stdout); path.chmod(0o600)
        return {'ciphertext_sha256':a.sha(path),'ciphertext_bytes':path.stat().st_size}

    def alter(self, role, raw, detail=None, catalog_edit=None):
        catalog = json.loads(self.decrypt('catalog')); record = json.loads((self.gen.folder/'generation.json').read_text())
        if role:
            row = self.trusted['artifacts'][role]; row.update(self.encrypt(self.gen.folder/row['path'],raw))
            catalog['artifacts'][role].update(row); record['artifacts'][role].update(row)
            if detail is not None: catalog['artifacts'][role]['detail'] = detail; record['artifacts'][role]['detail'] = detail
        if catalog_edit: catalog_edit(catalog)
        raw = json.dumps(catalog,sort_keys=True).encode(); row = self.trusted['artifacts']['catalog']
        row.update(self.encrypt(self.gen.folder/row['path'],raw)); record['artifacts']['catalog'].update(row)
        record['artifacts']['catalog']['detail']['sha256'] = hashlib.sha256(raw).hexdigest()
        record['receipt_sha256'] = c.fingerprint(self.trusted)
        for name,value in [('receipt.json',self.trusted),('generation.json',record)]:
            path = self.gen.folder/name; path.write_text(json.dumps(value)); path.chmod(0o600)

    def altered_tar(self, change):
        role = 'files'; raw = self.decrypt(role); out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(fileobj=out,mode='w',format=tarfile.PAX_FORMAT) as target:
            for member in source:
                data = source.extractfile(member).read() if member.isfile() else None
                member,data = change(member,data)
                if member is not None: target.addfile(member,io.BytesIO(data) if data is not None else None)
        return out.getvalue()

    def test_complete_generation_materializes_files_cold_store_and_image_archive(self):
        before = {str(p.relative_to(self.gen.folder)):p.read_bytes() for p in self.gen.folder.rglob('*') if p.is_file()}
        result = self.restore(); folder = Path(result['directory'])
        self.assertEqual((folder/'files/data/config/settings').read_bytes(),b'synthetic-secret')
        self.assertEqual((folder/'files/data/external').read_bytes(),b'external-reference')
        self.assertEqual((folder/'storage-data/data/data/record').read_bytes(),(self.src/'record').read_bytes())
        self.assertEqual((folder/'files/data/config/settings').stat().st_mtime_ns,1760000000123456789)
        self.assertEqual((folder/'storage-data/data/data/record').stat().st_mode & 0o777,0o640)
        self.assertEqual(result['image_archives'][0]['image_id'],self.ident)
        self.assertTrue(result['crypto_eof_authenticated']); self.assertFalse(result['backup_ready'])
        self.assertFalse(result['image_import_restore_acceptance']); self.assertFalse(result['application_restore_acceptance'])
        self.assertEqual(before,{str(p.relative_to(self.gen.folder)):p.read_bytes() for p in self.gen.folder.rglob('*') if p.is_file()})
        self.assertFalse(self.backend.helpers)

    def test_original_sources_can_be_missing_and_are_not_read(self):
        shutil.rmtree(self.config); shutil.rmtree(self.src); self.external.unlink()
        self.assertTrue(self.restore()['filesystem_restore_performed'])

    def test_plan_reads_only_key_metadata_and_creates_nothing(self):
        before = list(self.target.iterdir()); read = Path.read_bytes
        def read_bytes(path):
            if path == self.identity: raise AssertionError('identity body read')
            return read(path)
        with patch.object(Path,'read_bytes',read_bytes): self.restore_plan()
        self.assertEqual(list(self.target.iterdir()),before)

    def test_no_implicit_identity_or_test_codec(self):
        op = r.Restore(self.restore_plan(),object())
        with self.assertRaisesRegex(a.Failure,'RESTORE_NATIVE_AGE_REQUIRED'): op.run()
        self.assertFalse(list(self.target.iterdir()))

    def test_untrusted_receipt_rejected_before_decryption(self):
        self.trusted['review_id'] = '0'*64
        with self.assertRaisesRegex(a.Failure,'RESTORE_TRUSTED_RECEIPT_REQUIRED'): self.restore_plan()
        self.assertFalse(list(self.target.iterdir()))

    def test_changed_review_rejected_without_stage(self):
        value = self.restore_plan(); value['max_bytes'] += 1
        with self.assertRaisesRegex(a.Failure,'RESTORE_REVIEW_CHANGED'): r.Restore(value,self.codec).run()
        self.assertFalse(list(self.target.iterdir()))

    def test_ciphertext_replacement_rejected_without_publication(self):
        path = self.gen.folder/'files.age'; path.write_bytes(path.read_bytes()[:-8]+b'badbytes')
        self.fail_restore('RESTORE_CIPHERTEXT_CHANGED')

    def test_truncated_authenticated_ciphertext_cannot_pass_even_with_updated_public_hash(self):
        path = self.gen.folder/'files.age'; raw = path.read_bytes()[:-8]; path.write_bytes(raw)
        self.trusted['artifacts']['files'].update(ciphertext_sha256=a.sha(path),ciphertext_bytes=len(raw))
        record = json.loads((self.gen.folder/'generation.json').read_text()); record['artifacts']['files'].update(self.trusted['artifacts']['files'])
        catalog = json.loads(self.decrypt('catalog')); catalog['artifacts']['files'].update(self.trusted['artifacts']['files'])
        self.alter(None,None,catalog_edit=lambda value:value.update(artifacts=catalog['artifacts']))
        # alter synchronizes other rows; synchronize this trusted artifact too.
        record = json.loads((self.gen.folder/'generation.json').read_text()); record['artifacts']['files'].update(self.trusted['artifacts']['files'])
        (self.gen.folder/'generation.json').write_text(json.dumps(record))
        self.fail_restore()

    def test_catalog_missing_component_is_refused(self):
        self.alter(None,None,catalog_edit=lambda value:value['artifacts'].pop('files'))
        self.fail_restore('RESTORE_CATALOG_ARTIFACT_SET')

    def test_catalog_review_digest_cannot_be_changed(self):
        self.alter(None,None,catalog_edit=lambda value:value['proposal'].update(max_entries=1))
        self.fail_restore('RESTORE_CATALOG_REVIEW')

    def test_duplicate_json_key_is_refused(self):
        path = self.gen.folder/'receipt.json'; raw = path.read_text().rstrip(); path.write_text(raw[:-1]+',"schema":1}')
        self.fail_restore('RESTORE_JSON_DUPLICATE_KEY')

    def test_destination_overlap_and_key_inside_restore_are_refused(self):
        with self.assertRaises(a.Failure): r.plan(self.gen.folder,self.trusted,self.out,self.identity)
        key = self.target/'identity'; shutil.copyfile(self.identity,key); key.chmod(0o600)
        with self.assertRaises(a.Failure): r.plan(self.gen.folder,self.trusted,self.target,key)

    def test_original_source_destination_is_not_used(self):
        p = r.plan(self.gen.folder,self.trusted,self.config,self.identity)
        with self.assertRaisesRegex(a.Failure,'RESTORE_ORIGINAL_SOURCE_OVERLAP'): r.Restore(p,self.codec).run()
        self.assertEqual((self.config/'settings').read_bytes(),b'synthetic-secret')

    def test_symlink_hardlink_or_nonprivate_key_refused(self):
        for mode in ('symlink','hardlink','mode'):
            target = self.root/('key-'+mode)
            if mode=='symlink': target.symlink_to(self.identity)
            elif mode=='hardlink': os.link(self.identity,target)
            else: shutil.copyfile(self.identity,target); target.chmod(0o644)
            with self.assertRaises(a.Failure): r.plan(self.gen.folder,self.trusted,self.target,target)
            target.unlink()

    def test_existing_partial_or_complete_destination_never_overwritten(self):
        self.restore()
        with self.assertRaisesRegex(a.Failure,'RESTORE_ALREADY_RECORDED'): self.restore()
        self.assertEqual(len(list(self.target.iterdir())),1)

    def test_byte_and_entry_budgets_prevent_partial_publication(self):
        self.fail_restore(max_bytes=100)
        self.fail_restore(max_entries=1)

    def test_path_traversal_and_link_entries_refused(self):
        def change(member,data):
            if member.name=='data/config/settings': member.name='data/config/../../escape'
            return member,data
        self.alter('files',self.altered_tar(change)); self.fail_restore('CAPTURE_NAME_INVALID')
        self.assertFalse((self.target/'escape').exists())

    def test_symlink_member_is_not_materialized(self):
        def change(member,data):
            if member.name=='data/config/settings': member.type=tarfile.SYMTYPE; member.linkname='/etc/passwd'; member.size=0; data=None
            return member,data
        self.alter('files',self.altered_tar(change)); self.fail_restore('RESTORE_MEMBER_TYPE_REFUSED')

    def test_foreign_uid_is_not_silently_mapped(self):
        def change(member,data):
            if member.name=='data/config/settings': member.uid=os.getuid()+10000
            return member,data
        self.alter('files',self.altered_tar(change)); self.fail_restore('RESTORE_FOREIGN_UID_OR_METADATA_UNSUPPORTED')

    def test_payload_hash_mismatch_refused(self):
        def change(member,data):
            if member.name=='data/config/settings': data=b'different-secret'; member.size=len(data)
            return member,data
        self.alter('files',self.altered_tar(change)); self.fail_restore('RESTORE_HASH_OR_METADATA_MISMATCH')

    def test_manifest_digest_mismatch_refused(self):
        def change(member,data):
            if member.name=='manifest.json': data=data.replace(b'"schema": 1',b'"schema": 2'); member.size=len(data)
            return member,data
        self.alter('files',self.altered_tar(change)); self.fail_restore('RESTORE_MANIFEST_DIGEST')

    def test_nonzero_trailing_tar_data_refused(self):
        self.alter('files',self.decrypt('files')+b'nonzero trailing payload'); self.fail_restore('RESTORE_TRAILING_DATA')

    def test_same_plaintext_image_size_with_wrong_export_hash_refused(self):
        role='image-'+self.ident[7:]; self.alter(role,b'X'+self.archive[1:]); self.fail_restore('RESTORE_IMAGE_EXPORT_HASH')

    def test_failure_keeps_only_private_partial_and_failure_audit(self):
        self.alter('files',b'invalid tar'); self.fail_restore()
        partial = next(self.target.iterdir()); self.assertTrue(partial.name.endswith('.partial'))
        self.assertEqual(partial.stat().st_mode & 0o777,0o700)
        self.assertEqual(json.loads((partial/'restore.json').read_text())['phase'],'attention_required')
        with self.assertRaisesRegex(a.Failure,'RESTORE_ALREADY_RECORDED'): self.restore()

    def test_deadline_stops_decrypt_child_and_prevents_publication(self):
        op = r.Restore(self.restore_plan(),self.codec)
        op.hook = lambda phase:setattr(op,'until',time.monotonic()-1) if phase=='prepared' else None
        with self.assertRaisesRegex(a.Failure,'RESTORE_DEADLINE'): op.run()
        self.assertIsNone(op.process); self.assertFalse(any(not p.name.endswith('.partial') for p in self.target.iterdir()))

    def test_key_change_during_restore_is_refused(self):
        op = r.Restore(self.restore_plan(),self.codec)
        op.hook = lambda phase:self.identity.write_text('changed') if phase=='prepared' else None
        with self.assertRaisesRegex(a.Failure,'RESTORE_IDENTITY_CHANGED'): op.run()

    def test_staged_file_edit_before_publication_is_refused(self):
        op=r.Restore(self.restore_plan(),self.codec)
        op.hook=lambda phase:(op.stage/'files/data/config/settings').write_bytes(b'changed staged file') if phase=='content_verified' else None
        with self.assertRaisesRegex(a.Failure,'RESTORE_STAGED_ENTRY_CHANGED'): op.run()
        self.assertFalse((self.target/('restored-'+self.gen.p['id'])).exists())

    def test_extra_staged_file_before_publication_is_refused(self):
        op=r.Restore(self.restore_plan(),self.codec)
        op.hook=lambda phase:(op.stage/'unreviewed').write_text('extra') if phase=='content_verified' else None
        with self.assertRaisesRegex(a.Failure,'RESTORE_STAGED_SET_CHANGED'): op.run()

    def test_oci_archive_generation_restore(self):
        ident,raw = image_fixture(True); self.assertEqual(ident,self.ident)
        role='image-'+ident[7:]; catalog=json.loads(self.decrypt('catalog')); detail=catalog['artifacts'][role]['detail']
        detail.update(export_bytes=len(raw),export_sha256=hashlib.sha256(raw).hexdigest())
        self.alter(role,raw,detail)
        self.assertEqual(self.restore()['image_archives'][0]['layout'],'oci')

    def test_image_architecture_metadata_mismatch_refused(self):
        role='image-'+self.ident[7:]; catalog=json.loads(self.decrypt('catalog')); detail=catalog['artifacts'][role]['detail']
        detail['metadata'][1]='arm64'; self.alter(role,self.archive,detail)
        self.fail_restore('RESTORE_IMAGE_ARCHITECTURE_MISMATCH')

    def test_restrictive_zero_mode_can_be_applied_after_verified_content(self):
        (self.config/'settings').chmod(0)
        if os.getuid()!=0:
            # Capture must be readable; test metadata application independently
            # by creating a valid authenticated manifest/header with mode 000.
            (self.config/'settings').chmod(0o600)
        def change(member,data):
            if member.name=='data/config/settings': member.mode=0
            if member.name=='manifest.json':
                value=json.loads(data)
                for row in value['entries']:
                    if row['path']=='data/config/settings': row['mode']=0
                data=json.dumps(value,sort_keys=True).encode(); member.size=len(data)
            return member,data
        raw=self.altered_tar(change); catalog=json.loads(self.decrypt('catalog')); detail=catalog['artifacts']['files']['detail']
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive: manifest=archive.extractfile('manifest.json').read()
        detail['manifest_sha256']=hashlib.sha256(manifest).hexdigest(); self.alter('files',raw,detail)
        result=self.restore(); target=Path(result['directory'])/'files/data/config/settings'
        self.assertEqual(target.stat().st_mode & 0o777,0); target.chmod(0o600)


class ImageTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.path=self.root/'image.tar'; self.ident,self.raw=image_fixture()
        self.path.write_bytes(self.raw)
    def verify(self, **kw): return im.verify(self.path,self.ident,max_bytes=2**20,max_entries=100,**kw)
    def test_classic_config_and_layer_hashes_verified_without_extraction(self):
        self.assertTrue(self.verify()['uncompressed_layer_digests_verified']); self.assertEqual(list(self.root.iterdir()),[self.path])
    def test_wrong_image_id_refused(self):
        with self.assertRaisesRegex(a.Failure,'RESTORE_IMAGE_CONFIG_ID'): im.verify(self.path,'sha256:'+'0'*64,max_bytes=2**20,max_entries=100)
    def test_changed_layer_bytes_refused(self):
        raw=self.raw.replace(b'fixture image file',b'changed image file'); self.path.write_bytes(raw)
        with self.assertRaisesRegex(a.Failure,'RESTORE_IMAGE_LAYER_DIGEST'): self.verify()
    def test_oci_blob_size_and_hash_refused(self):
        _,raw=image_fixture(True); self.path.write_bytes(raw.replace(b'fixture image file',b'changed image file'))
        with self.assertRaisesRegex(a.Failure,'RESTORE_IMAGE_BLOB_DIGEST'): self.verify()
    def test_duplicate_paths_links_and_unknown_layout_refused(self):
        for raw in (packed([('x',b'a'),('x',b'b')]),packed([('other',b'unknown')])):
            self.path.write_bytes(raw)
            with self.assertRaises(a.Failure): self.verify()
    def test_nonzero_trailing_bytes_and_limits_refused(self):
        self.path.write_bytes(self.raw+b'junk')
        with self.assertRaisesRegex(a.Failure,'RESTORE_IMAGE_TRAILING_DATA'): self.verify()
        with self.assertRaises(a.Failure): im.verify(self.path,self.ident,max_bytes=1,max_entries=100)
    def test_large_pax_extension_refused_before_payload_read(self):
        member=tarfile.TarInfo('oversized-metadata'); member.type=tarfile.XHDTYPE; member.size=2**30
        self.path.write_bytes(member.tobuf()+b'\0'*1024)
        with self.assertRaisesRegex(a.Failure,'RESTORE_TAR_EXTENSION_LIMIT'): self.verify()
    def test_gzip_OCI_blob_and_uncompressed_diff_id_are_both_verified(self):
        ident,raw=image_fixture(True,True); self.assertEqual(ident,self.ident); self.path.write_bytes(raw)
        self.assertTrue(self.verify()['uncompressed_layer_digests_verified'])

    def test_declared_gzip_with_uncompressed_blob_is_refused(self):
        _,raw=image_fixture(True)
        # Blob digest itself binds the manifest; rebuild it with compression
        # media instead of accepting hash-valid unsupported metadata.
        items=[]
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            values={m.name:archive.extractfile(m).read() for m in archive}
        index=json.loads(values['index.json']); old=index['manifests'][0]['digest'][7:]
        manifest=json.loads(values.pop('blobs/sha256/'+old)); manifest['layers'][0]['mediaType']='application/vnd.oci.image.layer.v1.tar+gzip'
        data=json.dumps(manifest).encode(); digest=hashlib.sha256(data).hexdigest(); values['blobs/sha256/'+digest]=data
        index['manifests'][0].update(digest='sha256:'+digest,size=len(data)); values['index.json']=json.dumps(index).encode()
        self.path.write_bytes(packed(list(values.items())))
        with self.assertRaisesRegex(a.Failure,'RESTORE_IMAGE_LAYER_ENCODING'): self.verify()


def suite():
    result=unittest.TestSuite()
    for cls in (RestoreTests,ImageTests):
        result.addTests(cls(name) for name in sorted(cls.__dict__) if name.startswith('test_'))
    return result
