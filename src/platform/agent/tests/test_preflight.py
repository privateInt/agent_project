import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("preflight", Path(__file__).resolve().parents[1] / "ops/preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def config_with_tmpfs(mounts):
    services = {name: {"networks": {"agent-private": {}}} for name in preflight.EXPECTED}
    services["agent-runtime"]["secrets"] = [{"source": "agent-internal-token"}, {"source": "agent-tool-credentials"}]
    services["agent-runtime"]["tmpfs"] = mounts
    services["agent-mcp"]["secrets"] = [{"source": "agent-internal-token"}, {"source": "agent-api-token"}]
    return {"services": services, "networks": {"agent-private": {"internal": True}}}


class MountPreflightTest(unittest.TestCase):
    def test_single_mount_with_options_is_accepted(self):
        preflight.verify_config(config_with_tmpfs(["/tmp:size=16m,mode=1777"]))

    def test_yaml_comma_split_is_rejected_before_daemon_call(self):
        # Valid YAML and Compose config, but an invalid Docker mount. This is
        # the exact normalized value observed in the failed deployment.
        with self.assertRaisesRegex(ValueError, "absolute container path"):
            preflight.verify_config(config_with_tmpfs(["/tmp:size=16m", "mode=1777"]))


if __name__ == "__main__":
    unittest.main()
