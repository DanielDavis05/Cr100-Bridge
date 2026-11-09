"""The stock CR100's zero-based timezone picker mapped to current IANA rules.

Order verified against RHHSTR_TIME_ZONES in both original 4.3/4.3.2 resources.
Some historical multi-city groups now have different civil-time rules; the first
representative zone below defines that legacy selection. The web editor can use
any IANA timezone. We intentionally use current tzdata rather than stale offsets
in the original display labels (e.g. Moscow, Mexico City, Sri Lanka).
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import calendar
import re
import struct
from zoneinfo import ZoneInfo

from alarms import AlarmError

NATIVE_TIMEZONES = (
    'Etc/GMT+12', 'Pacific/Pago_Pago', 'Pacific/Honolulu', 'America/Anchorage',
    'America/Los_Angeles', 'America/Phoenix', 'America/Mazatlan', 'America/Denver',
    'America/Guatemala', 'America/Chicago', 'America/Mexico_City', 'America/Regina',
    'America/Bogota', 'America/New_York', 'America/Indiana/Indianapolis',
    'America/Halifax', 'America/Caracas', 'America/Santiago', 'America/St_Johns',
    'America/Sao_Paulo', 'America/Argentina/Buenos_Aires', 'America/Nuuk',
    'Etc/GMT+2', 'Atlantic/Azores', 'Atlantic/Cape_Verde', 'Africa/Casablanca',
    'Europe/London', 'Europe/Berlin', 'Europe/Belgrade', 'Europe/Paris',
    'Europe/Warsaw', 'Africa/Lagos', 'Europe/Athens', 'Europe/Bucharest',
    'Africa/Cairo', 'Africa/Johannesburg', 'Europe/Helsinki', 'Asia/Jerusalem',
    'Asia/Baghdad', 'Asia/Riyadh', 'Europe/Moscow', 'Africa/Nairobi', 'Asia/Tehran',
    'Asia/Dubai', 'Asia/Baku', 'Asia/Kabul', 'Asia/Yekaterinburg', 'Asia/Karachi',
    'Asia/Kolkata', 'Asia/Kathmandu', 'Asia/Almaty', 'Asia/Dhaka', 'Asia/Colombo',
    'Asia/Yangon', 'Asia/Bangkok', 'Asia/Krasnoyarsk', 'Asia/Shanghai', 'Asia/Irkutsk',
    'Asia/Singapore', 'Australia/Perth', 'Asia/Taipei', 'Asia/Tokyo', 'Asia/Seoul',
    'Asia/Yakutsk', 'Australia/Adelaide', 'Australia/Darwin', 'Australia/Brisbane',
    'Australia/Sydney', 'Pacific/Guam', 'Australia/Hobart', 'Asia/Vladivostok',
    'Asia/Magadan', 'Pacific/Auckland', 'Pacific/Fiji', 'Pacific/Tongatapu',
)


def native_timezone(index, adjust_dst=True):
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(NATIVE_TIMEZONES):
        raise AlarmError('Unknown native timezone selection', 402)
    name = NATIVE_TIMEZONES[index]
    zone = ZoneInfo(name)
    if adjust_dst: return name
    # Honour the original DST switch, including half/quarter-hour standard time.
    probe = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=zone)
    offset = probe.utcoffset() - (probe.dst() or timedelta())
    minutes = int(offset.total_seconds() // 60)
    return 'UTC%s%02d:%02d' % ('+' if minutes >= 0 else '-', abs(minutes) // 60, abs(minutes) % 60)


def native_timezone_index(name):
    if name in ('UTC', 'Etc/UTC', 'GMT', 'Etc/GMT'): return 26
    try: return NATIVE_TIMEZONES.index(name)
    except ValueError:
        # A web-selected IANA zone outside the legacy menu remains valid. Pick
        # the first same-standard-offset entry only for the legacy picker label.
        from alarms import _zone
        zone = _zone(name)
        probe = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=zone)
        offset = probe.utcoffset() - (probe.dst() or timedelta())
        for index, candidate in enumerate(NATIVE_TIMEZONES):
            test = probe.replace(tzinfo=ZoneInfo(candidate))
            if test.utcoffset() - (test.dst() or timedelta()) == offset: return index
        return 26


@lru_cache(maxsize=256)
def _annual_native_timezone_rule(name, year):
    """Sonos' 14-byte timezone rule, not an IANA identifier.

    The CR100 interprets the hex rule itself. Layout matches read-only native
    GetTimeZoneRule responses: big-endian signed minute bias, standard
    month/weekday/week/hour and bias, daylight transition and bias. Week 5 is
    the last weekday. Build from current IANA transitions rather than freezing
    the obsolete rules shipped in the controller or a speaker's timezone table.
    """
    from alarms import _zone
    zone = _zone(name)
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = start.replace(year=year + 1)
    standard = start.astimezone(zone)
    standard_offset = standard.utcoffset() - (standard.dst() or timedelta())
    bias = -int(standard_offset.total_seconds() // 60)
    transitions = {}
    changes = 0
    previous = start
    before = previous.astimezone(zone)
    probe = start + timedelta(hours=6)
    while probe <= end:
        after = probe.astimezone(zone)
        if after.utcoffset() != before.utcoffset():
            changes += 1
            # This compact format can describe just one transition in each
            # direction, with one standard offset for the whole year. Political
            # changes can violate both constraints (Casablanca in tzdata2026e).
            if changes > 2: return None
            low, high = previous, probe
            while (high - low).total_seconds() > 1:
                middle = low + timedelta(seconds=int((high - low).total_seconds()) // 2)
                if middle.astimezone(zone).utcoffset() == before.utcoffset(): low = middle
                else: high = middle
            old = (high - timedelta(seconds=1)).astimezone(zone)
            new = high.astimezone(zone)
            if new.utcoffset() - (new.dst() or timedelta()) != standard_offset: return None
            boundary = (high + old.utcoffset()).replace(tzinfo=None)
            week = 5 if boundary.day + 7 > calendar.monthrange(year, boundary.month)[1] else (boundary.day - 1) // 7 + 1
            transition = (boundary.month, (boundary.weekday() + 1) % 7, week, boundary.hour)
            extra = -int((new.utcoffset() - standard_offset).total_seconds() // 60)
            if boundary.minute or boundary.second: return None
            kind = 'daylight' if new.dst() else 'standard'
            if kind in transitions: return None
            transitions[kind] = (transition, extra)
        previous, before = probe, after
        probe += timedelta(hours=6)
    zero = ((0, 0, 0, 0), 0)
    std, std_bias = transitions.get('standard', zero)
    dst, dst_bias = transitions.get('daylight', zero)
    if changes and (not std[0] or not dst[0]): return None
    if not changes: bias = -int(standard.utcoffset().total_seconds() // 60)
    return struct.pack('>h4Bh4Bh', bias, *std, std_bias, *dst, dst_bias).hex()


def native_timezone_rule(name, year, *, instant=None):
    """Return a representable annual rule or the household's current offset.

    A complex year cannot honestly fit two recurring transitions. Its fixed
    offset is deliberately calculated on every read, outside the annual cache,
    so subsequent native clock reads follow the household instant and tzdata.
    """
    result = _annual_native_timezone_rule(name, year)
    if result is not None: return result
    from alarms import _zone
    when = datetime.now(timezone.utc) if instant is None else datetime.fromtimestamp(instant, timezone.utc)
    if instant is None and when.year != year: when = datetime(year, 1, 1, tzinfo=timezone.utc)
    bias = -int(when.astimezone(_zone(name)).utcoffset().total_seconds() // 60)
    return struct.pack('>h4Bh4Bh', bias, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0).hex()


def native_rule_zone(rule, local):
    """Resolve a native rule's offset for a manual local-time write."""
    if not isinstance(rule, str) or not re.fullmatch(r'[0-9a-fA-F]{28}', rule):
        raise AlarmError('Invalid native timezone rule', 402)
    fields = struct.unpack('>h4Bh4Bh', bytes.fromhex(rule))
    bias, std, std_bias, dst, dst_bias = fields[0], fields[1:5], fields[5], fields[6:10], fields[10]
    def transition(value):
        month, weekday, week, hour = value
        if not 1 <= month <= 12 or not 0 <= weekday <= 6 or not 1 <= week <= 5 or not 0 <= hour <= 23:
            raise AlarmError('Invalid native timezone transition', 402)
        first = datetime(local.year, month, 1)
        day = 1 + ((weekday - 1) % 7 - first.weekday()) % 7 + 7 * (week - 1)
        if day > calendar.monthrange(local.year, month)[1]: day -= 7
        return first.replace(day=day, hour=hour)
    extra = std_bias
    if std[0] and dst[0]:
        std_at, dst_at = transition(std), transition(dst)
        daylight = dst_at <= local < std_at if dst_at < std_at else local >= dst_at or local < std_at
        extra = dst_bias if daylight else std_bias
    if not -24 * 60 < bias + extra < 24 * 60:
        raise AlarmError('Invalid native timezone bias', 402)
    return timezone(timedelta(minutes=-(bias + extra)))
