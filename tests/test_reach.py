import socket
import threading
import unittest
from unittest.mock import patch

from kindle_drop import reach


def server(reply):
    """A one-shot TCP server on 127.0.0.1 that sends reply to its first client."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(1)

    def serve():
        client, _ = sock.accept()
        client.sendall(reply)
        client.close()
        sock.close()
    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


class PingTests(unittest.TestCase):
    def ping(self, port, **kwargs):
        # Production validation only accepts private LAN addresses; loopback stands in here.
        with patch('kindle_drop.reach.validate_kindle', lambda cfg: cfg):
            return reach.ping({'host': '127.0.0.1', 'port': port}, **kwargs)

    def test_ssh_banner_means_ready(self):
        ready, message = self.ping(server(b'SSH-2.0-dropbear_2025.88 (KOReader)\r\n'))
        self.assertTrue(ready)
        self.assertIn('dropbear', message)

    def test_other_service_is_not_ready(self):
        ready, message = self.ping(server(b'HTTP/1.1 400 Bad Request\r\n'))
        self.assertFalse(ready)
        self.assertIn('not with SSH', message)

    def test_closed_port_says_start_the_ssh_server(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens: connection refused
        ready, message = self.ping(port)
        self.assertFalse(ready)
        self.assertIn('SSH server', message)

    def test_silence_distinguishes_awake_from_unreachable(self):
        with patch('kindle_drop.reach.socket.create_connection', side_effect=TimeoutError()):
            with patch('kindle_drop.reach.icmp_ping', return_value=True):
                self.assertIn('awake on Wi-Fi', self.ping(2222)[1])
            with patch('kindle_drop.reach.icmp_ping', return_value=False):
                self.assertIn('No answer', self.ping(2222)[1])

    def test_only_private_lan_addresses_are_probed(self):
        with self.assertRaises(ValueError):
            reach.ping({'host': '8.8.8.8', 'port': 22})


if __name__ == '__main__':
    unittest.main()
