import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

import asyncssh

import server


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.key = asyncssh.generate_private_key('ssh-ed25519')
        authorized_keys = Path(directory.name) / 'authorized_keys'
        authorized_keys.write_bytes(self.key.export_public_key())
        self.listener = await asyncssh.listen(
            '127.0.0.1', 0, server_factory=server.SSHServer,
            server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],
            authorized_client_keys=str(authorized_keys),
            process_factory=server.shell, encoding=None,
            sftp_factory=asyncssh.SFTPServer, allow_scp=True)
        self.addAsyncCleanup(self.close_listener, self.listener)

    async def close_listener(self, listener):
        listener.close()
        await listener.wait_closed()

    async def connect(self, keys):
        connection = await asyncio.wait_for(asyncssh.connect(
            '127.0.0.1', self.listener.get_port(), username='test',
            client_keys=keys, agent_path=None, config=None,
            known_hosts=None), 5)
        self.addAsyncCleanup(self.close_listener, connection)
        return connection

    async def test_authorized_key(self):
        await self.connect([self.key])

    async def test_unauthorized_key(self):
        with self.assertRaises(asyncssh.PermissionDenied):
            await self.connect([asyncssh.generate_private_key('ssh-ed25519')])

    async def test_no_key(self):
        with self.assertRaises(asyncssh.PermissionDenied):
            await self.connect([])

    async def echo(self, reader, writer):
        try:
            writer.write(await reader.readexactly(4))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def check_forwarding(self, host):
        echo_server = await asyncio.start_server(self.echo, host, 0)
        self.addAsyncCleanup(self.close_listener, echo_server)
        connection = await self.connect([self.key])
        tunnel = await connection.forward_local_port(
            '127.0.0.1', 0, host, echo_server.sockets[0].getsockname()[1])
        self.addAsyncCleanup(self.close_listener, tunnel)
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', tunnel.get_port())
        try:
            writer.write(b'ping')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 5),
                             b'ping')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_ipv4_local_forwarding(self):
        await self.check_forwarding('127.0.0.1')

    async def test_ipv6_local_forwarding(self):
        if not socket.has_ipv6:
            self.skipTest('IPv6 unavailable')
        await self.check_forwarding('::1')

    async def test_rejected_destinations(self):
        connection = await self.connect([self.key])
        for host in ('192.0.2.1', '0.0.0.0', 'localhost', 'example.invalid',
                     '127.0.0.2', '::', '::ffff:127.0.0.1'):
            with self.subTest(host=host):
                with self.assertRaises(asyncssh.ChannelOpenError) as error:
                    await asyncio.wait_for(connection.open_connection(host, 9), 5)
                self.assertEqual(error.exception.reason, 'Connection refused')

    async def test_remote_forwarding_rejected(self):
        connection = await self.connect([self.key])
        with self.assertRaises(asyncssh.ChannelListenError):
            await connection.forward_remote_port('127.0.0.1', 0, '127.0.0.1', 9)

    async def test_shell_output_and_exitcode(self):
        connection = await self.connect([self.key])
        result = await asyncio.wait_for(connection.run(
            "printf stdout; printf stderr >&2; exit 7", input=b'',
            encoding=None), 5)
        self.assertTrue(result.stdout.endswith(b'stdout'))
        self.assertTrue(result.stderr.endswith(b'stderr'))
        self.assertEqual(result.exit_status, 7)


if __name__ == '__main__':
    unittest.main()