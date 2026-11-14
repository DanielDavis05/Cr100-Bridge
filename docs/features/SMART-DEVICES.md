# Home Assistant devices

**Music → Devices** controls Home Assistant lights, switches, toggle helpers,
fans, scenes and scripts from the CR100. It works whether music comes through
Home Assistant or the native Sonos source. The menu needs a bridge and controller
firmware that support Devices, Home Assistant credentials, a working connection,
and at least one eligible device assigned to an area.

## Using it

Select a light, switch, toggle helper or fan and press **OK** to turn it on or
off. OK requests the opposite of the row's displayed On/Off state. Pressing
again before that display changes repeats the same request. All Lights requests
one uniform on or off state for its eligible lamps. Successful power changes
have no popup; the list refreshes from Home Assistant's reported state.

A dimmable light has a **Brightness** softkey. A fan that supports percentages
has **Speed**. Turn the wheel in the centred 0–100 slider. The thumb moves
locally and the newest value is sent after a short pause. **Back** sends any
last unsent value once and returns to the list. The slider keeps the device
selected when it opened, even if the list changes behind it. Brightness zero
turns a light off; fan zero uses Home Assistant's percentage control.

Scenes and scripts live in **Scenes**. Press **OK** to start one. **Started**
appears briefly after Home Assistant accepts the call; it does not mean a script
has finished. Browsing, opening folders and scrolling never run a device action.

The list uses bulb icons for on/off state. Unavailable devices are inert. If
Home Assistant disconnects, an open list shows **Home Assistant unavailable**.
It restores the devices when the connection returns. Devices disappears and
returns automatically while the Music menu is open.

## Which area opens

For a room using the Home Assistant music source, the bridge uses the area on
its media-player entity, then its device's area. A child device can inherit its
parent device's area.

For native Sonos, the bridge matches the speaker's exact Sonos identity to Home
Assistant's Sonos registry and uses that device's area. A stored address alone
is not enough to identify a speaker. Renaming a bridge room does not change its
Home Assistant area.

A room with no resolved area still opens Devices, containing **Other Rooms**.
That folder includes every other area with an eligible device. With more than
one floor in use, areas sit inside floor folders ordered by level and name.
Areas without a floor appear under **No Floor**.

A grouped music zone starts with **All Lights** for the member rooms' combined
areas, then one folder per member room, followed by Other Rooms. Its All Lights
list removes duplicate lamps when rooms share an area. Each member folder keeps
that room's own area controls. Group membership comes from the bridge's current
topology.

## Lights and groups

An area's list starts with **All Lights** when it has at least two individual
lamps. It targets the explicit eligible lamp list, excluding light-group
entities. It shows On when any member is on. Its percentage is the rounded mean
of the members that are on and report brightness.

Home Assistant light groups, including Hue rooms and zones that report their
members, appear before ungrouped lights. Their members move into **Individual
Lights** in each lamp's own area. A group covering exactly all the area's
individual lamps is omitted because All Lights already covers it. That omitted
group does not move lamps out of the main list; only members of the groups that
remain shown move into Individual Lights. A group can affect lamps in other
areas; the group row appears only in the group's assigned area.

Names use the entity registry's custom name, then Home Assistant's friendly
name. A leading area name is removed inside that area. Long names are shortened
so the on/off or percentage text remains visible.

## Configuration

These keys are edited directly in the existing bridge configuration file. The
web UI reports the resolved areas; it does not have a separate area-override
form. After a manual configuration edit, apply it with:

```sh
sudo cr100-bridge restart
```

Changes saved through the existing web UI apply automatically.

| Key | Default | Meaning |
| --- | --- | --- |
| `smart_devices` | Enabled when Home Assistant credentials exist | Set `false` to hide Devices everywhere. |
| `rooms[].devices_area` | Automatic area resolution | A Home Assistant **area ID** overrides the area's automatic lookup for that bridge room. |
| `rooms[].devices_area: ""` | — | An explicit empty string gives the room no local area; Other Rooms remains available. |

For example, these invented room settings choose a particular area:

```json
{
  "smart_devices": true,
  "rooms": [
    {
      "entity": "media_player.studio_example",
      "devices_area": "studio_example"
    }
  ]
}
```

Use the area's ID rather than its display name. Keep the rest of the existing
configuration when editing a room. Removing `devices_area` restores automatic
resolution. A non-existent area ID resolves to no area.

Home Assistant credentials remain configured when switching the music source to
native Sonos. The player service uses one shared Home Assistant connection for
music-related reads and device controls. Applying a source change restarts the
player process, which reconnects using those retained credentials.

## What is included

Only entities assigned to an area, present in Home Assistant's state list, and
not disabled, hidden or marked as configuration/diagnostic entities are shown.
This keeps Sonos loudness and crossfade configuration switches out of Devices.

Supported types are `light`, `switch`, `input_boolean`, `fan`, `scene` and
`script`. This version has no light-colour controls, covers, climate controls,
locks, alarms, buttons, sirens, valves or sensors. It does not change the selected
music backend.

## Troubleshooting

| What you see | Check or try |
| --- | --- |
| No Devices entry | Check the controller firmware, Home Assistant connection, `smart_devices`, and whether any eligible entities have areas. An older bridge without the Devices capability hides the entry. |
| Only Other Rooms | Check the media player's/device's area, the native Sonos identity match, or that room's `devices_area` override. |
| A device is missing | Check its area, entity registry hidden/disabled/category settings, and whether it is a member under Individual Lights. Registry changes can take about a minute to appear. |
| A room shows only All Lights and Individual Lights | Its lamps belong to a light group (often a Hue room) that is assigned to a different area, so the group row appears there and the lamps move into Individual Lights here. Assign the group to this room's area in Home Assistant to get a group row here instead. |
| Home Assistant unavailable | Check Home Assistant reachability and the saved credentials. The bridge reconnects automatically. |
| List changed. Select again. | A name, membership or list structure changed while the row was selected. Move the selection and choose the intended row again. |
| Light not responding / Device not responding | Check the device in Home Assistant and its integration. Inspect its actual state before issuing another request. |
| Device action unavailable / Device action result unavailable | Check the bridge connection and actual state in Home Assistant. A request can reach the bridge even if its reply does not reach the controller. Check whether a scene or script ran before repeating it. |
| Device busy while other devices work | HA may have lost a service reply. The bridge holds that device and overlapping groups until a matching reply arrives. See below. |
| No Brightness or Speed key | The selected entity does not report the corresponding capability, is unavailable, or is a folder/one-shot action. |

The bridge status page shows whether Home Assistant devices are connected, area
and device counts, and the area resolved for each bridge room. Reported state is
authoritative: a busy integration can take a moment to update the controller.

If a device-service reply is lost, **Device busy** can remain for that device and
its overlapping groups while other devices still work. This prevents another
command overlapping an operation whose completion is unknown. A delayed HA
reply clears the hold; reconnecting alone does not. If the reply was lost
permanently, check the actual state in Home Assistant, then restart the bridge
before trying again.
