"""802.11 CCMP (AES-CCM, 8-byte MIC) for SonosNet frames, plus the 0x6969 tunnel parser.

The CR100 installs its network key as a shared default key in slot 2 (KeyID 2),
used for every frame in both directions (STATIC-FINDINGS §4). Data frames carry
SNAP AA AA 03 00 00 00 69 69 followed by a whole Ethernet frame (§5).
"""
from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

SNAP_PREFIX = bytes.fromhex('aaaa03000000')
TUNNEL_ETHERTYPES = (0x6969, 0x6971)


def header_length(frame):
    fc0, fc1 = frame[0], frame[1]
    length = 30 if (fc1 & 0x03) == 0x03 else 24
    if (fc0 & 0x0c) == 0x08 and fc0 & 0x80: length += 2  # QoS data
    return length


def _nonce_and_aad(header, pn):
    fc0, fc1 = header[0], header[1]
    qos = (fc0 & 0x0c) == 0x08 and fc0 & 0x80
    has_a4 = (fc1 & 0x03) == 0x03
    priority = header[-2] & 0x0f if qos else 0
    nonce = bytes((priority,)) + header[10:16] + pn.to_bytes(6, 'big')
    masked_fc = bytes((fc0 & (0x8f if (fc0 & 0x0c) == 0x08 else 0xff), (fc1 & 0xc7) | 0x40))
    masked_sc = struct.pack('<H', struct.unpack_from('<H', header, 22)[0] & 0x000f)
    aad = masked_fc + header[4:22] + masked_sc
    if has_a4: aad += header[24:30]
    if qos: aad += bytes((header[-2] & 0x0f, 0))
    return nonce, aad


def decrypt(frame, key):
    """Return (pn, key_id, plaintext) for a protected frame without FCS; raises on MIC failure."""
    hl = header_length(frame)
    header, ccmp = frame[:hl], frame[hl:hl + 8]
    if not ccmp[3] & 0x20: raise ValueError('ExtIV not set: not CCMP')
    pn = ccmp[0] | ccmp[1] << 8 | int.from_bytes(ccmp[4:8], 'little') << 16
    nonce, aad = _nonce_and_aad(header, pn)
    return pn, ccmp[3] >> 6, AESCCM(key, tag_length=8).decrypt(nonce, frame[hl + 8:], aad)


def encrypt(header, plaintext, key, pn, key_id=2):
    """Return header + CCMP header + ciphertext + MIC; header must have Protected set."""
    nonce, aad = _nonce_and_aad(header, pn)
    pn_bytes = pn.to_bytes(6, 'little')
    ccmp = pn_bytes[0:2] + b'\0' + bytes((0x20 | key_id << 6,)) + pn_bytes[2:6]
    return header + ccmp + AESCCM(key, tag_length=8).encrypt(nonce, plaintext, aad)


def parse_tunnel(plaintext):
    """Return (outer ethertype, inner dst, inner src, inner ethertype, inner payload) or None."""
    if plaintext[:6] != SNAP_PREFIX or len(plaintext) < 8 + 14: return None
    outer = struct.unpack_from('!H', plaintext, 6)[0]
    inner = plaintext[8:]
    return outer, inner[0:6], inner[6:12], struct.unpack_from('!H', inner, 12)[0], inner[14:]


def _self_test():
    # IEEE 802.11-2016 Annex J.6.4 CCMP test vector.
    key = bytes.fromhex('c97c1f67ce371185514a8a19f2bdd52f')
    header = bytes.fromhex('0848c32c0fd2e128a57c5030f1844408abaea5b8fcba8033')
    plaintext = bytes.fromhex('f8ba1a55d02f85ae967bb62fb6cda8eb7e78a050')
    expected = bytes.fromhex('0848c32c0fd2e128a57c5030f1844408abaea5b8fcba8033'
                             '0ce70020769703b5' 'f3d0a2fe9a3dbf2342a643e43246e80c3c04d019'
                             '7845ce0b16f97623')
    mpdu = encrypt(header, plaintext, key, 0xb5039776e70c, key_id=0)
    assert mpdu == expected, mpdu.hex()
    assert decrypt(mpdu, key) == (0xb5039776e70c, 0, plaintext)


if __name__ == '__main__':
    _self_test()
    print('CCMP test vector ok')
