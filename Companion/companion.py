#!/usr/bin/env python3
"""Apply location requests from the iPhone app using Apple's paired-device tools."""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import sys
import uuid

APP_ID = "com.luca1905.location-spoofer"


def lock_path(device):
    identifier = hashlib.sha256(device.casefold().encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"location-spoofer-companion-{identifier}.lock"


def devicectl(device, *args):
    result = subprocess.run(
        ["xcrun", "devicectl", "device", *args, "--device", device, "--timeout", "15"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-1500:])
    return result.stdout.strip()


def validate_request(request):
    if request.get("version") != 1:
        raise ValueError("Unsupported app command version; update the companion.")
    uuid.UUID(request["id"])
    if type(request.get("enabled")) is not bool:
        raise ValueError("Invalid enabled flag.")
    for key, limit in (("latitude", 90), ("longitude", 180)):
        value = request.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
            raise ValueError(f"Invalid {key}.")
    return request


def apply_request(device, request, run=devicectl):
    """A receipt confirms command execution; the iPhone verifies Core Location."""
    validate_request(request)
    receipt = {"requestId": request["id"], "success": False, "processedAt": time.time()}
    try:
        if request["enabled"]:
            run(device, "simulate", "location", "coordinate",
                "--latitude", str(request["latitude"]), "--longitude", str(request["longitude"]))
        else:
            run(device, "simulate", "location", "clear")
        receipt["success"] = True
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        receipt["error"] = f"Could not apply location. Unlock your iPhone and check its Mac connection. {error}"
    return receipt


def serve(device, once=False, run=devicectl, heartbeat=lambda: None):
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    last_id = None
    last_error = None
    changed_location = False
    print(f"Location Spoofer companion running for {device}.", flush=True)
    print("Choose a location or tap Disable in the iPhone app. Ctrl+C stops the companion and clears simulation.", flush=True)
    with tempfile.TemporaryDirectory(prefix="location-spoofer-") as directory:
        command_path = Path(directory) / "location-request.json"
        receipt_path = Path(directory) / "location-response.json"
        while not stopped:
            heartbeat()
            try:
                run(device, "copy", "from", "--domain-type", "appDataContainer",
                          "--domain-identifier", APP_ID, "--source", "Documents/location-request.json",
                          "--destination", str(command_path))
                if command_path.stat().st_size > 8192:
                    raise ValueError("App command is too large.")
                request = validate_request(json.loads(command_path.read_text()))
                if request["id"] != last_id:
                    receipt = apply_request(device, request, run)
                    if receipt["success"]:
                        changed_location = request["enabled"]
                    receipt_path.write_text(json.dumps(receipt))
                    run(device, "copy", "to", "--domain-type", "appDataContainer",
                              "--domain-identifier", APP_ID, "--source", str(receipt_path),
                              "--destination", "Documents/location-response.json")
                    last_id = request["id"]
                    if receipt["success"]:
                        message = (f"Applied {request.get('name') or 'location'}: "
                                   f"{request['latitude']}, {request['longitude']}"
                                   if request["enabled"] else "Cleared simulation; real location restored.")
                    else:
                        message = receipt["error"]
                    print(message, flush=True)
                    if once:
                        return 0 if receipt["success"] else 1
                last_error = None
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
                message = str(error)
                error_key = message.splitlines()[0] if message else type(error).__name__
                if error_key != last_error:
                    print(f"Waiting for app/device: {message}", flush=True)
                    last_error = error_key
                if once:
                    return 1
            time.sleep(1)
    if changed_location:
        try:
            run(device, "simulate", "location", "clear")
            print("Companion stopped; location simulation cleared.", flush=True)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"Could not clear simulation: {error}. Reconnect and run with --clear.", flush=True)
            return 1
    return 0


def main():
    from tailscale_bridge import CONFIG, configure
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG, help="Remote configuration for this phone; use a separate file for each iPhone.")
    parser.add_argument("--device", help="Paired iPhone name or UDID; auto-detected if exactly one physical iPhone is connected.")
    parser.add_argument("--once", action="store_true", help="Apply one pending command, then exit without clearing it.")
    parser.add_argument("--clear", action="store_true", help="Stop simulation and exit.")
    parser.add_argument("--configure-tailscale", action="store_true", help="Save this paired iPhone's discovery information and tailnet address.")
    parser.add_argument("--tailscale-peer", help="iPhone's Tailscale IP or full MagicDNS name.")
    parser.add_argument("--service-identifier", help="Bonjour identifier when multiple iPhones are present.")
    parser.add_argument("--listen-address", help="A distinct IPv4 address assigned to this Mac for this phone's relay.")
    parser.add_argument("--tunnel-ports", default="49152,55000-56000", help="Apple pairing and tunnel ports relayed over Tailscale.")
    parser.add_argument("--local-only", action="store_true", help="Do not run the configured Tailscale bridge.")
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    saved_config = json.loads(args.config.read_text()) if args.config.exists() else None
    device = args.device
    if not device and saved_config:
        device = saved_config["device"]
    if not device:
        with tempfile.TemporaryDirectory(prefix="location-spoofer-devices-") as directory:
            output = Path(directory) / "devices.json"
            subprocess.run(["xcrun", "devicectl", "list", "devices", "--json-output", str(output)],
                           check=True, capture_output=True, timeout=20)
            devices = json.loads(output.read_text())["result"]["devices"]
            phones = [d for d in devices if d.get("hardwareProperties", {}).get("reality") == "physical"
                      and d.get("hardwareProperties", {}).get("deviceType") == "iPhone"
                      and d.get("connectionProperties", {}).get("pairingState") == "paired"
                      and d.get("connectionProperties", {}).get("transportType") in ("wired", "localNetwork", "usb")]
            if len(phones) != 1:
                parser.error("Connect one paired iPhone, or pass --device with its name or UDID.")
            device = phones[0]["identifier"]
    if args.configure_tailscale:
        configure(device, args.tailscale_peer, args.service_identifier, args.tunnel_ports,
                  path=args.config, listen_address=args.listen_address)
        return 0
    if args.clear:
        print(devicectl(device, "simulate", "location", "clear"))
        return 0
    # Serialize commands per phone while allowing independent companion services.
    with open(lock_path(device), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Location Spoofer companion is already running for {device}.")
            return 0
        lock.write(str(os.getpid()))
        lock.flush()
        bridge = None
        awake = None
        try:
            if saved_config and not args.local_only:
                if saved_config["device"] != device:
                    parser.error("Remote configuration belongs to another iPhone. Run --configure-tailscale for this device.")
                bridge = subprocess.Popen([sys.executable, "-u", str(Path(__file__).with_name("tailscale_bridge.py")),
                                           "--config", str(args.config), "--parent-pid", str(os.getpid())])
                awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            def supervise_bridge():
                nonlocal bridge
                if bridge and bridge.poll() is not None:
                    print("Remote bridge exited; restarting it.", flush=True)
                    bridge = subprocess.Popen([sys.executable, "-u", str(Path(__file__).with_name("tailscale_bridge.py")),
                                               "--config", str(args.config), "--parent-pid", str(os.getpid())])
            return serve(device, args.once, heartbeat=supervise_bridge)
        finally:
            for process in (bridge, awake):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
