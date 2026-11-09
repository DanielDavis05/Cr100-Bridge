> Historical October 31 MVP snapshot. The original text below is preserved; its
> scope, candidate hardware, unresolved choices and open questions are not current
> status. See [the current findings and open items](RESEARCH.md).

# CR100 Bridge — MVP

A software bridge that lets an old Sonos **CR100** controller attach to something
other than its original Sonos hardware. A Raspberry Pi presents the "player" side
of the proprietary SonosNet link so the CR100 can join and be controlled; a later
layer relays the CR100's commands to a modern Sonos system — or anything else.

This document covers **only the MVP** and is deliberately light on decisions.
Everything under "Findings" is a **hint**, not a specification: it comes from
reverse engineering, emulator runs, and a handful of packet captures. Some of it
is single-capture or emulator-only, and at least one earlier reading was wrong and
later corrected. **Verify before relying on any of it.**

---

## MVP scope

**Goal:** the CR100 completes a SonosNet **pairing** with a Raspberry Pi that uses
**only its built-in Wi-Fi adapter**, and then associates to it.

That's the whole MVP. If the CR100 pairs with the Pi and the link comes up, the MVP
is done. Explicitly **out of scope for now** (but the design should not block them):

- Controlling real players / relaying UPnP to a modern Sonos system.
- Any backend ("anything else": local library, DLNA, streaming, etc.).
- Audio. Robustness, multiple controllers, reconnection, provisioning UX.

No approach is chosen here. Treat the findings below as inputs to that decision,
not as the decision.

---

## Hardware

### CR100 (the controller we must satisfy)
- Sonos handheld controller, mid-2000s vintage; SH4 CPU; **SonosNet-only** radio
  (an Atheros AR5212/RF-class part per its boot log). It does **not** join ordinary
  Wi-Fi — it attaches to a Sonos "player" over SonosNet, a proprietary 802.11g
  (2.4 GHz) scheme, then speaks UPnP over IP.
- Currently running display **4.3.2** (internal `25.2-50130`).
- Reverse-engineering material for it lives in `../cr100-analysis/`,
  `../cr100-emulator/`, and `../update-analysis/7.1-access/cr100/`.

### Raspberry Pi (the host for the bridge)
- The MVP targets the Pi's **built-in Wi-Fi only** (no external adapter).
- Candidate boards: **Pi 4** (Cypress CYW43455) or **Pi Zero W / Zero 2 W**
  (BCM43438). These onboard radios are **fullMAC** (`brcmfmac`).
- Hint, to weigh (not decided): fullMAC firmware limits low-level control
  (custom management frames, injection, static key install without the normal
  handshake). A patched-firmware project (`nexmon`) exists for these exact chips
  and is one way to gain monitor/injection — but whether it's needed, and whether
  it's reliable enough, is unverified here.

### The other end (post-MVP)
- A modern Sonos system, or any media backend. Not part of the MVP.

---

## Findings (HINTS — confidence-tagged)

Tags: `[OTA]` seen in a real over-the-air capture · `[outcome]` something that
demonstrably worked · `[emu]` only seen in the emulator, not on real hardware ·
`[static]` from firmware disassembly · `[inferred]` reasoning only ·
`[corrected]` an earlier wrong guess, since revised.

### Pairing (the MVP's core)
- `[OTA]` Pairing is a **Netstart** exchange carried inside 802.11 probe frames
  (a `Rincon Netstart 1.0` SSID IE followed by `0x46` TLV chunks that reassemble
  into an application payload). Framing seen: one flags byte, one message-type
  byte, then fields of `tag(1) · length(2, big-endian) · value`.
- `[OTA]` Message types observed: `0`=ALIVE (player announces), `1`=QUERY
  (controller asks to join), `2`=RESPONSE (player answers with the key).
- `[OTA]` A real QUERY (CR100 → player) carried: target UUID (tag `0x00`, 24 B),
  transaction id (`0x01`, 4 B), empty household (`0x02`), the CR100's RSA-1024
  public key (`0x03`, 260 B, RSAref layout), a 128-B signature (`0x10`), and a
  capability value (`0x06` = 4).
- `[OTA]` A real RESPONSE (player → CR100) carried: UUID, the **echoed** txid,
  household (`0x02`, 32 B), the player's public key (`0x03`), a 128-B signature
  (`0x10`), the **RSA-encrypted 16-byte network key** (`0x04`, 128 B, encrypted to
  the controller's public key), an operational **frequency** (`0x05` = 2437, i.e.
  channel 6), and capability (`0x06` = 7).
- `[OTA]` **Channels matter:** the exchange was seen on the player's **setup
  channel (ch 1)**; the RESPONSE's frequency field then points the CR100 at the
  **operational channel (ch 6)**, where it re-appears already keyed. A capture on
  the operational channel alone will *miss* the key exchange.
- `[emu]` A software responder (`../cr100-emulator/network/netstart_peer.py`) that
  omits the signature and uses different flag/capability/frequency values **did
  pair the real CR100 in the full-system emulator**. So the exchange's structure
  is right, and the signature appears **optional** for the controller's join path —
  but the real player's exact field values differ from that responder (flags,
  capability, signature presence, frequency). Match-vs-simplify is an open choice.
- `[inferred]` The 16-byte network key delivered in the RESPONSE is the device's
  secret (encrypted to its private key) and cannot be read from a capture. A bridge
  would generate and deliver its *own* key the same way.

### Association & link encryption (needed right after pairing)
- `[OTA]` After pairing, the CR100 associates with a **non-standard** convention:
  `ToDS=1` on its association request, only a Sonos vendor IE
  (`dd 00:0e:58 02 01 01`), **no RSN/WPA IE, no Open-System auth, no WPA2 4-way
  (EAPOL) handshake**.
- `[OTA]` `[corrected]` The link cipher appears to be **standard CCMP (AES-128-CCM)**
  — monotonic packet number, ExtIV set, standard header. (An earlier reading of a
  single mis-aligned frame suggested AES-OCB; that was wrong.) The on-air KeyID
  field was observed as `2`.
- `[static]` The driver's AES-key ioctl sets a key struct with type `2` and
  length `16`, consistent with AES-128. A hint, cross-checked by the on-air header.
- `[inferred]` Net effect: the 16-byte key from pairing is installed as a **static**
  link key (no 4-way to derive it). Whether a bridge installs it via the OS key
  API or handles the cipher itself is an open approach question, not decided here.

### Discovery / radio hints
- `[OTA]` The household SSID is the household ID string, broadcast hidden; beacons
  on ch 6 advertised an RSN (CCMP/PSK) IE even though the CR100's own join path
  above didn't use it.
- `[static]` A detailed, static-analysis map of the player-side 802.11 management
  frames (probe/assoc handling, the Netstart envelope) is in
  `../cr100-analysis/radio_frames.md`. It carries its own "likely / not proven"
  caveats.

---

## Source material / evidence

- Working (emulator) responder & peer: `../cr100-emulator/network/netstart_peer.py`,
  `zp80_peer.py`.
- Frame-layer notes: `../cr100-analysis/radio_frames.md`,
  `../cr100-analysis/packet_format.md`.
- Pairing analysis: `../update-analysis/7.1-access/cr100/pairing.md`,
  `../cr100-emulator/research_network/`.
- Over-the-air captures taken during this work (channel-6 steady state /
  re-join, and a **channel-1 fresh pairing** containing the QUERY/RESPONSE).
  These are the primary evidence for the pairing hints above — **preserve them**;
  they live outside this repo and are easy to lose.
- A dependency-free capture decoder: `../cr100-analysis/sonosnet_decode.py`.

---

## Open questions / to verify before building on the hints

- Whether the Pi's **onboard fullMAC** radio can be made to emit the Netstart probe
  frames and accept the CR100's association at all (and if `nexmon` is required).
- The real player's **flags / capability / signature** fields vs. the simplified
  emulator responder — which the CR100 actually requires for a *fresh* pair on real
  hardware (the emulator pair may have been more permissive).
- How the static CCMP key is best installed on the chosen radio path.
- The CR100's channel-scan behavior for discovery (setup was ch 1 here; is that
  fixed, configurable, or scanned?).
- Everything in `radio_frames.md` flagged "likely / not proven."

---

## Extensibility (not MVP)

Keep the pairing/attach layer separable from whatever comes later. Once the CR100
attaches to the Pi, later layers can present a player's UPnP surface to it and relay
to a backend — but none of that is designed or decided here. The MVP is: **pair the
CR100 to the Pi on built-in Wi-Fi.**
