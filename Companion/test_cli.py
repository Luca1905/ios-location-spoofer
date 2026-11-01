from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cli


class CLITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = self.home / "services"
        self.root.mkdir()
        self.stack.enter_context(patch("install_service.CONFIG", self.root / "tailscale.json"))
        self.stack.enter_context(patch("cli.Path.home", return_value=self.home))
        for directory, device, name, peer in [(self.root, "phone-one", "iPhone-One.local.", "one.example.ts.net"),
                (self.root / "phone-two", "phone-two", "iPhone-Two.local.", "two.example.ts.net")]:
            directory.mkdir(exist_ok=True)
            (directory / "tailscale.json").write_text(json.dumps({"device": device, "peer": peer,
                "service": {"originalHost": name, "port": 49152, "txt": ["authTag=do-not-print"]}}))
            (directory / "tailscale_bridge.py").write_text("def watch_tunnel_ports(): pass\n")
        self.phones = cli.discover(self.root)

    def run_main(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(args, root=self.root)
        return code, output.getvalue(), errors.getvalue()

    def running(self, args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, "state = running\npid = 42\n", "")

    def test_discovers_and_selects_two_independent_phone_services(self):
        first = cli.select(self.phones, "default")[0]
        second = cli.select(self.phones, "iPhone Two")[0]
        self.assertNotEqual(first.label, second.label)
        self.assertEqual(first.label, "com.luca1905.location-spoofer.companion")
        self.assertEqual(cli.select(self.phones, second.config["device"]), [second])
        with self.assertRaises(ValueError):
            cli.select(self.phones, "iPhone")

    def test_restart_targets_only_the_requested_phone(self):
        before = {p.path: p.path.read_bytes() for p in self.phones}
        second = cli.select(self.phones, "phone-two")[0]
        with patch("cli.command", side_effect=self.running) as run:
            code, _, _ = self.run_main(["restart", "phone-two"])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["launchctl", "print", second.target], ["launchctl", "kickstart", "-k", second.target]])
        self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))

    def test_stop_targets_only_the_requested_phone(self):
        second = cli.select(self.phones, "phone-two")[0]
        with patch("cli.command", side_effect=self.running) as run:
            self.assertEqual(self.run_main(["stop", "phone-two"])[0], 0)
        self.assertEqual(run.call_args_list[-1].args[0], ["launchctl", "bootout", second.target])
        self.assertTrue(all(second.target in call.args[0] for call in run.call_args_list))

    def test_unknown_phone_cannot_control_any_service(self):
        with patch("cli.command") as run:
            code, _, errors = self.run_main(["restart", "does-not-exist"])
        self.assertEqual(code, 2)
        self.assertIn("unknown or ambiguous", errors)
        run.assert_not_called()

    def test_status_json_omits_pairing_records(self):
        with patch("cli.command", side_effect=self.running):
            code, output, _ = self.run_main(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)), 2)
        self.assertNotIn("authTag", output)
        self.assertNotIn("do-not-print", output)

    def test_doctor_distinguishes_online_tailscale_from_refused_apple_service(self):
        tailnet = {"BackendState": "Running", "Peer": {"second": {
            "DNSName": "two.example.ts.net.", "Online": True, "TailscaleIPs": ["100.64.0.2"]}}}
        def run(args, **kwargs):
            if args[0] == "tailscale":
                return subprocess.CompletedProcess(args, 0, json.dumps(tailnet), "")
            return self.running(args, **kwargs)
        with patch("cli.command", side_effect=run), \
                patch("cli.socket.create_connection", side_effect=ConnectionRefusedError):
            code, output, _ = self.run_main(["doctor", "phone-two", "--json"])
        report = json.loads(output)[0]
        self.assertEqual(code, 1)
        self.assertTrue(report["phoneOnline"])
        self.assertEqual(report["pairingConnection"], "refused")
        self.assertTrue(report["dynamicTunnelPorts"])
        self.assertFalse(report["transportReachable"])
        self.assertIn("Apple pairing service", report["problems"][0])

    def test_cli_install_preserves_phone_configurations_and_does_not_restart(self):
        before = {p.path: p.path.read_bytes() for p in self.phones}
        with patch("cli.command") as run:
            wrapper = cli.install_cli(self.root)
        run.assert_not_called()
        self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))
        result = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("doctor", result.stdout)


if __name__ == "__main__":
    unittest.main()
