#!/usr/bin/env python3
"""Relay Apple's paired-device discovery and tunnel ports over a private tailnet.

Apple's authenticated pairing protocol remains end to end. Only connections from
this Mac are accepted by the relay; it is not a public device or location API.
"""
import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time

CONFIG = Path.home() / "Library/Application Support/Location Spoofer/tailscale.json"
SERVICE = "_remotepairing._tcp"
# Apple allocates a second tunnel port after connecting to the pairing service.
# This is configurable because Apple does not publish a stable port allocation API.
DEFAULT_PORTS = "49152,55000-56000"


def parse_ports(value):
    ports = set()
    for part in value.split(","):
        bounds = part.strip().split("-")
        if len(bounds) not in (1, 2):
            raise ValueError("Use comma-separated ports or ranges.")
        first, last = int(bounds[0]), int(bounds[-1])
        if not 1024 <= first <= last <= 65535:
            raise ValueError("Relay ports must be between 1024 and 65535.")
        ports.update(range(first, last + 1))
    if len(ports) > 4096:
        raise ValueError("Choose at most 4096 relay ports to preserve local networking resources.")
    return sorted(ports)


def tailnet_host(value):
    """Accept a Tailscale address or MagicDNS hostname, never a public endpoint."""
    value = value.rstrip(".")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net", value):
            raise ValueError("Choose a Tailscale IP or a full .ts.net hostname.")
    else:
        if not (address in ipaddress.ip_network("100.64.0.0/10") if address.version == 4
                else address in ipaddress.ip_network("fd7a:115c:a1e0::/48")):
            raise ValueError("Choose a Tailscale IP, not a public or LAN address.")
    return value


def parse_records(zone):
    records = {}
    for line in zone.splitlines():
        match = re.match(r"(\S+)\._remotepairing\._tcp\s+(SRV|TXT)\s+(.*)", line)
        if not match:
            continue
        name, kind, payload = match.groups()
        record = records.setdefault(name, {"name": name})
        if kind == "SRV":
            fields = payload.split()
            record.update(port=int(fields[2]), originalHost=fields[3])
        else:
            record["txt"] = re.findall(r'"([^"\r\n]*)"', payload)
    return [r for r in records.values() if "port" in r and "txt" in r]


def configure(device, peer=None, service_id=None, ports=DEFAULT_PORTS, path=CONFIG):
    # dns-sd's own deadline avoids leaving a discovery process behind.
    discovery = subprocess.run(["dns-sd", "-t", "4", "-Z", SERVICE, "local."],
                               capture_output=True, text=True, timeout=8)
    records = parse_records(discovery.stdout)
    if service_id:
        records = [r for r in records if r["name"] == service_id]
    if len(records) != 1:
        raise ValueError("Connect just this paired iPhone on USB/Wi-Fi and retry setup. "
                         "With multiple phones, pass --service-identifier.")
    status = json.loads(subprocess.run(["tailscale", "status", "--json"], check=True,
                                      capture_output=True, text=True, timeout=10).stdout)
    if status.get("BackendState") != "Running":
        raise ValueError("Connect Tailscale on this Mac before setting up remote access.")
    if not peer:
        phones = [p for p in status.get("Peer", {}).values() if p.get("OS") == "iOS"]
        if len(phones) != 1:
            raise ValueError("Pass --tailscale-peer with this iPhone's Tailscale IP or MagicDNS name.")
        peer = phones[0]["DNSName"]
    config = {"version": 1, "device": device, "peer": tailnet_host(peer),
              "service": records[0], "ports": ports}
    if records[0]["port"] not in parse_ports(ports):
        raise ValueError("Include the iPhone's pairing port in --tunnel-ports.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
    print(f"Remote companion configured for {config['peer']}. Settings saved in {path}.")
    return config


async def copy_stream(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()


async def relay_tcp(reader, writer, host, port, allowed, remote_port=None):
    address = writer.get_extra_info("peername")
    upstream = None
    try:
        if not address or address[0] not in allowed:
            return
        remote_reader, upstream = await asyncio.wait_for(
            asyncio.open_connection(host, remote_port or port), 10)
        # Wait for both halves, preserving replies after the client half-closes.
        await asyncio.gather(copy_stream(reader, upstream), copy_stream(remote_reader, writer))
    except (OSError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        for stream in (writer, upstream):
            if stream:
                stream.close()
                with contextlib.suppress(OSError, asyncio.CancelledError):
                    await stream.wait_closed()


class DatagramReply(asyncio.DatagramProtocol):
    def __init__(self, relay, address):
        self.relay, self.address = relay, address
        self.last_used = time.monotonic()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, _address):
        self.last_used = time.monotonic()
        self.relay.transport.sendto(data, self.address)


class DatagramRelay(asyncio.DatagramProtocol):
    def __init__(self, host, port, allowed, remote_port=None):
        self.host, self.port, self.allowed = host, remote_port or port, allowed
        self.clients, self.pending = {}, {}
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if address[0] not in self.allowed:
            return
        client = self.clients.get(address)
        if client:
            client.last_used = time.monotonic()
            client.transport.sendto(data)
        elif address in self.pending:
            # Preserve initial QUIC packets while DNS/socket creation is pending.
            if len(self.pending[address][1]) < 32:
                self.pending[address][1].append(data)
        elif len(self.clients) + len(self.pending) < 64:
            queue = [data]
            task = asyncio.create_task(self.connect(address, queue))
            self.pending[address] = (task, queue)

    async def connect(self, address, queue):
        try:
            _transport, client = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: DatagramReply(self, address), remote_addr=(self.host, self.port))
            self.clients[address] = client
            for data in queue:
                client.transport.sendto(data)
        except OSError:
            pass
        finally:
            self.pending.pop(address, None)

    def expire(self):
        for address, client in list(self.clients.items()):
            if time.monotonic() - client.last_used > 120:
                client.transport.close()
                del self.clients[address]

    def close(self):
        for task, _queue in self.pending.values():
            task.cancel()
        for client in self.clients.values():
            client.transport.close()
        if self.transport:
            self.transport.close()


def local_address():
    # Follow network changes instead of retaining a stale home Wi-Fi address.
    route = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                           text=True, timeout=5).stdout
    match = re.search(r"interface:\s*(\S+)", route)
    if not match:
        raise OSError("Connect the Mac to a local network.")
    result = subprocess.run(["ipconfig", "getifaddr", match[1]], capture_output=True,
                            text=True, timeout=5)
    address = result.stdout.strip()
    if result.returncode or not address:
        raise OSError("The Mac needs an IPv4 address on its local network.")
    return str(ipaddress.IPv4Address(address))


async def run_bridge(config):
    host = tailnet_host(config["peer"])
    ports = parse_ports(config.get("ports", DEFAULT_PORTS))
    record = config["service"]
    if record["port"] not in ports:
        raise ValueError("The discovery port must be included in relay ports.")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    last_error = None
    while not stopped.is_set():
        servers, datagrams, tasks, registration = [], [], set(), None
        try:
            # Advertise only when the remote phone actually accepts pairing traffic.
            _reader, probe = await asyncio.wait_for(asyncio.open_connection(host, record["port"]), 3)
            probe.close()
            await probe.wait_closed()
            address = local_address()
            allowed = {address, "127.0.0.1"}
            def connected(reader, writer):
                task = asyncio.create_task(relay_tcp(reader, writer, host,
                    writer.get_extra_info("sockname")[1], allowed))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            failed = []
            for port in ports:
                # Bind only the Mac's address; never consume all wildcard ports.
                try:
                    server = await asyncio.start_server(connected, address, port)
                    servers.append(server)
                    transport, protocol = await loop.create_datagram_endpoint(
                        lambda p=port: DatagramRelay(host, p, allowed), local_addr=(address, port))
                    datagrams.append(protocol)
                except OSError:
                    failed.append(port)
            if record["port"] in failed:
                raise OSError("Apple's pairing relay port is in use. Stop the conflicting relay before retrying.")
            if failed:
                print(f"Some tunnel ports are occupied: {failed[:12]}. If pairing fails, retry or close the conflicting app.", flush=True)
            registration = subprocess.Popen(["dns-sd", "-P", record["name"], SERVICE, "local.",
                str(record["port"]), "location-spoofer-remote.local.", address, *record["txt"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Tailscale bridge ready: {host}. Apple pairing remains end to end.", flush=True)
            last_error = None
            while not stopped.is_set():
                if registration.poll() is not None:
                    raise OSError("Bonjour registration exited; reconnecting.")
                for protocol in datagrams:
                    protocol.expire()
                try:
                    await asyncio.wait_for(stopped.wait(), 5)
                except asyncio.TimeoutError:
                    if local_address() != address:
                        raise OSError("Mac network changed; reconnecting the relay.")
                    # Recheck reachability; don't leave a dead phone advertised.
                    _reader, probe = await asyncio.wait_for(asyncio.open_connection(host, record["port"]), 3)
                    probe.close()
                    await probe.wait_closed()
        except (OSError, asyncio.TimeoutError, KeyError) as error:
            message = str(error) or "iPhone is not reachable over Tailscale."
            if message != last_error:
                print(f"Remote connection waiting: {message} Keep Tailscale on and the iPhone on Wi-Fi.", flush=True)
                last_error = message
        finally:
            if registration and registration.poll() is None:
                registration.terminate()
                try:
                    await asyncio.to_thread(registration.wait, timeout=3)
                except subprocess.TimeoutExpired:
                    registration.kill()
                    await asyncio.to_thread(registration.wait)
            for server in servers:
                server.close()
                await server.wait_closed()
            for protocol in datagrams:
                protocol.close()
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
        if not stopped.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stopped.wait(), 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    asyncio.run(run_bridge(json.loads(args.config.read_text())))


if __name__ == "__main__":
    main()
