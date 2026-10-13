import copy
import unittest
from enable_remote_node import enable


class PinTests(unittest.TestCase):
    def fixture(self):
        return ({'version': 1, 'nodes': [dict(name='worker', architecture='amd64', daemon_id='', enabled=False)]},
                dict(version=1, action='probe', status='ready', node_name='worker', architecture='amd64', daemon_id='daemon1'))

    def test_enable_replay_preserves_pin(self):
        catalog, probe = self.fixture()
        result = enable(catalog, probe)
        self.assertTrue(result['nodes'][0]['enabled'])
        before = copy.deepcopy(result)
        self.assertEqual(enable(result, probe), before)

    def test_failed_probe_or_changed_daemon_cannot_enable(self):
        catalog, probe = self.fixture()
        probe['status'] = 'unknown'
        with self.assertRaises(ValueError):
            enable(catalog, probe)
        self.assertFalse(catalog['nodes'][0]['enabled'])
        probe['status'] = 'ready'
        catalog['nodes'][0]['daemon_id'] = 'old-daemon'
        with self.assertRaisesRegex(ValueError, 'rebind'):
            enable(catalog, probe)
        self.assertFalse(catalog['nodes'][0]['enabled'])


if __name__ == '__main__':
    unittest.main()
