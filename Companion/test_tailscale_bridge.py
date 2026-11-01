import asyncio
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tailscale_bridge import DatagramRelay, RelayListeners, ensure_file_capacity, parse_ports, parse_records, relay_address, relay_hostname, relay_tcp, tailnet_host, tunnel_port, watch_tunnel_ports


class ConfigurationTests(unittest.TestCase):
    def test_apple_tunnel_endpoint_is_scoped_to_this_phones_relay_address(self):
        event = {
            "subsystem": "com.apple.dt.remotepairing",
            "category": "networktunnelmanager",
            "eventMessage": "tunnel-709: Got tunnel endpoint: '192.168.178.34%en1:58323', includePeerToPeer: false",
        }
        self.assertEqual(tunnel_port(event, "192.168.178.34"), 58323)
        self.assertIsNone(tunnel_port(event, "100.100.185.71"))
        for change in ({"category": "unrelated"}, {"eventMessage": None},
                       {"eventMessage": "Got tunnel endpoint: '192.168.178.34:80'"},
                       {"eventMessage": "Got tunnel endpoint: '192.168.178.34:65536'"}):
            self.assertIsNone(tunnel_port(event | change, "192.168.178.34"))
        self.assertEqual(tunnel_port(event | {"eventMessage": "Got tunnel endpoint: '100.100.185.71:65535'"},
                                     "100.100.185.71"), 65535)

    def test_relay_cleans_up_after_its_companion_is_killed(self):
        child_code = f'''
import asyncio, sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})
import tailscale_bridge as bridge
marker = Path(sys.argv[2])
async def relay(_config):
    marker.with_suffix('.ready').write_text('ready')
    try:
        await asyncio.Event().wait()
    finally:
        marker.write_text('cleaned')
bridge.run_bridge = relay
asyncio.run(bridge.run_with_parent({{}}, int(sys.argv[1])))
'''
        parent_code = '''
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', sys.argv[1], str(os.getpid()), sys.argv[2]])
print(child.pid, flush=True)
time.sleep(60)
'''
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'cleanup'
            parent = subprocess.Popen([sys.executable, '-c', parent_code, child_code, str(marker)], stdout=subprocess.PIPE, text=True)
            child_pid = int(parent.stdout.readline())
            try:
                deadline = time.monotonic() + 4
                while not marker.with_suffix('.ready').exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.with_suffix('.ready').exists())
                parent.kill()
                parent.wait(timeout=3)
                deadline = time.monotonic() + 4
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(marker.read_text(), 'cleaned')
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=3)
                parent.stdout.close()
                if not marker.exists():
                    try:
                        os.kill(child_pid, 15)
                    except ProcessLookupError:
                        pass

    def test_launchd_file_limit_can_support_the_default_tunnel_window(self):
        original = resource.getrlimit(resource.RLIMIT_NOFILE)
        descriptors = []
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, original[1]))
            ports = parse_ports("49152,55000-56000")
            ensure_file_capacity(ports)
            for _ in range(2 * len(ports)):
                descriptors.append(os.open(os.devnull, os.O_RDONLY))
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            resource.setrlimit(resource.RLIMIT_NOFILE, original)

    def test_two_phones_keep_distinct_relay_addresses_and_discovery_hosts(self):
        first = {"device": "phone-one"}
        second = {"device": "phone-two", "listenAddress": "100.100.185.71"}
        with patch("tailscale_bridge.local_address", return_value="192.168.1.10"):
            self.assertEqual(relay_address(first), "192.168.1.10")
            self.assertEqual(relay_address(second), "100.100.185.71")
        self.assertNotEqual(relay_hostname(first), relay_hostname(second))
        for address in ("0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "host.local"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                relay_address({"listenAddress": address})

    def test_discovery_keeps_the_existing_pairing_identifier_and_txt(self):
        zone = '''
_remotepairing._tcp PTR paired-phone._remotepairing._tcp
paired-phone._remotepairing._tcp SRV 0 0 49152 Phone.local. ; comment
paired-phone._remotepairing._tcp TXT "identifier=paired-phone" "authTag=hint" "ver=26" "minVer=8" "flags=0"
'''
        self.assertEqual(parse_records(zone), [{"name": "paired-phone", "port": 49152,
            "originalHost": "Phone.local.", "txt": ["identifier=paired-phone", "authTag=hint", "ver=26", "minVer=8", "flags=0"]}])

    def test_only_tailnet_destinations_are_accepted(self):
        for host in ("100.64.0.10", "fd7a:115c:a1e0::1", "phone.example.ts.net."):
            self.assertEqual(tailnet_host(host), host.rstrip("."))
        for host in ("example.org", "192.168.1.2", "127.0.0.1", "8.8.8.8", "::1", "100.128.0.1", "x.ts.net/elsewhere"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                tailnet_host(host)

    def test_ranges_are_bounded_and_deduplicated(self):
        self.assertEqual(parse_ports("49152,55000-55002,55001"), [49152, 55000, 55001, 55002])
        for value in ("80", "65536", "56000-55000", "1024-65535", "55000-55001-55002"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ports(value)


class UDPEcho(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        self.transport.sendto(data, address)


class UDPClient(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()

    def datagram_received(self, data, _address):
        self.received.put_nowait(data)


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_negotiated_port_outside_fixed_window_forwards_tcp_and_udp_on_retry(self):
        async def echo(reader, writer):
            while line := await reader.readline():
                writer.write(b"response:" + line)
                await writer.drain()
            writer.close()
            await writer.wait_closed()
        tcp = await asyncio.start_server(echo, "127.0.0.1", 0)
        tcp_port = tcp.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(UDPEcho, local_addr=("127.0.0.1", 0))
        udp_port = udp.get_extra_info("sockname")[1]
        relays = RelayListeners("127.0.0.1", "127.0.0.1", set())
        client_transport = None
        writer = None
        monitor = None
        children = []
        spawn = asyncio.create_subprocess_exec
        async def forward_tcp(reader, writer, host, port, allowed):
            await relay_tcp(reader, writer, host, port, allowed, tcp_port)
        # Reproduce the captured Apple endpoint, then its next retry port. The
        # fixture peer uses separate local ports so it can run on the same Mac.
        event = json.loads('{"subsystem":"com.apple.dt.remotepairing","category":"networktunnelmanager",'
                           '"eventMessage":"tunnel-709: Got tunnel endpoint: \'127.0.0.1%lo0:58323\', includePeerToPeer: false"}')
        async def log_fixture(*_args, **kwargs):
            child = await spawn(sys.executable, "-u", "-c",
                "import sys; print('Filtering the log data'); print(sys.argv[1]); "
                "exec('for line in sys.stdin:\\n print(line, end=\"\", flush=True)')",
                json.dumps(event), stdin=asyncio.subprocess.PIPE, **kwargs)
            children.append(child)
            return child
        async def wait_for_port(port):
            async with asyncio.timeout(3):
                while port not in relays.servers:
                    if monitor.done():
                        await monitor
                    await asyncio.sleep(.01)
        try:
            with self.assertRaises(ConnectionRefusedError):
                await asyncio.open_connection("127.0.0.1", 58324)
            with patch("tailscale_bridge.relay_tcp", new=forward_tcp), \
                 patch("tailscale_bridge.DatagramRelay", side_effect=lambda h, p, a:
                       DatagramRelay(h, p, a, udp_port)), \
                 patch("tailscale_bridge.asyncio.create_subprocess_exec", new=log_fixture):
                monitor = asyncio.create_task(watch_tunnel_ports("127.0.0.1", relays))
                await wait_for_port(58324)
                reader, writer = await asyncio.open_connection("127.0.0.1", 58324)
                writer.write(b"outside-default-window\n")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 3), b"response:outside-default-window\n")
                client_transport, client = await loop.create_datagram_endpoint(UDPClient, remote_addr=("127.0.0.1", 58325))
                client_transport.sendto(b"dynamic-udp")
                self.assertEqual(await asyncio.wait_for(client.received.get(), 2), b"dynamic-udp")
                # Moving to another negotiated window retires unused sockets,
                # while preserving the UDP flow that still has a live client.
                next_event = event | {"eventMessage": "Got tunnel endpoint: '127.0.0.1%lo0:62000'"}
                children[0].stdin.write((json.dumps(next_event) + "\n").encode())
                await children[0].stdin.drain()
                await wait_for_port(62000)
                self.assertNotIn(58323, relays.servers)
                self.assertNotIn(58324, relays.servers)
                self.assertIn(58325, relays.servers)
                client_transport.sendto(b"still-connected")
                self.assertEqual(await asyncio.wait_for(client.received.get(), 2), b"still-connected")
                writer.write(b"tcp-still-connected\n")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 3), b"response:tcp-still-connected\n")
                self.assertLessEqual(len(relays.servers), 129)
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                monitor = None
                await asyncio.wait_for(relays.close(), 3)
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if client_transport:
                client_transport.close()
            if writer:
                writer.close()
                await writer.wait_closed()
            await relays.close()
            tcp.close()
            await tcp.wait_closed()
            udp.close()
        self.assertEqual(children[0].returncode, -15)

    async def test_tcp_keeps_the_response_after_client_half_close(self):
        async def server(reader, writer):
            writer.write(b"response:" + await reader.read())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        upstream = await asyncio.start_server(server, "127.0.0.1", 0)
        target = upstream.sockets[0].getsockname()[1]
        bridge = await asyncio.start_server(lambda r, w: relay_tcp(r, w, "127.0.0.1", 0, {"127.0.0.1"}, target), "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.sockets[0].getsockname()[1])
            payload = bytes(range(256)) * 1024
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"response:" + payload)
            writer.close()
            await writer.wait_closed()
        finally:
            bridge.close()
            upstream.close()
            await bridge.wait_closed()
            await upstream.wait_closed()

    async def test_non_local_tcp_clients_cannot_use_the_relay(self):
        bridge = await asyncio.start_server(lambda r, w: relay_tcp(r, w, "127.0.0.1", 1, set()), "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.sockets[0].getsockname()[1])
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            writer.close()
            await writer.wait_closed()
        finally:
            bridge.close()
            await bridge.wait_closed()

    async def test_udp_preserves_datagrams_and_separates_clients(self):
        loop = asyncio.get_running_loop()
        upstream, _ = await loop.create_datagram_endpoint(UDPEcho, local_addr=("127.0.0.1", 0))
        target = upstream.get_extra_info("sockname")[1]
        bridge, protocol = await loop.create_datagram_endpoint(lambda: DatagramRelay("127.0.0.1", 0, {"127.0.0.1"}, target), local_addr=("127.0.0.1", 0))
        address = bridge.get_extra_info("sockname")
        clients = []
        try:
            for name in (b"one", b"two"):
                transport, client = await loop.create_datagram_endpoint(UDPClient, remote_addr=address)
                clients.append(transport)
                transport.sendto(name + b"-first")
                transport.sendto(name + b"-second")
                self.assertEqual(await asyncio.wait_for(client.received.get(), 2), name + b"-first")
                self.assertEqual(await asyncio.wait_for(client.received.get(), 2), name + b"-second")
            self.assertEqual(len(protocol.clients), 2)
        finally:
            for client in clients:
                client.close()
            protocol.close()
            upstream.close()
            await asyncio.sleep(0)
