"""Short-lived remote commands addressed to a paired CR100 over SonosNet."""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import secrets
import socket
import struct
import threading
import time

from controller_state import controller_records
from bridge_config import ConfigError

ADDRESS = '192.168.69.1'
PORT = 3481
WIRE = struct.Struct('!4sc12sI8sB24s')
TAG_SIZE = 32
MAGIC = b'RCV1'
EMPTY = bytes(8)
ACTIONS = {'wake': 1, 'rooms': 2, 'music': 3, 'settings': 4, 'now_playing': 5, 'find': 6}
MENUS = {'rooms': 'Rooms', 'music': 'Music', 'settings': 'System Settings'}
RESULTS = {1: 'Command completed.', 2: 'The room is unavailable on this controller.',
           3: 'The controller was busy and could not open that menu.',
           4: 'The controller could not sound its buzzer.'}


class RemoteCommandError(ConfigError):
    def __init__(self, status, code, message):
        super().__init__(status, message)
        self.code = code


@dataclass
class Command:
    id: bytes
    action: int
    argument: bytes
    key: bytes
    expires: float
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0


class ControllerRemote:
    def __init__(self, state, stop, log, *, timeout=8):
        self.state, self.stop, self.log = state, stop, log
        self.timeout = timeout
        self.lock = threading.Lock()
        self.pending = {}
        self.seen = {}

    def start(self):
        threading.Thread(target=self.serve, name='controller-remote', daemon=True).start()

    def supported(self, mac):
        with self.lock:
            return time.monotonic() - self.seen.get(mac, float('-inf')) < 8

    def execute(self, mac, action, argument=''):
        paired = {row['mac']: bytes.fromhex(row['key']) for row in controller_records(self.state())}
        if mac not in paired:
            raise ConfigError(404, 'That controller is no longer paired.')
        command = Command(secrets.token_bytes(8), ACTIONS[action], argument.encode('ascii'),
                          paired[mac], time.monotonic() + self.timeout)
        with self.lock:
            if mac in self.pending:
                raise RemoteCommandError(409, 'command_pending',
                                         'A command is already pending for this controller.')
            self.pending[mac] = command
        try:
            if not command.done.wait(self.timeout):
                raise RemoteCommandError(504, 'controller_timeout',
                                         'No response from the controller. Wake it or check its connection.')
            if command.status != 1:
                code = {2: 'room_unavailable', 3: 'controller_busy', 4: 'buzzer_failed'}[command.status]
                raise RemoteCommandError(409, code, RESULTS[command.status])
            return {'message': RESULTS[1]}
        finally:
            with self.lock:
                self.pending.pop(mac, None)

    def response(self, packet):
        if len(packet) != WIRE.size + TAG_SIZE:
            return None
        body, tag = packet[:WIRE.size], packet[WIRE.size:]
        magic, kind, session, sequence, receipt, status, argument = WIRE.unpack(body)
        if magic != MAGIC or kind != b'P' or argument != bytes(24):
            return None
        matched = []
        for row in controller_records(self.state()):
            key = bytes.fromhex(row['key'])
            if hmac.compare_digest(tag, hmac.digest(key, body, 'sha256')):
                matched.append((row['mac'], key))
        if len(matched) != 1:
            return None
        mac, key = matched[0]
        now = time.monotonic()
        with self.lock:
            self.seen[mac] = now
            command = self.pending.get(mac)
            if command and command.key == key and now < command.expires:
                if receipt == command.id and status in RESULTS:
                    command.status = status
                    command.done.set()
                if not command.done.is_set():
                    reply = WIRE.pack(MAGIC, b'C', session, sequence, command.id,
                                      command.action, command.argument)
                else:
                    reply = WIRE.pack(MAGIC, b'C', session, sequence, EMPTY, 0, b'')
            else:
                reply = WIRE.pack(MAGIC, b'C', session, sequence, EMPTY, 0, b'')
        return reply + hmac.digest(key, reply, 'sha256')

    def serve(self):
        while not self.stop.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
                    listener.bind((ADDRESS, PORT))
                    listener.settimeout(1)
                    self.log({'event': 'controller-remote-ready', 'port': PORT})
                    while not self.stop.is_set():
                        try:
                            packet, address = listener.recvfrom(WIRE.size + TAG_SIZE + 1)
                        except TimeoutError:
                            continue
                        try:
                            reply = self.response(packet)
                        except (OSError, ValueError):
                            continue
                        if reply is not None:
                            listener.sendto(reply, address)
            except OSError:
                self.stop.wait(1)
