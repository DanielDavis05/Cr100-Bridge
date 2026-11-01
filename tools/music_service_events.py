"""Encode the CR100's ZoneGroupTopology ThirdPartyMediaServersX value.

The controller parses account XML only on the X event path. Its legacy plain
ThirdPartyMediaServers path passes a string through without constructing the
media-server objects used by the Music menu.

Type 2 wire format is ``2:`` followed by standard base64 of IV || ciphertext.
The 16-byte household password is MD5(UTF-8 household ID || HOUSEHOLD_PEPPER).
The AES-128 key is MD5(IV || household password). AES-CBC uses the same random
16-byte IV, with PKCS#7 padding, and encrypts UTF-8 XML followed by the first
four bytes of MD5(XML). These legacy algorithms are required by the firmware.

Recovered from the original CR100 4.3 hh.anacapad: event dispatch 0x4334a0
(X) / 0x4335e0 (plain), household derivation 0x469b12, cipher key derivation
0x54fc60, checksum append 0x54f780, and version/base64 wrapper 0x5503a0.
The 4.3.2 X path at 0x4334e2 calls decoder 0x5507a0; the codec is unchanged.
The player 34.16 encoder independently matches at 0x5aa740 / 0x612e20.
"""
from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


HOUSEHOLD_PEPPER = bytes.fromhex('1a01a731c96e9ebde8475182b274b70e')


def encode_media_servers(xml: str, household: str, *, iv: bytes | None = None) -> str:
    """Return an encoded account XML event for the paired household.

    A fresh IV is generated for each call. The optional explicit IV exists for
    reproducible protocol vectors; callers normally leave it unspecified.
    """
    if not isinstance(xml, str) or not isinstance(household, str):
        raise TypeError('XML and household ID must be strings')
    if not household:
        raise ValueError('Household ID must not be empty')
    if iv is None:
        iv = secrets.token_bytes(16)
    if not isinstance(iv, bytes) or len(iv) != 16:
        raise ValueError('IV must be exactly 16 bytes')

    data = xml.encode('utf-8')
    password = hashlib.md5(household.encode('utf-8') + HOUSEHOLD_PEPPER).digest()
    key = hashlib.md5(iv + password).digest()
    plaintext = data + hashlib.md5(data).digest()[:4]
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return '2:' + base64.b64encode(iv + ciphertext).decode('ascii')
