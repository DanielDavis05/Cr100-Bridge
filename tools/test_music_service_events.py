"""Protocol checks for the original controller's encoded account events."""
import base64
import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

sys.path.insert(0, str(Path(__file__).resolve().parent))
from music_service_events import encode_media_servers


HOUSEHOLD = 'Sonos_EMULATED_CR100'
IV = bytes(range(16))
# Fixed derivation values recovered from the native algorithm. The ciphertext
# was separately generated with `openssl enc -aes-128-cbc -K KEY -iv IV`.
KEY = bytes.fromhex('4377cbe6daff2a99fdbcb25febf5dfe6')
VECTOR = '2:AAECAwQFBgcICQoLDA0OD4cEXtLKjB8oKV/ooCNSX//Jxsp5mYdM7AbiCWEBPVEc'


class MusicServiceEvents(unittest.TestCase):
    def test_known_wire_vector(self):
        self.assertEqual(encode_media_servers('<MediaServers/>', HOUSEHOLD, iv=IV), VECTOR)

    def test_wire_contains_utf8_xml_and_native_checksum(self):
        # Use a fixed independently calculated key, so a matching encoder and
        # decoder derivation mistake cannot make this check pass.
        for xml in ('', '<MediaServers/>', '<MediaServers><Service Name="Café &amp; 日本"/></MediaServers>',
                    '<MediaServers>' + 'x' * 2048 + '</MediaServers>'):
            with self.subTest(length=len(xml)):
                wire = encode_media_servers(xml, HOUSEHOLD, iv=IV)
                self.assertEqual(wire[:2], '2:')
                blob = base64.b64decode(wire[2:], validate=True)
                self.assertEqual(blob[:16], IV)
                self.assertEqual(len(blob[16:]) % 16, 0)
                decryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
                padded = decryptor.update(blob[16:]) + decryptor.finalize()
                unpadder = PKCS7(128).unpadder()
                plaintext = unpadder.update(padded) + unpadder.finalize()
                data = xml.encode('utf-8')
                self.assertEqual(plaintext[:-4], data)
                self.assertEqual(plaintext[-4:], hashlib.md5(data).digest()[:4])

    def test_random_iv_and_household_binding(self):
        with patch('music_service_events.secrets.token_bytes', side_effect=[IV, bytes(reversed(IV))]) as random:
            first = encode_media_servers('<MediaServers/>', HOUSEHOLD)
            second = encode_media_servers('<MediaServers/>', HOUSEHOLD)
        self.assertEqual(random.call_args_list, [call(16), call(16)])
        self.assertEqual(first, VECTOR)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, encode_media_servers('<MediaServers/>', 'Sonos_OTHER', iv=IV))

    def test_rejects_missing_household_and_invalid_inputs(self):
        with self.assertRaises(ValueError):
            encode_media_servers('<MediaServers/>', '')
        for invalid in (b'<MediaServers/>', None):
            with self.assertRaises(TypeError):
                encode_media_servers(invalid, HOUSEHOLD)
        for invalid in (b'Sonos_EMULATED_CR100', None):
            with self.assertRaises(TypeError):
                encode_media_servers('<MediaServers/>', invalid)
        for invalid in (b'', b'short', bytes(17), '0' * 16):
            with self.assertRaises(ValueError):
                encode_media_servers('<MediaServers/>', HOUSEHOLD, iv=invalid)


if __name__ == '__main__':
    unittest.main()
