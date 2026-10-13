import importlib.util
from pathlib import Path
import tempfile
import unittest
SPEC = importlib.util.spec_from_file_location("upgrade", Path(__file__).resolve().parents[1] / "ops/upgrade_a03.py")
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class UpgradeTest(unittest.TestCase):
    def test_additive_preparation_preserves_existing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".secrets").mkdir(mode=0o700)
            (root / ".env").write_text("AGENT_GATEWAY_PORT=16100\n")
            key = root / ".secrets/api-token"
            key.write_text("existing")
            upgrade.prepare(root)
            file = root / ".secrets/tool-credentials.json"
            self.assertEqual(file.read_text(), "{}\n")
            file.chmod(0o644)
            file.write_text('{"test":{"revision":"v1","value":"preserved"}}')
            before = file.read_bytes()
            upgrade.prepare(root)
            self.assertEqual(file.read_bytes(), before)
            self.assertEqual(key.read_text(), "existing")
            self.assertEqual((root / ".env").read_text(), "AGENT_GATEWAY_PORT=16100\n")
