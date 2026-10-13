import importlib.util,json,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def module(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/'ops'/f'{name}.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
w=module('workflow')
class Operations(unittest.TestCase):
 def test_workflow_inactive_and_credentials_not_embedded(self):
  v=w.workflow();self.assertFalse(v['active']);self.assertNotIn('credentials',v);self.assertNotIn('pinData',v)
  requests=[n for n in v['nodes'] if n['type'].endswith('.httpRequest')];self.assertEqual(len(requests),2)
  for n in requests:
   self.assertNotIn('credentials',n);self.assertEqual(n['parameters']['genericAuthType'],'httpHeaderAuth');self.assertFalse(n['parameters']['options']['redirect']['redirect']['followRedirects']);self.assertTrue(n['retryOnFail'])
  self.assertEqual(v['settings']['executionTimeout'],720)
 def test_replay_envelope_keeps_original_caller(self):
  envelope={'invocation_key':'123','body':{'caller_context':{'execution_id':'original'}}}
  v=w.workflow(request=envelope);s=v['nodes'][1]['parameters']['jsCode'];data=json.loads(json.loads(s.removeprefix('return [{json:JSON.parse(').removesuffix(')}];')))
  self.assertEqual(data['request'],envelope)
 def test_question_is_data_not_javascript(self):
  attack="'); throw Error('injected'); //\n中文 한국어"
  v=w.workflow(question=attack);s=v['nodes'][1]['parameters']['jsCode'];data=json.loads(json.loads(s.removeprefix('return [{json:JSON.parse(').removesuffix(')}];')))
  self.assertEqual(data['question'],attack)
if __name__=='__main__':unittest.main()
