"""Full synthetic generation with actual reader/export/crypto child pipes.

No daemon or production key/state/API access. Native age tests use a fresh
temporary identity. Image test producer is synthetic, never Docker acceptance.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import unittest
from unittest.mock import patch

from src.ops.candidates.backup_generation_20261001 import generation as g
from src.ops.candidates.cold_storage_20260930.tests.test_storage import Files
from src.ops.candidates.backup_capture_20260930 import age_tool as a


class Exporter:
    def __init__(self): self.calls = []; self.mode = 'good'; self.changed = False; self.children = []
    def inspect(self, ident): return [ident, 'synthetic', 123 if not self.changed else 124]
    def start(self, ident, scope, guard):
        guard(); self.calls.append(ident)
        code = 'import os,sys,time\n'
        if self.mode == 'hang': code += 'time.sleep(30)\n'
        elif self.mode == 'fail': code += 'os.write(1,b"partial-image");sys.exit(2)\n'
        elif self.mode == 'empty': code += 'pass\n'
        else: code += 'os.write(1,b"synthetic-image-export:"+sys.argv[1].encode())\n'
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, ident], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, pass_fds=(scope.descriptor,), start_new_session=True)
        self.children.append(process); return process


class GenerationTests(Files):
    def setUp(self):
        super().setUp()
        self.config = self.root/'config'; self.config.mkdir(mode=0o700)
        (self.config/'settings').write_bytes(b'synthetic-secret'); (self.config/'settings').chmod(0o600)
        self.external = self.root/'external'; self.external.write_bytes(b'external-reference'); self.external.chmod(0o600)
        self.exporter = Exporter(); self.addCleanup(self.close_exports)
        self.files = {'config': {'path': str(self.config), 'exclude_children': []},
                      'external': {'path': str(self.external), 'exclude_children': []}}
        self.images = [self.helper['image_id'], 'sha256:'+'4'*64]

    def close_exports(self):
        for process in self.exporter.children: a.Age.stop(process)

    def make(self, **kwargs):
        p = g.plan(self.proposal, self.files, self.images, **kwargs)
        return g.Generation(p, self.backend, self.codec, self.exporter, test_codec=True)

    def receipt(self, gen): return json.loads((gen.folder/'receipt.json').read_text())

    def fail(self, gen, code=None):
        with self.assertRaises(a.Failure) as caught: gen(self.scope)
        if code: self.assertEqual(caught.exception.code, code)
        self.assertFalse((gen.folder/'receipt.json').exists())
        self.assertEqual(gen.record['phase'], 'attention_required')
        return caught.exception.code

    def test_complete_generation_covers_storage_files_unique_images_and_catalog(self):
        gen = self.make(); self.assertEqual(gen(self.scope), {'result':'passed','child_processes_stopped':True})
        receipt = self.receipt(gen)
        self.assertEqual(len(receipt['artifacts']), 5)
        self.assertEqual(g.inspect(gen.folder, receipt)['result'], 'captured_integrity_verified')
        self.assertFalse(receipt['backup_ready']); self.assertTrue(receipt['worker_return_required'])
        self.assertEqual(self.exporter.calls, sorted(self.images))
        self.assertEqual([x for x in self.backend.calls if x[0] in ('stop','start')],
                         [('stop','frontend'),('stop','database'),('start','database'),('start','frontend')])
        self.assertTrue(all((gen.folder/row['path']).is_file() for row in receipt['artifacts'].values()))
        self.assertFalse(self.backend.helpers)

    def test_plan_does_not_read_source_bodies_or_write(self):
        before = list(self.out.iterdir())
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('body read')): gen = self.make()
        self.assertEqual(before, list(self.out.iterdir())); self.assertIsNone(gen.folder)

    def test_fresh_discovery_mapping_includes_all_sources_and_deduplicates_images(self):
        stores = [{'name':'db','path':str(self.src)}, {'name':'configuration','path':str(self.config)}]
        files = [{'name':'settings','path':str(self.config/'settings'),'status':'ok'},
                 {'name':'external','path':str(self.external),'status':'ok'}]
        images = {name:{'image_id':self.images[0]} for name in ('service','exporter')}
        proposal = g.discovered_plan(self.proposal,stores,files,images)
        self.assertEqual(proposal['images'], [self.images[0]])
        self.assertEqual(set(proposal['files']), {'configuration','external'})
        self.assertEqual(proposal['source_dispositions']['settings'],'files:configuration')
        self.assertEqual(proposal['source_dispositions']['db'],'storage-data')
        self.assertEqual(g.validate(proposal)['source_dispositions'], proposal['source_dispositions'])

    def test_discovery_missing_storage_or_required_file_is_not_silently_omitted(self):
        images = {'service':{'image_id':self.images[0]}}
        with self.assertRaises(a.Failure): g.discovered_plan(self.proposal,[],[],images)
        with self.assertRaises(a.Failure): g.discovered_plan(self.proposal,[{'name':'db','path':str(self.src)}],
            [{'name':'missing','path':str(self.external),'status':'unavailable'}],images)

    def test_source_disposition_is_bound_to_review(self):
        gen = self.make(); gen.original['source_dispositions']['invented']='skipped'
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertFalse(self.backend.calls)

    def test_modified_review_refused_before_directory_or_stop(self):
        gen = self.make(); gen.original['max_file_bytes'] += 1
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertFalse(list(self.out.iterdir())); self.assertFalse(self.backend.calls)

    def test_file_source_replaced_before_run_refused_without_stop(self):
        gen = self.make(); self.external.rename(self.root/'old'); self.external.write_text('new')
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertFalse(self.backend.calls)

    def test_file_metadata_edit_after_review_refused_without_stop(self):
        gen = self.make(); self.external.write_text('edited in place')
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertFalse(self.backend.calls); self.assertFalse(list(self.out.iterdir()))

    def test_source_symlink_and_hardlink_refused(self):
        (self.config/'link').symlink_to(self.external)
        self.fail(self.make(), 'GENERATION_ENTRY_TYPE_OR_LINK_REFUSED')
        self.assertFalse(any(x[0]=='start' for x in self.backend.calls))

    def test_special_entry_fifo_refused_without_blocking(self):
        os.mkfifo(self.config/'fifo', 0o600)
        self.fail(self.make(), 'GENERATION_ENTRY_TYPE_OR_LINK_REFUSED')

    def test_declared_excluded_child_is_not_traversed(self):
        (self.config/'skip').mkdir(); (self.config/'skip'/'link').symlink_to('/nonexistent')
        self.files['config']['exclude_children'] = ['skip']; gen = self.make(); gen(self.scope)
        with tarfile.open(gen.folder/'files.age') as archive:
            self.assertNotIn('data/config/skip', archive.getnames())

    def test_exclusions_cannot_escape_or_apply_to_single_file(self):
        for role, exclusions in [('config',['../secret']),('external',['child'])]:
            files = deepcopy(self.files); files[role]['exclude_children'] = exclusions
            with self.assertRaises(a.Failure): g.plan(self.proposal, files, self.images)

    def test_scoped_image_omission_and_duplicate_ids_refused(self):
        for images in [self.images[1:], self.images*2]:
            with self.assertRaises(a.Failure): g.plan(self.proposal, self.files, images)

    def test_destination_and_source_overlap_refused(self):
        files = {'bad':{'path':str(self.out),'exclude_children':[]}}
        with self.assertRaises(a.Failure): g.plan(self.proposal, files, self.images)

    def test_cold_directory_can_be_explicitly_omitted_from_private_state_root(self):
        container = self.root/'state'; container.mkdir(mode=0o700)
        moved = container/'data'; self.src.rename(moved)
        self.src = moved; self.stores['data']['source'] = str(moved); self.rows['database']['mounts'][0]['Source'] = str(moved)
        p = self.plan()
        self.backend.rows = deepcopy(self.rows)
        files = {'state':{'path':str(container),'exclude_children':['data']}}
        self.assertEqual(len(g.plan(p, files, self.images)['files']), 1)
        files['state']['exclude_children'] = []
        with self.assertRaises(a.Failure): g.plan(p, files, self.images)

    def test_export_partial_then_nonzero_exit_blocks_service_return(self):
        self.exporter.mode = 'fail'; self.fail(self.make(), 'AGE_PROCESS_FAILED')
        self.assertFalse(any(x[0]=='start' for x in self.backend.calls))
        self.assertTrue(all(x.poll() is not None for x in self.exporter.children))

    def test_empty_export_never_publishes_generation(self):
        self.exporter.mode = 'empty'; self.fail(self.make(), 'GENERATION_IMAGE_EXPORT_INVALID')

    def test_export_metadata_change_blocks_commit(self):
        original = self.exporter.start
        def start(*args):
            process = original(*args); self.exporter.changed = True; return process
        self.exporter.start = start; self.fail(self.make(), 'GENERATION_IMAGE_EXPORT_INVALID')

    def test_export_limit_terminates_producer_and_encryption(self):
        self.fail(self.make(max_image_bytes=8), 'RESTORE_PLAINTEXT_STREAM_LIMIT')
        self.assertTrue(all(x.poll() is not None for x in self.exporter.children))

    def test_file_byte_limit_preserves_unaccepted_partial(self):
        self.fail(self.make(max_file_bytes=1), 'GENERATION_FILE_BUDGET_EXCEEDED')

    def test_global_deadline_terminates_hanging_export_no_return(self):
        gen = self.make(); self.exporter.mode = 'hang'
        original = self.exporter.start
        def start(*args): gen.until = time.monotonic()+.15; return original(*args)
        self.exporter.start = start; self.fail(gen)
        self.assertTrue(all(x.poll() is not None for x in self.exporter.children))
        self.assertFalse(any(x[0]=='start' for x in self.backend.calls))

    def test_pause_before_commit_preserves_services_return_but_no_complete_receipt(self):
        gen = self.make(); gen.hook = lambda phase: setattr(self.scope,'active',False) if phase=='before_commit' else None
        self.fail(gen); self.assertEqual(gen.op.record['phase'],'services_returned')

    def test_commit_journal_write_failure_is_not_accepted(self):
        gen = self.make(); original = g.c.atomic_json
        def write(folder, name, value, previous=None):
            if name == 'generation.json' and value['phase']=='captured': raise OSError('disk failure')
            return original(folder, name, value, previous)
        with patch.object(g.c, 'atomic_json', write), self.assertRaises(OSError): gen(self.scope)
        self.assertEqual(g.inspect(gen.folder)['result'], 'attention_required')

    def test_generation_cannot_replay_or_overwrite_prior_receipt(self):
        gen = self.make(); gen(self.scope); before = (gen.folder/'receipt.json').read_bytes(); count = len(self.backend.calls)
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertEqual((gen.folder/'receipt.json').read_bytes(), before); self.assertEqual(len(self.backend.calls), count)

    def test_trusted_receipt_required_and_ciphertext_tampering_detected(self):
        gen = self.make(); gen(self.scope); receipt = self.receipt(gen)
        with self.assertRaises(a.Failure): g.inspect(gen.folder)
        (gen.folder/'files.age').write_bytes(b'changed')
        with self.assertRaises(a.Failure): g.inspect(gen.folder, receipt)

    def test_crypto_failure_never_returns_services(self):
        self.codec.mode = 'fail'; self.fail(self.make())
        self.assertFalse(any(x[0]=='start' for x in self.backend.calls))

    def test_unhealthy_same_id_return_never_commits(self):
        self.backend.unhealthy = True; self.fail(self.make(), 'COLD_SERVICE_RETURN_UNHEALTHY')

    def test_missing_scope_is_refused_before_writes(self):
        gen = self.make(); self.scope.active = False
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertFalse(list(self.out.iterdir()))

    def test_product_refuses_test_codec_unless_explicit(self):
        gen = self.make(); gen.test_codec = False
        with self.assertRaises(a.Failure): gen(self.scope)
        self.assertFalse(list(self.out.iterdir()))

    def test_catalog_is_encrypted_component_and_contains_exact_complete_source_roles(self):
        gen = self.make(); gen(self.scope)
        catalog = json.loads((gen.folder/'catalog.age').read_text())
        self.assertEqual(set(catalog['proposal']['files']), set(self.files))
        self.assertEqual(set(catalog['artifacts']), set(self.receipt(gen)['artifacts'])-{'catalog'})
        self.assertFalse(catalog['restore_verified'])

    def test_export_recipe_exact_id_clean_env_and_inherited_lease(self):
        backend = type('Docker', (), {'args':lambda self,args:['docker','--host','unix:///test',*args]})()
        exporter = g.ImageExporter(backend)
        with patch.object(g.subprocess, 'Popen') as popen: exporter.start(self.images[0], self.scope, self.scope.checkpoint)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][-3:], ['image','save',self.images[0]])
        self.assertEqual(kwargs['pass_fds'], (self.scope.descriptor,)); self.assertNotIn('DOCKER_CONTEXT', kwargs['env'])

    def controller(self, gen):
        from src.ops.candidates.maintenance_20260930.tests.test_maintenance import MaintenanceTests
        from src.ops.candidates.maintenance_intent_20260930 import maintenance
        prior = MaintenanceTests('test_plan_readonly_healthy_boot_history_and_new_scope')
        prior.setUp(); self.addCleanup(prior.doCleanups)
        controller = maintenance.Maintenance(prior.directory, prior.manager, inventory_fn=gen.inventory,
            snapshot_fn=lambda _:deepcopy(prior.fixture.binding), history_fn=lambda _:prior.history,
            environment_fn=lambda *args:None, readback_fn=prior.readback, clock=time.monotonic, sleep=lambda _:None,
            return_capture_fn=lambda *args:{'synthetic_metadata_only':True}, return_check_fn=lambda *args:None)
        return prior, controller

    def test_whole_generation_runs_under_real_maintenance_lease_then_explicit_worker_return(self):
        from src.ops.platform.ops import worker_state as ws
        from src.ops.platform.contracts import ReadFailure
        gen = self.make(); prior, controller = self.controller(gen)
        def callback(scope):
            with self.assertRaisesRegex(ReadFailure, 'AUTOMATIC_WORKER_BUSY'):
                with ws.automatic_lease(prior.directory): pass
            return gen(scope)
        result = controller.run(controller.plan(wait_seconds=1), callback)
        self.assertEqual(result['result'], 'passed', str(result)); prior.assert_paused()
        self.assertEqual((prior.manager.stops,prior.manager.starts),(1,0))
        self.assertEqual(g.inspect(gen.folder,self.receipt(gen))['result'],'captured_integrity_verified')
        returned = controller.resume(result['job_id'])
        self.assertTrue(returned['fresh_evaluation_verified']); self.assertEqual((prior.manager.stops,prior.manager.starts),(1,1))

    def test_failed_image_generation_cannot_return_worker_or_replay_callback(self):
        gen = self.make(); self.exporter.mode = 'fail'; prior, controller = self.controller(gen)
        result = controller.run(controller.plan(wait_seconds=1), gen)
        self.assertEqual(result['result'],'attention_required'); prior.assert_paused()
        with self.assertRaisesRegex(Exception,'MAINTENANCE_RETURN_REFUSED'): controller.resume(result['job_id'])
        self.assertEqual((prior.manager.stops,prior.manager.starts),(1,0))
        self.assertFalse((gen.folder/'receipt.json').exists())


class NativeAgeTests(GenerationTests):
    # Run native encryption through the whole callback, including storage and
    # files/images/catalog; inherited tests are deliberately not duplicated.
    __unittest_skip__ = False
    def setUp(self):
        super().setUp()
        tools = Path(os.environ.get('OPS_CAPTURE_TEST_AGE_DIR', '/unavailable'))
        self.assertTrue((tools/'age').is_file(), 'native age test tools required')
        self.identity = self.root/'temporary-identity'
        fd = os.open(self.identity, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        try: subprocess.run([str(tools/'age-keygen')], stdout=fd, stderr=subprocess.DEVNULL, check=True, timeout=10)
        finally: os.close(fd)
        recipient = subprocess.check_output([str(tools/'age-keygen'),'-y',str(self.identity)],timeout=10,stderr=subprocess.DEVNULL).decode().strip()
        self.codec = a.Age(tools/'age', a.sha(tools/'age')); self.recipient = recipient; self.proposal = self.plan()
    def test_native_full_generation_decrypts_catalog_files_and_image_eof(self):
        gen = self.make(); gen.test_codec = False; gen(self.scope); receipt = self.receipt(gen)
        self.assertEqual(receipt['encryption'], 'age_v1')
        def decrypt(path):
            result = subprocess.run([str(self.codec.path),'-d','-i',str(self.identity),str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True)
            return result.stdout
        catalog = json.loads(decrypt(gen.folder/'catalog.age'))
        self.assertEqual(len(catalog['artifacts']), 4)
        for ident in self.images:
            role = 'image-'+ident[7:]; raw = decrypt(gen.folder/receipt['artifacts'][role]['path'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), catalog['artifacts'][role]['detail']['export_sha256'])
        self.assertNotIn(b'synthetic-secret', (gen.folder/'files.age').read_bytes())
        self.assertEqual(g.inspect(gen.folder, receipt)['result'], 'captured_integrity_verified')
    def test_native_truncated_ciphertext_not_accepted(self):
        gen = self.make(); gen(self.scope); path = gen.folder/'files.age'
        path.write_bytes(path.read_bytes()[:-8])
        with self.assertRaises(a.Failure): g.inspect(gen.folder, self.receipt(gen))

# Select native cases explicitly; the checker does not rerun inherited codec
# cases as native tests with different expectations.
def suite():
    result = unittest.defaultTestLoader.loadTestsFromTestCase(GenerationTests)
    result.addTests(NativeAgeTests(name) for name in (
        'test_native_full_generation_decrypts_catalog_files_and_image_eof','test_native_truncated_ciphertext_not_accepted'))
    return result
