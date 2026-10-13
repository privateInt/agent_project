import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("configure", Path(__file__).resolve().parents[1] / "ops/configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ConfigureTest(unittest.TestCase):
    def test_private_independent_secrets_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure.configure(root, 16100, "0.0.0.0", "ops-infra")
            values = {p.name: p.read_text() for p in (root / ".secrets").iterdir()}
            self.assertEqual(values["tool-credentials.json"], "{}\n")
            self.assertEqual(len(set(v for k,v in values.items() if k != "tool-credentials.json")), 4)
            self.assertEqual((root / ".secrets").stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o444 for p in (root / ".secrets").iterdir()))
            with self.assertRaises(ValueError):
                configure.configure(root, 16101, "0.0.0.0", "ops-infra")
            self.assertEqual(values, {p.name: p.read_text() for p in (root / ".secrets").iterdir()})

    def test_invalid_settings_leave_no_files(self):
        for port, host, network in [(80, "0.0.0.0", "ops-infra"), (16100, "hostname", "ops-infra"),
                                    (16100, "0.0.0.0", "bad\nnetwork")]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(ValueError):
                    configure.configure(root, port, host, network)
                self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
