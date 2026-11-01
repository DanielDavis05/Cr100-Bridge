"""Room icons the CR100 can draw, and a default for each room from its name.

A ZoneGroupMember's Icon is x-rincon-roomicon:<name>; the CR100 maps it to the symbol
icon:rm:<name> in MainUI.swf (Household.roomIconURLToShortName, then attachMovie) and
falls back to generic. MainUI.swf 4.3.2 exports 23: 21 room names plus 'portable' and
'zoneextender', which are device glyphs. The 21 room names draw only 15 pictures: guest
room and main bedroom use bedroom's (an alarm clock; there is no bed), den uses TV room's,
playroom family room's, hallway foyer's and patio garden's. So the web interface offers
one choice per picture, and every name stays valid in the config. It shows the extracted
bitmaps (webui/room-icons/<name>.png).
"""
from __future__ import annotations

import re

URI_PREFIX = 'x-rincon-roomicon:'
DEFAULT = 'living'   # what every room showed before icons were configurable

# One choice per picture, in the order the web interface lists them:
# (name sent to the CR100, label, other names that draw the same picture)
PICTURES = [
    ('living', 'Living room', ()), ('family', 'Family room or playroom', ('playroom',)),
    ('tvroom', 'TV room or den', ('den',)), ('media', 'Media room', ()), ('kitchen', 'Kitchen', ()),
    ('dining', 'Dining room', ()), ('bedroom', 'Bedroom', ('masterbedroom', 'guestroom')),
    ('bathroom', 'Bathroom', ()), ('office', 'Office', ()), ('library', 'Library', ()),
    ('foyer', 'Foyer or hallway', ('hallway',)), ('garage', 'Garage', ()),
    ('garden', 'Garden or patio', ('patio',)), ('pool', 'Pool', ()), ('generic', 'Other', ()),
]

# every valid room icon name -> the picture's name (itself for the 15 above)
ICONS = {alias: name for name, _, aliases in PICTURES for alias in (name, *aliases)}

# Checked in order; the first pattern found in the room's name wins.
GUESSES = [
    (r'master|main bed|primary|guest|\bbed', 'bedroom'), (r'\bbath|shower|toilet|\bwash', 'bathroom'),
    (r'kitchen', 'kitchen'), (r'dining', 'dining'), (r'family|\bplay|\bkids?\b|nursery', 'family'),
    (r'\btv\b|television|\bden\b', 'tvroom'), (r'\bmedia|theat|cinema', 'media'),
    (r'garage|workshop', 'garage'), (r'office|study|\bwork', 'office'), (r'library|reading', 'library'),
    (r'foyer|\bentr|\bhall|landing|stair', 'foyer'),
    (r'garden|yard|lawn|patio|deck|porch|terrace|balcon', 'garden'),
    (r'\bpool|jacuzzi|hot tub|\bspa\b', 'pool'), (r'living|lounge|sitting', 'living'),
]


def picture(name):
    """The picture a room icon name draws (one of the 15 PICTURES names), or None."""
    return ICONS.get(name)


def guess(room_name):
    """A sensible picture for a room name, or DEFAULT."""
    name = (room_name or '').lower()
    for pattern, icon in GUESSES:
        if re.search(pattern, name): return icon
    return DEFAULT


def effective(room_name, configured):
    """The picture a room shows: its configured one if valid, else the guess from its name."""
    return picture(configured) or guess(room_name)


def uri(icon):
    return URI_PREFIX + (picture(icon) or DEFAULT)
