"""Region experiment isolation, scoring and resume guards; no live inference."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import document_regions as regions
import live_document_regions as live
from live_collection_smoke import API, save
from live_upload_smoke import make_report as uploads
from test_ocr_quality_tools import IDENTITY


class MetricTests(unittest.TestCase):
    def setUp(self):
        _,self.ref=regions.plan_and_reference()
        self.baseline={'blocks':[], 'quality':{'state':'needs_ocr','issues':[]}}
        self.doc={'blocks':[], 'pages':[{}], 'quality':{'state':'needs_review','issues':[]}}

    def test_only_visible_candidate_labels_count_and_washing_ii_is_not_i(self):
        labels=[x for x in self.ref['figure']['labels'] if x!='Washing I']
        self.doc['blocks']=[{'type':'paragraph','text':'\n'.join(labels)}]
        self.doc['ocr']={'raw_html':'Washing I'}
        result=regions.score_case('figure-labels',self.doc,self.baseline)
        self.assertEqual(result['matched'],23)
        self.assertEqual(result['missing'],['Washing I'])
        self.assertFalse(result['probes_passed'])

    def table_doc(self):
        cells=[]
        for row,words in enumerate(self.ref['table1']['triplets']):
            cells.extend({'row':row,'column':col,'text':word} for col,word in enumerate(['13.','Tanning']+words))
        for row,words in enumerate(self.ref['table1']['processes'],len(self.ref['table1']['triplets'])):
            cells.extend({'row':row,'column':col,'text':word} for col,word in enumerate(words+['Inputs:','','']))
        self.doc['blocks']=[{'type':'table','cells':cells}]
        return cells

    def test_table_requires_same_row_correct_half_columns_and_valid_spans(self):
        cells=self.table_doc()
        self.assertTrue(regions.score_case('table-right',self.doc,self.baseline)['probes_passed'])
        for variant in ('columns','spans','amount','prose'):
            with self.subTest(variant=variant):
                original=copy.deepcopy(self.doc)
                if variant=='columns':
                    for c in cells:c['column']+=5
                if variant=='spans':cells.append(copy.deepcopy(cells[0]))
                if variant=='amount':next(c for c in cells if c['text']=='47.44')['text']='47.45'
                if variant=='prose':self.doc['blocks']=[{'type':'paragraph','text':str(cells)}]
                self.assertFalse(regions.score_case('table-right',self.doc,self.baseline)['probes_passed'])
                self.doc=original;cells=self.doc['blocks'][0]['cells']

    def test_wrong_source_rejected_before_rendering(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'hashes differ'):
                regions.render_inputs(b'wrong',{'source':{'sha256':'wrong'}},Path(d))
            self.assertEqual(list(Path(d).iterdir()),[])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'report.json'
        self.baseline={'schema_version':'canonical-document-v1','source':{'document_id':'source','sha256':'a'*64,'format':'pdf'},
            'processor':{'version':'cpu-parser-surya-v1','image_id':'sha256:'+'d'*64,'rules_version':'basic-cleaning-v1'},
            'pages':[{}]*9,'blocks':[], 'ocr':{'identity':IDENTITY},
            'quality':{'state':'needs_review','issues':[{'requires_ocr':False}]}}
        raw=json.dumps(self.baseline).encode();(self.path.parent/'baseline.json').write_bytes(raw)
        source={'document_id':'source','sha256':'a'*64}
        run={'status':'succeeded','refinement_run_id':'baseline','output_document_id':'canon','source_format':'pdf','source_sha256':'a'*64,'ocr':IDENTITY}
        meta={'document_id':'canon','refinement_run_id':'baseline','source_document_id':'source','sha256':hashlib.sha256(raw).hexdigest(),
            'size_bytes':len(raw),'schema_version':'canonical-document-v1','processor_version':'cpu-parser-surya-v1',
            'processor_image_id':'sha256:'+'d'*64,'state':'needs_review','block_count':0,'page_count':9}
        inputs=self.path.parent/'inputs';inputs.mkdir()
        ids=['figure-labels','table-left','table-right']
        for n in ids:(inputs/(n+'.png')).write_bytes(b'fake-'+n.encode())
        up=uploads(API('http://example.test'),[inputs/(n+'.png') for n in ids])['uploads']
        self.report={'version':live.VERSION,'key':'test','code_identity':live.code_identity(),'api':'http://example.test',
            'model_api':'http://model.test','source':source,'baseline_run':run,'baseline_metadata':meta,'baseline':self.baseline,
            'identity':IDENTITY,'scope':'test','cases':[]}
        for n,u in zip(ids,up):
            seed={'input':{'id':n,'file':n+'.png','sha256':u['sha256'],'size_bytes':u['size_bytes']},'upload':u,'key':'stable-'+n,
                  'request':{'name':'test-'+n,'rules_version':'basic-cleaning-v1','ocr_deployment_id':IDENTITY['deployment_id']}}
            self.report['cases'].append({'id':n,'seed':seed,'seed_sha256':live.digest(seed),'state':'pending'})
        self.report['fixed_sha256']=live.fixed_digest(self.report);save(self.path,self.report)

    def test_changed_last_input_blocks_every_upload(self):
        (self.path.parent/'inputs/table-right.png').write_bytes(b'changed')
        with patch.object(live,'upload') as upload,patch.object(live,'ready'):
            with self.assertRaises(ValueError):live.execute(self.report,self.path)
            upload.assert_not_called()

    def test_changed_request_code_or_baseline_blocks_submission(self):
        for kind in ('request','code','baseline'):
            report=copy.deepcopy(self.report)
            if kind=='request':report['cases'][0]['seed']['key']='different'
            if kind=='code':report['code_identity']='different'
            if kind=='baseline':report['baseline']['source']['sha256']='different'
            with patch.object(live,'upload') as upload,patch.object(live,'ready'):
                with self.assertRaises(ValueError):live.execute(report,self.path)
                upload.assert_not_called()

    def test_offline_incomplete_cannot_pass_or_submit(self):
        with patch.object(live,'API') as api:
            live.execute(self.report,self.path,offline=True)
            api.assert_not_called()
        self.assertFalse(self.report['complete']);self.assertFalse(self.report['all_probes_passed'])

    def test_model_change_blocks_every_upload(self):
        with patch.object(live,'upload') as upload,patch.object(live,'ready',side_effect=ValueError('runtime changed')):
            with self.assertRaisesRegex(ValueError,'runtime changed'):live.execute(self.report,self.path)
            upload.assert_not_called()

    def test_lost_acceptance_reuses_exact_key_body_and_upload(self):
        first=self.report['cases'][0];keys=[];requests=[]
        source={'document_id':'source-crop'}
        class LostAPI:
            def __init__(self,*args):pass
            def call(self,path,body=None,key=None,**kwargs):
                keys.append(key);requests.append(copy.deepcopy(body));raise OSError('lost reply')
        with patch.object(live,'API',LostAPI),patch.object(live,'ready'),patch.object(live,'check_document'),patch.object(live,'upload',return_value=(200,source)) as upload:
            for _ in range(2):
                report=json.loads(self.path.read_text())
                with self.assertRaisesRegex(OSError,'lost reply'):live.execute(report,self.path)
            self.assertEqual(upload.call_count,2)
            self.assertEqual(keys,[first['seed']['key']]*2);self.assertEqual(requests[0],requests[1])

    def test_all_terminal_failures_are_retained_without_new_keys(self):
        class FailedAPI:
            def __init__(self,*args):pass
            def call(self,path,body=None,key=None,**kwargs):
                if body is not None:return 202,{'refinement_run_id':key,'operation_id':'op-'+key}
                key=path.rsplit('/',1)[1];name=key.removeprefix('stable-')
                return 200,{'refinement_run_id':key,'operation_id':'op-'+key,'source_document_id':'source-crop',
                    'name':'test-'+name,'status':'failed','error_code':'TEST_FAILURE'}
        with patch.object(live,'API',FailedAPI),patch.object(live,'ready'),patch.object(live,'check_document'),patch.object(live,'upload',return_value=(200,{'document_id':'source-crop'})) as upload:
            live.execute(self.report,self.path)
            self.assertTrue(self.report['complete']);self.assertFalse(self.report['all_probes_passed'])
            self.assertEqual(upload.call_count,3)
            live.execute(self.report,self.path);self.assertEqual(upload.call_count,3)

    def test_corrupt_completed_cache_blocks_all_new_work(self):
        case=self.report['cases'][0];case.update(state='evaluated',source={},run={},metadata={},document={})
        (self.path.parent/'figure-labels.json').write_text('{}')
        with patch.object(live,'API') as api:
            with self.assertRaises((ValueError,KeyError)):live.execute(self.report,self.path)
            api.assert_not_called()

    def test_verified_results_complete_all_cases_despite_quality_failures_and_recheck_offline(self):
        responses={}; contents={}; uploaded={}; submissions=[]
        def fake_upload(api,item,data):
            sid=str(uuid.uuid5(uuid.NAMESPACE_URL,item['key']))
            source={'document_id':sid,'source_type':'upload','original_filename':item['filename'],'name':item['name'],
                    'format':item['format'],'state':'ready','sha256':item['sha256'],'size_bytes':len(data),
                    'crawl_run_id':'','source_url':'','final_url':'','format_version':'source-format-v2',
                    'artifact_uri':'s3://data-sources/uploads/'+sid}
            uploaded[sid]=source
            return 200,source
        outer=self
        class ResultAPI:
            def __init__(self,*args):pass
            def call(self,path,body=None,key=None,**kwargs):
                if body is None:return 200,responses[path]
                submissions.append(key);rid=str(uuid.uuid5(uuid.NAMESPACE_URL,key));cid=str(uuid.uuid5(uuid.NAMESPACE_OID,key))
                source=uploaded[body['document_id']]
                doc=copy.deepcopy(outer.baseline);doc.update(pages=[{}],source={'document_id':source['document_id'],'sha256':source['sha256'],'format':'png'})
                raw=json.dumps(doc).encode()
                run={'refinement_run_id':rid,'operation_id':'op-'+rid,'name':body['name'],'source_document_id':source['document_id'],
                     'source_sha256':source['sha256'],'source_format':'png','status':'succeeded','output_document_id':cid,'ocr':IDENTITY}
                meta={'document_id':cid,'refinement_run_id':rid,'source_document_id':source['document_id'],
                      'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),'schema_version':'canonical-document-v1',
                      'processor_version':'cpu-parser-surya-v1','processor_image_id':doc['processor']['image_id'],
                      'state':'needs_review','block_count':0,'page_count':1}
                responses['/api/refinement-runs/'+rid]=run;responses['/api/canonical-documents/'+cid]=meta
                contents['/api/canonical-documents/'+cid+'/content']=raw
                return 202,{'refinement_run_id':rid,'operation_id':'op-'+rid}
        with patch.object(live,'API',ResultAPI),patch.object(live,'ready'),patch.object(live,'upload',side_effect=fake_upload),patch.object(live,'download',side_effect=lambda api,path:contents[path]):
            live.execute(self.report,self.path)
        self.assertEqual(len(submissions),3)
        self.assertTrue(self.report['complete']);self.assertFalse(self.report['all_probes_passed'])
        with patch.object(live,'API') as api:
            live.execute(self.report,self.path,offline=True);api.assert_not_called()
        self.report['cases'][0]['document']['quality']['state']='ready'
        with patch.object(live,'API') as api:
            with self.assertRaisesRegex(ValueError,'report/cache'):live.execute(self.report,self.path)
            api.assert_not_called()

if __name__=='__main__':unittest.main()
