import os
import socket
import tempfile
import unittest

from mpd_jukebox.systemd import notify


class NotifyTestCase(unittest.TestCase):
    def test_not_under_systemd(self):
        self.assertFalse(notify('READY=1', {}))

    def test_path_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'notify')
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.bind(path)
                self.assertTrue(notify('READY=1', {'NOTIFY_SOCKET': path}))
                self.assertEqual(sock.recv(100), b'READY=1')

    def test_abstract_socket(self):
        name = f'mpd-jukebox-test-{os.getpid()}'
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind('\0' + name)
            self.assertTrue(notify('READY=1', {'NOTIFY_SOCKET': '@' + name}))
            self.assertEqual(sock.recv(100), b'READY=1')

    def test_unreachable(self):
        self.assertFalse(
            notify('READY=1', {'NOTIFY_SOCKET': '/nonexistent/socket'})
        )
