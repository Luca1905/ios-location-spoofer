#!/usr/bin/env python3
"""Relay Apple's paired-device discovery and tunnel ports over a private tailnet.

Apple's authenticated pairing protocol remains end to end. Only connections from
this Mac are accepted by the relay; it is not a public device or location API.
"""
import argparse
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import resource
import signal
import socket
import subprocess
import time

CONFIG = Path.home() / "Library/Application Support/Location Spoofer/tailscale.json"
SERVICE = "_remotepairing._tcp"
# Apple allocates a second tunnel port after connecting to the pairing service.
# This is configurable because Apple does not publish a stable port allocation API.
DEFAULT_PORTS = "49152,55000-56000"
MAX_RELAY_PORTS = 4096
DYNAMIC_WINDOW = 128
TUNNEL_LOG_PREDICATE = ('subsystem == "com.apple.dt.remotepairing" AND '
                        'category == "networktunnelmanager" AND '
                        'eventMessage CONTAINS "Got tunnel endpoint:"')


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
    if len(ports) > MAX_RELAY_PORTS:
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


def configure(device, peer=None, service_id=None, ports=DEFAULT_PORTS, path=CONFIG, listen_address=None):
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
    if listen_address:
        config["listenAddress"] = relay_address({"listenAddress": listen_address})
        # Fail during setup if this is not an address assigned to this Mac.
        with socket.socket() as probe:
            probe.bind((config["listenAddress"], 0))
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


def relay_address(config):
    value = config.get("listenAddress")
    if value is None:
        return local_address()
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ValueError("Choose a specific IPv4 address assigned to this Mac.")
    return str(address)


def relay_hostname(config):
    identifier = hashlib.sha256(config["device"].casefold().encode()).hexdigest()[:12]
    return f"location-spoofer-remote-{identifier}.local."


def ensure_file_capacity(ports):
    # launchd commonly starts services with only 256 available descriptors.
    # Each tunnel port needs both a TCP and a UDP socket, plus connection space.
    needed = 2 * len(ports) + 256
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= needed:
        return
    if hard != resource.RLIM_INFINITY and hard < needed:
        raise ValueError(f"The relay needs at least {needed} file descriptors; reduce --tunnel-ports or increase the service's file limit.")
    resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))


def tunnel_port(event, address):
    """Only follow Apple's negotiated endpoint for this phone's relay address."""
    if not isinstance(event, dict) or event.get("subsystem") != "com.apple.dt.remotepairing" or event.get("category") != "networktunnelmanager":
        return None
    message = event.get("eventMessage")
    if not isinstance(message, str):
        return None
    match = re.search(r"Got tunnel endpoint: '([0-9.]+)(?:%[^:']+)?:([0-9]+)'", message)
    if match and match[1] == address and 49152 <= int(match[2]) <= 65535:
        return int(match[2])
    return None


class RelayListeners:
    """Fixed pairing ports plus a bounded window around Apple's tunnel port."""
    def __init__(self, host, address, ports):
        self.host, self.address = host, address
        self.allowed = {address, "127.0.0.1"}
        self.fixed, self.dynamic = set(ports), set()
        self.servers, self.datagrams, self.tasks = {}, {}, set()
        self.retired = set()
        self.lock = asyncio.Lock()

    def _retire(self, server):
        server.close()
        # wait_closed also waits for accepted TCP streams. Do not block opening
        # the next window while Apple's existing tunnel is still connected.
        waiter = asyncio.create_task(server.wait_closed())
        self.retired.add(waiter)
        waiter.add_done_callback(self.retired.discard)

    async def _open(self, port):
        if port in self.servers:
            return
        if len(self.servers) >= MAX_RELAY_PORTS:
            raise OSError("Relay socket limit reached; reduce the configured fixed port range.")
        def connected(reader, writer):
            task = asyncio.create_task(relay_tcp(reader, writer, self.host, port, self.allowed))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        server = await asyncio.start_server(connected, self.address, port)
        try:
            _transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: DatagramRelay(self.host, port, self.allowed), local_addr=(self.address, port))
        except BaseException:
            self._retire(server)
            raise
        self.servers[port], self.datagrams[port] = server, protocol

    async def _open_ports(self, ports):
        ensure_file_capacity(range(min(MAX_RELAY_PORTS, len(self.servers) + len(ports))))
        failed = []
        for port in sorted(ports):
            try:
                await self._open(port)
            except OSError:
                failed.append(port)
        return failed

    async def open_fixed(self):
        async with self.lock:
            return await self._open_ports(self.fixed)

    async def _prune(self):
        for port in list(self.servers):
            protocol = self.datagrams[port]
            if port not in self.fixed | self.dynamic and not protocol.clients and not protocol.pending:
                # Closing a TCP listener preserves its already accepted streams.
                self._retire(self.servers.pop(port))
                self.datagrams.pop(port).close()

    async def follow_port(self, port):
        if port is None or not 49152 <= port <= 65535:
            return None
        async with self.lock:
            if port in self.servers:
                return None
            # Apple may allocate a new port on retry. Include nearby subsequent
            # ports, so the first refused connection can recover automatically.
            first = max(49152, min(port - 8, 65536 - DYNAMIC_WINDOW))
            self.dynamic = set(range(first, first + DYNAMIC_WINDOW))
            await self._prune()
            failed = await self._open_ports(self.dynamic)
            return first, first + DYNAMIC_WINDOW - 1, failed

    async def expire(self):
        async with self.lock:
            for protocol in self.datagrams.values():
                protocol.expire()
            await self._prune()

    async def close(self):
        for server in self.servers.values():
            self._retire(server)
        for protocol in self.datagrams.values():
            protocol.close()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.retired:
            await asyncio.gather(*list(self.retired))
        self.servers.clear()
        self.datagrams.clear()


async def watch_tunnel_ports(address, listeners):
    """Observe public endpoint metadata without decrypting Apple's protocol."""
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/log", "stream", "--style", "ndjson", "--level", "info",
        "--predicate", TUNNEL_LOG_PREDICATE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        while line := await process.stdout.readline():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # log prints a non-JSON filtering banner on startup.
            port = tunnel_port(event, address)
            window = await listeners.follow_port(port)
            if window:
                first, last, failed = window
                print(f"Following Apple tunnel port {port} for {listeners.host}: relaying {first}-{last}.", flush=True)
                if failed:
                    print(f"Some negotiated tunnel ports are occupied: {failed[:12]}.", flush=True)
        raise OSError(f"Apple tunnel endpoint monitor exited ({await process.wait()}); reconnecting.")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def run_bridge(config):
    host = tailnet_host(config["peer"])
    ports = parse_ports(config.get("ports", DEFAULT_PORTS))
    ensure_file_capacity(ports)
    record = config["service"]
    if record["port"] not in ports:
        raise ValueError("The discovery port must be included in relay ports.")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    last_error = None
    while not stopped.is_set():
        listeners, monitor, registration = None, None, None
        try:
            # Advertise only when the remote phone actually accepts pairing traffic.
            _reader, probe = await asyncio.wait_for(asyncio.open_connection(host, record["port"]), 3)
            probe.close()
            await probe.wait_closed()
            address = relay_address(config)
            listeners = RelayListeners(host, address, ports)
            failed = await listeners.open_fixed()
            if record["port"] in failed:
                raise OSError("Apple's pairing relay port is in use. Stop the conflicting relay before retrying.")
            if failed:
                print(f"Some tunnel ports are occupied: {failed[:12]}. If pairing fails, retry or close the conflicting app.", flush=True)
            monitor = asyncio.create_task(watch_tunnel_ports(address, listeners))
            registration = subprocess.Popen(["dns-sd", "-P", record["name"], SERVICE, "local.",
                str(record["port"]), relay_hostname(config), address, *record["txt"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Tailscale bridge ready: {host}. Apple pairing remains end to end.", flush=True)
            last_error = None
            while not stopped.is_set():
                if registration.poll() is not None:
                    raise OSError("Bonjour registration exited; reconnecting.")
                if monitor.done():
                    await monitor
                await listeners.expire()
                try:
                    await asyncio.wait_for(stopped.wait(), 5)
                except asyncio.TimeoutError:
                    if relay_address(config) != address:
                        raise OSError("Mac network changed; reconnecting the relay.")
                    # Recheck reachability; don't leave a dead phone advertised.
                    _reader, probe = await asyncio.wait_for(asyncio.open_connection(host, record["port"]), 3)
                    probe.close()
                    await probe.wait_closed()
        except (OSError, asyncio.TimeoutError, KeyError, subprocess.TimeoutExpired) as error:
            message = str(error) or "iPhone is not reachable over Tailscale."
            if message != last_error:
                print(f"Remote connection waiting: {message} Keep Tailscale on and the iPhone on Wi-Fi.", flush=True)
                last_error = message
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if registration and registration.poll() is None:
                registration.terminate()
                try:
                    await asyncio.to_thread(registration.wait, timeout=3)
                except subprocess.TimeoutExpired:
                    registration.kill()
                    await asyncio.to_thread(registration.wait)
            if listeners:
                await listeners.close()
        if not stopped.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stopped.wait(), 5)


async def run_with_parent(config, parent_pid=None):
    if parent_pid is None:
        return await run_bridge(config)
    bridge = asyncio.create_task(run_bridge(config))
    try:
        while not bridge.done():
            if os.getppid() != parent_pid:
                print("Companion exited; closing its Tailscale relay.", flush=True)
                break
            await asyncio.sleep(1)
    finally:
        if not bridge.done():
            bridge.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--parent-pid", type=int, help="Exit and release relay ports if the owning companion exits.")
    args = parser.parse_args()
    asyncio.run(run_with_parent(json.loads(args.config.read_text()), args.parent_pid))


if __name__ == "__main__":
    main()
