"""Read-only mapping from host monotonic time to the radio's TSF.

GET433 requires the radio firmware's TSFQ v1 response. Calibration uses
the midpoint of the shortest of three startup requests; refreshes run in one
background worker. The model assumes one hardware microsecond per 1000 host
nanoseconds. Half the request RTT and a drift allowance are uncertainty bounds,
not evidence of the timestamp or timing of an actual on-air transmission.
The production reader isolates driver calls in one persistent child and bounds
parent IPC waits. A killed child still stuck in the kernel is retained and
prevents replacement; this does not claim to cancel the kernel callback itself.
A locked file descriptor inherited by that child also preserves this exclusion
across Link restarts. The lock file is never unlinked or explicitly unlocked.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

GET_TSF = 433
TSF_MAGIC = 0x51465354
TSF_SIZE = 24
TSF_MODULUS = 1 << 64
TSF_MASK = TSF_MODULUS - 1
BOOTSTRAP_SAMPLES = 3
DEFAULT_TIMEOUT = .075
# A rejected refresh is retried this soon instead of after a full refresh
# interval. Live (2026-11-08..10) GET433 RTT normally sits near 0.8 ms but
# spikes to 3.5-4 ms for a second or more; at one attempt per second, two or
# three slow samples in a row let a good model expire and stopped the link.
RETRY_INTERVAL = .05
IPC_ALLOWANCE = .025
DEFAULT_LOCK_PATH = '/run/cr100-bridge/tsf-query.lock'


class ClockUnavailable(RuntimeError):
    """The hardware clock has no current, sufficiently precise calibration."""


class _ClockJump(ClockUnavailable):
    pass


def parse_tsf(data):
    if len(data) != TSF_SIZE:
        raise ValueError('Invalid TSF reply length')
    magic, version, size, status, low, high = struct.unpack('<6I', data)
    if (magic, version, size) != (TSF_MAGIC, 1, TSF_SIZE):
        raise ValueError('Unsupported TSF reply ABI')
    if status != 1:
        raise ClockUnavailable('Radio hardware clock is unavailable')
    return low | high << 32


def read_tsf(*, timeout=DEFAULT_TIMEOUT, socket_factory=None, monotonic=time.monotonic):
    """Only fixed GET433/24, with one acceptance deadline and a kernel sender.

    Nexmon replies with NLMSG_DONE and sequence zero, including when the request
    sequence is one. Sender identity comes from recvfrom, not its payload header.
    A synchronous driver callback may outlast the socket timeout; a late reply
    is rejected, but cancelling such a callback requires process isolation.
    """
    if not .05 <= timeout <= .1:
        raise ValueError('TSF query timeout must be between .05 and .1 seconds')
    factory = socket_factory or socket.socket
    payload = b'NEX\0' + struct.pack('<III', 0, GET_TSF, 0) + bytes(TSF_SIZE)
    message_size = 16 + len(payload)
    deadline = monotonic() + timeout

    def remaining():
        value = deadline - monotonic()
        if value <= 0:
            raise TimeoutError('TSF query deadline expired')
        return value

    with factory(getattr(socket, 'AF_NETLINK', 16), socket.SOCK_RAW, 31) as control:
        control.settimeout(remaining())
        control.bind((0, 0))
        header = struct.pack('<IHHII', message_size, 0, 0, 1, control.getsockname()[0])
        control.settimeout(remaining())
        sent = control.sendto(header + payload, (0, 0))
        if sent != message_size:
            raise OSError(errno.EIO, 'Incomplete TSF query send')
        control.settimeout(remaining())
        reply, sender = control.recvfrom(message_size + 1)
        remaining()  # A late response cannot establish a bounded sample.
    if not sender or sender[0] != 0:
        raise ValueError('TSF reply did not come from the kernel')
    if len(reply) < 16:
        raise ValueError('Truncated TSF netlink reply')
    length, kind, flags, _, _ = struct.unpack_from('<IHHII', reply)
    if kind == 2:  # NLMSG_ERROR, never treat an ACK as a TSF response.
        if length != len(reply) or length < 20:
            raise ValueError('Invalid TSF netlink error')
        code = -struct.unpack_from('<i', reply, 16)[0]
        raise OSError(code or errno.EIO, 'TSF netlink GET failed')
    if length != message_size or len(reply) != message_size or kind != 3 or flags:
        raise ValueError('Invalid TSF netlink envelope')
    if reply[16:20] != b'NEX\0' or struct.unpack_from('<III', reply, 20) != (0, GET_TSF, 0):
        raise ValueError('TSF reply does not match the fixed GET request')
    return parse_tsf(reply[32:])


@dataclass(frozen=True)
class _Sample:
    raw_us: int
    midpoint_ns: int
    rtt_ns: int


@dataclass(frozen=True)
class _Model:
    sample: _Sample
    unwrapped_us: int


_HELLO = struct.pack('<4sI', b'CRCW', 1)
_REQUEST = struct.Struct('<4sI')
_RESPONSE = struct.Struct('<4sIIIQQQ')


class _ProcessReader:
    """One fixed-query child; never replace a killed child before poll reaps it.

    SIGKILL cannot terminate a task while its kernel callback is uninterruptible.
    Such a PID is retained and blocks new helpers, rather than accumulating
    processes. Parent pipe waits and cleanup are bounded and never join it.
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT, *, startup_timeout=.5, popen=subprocess.Popen,
                 lock_path=DEFAULT_LOCK_PATH):
        self.timeout, self.startup_timeout = timeout, startup_timeout
        self.lock_path = lock_path
        self._popen = popen
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pipe_lock = threading.Lock()
        self._process = None
        self._lease_fd = None
        self._retiring = self._closed = False
        self._sequence = self._starts = self._faults = 0
        self._last_fault = None

    def _pipe_io(self, stream, count_or_data, deadline, *, write=False):
        # Own a duplicate through the entire wait. Concurrent close() may close
        # the streams, but must never let an OS-reused descriptor redirect I/O.
        with self._pipe_lock:
            descriptor = os.dup(stream.fileno())
        try:
            return self._descriptor_io(descriptor, count_or_data, deadline, write=write)
        finally:
            os.close(descriptor)

    @staticmethod
    def _descriptor_io(descriptor, count_or_data, deadline, *, write=False):
        result = bytearray()
        remaining_data = memoryview(count_or_data) if write else None
        while (len(remaining_data) if write else len(result) < count_or_data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Hardware clock helper IPC timed out')
            readable, writable, _ = select.select([] if write else [descriptor],
                                                  [descriptor] if write else [], [], remaining)
            if not (writable if write else readable):
                raise TimeoutError('Hardware clock helper IPC timed out')
            try:
                if write:
                    sent = os.write(descriptor, remaining_data)
                    if sent == 0:
                        raise ClockUnavailable('Hardware clock helper pipe closed')
                    remaining_data = remaining_data[sent:]
                else:
                    chunk = os.read(descriptor, count_or_data - len(result))
                    if not chunk:
                        raise ClockUnavailable('Hardware clock helper exited')
                    result.extend(chunk)
            except BlockingIOError:
                continue
        return bytes(result)

    def _close_pipes(self, process):
        with self._pipe_lock:
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _retire(self, reason):
        with self._state_lock:
            process = self._process
            self._retiring = True
            self._faults += 1
            self._last_fault = reason
        if process:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            self._close_pipes(process)
            process.poll()  # WNOHANG only; retain the object/PID if not terminal.
        self._release_lease()

    def _release_lease(self):
        with self._state_lock:
            descriptor, self._lease_fd = self._lease_fd, None
        if descriptor is not None:
            # Do not LOCK_UN: the child shares this open-file description and
            # must retain the flock until it really exits, even after SIGKILL.
            os.close(descriptor)

    def _acquire_lease(self):
        if self.lock_path is None:  # Private injected transports/tests only.
            return None
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                             | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()):
                raise ClockUnavailable('Unsafe hardware clock helper lease file')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ClockUnavailable('Another hardware clock helper owns the radio lease') from error
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _ensure_process(self):
        with self._state_lock:
            if self._closed:
                raise ClockUnavailable('Hardware clock helper is closed')
            process, retiring = self._process, self._retiring
        if process:
            terminal = process.poll() is not None
            if retiring and not terminal:
                raise ClockUnavailable('Previous hardware clock helper has not exited')
            if terminal:
                self._close_pipes(process)
                self._release_lease()
                with self._state_lock:
                    self._process = None
                    self._retiring = False
                if not retiring:
                    with self._state_lock:
                        self._faults += 1
                        self._last_fault = 'Hardware clock helper exited'
                    raise ClockUnavailable('Hardware clock helper exited')
            else:
                return process
        deadline = time.monotonic() + self.startup_timeout
        lease_fd = None
        try:
            lease_fd = self._acquire_lease()
            process = self._popen([sys.executable, '-u', str(Path(__file__).resolve()),
                                   '--_clock-worker', str(self.timeout)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, bufsize=0, close_fds=True,
                                  pass_fds=() if lease_fd is None else (lease_fd,))
        except (OSError, ClockUnavailable) as error:
            if lease_fd is not None:
                os.close(lease_fd)
            with self._state_lock:
                self._faults += 1
                self._last_fault = (str(error) if isinstance(error, ClockUnavailable)
                                    else 'Hardware clock helper launch failed')
            raise ClockUnavailable(self._last_fault) from error
        with self._state_lock:
            self._process = process
            self._lease_fd = lease_fd
            self._starts += 1
            closed = self._closed
        if closed:
            self._retire('Hardware clock helper closed during startup')
            raise ClockUnavailable('Hardware clock helper is closed')
        try:
            with self._pipe_lock:
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
            if self._pipe_io(process.stdout, len(_HELLO), deadline) != _HELLO:
                raise ClockUnavailable('Invalid hardware clock helper handshake')
        except (OSError, ValueError, ClockUnavailable) as error:
            self._retire(str(error))
            raise ClockUnavailable('Hardware clock helper startup failed') from error
        return process

    def __call__(self):
        with self._io_lock:
            process = self._ensure_process()
            self._sequence = (self._sequence + 1) & 0xffffffff
            request_start = time.monotonic_ns()
            deadline = time.monotonic() + self.timeout + IPC_ALLOWANCE
            try:
                self._pipe_io(process.stdin, _REQUEST.pack(b'TSFR', self._sequence), deadline, write=True)
                response = self._pipe_io(process.stdout, _RESPONSE.size, deadline)
                received = time.monotonic_ns()
                magic, version, status, sequence, raw, before, after = _RESPONSE.unpack(response)
                if ((magic, version, sequence) != (b'TSFS', 1, self._sequence)
                        or status not in (1, 2) or not request_start <= before <= after <= received):
                    raise ClockUnavailable('Invalid hardware clock helper response')
            except (OSError, ValueError, ClockUnavailable) as error:
                self._retire(str(error))
                raise ClockUnavailable('Hardware clock helper query failed') from error
            with self._state_lock:
                self._last_fault = None if status == 1 else 'Hardware TSF query failed in helper'
                if status != 1:
                    self._faults += 1
            if status != 1:
                raise ClockUnavailable('Hardware TSF query failed in helper')
            return _Sample(raw, (before + after) // 2, after - before)

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._retire('Hardware clock helper closed')

    def status(self):
        with self._state_lock:
            process = self._process
            alive = bool(process and process.poll() is None)
            return {'pid': process.pid if process else None, 'alive': alive,
                    'awaiting_exit': alive and self._retiring, 'closed': self._closed,
                    'starts': self._starts, 'faults': self._faults, 'last_fault': self._last_fault,
                    'lease_held': self._lease_fd is not None, 'lease_path': self.lock_path}


def _clock_worker(timeout):
    """Private binary protocol: fixed GET433 only, no addresses/configuration."""
    if not .05 <= timeout <= .1:
        return 2
    source, output = sys.stdin.buffer, sys.stdout.buffer
    output.write(_HELLO)
    output.flush()
    while True:
        request = source.read(_REQUEST.size)
        if not request:
            return 0
        if len(request) != _REQUEST.size:
            return 2
        magic, sequence = _REQUEST.unpack(request)
        if magic != b'TSFR':
            return 2
        before = time.monotonic_ns()
        try:
            raw = read_tsf(timeout=timeout)
            status = 1
        except (OSError, ValueError, ClockUnavailable):
            raw, status = 0, 2
        after = time.monotonic_ns()
        output.write(_RESPONSE.pack(b'TSFS', 1, status, sequence, raw, before, after))
        output.flush()


class HardwareClock:
    """Thread-safe clock reads never perform I/O or wait for calibration.

    bootstrap() is the explicitly blocking startup operation (three requests
    plus one bounded child handshake). start() starts refreshes without waiting. Missing/stale clocks
    fail closed via ClockUnavailable; the caller chooses its recovery action.
    The production transport uses one persistent child to contain a stuck driver
    callback. It never replaces a nonterminal old child. An injected reader is
    a zero-argument callable returning uint64 TSF and retains caller ownership.
    """

    def __init__(self, *, reader=None, monotonic_ns=time.monotonic_ns,
                 query_timeout=DEFAULT_TIMEOUT, refresh_interval=1., max_age=3.,
                 max_drift_ppm=200., max_uncertainty_us=2000., lock_path=DEFAULT_LOCK_PATH,
                 retry_interval=None):
        if retry_interval is None:
            retry_interval = min(RETRY_INTERVAL, refresh_interval)
        if not .05 <= query_timeout <= .1:
            raise ValueError('TSF query timeout must be between .05 and .1 seconds')
        if not all(math.isfinite(v) and v > 0 for v in
                   (refresh_interval, max_age, max_drift_ppm, max_uncertainty_us, retry_interval)):
            raise ValueError('Clock limits must be finite and positive')
        if refresh_interval >= max_age:
            raise ValueError('Clock refresh interval must be shorter than maximum age')
        if retry_interval > refresh_interval:
            raise ValueError('Clock retry interval must not exceed the refresh interval')
        self.query_timeout = query_timeout
        self.refresh_interval = refresh_interval
        self.retry_interval = retry_interval
        self.max_age_ns = int(max_age * 1e9)
        self.max_drift_ppm = max_drift_ppm
        self.max_uncertainty_us = max_uncertainty_us
        self._process_reader = _ProcessReader(query_timeout, lock_path=lock_path) if reader is None else None
        self._reader = self._process_reader if reader is None else reader
        self._now = monotonic_ns
        self._lock = threading.Lock()
        self._sampling = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False
        self._generation = 0
        self._model = None
        self._last_error = None
        self._samples = self._failures = self._resets = 0

    def _uncertainty(self, model, now_ns):
        age_ns = abs(now_ns - model.sample.midpoint_ns)
        return model.sample.rtt_ns / 2000 + age_ns * self.max_drift_ppm / 1e9 + 1

    def _usable(self, model, now_ns):
        return (model is not None and 0 <= now_ns - model.sample.midpoint_ns <= self.max_age_ns
                and self._uncertainty(model, now_ns) <= self.max_uncertainty_us)

    def _sample(self):
        before = self._now()
        raw = self._reader()
        after = self._now()
        if isinstance(raw, _Sample):
            sample = raw  # Child brackets GET itself, excluding Python startup/IPC.
        else:
            sample = _Sample(raw, (before + after) // 2, after - before)
        if type(sample.raw_us) is not int or not 0 <= sample.raw_us <= TSF_MASK:
            raise ValueError('Invalid hardware TSF value')
        if not 0 <= sample.rtt_ns <= int(self.query_timeout * 1e9):
            raise ClockUnavailable('TSF sample exceeded its deadline')
        if sample.rtt_ns / 2000 + 1 > self.max_uncertainty_us:
            raise ClockUnavailable('TSF sample uncertainty is too large')
        return sample

    def _advance(self, model, sample):
        if model is None:
            return _Model(sample, sample.raw_us)
        elapsed_us = (sample.midpoint_ns - model.sample.midpoint_ns) / 1000
        delta = ((sample.raw_us - model.sample.raw_us + (1 << 63)) & TSF_MASK) - (1 << 63)
        allowance = ((sample.rtt_ns + model.sample.rtt_ns) / 2000 + 2
                     + max(0, elapsed_us) * self.max_drift_ppm / 1e6)
        if elapsed_us < 0 or abs(delta - elapsed_us) > allowance:
            raise _ClockJump('Hardware TSF changed outside the clock model')
        return _Model(sample, model.unwrapped_us + delta)

    def _valid_generation(self, generation):
        with self._lock:
            return not self._closed and generation == self._generation

    def _calibrate(self, count):
        with self._sampling:
            with self._lock:
                if self._closed:
                    return False
                generation, previous = self._generation, self._model
            samples = []
            try:
                running = previous
                for _ in range(count):
                    if not self._valid_generation(generation):
                        return False
                    sample = self._sample()
                    # Validate every sample, including samples not selected for
                    # minimum RTT, so a reset within startup cannot be hidden.
                    running = self._advance(running, sample)
                    samples.append(running)
                selected = min(samples, key=lambda item: item.sample.rtt_ns)
            except (OSError, ValueError, ClockUnavailable) as error:
                with self._lock:
                    if self._closed or generation != self._generation:
                        return False
                    self._failures += 1
                    self._last_error = str(error)
                    if isinstance(error, _ClockJump):
                        self._generation += 1
                        self._model = None
                        self._resets += 1
                return False
            with self._lock:
                if self._closed or generation != self._generation:
                    return False
                now_ns = self._now()
                if not self._usable(selected, now_ns):
                    self._failures += 1
                    self._last_error = 'TSF calibration expired before publication'
                    return False
                # A marginal RTT can be usable now but exhaust its uncertainty
                # budget before the next refresh. Keep the previous good model
                # rather than publishing a calibration guaranteed to expire.
                horizon_ns = int((self.refresh_interval + self.query_timeout + IPC_ALLOWANCE) * 1e9)
                if not self._usable(selected, now_ns + horizon_ns):
                    self._failures += 1
                    self._last_error = ('TSF sample lacks refresh headroom (RTT %.1f us)'
                                        % (selected.sample.rtt_ns / 1000))
                    return False
                self._model = selected
                self._samples += count
                self._last_error = None
                return True

    def bootstrap(self):
        """Try three samples/minimum RTT; explicitly blocking startup operation."""
        return self._calibrate(BOOTSTRAP_SAMPLES)

    def _run(self):
        while True:
            with self._lock:
                if self._closed:
                    return
                missing = self._model is None
            published = self._calibrate(BOOTSTRAP_SAMPLES if missing else 1)
            # The previous model stays published (and its uncertainty keeps
            # growing) until a refresh succeeds, so retry a rejected one soon.
            self._wake.wait(self.refresh_interval if published else self.retry_interval)
            self._wake.clear()

    def start(self):
        with self._lock:
            if self._closed:
                raise ClockUnavailable('Hardware clock is closed')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='nexmon-clock', daemon=True)
                self._thread.start()
        return self

    def invalidate(self):
        """Immediately reject the old epoch and any read already in flight."""
        with self._lock:
            self._generation += 1
            self._model = None
            self._last_error = 'Hardware clock invalidated'
        self._wake.set()

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._model = None
            worker = self._thread
        self._wake.set()
        if self._process_reader:
            self._process_reader.close()
        if worker and worker is not threading.current_thread():
            worker.join(self.query_timeout + .05)

    def _snapshot(self):
        with self._lock:
            now_ns = self._now()
            model = self._model
            if self._closed or not self._usable(model, now_ns):
                raise ClockUnavailable('Hardware clock calibration is unavailable or stale')
            return model, now_ns

    def tsf_us(self):
        model, now_ns = self._snapshot()
        return (model.unwrapped_us + (now_ns - model.sample.midpoint_ns) // 1000) & TSF_MASK

    def next_tbtt(self, now=None, interval_us=102400):
        """Return host monotonic seconds at the next hardware-clock grid point.

        This is strictly after ``now`` (host monotonic seconds), including a
        point already on the grid. The uint64 hardware rollover itself is zero
        on the new hardware grid. No radio operation occurs here.
        """
        if type(interval_us) is not int or not 0 < interval_us < TSF_MODULUS:
            raise ValueError('Beacon interval must be a positive uint64 microsecond count')
        model, current_ns = self._snapshot()
        if now is None:
            now_ns = current_ns
        else:
            if not math.isfinite(now):
                raise ValueError('Host time must be finite')
            now_ns = round(now * 1e9)
            if (abs(now_ns - model.sample.midpoint_ns) > self.max_age_ns
                    or self._uncertainty(model, now_ns) > self.max_uncertainty_us):
                raise ClockUnavailable('Requested beacon time exceeds the clock model')
        estimate_ns = (model.unwrapped_us * 1000 + now_ns - model.sample.midpoint_ns) % (TSF_MODULUS * 1000)
        delta_ns = interval_us * 1000 - estimate_ns % (interval_us * 1000)
        delta_ns = min(delta_ns, TSF_MODULUS * 1000 - estimate_ns)
        return (now_ns + delta_ns) / 1e9

    def status(self):
        with self._lock:
            now_ns = self._now()
            model = self._model
            result = {'ready': not self._closed and self._usable(model, now_ns),
                    'generation': self._generation, 'closed': self._closed,
                    'worker_alive': bool(self._thread and self._thread.is_alive()),
                    'samples': self._samples, 'failures': self._failures, 'resets': self._resets,
                    'age_seconds': (now_ns - model.sample.midpoint_ns) / 1e9 if model else None,
                    'rtt_us': model.sample.rtt_ns / 1000 if model else None,
                    'uncertainty_us': self._uncertainty(model, now_ns) if model else None,
                    'max_drift_ppm': self.max_drift_ppm, 'last_error': self._last_error}
        result['helper'] = self._process_reader.status() if self._process_reader else None
        return result


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--_clock-worker':
        raise SystemExit('This module only runs its private clock helper')
    raise SystemExit(_clock_worker(float(sys.argv[2])))
