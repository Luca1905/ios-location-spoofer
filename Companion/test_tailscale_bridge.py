import asyncio
import unittest

from tailscale_bridge import DatagramRelay, parse_ports, parse_records, relay_tcp, tailnet_host


class ConfigurationTests(unittest.TestCase):
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
