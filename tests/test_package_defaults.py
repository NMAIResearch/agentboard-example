"""Check the packaged default, override, home boundary and display prefix."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import agentboard


class PackageDefaultsTest(unittest.TestCase):
    def probe(self, override=None):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOARD_")}
            env["HOME"] = str(home)
            if override is not None:
                env["AGENTBOARD_DIR"] = override
            code = ("import json,sys; sys.path.insert(0,sys.argv[1]); import agentboard; "
                    "print(json.dumps([str(agentboard.board_dir()),agentboard.DEFAULT_DIR,agentboard.HOME_ROOT]))")
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(HERE)],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(temp).iterdir()), [])
            return json.loads(result.stdout), str(home)

    def test_default_uses_current_home_without_creating_state(self):
        values, home = self.probe()
        self.assertEqual(values, [str(Path(home) / ".local/state/agentboard")] * 2 + [home])

    def test_environment_override_wins(self):
        values, home = self.probe("/synthetic/other-board")
        self.assertEqual(values[0], "/synthetic/other-board")
        self.assertEqual(values[1], str(Path(home) / ".local/state/agentboard"))

    def test_short_paths_respect_home_component_boundary(self):
        with mock.patch.object(agentboard, "HOME_ROOT", "/synthetic/home"):
            self.assertEqual(agentboard.short("/synthetic/home/project"), "~/project")
            self.assertEqual(agentboard.short("/synthetic/home"), "/synthetic/home")
            self.assertEqual(agentboard.short("/synthetic/home-other/project"), "/synthetic/home-other/project")
            self.assertEqual(agentboard.short("/other/project"), "/other/project")

    def test_page_prefix_is_json_encoded_from_current_home(self):
        self.assertIn("const homePrefix = " + json.dumps(agentboard.HOME_ROOT + os.sep) + ";", agentboard.SCRIPT)
        self.assertIn("path.startsWith(homePrefix) ? '~/' + path.slice(homePrefix.length) : path", agentboard.SCRIPT)
        self.assertNotIn("__HOME_PREFIX__", agentboard.SCRIPT)


if __name__ == "__main__":
    unittest.main()
