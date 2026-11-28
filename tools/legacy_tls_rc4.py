"""Minimal RFC 2246 TLS 1.0 RC4 fallback for legacy CR100 Online Updates.

Pre-4.3 CR100 firmware (such as 3.6 / 16.5-48010a) offers only RC4 cipher
suites (0x0005 RC4-SHA, 0x0004 RC4-MD5, and export RC4) in its TLS 1.0
ClientHello. Modern Debian/Kali OpenSSL builds omit RC4 from libssl, so
standard SSLContext.wrap_socket() rejects those controllers with a fatal
handshake failure.

This module is invoked per connection ONLY when a paired controller's
ClientHello offers zero cipher suites supported by the host OpenSSL context
and does offer RC4-SHA (0x0005) or RC4-MD5 (0x0004). Newer controllers (4.3,
4.3.2, CFW) continue using OpenSSL unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
from pathlib import Path
import socket
import struct


TLS_RSA_WITH_RC4_128_SHA = 0x0005
TLS_RSA_WITH_RC4_128_MD5 = 0x0004
SUPPORTED_LEGACY_CIPHERS = (TLS_RSA_WITH_RC4_128_SHA, TLS_RSA_WITH_RC4_128_MD5)
MAX_RECORD_LENGTH = 18432
MAX_HANDSHAKE_LENGTH = 16384


def load_certificate_and_key(cert_path: Path, key_path: Path):
    """Load DER certificate bytes and RSA private key from PEM files."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    return key, cert.public_bytes(serialization.Encoding.DER)


def parse_client_hello_ciphers(data: bytes) -> list[int] | None:
    """Extract offered 16-bit cipher suite IDs from a raw TLS ClientHello record."""
    if len(data) < 44 or data[0] != 0x16 or data[1] != 0x03 or data[5] != 0x01:
        return None
    record_len = struct.unpack('>H', data[3:5])[0]
    if record_len < 39 or record_len > MAX_RECORD_LENGTH:
        return None
    if len(data) < 5 + record_len:
        return None
    hs_len = int.from_bytes(data[6:9], 'big')
    if hs_len < 35 or 4 + hs_len > record_len:
        return None
    # Handshake header (4) + client_version (2) + random (32) = offset 38 from handshake start -> 43 from record start
    pos = 5 + 38
    sid_len = data[pos]
    pos += 1 + sid_len
    if pos + 2 > 5 + 4 + hs_len:
        return None
    cs_len = struct.unpack('>H', data[pos:pos + 2])[0]
    pos += 2
    if cs_len < 2 or cs_len % 2 != 0 or pos + cs_len > 5 + 4 + hs_len:
        return None
    return [struct.unpack('>H', data[pos + i:pos + i + 2])[0] for i in range(0, cs_len, 2)]


def peek_client_hello_ciphers(sock: socket.socket) -> list[int] | None:
    """Non-destructively peek the TLS ClientHello and return offered cipher IDs."""
    try:
        peek = sock.recv(4096, socket.MSG_PEEK)
    except OSError:
        return None
    return parse_client_hello_ciphers(peek)


def needs_legacy_rc4(offered_ciphers: list[int] | None, openssl_cipher_ids: set[int] | frozenset[int]) -> bool:
    """Use the RC4 fallback only when OpenSSL shares no cipher with the client."""
    if not offered_ciphers:
        return False
    offered = set(offered_ciphers)
    if offered & openssl_cipher_ids:
        return False
    return any(cipher in offered for cipher in SUPPORTED_LEGACY_CIPHERS)


class _RC4:
    """Stateful RC4 stream cipher (TLS 1.0 keeps stream state across records)."""

    def __init__(self, key: bytes) -> None:
        state = list(range(256))
        j = 0
        key_len = len(key)
        for i in range(256):
            j = (j + state[i] + key[i % key_len]) & 0xFF
            state[i], state[j] = state[j], state[i]
        self.state = state
        self.i = 0
        self.j = 0

    def crypt(self, data: bytes) -> bytes:
        state = self.state
        i = self.i
        j = self.j
        out = bytearray(len(data))
        for index, byte in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + state[i]) & 0xFF
            state[i], state[j] = state[j], state[i]
            out[index] = byte ^ state[(state[i] + state[j]) & 0xFF]
        self.i = i
        self.j = j
        return bytes(out)


def _p_hash(hash_mod, secret: bytes, seed: bytes, length: int) -> bytes:
    out = bytearray()
    a = seed
    while len(out) < length:
        a = hmac.new(secret, a, hash_mod).digest()
        out.extend(hmac.new(secret, a + seed, hash_mod).digest())
    return bytes(out[:length])


def _tls10_prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    half = (len(secret) + 1) // 2
    s1 = secret[:half]
    s2 = secret[-half:]
    combined = label + seed
    p_md5 = _p_hash(hashlib.md5, s1, combined, length)
    p_sha1 = _p_hash(hashlib.sha1, s2, combined, length)
    return bytes(a ^ b for a, b in zip(p_md5, p_sha1))


def _hs_msg(htype: int, body: bytes) -> bytes:
    return bytes([htype]) + len(body).to_bytes(3, 'big') + body


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise EOFError('Unexpected EOF during legacy TLS read')
        buf.extend(chunk)
    return bytes(buf)


def _recv_record(sock: socket.socket) -> tuple[int, bytes, bytes]:
    header = _recv_exact(sock, 5)
    ctype = header[0]
    ver = header[1:3]
    length = struct.unpack('>H', header[3:5])[0]
    if length > MAX_RECORD_LENGTH:
        raise ValueError('TLS record exceeds maximum length')
    payload = _recv_exact(sock, length)
    return ctype, ver, payload


def _send_record(sock: socket.socket, ctype: int, payload: bytes) -> None:
    sock.sendall(bytes([ctype, 0x03, 0x01]) + struct.pack('>H', len(payload)) + payload)


class _LegacyRawReader(io.RawIOBase):
    def __init__(self, conn: LegacyRC4Socket) -> None:
        self._conn = conn

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        data = self._conn.recv(len(target))
        if not data:
            return 0
        target[:len(data)] = data
        return len(data)


class _LegacyWriter:
    def __init__(self, conn: LegacyRC4Socket) -> None:
        self._conn = conn
        self.closed = False

    def write(self, data: bytes | bytearray | memoryview) -> int:
        raw = bytes(data)
        self._conn.sendall(raw)
        return len(raw)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class LegacyRC4Socket:
    """Socket-like TLS 1.0 RC4-SHA/RC4-MD5 server wrapper for BaseHTTPRequestHandler."""

    def __init__(self, sock: socket.socket, private_key, cert_der: bytes) -> None:
        self.sock = sock
        self.private_key = private_key
        self.cert_der = cert_der
        self.cipher_suite = TLS_RSA_WITH_RC4_128_SHA
        self.mac_mod = hashlib.sha1
        self.mac_len = 20
        self.client_mac_key = b''
        self.server_mac_key = b''
        self.client_rc4: _RC4 | None = None
        self.server_rc4: _RC4 | None = None
        self.client_seq = 0
        self.server_seq = 0
        self._rx_buf = bytearray()
        self._eof = False
        self._closed = False
        self._handshake()

    @property
    def cipher_name(self) -> str:
        return 'RC4-SHA' if self.cipher_suite == TLS_RSA_WITH_RC4_128_SHA else 'RC4-MD5'

    def _mac(self, key: bytes, seq: int, ctype: int, payload: bytes) -> bytes:
        header = struct.pack('>QBBBH', seq, ctype, 3, 1, len(payload))
        return hmac.new(key, header + payload, self.mac_mod).digest()

    def _send_encrypted(self, ctype: int, plaintext: bytes) -> None:
        assert self.server_rc4 is not None
        if not plaintext:
            mac = self._mac(self.server_mac_key, self.server_seq, ctype, b'')
            self.server_seq += 1
            _send_record(self.sock, ctype, self.server_rc4.crypt(mac))
            return
        for offset in range(0, len(plaintext), 16384):
            chunk = plaintext[offset:offset + 16384]
            mac = self._mac(self.server_mac_key, self.server_seq, ctype, chunk)
            self.server_seq += 1
            _send_record(self.sock, ctype, self.server_rc4.crypt(chunk + mac))

    def _recv_encrypted(self) -> tuple[int, bytes]:
        assert self.client_rc4 is not None
        ctype, _ver, encrypted = _recv_record(self.sock)
        decrypted = self.client_rc4.crypt(encrypted)
        if len(decrypted) < self.mac_len:
            raise ValueError('Truncated encrypted TLS record')
        plaintext = decrypted[:-self.mac_len]
        got_mac = decrypted[-self.mac_len:]
        expected_mac = self._mac(self.client_mac_key, self.client_seq, ctype, plaintext)
        self.client_seq += 1
        if not hmac.compare_digest(got_mac, expected_mac):
            raise ValueError('Invalid TLS record MAC')
        return ctype, plaintext

    def _handshake(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import padding

        transcript = bytearray()
        ctype, _ver, payload = _recv_record(self.sock)
        if ctype != 0x16 or len(payload) < 42 or payload[0] != 0x01:
            raise ValueError('Expected TLS ClientHello')
        hs_len = int.from_bytes(payload[1:4], 'big')
        if hs_len > MAX_HANDSHAKE_LENGTH or 4 + hs_len > len(payload):
            raise ValueError('Invalid ClientHello length')
        client_hello = payload[:4 + hs_len]
        transcript.extend(client_hello)
        client_version = client_hello[4:6]
        client_random = client_hello[6:38]
        pos = 38
        sid_len = client_hello[pos]
        pos += 1 + sid_len
        cs_len = struct.unpack('>H', client_hello[pos:pos + 2])[0]
        pos += 2
        ciphers = [struct.unpack('>H', client_hello[pos + i:pos + i + 2])[0] for i in range(0, cs_len, 2)]

        if TLS_RSA_WITH_RC4_128_SHA in ciphers:
            self.cipher_suite = TLS_RSA_WITH_RC4_128_SHA
            self.mac_mod = hashlib.sha1
            self.mac_len = 20
        elif TLS_RSA_WITH_RC4_128_MD5 in ciphers:
            self.cipher_suite = TLS_RSA_WITH_RC4_128_MD5
            self.mac_mod = hashlib.md5
            self.mac_len = 16
        else:
            _send_record(self.sock, 0x15, b'\x02\x28')
            raise ValueError('Client offered no supported RC4 cipher suite')

        server_random = os.urandom(32)
        server_hello = _hs_msg(
            0x02,
            b'\x03\x01' + server_random + b'\x00' + struct.pack('>H', self.cipher_suite) + b'\x00',
        )
        cert_entry = len(self.cert_der).to_bytes(3, 'big') + self.cert_der
        certificate = _hs_msg(0x0B, len(cert_entry).to_bytes(3, 'big') + cert_entry)
        server_done = _hs_msg(0x0E, b'')
        for message in (server_hello, certificate, server_done):
            transcript.extend(message)
            _send_record(self.sock, 0x16, message)

        ctype, _ver, payload = _recv_record(self.sock)
        if ctype != 0x16 or len(payload) < 6 or payload[0] != 0x10:
            raise ValueError('Expected TLS ClientKeyExchange')
        cke_len = int.from_bytes(payload[1:4], 'big')
        if cke_len > MAX_HANDSHAKE_LENGTH or 4 + cke_len > len(payload):
            raise ValueError('Invalid ClientKeyExchange length')
        cke = payload[:4 + cke_len]
        transcript.extend(cke)
        enc_len = struct.unpack('>H', cke[4:6])[0]
        if 6 + enc_len != len(cke):
            raise ValueError('Invalid encrypted premaster secret length')
        pre_master = self.private_key.decrypt(cke[6:6 + enc_len], padding.PKCS1v15())
        if len(pre_master) != 48 or pre_master[:2] != client_version:
            raise ValueError('Invalid TLS premaster secret')

        master = _tls10_prf(pre_master, b'master secret', client_random + server_random, 48)
        key_block = _tls10_prf(master, b'key expansion', server_random + client_random, 2 * self.mac_len + 32)
        offset = 0
        self.client_mac_key = key_block[offset:offset + self.mac_len]
        offset += self.mac_len
        self.server_mac_key = key_block[offset:offset + self.mac_len]
        offset += self.mac_len
        self.client_rc4 = _RC4(key_block[offset:offset + 16])
        offset += 16
        self.server_rc4 = _RC4(key_block[offset:offset + 16])

        ctype, _ver, payload = _recv_record(self.sock)
        if ctype != 0x14 or payload != b'\x01':
            raise ValueError('Expected TLS ChangeCipherSpec')

        ctype, client_fin = self._recv_encrypted()
        if ctype != 0x16 or len(client_fin) != 16 or client_fin[0] != 0x14:
            raise ValueError('Expected TLS client Finished')
        digest = hashlib.md5(transcript).digest() + hashlib.sha1(transcript).digest()
        expected = _tls10_prf(master, b'client finished', digest, 12)
        if not hmac.compare_digest(client_fin[4:16], expected):
            raise ValueError('Client Finished verify_data mismatch')
        transcript.extend(client_fin)

        _send_record(self.sock, 0x14, b'\x01')
        digest = hashlib.md5(transcript).digest() + hashlib.sha1(transcript).digest()
        server_verify = _tls10_prf(master, b'server finished', digest, 12)
        self._send_encrypted(0x16, _hs_msg(0x14, server_verify))

    def settimeout(self, timeout: float | None) -> None:
        self.sock.settimeout(timeout)

    def recv(self, size: int) -> bytes:
        if size <= 0:
            return b''
        while not self._rx_buf and not self._eof:
            try:
                ctype, data = self._recv_encrypted()
            except EOFError:
                self._eof = True
                break
            if ctype == 0x15:
                self._eof = True
                break
            if ctype == 0x17 and data:
                self._rx_buf.extend(data)
        if not self._rx_buf:
            return b''
        out = bytes(self._rx_buf[:size])
        del self._rx_buf[:size]
        return out

    def sendall(self, data: bytes | bytearray | memoryview) -> None:
        self._send_encrypted(0x17, bytes(data))

    def makefile(self, mode: str = 'r', buffering: int = -1):
        if 'r' in mode:
            reader = _LegacyRawReader(self)
            return reader if buffering == 0 else io.BufferedReader(reader, buffer_size=max(4096, buffering))
        if 'w' in mode:
            return _LegacyWriter(self)
        raise ValueError(f'Unsupported makefile mode: {mode!r}')

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.server_rc4 is not None:
                self._send_encrypted(0x15, b'\x01\x00')
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> LegacyRC4Socket:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
