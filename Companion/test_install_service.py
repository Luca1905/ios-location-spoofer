from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from install_service import install, LABEL, stop_orphaned_bridges


class ServiceInstallationTests(unittest.TestCase):
    def test_reinstall_stops_only_this_phones_orphaned_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "tailscale_bridge.py"
            script.write_text("import time; time.sleep(60)\n")
            first_config, second_config = root / "first.json", root / "second.json"
            first = subprocess.Popen([sys.executable, str(script), "--config", str(first_config)])
            second = subprocess.Popen([sys.executable, str(script), "--config", str(second_config)])
            try:
                stop_orphaned_bridges(root, first_config)
                self.assertEqual(first.wait(timeout=3), -15)
                self.assertIsNone(second.poll())
            finally:
                for process in (first, second):
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)

    def test_a_lock_owner_that_exits_during_reinstallation_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "tailscale.json"
            config.write_text(json.dumps({"device": "phone-one"}))

            def command_result(command, **_kwargs):
                if command[:2] == ["/usr/sbin/lsof", "-t"]:
                    return subprocess.CompletedProcess(command, 0, stdout="12345\n")
                if command[0] == "ps":
                    return subprocess.CompletedProcess(command, 1, stdout="")
                return subprocess.CompletedProcess(command, 0, stdout="")

            with patch("install_service.CONFIG", config), patch("install_service.Path.home", return_value=home), \
                    patch("install_service.subprocess.run", side_effect=command_result) as run, \
                    redirect_stdout(io.StringIO()):
                install(config)
            self.assertTrue(any(call.args[0][:2] == ["launchctl", "bootstrap"] for call in run.call_args_list))

    def test_adding_a_second_phone_preserves_the_first_service_and_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = home / "first" / "tailscale.json"
            second = home / "second" / "tailscale.json"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text(json.dumps({"device": "phone-one", "peer": "first.example.ts.net"}))
            second.write_text(json.dumps({"device": "phone-two", "peer": "second.example.ts.net"}))
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            old_plist = agents / f"{LABEL}.plist"
            old_plist.write_text("existing phone service")
            original = first.read_bytes()
            with patch("install_service.CONFIG", first), patch("install_service.Path.home", return_value=home), \
                    patch("install_service.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as run, \
                    redirect_stdout(io.StringIO()):
                install(second)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(old_plist.read_text(), "existing phone service")
            calls = [call.args[0] for call in run.call_args_list]
            bootout = next(command for command in calls if command[:2] == ["launchctl", "bootout"])
            self.assertNotEqual(bootout[2].split("/", 2)[-1], LABEL)
            new_plist = next(path for path in agents.glob("*.plist") if path != old_plist)
            data = plistlib.loads(new_plist.read_bytes())
            self.assertEqual(data["ProgramArguments"][-2:], ["--config", str(second.resolve())])


if __name__ == "__main__":
    unittest.main()
