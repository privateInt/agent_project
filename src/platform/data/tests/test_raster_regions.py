import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from PIL import Image,ImageDraw
import pypdfium2 as pdfium

import raster_regions as pixels
import raster_document as raster
import raster_quality as quality
import live_raster_regions as live
import live_document_regions as shared
from live_collection_smoke import API,save
from live_upload_smoke import make_report
from test_ocr_quality_tools import IDENTITY


def authored_doc(case):
    cells=[];data=[['No.','Process','Inputs and Outputs','Amount','Unit']];previous=None
    for section,item,amount,unit in case['rows']:
        if section!=previous:data.append([*case['process'],section,'','']);previous=section
        data.append([*case['process'],item,amount,unit])
    for row,values in enumerate(data):
        cells.extend({'row':row,'column':col,'text':s,'row_span':1,'column_span':1} for col,s in enumerate(values))
    return {'blocks':[{'id':'b1','type':'table','cells':cells}],'pages':[{}],'quality':{'state':'needs_review'}}


class RasterTests(unittest.TestCase):
    def diagram(self,offset=(0,0)):
        im=Image.new('RGB',(800,600),'white');d=ImageDraw.Draw(im);x,y=offset
        for b in ([100+x,180+y,260+x,240+y],[400+x,180+y,620+x,240+y]):d.rectangle(b,outline='black',width=2)
        d.line((260+x,210+y,400+x,210+y),fill='black',width=2)
        d.text((130+x,200+y),'Label one',fill='black');d.text((420+x,200+y),'Label two',fill='black')
        return im

    def table(self):
        im=Image.new('RGB',(800,600),'white');d=ImageDraw.Draw(im)
        for y in (70,94,550):d.line((20,y,780,y),fill='black',width=2)
        for offset in (0,380):
            for x,width in zip((30,74,150,270,328),(20,40,90,40,20)):d.rectangle((x+offset,76,x+offset+width,87),fill='black')
        for x,ys in ((33,(150,300)),(413,(180,400))):
            for y in ys:d.rectangle((x,y,x+8,y+12),fill='black');d.rectangle((x+140,y,x+200,y+12),fill='black')
        d.rectangle((560,115,610,125),fill='black')
        return im

    def test_diagram_detection_translation_scaling_and_arrows(self):
        for offset in ((0,0),(23,45)):
            for scale in (1,.75,1.25):
                im=self.diagram(offset);im=im.resize((round(im.width*scale),round(im.height*scale)))
                boxes=pixels.diagram_boxes(im);self.assertEqual(len(boxes),2)
                self.assertAlmostEqual(boxes[0][0],(100+offset[0])*scale,delta=3)
                out,mapping=pixels.compose_diagram(im,boxes)
                self.assertEqual(len(mapping),2);self.assertLessEqual(out.width*out.height,pixels.MAX_PIXELS)

    def test_table_pixel_panels_anchor_groups_and_unassigned_prefix(self):
        panels=pixels.table_groups(self.table())
        self.assertEqual([p['columns'] for p in panels],[5,5])
        self.assertEqual([g['kind'] for g in panels[0]['groups']],['anchor-group']*2)
        self.assertEqual([g['kind'] for g in panels[1]['groups']],['leading-unassigned','anchor-group','anchor-group'])
        for p in panels:
            for a,b in zip(p['groups'],p['groups'][1:]):self.assertEqual(a['box'][3],b['box'][1])
            for g in p['groups']:
                out,m=pixels.compose_table(self.table(),p['header'],g)
                self.assertTrue(all(0<=v<=out.size[j%2] for piece in m for j,v in enumerate(piece['image_bbox'])))

    def test_blank_and_ambiguous_layout_are_not_silent_success(self):
        im=Image.new('RGB',(800,600),'white');self.assertEqual(pixels.diagram_boxes(im),[])
        with self.assertRaises(ValueError):pixels.table_groups(im)
        im=self.table();ImageDraw.Draw(im).line((20,52,780,52),fill='black',width=2)
        with self.assertRaises(ValueError):pixels.table_groups(im)
        with self.assertRaises(ValueError):pixels.diagram_boxes(Image.new('L',(3000,3000)))

    def test_image_only_pdf_render_works_without_parser(self):
        buf=io.BytesIO();self.diagram().save(buf,format='PDF')
        with patch('pdfplumber.open',side_effect=AssertionError('Text parser must not run')):
            with pdfium.PdfDocument(buf.getvalue()) as pdf:
                im=raster.page_image(pdf,1)
                self.assertEqual(len(pixels.diagram_boxes(im)),2)

    def test_wrong_source_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):raster.prepare(b'wrong',Path(d)/'inputs')
            self.assertFalse((Path(d)/'inputs').exists())


class QualityTests(unittest.TestCase):
    def test_all_authored_process_section_value_rows_pass(self):
        for case in quality.reference()['cases']:
            with self.subTest(case=case['id']):self.assertTrue(quality.score_tables(authored_doc(case),[case])['passed'])

    def test_correct_values_in_wrong_process_or_section_fail(self):
        case=quality.reference()['cases'][5]
        for variant in ('number','name','section','quantity','sixth_column','duplicate'):
            doc=authored_doc(case);cells=doc['blocks'][0]['cells']
            if variant=='number':
                for c in cells:
                    if c['row']>0 and c['column']==0:c['text']='17.'
            if variant=='name':
                for c in cells:
                    if c['row']>0 and c['column']==1:c['text']='Sammying'
            if variant=='section':
                for c in cells:
                    if c['text']=='Inputs:':c['text']='Outputs:'
            if variant=='quantity':next(c for c in cells if c['text']=='47.44')['text']='47.45'
            if variant=='sixth_column':cells.append({'row':1,'column':5,'text':'extra'})
            if variant=='duplicate':cells.append(copy.deepcopy(cells[6]))
            self.assertFalse(quality.score_tables(doc,[case])['passed'],variant)

    def test_missing_extra_or_duplicate_association_fails(self):
        case=quality.reference()['cases'][-1]
        for variant in ('missing','extra','duplicate'):
            doc=authored_doc(case);cells=doc['blocks'][0]['cells']
            if variant=='missing':doc['blocks'][0]['cells']=[c for c in cells if c['row']!=2]
            else:
                row=[dict(c,row=3) for c in cells if c['row']==2]
                if variant=='extra':row[2]['text']='Invented material'
                cells.extend(row)
            self.assertFalse(quality.score_tables(doc,[case])['passed'])

    def test_unassigned_rows_cannot_gain_a_process(self):
        case=quality.reference()['cases'][4];doc=authored_doc(case)
        for c in doc['blocks'][0]['cells']:
            if c['row']>0 and c['column'] in (0,1):c['text']=('13.','Tanning')[c['column']]
        result=quality.score_tables(doc,[case]);self.assertFalse(result['passed']);self.assertTrue(result['unexpected_processes'])

    def test_figure_labels_must_match_their_pixel_region(self):
        labels=json.loads(quality.REFERENCE.read_bytes())['figure']['labels'];mapping=[];blocks=[]
        for i,text in enumerate(labels):
            b=[0,i*20,100,i*20+10];mapping.append({'source_bbox':b,'image_bbox':b,'source_page':3})
            blocks.append({'id':'b'+str(i+1),'type':'paragraph','text':text,'location':{'page':1,'bbox':b}})
        doc={'blocks':blocks};self.assertTrue(quality.score_figure(doc,mapping)['passed'])
        blocks[0]['text'],blocks[1]['text']=blocks[1]['text'],blocks[0]['text']
        result=quality.score_figure(doc,mapping);self.assertEqual(result['matched'],24);self.assertFalse(result['passed'])
        self.assertEqual(len(result['region_label_mismatches']),2)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'report.json'
        sha=quality.reference()['source_sha256']
        baseline={'schema_version':'canonical-document-v1','source':{'document_id':'source','sha256':sha,'format':'pdf'},
                  'processor':{'version':'cpu-parser-surya-v1','image_id':'sha256:'+'d'*64,'rules_version':'basic-cleaning-v1'},
                  'pages':[{}]*9,'blocks':[],'ocr':{'identity':IDENTITY},'quality':{'state':'needs_review','issues':[{'requires_ocr':False}]}}
        self.baseline=baseline
        raw=json.dumps(baseline).encode();(self.path.parent/'baseline.json').write_bytes(raw)
        source={'document_id':'source','sha256':sha};run={'status':'succeeded','refinement_run_id':'base','output_document_id':'canon','source_format':'pdf','source_sha256':sha,'ocr':IDENTITY}
        meta={'document_id':'canon','refinement_run_id':'base','source_document_id':'source','sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),
              'schema_version':'canonical-document-v1','processor_version':'cpu-parser-surya-v1','processor_image_id':'sha256:'+'d'*64,'state':'needs_review','block_count':0,'page_count':9}
        directory=self.path.parent/'inputs';directory.mkdir();ids=['figure-labels']+[c['id'] for c in quality.reference()['cases']]
        for n in ids:(directory/(n+'.png')).write_bytes(n.encode())
        uploads=make_report(API('http://example.test'),[directory/(n+'.png') for n in ids])['uploads']
        self.report={'version':live.VERSION,'key':'test','code_identity':live.code_identity(),'api':'http://example.test','model_api':'http://model.test',
                     'source':source,'baseline_run':run,'baseline_metadata':meta,'baseline':baseline,'identity':IDENTITY,'scope':'test','cases':[]}
        for n,u in zip(ids,uploads):
            seed={'input':{'id':n,'file':n+'.png','sha256':u['sha256'],'size_bytes':u['size_bytes'],'mapping':[]},'upload':u,'key':'stable-'+n,
                  'request':{'name':'test-'+n,'rules_version':'basic-cleaning-v1','ocr_deployment_id':IDENTITY['deployment_id']}}
            self.report['cases'].append({'id':n,'seed':seed,'seed_sha256':shared.digest(seed),'state':'pending'})
        self.report['fixed_sha256']=shared.fixed_digest(self.report);save(self.path,self.report)

    def test_changed_last_input_or_mapping_blocks_every_request(self):
        for variant in ('mapping','pixels'):
            report=copy.deepcopy(self.report)
            if variant=='mapping':report['cases'][-1]['seed']['input']['mapping']=[{'changed':True}]
            else:(self.path.parent/'inputs/table-right-3.png').write_bytes(b'changed')
            with patch.object(shared,'API') as api:
                with self.assertRaises(ValueError):live.execute(report,self.path)
                api.assert_not_called()

    def test_offline_incomplete_never_submits_or_passes(self):
        with patch.object(shared,'API') as api:
            live.execute(self.report,self.path,offline=True);api.assert_not_called()
        self.assertFalse(self.report['complete']);self.assertFalse(self.report['all_probes_passed'])

    def test_runtime_change_and_corrupt_cache_block_all_uploads(self):
        with patch.object(shared,'ready',side_effect=ValueError('changed')),patch.object(shared,'upload') as upload:
            with self.assertRaises(ValueError):live.execute(self.report,self.path)
            upload.assert_not_called()
        self.report['cases'][0].update(state='evaluated',source={},run={},metadata={},document={})
        (self.path.parent/'figure-labels.json').write_text('{}')
        with patch.object(shared,'API') as api:
            with self.assertRaises((ValueError,KeyError)):live.execute(self.report,self.path)
            api.assert_not_called()

    def test_lost_reply_resumes_same_keys(self):
        requests=[]
        class Lost:
            def __init__(self,*args):pass
            def call(self,path,body=None,key=None,**kwargs):requests.append((key,copy.deepcopy(body)));raise OSError('lost')
        with patch.object(shared,'API',Lost),patch.object(shared,'ready'),patch.object(shared,'check_document'),patch.object(live,'check_document'),patch.object(shared,'upload',return_value=(200,{'document_id':'crop'})):
            for i in range(2):
                with self.assertRaises(OSError):live.execute(json.loads(self.path.read_bytes()),self.path)
        self.assertEqual(requests[0],requests[1])

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
        with patch.object(shared,'API',ResultAPI),patch.object(shared,'ready'),patch.object(shared,'upload',side_effect=fake_upload),patch.object(shared,'download',side_effect=lambda api,path:contents[path]):
            live.execute(self.report,self.path)
        self.assertEqual(len(submissions),9)
        self.assertTrue(self.report['complete']);self.assertFalse(self.report['all_probes_passed'])
        with patch.object(shared,'API') as api:
            live.execute(self.report,self.path,offline=True);api.assert_not_called()
        self.report['cases'][0]['document']['quality']['state']='ready'
        with patch.object(shared,'API') as api:
            with self.assertRaisesRegex(ValueError,'cache'):live.execute(self.report,self.path)
            api.assert_not_called()

if __name__=='__main__':unittest.main()
