"""Interactive echo must not wait for the next SSH packet."""
import unittest
from unittest.mock import patch

from yhshell.ssh import Session


class TerminalEchoTests(unittest.TestCase):
    def setUp(self):
        self.session = Session({})

    def test_free_h_echoes_every_keystroke_immediately(self):
        for character in 'free -h':
            self.session._handle_bytes(character.encode())
            self.assertEqual(self.session.drain(), character)
        self.assertEqual(self.session._scan, b'')

    def test_full_command_and_prompt_have_no_delayed_tail(self):
        text = 'root@host:~# free -h'
        self.session._handle_bytes(text.encode())
        self.assertEqual(self.session.drain(), text)

    def test_split_utf8_is_decoded_without_delaying_complete_character(self):
        encoded = '中文'.encode()
        self.session._handle_bytes(encoded[:2])
        self.assertEqual(self.session.drain(), '')
        self.session._handle_bytes(encoded[2:3])
        self.assertEqual(self.session.drain(), '中')
        self.session._handle_bytes(encoded[3:])
        self.assertEqual(self.session.drain(), '文')

    def test_only_possible_handshake_prefix_is_buffered(self):
        self.session._handle_bytes(b'ls *')
        self.assertEqual(self.session.drain(), 'ls ')
        self.session._handle_bytes(b'.log')
        self.assertEqual(self.session.drain(), '*.log')
        self.assertEqual(self.session._scan, b'')

    def test_handshakes_detected_at_every_packet_boundary(self):
        for marker in (b'*\x18A', b'*\x18B', b'*\x18C'):
            payload = marker + b'header-data'
            for split in (1, 2):
                with self.subTest(marker=marker, split=split):
                    session = Session({})
                    with patch.object(session, '_begin_transfer') as begin:
                        session._handle_bytes(b'prompt ' + payload[:split])
                        session._handle_bytes(payload[split:])
                        self.assertEqual(session.drain(), 'prompt ')
                        begin.assert_called_once_with(payload)

    def test_invalid_header_is_output_and_scanning_continues(self):
        with patch.object(self.session, '_begin_transfer') as begin:
            self.session._handle_bytes(b'*\x18Xplain*\x18Bheader')
            self.assertEqual(self.session.drain(), '*\x18Xplain')
            begin.assert_called_once_with(b'*\x18Bheader')


if __name__ == '__main__':
    unittest.main()
